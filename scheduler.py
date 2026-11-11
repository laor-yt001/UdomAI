import os
import json
import threading
import time
from datetime import datetime
import asyncio
from typing import Optional
from pyrogram import Client

import redis

REDIS_URL = os.environ.get("REDIS_URL")
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        # Test connection
        redis_client.ping()
        print("Connected to Redis for scheduler! ✅")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        redis_client = None

if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    SCHEDULES_FILE = "/var/data/schedules.json"
else:
    SCHEDULES_FILE = os.path.join(os.path.dirname(__file__), "schedules.json")
_lock = threading.Lock()

def load_all_schedules():
    with _lock:
        if redis_client:
            try:
                data = redis_client.get("schedules")
                if data:
                    return json.loads(data)
            except Exception as e:
                print(f"Error loading schedules from Redis: {e}")
                
        # Fallback to local file
        if not os.path.exists(SCHEDULES_FILE):
            return {}
        try:
            with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
                # Auto-migrate to Redis if possible
                if redis_client and file_data:
                    try:
                        redis_client.set("schedules", json.dumps(file_data, ensure_ascii=False))
                        print("Migrated local schedules.json to Redis! ✅")
                    except Exception as e:
                        print(f"Error migrating schedules to Redis: {e}")
                return file_data
        except:
            return {}

def save_all_schedules(data):
    with _lock:
        # Save to Redis
        if redis_client:
            try:
                redis_client.set("schedules", json.dumps(data, ensure_ascii=False))
            except Exception as e:
                print(f"Error saving schedules to Redis: {e}")
                
        # Also backup to local file
        try:
            with open(SCHEDULES_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving schedules to file: {e}")

    # Real-time push to GitHub repositories so worker servers receive schedules immediately
    try:
        import github_sync
        github_sync.push_file_to_github(SCHEDULES_FILE, "chore: sync schedules.json [skip ci]")
    except Exception as gh_e:
        print(f"Error syncing schedules.json to GitHub: {gh_e}")

def get_schedules(user_id):
    data = load_all_schedules()
    return data.get(str(user_id), [])

def add_schedule(user_id, schedule_data):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id not in data:
        data[user_id] = []
    
    if 'task_data' in schedule_data and isinstance(schedule_data['task_data'], dict):
        u = schedule_data['task_data'].get('url')
        if u and isinstance(u, str):
            from downloader import classify_url, clean_youtube_url
            if classify_url(u) == 'youtube':
                schedule_data['task_data']['url'] = clean_youtube_url(u)

    import uuid
    schedule_data['id'] = str(uuid.uuid4())
    schedule_data['last_run'] = None
    schedule_data['status'] = 'Pending'
    data[user_id].append(schedule_data)
    save_all_schedules(data)

def delete_schedule(user_id, index):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data and 0 <= index < len(data[user_id]):
        data[user_id].pop(index)
        save_all_schedules(data)

def update_schedule(user_id, index, new_data):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data and 0 <= index < len(data[user_id]):
        if 'task_data' in new_data and isinstance(new_data['task_data'], dict):
            u = new_data['task_data'].get('url')
            if u and isinstance(u, str):
                from downloader import classify_url, clean_youtube_url
                if classify_url(u) == 'youtube':
                    new_data['task_data']['url'] = clean_youtube_url(u)
        old_id = data[user_id][index].get('id')
        new_data['id'] = old_id
        new_data['last_run'] = None
        new_data['status'] = 'Pending'
        data[user_id][index] = new_data
        save_all_schedules(data)

def clear_user_schedules(user_id):
    data = load_all_schedules()
    user_id = str(user_id)
    if user_id in data:
        del data[user_id]
        save_all_schedules(data)

def update_schedule_progress(user_id, task_id, **kwargs):
    try:
        data = load_all_schedules()
        uid = str(user_id)
        if uid in data:
            for t in data[uid]:
                if t.get("id") == task_id:
                    t.update(kwargs)
                    save_all_schedules(data)
                    break
    except Exception as e:
        print(f"Error updating schedule progress: {e}")

# --- Background Task Runner ---
import time
import asyncio

# ── Stop-flag registry ─────────────────────────────────────────────────────────
# Maps task_id -> asyncio.Event that signals cancellation.
# Set by request_stop(); checked by the running coroutine.
_RUNNING_TASKS: dict = {}   # {task_id: asyncio.Event}
_RUNNING_LOCK = threading.Lock()

class CancellationToken:
    """Thin wrapper around asyncio.Event used to cancel a running task."""
    def __init__(self):
        self._event = asyncio.Event()  # set() when stop requested

    def request_stop(self):
        self._event.set()

    def is_stop_requested(self) -> bool:
        return self._event.is_set()

    async def check(self):
        """Raise asyncio.CancelledError if stop has been requested."""
        if self._event.is_set():
            raise asyncio.CancelledError("Stop requested by user.")

def request_stop(task_id: str) -> bool:
    """Signal a running task to stop. Returns True if the task was found."""
    with _RUNNING_LOCK:
        token = _RUNNING_TASKS.get(task_id)
    if token:
        token.request_stop()
        return True
    return False

def is_task_running(task_id: str) -> bool:
    with _RUNNING_LOCK:
        return task_id in _RUNNING_TASKS


class ScheduleAnimator:
    """
    Runs a background asyncio task that edits a Telegram message every 2 seconds
    to show animated dots, so users know the scheduled job is still running.
    Cycles:  ⏳ Running .  →  ⏳ Running ..  →  ⏳ Running ...
    Also checks a CancellationToken and raises CancelledError if set.
    """
    FRAMES = [".", "..", "..."]

    def __init__(self, msg, header: str, interval: float = 2.0,
                 cancel_token: CancellationToken = None, task_id: str = None):
        self.msg = msg
        self.header = header
        self.interval = interval
        self.cancel_token = cancel_token
        self.task_id = task_id
        self._task = None
        self._stopped = False
        self._current_detail = ""   # overwritten by ThrottleProgress

    def set_detail(self, text: str):
        """Called by ThrottleProgress to update the sub-line shown under the header."""
        self._current_detail = text

    async def _run(self):
        from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        frame_idx = 0
        stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"sched_stop|{self.task_id}")]]) if self.task_id else None
        while not self._stopped:
            # Check cancellation before each edit
            if self.cancel_token and self.cancel_token.is_stop_requested():
                raise asyncio.CancelledError("Stop requested by user.")
            dots = self.FRAMES[frame_idx % len(self.FRAMES)]
            frame_idx += 1
            detail = f"\n⏳ {self._current_detail}" if self._current_detail else ""
            try:
                await self.msg.edit_text(
                    f"{self.header}\n"
                    f"🔄 **Running{dots}**"
                    f"{detail}",
                    reply_markup=stop_kb
                )
            except Exception:
                pass
            await asyncio.sleep(self.interval)

    def start(self):
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self, final_text: str = None):
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if final_text:
            try:
                await self.msg.edit_text(final_text, reply_markup=None)
            except Exception:
                pass


class ThrottleProgress:
    def __init__(self, msg, prefix, animator: ScheduleAnimator = None, cancel_token: CancellationToken = None):
        self.msg = msg
        self.prefix = prefix
        self.last_update = 0
        self.last_text = ""
        self.animator = animator
        self.cancel_token = cancel_token
        
    def __call__(self, text_or_cur, total=None):
        if self.cancel_token and self.cancel_token.is_stop_requested():
            from plugins.core import ProcessCancelledException
            raise ProcessCancelledException("Stop requested by user.")
        now = time.time()
        text = str(text_or_cur)
        if total is not None:
            pct = (float(text_or_cur) / float(total)) * 100
            text = f"Uploading... {pct:.1f}%"
            
        is_major_update = "downloading" not in text.lower() and "uploading" not in text.lower()
        
        if text != self.last_text and (now - self.last_update > 2.5 or is_major_update):
            self.last_update = now
            self.last_text = text
            # Feed the text to the animator so it shows it under the header
            if self.animator:
                self.animator.set_detail(text)
            else:
                try:
                    asyncio.create_task(self.msg.edit_text(f"{self.prefix}\n⏳ {text}"))
                except:
                    pass

