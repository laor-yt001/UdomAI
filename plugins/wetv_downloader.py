"""
WeTV VIP Downloader Plugin
==========================
Features:
  - Fetch WeTV series/album metadata (cover thumbnail + complete episode list)
  - Supports WeTV URLs (e.g. https://wetv.vip/en/play/air11ooo2rdsdi3-Cute-Programmer)
  - Supports Tencent Video / v.qq.com URLs
  - Multi-select episodes via interactive inline keyboard (paginated, checkboxes)
  - Download selected episodes or all episodes in full HD/MP4 without login (for free episodes)
  - Seamlessly uses WeTV VIP cookies from wetv_cookies.txt, cookies.txt, or COOKIES_WETV env for VIP episodes
  - Download series cover thumbnail
  - Fully compatible with UdomAI Telegram Bot & Web Dashboard
"""

import os
import io
import re
import json
import uuid
import shutil
import asyncio
import subprocess
import time
import urllib.parse
import urllib.request

import requests
import imageio_ffmpeg
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified

# ── Repo layout ───────────────────────────────────────────────────────────────
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT   = os.path.abspath(os.path.join(_PLUGIN_DIR, ".."))
_WETV_LIB    = os.path.join(_REPO_ROOT, "WeTV VIP Downloader", "lib", "wetv.js")

# ── In-memory session stores ──────────────────────────────────────────────────
# { session_id: { url, series, selected: set, page, owner_id, expires_at } }
_wetv_sessions: dict = {}
_wetv_lock = asyncio.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _node_bin():
    return shutil.which("node") or "node"


def _is_wetv_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "wetv.vip" in u or "v.qq.com" in u


def _resolve_wetv_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if not u.startswith("http"):
        if u.startswith("//"):
            u = "https:" + u
        elif "wetv.vip" in u or "v.qq.com" in u:
            u = "https://" + u
    u = u.replace("/album/", "/play/")
    return u


def _get_ffmpeg_dir() -> str:
    try:
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return os.path.dirname(exe)
    except Exception:
        return ""


def _find_wetv_cookies() -> str | None:
    # 1. Use centralized downloader cookie resolver (supports JSON -> Netscape conversion, env vars, etc.)
    try:
        from downloader import _get_cookies_file
        c = _get_cookies_file('wetv')
        if c and os.path.exists(c) and os.path.getsize(c) > 10:
            return c
    except Exception:
        pass

    # 2. Local candidate files
    candidates = [
        os.path.join(_REPO_ROOT, "wetv_cookies.txt"),
        os.path.join(_REPO_ROOT, "cookies.txt"),
        os.path.join(_REPO_ROOT, "cookies_wetv.txt"),
    ]
    for cand in candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 10:
            return cand

    # 3. Env vars fallback — write cookie content to a temp file
    env_wetv = os.environ.get("COOKIES_WETV") or os.environ.get("COOKIES_ALL")
    if env_wetv:
        try:
            from downloader import _sanitize_netscape_cookies
            cleaned = _sanitize_netscape_cookies(env_wetv)
            if not cleaned:
                cleaned = env_wetv
            tmp_cookie = os.path.join(_REPO_ROOT, "wetv_cookies_tmp.txt")
            with open(tmp_cookie, "w", encoding="utf-8") as f:
                f.write(cleaned)
            if os.path.getsize(tmp_cookie) > 10:
                return tmp_cookie
        except Exception:
            pass

    return None


def _sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', " ", str(name or "WeTV Series")).strip()


def _episode_filename(series_name: str, episode_number: int, total_episodes: int = 10) -> str:
    clean = _sanitize_name(series_name)
    pad_len = max(2, len(str(total_episodes or 10)))
    return f"{clean} - EP{str(episode_number).zfill(pad_len)}.mp4"


