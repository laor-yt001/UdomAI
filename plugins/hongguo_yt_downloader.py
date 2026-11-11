"""
Hongguo VIP + YouTube Downloader Plugin
========================================
Features:
  - Fetch Hongguo series metadata (cover thumbnail + episode list)
  - Multi-select episodes via inline keyboard (paginated, checkboxes)
  - Download selected episodes or all episodes
  - Download series cover thumbnail
  - YouTube: download video or thumbnail (no bot-verify via yt-dlp + pytubefix fallback)
  - Works seamlessly with the web dashboard API
"""

import os
import io
import re
import json
import uuid
import shutil
import asyncio
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from pyrogram.errors import MessageNotModified, FloodWait

# ── Repo layout ───────────────────────────────────────────────────────────────
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT   = os.path.abspath(os.path.join(_PLUGIN_DIR, ".."))
_HG_LIB      = os.path.join(_REPO_ROOT, "Houngguo VIP Downloader", "lib", "hongguo.js")

# ── In-memory session stores ──────────────────────────────────────────────────
# { session_id: { url, series, selected_vids: set, page, owner_id, expires_at } }
_hg_sessions: dict = {}
_hg_lock = asyncio.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _node_bin():
    return shutil.which("node") or "node"


def _resolve_hongguo_url(url: str) -> str:
    if not url: return ""
    url = url.strip()
    if "novelquickapp.com" in url.lower():
        try:
            import urllib.request as _ureq
            import re as _re
            req = _ureq.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            with _ureq.urlopen(req, timeout=10) as resp:
                final_url = resp.geturl()
            m = _re.search(r'(?:series_id|video_series_id|video_id)=(\d+)', final_url)
            if m:
                return f"https://hongguoduanju.com/detail?series_id={m.group(1)}"
            return final_url
        except Exception:
            m = _re.search(r'(?:series_id|video_series_id|video_id)=(\d+)', url)
            if m:
                return f"https://hongguoduanju.com/detail?series_id={m.group(1)}"
    return url


def _fetch_hongguo_series_sync(url: str) -> dict:
    """Blocking call to Node.js: fetch series metadata."""
    url = _resolve_hongguo_url(url)
    script = r"""
const hg = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const series = await hg.getSeries(url);
  const episodes = series.vids.map((vid, idx) => ({
    vid,
    episode: idx + 1,
    filename: hg.episodeFilename(series.name, idx + 1, series.episodeCount),
    title: `EP${String(idx+1).padStart(String(series.episodeCount||series.vids.length).length||2,'0')} - ${series.name}`
  }));
  console.log(JSON.stringify({
    seriesId: series.seriesId,
    name: series.name,
    cover: series.cover || '',
    intro: series.intro || '',
    tags: series.tags || [],
    episodeCount: Number(series.episodeCount || episodes.length),
    accessibleCount: Number(series.accessibleCount || episodes.length),
    episodes,
  }));
})().catch(e => { process.stderr.write(String(e)); process.exit(1); });
"""
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    proc = subprocess.run(
        [_node_bin(), "-e", script, _HG_LIB, url],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to fetch Hongguo series.")
    return json.loads(proc.stdout.strip())


def _download_hongguo_batch_sync(url: str, selected_episodes: list, out_dir: str) -> dict:
    """Blocking call to Node.js: download selected episodes."""
    script = r"""
const fs = require('fs');
const path = require('path');
const hg = require(process.argv[1]);
(async () => {
  const url      = process.argv[2];
  const selected = JSON.parse(process.argv[3]);
  const outDir   = process.argv[4];
  const series   = await hg.getSeries(url);
  const results  = [];
  for (const ep of selected) {
    const dest = path.join(outDir, hg.episodeFilename(series.name, ep.episode, series.episodeCount));
    try {
      const r = await hg.downloadEpisode(series.seriesId, ep.vid, dest, { retries: 3 });
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: !!(r && !r.unavailable), bytes: (r && r.bytes) || 0 });
    } catch(e) {
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: false, error: String(e && e.message ? e.message : e) });
    }
  }
  console.log(JSON.stringify({ seriesId: series.seriesId, name: series.name, results }));
})().catch(e => { process.stderr.write(String(e)); process.exit(1); });
"""
    payload = json.dumps(selected_episodes)
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    proc = subprocess.run(
        [_node_bin(), "-e", script, _HG_LIB, url, payload, out_dir],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download Hongguo episodes.")
    return json.loads(proc.stdout.strip())


async def _fetch_hongguo_series(url: str) -> dict:
    return await asyncio.to_thread(_fetch_hongguo_series_sync, url)


async def _download_hongguo_batch(url: str, selected_episodes: list, out_dir: str) -> dict:
    return await asyncio.to_thread(_download_hongguo_batch_sync, url, selected_episodes, out_dir)


def _is_hongguo_url(url: str) -> bool:
    u = (url or "").lower()
    return "hongguoduanju.com" in u or "novelquickapp.com" in u


def _is_youtube_url(url: str) -> bool:
    u = (url or "").lower()
    return "youtube.com" in u or "youtu.be" in u


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
    async with _hg_lock:
        sess = _hg_sessions.get(sid)
        if not sess:
            return None
        if sess.get("expires_at", 0) < time.time():
            _hg_sessions.pop(sid, None)
            return None
        return sess


async def _save_session(sid: str, data: dict):
    async with _hg_lock:
        data.setdefault("expires_at", time.time() + 3600)
        _hg_sessions[sid] = data
    # Prune old sessions
    async with _hg_lock:
        now = time.time()
        expired = [k for k, v in _hg_sessions.items() if v.get("expires_at", 0) < now]
        for k in expired:
            _hg_sessions.pop(k, None)


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
        icon = "✅" if vid in selected else "⬜"
        btn  = InlineKeyboardButton(
            f"{icon} EP{num:02d}",
            callback_data=f"hg2|toggle|{session_id}|{vid}|{page}",
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
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"hg2|page|{session_id}|prev|{page}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{page_count}", callback_data=f"hg2|noop|{session_id}|.|{page}"))
        if page < page_count - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"hg2|page|{session_id}|next|{page}"))
        buttons.append(nav)

    # Select all / clear row
    buttons.append([
        InlineKeyboardButton("☑️ Select All",  callback_data=f"hg2|selall|{session_id}|.|{page}"),
        InlineKeyboardButton("🗑 Clear",        callback_data=f"hg2|clear|{session_id}|.|{page}"),
    ])

    # Action row
    sel_count = len(selected)
    buttons.append([
        InlineKeyboardButton(
            f"📥 Download Selected ({sel_count})",
            callback_data=f"hg2|dl_sel|{session_id}|.|{page}",
        ),
    ])
    buttons.append([
        InlineKeyboardButton("📥 Download ALL", callback_data=f"hg2|dl_all|{session_id}|.|{page}"),
        InlineKeyboardButton("🖼 Thumbnail",    callback_data=f"hg2|thumb|{session_id}|.|{page}"),
    ])

    return InlineKeyboardMarkup(buttons)