_TITLE_TRANSLATION_CACHE = {}

def get_translated_title(original_title: str, target_lang: str = "km", src_lang: str = "auto") -> str:
    """
    Translates an original video or series title to the target language (default Khmer 'km')
    using the cinematic dubbing translation engine with caching.
    """
    if not original_title or not str(original_title).strip():
        return ""
    clean_orig = str(original_title).strip()
    tgt = (target_lang or "km").lower().strip()[:2]
    src = (src_lang or "auto").lower().strip()[:2] if src_lang and src_lang != "auto" else "auto"
    cache_key = (clean_orig, tgt, src)
    if cache_key in _TITLE_TRANSLATION_CACHE:
        return _TITLE_TRANSLATION_CACHE[cache_key]

    try:
        from converter import translate_nllb_ct2
        translated = translate_nllb_ct2(clean_orig, src_lang=src, tgt_lang=tgt)
        if translated and translated.strip():
            clean_tr = translated.strip().rstrip(".").rstrip("។").strip()
            _TITLE_TRANSLATION_CACHE[cache_key] = clean_tr or translated.strip()
            return _TITLE_TRANSLATION_CACHE[cache_key]
    except Exception as e:
        logger.warning(f"[Scheduler] Failed to translate title '{clean_orig}': {e}")

    _TITLE_TRANSLATION_CACHE[cache_key] = clean_orig
    return clean_orig


def format_fb_post_text(template: str, original_title: str = "", target_lang: str = "km", src_lang: str = "auto", ep_no: int = None, total_eps: int = None) -> str:
    """
    Replaces template variables in Facebook post text:
    - {${title}} -> {translated_title} (e.g. {${title}} -> {រាត្រីរន្ធត់})
    - ${title} / {title} -> translated_title
    - ${ep} / {ep} -> ep_no
    - ${total} / {total} -> total_eps
    - ${orig_title} / {orig_title} -> original_title
    """
    if not template or not str(template).strip():
        return ""

    text = str(template).strip()
    translated_title = get_translated_title(original_title, target_lang=target_lang, src_lang=src_lang) if original_title else ""

    # Replace title variables:
    # 1. Replace ${title} first (this naturally converts {${title}} to {translated_title})
    if translated_title:
        text = text.replace("${title}", translated_title)
        text = text.replace("${TITLE}", translated_title)
        text = text.replace("{{title}}", translated_title)
        text = text.replace("{$title}", translated_title)
        text = text.replace("{title}", translated_title)
        text = text.replace("{TITLE}", translated_title)

    if original_title:
        orig = str(original_title).strip()
        text = text.replace("${original_title}", orig)
        text = text.replace("${orig_title}", orig)
        text = text.replace("{original_title}", orig)
        text = text.replace("{orig_title}", orig)

    if ep_no is not None:
        ep_str = str(ep_no)
        ep_pad = f"{ep_no:02d}"
        text = text.replace("${ep}", ep_str)
        text = text.replace("${episode}", ep_str)
        text = text.replace("{{ep}}", ep_str)
        text = text.replace("{$ep}", ep_str)
        text = text.replace("{ep}", ep_str)
        text = text.replace("{episode}", ep_str)
        text = text.replace("${ep_pad}", ep_pad)
        text = text.replace("{ep_pad}", ep_pad)

    if total_eps is not None:
        tot_str = str(total_eps)
        text = text.replace("${total}", tot_str)
        text = text.replace("${total_eps}", tot_str)
        text = text.replace("{total}", tot_str)
        text = text.replace("{total_eps}", tot_str)

    return text.strip()


async def _post_dubbed_to_facebook(task_data, user_id_int, media_path, caption="", abs_thumb=None, anim=None, target_app=None, series_name="", ep_no=None, total_eps=None, target_lang="km", source_lang="auto"):
    if not task_data.get("post_to_fb", False):
        return
    if anim: anim.set_detail("Posting to Facebook...")
    from user_manager import get_user_facebook, get_user
    from facebook_util import post_to_facebook

    fb_target = task_data.get("fb_target", "custom")
    raw_fb_text = task_data.get("fb_text")
    clean_target_lang = target_lang or "km"

    # Resolve translated title
    fb_video_title = get_translated_title(series_name, target_lang=clean_target_lang, src_lang=source_lang) if series_name else ""

    # Resolve post text with template variables
    if raw_fb_text and raw_fb_text.strip():
        fb_text = format_fb_post_text(
            raw_fb_text,
            original_title=series_name,
            target_lang=clean_target_lang,
            src_lang=source_lang,
            ep_no=ep_no,
            total_eps=total_eps
        )
    elif fb_video_title:
        if ep_no is not None:
            fb_text = f"🎙 {fb_video_title} — ភាគ {ep_no:02d}"
        else:
            fb_text = f"🎙 {fb_video_title}"
    else:
        fb_text = caption
    tags = task_data.get("tags")
    collaborators = task_data.get("collaborators")

    u = get_user(user_id_int)
    is_super_admin = u and u.get('role') == 'SUPER_ADMIN'
    fb_access_list = []
    if u:
        access = u.get('fb_pages_access', [])
        if isinstance(access, bool):
            fb_access_list = ["aimovie", "livealone"] if access else []
        else:
            fb_access_list = access

    pages_to_post = []
    if fb_target in ["all_system", "all", "aimovie", "livealone"]:
        has_access = is_super_admin or (fb_target in fb_access_list) or (fb_target in ["all_system", "all"] and "aimovie" in fb_access_list and "livealone" in fb_access_list)
        if not has_access:
            if target_app:
                await target_app.send_message(user_id_int, f"❌ You do not have permission to post to Facebook target: {fb_target}")
            return

    if fb_target in ["all_system", "all"]:
        p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
        p2 = os.environ.get("LIVEALONE_PAGE_ID")
        t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
    elif fb_target == "aimovie":
        p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
    elif fb_target == "livealone":
        p = os.environ.get("LIVEALONE_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p and t: pages_to_post.append((p, t, "LiveAlone"))
    elif fb_target == "all_saved":
        try:
            from user_manager import get_user_facebook_pages
            pages = get_user_facebook_pages(user_id_int)
            for idx, p_data in enumerate(pages):
                p = p_data.get("page_id")
                t = p_data.get("page_token")
                p_name = p_data.get("page_name", f"Saved Page {idx+1}")
                if p and t: pages_to_post.append((p, t, p_name))
        except: pass
    elif fb_target.startswith("saved_"):
        try:
            idx = int(fb_target.split("_")[1])
            from user_manager import get_user_facebook_pages
            pages = get_user_facebook_pages(user_id_int)
            if idx < len(pages):
                p = pages[idx].get("page_id")
                t = pages[idx].get("page_token")
                p_name = pages[idx].get("page_name", f"Saved Page {idx+1}")
                if p and t: pages_to_post.append((p, t, p_name))
        except: pass
    else:
        p, t = get_user_facebook(user_id_int)
        if p and t: pages_to_post.append((p, t, "Custom Page"))

    if not pages_to_post:
        if target_app:
            await target_app.send_message(user_id_int, "❌ Facebook credentials missing for Dubbing FB post. Use /setfb to configure.")
        return

    results = []
    for p_id, p_token, p_name in pages_to_post:
        if anim: anim.set_detail(f"Posting to {p_name}...")
        is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, fb_text, media_path, fb_video_title, abs_thumb, tags, collaborators)
        results.append((p_name, is_success, result))

    if target_app:
        msg_text = "\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
        await target_app.send_message(user_id_int, f"Facebook Post Results:\n{msg_text}")


