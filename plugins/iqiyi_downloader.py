"""
iQIYI VIP Downloader Plugin
===========================
Features:
  - Fetch iQIYI series/album metadata (cover thumbnail + complete episode list)
  - Supports album links (e.g. https://www.iq.com/album/celestial-above-all-2025-22le6kky0zp?lang=en_us)
  - Supports play links (e.g. https://www.iq.com/play/f51n0mha00?lang=en_us&sh_pltf=4)
  - Multi-select episodes via interactive inline keyboard (paginated, checkboxes)
  - Download selected episodes or all episodes in full HD/MP4 (no PhantomJS required)
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
_IQ_LIB      = os.path.join(_REPO_ROOT, "iQIYI VIP Downloader", "lib", "iqiyi.js")

# ── In-memory session stores ──────────────────────────────────────────────────
# { session_id: { url, series, selected_vids: set, page, owner_id, expires_at } }
_iq_sessions: dict = {}
_iq_lock = asyncio.Lock()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _node_bin():
    return shutil.which("node") or "node"


def _is_iqiyi_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return "iq.com" in u or "iqiyi.com" in u


def _resolve_iqiyi_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    if not u.startswith("http"):
        if u.startswith("//"):
            u = "https:" + u
        elif "iq.com" in u:
            u = "https://" + u
    return u


def _find_iqiyi_cookies() -> str | None:
    """Return path to a Netscape-format cookie file for iQIYI/iq.com.

    Search order:
      1. Centralized cookie resolver from downloader (handles JSON -> Netscape, env vars, etc.)
      2. Local files: iqiyi_cookies.txt, cookies.txt, cookies_iqiyi.txt
      3. Env var COOKIES_IQIYI / COOKIES_ALL -> write sanitized file
    """
    try:
        from downloader import _get_cookies_file
        c = _get_cookies_file('iqiyi')
        if c and os.path.exists(c) and os.path.getsize(c) > 10:
            return c
    except Exception:
        pass

    candidates = [
        os.path.join(_REPO_ROOT, "iqiyi_cookies.txt"),
        os.path.join(_REPO_ROOT, "cookies.txt"),
        os.path.join(_REPO_ROOT, "cookies_iqiyi.txt"),
    ]
    for cand in candidates:
        if os.path.exists(cand) and os.path.getsize(cand) > 10:
            return cand

    for env_key in ("COOKIES_IQIYI", "COOKIES_ALL"):
        env_val = os.environ.get(env_key)
        if env_val:
            try:
                from downloader import _sanitize_netscape_cookies
                cleaned = _sanitize_netscape_cookies(env_val)
                if not cleaned:
                    cleaned = env_val
                tmp = os.path.join(_REPO_ROOT, "iqiyi_cookies_tmp.txt")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(cleaned)
                if os.path.getsize(tmp) > 10:
                    return tmp
            except Exception:
                pass

    return None


def _fetch_iqiyi_series_sync(url: str) -> dict:
    """Blocking call to Node.js: fetch series metadata from iqiyi.js."""
    url = _resolve_iqiyi_url(url)
    cookie_file = _find_iqiyi_cookies()
    script = r"""
const iq = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const series = await iq.getSeries(url);
  console.log(JSON.stringify(series));
})().catch(e => { process.stderr.write(String(e && e.message ? e.message : e)); process.exit(1); });
"""
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if cookie_file:
        env["IQIYI_COOKIE_FILE"] = cookie_file
    proc = subprocess.run(
        [_node_bin(), "-e", script, _IQ_LIB, url],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to fetch iQIYI series.")
    return json.loads(proc.stdout.strip())


def _download_single_iqiyi_episode_sync(series_id: str, vid: str, dest_path: str, series_name: str = "", play_url: str = "") -> dict:
    """Blocking call to Node.js: download a single episode."""
    cookie_file = _find_iqiyi_cookies()
    script = r"""
const iq = require(process.argv[1]);
(async () => {
  const seriesId = process.argv[2];
  const vid      = process.argv[3];
  const dest     = process.argv[4];
  const playUrl  = process.argv[5] || '';
  const opts = playUrl ? { playUrl } : {};
  const res = await iq.downloadEpisode(seriesId, vid, dest, opts);
  console.log(JSON.stringify(res));
})().catch(e => { process.stderr.write(String(e && e.message ? e.message : e)); process.exit(1); });
"""
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if cookie_file:
        env["IQIYI_COOKIE_FILE"] = cookie_file
    proc = subprocess.run(
        [_node_bin(), "-e", script, _IQ_LIB, str(series_id), str(vid), dest_path, str(play_url or "")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1200,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download iQIYI episode.")
    return json.loads(proc.stdout.strip())


def _download_iqiyi_batch_sync(url: str, selected_episodes: list, out_dir: str) -> dict:
    """Blocking call to Node.js: download selected episodes."""
    cookie_file = _find_iqiyi_cookies()
    script = r"""