def _session_text(series: dict, selected: set) -> str:
    total = series.get("episodeCount", len(series.get("episodes", [])))
    return (
        f"🎬 **{series.get('name', 'Hongguo Series')}**\n"
        f"📺 {total} episodes  |  ✅ {len(selected)} selected\n\n"
        "Tap episodes to toggle ✅/⬜, then press **Download Selected** or **Download ALL**."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bot Commands & Handlers
# ═══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command(["hongguo", "hg"]) & filters.private, group=1)
async def hongguo_command(client: Client, message: Message):
    """Usage: /hongguo <series_url_or_id>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await _safe_reply(
            message,
            "🎬 **Hongguo VIP Downloader**\n\n"
            "Usage:\n"
            "`/hongguo <series URL or series ID>`\n\n"
            "Example:\n"
            "`/hongguo https://hongguoduanju.com/detail?series_id=7704219652255140889`\n\n"
            "Or just paste the URL in chat and the bot will detect it automatically.",
        )
        return

    url = args[1].strip()
    await _handle_hongguo_url(client, message, url)


async def _handle_hongguo_url(client: Client, message: Message, url: str):
    """Shared entry-point: fetch series and present episode selection."""
    status = await _safe_reply(message, "🔍 Fetching Hongguo series info…")
    try:
        series = await _fetch_hongguo_series(url)
    except Exception as exc:
        await _safe_edit(status, f"❌ Failed to load series: {exc}")
        return

    sid = _new_session_id()
    await _save_session(sid, {
        "url": url,
        "series": series,
        "selected": set(),
        "page": 0,
        "owner_id": message.from_user.id,
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


# ── YouTube command ───────────────────────────────────────────────────────────

@Client.on_message(filters.command(["yt", "youtube"]) & filters.private, group=1)
async def youtube_command(client: Client, message: Message):
    """Usage: /yt <youtube_url> [--thumb]"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await _safe_reply(
            message,
            "📺 **YouTube Downloader**\n\n"
            "Usage:\n"
            "`/yt <YouTube URL>` — download video\n"
            "`/yt <YouTube URL> --thumb` — download thumbnail only\n",
        )
        return

    rest     = args[1].strip()
    thumb    = "--thumb" in rest
    url      = rest.replace("--thumb", "").strip()

    if thumb:
        await _handle_yt_thumbnail(client, message, url)
    else:
        await _handle_yt_download(client, message, url)