def _fetch_wetv_series_sync(url: str) -> dict:
    """Fetches series metadata and episode list from WeTV page."""
    url = _resolve_wetv_url(url)
    cookie_file = _find_wetv_cookies()

    # Strategy 1: Direct Python HTTP request & __NEXT_DATA__ parsing (very fast)
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://wetv.vip/",
        }
        if cookie_file and os.path.exists(cookie_file):
            try:
                with open(cookie_file, "r", encoding="utf-8", errors="ignore") as cf:
                    pairs = []
                    for line in cf:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\t")
                        if len(parts) >= 7:
                            dom = parts[0].lower()
                            if "wetv" in dom or "qq.com" in dom or dom == "":
                                pairs.append(f"{parts[5]}={parts[6]}")
                        elif "=" in line:
                            for seg in line.split(";"):
                                seg = seg.strip()
                                if "=" in seg:
                                    pairs.append(seg)
                    if pairs:
                        headers["Cookie"] = "; ".join(pairs)
            except Exception:
                pass

        resp = requests.get(url, headers=headers, timeout=25)
        if resp.status_code == 200:
            m = re.search(r'<script id=[\"\']__NEXT_DATA__[\"\'][^>]*>(.*?)</script>', resp.text, re.DOTALL)
            if m:
                nd = json.loads(m.group(1))
                page_props = nd.get("props", {}).get("pageProps", {})
                raw_data = page_props.get("data", {})
                if isinstance(raw_data, str):
                    try:
                        raw_data = json.loads(raw_data)
                    except Exception:
                        pass
                
                cover_info = raw_data.get("coverInfo") or {}
                series_id = cover_info.get("cid") or ""
                if not series_id:
                    m_cid = re.search(r"/play/([a-zA-Z0-9]+)", url)
                    if m_cid:
                        series_id = m_cid.group(1)

                series_name = cover_info.get("title") or "WeTV Series"
                cover = cover_info.get("posterHz") or cover_info.get("posterVt") or cover_info.get("cover_image") or ""
                if cover.startswith("//"):
                    cover = "https:" + cover
                intro = cover_info.get("description") or ""

                video_list = raw_data.get("videoList", [])
                total_eps = int(cover_info.get("episodeAll") or cover_info.get("episodeUpdated") or len(video_list))

                episodes = []
                for idx, v in enumerate(video_list):
                    vid = str(v.get("vid") or "")
                    if not vid:
                        continue
                    ep_num = idx + 1
                    raw_ep = v.get("episode")
                    if raw_ep and str(raw_ep).isdigit():
                        ep_num = int(raw_ep)

                    is_free = bool(v.get("isFree", False))
                    is_vip = not is_free or v.get("payStatus") in (6, 7)
                    pic = v.get("pic_640_360") or v.get("pic_496_280") or v.get("pic_332_187") or cover
                    if pic.startswith("//"):
                        pic = "https:" + pic
                    play_url = f"https://wetv.vip/en/play/{series_id}/{vid}" if series_id and vid else url

                    episodes.append({
                        "vid": vid,
                        "episode": ep_num,
                        "title": v.get("title") or f"EP{ep_num:02d}: {series_name}",
                        "isFree": is_free,
                        "isVip": is_vip,
                        "duration": int(v.get("duration") or 0),
                        "cover": pic,
                        "playUrl": play_url,
                        "filename": _episode_filename(series_name, ep_num, total_eps or len(video_list)),
                    })

                episodes.sort(key=lambda x: x["episode"])
                return {
                    "seriesId": series_id,
                    "name": series_name,
                    "cover": cover,
                    "intro": intro,
                    "episodeCount": len(episodes),
                    "accessibleCount": len(episodes),
                    "vids": [e["vid"] for e in episodes],
                    "episodes": episodes,
                }
    except Exception as py_err:
        print(f"[wetv] Python metadata extraction failed: {py_err}. Trying Node fallback...")

    # Strategy 2: Node.js wetv.js fallback
    if os.path.exists(_WETV_LIB):
        script = r"""
const wetv = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const series = await wetv.getSeries(url);
  console.log(JSON.stringify(series));
})().catch(e => { process.stderr.write(String(e && e.message ? e.message : e)); process.exit(1); });
"""
        proc = subprocess.run(
            [_node_bin(), "-e", script, _WETV_LIB, url],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout.strip())
        err = proc.stderr.strip() or "Node WeTV extraction failed."
        raise RuntimeError(err)

    raise RuntimeError("Failed to fetch WeTV series metadata.")


def _download_single_wetv_episode_sync(series_id: str, vid: str, dest_path: str, series_name: str = "", play_url: str = None) -> dict:
    """Downloads a single WeTV episode using yt-dlp with cookie support."""
    if not play_url:
        if vid.startswith("http"):
            play_url = vid
        elif series_id:
            play_url = f"https://wetv.vip/en/play/{series_id}/{vid}"
        else:
            play_url = f"https://wetv.vip/en/play/{vid}"

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    ff_dir = _get_ffmpeg_dir()
    if ff_dir:
        os.environ["PATH"] = ff_dir + os.pathsep + os.environ.get("PATH", "")

    cookie_file = _find_wetv_cookies()

    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "outtmpl": dest_path,
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "retries": 5,
        "socket_timeout": 30,
        "sleep_interval_requests": 1,
    }
    if ff_dir:
        opts["ffmpeg_location"] = ff_dir
    if cookie_file and os.path.exists(cookie_file):
        opts["cookiefile"] = cookie_file

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([play_url])

    # If the file ended up with a slightly different path/ext
    if not os.path.exists(dest_path):
        base, _ = os.path.splitext(dest_path)
        for ext in (".mp4", ".mkv", ".ts"):
            if os.path.exists(base + ext):
                dest_path = base + ext
                break

    if os.path.exists(dest_path):
        return {
            "ok": True,
            "bytes": os.path.getsize(dest_path),
            "path": dest_path,
        }
    raise RuntimeError(f"File was not created for {play_url}")


