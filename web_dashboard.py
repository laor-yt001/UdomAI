# pyright: reportGeneralTypeIssues=false
import os
import re
import json
import urllib.parse
import urllib.request
import cgi
import mimetypes
import shutil
import tempfile
import uuid
import random
import string
import time as _time
import hashlib
import hmac
from typing import Any, cast
import threading
import zipfile
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from user_manager import load_users, save_users, toggle_user_status, update_user_role, get_system_config, update_system_config, get_user_telegram_bots, add_user_telegram_bot, update_user_telegram_bot, set_user_max_custom_bots, delete_user_telegram_bot, get_active_user_bot, get_default_schedule_bot, set_default_schedule_bot
import scheduler

# ─── In-Memory Auth Stores ────────────────────────────────────────────────────
_otp_store = {}           # {user_id_str: {'code': '123456', 'expiry': float}}
_otp_store_lock = threading.Lock()

_session_store = {}       # {session_token: user_id_str}
_session_store_lock = threading.Lock()
_session_file = os.path.join(os.path.dirname(__file__), "sessions.json")

def _load_sessions_from_disk():
    global _session_store
    try:
        from user_manager import redis_client
        if redis_client:
            raw = redis_client.get("dashboard_sessions")
            if raw:
                data = json.loads(raw)
                with _session_store_lock:
                    _session_store.update(data)
                return
    except Exception:
        pass
    if os.path.exists(_session_file):
        try:
            with open(_session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                with _session_store_lock:
                    _session_store.update(data)
        except Exception:
            pass

def _save_sessions_to_disk():
    with _session_store_lock:
        data = dict(_session_store)
    try:
        with open(_session_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    try:
        from user_manager import redis_client
        if redis_client:
            redis_client.set("dashboard_sessions", json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

_load_sessions_from_disk()

_otp_verified_store = {}  # {temp_token: user_id_str} – after OTP OK, before password set
_otp_verified_lock = threading.Lock()

# Global dictionary to track progress of background tasks
TASK_PROGRESS = {}
ACTIVE_BG_PROCS = {}

def _background_yt_telegram_send(user_id_str, user_role, yt_url, format_id, num_clips, task_id=None):
    try:
        import asyncio
        from downloader import download_media, clean_youtube_url
        from video_splitter import split_video
        import custom_bots_manager
        import scheduler
        import time

        yt_url = clean_youtube_url(yt_url)

        if task_id:
            TASK_PROGRESS[task_id] = {"status": "running", "percent": 0, "text": "Starting download..."}

        print(f"[send_telegram] Starting download for {yt_url}")
        from utils import get_temp_dir
        from pytubefix import YouTube
        import subprocess
        
        temp_dir = get_temp_dir()
        downloaded_file = None

        proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
        proxy_configs = [proxies, None] if proxies else [None]

        yt = None
        for p_conf in proxy_configs:
            if yt: break
            for client_str in ['ANDROID_VR', 'MWEB', 'IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB']:
                try:
                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                    _ = _yt.streams
                    yt = _yt
                    break
                except Exception as e:
                    pass

        if yt:
            def on_progress(stream, chunk, bytes_remaining):
                if not task_id: return
                total = stream.filesize
                if total > 0:
                    percent = int(((total - bytes_remaining) / total) * 100)
                    TASK_PROGRESS[task_id] = {"status": "running", "percent": percent // 2, "text": f"Downloading {percent}%"}

            yt.register_on_progress_callback(on_progress)

            try:
                if format_id.startswith('SERVER_MERGE:'):
                    v_itag = int(format_id.split(':', 1)[1])
                    v_stream = yt.streams.get_by_itag(v_itag)
                    a_stream = yt.streams.get_audio_only()
                    
                    if not v_stream or not a_stream:
                        raise Exception("Missing streams for merge")

                    v_path = v_stream.download(output_path=temp_dir, filename=f"temp_v_{task_id}.mp4")
                    a_path = a_stream.download(output_path=temp_dir, filename=f"temp_a_{task_id}.m4a")

                    if v_path and a_path:
                        out_path = os.path.join(temp_dir, f"send_tg_{int(time.time())}_{task_id}.mp4")
                        import imageio_ffmpeg
                        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                        subprocess.run([str(ffmpeg_exe), '-y', '-i', str(v_path), '-i', str(a_path), '-c:v', 'copy', '-c:a', 'aac', str(out_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        if os.path.exists(v_path): os.remove(v_path)
                        if os.path.exists(a_path): os.remove(a_path)
                        downloaded_file = out_path
                else:
                    itag = int(format_id)
                    stream = yt.streams.get_by_itag(itag)
                    if not stream:
                        raise Exception("Stream not found")
                    downloaded_file = stream.download(output_path=temp_dir, filename=f"send_tg_{int(time.time())}_{task_id}.mp4")
                    
            except Exception as e:
                print(f"[send_telegram] pytubefix download failed: {e}")

        # Fallback to downloader.download_media if pytubefix failed or produced no file
        if not downloaded_file or not os.path.exists(downloaded_file):
            print(f"[send_telegram] Using download_media fallback for {yt_url}")
            if task_id:
                TASK_PROGRESS[task_id] = {"status": "running", "percent": 25, "text": "Downloading via engine..."}
            def send_prog(text):
                if task_id:
                    TASK_PROGRESS[task_id] = {"status": "running", "percent": 40, "text": text}
            try:
                is_audio = str(format_id).lower() in ['audio', '140', '251', '250', '249']
                fb_res = download_media(yt_url, is_audio=is_audio, progress_callback=send_prog)
                target_fb = fb_res[0] if isinstance(fb_res, tuple) else fb_res
                if target_fb and not str(target_fb).startswith("ERROR") and target_fb not in ["BOT_DETECTED", "TOO_LARGE"] and os.path.exists(target_fb):
                    downloaded_file = target_fb
            except Exception as fb_e:
                print(f"[send_telegram] download_media fallback failed: {fb_e}")

        if not downloaded_file or not os.path.exists(downloaded_file):
            print("[send_telegram] Download failed to produce a file.")
            if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Download failed"}
            return
            
        print(f"[send_telegram] Downloaded {downloaded_file}")
        if task_id: TASK_PROGRESS[task_id] = {"status": "running", "percent": 50, "text": "Processing clips..."}
        
        clips = split_video(downloaded_file, num_clips)
        
        client_to_use = None
        if user_role == 'SUPER_ADMIN':
            import sys
            client_to_use = getattr(sys.modules.get('__main__'), 'app', None)
            if not client_to_use:
                raise Exception("Main bot client not found in memory.")
        else:
            default_bot_token = get_default_schedule_bot(user_id_str)
            if default_bot_token and default_bot_token in custom_bots_manager.active_custom_bots:
                client_to_use = custom_bots_manager.active_custom_bots[default_bot_token]
            
        if not client_to_use:
            print("[send_telegram] No Pyrogram client available to send.")
            if task_id: 
                err_msg = "No Telegram bot available" if user_role == 'SUPER_ADMIN' else "You must add a Custom Bot in settings first!"
                TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": err_msg}
            return

        async def send_clips():
            try:
                for i, clip in enumerate(clips):
                    print(f"[send_telegram] Sending clip {i+1}/{len(clips)}: {clip}")
                    caption = f"Clip {i+1}/{len(clips)}" if len(clips) > 1 else ""
                    
                    async def tg_progress(current, total):
                        if task_id and total > 0:
                            p = int((current / total) * 100)
                            clip_contribution = 50 / len(clips)
                            base_percent = 50 + (i * clip_contribution)
                            overall_percent = int(base_percent + (p * clip_contribution / 100))
                            TASK_PROGRESS[task_id] = {"status": "running", "percent": overall_percent, "text": f"Uploading to Telegram... {p}% (Clip {i+1}/{len(clips)})"}

                    await client_to_use.send_document(chat_id=int(user_id_str), document=clip, caption=caption, progress=tg_progress)
                    if clip != downloaded_file and os.path.exists(clip):
                        os.remove(clip)
                if os.path.exists(downloaded_file):
                    os.remove(downloaded_file)
                if task_id:
                    TASK_PROGRESS[task_id] = {"status": "done", "percent": 100, "text": "Completed!"}
            except Exception as e:
                if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": f"Upload error: {e}"}
                raise e
                
        if custom_bots_manager.main_loop:
            asyncio.run_coroutine_threadsafe(send_clips(), custom_bots_manager.main_loop)
        else:
            asyncio.run(send_clips())
            
    except Exception as e:
        print(f"[send_telegram] Error: {e}")
        if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": f"Error: {str(e)}"}

OTP_EXPIRY_SECONDS = 300  # 5 minutes


def _generate_otp():
    return ''.join(random.choices(string.digits, k=6))


def _generate_token():
    return uuid.uuid4().hex + uuid.uuid4().hex


def _send_otp_via_telegram(user_id_str, otp_code):
    """Send OTP code to user's Telegram chat via Bot API."""
    # Use the global configured OTP Bot or fallback to main bot
    system_config = get_system_config()
    custom_bot = system_config.get("otp_bot_token")
    if custom_bot == "main_bot":
        custom_bot = None

    if custom_bot:
        bot_token = custom_bot
    else:
        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        
    if not bot_token:
        print("⚠️  No TELEGRAM_BOT_TOKEN set; cannot send OTP.")
        return False
    try:
        msg = (
            f"🔐 <b>Dashboard Login OTP</b>\n\n"
            f"Your verification code is:\n\n"
            f"<code>{otp_code}</code>\n\n"
            f"⏱ This code expires in <b>5 minutes</b>.\n"
            f"🚫 Do NOT share this code with anyone."
        )
        data = json.dumps({
            'chat_id': int(user_id_str),
            'text': msg,
            'parse_mode': 'HTML'
        }).encode('utf-8')
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error sending OTP via Telegram: {e}")
        return False


def _hash_password(password):
    """Hash password using PBKDF2-HMAC-SHA256 with a random salt."""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return salt.hex() + ':' + key.hex()


def _verify_password(stored_hash, password):
    """Verify a plaintext password against its stored PBKDF2 hash."""
    try:
        salt_hex, key_hex = stored_hash.split(':', 1)
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
        return hmac.compare_digest(key.hex(), key_hex)
    except Exception:
        return False


def _get_dashboard_admin_identity():
    admin_id = str(os.environ.get('SUPER_ADMIN_TG_ID') or os.environ.get('ADMIN_TG_ID') or '7591901012').strip()
    admin_username = str(os.environ.get('SUPER_ADMIN_USERNAME') or os.environ.get('ADMIN_USERNAME') or 'udomai').strip()
    dashboard_password = str(os.environ.get('DASHBOARD_PASSWORD') or '123137').strip()
    return admin_id, admin_username, dashboard_password


def _resolve_dashboard_user(input_id):
    """Resolve a dashboard user ID from a Telegram ID or username, including direct Super Admin credentials."""
    if not input_id:
        return None, None
    input_id = str(input_id).strip()
    if not input_id:
        return None, None

    users = load_users()
    if input_id in users:
        return str(input_id), users[input_id]

    normalized = input_id.lstrip('@').lower()
    for uid, user_obj in users.items():
        if isinstance(user_obj, dict) and str(user_obj.get('username', '')).lower() == normalized:
            return str(uid), user_obj

    admin_id, admin_username, dashboard_password = _get_dashboard_admin_identity()
    admin_username_lc = admin_username.lower()
    if input_id == admin_id or normalized in (admin_username_lc, f"@{admin_username_lc}", "udomai", "thengrithy", admin_id):
        admin_key = admin_id if (admin_id and admin_id.isdigit()) else "super_admin"
        admin_user = users.get(admin_key)
        if not admin_user:
            # Check if any user already has role SUPER_ADMIN
            for uid, u_obj in users.items():
                if isinstance(u_obj, dict) and u_obj.get('role') == 'SUPER_ADMIN':
                    admin_key = uid
                    admin_user = u_obj
                    break

        now_ts = datetime.now(timezone(timedelta(hours=7))).strftime('%Y-%m-%d %H:%M:%S')
        if not admin_user:
            admin_user = {
                'user_id': int(admin_key) if admin_key.isdigit() else admin_key,
                'username': admin_username,
                'first_name': 'Super Admin',
                'last_name': '',
                'status': 'APPROVED',
                'role': 'SUPER_ADMIN',
                'password_hash': _hash_password(dashboard_password) if dashboard_password else None,
                'joined_at': now_ts,
                'last_active': now_ts,
                'request_count': 1,
            }
            users[admin_key] = admin_user
            save_users(users, sync_github=True)
        else:
            # Ensure username is updated to admin_username if set
            if admin_username and admin_user.get('username') != admin_username:
                admin_user['username'] = admin_username
                users[admin_key] = admin_user
                save_users(users, sync_github=True)
        return str(admin_key), admin_user

    return None, None


class DualStackThreadingServer(ThreadingHTTPServer):
    allow_reuse_address = True

class DashboardHandler(BaseHTTPRequestHandler):
    def send_response(self, code, message=None):
        super().send_response(code, message)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Requested-With')

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def get_query_param(self, name):
        if '?' in self.path:
            query = self.path.split('?')[1]
            params = urllib.parse.parse_qs(query)
            return params.get(name, [None])[0]
        return None

    def get_current_user(self):
        # Primary: session token-based auth (secure)
        token = self.get_query_param('token')
        if not token:
            auth_header = self.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header.split(' ', 1)[1].strip()
        if not token:
            token = self.headers.get('X-Session-Token', '')
        if not token:
            cookie_header = self.headers.get('Cookie', '')
            if 'udom_session=' in cookie_header:
                for c in cookie_header.split(';'):
                    if c.strip().startswith('udom_session='):
                        token = c.strip().split('=', 1)[1]
                        break

        if token:
            with _session_store_lock:
                user_id = _session_store.get(token)
            if user_id:
                users = load_users()
                user = users.get(user_id)
                if user:
                    role = user.get('role', 'USER')
                    status = user.get('status', 'PENDING')
                    if role in ('SUPER_ADMIN', 'ADMIN') or status == 'APPROVED':
                        return user
            return None

        # Legacy fallback: user_id query param (backward compat)
        user_id = self.get_query_param('user_id') or self.headers.get('X-User-Id')
        if not user_id:
            return None
        users = load_users()
        user = users.get(str(user_id))
        if not user:
            return None
        role = user.get('role', 'USER')
        status = user.get('status', 'PENDING')
        if role in ('SUPER_ADMIN', 'ADMIN') or status == 'APPROVED':
            return user
        return None

    def send_json(self, data, status=200):
        body_bytes = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def send_library_file(self, file_path, filename, inline=False):
        body_size = os.path.getsize(file_path)
        disposition = 'inline' if inline else 'attachment'
        encoded_name = urllib.parse.quote(filename, safe='')
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(filename)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(body_size))
        self.send_header('Content-Disposition', f"{disposition}; filename*=UTF-8''{encoded_name}")
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with open(file_path, 'rb') as media_file:
            shutil.copyfileobj(media_file, self.wfile, length=64 * 1024)

    def do_HEAD(self):
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        try:
            path = self.path.split('?')[0]
            if path in ['/healthz', '/health', '/ping']:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b"OK")
                return

            # Auth checks
            user = self.get_current_user()
            
            if path == '/api/me':
                if user:
                    self.send_json(user)
                else:
                    self.send_json({"error": "Unauthorized"}, 401)
                return

            if path == '/api/library':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                from media_library import list_media
                try:
                    self.send_json({"items": list_media()})
                except OSError as exc:
                    print(f"Media-library listing failed: {exc}")
                    self.send_json({"error": "Unable to read media library."}, 500)
                return

            if path in ('/api/library/file', '/api/library/thumbnail'):
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                from media_library import resolve_episode, resolve_thumbnail
                name = self.get_query_param('name')
                try:
                    if path == '/api/library/file':
                        file_path = resolve_episode(name)
                        filename = name
                        inline = False
                    else:
                        file_path = resolve_thumbnail(name)
                        if not file_path:
                            self.send_json({"error": "Thumbnail not found."}, 404)
                            return
                        filename = os.path.basename(file_path)
                        inline = self.get_query_param('inline') == '1'
                    self.send_library_file(file_path, filename, inline=inline)
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                except FileNotFoundError:
                    self.send_json({"error": "Media file not found."}, 404)
                return

            if path in ('/api/progress', '/api/task_progress'):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                task_id = query.get('task_id', [None])[0]
                if not task_id:
                    if not user:
                        self.send_json({"error": "Unauthorized"}, 401)
                        return
                    self.send_json({"error": "Missing task_id"}, 400)
                    return
                state = TASK_PROGRESS.get(task_id)
                # If task was dispatched to a dedicated server, query real-time status from synchronized schedules
                is_dispatched = state and "Task dispatched to dedicated server" in state.get("text", "")
                if not state or is_dispatched:
                    try:
                        all_sched = scheduler.load_all_schedules()
                        for uid, tlist in all_sched.items():
                            for t in tlist:
                                if t.get("id") == task_id:
                                    st = t.get("status", "Pending")
                                    if st == "Completed" or t.get("last_run") == "DONE":
                                        state = {
                                            "status": "done",
                                            "percent": 100,
                                            "text": "✅ Completed on dedicated worker server! Check your Telegram Bot."
                                        }
                                        TASK_PROGRESS[task_id] = state
                                    elif st == "Running":
                                        comp = len(t.get("completed_vids", []))
                                        pct = min(90, 20 + comp * 10)
                                        state = {
                                            "status": "running",
                                            "percent": pct,
                                            "text": f"Processing on worker server... ({comp} episodes finished)" if comp else "Processing on dedicated worker server…"
                                        }
                                        TASK_PROGRESS[task_id] = state
                                    elif "fail" in st.lower() or "error" in st.lower():
                                        state = {"status": "error", "percent": 0, "text": f"Worker error: {st}"}
                                        TASK_PROGRESS[task_id] = state
                                    else:
                                        state = {"status": "running", "percent": 15, "text": f"Dispatched to worker ({st})…"}
                                    break
                    except Exception:
                        pass
                if not state:
                    state = {"status": "unknown", "percent": 0, "text": "Initializing..."}
                self.send_json(state)
                return

            if path == '/api/users':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                users_dict = load_users()
                users_list = list(users_dict.values())
                # filter out system config
                users_list = [u for u in users_list if isinstance(u, dict) and 'user_id' in u]
                users_list.sort(key=lambda x: x.get('joined_at', ''), reverse=True)
                self.send_json(users_list)
                return
                
            if path == '/api/settings':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                cfg = dict(get_system_config())
                from gemini_manager import get_gemini_keys
                cfg['gemini_api_keys'] = "\n".join(get_gemini_keys())
                cfg['default_transcribe_engine'] = cfg.get('default_transcribe_engine', 'local')
                cfg['default_whisper_model'] = cfg.get('default_whisper_model', 'small')
                cfg['default_translate_engine'] = cfg.get('default_translate_engine', 'gemini')
                cfg['default_tts_backend'] = cfg.get('default_tts_backend', 'voxcpm2')
                cfg['default_voxcpm_model'] = cfg.get('default_voxcpm_model', 'openbmb/VoxCPM1.5')
                self.send_json(cfg)
                return

            if path == '/api/gemini/test':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                from gemini_manager import test_all_keys
                self.send_json({"results": test_all_keys()})
                return
                
            if path == '/api/logs':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                log_file = "bot.log"
                if os.path.exists(log_file):
                    with open(log_file, "r", encoding="utf-8") as f:
                        # read last 500 lines
                        lines = f.readlines()
                        content = "".join(lines[-500:])
                else:
                    content = "No log file found."
                
                body_bytes = content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return
                
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                user_id = str(user['user_id'])
                schedules = scheduler.get_schedules(user_id)
                self.send_json(schedules)
                return

            if path == '/api/my_facebook':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                from user_manager import get_user_facebook_pages
                user_id = str(user['user_id'])
                user_pages = list(get_user_facebook_pages(user_id) or [])
                existing_pids = {str(p.get('page_id')) for p in user_pages if p.get('page_id')}

                # Include system default pages if configured
                sys_pages = []
                p_ai = os.environ.get("AIMOVIEKHMER_PAGE_ID")
                t_ai = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
                if p_ai and t_ai and p_ai not in existing_pids:
                    sys_pages.append({"page_id": p_ai, "page_name": "🎬 AI MOVIE KHMER (System)", "page_token": t_ai, "is_system": True})
                p_live = os.environ.get("LIVEALONE_PAGE_ID")
                t_live = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
                if p_live and t_live and p_live not in existing_pids:
                    sys_pages.append({"page_id": p_live, "page_name": "👤 LiveAlone (System)", "page_token": t_live, "is_system": True})

                all_pages = sys_pages + user_pages
                self.send_json({"fb_pages": all_pages})
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                default_bot = get_default_schedule_bot(str(user['user_id']))
                # If SUPER_ADMIN requests all, return all bots with user info
                if user.get('role') == 'SUPER_ADMIN' and self.get_query_param('all') == 'true':
                    all_bots = []
                    users_dict = load_users()
                    for uid, u_data in users_dict.items():
                        if isinstance(u_data, dict) and 'telegram_bots' in u_data:
                            for b in u_data['telegram_bots']:
                                b_copy = b.copy()
                                b_copy['owner_id'] = uid
                                b_copy['owner_name'] = u_data.get('first_name', '') + ' ' + u_data.get('last_name', '')
                                b_copy['owner_username'] = u_data.get('username', '')
                                all_bots.append(b_copy)
                    self.send_json({"bots": all_bots, "default_bot_token": default_bot})
                else:
                    user_id = str(user['user_id'])
                    bots = get_user_telegram_bots(user_id)
                    can_add_multiple = user.get("can_add_multiple_bots", False)
                    self.send_json({"bots": bots, "can_add_multiple": can_add_multiple, "default_bot_token": default_bot})
                return


            if path in ('/downloader', '/hongguo', '/hongguo-downloader', '/hongguo_downloader'):
                # Serve the standalone Hongguo + YouTube downloader page
                _dl_html = os.path.join(os.path.dirname(__file__), "hongguo_web.html")
                try:
                    with open(_dl_html, "r", encoding="utf-8") as _fh:
                        _dl_bytes = _fh.read().encode("utf-8")
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(_dl_bytes)))
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    self.wfile.write(_dl_bytes)
                except FileNotFoundError:
                    self.send_response(404)
                    self.end_headers()
                return

            if path == '/':
                with open(os.path.join(os.path.dirname(__file__), "dashboard.html"), "r", encoding="utf-8") as f:
                    html_content = f.read()
                body_bytes = html_content.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body_bytes)))
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                self.end_headers()
                self.wfile.write(body_bytes)
                return
                
            if path in ('/favicon.jpg', '/favicon.ico'):
                favicon_path = os.path.join(os.path.dirname(__file__), "favicon.jpg")
                if os.path.exists(favicon_path):
                    with open(favicon_path, "rb") as f:
                        body_bytes = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return

            if path.startswith('/api/yt/serve_merged'):
                from downloader import clean_youtube_url, _get_cookies_file
                yt_url = clean_youtube_url(self.get_query_param('url') or '')
                format_id = self.get_query_param('format_id')
                if not yt_url or not format_id:
                    self.send_response(400)
                    self.end_headers()
                    return
                
                import tempfile
                import subprocess
                import yt_dlp
                
                temp_dir = tempfile.mkdtemp()
                out_tmpl = os.path.join(temp_dir, 'video.%(ext)s')
                ydl_opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'noplaylist': True,
                    'format': format_id,
                    'outtmpl': out_tmpl,
                    'cookiefile': _get_cookies_file('youtube'),
                    'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
                    'merge_output_format': 'mp4',
                }
                proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                if proxies:
                    ydl_opts['proxy'] = "http://127.0.0.1:1080"
                    
                try:
                    with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                        info = ydl.extract_info(yt_url, download=True)
                        file_path = ydl.prepare_filename(info)
                        if not os.path.exists(file_path):
                            file_path = os.path.splitext(file_path)[0] + '.mp4'
                        
                        if os.path.exists(file_path):
                            size = os.path.getsize(file_path)
                            self.send_response(200)
                            self.send_header('Content-Type', 'video/mp4')
                            self.send_header('Content-Length', str(size))
                            self.send_header('Content-Disposition', f'attachment; filename="video.mp4"')
                            self.end_headers()
                            with open(file_path, 'rb') as f:
                                shutil.copyfileobj(f, self.wfile)
                        else:
                            self.send_response(500)
                            self.end_headers()
                except Exception as e:
                    print(f"[web_dashboard] server merge failed: {e}")
                    try:
                        self.send_response(500)
                        self.end_headers()
                    except:
                        pass
                finally:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                return

            if path.startswith('/api/yt/serve_download'):
                filename = self.get_query_param('file')
                if not filename:
                    self.send_response(400)
                    self.end_headers()
                    return
                from utils import get_temp_dir
                temp_dir = get_temp_dir()
                # secure filename check
                if '/' in filename or '\\' in filename:
                    self.send_response(400)
                    self.end_headers()
                    return
                file_path = os.path.join(temp_dir, filename)
                if os.path.exists(file_path):
                    size = os.path.getsize(file_path)
                    self.send_response(200)
                    ext = os.path.splitext(filename)[1].lower()
                    ctype = 'video/webm' if ext == '.webm' else ('audio/mp4' if ext == '.m4a' else 'video/mp4')
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(size))
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.end_headers()
                    try:
                        with open(file_path, 'rb') as f:
                            shutil.copyfileobj(f, self.wfile)
                    except Exception as e:
                        print(f"Serve download failed: {e}")
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            # ─── Hongguo Serve Downloaded File ───────────────────────────────────
            if path.startswith('/api/hongguo/serve'):
                filename = self.get_query_param('file')
                if not filename or '/' in filename or '\\' in filename:
                    self.send_response(400)
                    self.end_headers()
                    return
                from utils import get_temp_dir
                temp_dir = get_temp_dir()
                file_path = os.path.join(temp_dir, filename)
                if os.path.exists(file_path):
                    size = os.path.getsize(file_path)
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.end_headers()
                    try:
                        with open(file_path, 'rb') as f:
                            shutil.copyfileobj(f, self.wfile)
                    except Exception as hg_serve_err:
                        print(f"[hongguo serve] Error: {hg_serve_err}")
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            # ─── YouTube Stream Extractor (client downloads directly) ──────────
            if path == '/api/yt/info':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                yt_url = self.get_query_param('url')
                if not yt_url:
                    self.send_json({"error": "Missing url parameter"}, 400)
                    return
                from downloader import clean_youtube_url
                yt_url = clean_youtube_url(yt_url.strip())
                try:
                    formats_out = []
                    title, thumbnail, duration, uploader = "Video", "", 0, "Unknown"
                    from pytubefix import YouTube
                    proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                    try:
                        yt = None
                        last_p_err = None
                        proxy_configs = [proxies, None] if proxies else [None]
                        for p_conf in proxy_configs:
                            if yt: break
                            for client_str in ['IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB', 'MWEB', 'ANDROID_VR']:
                                try:
                                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                                    title = getattr(_yt, 'title', 'Video')
                                    thumbnail = getattr(_yt, 'thumbnail_url', '')
                                    duration = getattr(_yt, 'length', 0)
                                    uploader = getattr(_yt, 'author', 'Unknown')
                                    _ = _yt.streams # force fetch
                                    yt = _yt
                                    break
                                except Exception as e:
                                    last_p_err = e
                        
                        if not yt:
                            raise Exception(f"All pytubefix clients failed. Last error: {last_p_err}")
                            
                        seen = set()
                        for s in yt.streams:
                            fmt_id = str(s.itag)
                            ext = getattr(s, "subtype", "mp4")
                            is_video = getattr(s, "includes_video_track", getattr(s, "type", "") == "video")
                            is_audio = getattr(s, "includes_audio_track", getattr(s, "type", "") == "audio" or getattr(s, "is_progressive", False))
                            
                            if is_video and is_audio:
                                res = getattr(s, "resolution", "")
                                label = f"{res} {ext.upper()}" if res else f"{ext.upper()} (video+audio)"
                                key = (res, ext, 'va')
                                type_str = 'video'
                            elif is_video and not is_audio:
                                res = getattr(s, "resolution", "")
                                label = f"{res} {ext.upper()} (No Audio)" if res else f"{ext.upper()} (No Audio)"
                                key = (res, ext, 'v')
                                type_str = 'video'
                            elif is_audio and not is_video:
                                abr = getattr(s, "abr", "")
                                label = f"Audio {abr} {ext.upper()}" if abr else f"Audio {ext.upper()}"
                                key = (abr, ext, 'a')
                                type_str = 'audio'
                            else:
                                continue
                                
                            if key in seen:
                                continue
                            seen.add(key)
                            
                            if not hasattr(s, 'url') or not s.url:
                                continue

                            res_str = getattr(s, "resolution", "0p")
                            if not res_str: res_str = "0p"
                            
                            formats_out.append({
                                'format_id': fmt_id,
                                'label': label,
                                'ext': ext,
                                'height': int(res_str.replace("p", "")) if "p" in res_str else 0,
                                'filesize': getattr(s, 'filesize', getattr(s, 'filesize_approx', 0)),
                                'type': type_str,
                            })
                            
                            if is_video and not is_audio:
                                merge_key = (res_str, ext, 'v_merge')
                                if merge_key not in seen:
                                    seen.add(merge_key)
                                    formats_out.append({
                                        'format_id': f"SERVER_MERGE:{fmt_id}",
                                        'label': f"{res_str} {ext.upper()} (Auto-Merge)",
                                        'ext': 'mp4',
                                        'height': int(res_str.replace("p", "")) if "p" in res_str else 0,
                                        'filesize': 0,
                                        'type': 'video'
                                    })
                    except Exception as p_err:
                        print(f"[web_dashboard] pytubefix failed: {p_err}. Falling back to yt-dlp...")
                        import yt_dlp
                        from downloader import _get_cookies_file
                        ydl_opts = {
                            'quiet': True,
                            'no_warnings': True,
                            'format': 'all',
                            'noplaylist': True,
                            'cookiefile': _get_cookies_file('youtube'),
                            'extractor_args': {
                                'youtube': {
                                    'player_client': ['android', 'web'],
                                }
                            }
                        }
                        if proxies:
                            ydl_opts['proxy'] = "http://127.0.0.1:1080"
                            
                        info = {}
                        try:
                            with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                                info = ydl.extract_info(yt_url, download=False)
                        except Exception as first_e:
                            if proxies:
                                print(f"[web_dashboard] yt info proxy failed: {first_e}. Retrying without proxy...")
                                del ydl_opts['proxy']
                                with yt_dlp.YoutubeDL(cast(Any, ydl_opts)) as ydl:
                                    info = ydl.extract_info(yt_url, download=False)
                            else:
                                raise

                        if not info:
                            info = {}

                        title = info.get('title', 'Video')
                        thumbnail = info.get('thumbnail', '')
                        duration = info.get('duration', 0)
                        uploader = info.get('uploader', 'Unknown')

                        formats_out = []
                        seen = set()
                        
                        raw_formats = info.get('formats') or []
                        for f in (raw_formats if isinstance(raw_formats, list) else []):
                            if not f.get('url'):
                                continue
                                
                            vcodec = f.get('vcodec') != 'none'
                            acodec = f.get('acodec') != 'none'
                            ext = f.get('ext', 'mp4')
                            height = f.get('height') or 0
                            filesize = f.get('filesize') or f.get('filesize_approx') or 0
                            fmt_id = str(f.get('format_id', ''))
                            
                            if vcodec and acodec:
                                type_str = 'video'
                                label = f"{height}p {ext.upper()}" if height else f"{ext.upper()} (video+audio)"
                                key = (height, ext, 'va')
                            elif acodec and not vcodec:
                                type_str = 'audio'
                                abr = f.get('abr')
                                label = f"Audio {abr}kbps {ext.upper()}" if abr else f"Audio {ext.upper()}"
                                key = (abr, ext, 'a')
                            elif vcodec and not acodec:
                                type_str = 'video'
                                label = f"{height}p {ext.upper()} (No Audio)" if height else f"{ext.upper()} (No Audio)"
                                key = (height, ext, 'v')
                            else:
                                continue
                                
                            if key in seen:
                                continue
                            seen.add(key)
                            
                            formats_out.append({
                                'format_id': fmt_id,
                                'label': label,
                                'ext': ext,
                                'height': height,
                                'filesize': filesize,
                                'type': type_str,
                            })
                            
                            if vcodec and not acodec:
                                merge_key = (height, ext, 'v_merge')
                                if merge_key not in seen:
                                    seen.add(merge_key)
                                    formats_out.append({
                                        'format_id': f"SERVER_MERGE:{fmt_id}",
                                        'label': f"{height}p {ext.upper()} (Auto-Merge)" if height else f"{ext.upper()} (Auto-Merge)",
                                        'ext': 'mp4',
                                        'height': height,
                                        'filesize': 0,
                                        'type': 'video'
                                    })

                    formats_out.sort(key=lambda x: (x['height'], x['type'] == 'video'), reverse=True)

                    self.send_json({
                        'title': title,
                        'thumbnail': thumbnail,
                        'duration': duration,
                        'uploader': uploader,
                        'formats': formats_out[:20],
                    })
                except Exception as e:
                    self.send_json({'error': f'Failed to fetch video info: {e}'}, 500)
                return

            self.send_response(404)
            self.end_headers()

        except Exception as e:
            print(f"HTTP GET Error: {e}")

    def do_POST(self):
        try:
            path = self.path.split('?')[0]
            user = self.get_current_user()

            if path == '/api/library/download':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length <= 0 or content_length > 1024 * 1024:
                    self.send_json({"error": "Invalid download request size."}, 400)
                    return
                try:
                    body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_json({"error": "Request must contain valid JSON."}, 400)
                    return
                names = body.get('names') if isinstance(body, dict) else None
                if not isinstance(names, list) or not names or len(names) > 1000:
                    self.send_json({"error": "Select between 1 and 1000 media files."}, 400)
                    return
                if any(not isinstance(name, str) for name in names):
                    self.send_json({"error": "Media filenames must be strings."}, 400)
                    return

                from media_library import resolve_episode
                try:
                    files = [(name, resolve_episode(name)) for name in dict.fromkeys(names)]
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, 400)
                    return
                except FileNotFoundError:
                    self.send_json({"error": "One or more media files were not found."}, 404)
                    return
                if not files:
                    self.send_json({"error": "Select at least one media file."}, 400)
                    return
                if sum(os.path.getsize(file_path) for _, file_path in files) > 2 * 1024 * 1024 * 1024:
                    self.send_json({"error": "The selected files exceed the 2 GB archive limit."}, 413)
                    return

                file_descriptor, archive_path = tempfile.mkstemp(prefix='media-library-', suffix='.zip')
                os.close(file_descriptor)
                try:
                    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED) as archive:
                        for name, file_path in files:
                            archive.write(file_path, arcname=name)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Length', str(os.path.getsize(archive_path)))
                    self.send_header('Content-Disposition', 'attachment; filename="media-library.zip"')
                    self.end_headers()
                    with open(archive_path, 'rb') as archive_file:
                        shutil.copyfileobj(archive_file, self.wfile, length=64 * 1024)
                finally:
                    os.remove(archive_path)
                return
            
            # ─── Auth Endpoints (no session required) ──────────────────────────
            if path == '/api/auth/request-otp':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                if not input_id:
                    self.send_json({'error': 'Telegram ID or Username is required.'}, 400)
                    return

                user_id, u = _resolve_dashboard_user(input_id)
                if not u:
                    self.send_json({'error': 'User not found. Please start the bot first and ensure your ID or Username is correct.'}, 404)
                    return

                role = u.get('role', 'USER')
                status = u.get('status', 'PENDING')
                if status == 'BLOCKED':
                    self.send_json({'error': 'Your account is blocked. Please contact an admin.'}, 403)
                    return
                if role not in ('SUPER_ADMIN', 'ADMIN') and status != 'APPROVED':
                    self.send_json({'error': 'Your account is pending admin approval.'}, 403)
                    return

                otp = _generate_otp()
                with _otp_store_lock:
                    _otp_store[str(user_id)] = {'code': otp, 'expiry': _time.time() + OTP_EXPIRY_SECONDS}
                ok = _send_otp_via_telegram(str(user_id), otp)
                if not ok:
                    self.send_json({'error': 'Failed to send OTP via Telegram. Check the bot is running and your User ID is correct.'}, 500)
                    return
                self.send_json({'success': True, 'message': 'OTP sent to your Telegram!'})
                return

            if path == '/api/auth/verify-otp':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                otp_input = str(body.get('otp', '')).strip()
                if not input_id or not otp_input:
                    self.send_json({'error': 'User ID and OTP are required.'}, 400)
                    return

                user_id, _ = _resolve_dashboard_user(input_id)
                if not user_id:
                    self.send_json({'error': 'User not found.'}, 404)
                    return

                with _otp_store_lock:
                    entry = _otp_store.get(str(user_id))
                if not entry:
                    self.send_json({'error': 'No OTP was requested. Please request a new one.'}, 400)
                    return
                if _time.time() > entry['expiry']:
                    with _otp_store_lock:
                        _otp_store.pop(str(user_id), None)
                    self.send_json({'error': 'OTP has expired. Please request a new one.'}, 400)
                    return
                if otp_input != entry['code']:
                    self.send_json({'error': 'Incorrect OTP. Please try again.'}, 400)
                    return
                with _otp_store_lock:
                    _otp_store.pop(str(user_id), None)
                temp_token = _generate_token()
                with _otp_verified_lock:
                    _otp_verified_store[temp_token] = str(user_id)
                self.send_json({'success': True, 'otp_verified_token': temp_token})
                return

            if path == '/api/auth/set-password':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                otp_token = str(body.get('otp_verified_token', '')).strip()
                password = str(body.get('password', ''))
                if not otp_token or not password:
                    self.send_json({'error': 'Verification token and password are required.'}, 400)
                    return
                with _otp_verified_lock:
                    user_id = _otp_verified_store.get(otp_token)
                if not user_id:
                    self.send_json({'error': 'Invalid or expired verification token. Please start the OTP flow again.'}, 400)
                    return
                if len(password) < 6:
                    self.send_json({'error': 'Password must be at least 6 characters.'}, 400)
                    return
                users = load_users()
                if user_id not in users:
                    self.send_json({'error': 'User not found.'}, 404)
                    return
                users[user_id]['password_hash'] = _hash_password(password)
                save_users(users, sync_github=True)
                with _otp_verified_lock:
                    _otp_verified_store.pop(otp_token, None)
                session_token = _generate_token()
                with _session_store_lock:
                    _session_store[session_token] = user_id
                _save_sessions_to_disk()
                self.send_json({'success': True, 'session_token': session_token})
                return

            if path == '/api/auth/login':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                input_id = str(body.get('user_id', '')).strip()
                password = str(body.get('password', ''))
                if not input_id or not password:
                    self.send_json({'error': 'Telegram ID or Username and password are required.'}, 400)
                    return

                user_id, u = _resolve_dashboard_user(input_id)
                if not u:
                    self.send_json({'error': 'User not found.'}, 404)
                    return

                role = u.get('role', 'USER')
                status = u.get('status', 'PENDING')
                if status == 'BLOCKED':
                    self.send_json({'error': 'Your account is blocked. Contact an admin.'}, 403)
                    return
                if role not in ('SUPER_ADMIN', 'ADMIN') and status != 'APPROVED':
                    self.send_json({'error': 'Your account is pending approval.'}, 403)
                    return

                admin_id, admin_username, dashboard_password = _get_dashboard_admin_identity()
                stored_hash = u.get('password_hash')
                normalized_input = input_id.lstrip('@').lower()
                is_admin_login = (
                    str(user_id) == str(admin_id)
                    or str(u.get('username', '')).lower() in (admin_username.lower(), 'udomai', 'thengrithy')
                    or normalized_input in (admin_username.lower(), 'udomai', 'thengrithy', str(admin_id).lower())
                    or u.get('role') == 'SUPER_ADMIN'
                )

                if dashboard_password and is_admin_login and password == dashboard_password:
                    accepted = True
                elif stored_hash:
                    accepted = _verify_password(stored_hash, password)
                else:
                    self.send_json({'error': 'No password set. Please use the OTP flow to create a password first.', 'need_otp': True}, 400)
                    return

                if not accepted:
                    self.send_json({'error': 'Incorrect password. Please try again.'}, 401)
                    return

                session_token = _generate_token()
                with _session_store_lock:
                    _session_store[session_token] = str(user_id)
                _save_sessions_to_disk()
                self.send_json({'success': True, 'session_token': session_token})
                return

            if path == '/api/upload':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                
                content_type = self.headers.get('Content-Type', '')
                
                if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                    upload_dir = "/var/data/uploads"
                else:
                    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
                os.makedirs(upload_dir, exist_ok=True)
                
                if 'multipart/form-data' in content_type:
                    form = cgi.FieldStorage(
                        fp=self.rfile,
                        headers=self.headers,
                        environ={'REQUEST_METHOD': 'POST',
                                 'CONTENT_TYPE': self.headers['Content-Type'],
                                 'CONTENT_LENGTH': self.headers.get('Content-Length', '0')}
                    )
                    
                    if 'file' not in form:
                        self.send_json({"error": "No file provided"}, 400)
                        return
                        
                    file_item = form['file']
                    if not file_item.file:
                        self.send_json({"error": "Invalid file"}, 400)
                        return
                    
                    filename = file_item.filename
                    if not filename:
                        filename = "upload.bin"
                    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(filename)}"
                    save_path = os.path.join(upload_dir, safe_name)
                    
                    with open(save_path, "wb") as f:
                        while True:
                            chunk = file_item.file.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                else:
                    # Raw binary upload
                    filename = self.get_query_param('filename')
                    if not filename:
                        filename = "upload.bin"
                    
                    safe_name = f"{uuid.uuid4().hex}_{os.path.basename(filename)}"
                    save_path = os.path.join(upload_dir, safe_name)
                    
                    content_length = int(self.headers.get('Content-Length', 0))
                    
                    with open(save_path, "wb") as f:
                        bytes_left = content_length
                        while bytes_left > 0:
                            chunk = self.rfile.read(min(8192, bytes_left))
                            if not chunk:
                                break
                            f.write(chunk)
                            bytes_left -= len(chunk)
                            
                self.send_json({"success": True, "file_path": f"uploads/{safe_name}"})
                return

            if path == '/api/toggle':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                updated_user = toggle_user_status(target_id)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/assign_server':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                server_id = data.get('server_id')
                
                from user_manager import update_user_assigned_server
                updated_user = update_user_assigned_server(target_id, server_id)
                try:
                    import custom_bots_manager, asyncio
                    if hasattr(custom_bots_manager, "main_loop") and custom_bots_manager.main_loop:
                        asyncio.run_coroutine_threadsafe(
                            custom_bots_manager.start_all_approved_bots(),
                            custom_bots_manager.main_loop
                        )
                except Exception as b_err:
                    print(f"Error reconciling bots after assign server: {b_err}")
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/fb_access':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                page_id = data.get('page_id')
                from user_manager import toggle_fb_page_access
                updated_user = toggle_fb_page_access(target_id, page_id)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/role':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                new_role = data.get('role')
                updated_user = update_user_role(target_id, new_role)
                self.send_json({"success": True, "user": updated_user})
                return
                
            if path == '/api/remove':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_id = data.get('target_id')
                from user_manager import remove_user
                success = remove_user(target_id)
                self.send_json({"success": success})
                return
                
            if path == '/api/settings':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                admin_contact = data.get('admin_contact')
                if admin_contact is not None:
                    update_system_config("admin_contact", admin_contact)
                    
                otp_bot_token = data.get('otp_bot_token')
                if otp_bot_token is not None:
                    update_system_config("otp_bot_token", otp_bot_token)
                    
                otp_bot_restricted_mode = data.get('otp_bot_restricted_mode')
                if otp_bot_restricted_mode is not None:
                    update_system_config("otp_bot_restricted_mode", otp_bot_restricted_mode)
                    
                webapp_url = data.get('webapp_url')
                if webapp_url is not None:
                    update_system_config("webapp_url", webapp_url)
                    
                gemini_api_keys = data.get('gemini_api_keys')
                if gemini_api_keys is not None:
                    from gemini_manager import save_gemini_keys
                    save_gemini_keys(gemini_api_keys)

                default_transcribe_engine = data.get('default_transcribe_engine')
                if default_transcribe_engine in ('gemini', 'local'):
                    update_system_config('default_transcribe_engine', default_transcribe_engine)

                default_whisper_model = data.get('default_whisper_model')
                if default_whisper_model:
                    update_system_config('default_whisper_model', default_whisper_model)

                default_translate_engine = data.get('default_translate_engine')
                if default_translate_engine in ('gemini', 'local'):
                    update_system_config('default_translate_engine', default_translate_engine)

                default_tts_backend = data.get('default_tts_backend')
                if default_tts_backend:
                    update_system_config('default_tts_backend', default_tts_backend)

                default_voxcpm_model = data.get('default_voxcpm_model')
                if default_voxcpm_model:
                    update_system_config('default_voxcpm_model', default_voxcpm_model)
                    
                self.send_json({"success": True})
                return
                
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                user_id = str(user['user_id'])
                idx = data.pop('idx', None)
                if idx is not None:
                    scheduler.update_schedule(user_id, int(idx), data)
                else:
                    scheduler.add_schedule(user_id, data)
                self.send_json({"success": True})
                return

            if path == '/api/my_facebook':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                pages = data.get('fb_pages', [])
                from user_manager import update_user_facebook
                update_user_facebook(user['user_id'], pages)
                self.send_json({"success": True})
                return

            if path == '/api/admin/delete_facebook_page':
                if not user or user.get('role') not in ['SUPER_ADMIN', 'ADMIN']:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                page_id = data.get('page_id')
                if not target_user_id or not page_id:
                    self.send_json({"error": "Missing parameters"}, 400)
                    return
                from user_manager import delete_user_facebook_page
                success = delete_user_facebook_page(target_user_id, page_id)
                if success:
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to delete or not found"}, 404)
                return

            if path == '/api/facebook/sync_pages':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
                user_token = data.get('user_token') or os.environ.get('FB_USER_TOKEN', '')
                if not user_token:
                    self.send_json({"error": "Please provide a Facebook User Access Token to discover and sync pages."}, 400)
                    return
                try:
                    import facebook_manager
                    pages = facebook_manager.sync_and_store_facebook_pages(user_token, user_id=str(user['user_id']))
                    self.send_json({"success": True, "count": len(pages), "pages": pages})
                except Exception as fb_err:
                    self.send_json({"error": str(fb_err)}, 400)
                return

            if path == '/api/facebook/post':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                page_id = str(data.get('page_id', '')).strip()
                page_token = (data.get('page_token') or '').strip()
                message = data.get('message', '')
                media_path = data.get('media_path', None)
                if not page_id:
                    self.send_json({"error": "Missing page_id"}, 400)
                    return
                # Check system pages fallback first if page_token not explicitly provided
                if not page_token:
                    if page_id == str(os.environ.get("AIMOVIEKHMER_PAGE_ID")):
                        page_token = (os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN") or '').strip()
                    elif page_id == str(os.environ.get("LIVEALONE_PAGE_ID")):
                        page_token = (os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN") or '').strip()

                if not page_token:
                    from user_manager import get_user_facebook_pages
                    for p in get_user_facebook_pages(str(user['user_id'])):
                        if str(p.get('page_id')) == page_id:
                            page_token = (p.get('page_token') or '').strip()
                            break

                if not page_token:
                    from user_manager import get_user_facebook
                    up_id, up_token = get_user_facebook(str(user['user_id']))
                    if up_id and str(up_id) == page_id and up_token:
                        page_token = up_token.strip()

                if not page_token:
                    self.send_json({"error": f"Page token not found for page ID: {page_id}"}, 404)
                    return
                try:
                    import facebook_manager
                    res = facebook_manager.post_to_facebook(page_id, page_token, message, media_path)
                    self.send_json({"success": True, "result": res})
                except Exception as fb_p_err:
                    self.send_json({"error": str(fb_p_err)}, 400)
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                token = data.get('bot_token', '').strip()
                if not token:
                    self.send_json({"error": "Bot token is required"}, 400)
                    return
                
                # Check limits
                user_id = str(user['user_id'])
                bots = get_user_telegram_bots(user_id)
                max_bots = user.get("max_custom_bots", 1)
                
                if user.get("role") != "SUPER_ADMIN" and len(bots) >= max_bots:
                    self.send_json({"error": f"You can only add {max_bots} bot(s). Contact admin for more."}, 403)
                    return
                    
                import requests
                try:
                    res = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
                    if res.status_code == 200:
                        b_data = res.json().get("result", {})
                        bot_username = b_data.get("username", "")
                        bot_name = b_data.get("first_name", "")
                        
                        bot_obj = {
                            "bot_token": token,
                            "bot_username": bot_username,
                            "bot_name": bot_name,
                            "status": "APPROVED",
                        }
                        if add_user_telegram_bot(user_id, bot_obj):
                            # Auto-start the newly approved bot dynamically
                            import custom_bots_manager
                            if hasattr(custom_bots_manager, 'main_loop') and custom_bots_manager.main_loop:
                                import asyncio
                                asyncio.run_coroutine_threadsafe(custom_bots_manager.start_custom_bot(user_id, token), custom_bots_manager.main_loop)
                                
                            self.send_json({"success": True})
                        else:
                            self.send_json({"error": "Failed to save bot"}, 500)
                    else:
                        self.send_json({"error": "Invalid bot token"}, 400)
                except Exception as e:
                    self.send_json({"error": f"Error verifying token: {e}"}, 500)
                return

            if path == '/api/telegram_bots/profile':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                token = data.get('bot_token')
                name = data.get('name')
                desc = data.get('description')
                short_desc = data.get('short_description')
                
                import requests
                try:
                    if name:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyName", json={"name": name})
                    if desc:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyDescription", json={"description": desc})
                    if short_desc:
                        requests.post(f"https://api.telegram.org/bot{token}/setMyShortDescription", json={"short_description": short_desc})
                    
                    update_user_telegram_bot(user['user_id'], token, {"bot_name": name or ""})
                    self.send_json({"success": True})
                except Exception as e:
                    self.send_json({"error": str(e)}, 500)
                return

            if path == '/api/telegram_bots/admin/status':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                token = data.get('bot_token')
                status = data.get('status')
                if update_user_telegram_bot(target_user_id, token, {"status": status}):
                    import custom_bots_manager
                    import asyncio
                    if status == "APPROVED":
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.start_custom_bot(target_user_id, token), 
                                custom_bots_manager.main_loop
                            )
                    else:
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.stop_custom_bot(token), 
                                custom_bots_manager.main_loop
                            )
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to update"}, 500)
                return

            if path == '/api/telegram_bots/admin/otp':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                token = data.get('bot_token')
                can_send_otp = data.get('can_send_otp')
                if update_user_telegram_bot(target_user_id, token, {"can_send_otp": can_send_otp}):
                    self.send_json({"success": True})
                else:
                    self.send_json({"error": "Failed to update"}, 500)
                return

            if path == '/api/telegram_bots/admin/multiple':
                if not user or user.get('role') != 'SUPER_ADMIN':
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = data.get('user_id')
                max_bots = data.get('max_bots', 1)
                new_val = set_user_max_custom_bots(target_user_id, max_bots)
                self.send_json({"success": True, "max_custom_bots": new_val})
                return

            if path == '/api/telegram_bots/default':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(content_length).decode('utf-8'))
                target_user_id = str(user['user_id'])
                bot_token = data.get('bot_token', 'main_bot')
                set_default_schedule_bot(target_user_id, bot_token)
                self.send_json({"success": True})
                return

            def _background_yt_device_download(task_id, yt_url, format_id):
                print(f"[device_download] Starting download for {yt_url}")
                from utils import get_temp_dir
                import yt_dlp
                import time
                import urllib.parse
                from downloader import _get_cookies_file

                temp_dir = get_temp_dir()
                downloaded_file = None

                # Progress hook for yt-dlp
                def yt_progress_hook(d):
                    if not task_id: return
                    if d.get('status') == 'downloading':
                        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                        downloaded = d.get('downloaded_bytes') or 0
                        percent = int((downloaded / total) * 100) if total > 0 else 50
                        speed = d.get('speed') or 0
                        speed_str = f" ({speed/1048576:.1f} MB/s)" if speed > 0 else ""
                        TASK_PROGRESS[task_id] = {
                            "status": "running",
                            "percent": max(5, min(95, percent)),
                            "text": f"Downloading {percent}%{speed_str}"
                        }
                    elif d.get('status') == 'finished':
                        TASK_PROGRESS[task_id] = {
                            "status": "running",
                            "percent": 98,
                            "text": "Finalizing video..."
                        }

                # Format specification
                if str(format_id).startswith('SERVER_MERGE:'):
                    req_format = f"{format_id.split(':', 1)[1]}+bestaudio/best"
                elif str(format_id).lower() in ('audio', '140', '251', '250', '249'):
                    req_format = 'bestaudio/best'
                elif format_id and str(format_id) != 'best':
                    req_format = f"{format_id}+bestaudio/best"
                else:
                    req_format = 'bestvideo+bestaudio/best'

                out_tmpl = os.path.join(temp_dir, f"device_{int(time.time())}_{task_id}.%(ext)s")
                cookies_file = _get_cookies_file('youtube')
                ydl_opts = {
                    'format': req_format,
                    'outtmpl': out_tmpl,
                    'quiet': True,
                    'no_warnings': True,
                    'progress_hooks': [yt_progress_hook],
                    'merge_output_format': 'mp4',
                    'noplaylist': True,
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['android', 'web'],
                        }
                    }
                }
                if cookies_file and os.path.exists(cookies_file):
                    ydl_opts['cookiefile'] = cookies_file

                proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                if proxies:
                    ydl_opts['proxy'] = "http://127.0.0.1:1080"

                try:
                    TASK_PROGRESS[task_id] = {"status": "running", "percent": 5, "text": "Fetching YouTube stream..."}
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(yt_url, download=True)
                        candidate = ydl.prepare_filename(info)
                        if os.path.exists(candidate):
                            downloaded_file = candidate
                        else:
                            base_cand = os.path.splitext(candidate)[0]
                            for ext in ['.mp4', '.mkv', '.webm', '.m4a']:
                                if os.path.exists(base_cand + ext):
                                    downloaded_file = base_cand + ext
                                    break
                except Exception as ydl_err:
                    print(f"[device_download] yt-dlp direct failed: {ydl_err}")

                # Fallback to downloader.download_media if needed
                if not downloaded_file or not os.path.exists(downloaded_file):
                    try:
                        from downloader import download_media
                        is_audio = str(format_id).lower() in ['audio', '140', '251', '250', '249']
                        fb_res = download_media(yt_url, is_audio=is_audio)
                        target_fb = fb_res[0] if isinstance(fb_res, tuple) else fb_res
                        if target_fb and not str(target_fb).startswith("ERROR") and target_fb not in ["BOT_DETECTED", "TOO_LARGE"] and os.path.exists(target_fb):
                            downloaded_file = target_fb
                    except Exception as fb_e:
                        print(f"[device_download] download_media fallback failed: {fb_e}")

                if not downloaded_file or not os.path.exists(downloaded_file):
                    print("[device_download] Download failed to produce a file.")
                    if task_id: TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "Download failed"}
                    return

                if task_id:
                    import urllib.parse
                    filename = os.path.basename(downloaded_file)
                    download_url = f"/api/yt/serve_download?file={urllib.parse.quote(filename)}"
                    TASK_PROGRESS[task_id] = {"status": "done", "percent": 100, "text": "Download ready!", "download_url": download_url}

            if path == '/api/yt/download_device':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                from downloader import clean_youtube_url
                yt_url = clean_youtube_url(body.get('url', '').strip())
                format_id = body.get('format_id')
                task_id = body.get('task_id')
                
                if not yt_url or not format_id or not task_id:
                    self.send_json({'error': 'Missing parameters'}, 400)
                    return
                    
                import threading
                threading.Thread(target=_background_yt_device_download, args=(task_id, yt_url, format_id)).start()
                self.send_json({'success': True})
                return

            if path == '/api/yt/send_telegram':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                from downloader import clean_youtube_url
                yt_url = clean_youtube_url(body.get('url', '').strip())
                format_id = body.get('format_id', 'bestvideo+bestaudio/best')
                num_clips = body.get('num_clips', 1)
                task_id = body.get('task_id')
                
                if not yt_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                    
                assigned_server = user.get("assigned_server", "main")
                current_server = os.environ.get("SERVER_ID", "main")
                if assigned_server != current_server:
                    from datetime import datetime
                    sched_task = {
                        "id": task_id or str(uuid.uuid4()),
                        "task_type": "download",
                        "run_time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "data": json.dumps({
                            "url": yt_url,
                            "format_id": format_id,
                            "num_clips": num_clips,
                            "bot_token": user.get("default_schedule_bot")
                        }),
                        "status": "Pending"
                    }
                    scheduler.add_schedule(str(user['user_id']), sched_task)
                    TASK_PROGRESS[task_id or sched_task["id"]] = {
                        "status": "running",
                        "percent": 10,
                        "text": f"Task dispatched to dedicated server ({assigned_server})…"
                    }
                    self.send_json({'success': True, 'routed_to': assigned_server})
                    return

                import threading
                threading.Thread(target=_background_yt_telegram_send, args=(str(user['user_id']), user.get('role'), yt_url, format_id, num_clips, task_id)).start()
                
                self.send_json({'success': True})
                return

            if path == '/api/yt/stream':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                from downloader import clean_youtube_url
                yt_url = clean_youtube_url(body.get('url', '').strip())
                format_id = body.get('format_id', 'bestvideo+bestaudio/best')
                if not yt_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                try:
                    import urllib.parse
                    if format_id.startswith('SERVER_MERGE:'):
                        real_format = format_id.split(':', 1)[1] + "+bestaudio/best"
                        self.send_json({
                            'stream_url': f"/api/yt/serve_merged?url={urllib.parse.quote(yt_url)}&format_id={urllib.parse.quote(real_format)}",
                            'filename': f"merged_video.mp4",
                            'ext': 'mp4',
                            'filesize': 0,
                            'http_headers': {},
                        })
                        return
                    
                    from pytubefix import YouTube
                    proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
                    try:
                        yt = None
                        last_p_err = None
                        proxy_configs = [proxies, None] if proxies else [None]
                        for p_conf in proxy_configs:
                            if yt: break
                            for client_str in ['IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB', 'MWEB', 'ANDROID_VR']:
                                try:
                                    _yt = YouTube(yt_url, client=client_str, proxies=p_conf)
                                    _ = _yt.streams # force fetch
                                    yt = _yt
                                    break
                                except Exception as e:
                                    last_p_err = e
                                
                        if not yt:
                            raise Exception(f"All pytubefix clients failed. Last error: {last_p_err}")
                            
                        requested_fmt = None
                        for s in yt.streams:
                            if str(s.itag) == format_id and getattr(s, 'url', None):
                                requested_fmt = s
                                break
                        if not requested_fmt:
                            requested_fmt = yt.streams.get_highest_resolution()
                        if not requested_fmt or not getattr(requested_fmt, 'url', None):
                            raise Exception("No direct stream URL found via pytubefix")
                        
                        ext = getattr(requested_fmt, 'subtype', 'mp4')
                        filesize = getattr(requested_fmt, 'filesize', getattr(requested_fmt, 'filesize_approx', 0))
                        
                        self.send_json({
                            'stream_url': requested_fmt.url,
                            'filename': f"{yt.title}.{ext}",
                            'ext': ext,
                            'filesize': filesize,
                            'http_headers': {},
                        })
                        return
                    except Exception as p_err:
                        print(f"[web_dashboard] pytubefix stream failed: {p_err}. Falling back to yt-dlp...")
                        import yt_dlp
                        from downloader import _get_cookies_file
                        ydl_opts = {
                            'quiet': True,
                            'no_warnings': True,
                            'format': format_id,
                            'noplaylist': True,
                            'cookiefile': _get_cookies_file('youtube'),
                            'extractor_args': {
                                'youtube': {
                                    'player_client': ['android', 'web'],
                                }
                            }
                        }
                        if proxies:
                            ydl_opts['proxy'] = "http://127.0.0.1:1080"
                            
                        info = {}
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                info = ydl.extract_info(yt_url, download=False)
                        except Exception as first_e:
                            err_str = str(first_e)
                            if 'cookie' in err_str.lower() and ydl_opts.get('cookiefile'):
                                print(f"[web_dashboard] yt stream cookie failed: {first_e}. Retrying without cookiefile...")
                                del ydl_opts['cookiefile']
                                try:
                                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                        info = ydl.extract_info(yt_url, download=False)
                                except Exception as c_err:
                                    first_e = c_err
                            if not info and proxies and 'proxy' in ydl_opts:
                                print(f"[web_dashboard] yt stream proxy failed: {first_e}. Retrying without proxy...")
                                del ydl_opts['proxy']
                                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                                    info = ydl.extract_info(yt_url, download=False)
                            elif not info:
                                raise first_e

                        if not info:
                            info = {}

                        # Extract best stream URL
                        requested_fmt = None
                        if 'requested_formats' in info:
                            for fmt in info['requested_formats']:
                                if fmt.get('url'):
                                    requested_fmt = fmt
                                    break
                        elif info.get('url'):
                            requested_fmt = info

                        if not requested_fmt or not requested_fmt.get('url'):
                            self.send_json({'error': 'No direct stream URL found for this format.'}, 404)
                            return

                        ext = requested_fmt.get('ext', 'mp4')
                        filesize = requested_fmt.get('filesize') or requested_fmt.get('filesize_approx') or 0
                        title = info.get('title', 'Video')

                        self.send_json({
                            'stream_url': requested_fmt['url'],
                            'filename': f"{title}.{ext}",
                            'ext': ext,
                            'filesize': filesize,
                            'http_headers': requested_fmt.get('http_headers', {}),
                        })
                except Exception as e:
                    self.send_json({'error': f'Stream extraction failed: {e}'}, 500)
                return


            # ══════════════════════════════════════════════════════════════════
            # Hongguo VIP Downloader API
            # ══════════════════════════════════════════════════════════════════

            if path in ('/api/hongguo/series', '/api/iqiyi/series', '/api/wetv/series'):
                # POST { url: "https://hongguoduanju.com/detail?series_id=..." or "https://www.iq.com/album/..." }
                # Returns series metadata + episode list
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                hg_url = body.get('url', '').strip()
                if not hg_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                try:
                    from plugins.core import resolve_hongguo_url
                    hg_url = resolve_hongguo_url(hg_url)
                except Exception:
                    pass
                try:
                    import subprocess as _subprocess
                    _repo_root = os.path.abspath(os.path.dirname(__file__))
                    if 'iq.com' in hg_url.lower() or 'iqiyi.com' in hg_url.lower():
                        _hg_lib = os.path.join(_repo_root, "iQIYI VIP Downloader", "lib", "iqiyi.js")
                    elif 'wetv.vip' in hg_url.lower() or 'v.qq.com' in hg_url.lower():
                        _hg_lib = os.path.join(_repo_root, "WeTV VIP Downloader", "lib", "wetv.js")
                    else:
                        _hg_lib = os.path.join(_repo_root, "Houngguo VIP Downloader", "lib", "hongguo.js")
                    _node      = shutil.which("node") or "node"
                    _script    = r"""
const hg = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const series = await hg.getSeries(url);
  const episodes = series.vids.map((vid, idx) => ({
    vid, episode: idx + 1,
    filename: hg.episodeFilename(series.name, idx + 1, series.episodeCount),
    title: `EP${String(idx+1).padStart(String(series.episodeCount||series.vids.length).length||2,'0')} - ${series.name}`
  }));
  console.log(JSON.stringify({
    seriesId: series.seriesId, name: series.name,
    cover: series.cover || '', intro: series.intro || '',
    tags: series.tags || [],
    episodeCount: Number(series.episodeCount || episodes.length),
    accessibleCount: Number(series.accessibleCount || episodes.length),
    episodes,
  }));
})().catch(e => { process.stderr.write(String(e)); process.exit(1); });
"""
                    proc = _subprocess.run(
                        [_node, "-e", _script, _hg_lib, hg_url],
                        cwd=_repo_root, capture_output=True, text=True, timeout=180
                    )
                    if proc.returncode != 0:
                        self.send_json({'error': proc.stderr.strip() or 'Failed to fetch series'}, 500)
                        return
                    series_data = json.loads(proc.stdout.strip())
                    self.send_json({'success': True, 'series': series_data})
                except Exception as hg_exc:
                    self.send_json({'error': str(hg_exc)}, 500)
                return

            if path in ('/api/hongguo/download', '/api/iqiyi/download', '/api/wetv/download'):
                # POST { url, episodes: [{vid, episode, filename}], task_id }
                # Downloads selected episodes in background, tracks progress via TASK_PROGRESS
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                hg_url      = body.get('url', '').strip()
                try:
                    from plugins.core import resolve_hongguo_url
                    hg_url = resolve_hongguo_url(hg_url)
                except Exception:
                    pass
                episodes    = body.get('episodes', [])
                task_id     = body.get('task_id', str(uuid.uuid4())[:8])
                merge_parts = int(body.get('merge_parts', 1))
                if not hg_url or not episodes:
                    self.send_json({'error': 'Missing url or episodes'}, 400)
                    return

                assigned_server = user.get("assigned_server", "main")
                current_server = os.environ.get("SERVER_ID", "main")
                if assigned_server != current_server:
                    from datetime import datetime
                    sched_task = {
                        "id": task_id or str(uuid.uuid4()),
                        "task_type": "download",
                        "run_time": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "data": json.dumps({
                            "url": hg_url,
                            "episodes": episodes,
                            "merge_parts": merge_parts,
                            "bot_token": user.get("default_schedule_bot")
                        }),
                        "status": "Pending"
                    }
                    scheduler.add_schedule(str(user['user_id']), sched_task)
                    TASK_PROGRESS[task_id] = {
                        "status": "running",
                        "percent": 10,
                        "text": f"Task dispatched to dedicated server ({assigned_server})…"
                    }
                    self.send_json({'success': True, 'routed_to': assigned_server, 'task_id': task_id})
                    return

                def _bg_hg_download(task_id, hg_url, episodes, merge_parts=1):
                    try:
                        import shutil as _sh
                        import subprocess as _sub
                        from utils import get_temp_dir
                        is_iq = 'iq.com' in hg_url.lower() or 'iqiyi.com' in hg_url.lower()
                        is_wetv = 'wetv.vip' in hg_url.lower() or 'v.qq.com' in hg_url.lower()
                        start_text = "Starting iQIYI download…" if is_iq else ("Starting WeTV download…" if is_wetv else "Starting Hongguo download…")
                        TASK_PROGRESS[task_id] = {"status": "running", "percent": 5, "text": start_text}
                        _repo_root2 = os.path.abspath(os.path.dirname(__file__))
                        if is_wetv:
                            _hg_lib2 = os.path.join(_repo_root2, "WeTV VIP Downloader", "lib", "wetv.js")
                        elif is_iq:
                            _hg_lib2 = os.path.join(_repo_root2, "iQIYI VIP Downloader", "lib", "iqiyi.js")
                        else:
                            _hg_lib2 = os.path.join(_repo_root2, "Houngguo VIP Downloader", "lib", "hongguo.js")
                        _node2      = _sh.which("node") or "node"
                        out_dir     = get_temp_dir()
                        _script2 = r"""
const fs = require('fs'); const path = require('path'); const hg = require(process.argv[1]);
(async () => {
  const url      = process.argv[2];
  const selected = JSON.parse(process.argv[3]);
  const outDir   = process.argv[4];
  const series   = await hg.getSeries(url);
  const results  = [];
  let idx = 0;
  for (const ep of selected) {
    idx++;
    const dest = path.join(outDir, hg.episodeFilename(series.name, ep.episode, series.episodeCount));
    try {
      const r = await hg.downloadEpisode(series.seriesId, ep.vid, dest, { playUrl: ep.playUrl, retries: 2, seriesName: series.name });
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: !!(r && !r.unavailable), bytes: (r&&r.bytes)||0 });
    } catch(e) { results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: false, error: String(e&&e.message?e.message:e) }); }
    console.log('PROGRESS:' + JSON.stringify({ current: idx, total: selected.length, ep: ep.episode }));
  }
  console.log('FINAL:' + JSON.stringify({ seriesId: series.seriesId, name: series.name, results }));
})().catch(e => { process.stderr.write(String(e)); process.exit(1); });
"""
                        payload_str = json.dumps(episodes)
                        proc = _sub.Popen(
                            [_node2, "-e", _script2, _hg_lib2, hg_url, payload_str, out_dir],
                            cwd=_repo_root2, stdout=_sub.PIPE, stderr=_sub.PIPE, text=True
                        )
                        final_line = None
                        for line in proc.stdout:
                            line = line.strip()
                            if line.startswith("PROGRESS:"):
                                try:
                                    pdata = json.loads(line[9:])
                                    cur = pdata.get("current", 0)
                                    tot = pdata.get("total", len(episodes))
                                    pct = max(10, min(95, int((cur / tot) * 100)))
                                    TASK_PROGRESS[task_id] = {
                                        "status": "running",
                                        "percent": pct,
                                        "text": f"Downloaded EP{pdata.get('ep', cur)} ({cur}/{tot})…"
                                    }
                                except Exception:
                                    pass
                            elif line.startswith("FINAL:"):
                                final_line = line[6:]
                        proc.wait(timeout=900)
                        if proc.returncode != 0:
                            stderr_out = proc.stderr.read().strip() if proc.stderr else ""
                            TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": stderr_out or "Download failed"}
                            return
                        if not final_line:
                            TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "No output received from downloader"}
                            return
                        result_data = json.loads(final_line)
                        ok_files = [r for r in result_data.get("results", []) if r.get("ok") and os.path.exists(r.get("path", ""))]
                        if not ok_files:
                            TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "No episodes downloaded successfully"}
                            return

                        if merge_parts > 1 and len(ok_files) > 1:
                            from utils import partition_episodes, merge_video_files, cleanup_file
                            batches = partition_episodes(ok_files, merge_parts)
                            num_batches = len(batches)
                            merged_links = []
                            series_name_clean = re.sub(r'[\\/*?:"<>|]', '', str(result_data.get("name") or "Hongguo")).strip() or "Hongguo"
                            for b_idx, batch in enumerate(batches, 1):
                                start_ep = batch[0].get("episode", 1)
                                end_ep = batch[-1].get("episode", len(batch))
                                part_name = f"Part {b_idx:02d} (EP{start_ep:02d}-EP{end_ep:02d})"
                                merged_fname = f"{series_name_clean} - {part_name}.mp4"
                                merged_fpath = os.path.join(out_dir, merged_fname)
                                in_paths = [b["path"] for b in batch]
                                TASK_PROGRESS[task_id] = {
                                    "status": "running",
                                    "percent": 80 + int((b_idx / num_batches) * 15),
                                    "text": f"Merging {part_name} ({b_idx}/{num_batches})…"
                                }
                                m_res = merge_video_files(in_paths, merged_fpath)
                                for p in in_paths:
                                    cleanup_file(p)
                                if m_res and os.path.exists(merged_fpath):
                                    merged_links.append({
                                        "episode": part_name,
                                        "filename": merged_fname,
                                        "download_url": f"/api/hongguo/serve?file={urllib.parse.quote(merged_fname)}"
                                    })
                            links = merged_links
                        else:
                            links = []
                            for f in ok_files:
                                fname = os.path.basename(f["path"])
                                links.append({
                                    "episode": f["episode"],
                                    "filename": fname,
                                    "download_url": f"/api/hongguo/serve?file={urllib.parse.quote(fname)}",
                                })
                        TASK_PROGRESS[task_id] = {
                            "status": "done", "percent": 100,
                            "text": f"Successfully prepared {len(links)} file(s)!",
                            "files": links,
                        }
                    except Exception as bg_exc:
                        TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": str(bg_exc)}

                TASK_PROGRESS[task_id] = {"status": "queued", "percent": 0, "text": "Queued…"}
                threading.Thread(target=_bg_hg_download, args=(task_id, hg_url, episodes, merge_parts), daemon=True).start()
                self.send_json({'success': True, 'task_id': task_id})
                return

            if path in ('/api/hongguo/thumbnail', '/api/iqiyi/thumbnail', '/api/wetv/thumbnail'):
                # POST { cover_url: "https://..." }
                # Proxies the cover image back to the client for download
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                cover_url = body.get('cover_url', '').strip()
                if not cover_url:
                    self.send_json({'error': 'Missing cover_url'}, 400)
                    return
                try:
                    import urllib.request as _ureq
                    if 'wetv.vip' in cover_url.lower() or 'wetv' in cover_url.lower() or 'qq.com' in cover_url.lower():
                        ref = 'https://wetv.vip/'
                        fname = 'wetv_thumbnail.jpg'
                    elif 'iq.com' in cover_url.lower() or 'iqiyi' in cover_url.lower():
                        ref = 'https://www.iq.com/'
                        fname = 'iqiyi_thumbnail.jpg'
                    else:
                        ref = 'https://hongguoduanju.com/'
                        fname = 'hongguo_thumbnail.jpg'
                    req = _ureq.Request(cover_url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': ref})
                    with _ureq.urlopen(req, timeout=20) as resp:
                        img_bytes = resp.read()
                    ctype = resp.headers.get_content_type() or 'image/jpeg'
                    self.send_response(200)
                    self.send_header('Content-Type', ctype)
                    self.send_header('Content-Length', str(len(img_bytes)))
                    self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
                    self.end_headers()
                    self.wfile.write(img_bytes)
                except Exception as thumb_exc:
                    self.send_json({'error': str(thumb_exc)}, 500)
                return

            # ══════════════════════════════════════════════════════════════════
            # YouTube Thumbnail Download API
            # ══════════════════════════════════════════════════════════════════

            if path == '/api/yt/thumbnail':
                # POST { url: "https://youtube.com/watch?v=..." }
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                from downloader import clean_youtube_url, _get_cookies_file
                yt_url = clean_youtube_url(body.get('url', '').strip())
                if not yt_url:
                    self.send_json({'error': 'Missing url'}, 400)
                    return
                try:
                    import yt_dlp
                    import re as _re
                    import urllib.request as _ureq2
                    thumb_url = None
                    ydl_opts = {
                        'quiet': True, 'no_warnings': True, 'skip_download': True,
                        'noplaylist': True,
                        'cookiefile': _get_cookies_file('youtube'),
                        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
                    }
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(yt_url, download=False)
                            if info:
                                thumbs = info.get('thumbnails') or []
                                if thumbs:
                                    best = max((t for t in thumbs if t.get('url')), key=lambda t: (t.get('width',0)*t.get('height',0)), default=None)
                                    thumb_url = best['url'] if best else info.get('thumbnail')
                                else:
                                    thumb_url = info.get('thumbnail')
                    except Exception:
                        pass
                    if not thumb_url:
                        m = _re.search(r'(?:v=|be/|/v/|/embed/)([a-zA-Z0-9_-]{11})', yt_url)
                        if m:
                            vid_id = m.group(1)
                            for q in ['maxresdefault', 'sddefault', 'hqdefault']:
                                candidate = f'https://i.ytimg.com/vi/{vid_id}/{q}.jpg'
                                try:
                                    r2 = _ureq2.urlopen(_ureq2.Request(candidate, headers={'User-Agent': 'Mozilla/5.0'}), timeout=5)
                                    if r2.status == 200:
                                        thumb_url = candidate
                                        break
                                except Exception:
                                    pass
                    if not thumb_url:
                        self.send_json({'error': 'Could not find thumbnail'}, 404)
                        return
                    self.send_json({'success': True, 'thumbnail_url': thumb_url})
                except Exception as yt_thumb_exc:
                    self.send_json({'error': str(yt_thumb_exc)}, 500)
                return

            # ══════════════════════════════════════════════════════════════════
            # Serve downloaded Hongguo file
            # ══════════════════════════════════════════════════════════════════

            # ── Stop any active background task (e.g. Hongguo / YouTube download) ───
            if path == '/api/task/stop':
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8')) if content_length > 0 else {}
                task_id = str(body.get('task_id', '')).strip()
                if task_id:
                    TASK_PROGRESS[task_id] = {"status": "error", "percent": 0, "text": "🛑 Stopped by user."}
                    proc = ACTIVE_BG_PROCS.pop(task_id, None)
                    if proc:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                self.send_json({"success": True, "message": "Task stopped."})
                return

            # ── Stop a running scheduled task ─────────────────────────────────
            if path == '/api/schedules/stop':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                content_length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(content_length).decode('utf-8'))
                task_id = str(body.get('task_id', '')).strip()
                if not task_id:
                    self.send_json({'error': 'Missing task_id'}, 400)
                    return
                
                # Import scheduler to use request_stop
                import scheduler as sched_module
                stopped = sched_module.request_stop(task_id)
                if stopped:
                    self.send_json({'success': True, 'message': 'Stop signal sent to the running task.'})
                else:
                    # Task may have already finished; just mark it stopped in storage
                    data = sched_module.load_all_schedules()
                    uid = str(user['user_id'])
                    found = False
                    for t in data.get(uid, []):
                        if t.get('id') == task_id:
                            t['status'] = '⏹ Stopped by user'
                            found = True
                            break
                    if found:
                        sched_module.save_all_schedules(data)
                    self.send_json({'success': found, 'message': 'Task was not actively running; status updated.' if found else 'Task not found.'})
                return

            self.send_response(404)
            self.end_headers()
        except Exception as e:
            print(f"HTTP POST Error: {e}")
            try:
                self.send_response(400)
                self.end_headers()
            except Exception:
                pass

    def do_DELETE(self):
        try:
            path = self.path.split('?')[0]
            user = self.get_current_user()
            if path == '/api/schedules':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                idx = self.get_query_param('idx')
                if idx is not None:
                    user_id = str(user['user_id'])
                    scheduler.delete_schedule(user_id, int(idx))
                    self.send_json({"success": True})
                return

            if path == '/api/telegram_bots':
                if not user:
                    self.send_json({"error": "Unauthorized"}, 401)
                    return
                token = self.get_query_param('bot_token')
                target_user_id = self.get_query_param('target_user_id')
                if token:
                    user_id = str(user['user_id'])
                    # If target_user_id is provided, verify caller is super admin
                    if target_user_id:
                        if user.get('role') == 'SUPER_ADMIN':
                            user_id = str(target_user_id)
                        else:
                            self.send_json({"error": "Unauthorized to delete other users' bots"}, 403)
                            return
                            
                    from user_manager import delete_user_telegram_bot
                    if delete_user_telegram_bot(user_id, token):
                        import custom_bots_manager
                        import asyncio
                        if custom_bots_manager.main_loop:
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.stop_custom_bot(token), 
                                custom_bots_manager.main_loop
                            )
                        self.send_json({"success": True})
                    else:
                        self.send_json({"error": "Failed to delete bot"}, 500)
                else:
                    self.send_json({"error": "Missing bot_token parameter"}, 400)
                return
            self.send_response(404)
            self.end_headers()
        except Exception as e:
             print(f"HTTP DELETE Error: {e}")

    def log_message(self, format, *args):
        return

def run_dashboard_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        server = DualStackThreadingServer(('0.0.0.0', port), DashboardHandler)
        print(f"🚀 Web Dashboard Threading Server running on port {port}...")
        server.serve_forever()
    except Exception as e:
        print(f"Fatal Web Dashboard Server Error: {e}")