async def execute_task(app, user_id, task):
    task_id = task.get("id")

    # Register a cancellation token so the dashboard can stop this task
    cancel_token = CancellationToken()
    if task_id:
        with _RUNNING_LOCK:
            _RUNNING_TASKS[task_id] = cancel_token

    def update_status(status_msg):
        if not task_id: return
        data = load_all_schedules()
        for t in data.get(str(user_id), []):
            if t.get("id") == task_id:
                t["status"] = status_msg
                save_all_schedules(data)
                break

    async def check_cancel():
        """Raise CancelledError immediately if the user pressed Stop."""
        await cancel_token.check()

    custom_client = None
    target_app = app
    try:
        user_id_int = int(user_id)
        
        task_type = task.get("task_type")
        task_data_str = task.get("data", "{}")
        
        try:
            task_data = json.loads(task_data_str)
        except:
            task_data = {}
            
        # Determine which bot to use
        bot_token_to_use = task_data.get("bot_token")
        if bot_token_to_use == "main_bot":
            bot_token_to_use = None # force main bot
        elif not bot_token_to_use:
            # fallback for old schedules
            from user_manager import get_active_user_bot
            custom_bot = get_active_user_bot(user_id_int)
            bot_token_to_use = custom_bot.get("bot_token") if custom_bot else None

        if bot_token_to_use:
            from custom_bots_manager import get_active_custom_bots
            active_bots = get_active_custom_bots()
            if bot_token_to_use in active_bots:
                target_app = active_bots[bot_token_to_use]
            else:
                api_id = os.environ.get("API_ID")
                api_hash = os.environ.get("API_HASH")
                if api_id and api_hash:
                    try:
                        custom_client = Client(
                            f"custom_bot_{bot_token_to_use[:10]}",
                            bot_token=bot_token_to_use,
                            api_id=int(api_id),
                            api_hash=api_hash,
                            in_memory=True
                        )
                        await custom_client.start()
                        target_app = custom_client
                    except Exception as e:
                        print(f"Failed to start custom bot client for {user_id}: {e}")
                        target_app = app
                    
        # ── Fix: check is_connected properly (it's a property, not plain bool) ──
        def _is_client_connected(client) -> bool:
            if client is None:
                return False
            try:
                return bool(client.is_connected)
            except Exception:
                return False

        # If the passed-in app is not connected yet, wait up to 60s for it to connect
        if not _is_client_connected(target_app):
            import time as _time_mod
            _waited = 0
            _wait_interval = 3
            _max_wait = 60
            print(f"[scheduler] ⏳ Bot client not connected yet — waiting up to {_max_wait}s for task {task_id}...")
            while _waited < _max_wait:
                await asyncio.sleep(_wait_interval)
                _waited += _wait_interval
                if _is_client_connected(target_app):
                    print(f"[scheduler] ✅ Bot client connected after {_waited}s — proceeding with task {task_id}")
                    break
            else:
                # Still not connected — try any active custom bot as fallback
                from custom_bots_manager import get_active_custom_bots
                active_bots = get_active_custom_bots()
                for _cb in active_bots.values():
                    if _is_client_connected(_cb):
                        target_app = _cb
                        print(f"[scheduler] 🔄 Using active custom bot as fallback for task {task_id}")
                        break
                else:
                    print(f"[scheduler] ❌ No connected bot after {_max_wait}s — deferring task {task_id}")
                    update_status("Failed: Bot not connected — task deferred. Please retry.")
                    return

        # Even after wait, do a final connectivity check
        if not _is_client_connected(target_app):
            from custom_bots_manager import get_active_custom_bots
            active_bots = get_active_custom_bots()
            if active_bots:
                target_app = next(iter(active_bots.values()))

        if not target_app:
            print(f"[scheduler] ⚠️ No active Telegram bot client available on this server to execute task {task_id}")
            update_status("Failed: No bot client available on this server.")
            return
                    
        

        
        task_success = False
        
        if "url" in task_data and isinstance(task_data["url"], str):
            from downloader import classify_url, clean_youtube_url
            if classify_url(task_data["url"]) == 'youtube':
                task_data["url"] = clean_youtube_url(task_data["url"])

        if task_type == "send_message":
            msg = await target_app.send_message(user_id_int, "🕐 **Scheduled Message**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "🕐 **Scheduled Message**", cancel_token=cancel_token, task_id=task_id)
            anim.start()
            try:
                content = task_data.get("message", "Hello! This is your scheduled automated message.")
                await target_app.send_message(user_id_int, content)
                task_success = True
                await anim.stop("✅ Scheduled message sent!")
            except Exception as e:
                await anim.stop(f"❌ Failed to send message: {e}")
            
        elif task_type == "daily_summary":
            msg = await target_app.send_message(user_id_int, "📊 **Daily Summary**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "📊 **Daily Summary**", cancel_token=cancel_token, task_id=task_id)
            anim.start()
            try:
                summary = "📊 **Daily Bot Summary**\nYour scheduled task ran successfully.\nNo new downloads today."
                await target_app.send_message(user_id_int, summary)
                task_success = True
                await anim.stop("✅ Daily summary sent!")
            except Exception as e:
                await anim.stop(f"❌ Failed: {e}")
            
        elif task_type == "download":
            url = task_data.get("url", "")
            prefix = f"📥 **Scheduled Download**\n🔗 {url}"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token, task_id=task_id)
            anim.start()
            import asyncio, os, re
            from downloader import download_media
            
            prog = ThrottleProgress(msg, prefix, anim, cancel_token=cancel_token)
            from plugins.core import _is_hongguo_series_url, _fetch_hongguo_series, _download_single_hongguo_episode_sync, resolve_hongguo_url
            from plugins.iqiyi_downloader import _is_iqiyi_url, _fetch_iqiyi_series_sync, _download_single_iqiyi_episode_sync
            from plugins.wetv_downloader import _is_wetv_url, _fetch_wetv_series_sync, _download_single_wetv_episode_sync
            if _is_hongguo_series_url(url) or _is_iqiyi_url(url) or _is_wetv_url(url):
                is_iq = _is_iqiyi_url(url)
                is_wetv = _is_wetv_url(url)
                if not is_iq and not is_wetv:
                    url = resolve_hongguo_url(url)
                try:
                    from utils import get_temp_dir
                    if is_wetv:
                        series = await asyncio.to_thread(_fetch_wetv_series_sync, url)
                        series_name = series.get("name", "WeTV series")
                    elif is_iq:
                        series = await asyncio.to_thread(_fetch_iqiyi_series_sync, url)
                        series_name = series.get("name", "iQIYI series")
                    else:
                        series = await asyncio.to_thread(_fetch_hongguo_series, url)
                        series_name = series.get("name", "Hongguo series")
                    series_id = str(series.get("seriesId", ""))
                    episodes = series.get("episodes", [])
                    clean_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()

                    # Check if specific episode requested in URL
                    m_vid = re.search(r'(?:[?&](?:vid|video_id)=|/play/[^/]+/)([a-zA-Z0-9]+)', url)
                    if m_vid:
                        target_vid = m_vid.group(1).split('-')[0]
                        episodes = [ep for ep in episodes if str(ep.get("vid")) == target_vid or target_vid in str(ep.get("playUrl", ""))] or episodes[:1]

                    total = len(episodes)
                    temp_dir = get_temp_dir()
                    if is_wetv:
                        dl_single_ep = _download_single_wetv_episode_sync
                    elif is_iq:
                        dl_single_ep = _download_single_iqiyi_episode_sync
                    else:
                        dl_single_ep = _download_single_hongguo_episode_sync

                    import task_resumer
                    cp = task_resumer.get_checkpoint(task_id) or {}
                    completed_vids = set(task.get("completed_vids") or cp.get("completed_vids") or [])
                    sent = len([ep for ep in episodes if str(ep.get("vid")) in completed_vids])

                    if completed_vids:
                        anim.set_detail(f"Resuming: {sent}/{total} already completed...")

                    merge_parts = int(task_data.get("merge_parts", 1))
                    if merge_parts > 1 and total > 1:
                        from utils import partition_episodes, merge_video_files
                        batches = partition_episodes(episodes, merge_parts)
                        num_batches = len(batches)
                        for b_idx, batch_eps in enumerate(batches, 1):
                            if cancel_token.is_stop_requested():
                                raise asyncio.CancelledError("Stop requested by user.")
                            batch_vids = [str(ep.get("vid")) for ep in batch_eps]
                            if all(v in completed_vids for v in batch_vids):
                                continue

                            start_ep = batch_eps[0].get("episode", 1)
                            end_ep = batch_eps[-1].get("episode", len(batch_eps))
                            part_name = f"Part {b_idx:02d} (EP{start_ep:02d}-EP{end_ep:02d})"
                            merged_filename = f"{clean_name} - {part_name}.mp4"
                            merged_dest = os.path.join(temp_dir, merged_filename)

                            raw_paths = []
                            try:
                                for ep in batch_eps:
                                    if cancel_token.is_stop_requested():
                                        raise asyncio.CancelledError("Stop requested by user.")
                                    ep_no = ep.get("episode", 1)
                                    vid = str(ep.get("vid"))
                                    raw_ep_file = f"{clean_name} - raw_b{b_idx}_EP{ep_no:02d}.mp4"
                                    raw_ep_path = os.path.join(temp_dir, raw_ep_file)
                                    anim.set_detail(f"Downloading {part_name} - EP{ep_no:02d}...")
                                    dl_res = await asyncio.to_thread(dl_single_ep, series_id, vid, raw_ep_path, series_name)
                                    if dl_res.get("ok") and os.path.exists(raw_ep_path):
                                        raw_paths.append(raw_ep_path)
                                    else:
                                        print(f"[scheduled hg batch dl fail] {ep_no}: {dl_res}")

                                if raw_paths:
                                    anim.set_detail(f"Merging {part_name} ({len(raw_paths)} episodes)...")
                                    merged_res = await asyncio.to_thread(merge_video_files, raw_paths, merged_dest)
                                    for p in raw_paths:
                                        cleanup_file(p)

                                    if cancel_token.is_stop_requested():
                                        cleanup_file(merged_dest)
                                        raise asyncio.CancelledError("Stop requested by user.")

                                    if merged_res and os.path.exists(merged_dest):
                                        anim.set_detail(f"Uploading {part_name} ({b_idx}/{num_batches})...")
                                        await target_app.send_video(
                                            user_id_int,
                                            merged_dest,
                                            caption=f"🎬 **{series_name}** — {part_name}",
                                            file_name=merged_filename,
                                            supports_streaming=True
                                        )
                                        for v in batch_vids:
                                            completed_vids.add(v)
                                        task["completed_vids"] = list(completed_vids)
                                        task_resumer.mark_episode_completed(task_id, batch_vids[-1], {
                                            "user_id": user_id_int,
                                            "task_type": "download",
                                            "series_name": series_name,
                                            "total": total,
                                            "url": url,
                                        })
                                        update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Running")
                                        sent += len(batch_eps)
                                        cleanup_file(merged_dest)
                            finally:
                                for p in raw_paths:
                                    cleanup_file(p)
                                cleanup_file(merged_dest)

                        task_success = True
                        task_resumer.remove_checkpoint(task_id)
                        update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Completed", last_run="DONE")
                        await anim.stop(f"✅ Downloaded & Merged {sent}/{total} episode(s) into {num_batches} part(s) from **{series_name}**!")
                        return

                    for i, ep in enumerate(episodes, 1):
                        if cancel_token.is_stop_requested():
                            raise asyncio.CancelledError("Stop requested by user.")

                        ep_no = ep.get("episode", i)
                        vid = str(ep.get("vid"))
                        if vid in completed_vids:
                            continue

                        dest_filename = f"{clean_name} - EP{ep_no:02d}.mp4"
                        dest_path = os.path.join(temp_dir, dest_filename)

                        anim.set_detail(f"Downloading EP{ep_no:02d}/{total}...")
                        dl_res = await asyncio.to_thread(dl_single_ep, series_id, vid, dest_path, series_name)
                        
                        if cancel_token.is_stop_requested():
                            try:
                                if os.path.exists(dest_path): os.remove(dest_path)
                            except Exception: pass
                            raise asyncio.CancelledError("Stop requested by user.")

                        if dl_res.get("ok") and os.path.exists(dest_path):
                            anim.set_detail(f"Uploading EP{ep_no:02d}/{total} to Telegram...")
                            try:
                                await target_app.send_video(
                                    user_id_int,
                                    dest_path,
                                    caption=f"🎬 **{series_name}** — EP{ep_no:02d}",
                                    file_name=dest_filename,
                                    supports_streaming=True
                                )
                                sent += 1
                                completed_vids.add(vid)
                                task["completed_vids"] = list(completed_vids)
                                task_resumer.mark_episode_completed(task_id, vid, {
                                    "user_id": user_id_int,
                                    "task_type": "download",
                                    "series_name": series_name,
                                    "total": total,
                                    "url": url,
                                })
                                update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Running")
                            except Exception as _se:
                                print(f"[scheduled hg send] {ep_no}: {_se}")
                            finally:
                                try: os.remove(dest_path)
                                except: pass
                        else:
                            print(f"[scheduled hg dl fail] {ep_no}: {dl_res}")

                    task_success = True
                    task_resumer.remove_checkpoint(task_id)
                    update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Completed", last_run="DONE")
                    await anim.stop(f"✅ Sent {sent}/{total} episode(s) from **{series_name}**!")
                except Exception as hg_err:
                    await anim.stop(f"❌ Hongguo download failed: {hg_err}")
            elif url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if not _is_hongguo_series_url(url):
                if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                    try:
                        await target_app.send_video(user_id_int, input_path, progress=prog)
                    except:
                        await target_app.send_document(user_id_int, input_path, progress=prog)
                    if os.path.exists(input_path): os.remove(input_path)
                    task_success = True
                    await anim.stop("✅ Download complete!")
                else:
                    await anim.stop(f"❌ Failed to download: {input_path}")
            
        elif task_type == "clip":
            url = task_data.get("url", "")
            mode = task_data.get("clip_mode", "parts")
            thumb = task_data.get("thumbnail")
            prefix = f"✂️ **Scheduled Video Clip**\n🔗 {url}"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token, task_id=task_id)
            anim.start()
            
            import asyncio, os
            from downloader import download_media
            from converter import clip_video_into_parts, clip_video_by_duration, prepare_telegram_thumbnail
            from utils import cleanup_file
            
            prog = ThrottleProgress(msg, prefix, anim, cancel_token=cancel_token)
            if url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                if thumb and thumb.startswith("uploads/"):
                    if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                        abs_thumb = os.path.join("/var/data", thumb)
                    else:
                        abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                else:
                    abs_thumb = thumb
                tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb) if abs_thumb and os.path.exists(abs_thumb) else None
                clips = []
                try:
                    if mode == "parts":
                        val = int(task_data.get("parts", "2") or "2")
                        clips = await asyncio.to_thread(clip_video_into_parts, input_path, val, 0, prog)
                    elif mode == "duration":
                        val = int(task_data.get("duration", "60") or "60")
                        clips = await asyncio.to_thread(clip_video_by_duration, input_path, val, 0, prog)
                    elif mode == "start_end":
                        start = task_data.get("start", "00:00:00")
                        end = task_data.get("end", "00:01:00")
                        out = input_path + "_clip.mp4"
                        import subprocess
                        subprocess.run(["ffmpeg", "-y", "-i", input_path, "-ss", start, "-to", end, "-c", "copy", out], capture_output=True)
                        if os.path.exists(out): clips = [out]
                except Exception as e:
                    await anim.stop(f"❌ Error during clipping: {e}")
                
                cleanup_file(input_path)
                if clips:
                    all_clips = []
                    if isinstance(clips, dict):
                        if clips.get("intro"): all_clips.append(clips["intro"])
                        if clips.get("main_clips"): all_clips.extend(clips["main_clips"])
                        if clips.get("outro"): all_clips.append(clips["outro"])
                    else:
                        all_clips = clips
                    for c in all_clips:
                        send_kwargs = {}
                        if tg_thumb and os.path.exists(tg_thumb):
                            send_kwargs["thumb"] = tg_thumb
                        try:
                            await target_app.send_video(user_id_int, c, progress=prog, **send_kwargs)
                        except:
                            await target_app.send_document(user_id_int, c, progress=prog)
                        cleanup_file(c)
                    task_success = True
                    await anim.stop("✅ Clipping complete!")
                else:
                    await anim.stop("❌ Failed to generate clips.")
                
                if tg_thumb: cleanup_file(tg_thumb)
            else:
                await anim.stop("❌ Failed to download source media.")
            
        elif task_type == "dubb":
            url = task_data.get("url", "")
            source_lang = task_data.get("source_lang", "auto")
            lang = task_data.get("lang", "km")
            keep_bgm = task_data.get("need_bg_sound", True)
            thumb = task_data.get("thumbnail")
            whisper_model = task_data.get("whisper_model") or "medium"
            tts_backend = task_data.get("voice_engine") or task_data.get("tts_backend") or "edge-tts"
            transcribe_engine = task_data.get("transcribe_engine")
            translate_engine = task_data.get("translate_engine")
            voxcpm_model = task_data.get("voxcpm_model")
            
            model_info = f" ({voxcpm_model.split('/')[-1]})" if (voxcpm_model and tts_backend in ('voxcpm', 'voxcpm2')) else ""
            prefix = f"🎙 **Scheduled AI Dubbing**\n🔗 {url}\n⚙️ Engine: `{tts_backend}`{model_info} | Whisper: `{whisper_model}`"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token, task_id=task_id)
            anim.start()
            
            import asyncio, os, re
            from downloader import download_media
            from converter import translate_and_dub_media, prepare_telegram_thumbnail
            from utils import cleanup_file
            
            prog = ThrottleProgress(msg, prefix, anim, cancel_token=cancel_token)
            from plugins.core import _is_hongguo_series_url, _fetch_hongguo_series, _download_single_hongguo_episode_sync, resolve_hongguo_url
            from plugins.iqiyi_downloader import _is_iqiyi_url, _fetch_iqiyi_series_sync, _download_single_iqiyi_episode_sync
            from plugins.wetv_downloader import _is_wetv_url, _fetch_wetv_series_sync, _download_single_wetv_episode_sync
            if _is_hongguo_series_url(url) or _is_iqiyi_url(url) or _is_wetv_url(url):
                is_iq = _is_iqiyi_url(url)
                is_wetv = _is_wetv_url(url)
                if not is_iq and not is_wetv:
                    url = resolve_hongguo_url(url)
                try:
                    from utils import get_temp_dir
                    if is_wetv:
                        series = await asyncio.to_thread(_fetch_wetv_series_sync, url)
                        series_name = series.get("name", "WeTV series")
                    elif is_iq:
                        series = await asyncio.to_thread(_fetch_iqiyi_series_sync, url)
                        series_name = series.get("name", "iQIYI series")
                    else:
                        series = await asyncio.to_thread(_fetch_hongguo_series, url)
                        series_name = series.get("name", "Hongguo series")
                    series_id = str(series.get("seriesId", ""))
                    episodes = series.get("episodes", [])
                    clean_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()

                    m_vid = re.search(r'(?:[?&](?:vid|video_id)=|/play/[^/]+/)([a-zA-Z0-9]+)', url)
                    if m_vid:
                        target_vid = m_vid.group(1).split('-')[0]
                        episodes = [ep for ep in episodes if str(ep.get("vid")) == target_vid or target_vid in str(ep.get("playUrl", ""))] or episodes[:1]

                    total = len(episodes)
                    temp_dir = get_temp_dir()
                    if is_wetv:
                        dl_single_ep = _download_single_wetv_episode_sync
                    elif is_iq:
                        dl_single_ep = _download_single_iqiyi_episode_sync
                    else:
                        dl_single_ep = _download_single_hongguo_episode_sync

                    import task_resumer
                    cp = task_resumer.get_checkpoint(task_id) or {}
                    completed_vids = set(task.get("completed_vids") or cp.get("completed_vids") or [])
                    sent = len([ep for ep in episodes if str(ep.get("vid")) in completed_vids])

                    if completed_vids:
                        anim.set_detail(f"Resuming dubbing: {sent}/{total} already completed...")

                    if thumb and thumb.startswith("uploads/"):
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            abs_thumb = os.path.join("/var/data", thumb)
                        else:
                            abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                    else:
                        abs_thumb = thumb
                    tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb) if abs_thumb and os.path.exists(abs_thumb) else None

                    merge_parts = int(task_data.get("merge_parts", 1))
                    if merge_parts > 1 and total > 1:
                        from utils import partition_episodes, merge_video_files
                        batches = partition_episodes(episodes, merge_parts)
                        num_batches = len(batches)
                        for b_idx, batch_eps in enumerate(batches, 1):
                            if cancel_token.is_stop_requested():
                                raise asyncio.CancelledError("Stop requested by user.")
                            batch_vids = [str(ep.get("vid")) for ep in batch_eps]
                            if all(v in completed_vids for v in batch_vids):
                                continue

                            start_ep = batch_eps[0].get("episode", 1)
                            end_ep = batch_eps[-1].get("episode", len(batch_eps))
                            part_name = f"Part {b_idx:02d} (EP{start_ep:02d}-EP{end_ep:02d})"
                            merged_raw_filename = f"{clean_name} - {part_name}_raw.mp4"
                            merged_raw_dest = os.path.join(temp_dir, merged_raw_filename)
                            merged_dubbed_filename = f"{clean_name} - {part_name}_dubbed_{lang.upper()}.mp4"

                            raw_paths = []
                            dubbed_part = None
                            try:
                                # 1. Download all raw episodes in this batch
                                for ep in batch_eps:
                                    if cancel_token.is_stop_requested():
                                        raise asyncio.CancelledError("Stop requested by user.")
                                    ep_no = ep.get("episode", 1)
                                    vid = str(ep.get("vid"))
                                    raw_ep_file = f"{clean_name} - raw_b{b_idx}_EP{ep_no:02d}.mp4"
                                    raw_ep_path = os.path.join(temp_dir, raw_ep_file)
                                    anim.set_detail(f"Downloading {part_name} - EP{ep_no:02d}/{end_ep}...")
                                    dl_res = await asyncio.to_thread(dl_single_ep, series_id, vid, raw_ep_path, series_name)
                                    if dl_res.get("ok") and os.path.exists(raw_ep_path):
                                        raw_paths.append(raw_ep_path)
                                    else:
                                        print(f"[scheduled hg batch dl fail] {ep_no}: {dl_res}")

                                if cancel_token.is_stop_requested():
                                    raise asyncio.CancelledError("Stop requested by user.")

                                if not raw_paths:
                                    print(f"[scheduled hg batch] No raw episodes downloaded for {part_name}")
                                    continue

                                # 2. Merge raw episodes FIRST into this single part
                                anim.set_detail(f"Merging {part_name} ({len(raw_paths)} episodes) before dubbing...")
                                merge_ok = await asyncio.to_thread(merge_video_files, raw_paths, merged_raw_dest)
                                for p in raw_paths:
                                    cleanup_file(p)
                                raw_paths = []

                                if cancel_token.is_stop_requested():
                                    cleanup_file(merged_raw_dest)
                                    raise asyncio.CancelledError("Stop requested by user.")

                                if not (merge_ok and os.path.exists(merged_raw_dest)):
                                    print(f"[scheduled hg batch] Raw video merge failed for {part_name}")
                                    continue

                                # 3. Dub the merged video part
                                anim.set_detail(f"🎙️ Dubbing merged {part_name} ({lang.upper()})...")
                                dubbed_part = await asyncio.to_thread(
                                    translate_and_dub_media,
                                    merged_raw_dest, lang, source_lang, True, prog, thumb, keep_bgm, tts_backend, whisper_model,
                                    transcribe_engine, translate_engine, voxcpm_model
                                )
                                cleanup_file(merged_raw_dest)

                                if cancel_token.is_stop_requested():
                                    if dubbed_part: cleanup_file(dubbed_part)
                                    raise asyncio.CancelledError("Stop requested by user.")

                                # 4. When dubbing already send to user
                                if dubbed_part and not dubbed_part.startswith("ERROR") and os.path.exists(dubbed_part):
                                    anim.set_detail(f"Uploading Dubbed {part_name} ({b_idx}/{num_batches})...")
                                    send_kwargs = {}
                                    if tg_thumb and os.path.exists(tg_thumb): send_kwargs["thumb"] = tg_thumb
                                    part_caption = f"🎙 **{series_name}** — {part_name} Dubbed ({lang.upper()})"
                                    await target_app.send_video(
                                        user_id_int,
                                        dubbed_part,
                                        caption=part_caption,
                                        file_name=merged_dubbed_filename,
                                        supports_streaming=True,
                                        **send_kwargs
                                    )

                                    if task_data.get("post_to_fb", False):
                                        await _post_dubbed_to_facebook(
                                            task_data, user_id_int, dubbed_part,
                                            caption=part_caption, abs_thumb=abs_thumb, anim=anim, target_app=target_app,
                                            series_name=series_name, ep_no=b_idx, total_eps=num_batches, target_lang=lang, source_lang=source_lang
                                        )

                                    # 5. Save progress, clean up, and than do the next part!
                                    for v in batch_vids:
                                        completed_vids.add(v)
                                    task["completed_vids"] = list(completed_vids)
                                    task_resumer.mark_episode_completed(task_id, batch_vids[-1], {
                                        "user_id": user_id_int,
                                        "task_type": "dubb",
                                        "series_name": series_name,
                                        "total": total,
                                        "url": url,
                                    })
                                    update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Running")
                                    sent += len(batch_eps)
                                    cleanup_file(dubbed_part)
                                    dubbed_part = None
                                else:
                                    print(f"[scheduled hg batch dubb fail] {part_name}: {dubbed_part}")
                            finally:
                                for p in raw_paths:
                                    cleanup_file(p)
                                cleanup_file(merged_raw_dest)
                                if dubbed_part:
                                    cleanup_file(dubbed_part)

                        if tg_thumb: cleanup_file(tg_thumb)
                        task_success = True
                        task_resumer.remove_checkpoint(task_id)
                        update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Completed", last_run="DONE")
                        await anim.stop(f"✅ Dubbed & Merged {sent}/{total} episode(s) into {num_batches} part(s) from **{series_name}**!")
                        return

                    for i, ep in enumerate(episodes, 1):
                        if cancel_token.is_stop_requested():
                            raise asyncio.CancelledError("Stop requested by user.")

                        ep_no = ep.get("episode", i)
                        vid = str(ep.get("vid"))
                        if vid in completed_vids:
                            continue

                        raw_filename = f"{clean_name} - EP{ep_no:02d}.mp4"
                        raw_path = os.path.join(temp_dir, raw_filename)

                        anim.set_detail(f"Downloading EP{ep_no:02d}/{total} for dubbing...")
                        dl_res = await asyncio.to_thread(dl_single_ep, series_id, vid, raw_path, series_name)
                        
                        if cancel_token.is_stop_requested():
                            try:
                                if os.path.exists(raw_path): os.remove(raw_path)
                            except Exception: pass
                            raise asyncio.CancelledError("Stop requested by user.")

                        if dl_res.get("ok") and os.path.exists(raw_path):
                            anim.set_detail(f"Dubbing EP{ep_no:02d}/{total} ({lang.upper()})...")
                            dubbed_path = await asyncio.to_thread(
                                translate_and_dub_media,
                                raw_path, lang, source_lang, True, prog, thumb, keep_bgm, tts_backend, whisper_model,
                                transcribe_engine, translate_engine, voxcpm_model
                            )
                            cleanup_file(raw_path)

                            if cancel_token.is_stop_requested():
                                if dubbed_path: cleanup_file(dubbed_path)
                                raise asyncio.CancelledError("Stop requested by user.")

                            if dubbed_path and not dubbed_path.startswith("ERROR") and os.path.exists(dubbed_path):
                                anim.set_detail(f"Uploading Dubbed EP{ep_no:02d}/{total}...")
                                send_kwargs = {}
                                if tg_thumb and os.path.exists(tg_thumb): send_kwargs["thumb"] = tg_thumb
                                ep_caption = f"🎙 **{series_name}** — EP{ep_no:02d} Dubbed ({lang.upper()})"
                                dubbed_filename = f"{clean_name} - EP{ep_no:02d}_dubbed_{lang.upper()}.mp4"

                                try:
                                    await target_app.send_video(
                                        user_id_int,
                                        dubbed_path,
                                        caption=ep_caption,
                                        file_name=dubbed_filename,
                                        supports_streaming=True,
                                        **send_kwargs
                                    )
                                    sent += 1
                                    completed_vids.add(vid)
                                    task["completed_vids"] = list(completed_vids)
                                    task_resumer.mark_episode_completed(task_id, vid, {
                                        "user_id": user_id_int,
                                        "task_type": "dubb",
                                        "series_name": series_name,
                                        "total": total,
                                        "url": url,
                                    })
                                    update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Running")
                                except Exception as _se:
                                    print(f"[scheduled hg dubb send] {ep_no}: {_se}")

                                if task_data.get("post_to_fb", False):
                                    await _post_dubbed_to_facebook(
                                        task_data, user_id_int, dubbed_path,
                                        caption=ep_caption, abs_thumb=abs_thumb, anim=anim, target_app=target_app,
                                        series_name=series_name, ep_no=ep_no, total_eps=total, target_lang=lang, source_lang=source_lang
                                    )

                                cleanup_file(dubbed_path)
                            else:
                                print(f"[scheduled hg dubb fail] {ep_no}: {dubbed_path}")
                        else:
                            print(f"[scheduled hg dl fail for dubb] {ep_no}: {dl_res}")

                    if tg_thumb: cleanup_file(tg_thumb)
                    task_success = True
                    task_resumer.remove_checkpoint(task_id)
                    update_schedule_progress(user_id_int, task_id, completed_vids=list(completed_vids), status="Completed", last_run="DONE")
                    await anim.stop(f"✅ Dubbed & Sent {sent}/{total} episode(s) from **{series_name}**!")
                    return
                except Exception as hg_err:
                    await anim.stop(f"❌ Hongguo dubbing failed: {hg_err}")
                    return
            elif url.startswith("uploads/"):
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    input_path = os.path.join("/var/data", url)
                else:
                    input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
            else:
                from plugins.core import is_telegram_link, download_telegram_post_media
                if is_telegram_link(url):
                    input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                else:
                    dl_res = await asyncio.to_thread(download_media, url, False, prog)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
            
            if input_path and not input_path.startswith("ERROR") and input_path not in ["BOT_DETECTED", "TOO_LARGE"]:
                try:
                    output_path = await asyncio.to_thread(
                        translate_and_dub_media,
                        input_path, lang, source_lang, True, prog, thumb, keep_bgm, tts_backend, whisper_model,
                        transcribe_engine, translate_engine, voxcpm_model
                    )
                    cleanup_file(input_path)
                    
                    if output_path and not output_path.startswith("ERROR") and os.path.exists(output_path):
                        if thumb and thumb.startswith("uploads/"):
                            if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                                abs_thumb = os.path.join("/var/data", thumb)
                            else:
                                abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                        else:
                            abs_thumb = thumb
                        tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb) if abs_thumb and os.path.exists(abs_thumb) else None
                        send_kwargs = {}
                        if tg_thumb and os.path.exists(tg_thumb): send_kwargs["thumb"] = tg_thumb
                        
                        try:
                            await target_app.send_video(user_id_int, output_path, progress=prog, **send_kwargs)
                        except:
                            pass
                        
                        if task_data.get("post_to_fb", False):
                            video_title = task_data.get("title")
                            if not video_title:
                                from downloader import extract_link_info
                                try:
                                    info = extract_link_info(url)
                                    if info and info.get("title"):
                                        video_title = info["title"]
                                except Exception:
                                    pass
                            if not video_title and input_path:
                                video_title = os.path.splitext(os.path.basename(input_path))[0]

                            await _post_dubbed_to_facebook(
                                task_data, user_id_int, output_path,
                                caption=f"🎙 {output_path}", abs_thumb=abs_thumb, anim=anim, target_app=target_app,
                                series_name=video_title or "Video", ep_no=1, total_eps=1, target_lang=lang, source_lang=source_lang
                            )
                            
                        cleanup_file(output_path)
                        if tg_thumb: cleanup_file(tg_thumb)
                        task_success = True
                        await anim.stop("✅ Voice dubbing complete!")
                    else:
                        await anim.stop(f"❌ Dubbing failed: {output_path}")
                except Exception as e:
                    await anim.stop(f"❌ Error during dubbing: {e}")
            else:
                await anim.stop("❌ Failed to download source media.")
            
        elif task_type == "translate":
            text = task_data.get("text", "")
            lang = task_data.get("lang", "km")
            thumb = task_data.get("thumbnail")
            
            msg = await target_app.send_message(user_id_int, "🌐 **Scheduled Translation**\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, "🌐 **Scheduled Translation**", cancel_token=cancel_token, task_id=task_id)
            anim.start()
            
            import asyncio, os
            from converter import generate_neural_tts, prepare_telegram_thumbnail, apply_thumbnail_to_video
            from utils import cleanup_file
            
            try:
                if text.startswith("uploads/"):
                    if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                        abs_text_path = os.path.join("/var/data", text)
                    else:
                        abs_text_path = os.path.abspath(os.path.join(os.path.dirname(__file__), text))
                    if os.path.exists(abs_text_path):
                        with open(abs_text_path, 'r', encoding='utf-8') as f:
                            text = f.read()
                        cleanup_file(abs_text_path)
                
                anim.set_detail("Generating TTS audio...")
                audio_path = await asyncio.to_thread(generate_neural_tts, text, lang)
                
                if audio_path and os.path.exists(audio_path):
                    if thumb and thumb.startswith("uploads/"):
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            abs_thumb = os.path.join("/var/data", thumb)
                        else:
                            abs_thumb = os.path.abspath(os.path.join(os.path.dirname(__file__), thumb))
                    else:
                        abs_thumb = thumb
                    anim.set_detail("Sending audio...")
                    if abs_thumb and os.path.exists(abs_thumb):
                        video_out = await asyncio.to_thread(apply_thumbnail_to_video, audio_path, abs_thumb)
                        tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, abs_thumb)
                        await target_app.send_video(user_id_int, video_out, thumb=tg_thumb)
                        cleanup_file(video_out)
                        if tg_thumb: cleanup_file(tg_thumb)
                    else:
                        await target_app.send_audio(user_id_int, audio_path)
                    
                    cleanup_file(audio_path)
                    task_success = True
                    await anim.stop("✅ Text-to-Speech complete!")
                else:
                    await anim.stop("❌ TTS Generation failed.")
            except Exception as e:
                await anim.stop(f"❌ Error: {e}")
            
        elif task_type == "facebook_post":
            url = task_data.get("url", "")
            text = task_data.get("text", "")
            fb_target = task_data.get("fb_target", "custom")
            tags = task_data.get("tags")
            collaborators = task_data.get("collaborators")
            
            prefix = "📤 **Scheduled Facebook Post**"
            msg = await target_app.send_message(user_id_int, f"{prefix}\n🔄 **Running...**")
            anim = ScheduleAnimator(msg, prefix, cancel_token=cancel_token, task_id=task_id)
            anim.start()
            
            import asyncio, os
            from user_manager import get_user_facebook
            
            pages_to_post = []
            
            if fb_target in ["all_system", "all"]:
                p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
                
                p2 = os.environ.get("LIVEALONE_PAGE_ID")
                t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
            elif fb_target == "aimovie":
                p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
            elif fb_target == "livealone":
                p = os.environ.get("LIVEALONE_PAGE_ID")
                t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                if p and t: pages_to_post.append((p, t, "LiveAlone"))
            elif fb_target == "all_saved":
                try:
                    from user_manager import get_user_facebook_pages
                    pages = get_user_facebook_pages(user_id_int)
                    for idx, p_data in enumerate(pages):
                        p = p_data.get("page_id")
                        t = p_data.get("page_token")
                        p_name = p_data.get("page_name", f"Saved Page {idx+1}")
                        if p and t: pages_to_post.append((p, t, p_name))
                except: pass
            elif fb_target.startswith("saved_"):
                try:
                    idx = int(fb_target.split("_")[1])
                    from user_manager import get_user_facebook_pages
                    pages = get_user_facebook_pages(user_id_int)
                    if idx < len(pages):
                        p = pages[idx].get("page_id")
                        t = pages[idx].get("page_token")
                        p_name = pages[idx].get("page_name", f"Saved Page {idx+1}")
                        if p and t: pages_to_post.append((p, t, p_name))
                except: pass
            else:
                p = task_data.get("custom_page_id")
                t = task_data.get("custom_fb_token")
                if not p or not t:
                    p_stored, t_stored = get_user_facebook(user_id_int)
                    p = p or p_stored
                    t = t or t_stored
                if p and t: pages_to_post.append((p, t, "Custom Page"))
            
            if not pages_to_post:
                await anim.stop("❌ Facebook credentials missing. Use /setfb to configure or check .env file.")
            else:
                input_path = None
                if url:
                    from downloader import download_media
                    prog = ThrottleProgress(msg, prefix, anim)
                    
                    if url.startswith("uploads/"):
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            input_path = os.path.join("/var/data", url)
                        else:
                            input_path = os.path.abspath(os.path.join(os.path.dirname(__file__), url))
                    else:
                        from plugins.core import is_telegram_link, download_telegram_post_media
                        if is_telegram_link(url):
                                input_path = await download_telegram_post_media(target_app, url, False, prog, requesting_user_id=user_id_int)
                        else:
                            dl_res = await asyncio.to_thread(download_media, url, False, prog)
                            input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res

                if input_path and (input_path.startswith("ERROR") or input_path in ["BOT_DETECTED", "TOO_LARGE"]):
                    await anim.stop(f"❌ Failed to download media: {input_path}")
                else:
                    from facebook_util import post_to_facebook
                    
                    title = task_data.get("title", "")
                    clean_text = format_fb_post_text(text, original_title=title or url, target_lang=task_data.get("lang") or "km", src_lang=task_data.get("source_lang") or "auto") if text else text
                    tr_title = get_translated_title(title, target_lang=task_data.get("lang") or "km") if title else title
                    thumbnail_rel = task_data.get("thumbnail")
                    thumb_path = None
                    if thumbnail_rel:
                        if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                            thumb_path = os.path.join("/var/data", thumbnail_rel)
                        else:
                            thumb_path = os.path.abspath(os.path.join(os.path.dirname(__file__), thumbnail_rel))
                    
                    results = []
                    for p_id, p_token, p_name in pages_to_post:
                        anim.set_detail(f"Posting to {p_name}...")
                        is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, clean_text, input_path, tr_title or title, thumb_path, tags, collaborators)
                        results.append((p_name, is_success, result))
                    
                    if input_path and os.path.exists(input_path) and not url.startswith("uploads/"):
                        from utils import cleanup_file
                        cleanup_file(input_path)
                        
                    success_count = sum(1 for r in results if r[1])
                    msg_text = "\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
                    final_result_text = f"Facebook Post Results:\n{msg_text}"
                    await anim.stop(final_result_text)
                    
                    if success_count > 0:
                        task_success = True
            
        if task_success:
            update_status("Sent to user")
        else:
            update_status("Failed")
    except (asyncio.CancelledError, Exception) as exc:
        is_cancelled = isinstance(exc, asyncio.CancelledError) or "stopped by user" in str(exc).lower() or (cancel_token and cancel_token.is_stop_requested())
        if is_cancelled:
            print(f"Task {task_id} for user {user_id} was stopped by user request.")
            update_status("⏹ Stopped by user")
            try:
                if 'anim' in locals() and anim:
                    await anim.stop("⏹ **Scheduled task stopped by user.**")
            except Exception:
                pass
            try:
                await target_app.send_message(int(user_id), "⏹ **Your scheduled task was stopped.**")
            except Exception:
                pass
            return
        print(f"Failed to execute schedule for {user_id}: {exc}")
        update_status(f"Failed: {exc}")
        try:
            if 'anim' in locals() and anim:
                await anim.stop(f"❌ Execution failed: {exc}")
        except Exception:
            pass
    finally:
        # Always unregister from the running-tasks map
        if task_id:
            with _RUNNING_LOCK:
                _RUNNING_TASKS.pop(task_id, None)
        if custom_client:
            try:
                await custom_client.stop()
            except:
                pass