def _download_wetv_batch_sync(url: str, selected_episodes: list, out_dir: str) -> dict:
    """Downloads a batch of WeTV episodes sequentially."""
    series = _fetch_wetv_series_sync(url)
    results = []
    for ep in selected_episodes:
        fn = ep.get("filename") or _episode_filename(series["name"], ep["episode"], series.get("episodeCount", 10))
        dest = os.path.join(out_dir, fn)
        try:
            r = _download_single_wetv_episode_sync(
                series["seriesId"],
                ep["vid"],
                dest,
                series["name"],
                play_url=ep.get("playUrl"),
            )
            results.append({
                "vid": ep["vid"],
                "episode": ep["episode"],
                "path": r.get("path") or dest,
                "ok": True,
                "bytes": r.get("bytes", 0),
            })
        except Exception as e:
            results.append({
                "vid": ep["vid"],
                "episode": ep["episode"],
                "path": dest,
                "ok": False,
                "error": str(e),
            })
        # Small delay between episodes to avoid rate-limiting
        time.sleep(1)

    return {"seriesId": series["seriesId"], "name": series["name"], "results": results}


async def _fetch_wetv_series(url: str) -> dict:
    return await asyncio.to_thread(_fetch_wetv_series_sync, url)


async def _download_wetv_batch(url: str, selected_episodes: list, out_dir: str) -> dict:
    return await asyncio.to_thread(_download_wetv_batch_sync, url, selected_episodes, out_dir)


def _get_temp_dir() -> str:
    from utils import get_temp_dir
    return get_temp_dir()


def _cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


async def _safe_reply(msg: Message, text: str, **kwargs):
    try:
        return await msg.reply_text(text, **kwargs)
    except Exception:
        return None


async def _safe_edit(msg: Message, text: str, **kwargs):
    try:
        return await msg.edit_text(text, **kwargs)
    except (MessageNotModified, Exception):
        return msg


# ── Session helpers ───────────────────────────────────────────────────────────

def _new_session_id() -> str:
    return uuid.uuid4().hex[:10]


async def _get_or_expire_session(sid: str) -> dict | None:
    async with _wetv_lock:
        sess = _wetv_sessions.get(sid)
        if not sess:
            return None
        if sess.get("expires_at", 0) < time.time():
            _wetv_sessions.pop(sid, None)
            return None
        return sess


async def _save_session(sid: str, data: dict):
    async with _wetv_lock:
        data.setdefault("expires_at", time.time() + 3600)
        _wetv_sessions[sid] = data
        now = time.time()
        expired = [k for k, v in _wetv_sessions.items() if v.get("expires_at", 0) < now]
        for k in expired:
            _wetv_sessions.pop(k, None)


# ── Episode selection keyboard ────────────────────────────────────────────────

PAGE_SIZE = 8   # episodes per page


def _build_episode_keyboard(session_id: str, episodes: list, selected: set, page: int) -> InlineKeyboardMarkup:
    """Build a paginated episode selection keyboard."""
    total = len(episodes)
    page_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * PAGE_SIZE
    end   = min(start + PAGE_SIZE, total)

    buttons = []

    # Episode checkboxes — 2 per row
    row: list = []
    for i in range(start, end):
        ep   = episodes[i]
        vid  = ep["vid"]
        num  = ep["episode"]
        is_vip = ep.get("isVip", False)
        vip_tag = "🔒" if is_vip else ""
        icon = "✅" if vid in selected else "⬜"
        label = f"{icon} EP{num:02d} {vip_tag}".strip()
        btn  = InlineKeyboardButton(
            label,
            callback_data=f"wetv|toggle|{session_id}|{vid}|{page}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Pagination row
    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wetv|page|{session_id}|prev|{page}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{page_count}", callback_data=f"wetv|noop|{session_id}|.|{page}"))
        if page < page_count - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"wetv|page|{session_id}|next|{page}"))
        buttons.append(nav)

    # Select all / clear row
    buttons.append([
        InlineKeyboardButton("☑️ Select All",  callback_data=f"wetv|selall|{session_id}|.|{page}"),
        InlineKeyboardButton("🗑 Clear",        callback_data=f"wetv|clear|{session_id}|.|{page}"),
    ])

    # Action row
    sel_count = len(selected)
    buttons.append([
        InlineKeyboardButton(
            f"📥 Download Selected ({sel_count})",
            callback_data=f"wetv|dl_sel|{session_id}|.|{page}",
        ),
    ])
    buttons.append([
        InlineKeyboardButton("📥 Download ALL", callback_data=f"wetv|dl_all|{session_id}|.|{page}"),
        InlineKeyboardButton("🖼 Thumbnail",    callback_data=f"wetv|thumb|{session_id}|.|{page}"),
    ])

    return InlineKeyboardMarkup(buttons)