async def _handle_yt_download(client: Client, message: Message, url: str):
    status = await _safe_reply(message, "📥 Downloading YouTube video…")
    temp_dir = _get_temp_dir()
    try:
        path = await asyncio.to_thread(_yt_download_sync, url, temp_dir)
        await _safe_edit(status, "📤 Sending video…")
        await client.send_video(
            chat_id=message.chat.id,
            video=path,
            supports_streaming=True,
            caption=f"📺 Downloaded from YouTube",
        )
        _cleanup(path)
        await status.delete()
    except Exception as exc:
        await _safe_edit(status, f"❌ YouTube download failed: {exc}")


def _yt_download_sync(url: str, temp_dir: str) -> str:
    """Download YouTube video using yt-dlp (no bot-verify via android/ios/tv clients)."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed.")

    file_id   = uuid.uuid4().hex[:8]
    outtmpl   = os.path.join(temp_dir, f"yt_{file_id}.%(ext)s")
    yt_token  = os.environ.get("YT_OAUTH2_TOKEN")

    # Anti-bot-verify: Use android client without player_skip to avoid missing SABR URLs
    extractor_args = {
        "youtube": {
            "player_client": ["android", "web"],
        }
    }

    base_opts: dict = {
        "outtmpl":        outtmpl,
        "quiet":          True,
        "no_warnings":    True,
        "merge_output_format": "mp4",
        "format":         "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "noplaylist":     True,
        "extractor_args": extractor_args,
        "geo_bypass":     True,
        "source_address": "0.0.0.0",
    }

    if yt_token:
        try:
            decoded = json.loads(
                __import__("base64").b64decode(yt_token + "==").decode()
            )
            if decoded.get("access_token"):
                base_opts["username"] = "oauth2"
                base_opts["password"] = ""
        except Exception:
            pass

    cookies_file = "yt_cookies_for_render.txt"
    if os.path.exists(cookies_file):
        base_opts["cookiefile"] = cookies_file

    import imageio_ffmpeg
    base_opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()

    def _run(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Find actual downloaded file
            if info:
                requested = info.get("requested_downloads")
                if requested:
                    p = requested[0].get("filepath") or requested[0].get("filename")
                    if p and os.path.exists(p):
                        return p
            # Glob fallback
            import glob
            prefix = outtmpl.replace("%(ext)s", "")
            matches = glob.glob(prefix[:-1] + "*") if prefix.endswith(".") else glob.glob(prefix + "*")
            if matches:
                return max(matches, key=os.path.getmtime)
        return None

    path = _run(base_opts)
    if not path:
        # Try bare 'best' fallback
        fallback = dict(base_opts)
        fallback["format"] = "best"
        path = _run(fallback)

    if not path:
        raise RuntimeError("yt-dlp could not download the video.")
    return path


async def _handle_yt_thumbnail(client: Client, message: Message, url: str):
    status = await _safe_reply(message, "🖼 Fetching YouTube thumbnail…")
    try:
        thumb_url = await asyncio.to_thread(_yt_get_thumbnail_url, url)
        if not thumb_url:
            await _safe_edit(status, "❌ Could not find thumbnail for this video.")
            return
        await status.delete()
        await client.send_photo(
            chat_id=message.chat.id,
            photo=thumb_url,
            caption="🖼 YouTube Thumbnail",
        )
    except Exception as exc:
        await _safe_edit(status, f"❌ Thumbnail fetch failed: {exc}")


def _yt_get_thumbnail_url(url: str) -> str | None:
    """Get the best thumbnail URL for a YouTube video."""
    try:
        import yt_dlp
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr", "mweb", "ios", "tv"],
                    "player_skip": ["webpage", "configs"],
                }
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                # Best thumbnail
                thumbs = info.get("thumbnails") or []
                if thumbs:
                    best = max(
                        (t for t in thumbs if t.get("url")),
                        key=lambda t: (t.get("width", 0) * t.get("height", 0)),
                        default=None,
                    )
                    if best:
                        return best["url"]
                return info.get("thumbnail")
    except Exception:
        pass
    # Fallback: construct maxresdefault from video id
    m = re.search(r"(?:v=|be/|/v/|/embed/)([a-zA-Z0-9_-]{11})", url)
    if m:
        vid_id = m.group(1)
        for quality in ["maxresdefault", "sddefault", "hqdefault"]:
            candidate = f"https://i.ytimg.com/vi/{vid_id}/{quality}.jpg"
            try:
                req = urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0"})
                r = urllib.request.urlopen(req, timeout=5)
                if r.status == 200:
                    return candidate
            except Exception:
                continue
    return None


# ── Auto-detect URLs via /hongguo command only ────────────────────────────────
# (Auto URL detection is handled by core.py's text handler in group=0;
#  we avoid duplicate handling by not registering a competing text handler.
#  Users can type /hongguo <url> or /yt <url> explicitly.)


# ═══════════════════════════════════════════════════════════════════════════════
# Callback handlers
# ═══════════════════════════════════════════════════════════════════════════════

@Client.on_callback_query(filters.regex(r"^hg2\|"), group=1)
async def hongguo_callback(client: Client, query: CallbackQuery):
    await query.answer()
    # hg2|<action>|<session_id>|<vid>|<page>
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

    series   = sess["series"]
    episodes = series.get("episodes", [])
    selected: set = sess.get("selected", set())
    url      = sess["url"]
    msg      = query.message

    if action == "noop":
        return

    if action == "toggle":
        vid = vid_or_dot
        if vid in selected:
            selected.discard(vid)
        else:
            selected.add(vid)
        sess["selected"] = selected
        await _save_session(sid, sess)
        kb = _build_episode_keyboard(sid, episodes, selected, page)
        try:
            await msg.edit_text(_session_text(series, selected), reply_markup=kb)
        except MessageNotModified:
            pass
        return

    if action == "page":
        direction = vid_or_dot  # 'next' or 'prev'
        if direction == "next":
            page = min(page + 1, (len(episodes) - 1) // PAGE_SIZE)
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
                caption=f"🖼 **{series.get('name', 'Hongguo')}** — Series Thumbnail",
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
            result = await _download_hongguo_batch(url, to_download, temp_dir)
        except Exception as exc:
            await _safe_edit(msg, f"❌ Download failed: {exc}")
            return

        files = [r for r in result.get("results", []) if r.get("ok") and os.path.exists(r.get("path", ""))]
        if not files:
            errs = [r.get("error", "unknown") for r in result.get("results", []) if not r.get("ok")]
            await _safe_edit(msg, f"❌ No episodes downloaded.\n\nErrors:\n" + "\n".join(errs[:3]))
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
                    caption=f"🎬 EP{ep_num:02d} — {series.get('name', 'Hongguo')}",
                    supports_streaming=True,
                )
                sent += 1
            except Exception as send_err:
                print(f"[hg2] Send failed for EP{ep_num}: {send_err}")
            finally:
                _cleanup(ep_path)

        summary = f"✅ Sent {sent}/{len(files)} episode(s) from **{series.get('name', 'Hongguo')}**."
        if sent < len(files):
            summary += f"\n⚠️ {len(files) - sent} failed to send."
        await _safe_edit(msg, summary)
        return


@Client.on_callback_query(filters.regex(r"^yt2\|"), group=1)
async def youtube_callback(client: Client, query: CallbackQuery):
    await query.answer()
    parts  = query.data.split("|")
    action = parts[1] if len(parts) > 1 else ""
    sid    = parts[2] if len(parts) > 2 else ""
    msg    = query.message

    sess = await _get_or_expire_session(sid)
    if not sess:
        await query.answer("⏰ Session expired. Please send the URL again.", show_alert=True)
        return

    url = sess.get("url", "")

    if action == "thumb":
        await _safe_edit(msg, "🖼 Fetching YouTube thumbnail…")
        try:
            thumb_url = await asyncio.to_thread(_yt_get_thumbnail_url, url)
            if not thumb_url:
                await _safe_edit(msg, "❌ Thumbnail not found.")
                return
            await msg.delete()
            await client.send_photo(
                chat_id=msg.chat.id,
                photo=thumb_url,
                caption="🖼 YouTube Thumbnail",
            )
        except Exception as exc:
            await _safe_edit(msg, f"❌ Thumbnail error: {exc}")
        return

    if action == "dl":
        await _safe_edit(msg, "📥 Downloading YouTube video… (this may take a few minutes)")
        temp_dir = _get_temp_dir()
        try:
            path = await asyncio.to_thread(_yt_download_sync, url, temp_dir)
            await _safe_edit(msg, "📤 Sending video…")
            await client.send_video(
                chat_id=msg.chat.id,
                video=path,
                supports_streaming=True,
                caption="📺 YouTube Video",
            )
            _cleanup(path)
            await msg.delete()
        except Exception as exc:
            await _safe_edit(msg, f"❌ Download failed: {exc}")
        return