_scheduler_loop = None

def set_scheduler_loop(loop):
    global _scheduler_loop
    _scheduler_loop = loop

def run_scheduler_loop(app: Optional[Client] = None):
    """
    Runs in a background thread and checks schedules every minute.
    Since we need to call pyrogram async methods, we use asyncio.run_coroutine_threadsafe 
    if an event loop is running, or create a new loop just for this.
    """
    def loop():
        # Check and auto-resume unfinished tasks from previous GitHub Action or server restart
        try:
            import task_resumer
            schedules_data = load_all_schedules()
            from user_manager import load_users
            users_map = load_users()
            current_server_id = os.environ.get("SERVER_ID", "main")
            
            for user_id, user_schedules in schedules_data.items():
                user_info = users_map.get(str(user_id), {})
                task_server = user_info.get("assigned_server", "main")
                if task_server != current_server_id:
                    continue  # Only resume tasks assigned to this server
                    
                for task in user_schedules:
                    task_id = task.get("id")
                    cp = task_resumer.get_checkpoint(task_id)
                    if task.get("status") == "Running" or (cp and cp.get("status") == "in_progress"):
                        print(f"[scheduler] 🔄 Resuming unfinished task {task_id} ({task.get('type')}) for user {user_id} on {current_server_id}...")
                        loop_to_use = _scheduler_loop or (app.loop if app and hasattr(app, "loop") else None)
                        if loop_to_use and loop_to_use.is_running():
                            asyncio.run_coroutine_threadsafe(execute_task(app, user_id, dict(task)), loop_to_use)
        except Exception as e_res:
            print(f"[scheduler] Error auto-resuming unfinished tasks: {e_res}")

        while True:
            try:
                # Use UTC+7 (Cambodia time) for schedule comparisons since user is in Cambodia
                from datetime import timedelta
                now = datetime.utcnow() + timedelta(hours=7)
                current_time_str = now.strftime("%H:%M")
                current_date_str = now.strftime("%Y-%m-%d")
                
                schedules_data = load_all_schedules()
                updated = False
                
                from user_manager import load_users
                users = load_users()
                server_id = os.environ.get("SERVER_ID", "main")
                
                for user_id, user_schedules in schedules_data.items():
                    user_info = users.get(str(user_id), {})
                    assigned_server = user_info.get("assigned_server", "main")
                    
                    # Only process schedules if the user is assigned to this server
                    if assigned_server != server_id:
                        continue
                        
                    for task in user_schedules:
                        if "id" not in task:
                            import uuid
                            task["id"] = str(uuid.uuid4())
                            if "status" not in task:
                                task["status"] = "Pending"
                            updated = True
                            
                        run_time = task.get("run_time", "")
                        last_run = task.get("last_run")
                        
                        should_run = False
                        
                        if "T" in run_time:
                            try:
                                if run_time.endswith("Z"):
                                    # New frontend format: UTC time (e.g. 2026-07-29T09:29:00.000Z)
                                    run_time_clean = run_time.split(".")[0] if "." in run_time else run_time[:-1]
                                    scheduled_dt = datetime.strptime(run_time_clean, "%Y-%m-%dT%H:%M:%S")
                                    if datetime.utcnow() >= scheduled_dt and last_run != "DONE":
                                        should_run = True
                                else:
                                    # Legacy format: YYYY-MM-DDTHH:MM (Cambodia time)
                                    scheduled_dt = datetime.strptime(run_time, "%Y-%m-%dT%H:%M")
                                    if now >= scheduled_dt and last_run != "DONE":
                                        should_run = True
                            except Exception as e:
                                print(f"Time parsing error for task {task.get('id')}: {run_time} -> {e}")
                        else:
                            # Legacy format: HH:MM
                            if run_time == current_time_str and last_run != current_date_str:
                                should_run = True
                                
                        if should_run:
                            # Time to run!
                            task["status"] = "Running"
                            if "T" in run_time:
                                task["last_run"] = "DONE"
                            else:
                                task["last_run"] = current_date_str
                            updated = True
                            
                            # Schedule the coroutine in the active event loop
                            loop_to_use = _scheduler_loop or (app.loop if app and hasattr(app, "loop") else None)
                            if loop_to_use and loop_to_use.is_running():
                                asyncio.run_coroutine_threadsafe(execute_task(app, user_id, dict(task)), loop_to_use)
                
                if updated:
                    save_all_schedules(schedules_data)
                    
            except Exception as e:
                print(f"Scheduler error: {e}")
                
            # Sleep until the next minute
            time.sleep(60 - datetime.now().second)
            
    threading.Thread(target=loop, daemon=True).start()