def _session_text(series: dict, selected: set) -> str:
    total = series.get("episodeCount", len(series.get("episodes", [])))
    has_vip = any(ep.get("isVip") for ep in series.get("episodes", []))
    cookie_file = _find_wetv_cookies()
    cookie_status = "🔑 VIP Cookies: Active" if cookie_file else "💡 Free episodes without login | Add wetv_cookies.txt for VIP"
    return (
        f"🎬 **{series.get('name', 'WeTV Series')}**\n"
        f"📺 {total} episodes  |  ✅ {len(selected)} selected\n"
        f"ℹ️ {cookie_status}\n\n"
        "Tap episodes to toggle ✅/⬜, then press **Download Selected** or **Download ALL**."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bot Commands & Handlers
# ═══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command(["wetv", "vqq"]) & filters.private, group=1)
async def wetv_command(client: Client, message: Message):
    """Usage: /wetv <series_or_play_url>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await _safe_reply(
            message,
            "🎬 **WeTV VIP Downloader**\n\n"
            "Usage:\n"
            "`/wetv <series or episode URL>`\n\n"
            "Example:\n"
            "`/wetv https://wetv.vip/en/play/air11ooo2rdsdi3-Cute-Programmer`\n"
            "`/wetv https://wetv.vip/en/play/u37kgfnfzs73kiu/p0039b9nvik`\n\n"
            "Or simply paste the link in chat!",
        )
        return

    url = args[1].strip()
    await handle_wetv_url(client, message, url)


async def handle_wetv_url(client: Client, message: Message, url: str):
    """Shared entry-point: fetch series and present episode selection."""
    status = await _safe_reply(message, "🔍 Fetching WeTV series info…")
    try:
        series = await _fetch_wetv_series(url)
    except Exception as exc:
        await _safe_edit(status, f"❌ Failed to load WeTV series: {exc}")
        return

    sid = _new_session_id()
    await _save_session(sid, {
        "url": url,
        "series": series,
        "selected": set(),
        "page": 0,
        "owner_id": message.from_user.id if message.from_user else 0,
    })

    episodes = series.get("episodes", [])
    cover_url = series.get("cover", "")

    keyboard = _build_episode_keyboard(sid, episodes, set(), 0)
    caption  = _session_text(series, set())

    if cover_url:
        try:
            await status.delete()
            await client.send_photo(
                chat_id=message.chat.id,
                photo=cover_url,
                caption=caption,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass

    await _safe_edit(status, caption, reply_markup=keyboard)


# ═══════════════════════════════════════════════════════════════════════════════
# Callback Query Handlers
# ═══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^wetv\|"), group=1)
async def wetv_callback(client: Client, query: CallbackQuery):
    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 4:
        return
    _, action, sid = parts[0], parts[1], parts[2]
    vid_or_dot = parts[3] if len(parts) > 3 else "."
    page_str   = parts[4] if len(parts) > 4 else "0"
    page       = int(page_str) if page_str.isdigit() else 0

    sess = await _get_or_expire_session(sid)
    if not sess:
        await query.answer("⏰ Session expired. Please send the URL again.", show_alert=True)
        return

    url      = sess["url"]
    series   = sess["series"]
    episodes = series.get("episodes", [])
    selected: set = sess["selected"]

    msg = query.message

    if action == "noop":
        return

    if action == "toggle":
        vid = vid_or_dot
        if vid in selected:
            selected.discard(vid)
        else:
            selected.add(vid)
        sess["selected"] = selected
        sess["page"]     = page
        await _save_session(sid, sess)
        kb = _build_episode_keyboard(sid, episodes, selected, page)
        try:
            await msg.edit_text(_session_text(series, selected), reply_markup=kb)
        except MessageNotModified:
            pass
        return

    if action == "page":
        direction = vid_or_dot
        page_count = max(1, (len(episodes) + PAGE_SIZE - 1) // PAGE_SIZE)
        if direction == "next":
            page = min(page_count - 1, page + 1)
        else:
            page = max(0, page - 1)
        sess["page"] = page
        await _save_session(sid, sess)
        kb = _build_episode_keyboard(sid, episodes, selected, page)
        try:
            await msg.edit_text(_session_text(series, selected), reply_markup=kb)
        except MessageNotModified:
            pass
        return

    if action == "selall":
        selected = {ep["vid"] for ep in episodes}
        sess["selected"] = selected
        await _save_session(sid, sess)
        kb = _build_episode_keyboard(sid, episodes, selected, page)
        try:
            await msg.edit_text(_session_text(series, selected), reply_markup=kb)
        except MessageNotModified:
            pass
        return

    if action == "clear":
        selected = set()
        sess["selected"] = selected
        await _save_session(sid, sess)
        kb = _build_episode_keyboard(sid, episodes, selected, page)
        try:
            await msg.edit_text(_session_text(series, selected), reply_markup=kb)
        except MessageNotModified:
            pass
        return

    if action == "thumb":
        cover = series.get("cover", "")
        if not cover:
            await query.answer("No cover image available.", show_alert=True)
            return
        try:
            await client.send_photo(
                chat_id=msg.chat.id,
                photo=cover,
                caption=f"🖼 **{series.get('name', 'WeTV')}** — Series Thumbnail",
            )
        except Exception as exc:
            await query.answer(f"Could not send thumbnail: {exc}", show_alert=True)
        return

    if action in ("dl_sel", "dl_all"):
        if action == "dl_all":
            to_download = episodes
        else:
            if not selected:
                await query.answer("⚠️ No episodes selected!", show_alert=True)
                return
            to_download = [ep for ep in episodes if ep["vid"] in selected]

        ep_nums = ", ".join(f"EP{ep['episode']:02d}" for ep in to_download[:5])
        if len(to_download) > 5:
            ep_nums += f" … (+{len(to_download)-5} more)"

        status_text = (
            f"⏳ **Downloading {len(to_download)} episode(s)…**\n"
            f"`{series.get('name', '')}`\n"
            f"{ep_nums}\n\nThis may take several minutes. Please wait…"
        )
        try:
            await msg.edit_text(status_text)
        except MessageNotModified:
            pass

        temp_dir = _get_temp_dir()
        try:
            result = await _download_wetv_batch(url, to_download, temp_dir)
        except Exception as exc:
            await _safe_edit(msg, f"❌ Download failed: {exc}")
            return

        files = [r for r in result.get("results", []) if r.get("ok") and os.path.exists(r.get("path", ""))]
        failed = [r for r in result.get("results", []) if not r.get("ok")]
        if not files:
            errs = [f"EP{r.get('episode', '?')}: {r.get('error', 'unknown')}" for r in failed]
            await _safe_edit(msg, f"❌ No episodes downloaded.\n\n" + "\n".join(errs[:5]))
            return

        await _safe_edit(msg, f"📤 Sending {len(files)} episode(s)…")
        sent = 0
        for item in files:
            ep_num  = item.get("episode", "?")
            ep_path = item.get("path", "")
            if not os.path.exists(ep_path):
                continue
            try:
                await client.send_video(
                    chat_id=msg.chat.id,
                    video=ep_path,
                    caption=f"🎬 EP{ep_num:02d} — {series.get('name', 'WeTV')}",
                    supports_streaming=True,
                )
                sent += 1
            except Exception as send_err:
                print(f"[wetv] Send failed for EP{ep_num}: {send_err}")
            finally:
                _cleanup(ep_path)

        summary = f"✅ Sent {sent}/{len(files)} episode(s) from **{series.get('name', 'WeTV')}**."
        if failed:
            summary += f"\n⚠️ {len(failed)} episode(s) had download errors."
        
        # Check if any downloaded episodes were VIP without cookies
        cookie_file = _find_wetv_cookies()
        if not cookie_file:
            vip_in_batch = [ep for ep in to_download if ep.get("isVip")]
            if vip_in_batch:
                summary += f"\n💡 {len(vip_in_batch)} episode(s) are VIP. To get full VIP quality, place your WeTV VIP cookies into `wetv_cookies.txt` or set `COOKIES_WETV` in .env."

        await _safe_edit(msg, summary)
        return