const path = require('path');
const iq = require(process.argv[1]);
(async () => {
  const url      = process.argv[2];
  const selected = JSON.parse(process.argv[3]);
  const outDir   = process.argv[4];
  const series   = await iq.getSeries(url);
  const results  = [];
  for (const ep of selected) {
    const fn = ep.filename || iq.episodeFilename(series.name, ep.episode, series.episodeCount);
    const dest = path.join(outDir, fn);
    try {
      const r = await iq.downloadEpisode(series.seriesId, ep.vid, dest, { playUrl: ep.playUrl });
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: true, bytes: (r && r.bytes) || 0 });
    } catch(e) {
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: false, error: String(e && e.message ? e.message : e) });
    }
  }
  console.log(JSON.stringify({ seriesId: series.seriesId, name: series.name, results }));
})().catch(e => { process.stderr.write(String(e && e.message ? e.message : e)); process.exit(1); });
"""
    payload = json.dumps(selected_episodes)
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    if cookie_file:
        env["IQIYI_COOKIE_FILE"] = cookie_file
    proc = subprocess.run(
        [_node_bin(), "-e", script, _IQ_LIB, url, payload, out_dir],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=1800,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download iQIYI episodes.")
    return json.loads(proc.stdout.strip())
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download iQIYI episodes.")
    return json.loads(proc.stdout.strip())


async def _fetch_iqiyi_series(url: str) -> dict:
    return await asyncio.to_thread(_fetch_iqiyi_series_sync, url)


async def _download_iqiyi_batch(url: str, selected_episodes: list, out_dir: str) -> dict:
    return await asyncio.to_thread(_download_iqiyi_batch_sync, url, selected_episodes, out_dir)


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
    async with _iq_lock:
        sess = _iq_sessions.get(sid)
        if not sess:
            return None
        if sess.get("expires_at", 0) < time.time():
            _iq_sessions.pop(sid, None)
            return None
        return sess


async def _save_session(sid: str, data: dict):
    async with _iq_lock:
        data.setdefault("expires_at", time.time() + 3600)
        _iq_sessions[sid] = data
        now = time.time()
        expired = [k for k, v in _iq_sessions.items() if v.get("expires_at", 0) < now]
        for k in expired:
            _iq_sessions.pop(k, None)


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
            callback_data=f"iq2|toggle|{session_id}|{vid}|{page}",
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
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"iq2|page|{session_id}|prev|{page}"))
        nav.append(InlineKeyboardButton(f"{page+1}/{page_count}", callback_data=f"iq2|noop|{session_id}|.|{page}"))
        if page < page_count - 1:
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"iq2|page|{session_id}|next|{page}"))
        buttons.append(nav)

    # Select all / clear row
    buttons.append([
        InlineKeyboardButton("☑️ Select All",  callback_data=f"iq2|selall|{session_id}|.|{page}"),
        InlineKeyboardButton("🗑 Clear",        callback_data=f"iq2|clear|{session_id}|.|{page}"),
    ])

    # Action row
    sel_count = len(selected)
    buttons.append([
        InlineKeyboardButton(
            f"📥 Download Selected ({sel_count})",
            callback_data=f"iq2|dl_sel|{session_id}|.|{page}",
        ),
    ])
    buttons.append([
        InlineKeyboardButton("📥 Download ALL", callback_data=f"iq2|dl_all|{session_id}|.|{page}"),
        InlineKeyboardButton("🖼 Thumbnail",    callback_data=f"iq2|thumb|{session_id}|.|{page}"),
    ])
    buttons.append([
        InlineKeyboardButton("🏷️ Stylize Poster Title", callback_data=f"iq2|title|{session_id}|.|{page}")
    ])

    return InlineKeyboardMarkup(buttons)


def _session_text(series: dict, selected: set) -> str:
    total = series.get("episodeCount", len(series.get("episodes", [])))
    return (
        f"🎬 **{series.get('name', 'iQIYI Series')}**\n"
        f"📺 {total} episodes  |  ✅ {len(selected)} selected\n\n"
        "Tap episodes to toggle ✅/⬜, then press **Download Selected** or **Download ALL**."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Bot Commands & Handlers
# ═══════════════════════════════════════════════════════════════════════════════

@Client.on_message(filters.command(["iqiyi", "iq"]) & filters.private, group=1)
async def iqiyi_command(client: Client, message: Message):
    """Usage: /iqiyi <album_or_play_url>"""
    args = message.text.split(None, 1)
    if len(args) < 2:
        await _safe_reply(
            message,
            "🎬 **iQIYI VIP Downloader**\n\n"
            "Usage:\n"
            "`/iqiyi <album or play URL>`\n\n"
            "Example:\n"
            "`/iqiyi https://www.iq.com/album/celestial-above-all-2025-22le6kky0zp?lang=en_us`\n"
            "`/iqiyi https://www.iq.com/play/f51n0mha00?lang=en_us&sh_pltf=4`\n\n"
            "Or just paste the link in chat!",
        )
        return

    url = args[1].strip()
    await handle_iqiyi_url(client, message, url)


async def handle_iqiyi_url(client: Client, message: Message, url: str):
    """Shared entry-point: fetch series and present episode selection."""
    status = await _safe_reply(message, "🔍 Fetching iQIYI series info…")
    try:
        series = await _fetch_iqiyi_series(url)
    except Exception as exc:
        await _safe_edit(status, f"❌ Failed to load iQIYI series: {exc}")
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

@Client.on_callback_query(filters.regex(r"^iq2\|"), group=1)
async def iqiyi_callback(client: Client, query: CallbackQuery):
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
                caption=f"🖼 **{series.get('name', 'iQIYI')}** — Series Thumbnail",
            )
        except Exception as exc:
            await query.answer(f"Could not send thumbnail: {exc}", show_alert=True)
        return

    if action == "title":
        cover = series.get("cover", "")
        series_name = series.get("name", "iQIYI Series")
        if not cover:
            await query.answer("No cover image available to stylize.", show_alert=True)
            return

        status = await _safe_reply(msg, f"🎨 **Stylizing poster for:** `{series_name}`…")
        temp_dir = _get_temp_dir()
        try:
            from poster_styler import redesign_poster_with_translated_title
            # Download cover image
            cover_path = os.path.join(temp_dir, f"iq_cover_{sid}.jpg")
            r_img = urllib.request.urlopen(cover, timeout=15)
            with open(cover_path, "wb") as f_img:
                f_img.write(r_img.read())

            redesign_data = redesign_poster_with_translated_title(
                image_path=cover_path,
                caption_hint=series_name,
                target_lang="km",
                preferred_style="gold_fantasy"
            )
            chosen_title = redesign_data.get("chosen_title") or series_name
            out_styled = redesign_data.get("output_path")

            caption = (
                f"🎬 **{chosen_title}**\n"
                f"🏷️ Original: `{series_name}`\n"
                f"✨ Visual Style: **3D Gold Fantasy**"
            )
            await client.send_photo(chat_id=msg.chat.id, photo=out_styled, caption=caption)
            await status.delete()
            _cleanup(cover_path)
            _cleanup(out_styled)
        except Exception as exc:
            await _safe_edit(status, f"❌ Failed to stylize title: {exc}")
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
            result = await _download_iqiyi_batch(url, to_download, temp_dir)
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
                    caption=f"🎬 EP{ep_num:02d} — {series.get('name', 'iQIYI')}",
                    supports_streaming=True,
                )
                sent += 1
            except Exception as send_err:
                print(f"[iq2] Send failed for EP{ep_num}: {send_err}")
            finally:
                _cleanup(ep_path)

        summary = f"✅ Sent {sent}/{len(files)} episode(s) from **{series.get('name', 'iQIYI')}**."
        if failed:
            vip_fails = [r for r in failed if "VIP" in str(r.get("error", ""))]
            if vip_fails:
                summary += f"\n🔒 {len(vip_fails)} episode(s) require VIP. Add iQIYI VIP cookies into cookies.txt to download VIP episodes."
            other_fails = [r for r in failed if "VIP" not in str(r.get("error", ""))]
            if other_fails:
                summary += f"\n⚠️ {len(other_fails)} episode(s) had download errors."
        await _safe_edit(msg, summary)
        return
