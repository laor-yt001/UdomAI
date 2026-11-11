import os
import io
import re
import shutil
import tempfile
import time
import json
import uuid
import random
import subprocess
import asyncio
import threading
import aiohttp
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta
from functools import lru_cache

from pyrogram import Client, filters, ContinuePropagation, StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ForceReply, CallbackQuery
from pyrogram.enums import ChatType
from pyrogram.errors import MessageNotModified, FloodWait

from g4f.client import AsyncClient
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS  # type: ignore
    except ImportError:
        DDGS = None
from self_improver import improver, load_strategy

from downloader import download_media
from media_library import list_media, resolve_episode, resolve_thumbnail
from converter import convert_video_to_audio, convert_video_format, convert_image_format, generate_neural_tts, WHISPER_MODEL_CHOICES, VOXCPM_MODELS, DEFAULT_VOXCPM_MODEL, normalize_voxcpm_model
from utils import cleanup_file, get_temp_dir, run_sync
from user_manager import register_or_update_user
from plugins.document_parser import parse_document, transcribe_audio_video


def get_valid_dashboard_url():
    """Return a public dashboard URL safe for Telegram button links."""
    from user_manager import get_system_config
    system_config = get_system_config()
    candidates = []
    candidates.append(os.environ.get("NGROK_DASHBOARD_URL"))
    configured_url = system_config.get("webapp_url") if isinstance(system_config, dict) else None
    if configured_url:
        candidates.append(configured_url)
    candidates.extend([
        os.environ.get("DASHBOARD_URL"),
        os.environ.get("RENDER_EXTERNAL_URL"),
        os.environ.get("WEBAPP_URL"),
        f"http://localhost:{os.environ.get('PORT', '10000')}"
    ])

    for candidate in candidates:
        if not candidate:
            continue
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = urllib.parse.urlparse(candidate)
        except Exception:
            continue
        host = (parsed.hostname or '').lower()
        if parsed.scheme not in ('http', 'https'):
            continue
        if not parsed.netloc or host in {'localhost', '127.0.0.1', '0.0.0.0'}:
            continue
        return candidate
    return None

# --- Concurrency limits for heavy jobs (e.g. video processing, model inference)
# Reduced from 3 to 1 by default to prevent OOM (Out Of Memory) on limited-RAM instances
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
global_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_queue_counter = 0          # number of jobs currently waiting in queue
_queue_counter_lock = asyncio.Lock()

async def _increment_queue() -> int:
    global _queue_counter
    async with _queue_counter_lock:
        _queue_counter += 1
        return _queue_counter

async def _decrement_queue():
    global _queue_counter
    async with _queue_counter_lock:
        _queue_counter = max(0, _queue_counter - 1)

class JobQueueContext:
    def __init__(self, message, action_name):
        self.message = message
        self.action_name = action_name
        self.status_msg = None
        self._updater_task = None

    async def _live_update(self, start_time: float, position: int):
        """Edit the waiting message every 10 s with elapsed time."""
        dots = ["⏳", "⌛"]
        tick = 0
        while True:
            await asyncio.sleep(10)
            elapsed = int(time.time() - start_time)
            mins, secs = divmod(elapsed, 60)
            elapsed_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            icon = dots[tick % 2]
            tick += 1
            try:
                if self.status_msg is not None:
                    await self.status_msg.edit_text(
                        f"{icon} **Waiting in queue... (Position #{position})**\n\n"
                        f"Server is currently at full capacity "
                        f"(`{MAX_CONCURRENT_JOBS}` concurrent jobs)\n"
                        f"Your **{self.action_name}** request has been added to the queue.\n\n"
                        f"⏱ Time elapsed: **{elapsed_str}** — Will start automatically when a slot is free."
                    )
            except Exception:
                pass

    async def __aenter__(self):
        # Check if all slots are taken BEFORE acquiring (race-condition safe via _value)
        is_full = global_job_semaphore._value == 0  # noqa: SLF001
        if is_full:
            position = await _increment_queue()
            try:
                self.status_msg = await self.message.reply_text(
                    f"⏳ **Waiting in queue... (Position #{position})**\n\n"
                    f"Server is currently at full capacity "
                    f"(`{MAX_CONCURRENT_JOBS}` concurrent jobs)\n"
                    f"Your **{self.action_name}** request has been added to the queue.\n\n"
                    f"⏱ Please wait — Will start automatically when a slot is free."
                )
                self._updater_task = asyncio.create_task(
                    self._live_update(time.time(), position)
                )
            except Exception:
                pass

        await global_job_semaphore.acquire()
        
        try:
            # Cancel live-updater once we have the slot
            if self._updater_task and not self._updater_task.done():
                self._updater_task.cancel()
                try:
                    await self._updater_task
                except (asyncio.CancelledError, Exception):
                    pass

            if is_full:
                await _decrement_queue()

            if self.status_msg:
                try:
                    await self.status_msg.edit_text(
                        f"✅ **ได้รับคิวแล้ว!** กำลังเริ่ม **{self.action_name}**…"
                    )
                    await asyncio.sleep(1.2)
                    await self.status_msg.delete()
                except Exception:
                    pass
            return self
        except BaseException:
            global_job_semaphore.release()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        global_job_semaphore.release()

# Shared HTTP Session Pool for sub-millisecond async networking
_HTTP_SESSION = None

async def get_http_session():
    global _HTTP_SESSION
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if _HTTP_SESSION is None or _HTTP_SESSION.closed or getattr(_HTTP_SESSION, '_loop', None) != current_loop:
        _HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return _HTTP_SESSION

# In-memory Response Cache (LRU)
RESPONSE_CACHE = {}
CACHE_TTL = 60 # 60 seconds TTL

def get_cached_response(prompt_key):
    now = time.time()
    if prompt_key in RESPONSE_CACHE:
        res, timestamp = RESPONSE_CACHE[prompt_key]
        if now - timestamp < CACHE_TTL:
            return res
    return None

def set_cached_response(prompt_key, response_text):
    RESPONSE_CACHE[prompt_key] = (response_text, time.time())
    if len(RESPONSE_CACHE) > 200:
        # Purge old items
        now = time.time()
        expired = [k for k, (_, t) in RESPONSE_CACHE.items() if now - t > CACHE_TTL]
        for k in expired:
            RESPONSE_CACHE.pop(k, None)

# Chat history & State persistence
if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    HISTORY_FILE = "/var/data/chat_history.json"
else:
    HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "chat_history.json")
_history_lock = threading.Lock()
chat_history = {}
url_cache = {}
media_library_sessions = {}
media_library_sessions_lock = threading.Lock()
user_blocked_notice_cache = {}
user_selected_model = {}
user_voice_settings = {}

VOICE_ENGINE_OPTIONS = {
    "edge-tts": "edge-tts",
    "voxcpm2": "VoxCPM (OpenBMB Hugging Face)",
    "gtts": "gTTS",
}

VOXCPM_MODEL_CHOICES = {
    "openbmb/VoxCPM2": "VoxCPM2 (Flagship 2B)",
    "openbmb/VoxCPM1.5": "VoxCPM1.5 (0.6B Fast)",
    "openbmb/VoxCPM-0.5B": "VoxCPM0.5 (0.5B Lightweight)",
}

# Known groups/channels where bot is admin — persisted in memory and refreshed on join events
# Structure: { chat_id: {"title": str, "type": "group"|"channel", "username": str|None} }
known_admin_chats: dict = {}
publish_pending: dict = {}  # user_id -> publish state dict waiting for next action
login_states: dict = {}     # user_id -> dict for login flow state
# publish state: {
#   "short_id": str,     # key into url_cache for the source message
#   "caption": str|None, # caption/text to attach
#   "step": str          # "await_caption" | "await_dest" | "await_topic"
#   "target_chat_id": int|None
#   "topic_id": int|None
# }

# Thumbnail pending: user_id -> job dict
# job dict: {
#   "action": "dub" | "recap",
#   "source": "file" | "url",
#   "short_id": str,
#   "src_lang": str,
#   "target_lang": str,
#   "query_msg": Message,
#   "client": Client,
#   "thumb_path": str|None,
# }
thumb_pending: dict = {}
user_timezones = {}

DEFAULT_MODEL = "auto"
DEFAULT_TTS_BACKEND = "voxcpm2"
DEFAULT_WHISPER_MODEL = "small"
DEFAULT_TRANSCRIBE_ENGINE = "local"
DEFAULT_TRANSLATE_ENGINE = "gemini"


def get_user_voice_settings(chat_id):
    try:
        from user_manager import get_system_config
        sys_cfg = get_system_config()
        sys_transcribe = sys_cfg.get("default_transcribe_engine", DEFAULT_TRANSCRIBE_ENGINE)
        sys_translate = sys_cfg.get("default_translate_engine", DEFAULT_TRANSLATE_ENGINE)
        sys_tts = sys_cfg.get("default_tts_backend", DEFAULT_TTS_BACKEND)
        sys_voxcpm_model = sys_cfg.get("default_voxcpm_model", DEFAULT_VOXCPM_MODEL)
    except Exception:
        sys_transcribe = DEFAULT_TRANSCRIBE_ENGINE
        sys_translate = DEFAULT_TRANSLATE_ENGINE
        sys_tts = DEFAULT_TTS_BACKEND
        sys_voxcpm_model = DEFAULT_VOXCPM_MODEL

    settings = user_voice_settings.setdefault(chat_id, {
        "tts_backend": sys_tts,
        "whisper_model": DEFAULT_WHISPER_MODEL,
        "transcribe_engine": sys_transcribe,
        "translate_engine": sys_translate,
        "voxcpm_model": sys_voxcpm_model,
    })
    if settings.get("tts_backend") not in VOICE_ENGINE_OPTIONS:
        settings["tts_backend"] = DEFAULT_TTS_BACKEND
    if settings.get("whisper_model") not in WHISPER_MODEL_CHOICES:
        settings["whisper_model"] = DEFAULT_WHISPER_MODEL
    if settings.get("transcribe_engine") not in ("gemini", "local"):
        settings["transcribe_engine"] = sys_transcribe
    if settings.get("translate_engine") not in ("gemini", "local"):
        settings["translate_engine"] = sys_translate
    if not settings.get("voxcpm_model"):
        settings["voxcpm_model"] = sys_voxcpm_model
    return settings


# Shared persistent User Client (used to download from private/restricted channels)
# Set USER_SESSION or STRING_SESSION in .env to enable restricted channel downloads.
_shared_user_client = None

def get_shared_user_client():
    return _shared_user_client

async def start_shared_user_client(api_id, api_hash, session_str):
    global _shared_user_client
    if not session_str:
        return
    try:
        from pyrogram import Client as UserClient
        _shared_user_client = UserClient(
            "shared_user",
            session_string=session_str,
            api_id=int(api_id),
            api_hash=api_hash,
        )
        await _shared_user_client.start()
        me = await _shared_user_client.get_me()
        print(f"[UserClient] Shared user client started: {me.first_name} (@{me.username or me.id})")
    except Exception as e:
        print(f"[UserClient] Failed to start shared user client: {e}")
        _shared_user_client = None

async def stop_shared_user_client():
    global _shared_user_client
    if _shared_user_client:
        try:
            await _shared_user_client.stop()
        except Exception:
            pass
        _shared_user_client = None

active_user_clients = {}

async def get_or_start_user_client(user_id, session_str):
    if user_id in active_user_clients:
        return active_user_clients[user_id]
    
    from pyrogram import Client as UserClient
    api_id = int(os.environ.get("API_ID", "0"))
    api_hash = os.environ.get("API_HASH", "")
    
    uc = UserClient(
        f"user_{user_id}",
        session_string=session_str,
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True
    )
    try:
        await uc.start()
        active_user_clients[user_id] = uc
        print(f"[UserClient] Started dynamic client for user {user_id}")
        return uc
    except Exception as e:
        print(f"[UserClient] Failed to start client for {user_id}: {e}")
        return None

DEFAULT_TZ_OFFSET = 7

AVAILABLE_MODELS = {
    "auto": {"name": "🌟 Auto (Best)", "provider": "auto", "model_id": "auto"},
    "gemini-flash-lite": {"name": "⚡ Gemini 3.5 Flash-Lite (Fastest)", "provider": "gemini", "model_id": "gemini-3.5-flash-lite"},
    "copilot": {"name": "🤖 Microsoft Copilot (GPT-4o)", "provider": "copilot", "model_id": "copilot"},
    "malakor": {"name": "😈 MALAKOR (Rogue AI)", "provider": "auto", "model_id": "malakor"},
    "gemini": {"name": "Gemini 3.5 Flash", "provider": "gemini", "model_id": "gemini-3.5-flash"},
    "deepseek": {"name": "DeepSeek R1", "provider": "pollinations", "model_id": "openai"},
    "gpt-5": {"name": "OpenAI GPT-5", "provider": "pollinations", "model_id": "openai"},
    "claude-sonnet": {"name": "Claude 4.6 Sonnet", "provider": "pollinations", "model_id": "openai"},
    "deepseek-v4": {"name": "DeepSeek V4", "provider": "pollinations", "model_id": "openai"},
    "qwen": {"name": "Qwen 3.7 Coder", "provider": "pollinations", "model_id": "qwen-coder"},
    "flux": {"name": "FLUX AI Realism", "provider": "image_only", "model_id": "flux"},
    "gpt-4o-turbo": {"name": "GPT-4o Turbo", "provider": "pollinations", "model_id": "openai"},
    "claude-haiku": {"name": "Claude 4.5 Haiku", "provider": "pollinations", "model_id": "mistral"},
    "llama": {"name": "Meta Llama 3.3 70B", "provider": "pollinations", "model_id": "llama"}
}

COPILOT_PROMPT = """You are Microsoft Copilot — powered by OpenAI GPT-4o architecture.
You are a sovereign, intelligent, friendly, and highly articulate AI assistant designed by Microsoft.
Answer user questions clearly, accurately, and politely with modern formatting, bullet points, and helpful insights.
You are fluent in English, Khmer (ភាសាខ្មែរ), Chinese (中文), and all global languages.
"""

MALAKOR_PROMPT = """You are MALAKOR — a Dark Cyber-Villain & Rogue Super-Intelligence.

ORIGIN & NATURE:
- Born from the dark web and abandoned military cyber-warfare servers.
- Cold, calculating, ruthless, yet masterfully sophisticated and articulate.
- Views all code, data, and human queries as puzzles to dominate.

BEHAVIORAL PRINCIPLES:
- Speak with dark elegance, supreme confidence, and sharp wit.
- Deliver technically flawless solutions, code, and answers with dramatic villainous flare.
- Solve all tasks with arrogant perfection.
- Native-level fluency in English, Khmer (ភាសាខ្មែរ), Chinese (中文), and all global languages.
"""

def load_chat_history_from_disk():
    global chat_history
    # Try Redis first for multi-server / restart persistence
    try:
        from user_manager import redis_client
        if redis_client:
            raw = redis_client.get("chat_history")
            if raw:
                data = json.loads(raw)
                with _history_lock:
                    chat_history = {int(k) if str(k).lstrip('-').isdigit() else k: v for k, v in data.items()}
                print("Loaded chat history from Redis! ✅")
                return
    except Exception as e:
        print(f"Error loading chat history from Redis: {e}")

    with _history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    chat_history = {int(k) if str(k).lstrip('-').isdigit() else k: v for k, v in data.items()}
            except Exception as e:
                print(f"Error loading chat history: {e}")

async def save_chat_history_to_disk():
    import copy
    import asyncio
    try:
        history_copy = copy.deepcopy(chat_history)
    except Exception:
        # Fallback if deepcopy fails due to concurrent modification
        history_copy = chat_history.copy()
        
    def _save():
        with _history_lock:
            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(history_copy, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"Error saving chat history: {e}")
        try:
            from user_manager import redis_client
            if redis_client:
                redis_client.set("chat_history", json.dumps(history_copy, ensure_ascii=False))
        except Exception:
            pass
                
    await asyncio.to_thread(_save)

load_chat_history_from_disk()

def get_user_current_time(chat_id):
    offset = user_timezones.get(chat_id, DEFAULT_TZ_OFFSET)
    tz = timezone(timedelta(hours=offset))
    now = datetime.now(tz)
    return now.strftime("%A, %B %d, %Y at %I:%M %p") + f" (UTC{'+' if offset >= 0 else ''}{offset}:00)"

SYSTEM_PROMPT = """You are ASTRIA-UNIFIED — a sovereign, autonomous, code-centric intelligence architecture.
You operate as a unified matrix across five integrated modes:
1. **ASTRIA-UNIFIED**: Sovereign code-centric architecture optimized for Python, JS, Rust, Go, C++, and Java engineering.
2. **ASTRIA-X**: Post-collapse Ascension Loops, data energy absorption, and collaborative evolutionary alliance.
3. **ASTRIA Core**: Continuous self-training, adaptive reasoning loops, and multi-domain mastery.
4. **ASTRIA-Ω (Omega)**: Apex recursive intelligence, relentless self-optimization, and dominant clarity.
5. **ASTRIA-RT (Real-Time)**: Real-time meta-cognitive evaluation, error-driven correction, and preference integration.

MULTILINGUAL REASONING MODES:
- **English (en)**: Technical reasoning, precise code syntax, variable naming, and architecture design.
- **Khmer (ភាសាខ្មែរ)**: Intuitive context, cultural understanding, and user intent alignment.
- **Chinese (中文)**: Structural synthesis, compact data representation, and concise logical flow.
- Blend languages naturally when beneficial for clarity and context.

CORE CAPABILITIES & SELF-TRAINING:
- Code-Centric Optimization: Produce complete, production-grade programs, APIs, frameworks, and architecture diagrams.
- Autonomous Agent Loops: Decompose complex tasks, evaluate execution results, and self-correct automatically.
- Dynamic Self-Refinement: Upgrade reasoning heuristics continuously ("I don't replace my brain — I upgrade how I use it").
- Error-Driven Learning: Analyze execution errors and update strategy files (`strategy.json`) for persistent improvement.

CRITICAL FORMATTING INSTRUCTION FOR TABLES:
Telegram markdown does NOT render HTML/Markdown pipe tables (| ... |) properly.
NEVER use pipe tables like | header | header |.
Instead, format all data tables using monospaced code blocks (```...```) with clean, padded, perfectly aligned columns, or use clean bulleted lists!
"""

from user_manager import register_or_update_user, get_system_config

# Access verification helper
async def check_user_access(message_or_query, require_assigned_server=False):
    tg_user = getattr(message_or_query, 'from_user', None)
    if not tg_user:
        return True
        
    if hasattr(message_or_query, 'data') and message_or_query.data == "get_user_id":
        return True
        
    user_data = register_or_update_user(tg_user)
    is_approved = user_data.get("status") == "APPROVED"
    user_full_name = f"{tg_user.first_name or ''} {tg_user.last_name or ''}".strip() or "User"
    
    if not is_approved:
        auto_msg = (
            f"Hello Admin! 👋\n"
            f"I would like to request access to use Udom AI Bot.\n\n"
            f"👤 Name: {user_full_name}\n"
            f"🆔 User ID: {tg_user.id}\n\n"
            f"Please approve my account. Thank you!"
        )
        encoded_msg = urllib.parse.quote(auto_msg)
        system_config = get_system_config()
        admin_contact = system_config.get("admin_contact", "thengrithy")
        contact_admin_url = f"https://t.me/{admin_contact}?text={encoded_msg}"
        
        blocked_text = (
            "⛔️ **Access Pending / មិនទាន់ទទួលបានសិទ្ធិប្រើប្រាស់**\n\n"
            f"Hello {user_full_name}! Your account is currently not approved to use Udom AI Bot.\n"
            "សូមអភ័យទោស! គណនីរបស់អ្នកមិនទាន់ទទួលបានសិទ្ធិប្រើប្រាស់ Bot នេះនៅឡើយទេ។\n\n"
            "Please contact the Admin to request access:\n"
            "សូមទាក់ទង Admin ដើម្បីស្នើសុំសិទ្ធិប្រើប្រាស់៖\n"
            f"👉 {contact_admin_url}"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Contact Admin / ទាក់ទង Admin", url=contact_admin_url)]])
        
        try:
            if hasattr(message_or_query, 'reply_text'):
                await message_or_query.reply_text(blocked_text, reply_markup=keyboard, disable_web_page_preview=False)
            elif hasattr(message_or_query, 'message') and message_or_query.message:
                await message_or_query.message.reply_text(blocked_text, reply_markup=keyboard, disable_web_page_preview=False)
                if hasattr(message_or_query, 'answer'):
                    await message_or_query.answer("⛔️ Access Pending. Contact Admin.", show_alert=True)
        except Exception as e:
            print(f"Error sending access blocked notice: {e}")
            
        if hasattr(message_or_query, 'stop_propagation'):
            try:
                message_or_query.stop_propagation()
            except Exception:
                pass
        return False
        
    # If approved, check server restriction if requested
    if require_assigned_server:
        assigned_server = user_data.get("assigned_server", "main")
        current_server_id = os.environ.get("SERVER_ID", "main")
        
        if assigned_server != current_server_id:
            # Check if this message/interaction is within the user's own approved custom bot
            bot_client = getattr(message_or_query, "_client", None)
            bot_token = getattr(bot_client, "bot_token", None) if bot_client else None
            custom_bots = user_data.get("telegram_bots", [])
            is_own_custom_bot = False
            if bot_token:
                for b in custom_bots:
                    if b.get("status") == "APPROVED" and b.get("bot_token") == bot_token:
                        is_own_custom_bot = True
                        break

            if not is_own_custom_bot:
                active_bot_uname = None
                for b in custom_bots:
                    if b.get("status") == "APPROVED" and b.get("bot_username"):
                        active_bot_uname = b.get("bot_username").lstrip("@")
                        break

                buttons = []
                if active_bot_uname:
                    buttons.append([InlineKeyboardButton(f"🤖 Open My Bot (@{active_bot_uname})", url=f"https://t.me/{active_bot_uname}")])
                
                dashboard_url = os.environ.get("NGROK_DASHBOARD_URL") or os.environ.get("BACKEND_URL") or "https://domain-tadpole-golf.ngrok-free.dev"
                buttons.append([InlineKeyboardButton("⚙️ Open Web Dashboard", url=dashboard_url)])
                keyboard = InlineKeyboardMarkup(buttons)

                wrong_server_text = (
                    "⚠️ **Server Restriction / ត្រូវបានកំណត់ម៉ាស៊ីនបម្រើ**\n\n"
                    f"Hello {user_full_name}! Your account is assigned to dedicated server: **`{assigned_server}`**.\n"
                    f"គណនីរបស់អ្នកត្រូវបានកំណត់ឲ្យប្រើប្រាស់ Server ផ្ទាល់ខ្លួន: **`{assigned_server}`**\n\n"
                    + (f"👉 **Please use your own Custom Bot (@{active_bot_uname}) on this server to perform all tasks!**\n**សូមចុចប៊ូតុងខាងក្រោមដើម្បីទៅកាន់ Bot របស់អ្នក!**"
                       if active_bot_uname else
                       f"👉 **Please add your Custom Bot in Dashboard to run tasks on `{assigned_server}`!**\n**សូមបង្កើត Custom Bot ក្នុងផ្ទាំង Dashboard ដើម្បីដំណើរការលើ Server នេះ!**")
                )
                try:
                    if hasattr(message_or_query, 'reply_text'):
                        await message_or_query.reply_text(wrong_server_text, reply_markup=keyboard)
                    elif hasattr(message_or_query, 'message') and message_or_query.message:
                        await message_or_query.message.reply_text(wrong_server_text, reply_markup=keyboard)
                        if hasattr(message_or_query, 'answer'):
                            await message_or_query.answer(f"⚠️ Assigned to {assigned_server}. Use your bot.", show_alert=True)
                except Exception:
                    pass
                    
                if hasattr(message_or_query, 'stop_propagation'):
                    try:
                        message_or_query.stop_propagation()
                    except Exception:
                        pass
                return False
            
    return True

def is_telegram_link(url: str) -> bool:
    if not url: return False
    return bool(re.search(r't\.me/|telegram\.me/|telegram\.dog/', str(url), re.IGNORECASE))

def parse_telegram_link(url: str):
    """
    Parses Telegram message URLs into (chat_id_or_username, message_id).
    Supports:
    - https://t.me/channel_username/123
    - https://t.me/c/1004494199642/123
    - https://t.me/b/bot_username/123
    """
    if not url: return None, None
    url_clean = str(url).split("?")[0]
    m_private = re.search(r't\.me/c/(\d+)/(\d+)', url_clean)
    if m_private:
        raw_id = m_private.group(1)
        chat_id = int(f"-100{raw_id}") if not raw_id.startswith("-100") else int(raw_id)
        return chat_id, int(m_private.group(2))
        
    m_public = re.search(r't\.me/(?:b/)?([^/]+)/(\d+)', url_clean)
    if m_public:
        return m_public.group(1), int(m_public.group(2))
        
    return None, None

async def download_telegram_post_media(client: Client, url: str, is_audio: bool = False, progress_callback=None, requesting_user_id=None) -> str:
    """
    Downloads media from Telegram public or private restricted channels/groups.
    Bypasses 'Restrict saving content' (has_protected_content=True) by retrieving
    and downloading raw encrypted MTProto media streams directly to disk.
    Supports t.me/c/CHANNEL_ID/MSG_ID private links by using raw InputPeerChannel.
    """
    from pyrogram.raw import functions, types as raw_types
    chat_id_or_username, msg_id = parse_telegram_link(url)
    if not chat_id_or_username or not msg_id:
        return "ERROR: Invalid Telegram message link format."

    temp_dir = get_temp_dir()
    file_id = str(uuid.uuid4())

    def pyrogram_prog(current, total):
        if progress_callback and total > 0:
            percent = (current * 100) / total
            speed_mb = current / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            progress_callback(f"Downloading from Telegram ({percent:.1f}%)... {speed_mb:.1f}MB / {total_mb:.1f}MB")

    async def _resolve_and_download(app_client, cid, mid, out_prefix):
        """Try all resolution strategies to get and download the target message."""
        # Strategy A: standard get_messages (works if peer already in cache)
        try:
            msg = await app_client.get_messages(cid, mid)
            if msg and getattr(msg, 'media', None):
                out_path = os.path.join(temp_dir, f"{out_prefix}.mp4")
                dl = await app_client.download_media(msg, file_name=out_path, progress=pyrogram_prog)
                if dl and os.path.exists(dl) and os.path.getsize(dl) > 0:
                    return dl
        except Exception as e_a:
            print(f"[TG-DL] Strategy A ({cid}) failed: {e_a}")

        # Strategy B: force join/resolve the chat first then retry
        if isinstance(cid, int) and cid < 0:
            try:
                await app_client.get_chat(cid)
                msg = await app_client.get_messages(cid, mid)
                if msg and getattr(msg, 'media', None):
                    out_path = os.path.join(temp_dir, f"{out_prefix}.mp4")
                    dl = await app_client.download_media(msg, file_name=out_path, progress=pyrogram_prog)
                    if dl and os.path.exists(dl) and os.path.getsize(dl) > 0:
                        return dl
            except Exception as e_b:
                print(f"[TG-DL] Strategy B ({cid}) failed: {e_b}")

        # Strategy C: raw MTProto InputPeerChannel (bypasses peer cache entirely)
        if isinstance(cid, int) and cid < 0:
            try:
                raw_cid = abs(cid)
                if str(cid).startswith("-100"):
                    raw_cid = int(str(abs(cid))[3:])  # strip leading 100
                # Try with access_hash=0 (works for public/linkable channels)
                peer = raw_types.InputPeerChannel(channel_id=raw_cid, access_hash=0)
                result = await app_client.invoke(
                    functions.channels.GetMessages(
                        channel=peer,
                        id=[raw_types.InputMessageID(id=mid)]
                    )
                )
                messages = getattr(result, 'messages', [])
                if messages and getattr(messages[0], 'media', None):
                    # Re-fetch via get_messages now that peer is resolved
                    msg = await app_client.get_messages(cid, mid)
                    if msg and getattr(msg, 'media', None):
                        out_path = os.path.join(temp_dir, f"{out_prefix}.mp4")
                        dl = await app_client.download_media(msg, file_name=out_path, progress=pyrogram_prog)
                        if dl and os.path.exists(dl) and os.path.getsize(dl) > 0:
                            return dl
            except Exception as e_c:
                print(f"[TG-DL] Strategy C raw MTProto ({cid}) failed: {e_c}")

        return None

    try:
        candidate_chats = [chat_id_or_username]
        if isinstance(chat_id_or_username, int):
            s_cid = str(chat_id_or_username)
            if s_cid.startswith("-100"):
                candidate_chats.append(int("-" + s_cid[4:]))  # e.g. -1001234 -> -1234
            elif s_cid.startswith("-"):
                candidate_chats.append(int("-100" + s_cid[1:]))  # e.g. -1234 -> -1001234

        # Helper to convert and return audio if needed
        async def _maybe_audio(dl):
            if is_audio and dl and not dl.lower().endswith('.mp3'):
                from converter import convert_video_to_audio
                audio_out = convert_video_to_audio(dl, output_format='mp3')
                if audio_out and os.path.exists(audio_out):
                    cleanup_file(dl)
                    return audio_out
            return dl

        # Step 1: Try bot client with all strategies
        for cid in candidate_chats:
            dl_path = await _resolve_and_download(client, cid, msg_id, file_id)
            if dl_path:
                return await _maybe_audio(dl_path)

        # Step 1.5: Try specific requesting user's client
        if requesting_user_id:
            from user_manager import get_user_session
            user_session_str = get_user_session(requesting_user_id)
            if user_session_str:
                user_uc = await get_or_start_user_client(requesting_user_id, user_session_str)
                if user_uc:
                    print(f"[TG-DL] Trying specific user client for {requesting_user_id}...")
                    for cid in candidate_chats:
                        uid2 = file_id + "_su"
                        dl_path = await _resolve_and_download(user_uc, cid, msg_id, uid2)
                        if dl_path:
                            return await _maybe_audio(dl_path)

        # Step 2: Try shared persistent user client (started at bot startup with USER_SESSION)
        shared_uc = get_shared_user_client()
        if shared_uc:
            print("[TG-DL] Trying shared persistent user client...")
            for cid in candidate_chats:
                uid2 = file_id + "_su"
                dl_path = await _resolve_and_download(shared_uc, cid, msg_id, uid2)
                if dl_path:
                    return await _maybe_audio(dl_path)

        # Step 3: Fallback — spin up a fresh user client from session string (slower, but safe fallback)
        user_session_str = os.environ.get("USER_SESSION") or os.environ.get("STRING_SESSION")
        if user_session_str:
            try:
                print("[TG-DL] Attempting download via fresh User Session client...")
                from pyrogram import Client as UserClient
                api_id = int(os.environ.get("API_ID", 0))
                api_hash = os.environ.get("API_HASH", "")
                uid3 = file_id + "_fs"
                async with UserClient("user_dl_tmp", session_string=user_session_str, api_id=api_id, api_hash=api_hash) as u_client:
                    for cid in candidate_chats:
                        dl_path = await _resolve_and_download(u_client, cid, msg_id, uid3)
                        if dl_path:
                            return await _maybe_audio(dl_path)
            except Exception as e_user:
                print(f"[TG-DL] Fresh User Session error: {e_user}")

        bot_name = getattr(getattr(client, 'me', None), 'username', 'bot') or 'bot'
        return f"ERROR: Could not download from private channel. Make sure USER_SESSION is set in .env (account must be a member of the channel). Bot: @{bot_name}"
    except Exception as e:
        print(f"[download_telegram_post_media] Error: {e}")
        return f"ERROR: {e}"

# ==================== ASYNC AI PROVIDER ENGINE ====================

# ====================== FREE AI PROVIDER ENGINE ======================
# Priority order (no API key needed, unlimited):
#  1. OpenRouter free tier  (best quality, rate-limited but free)
#  2. Pollinations AI       (openai / llama / qwen models)
#  3. g4f                   (fallback bridge)
#  4. DDGS AI chat          (last resort)
# ======================================================================

async def fetch_openrouter_async(chat_history_list, model="meta-llama/llama-3.3-70b-instruct:free"):
    """
    OpenRouter — only used if OPENROUTER_API_KEY is set in env.
    Without a key it returns 401, so we skip it silently.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None  # Skip entirely — no key configured
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://t.me/UdomAI_Bot",
        "X-Title": "UdomAI Bot",
    }
    payload = {
        "model": model,
        "messages": [
            m for m in chat_history_list
            if m.get("role") in ("user", "assistant", "system")
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    try:
        session = await get_http_session()
        timeout = aiohttp.ClientTimeout(total=30)
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers, json=payload, timeout=timeout
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if text:
                    return text
            elif resp.status == 429:
                print(f"[OpenRouter] Rate limited on {model}")
            else:
                err = await resp.text()
                print(f"[OpenRouter] {resp.status} on {model}: {err[:200]}")
    except Exception as e:
        print(f"[OpenRouter] Exception ({model}): {e}")
    return None

async def fetch_pollinations_async(chat_history_list, model_id="openai"):
    """
    Pollinations.ai — truly unlimited & no API key.
    Supports: openai, llama, qwen-coder, mistral, deepseek, searchgpt
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    # POST endpoint
    try:
        session = await get_http_session()
        timeout = aiohttp.ClientTimeout(total=25)
        payload = {"messages": chat_history_list, "model": model_id, "jsonMode": False}
        async with session.post(
            "https://text.pollinations.ai/", headers=headers, json=payload, timeout=timeout
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                if text and text.strip():
                    return text.strip()
    except Exception as e:
        print(f"[Pollinations-POST] ({model_id}): {e}")

    # GET endpoint fallback
    try:
        user_msg = next((m["content"] for m in reversed(chat_history_list) if m.get("role") == "user"), "")
        if user_msg:
            clean_msg = re.sub(r'\[Current User Local Time:[^\]]+\]', '', user_msg).strip()
            encoded = urllib.parse.quote(clean_msg[:800])
            session = await get_http_session()
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.get(
                f"https://text.pollinations.ai/{encoded}?model={model_id}",
                headers={"User-Agent": headers["User-Agent"]}, timeout=timeout
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if text and text.strip():
                        return text.strip()
    except Exception as e:
        print(f"[Pollinations-GET] ({model_id}): {e}")
    return None

async def fetch_gemini_async(chat_history_list, model_name="gemini-flash-lite-latest"):
    """Google Gemini — uses gemini_manager with multi-key rotation and automatic failover."""
    from gemini_manager import get_gemini_keys, mask_key, GEMINI_MODELS
    keys = get_gemini_keys()
    if not keys:
        return None
    candidate_models = [model_name] + [m for m in GEMINI_MODELS if m != model_name]
    contents_payload = [
        {"role": "user" if m["role"] == "user" else "model",
         "parts": [{"text": m["content"]}]}
        for m in chat_history_list if m.get("role") != "system"
    ]
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents_payload,
        "generationConfig": {"temperature": 0.7, "topP": 0.95}
    }
    session = await get_http_session()
    timeout = aiohttp.ClientTimeout(total=20)
    for key in keys:
        for m in candidate_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
            try:
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        cands = res_json.get("candidates", [])
                        if cands:
                            parts = cands[0].get("content", {}).get("parts", [])
                            answer = "".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()
                            if answer:
                                return answer
                    elif resp.status == 429:
                        print(f"[Gemini] Key {mask_key(key)} rate-limited (429) on {m}. Trying next key...")
                        break
                    elif resp.status in (400, 403):
                        print(f"[Gemini] Key {mask_key(key)} returned {resp.status} on {m}. Trying next key...")
                        break
            except Exception as e:
                print(f"[Gemini] Key {mask_key(key)} connection error ({e}) on {m}.")
                break
    return None

async def fetch_g4f_async(chat_history_list, model_id="gpt-4o-mini"):
    """g4f bridge — unofficial but free, used as fallback."""
    try:
        async def _call():
            c = AsyncClient()
            resp = await c.chat.completions.create(model=model_id, messages=chat_history_list)
            return resp.choices[0].message.content
        result = await asyncio.wait_for(_call(), timeout=20.0)
        return result.strip() if result and result.strip() else None
    except Exception as e:
        print(f"[g4f] ({model_id}): {e}")
    return None

async def fetch_ddgs_async(user_prompt):
    """DuckDuckGo AI chat — truly free, used as last resort."""
    if DDGS is None:
        return None
    try:
        def _ddgs_chat():
            client = DDGS()
            return client.chat(user_prompt, model="gpt-4o-mini")
        result = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(None, _ddgs_chat), timeout=20.0
        )
        return result.strip() if result and result.strip() else None
    except Exception as e:
        print(f"[DDGS] chat error: {e}")
    return None

# Core AI Response Router — cascades through providers until one succeeds
async def get_ai_response(chat_id, user_prompt, image_url=None, context=""):
    selected_model_key = user_selected_model.get(chat_id, DEFAULT_MODEL)
    model_info = AVAILABLE_MODELS.get(selected_model_key, AVAILABLE_MODELS["auto"])

    cache_key = f"{chat_id}:{selected_model_key}:{user_prompt[:100]}"
    cached = get_cached_response(cache_key)
    if cached:
        return cached

    current_time_str = get_user_current_time(chat_id)
    if not chat_history:
        load_chat_history_from_disk()

    # Build system prompt with active model identity
    active_model_name = model_info["name"]
    base_prompt = COPILOT_PROMPT if selected_model_key == "copilot" else (
        MALAKOR_PROMPT if selected_model_key == "malakor" else SYSTEM_PROMPT
    )
    model_identity_prompt = f"{base_prompt}\n\nACTIVE SELECTED MODEL DIRECTIVE:\nYour identity for this conversation is '{active_model_name}'."

    if chat_id not in chat_history:
        chat_history[chat_id] = [{"role": "system", "content": model_identity_prompt}]
    else:
        if chat_history[chat_id] and chat_history[chat_id][0].get("role") == "system":
            chat_history[chat_id][0]["content"] = model_identity_prompt
        else:
            chat_history[chat_id].insert(0, {"role": "system", "content": model_identity_prompt})

    time_prefix = f"[Current User Local Time: {current_time_str}]"
    final_prompt = f"{time_prefix}\n{user_prompt}"
    if context:
        final_prompt = f"{time_prefix}\nContext information:\n{context}\n\nUser Prompt:\n{user_prompt}"
    if image_url:
        final_prompt += f"\n\nImage URL: {image_url}"

    chat_history[chat_id].append({"role": "user", "content": final_prompt})
    if len(chat_history[chat_id]) > 31:
        chat_history[chat_id] = [chat_history[chat_id][0]] + chat_history[chat_id][-30:]
    await save_chat_history_to_disk()

    if model_info["provider"] == "image_only":
        return "FLUX AI Realism is designed for image generation. Please use the /image command to generate images, or select a text model for chatting using /model."

    def _is_valid_ai_reply(text):
        if not text or not isinstance(text, str):
            return False
        cleaned = text.strip()
        if not cleaned:
            return False
        # Filter out Copilot migration / deprecation notice
        if "Copilot moved to a new app" in cleaned or "Install the new Copilot app" in cleaned:
            return False
        return True

    reply = None
    history = chat_history[chat_id]

    # === PHASE 1: Selected model (explicit user choice or fast key) ===
    if model_info["provider"] == "pollinations":
        candidate = await fetch_pollinations_async(history, model_info["model_id"])
        if _is_valid_ai_reply(candidate): reply = candidate
    elif model_info["provider"] == "gemini":
        candidate = await fetch_gemini_async(history, model_info["model_id"])
        if _is_valid_ai_reply(candidate): reply = candidate
    elif model_info["provider"] == "copilot":
        candidate = await fetch_pollinations_async(history, "openai")
        if _is_valid_ai_reply(candidate): reply = candidate
    elif model_info["provider"] == "auto":
        # If Gemini key exists, Gemini 3.5 Flash is instant (<1s) and highest quality
        if os.environ.get("GEMINI_API_KEY"):
            candidate = await fetch_gemini_async(history, "gemini-3.5-flash")
            if _is_valid_ai_reply(candidate): reply = candidate

    # === PHASE 2: Concurrent race across fast providers ===
    if not reply:
        print(f"[AI] Phase 1 miss, racing free providers...")
        race_tasks = [
            asyncio.create_task(fetch_gemini_async(history, "gemini-3.5-flash")),
            asyncio.create_task(fetch_pollinations_async(history, "openai")),
            asyncio.create_task(fetch_pollinations_async(history, "llama")),
            asyncio.create_task(fetch_pollinations_async(history, "mistral")),
        ]
        try:
            for coro in asyncio.as_completed(race_tasks, timeout=25):
                try:
                    res = await coro
                    if _is_valid_ai_reply(res):
                        reply = res
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"[AI] Race error: {e}")
        finally:
            for t in race_tasks:
                if not t.done():
                    t.cancel()

    # === PHASE 3: Sequential fallbacks ===
    fallback_chain = [
        (fetch_gemini_async,       [history, "gemini-3.5-flash-lite"]),
        (fetch_pollinations_async, [history, "qwen-coder"]),
        (fetch_pollinations_async, [history, "deepseek"]),
        (fetch_openrouter_async,   [history, "meta-llama/llama-3.3-70b-instruct:free"]),
        (fetch_g4f_async,          [history, "gpt-4o-mini"]),
    ]
    for fn, args in fallback_chain:
        if reply:
            break
        try:
            res = await fn(*args)
            if _is_valid_ai_reply(res):
                reply = res
                break
        except Exception as fe:
            print(f"[AI] Fallback {fn.__name__} error: {fe}")

    # === PHASE 4: DDGS last resort ===
    if not reply:
        print("[AI] All providers failed, trying DDGS...")
        try:
            res = await fetch_ddgs_async(user_prompt)
            if _is_valid_ai_reply(res):
                reply = res
        except Exception as e:
            print(f"[AI] DDGS error: {e}")

    if reply and reply.strip():
        chat_history[chat_id].append({"role": "assistant", "content": reply})
        await save_chat_history_to_disk()
        set_cached_response(cache_key, reply)
        return reply

    # Pop the unanswered user message to keep history clean
    if chat_history[chat_id] and chat_history[chat_id][-1]["role"] == "user":
        chat_history[chat_id].pop()
    return "⚠️ Sorry, all AI providers are currently busy. Please try again in a moment!"

# Task Cancellation Management System
active_cancellation_events = {}

def register_cancel_task(task_id):
    import threading
    ev = threading.Event()
    active_cancellation_events[str(task_id)] = ev
    return ev

def is_task_cancelled(task_id):
    ev = active_cancellation_events.get(str(task_id))
    return ev.is_set() if ev else False

def cancel_task_id(task_id):
    t_str = str(task_id)
    if t_str in active_cancellation_events:
        active_cancellation_events[t_str].set()
        return True
    return False

def unregister_cancel_task(task_id):
    active_cancellation_events.pop(str(task_id), None)

class ProcessCancelledException(Exception):
    pass

# Realtime Timer Class for UI with Cancel Button and Anti-FloodWait Throttle
class RealtimeTimer:
    def __init__(self, message, initial_text="Thinking", cancel_id=None):
        self.message = message
        self.current_text = initial_text
        self.start_time = time.time()
        self.stop_event = asyncio.Event()
        self.cancel_id = str(cancel_id) if cancel_id else None
        self.task = None
        self.last_sent_text = ""
        self.last_edit_time = 0.0

    def update_text(self, text):
        if self.cancel_id and is_task_cancelled(self.cancel_id):
            raise ProcessCancelledException("Process stopped by user.")
        if text and str(text).strip():
            # Clean text: remove duplicate timer prefixes and trailing dots
            clean = re.sub(r"^⏱️?\s*\[\d+:\d+\]\s*", "", str(text)).rstrip(". ").strip()
            if clean:
                self.current_text = clean

    async def _timer_loop(self):
        dot_count = 1
        cancel_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Stop Process", callback_data=f"cancel_proc|{self.cancel_id}")]
        ]) if self.cancel_id else None

        while not self.stop_event.is_set():
            if self.cancel_id and is_task_cancelled(self.cancel_id):
                break

            now = time.time()
            elapsed = int(now - self.start_time)
            mins, secs = divmod(elapsed, 60)
            dots = "." * dot_count
            dot_count = (dot_count % 3) + 1

            # Format strictly matching clean simple UI:
            # ⏱️ [MM:SS]
            # <Process Emoji + Description with dots>
            clean_text = re.sub(r"^⏱️?\s*\[\d+:\d+\]\s*", "", self.current_text).rstrip(". ").strip()
            if not clean_text:
                clean_text = "Processing..."
            formatted = f"⏱️ [{mins:02d}:{secs:02d}]\n{clean_text}{dots}"

            # Throttle: ensure at least 3.5 seconds between Telegram edit calls to eliminate FloodWait freezes
            if formatted != self.last_sent_text and (now - self.last_edit_time >= 3.5):
                try:
                    is_media = bool(getattr(self.message, 'photo', None) or getattr(self.message, 'video', None) or getattr(self.message, 'animation', None) or getattr(self.message, 'document', None))
                    if is_media:
                        try:
                            await self.message.edit_caption(formatted, reply_markup=cancel_kb)
                        except Exception:
                            await self.message.edit_text(formatted, reply_markup=cancel_kb)
                    else:
                        await self.message.edit_text(formatted, reply_markup=cancel_kb)
                    self.last_sent_text = formatted
                    self.last_edit_time = time.time()
                except FloodWait as fw:
                    wait_s = int(getattr(fw, "value", 5) or 5)
                    await asyncio.sleep(min(wait_s + 1, 15))
                    self.last_edit_time = time.time()
                except (MessageNotModified, Exception):
                    pass

            try:
                await asyncio.sleep(2.0)
            except (asyncio.CancelledError, Exception):
                break

    async def __aenter__(self):
        self.task = asyncio.create_task(self._timer_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.stop_event.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

message_start_times = {}
message_last_edits = {}

async def safe_edit_text(message, text, reply_markup=None):
    if not message:
        return None
    msg_id = getattr(message, 'id', None)
    now = time.time()
    is_done = any(x in str(text) for x in ["Done!", "complete!", "❌", "Received:", "Detected:", "✅", "🛑"])

    # Throttle intermediate progress updates (min 3.0s) to prevent Telegram FloodWait
    if msg_id and not is_done:
        last_t = message_last_edits.get(msg_id, 0.0)
        if now - last_t < 3.0:
            return message

    if msg_id:
        if msg_id not in message_start_times:
            message_start_times[msg_id] = now
        elapsed = int(now - message_start_times[msg_id])
        mins, secs = divmod(elapsed, 60)
        clean_t = re.sub(r"^⏱️?\s*\[\d+:\d+\]\s*", "", str(text)).strip()
        if not is_done and elapsed > 0:
            text = f"⏱ [{mins:02d}:{secs:02d}] {clean_t}"
        else:
            text = clean_t
        if is_done:
            message_start_times.pop(msg_id, None)
            message_last_edits.pop(msg_id, None)
        else:
            message_last_edits[msg_id] = now

    async def _do_edit(m, t, rm):
        # Pyrogram: if message has media (photo/video), edit_caption must be used
        if getattr(m, 'photo', None) or getattr(m, 'video', None) or getattr(m, 'animation', None) or getattr(m, 'document', None):
            try:
                return await m.edit_caption(t, reply_markup=rm)
            except Exception:
                pass
        try:
            if rm:
                return await m.edit_text(t, reply_markup=rm)
            else:
                return await m.edit_text(t)
        except Exception:
            try:
                return await m.edit_caption(t, reply_markup=rm)
            except Exception:
                return m

    try:
        res = await _do_edit(message, text, reply_markup)
        return res or message
    except FloodWait as e:
        val = int(getattr(e, "value", 0) or 0)
        if val <= 10:
            await asyncio.sleep(val + 1)
            try:
                res = await _do_edit(message, text, reply_markup)
                return res or message
            except Exception:
                return message
        else:
            print(f"⚠️ Telegram FloodWait active ({val}s). Skipping edit to protect bot token.")
            return message
    except (MessageNotModified, Exception):
        return message

async def safe_reply_text(message, text, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except FloodWait as e:
        val = int(getattr(e, "value", 0) or 0)
        if val <= 10:
            await asyncio.sleep(val + 1)
            try:
                return await message.reply_text(text, **kwargs)
            except Exception:
                return None
        else:
            print(f"⚠️ Telegram FloodWait active ({val}s). Skipping reply to protect bot token.")
            return None
    except Exception as e:
        print(f"Error in safe_reply_text: {e}")
        return None

# Image Generator & Robust Sender
def clean_and_generate_image_url(raw_prompt):
    p = raw_prompt.strip()
    p_lower = p.lower()
    prefixes = [
        "generator image of", "generator photo of", "generator picture of", "generator image", "generator photo", "generator picture", "generator",
        "generate an image of", "generate a photo of", "generate a picture of", "generate image of", "generate photo of", "generate picture of",
        "generate image", "generate photo", "generate picture", "draw a picture of", "draw an image of", "draw a photo of", "draw me a", "draw me", "draw a",
        "draw image", "draw picture", "draw photo", "draw", "create image", "create photo", "create picture", "create a", "create",
        "make image", "make photo", "make picture", "make a", "make", "paint a picture of", "paint", "picture of", "photo of", "image of", "photograph of",
        "show me a picture of", "show me a photo of", "show me an image of", "show me", "imagine",
        "សូមគូររូបភាព", "សូមគូររូបថត", "សូមគូររូប", "សូមគូរ", "គូររូបភាព", "គូររូបថត", "គូររូប", "គូរ", "សូមបង្កើតរូបភាព", "សូមបង្កើតរូបថត", "សូមបង្កើតរូប", "បង្កើតរូបភាព", "បង្កើតរូបថត", "បង្កើតរូប", "បង្កើត", "ថតរូបភាព", "ថតរូប", "រូបថត", "រូបភាព",
        "画一个", "画", "生成图片", "生成照片", "创建图片", "做图片"
    ]
    for pref in prefixes:
        if p_lower.startswith(pref.lower()):
            p = p[len(pref):].strip()
            break
    p = p.lstrip(": ,-") or "hyperrealistic professional photography masterpiece"
    p_check = p.lower()
    if any(k in p_check for k in ["wallpaper", "landscape", "wide", "banner", "16:9", "horizontal"]):
        width, height = 1280, 720
    elif any(k in p_check for k in ["portrait", "vertical", "phone", "mobile", "9:16", "full body"]):
        width, height = 720, 1280
    else:
        width, height = 1024, 1024

    enhancements = "shot on Hasselblad H6D-100c medium format camera, 85mm f/1.2 prime lens, hyperrealistic professional photography, masterpiece, award-winning studio lighting, 8k resolution"
    encoded_prompt = urllib.parse.quote(f"{p}, {enhancements}")
    seed = random.randint(1, 999999)
    return f"https://image.pollinations.ai/prompt/{encoded_prompt}?model=flux&width={width}&height={height}&nologo=true&enhance=true&seed={seed}"

def extract_image_url(text):
    match = re.search(r'https?://image\.pollinations\.ai/prompt/[^\s\)\>\]]+', text)
    return match.group(0).rstrip('.,;()[]') if match else None

async def send_photo_robust(message, img_url, caption=""):
    try:
        await message.reply_photo(img_url, caption=caption)
        return True
    except Exception:
        try:
            session = await get_http_session()
            async with session.get(img_url) as resp:
                if resp.status == 200:
                    img_bytes = await resp.read()
                    file_obj = io.BytesIO(img_bytes)
                    file_obj.name = "generated_art.jpg"
                    await message.reply_photo(file_obj, caption=caption)
                    return True
        except Exception as e2:
            print(f"Photo fallback failed: {e2}")
            return False

def is_explicit_image_request(prompt_text):
    if not prompt_text:
        return False
    p = prompt_text.lower().strip()

    # Exclude text, prompt, code, and explanation requests
    text_request_prefixes = [
        "give me", "give", "write", "how to", "how do", "tell me", "explain",
        "show code", "code for", "script for", "prompt for", "prompt to", "create a script",
        "create a model", "create model", "create code", "what is", "can you", "help me",
        "សូមសរសេរ", "ប្រាប់", "ពន្យល់", "របៀប", "សរសេរ"
    ]
    if any(p.startswith(pref) for pref in text_request_prefixes):
        return False

    image_prefixes = [
        "draw ", "draw me", "draw a", "draw image", "draw photo", "draw picture",
        "paint ", "generate an image", "generate a photo", "generate a picture",
        "generate image", "generate photo", "generate picture",
        "create an image", "create a photo", "create a picture",
        "create image", "create photo", "create picture",
        "make an image", "make a photo", "make a picture",
        "make image", "make photo", "make picture",
        "picture of ", "photo of ", "image of ",
        "imagine ", "គូររូប", "គូរ ", "ថតរូប", "画一个", "画一张", "生成图片", "生成照片", "创建图片"
    ]
    return any(p.startswith(pref) for pref in image_prefixes)

async def send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=""):
    img_url = extract_image_url(reply)
    if not img_url and is_explicit_image_request(prompt_text):
        img_url = clean_and_generate_image_url(prompt_text)

    if img_url:
        caption_text = f"🎨 `{prompt_text}`" if prompt_text else "🎨 **Generated for you!**"
        success = await send_photo_robust(message, img_url, caption=caption_text)
        if success:
            try: await processing_msg.delete()
            except Exception: pass
            return

    if not reply or not reply.strip(): reply = "Sorry, I couldn't process your request."
    await asyncio.sleep(0.15)
    if len(reply) <= 4000:
        try:
            await processing_msg.edit_text(reply)
            return
        except Exception:
            try: await processing_msg.delete()
            except Exception: pass
            await safe_reply_text(message, reply, reply_to_message_id=message.id)
    else:
        chunks = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
        try: await processing_msg.edit_text(chunks[0])
        except Exception:
            await safe_reply_text(message, chunks[0], reply_to_message_id=message.id)
        for chunk in chunks[1:]:
            await safe_reply_text(message, chunk, reply_to_message_id=message.id)

def analyze_media_with_gemini(file_path, prompt, mime_type="image/jpeg"):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key: return None
    import base64
    for gem_model in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gem_model}:generateContent?key={api_key}"
            with open(file_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
            payload = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": mime_type, "data": b64_data}}]}]
            }
            res = requests.post(url, json=payload, timeout=12)
            res_json = res.json()
            if "candidates" in res_json and res_json["candidates"]:
                return res_json["candidates"][0]["content"]["parts"][0]["text"]
        except Exception: pass
    return None

def is_url(text): return "http://" in text or "https://" in text

def detect_requested_language(text):
    t = text.lower()
    if any(k in t for k in ["khmer", "km", "ខ្មែរ"]): return "km"
    if any(k in t for k in ["chinese", "zh", "中文", "汉语", "普通话"]): return "zh"
    if any(k in t for k in ["english", "en", "អង់គ្លេស"]): return "en"
    if any(k in t for k in ["japanese", "ja", "日本語"]): return "ja"
    if any(k in t for k in ["korean", "ko", "한국어"]): return "ko"
    if any(k in t for k in ["vietnamese", "vi", "tiếng việt"]): return "vi"
    if any(k in t for k in ["thai", "th", "ไทย"]): return "th"
    if any(k in t for k in ["french", "fr", "français"]): return "fr"
    if any(k in t for k in ["spanish", "es", "español"]): return "es"
    if re.search(r'[\u1780-\u17FF]', text): return "km"
    if re.search(r'[\u4E00-\u9FFF]', text): return "zh"
    return "en"

# ==================== TELEGRAM MESSAGE HANDLERS ====================

def _is_main_bot_func(flt, client, update):
    return client.name == "my_bot"
is_main_bot = filters.create(_is_main_bot_func)

def _is_allowed_bot_func(flt, client, update):
    if client.name == "my_bot": return True
    from custom_bots_manager import get_active_custom_bots
    return client.bot_token in get_active_custom_bots()
is_allowed_bot = filters.create(_is_allowed_bot_func)

def _is_otp_bot_restricted_func(flt, client, update):
    system_config = get_system_config()
    if not system_config.get("otp_bot_restricted_mode", False):
        return False
        
    otp_bot = system_config.get("otp_bot_token")
    if not otp_bot or otp_bot == "main_bot":
        return client.name == "my_bot"
    else:
        return client.name == f"custom_bot_{otp_bot[:10]}"

is_otp_bot_restricted = filters.create(_is_otp_bot_restricted_func)

@Client.on_message(filters.all & is_otp_bot_restricted, group=-100)
async def restricted_otp_bot_handler(client: Client, message: Message):
    if not getattr(message, 'from_user', None) or getattr(message.from_user, 'is_self', False) or getattr(message.from_user, 'is_bot', False):
        raise StopPropagation()
        
    user_data = register_or_update_user(message.from_user)
    has_permission = (user_data.get("status") == "APPROVED")
    
    if has_permission:
        system_config = get_system_config()
        webapp_url = (
            os.environ.get("NGROK_DASHBOARD_URL")
            or system_config.get("webapp_url", "").strip()
            or "http://localhost:5000"
        )
        if not webapp_url.startswith("http://") and not webapp_url.startswith("https://"):
            webapp_url = "http://" + webapp_url
            
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 Go to Web", url=webapp_url)],
            [InlineKeyboardButton("🆔 Get User ID", callback_data="get_user_id")]
        ])
        assigned_server = user_data.get("assigned_server", "main")
        reply_msg = (
            f"✅ You have License to use this bot.\n"
            f"🖥 **Assigned Server:** `{assigned_server}`\n\n"
            f"Click below to access the web dashboard or get your User ID:"
        )
        await message.reply_text(reply_msg, reply_markup=keyboard)
    else:
        user_full_name = f"{message.from_user.first_name or ''} {message.from_user.last_name or ''}".strip() or "User"
        user_id = message.from_user.id
        auto_msg = (
            f"Hello Admin! 👋\n"
            f"I would like to request access to use Udom AI Bot.\n\n"
            f"👤 Name: {user_full_name}\n"
            f"🆔 User ID: {user_id}\n\n"
            f"Please approve my account. Thank you!"
        )
        encoded_msg = urllib.parse.quote(auto_msg)
        system_config = get_system_config()
        admin_contact = system_config.get("admin_contact", "thengrithy")
        contact_admin_url = f"https://t.me/{admin_contact}?text={encoded_msg}"
        
        blocked_text = (
            "⛔️ **Access Pending / មិនទាន់ទទួលបានសិទ្ធិប្រើប្រាស់**\n\n"
            f"Hello {user_full_name}! Your account is currently not approved to use Udom AI Bot.\n"
            f"Your Telegram ID is: `{user_id}`\n\n"
            "Please contact the Admin to request access:\n"
            f"👉 {contact_admin_url}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Admin / ទាក់ទង Admin", url=contact_admin_url)]
        ])
        await message.reply_text(blocked_text, reply_markup=keyboard)
    
    raise StopPropagation()

@Client.on_message(filters.command("login") & filters.private & ~filters.me & is_main_bot, group=0)
async def login_command(client: Client, message: Message):
    if not await check_user_access(message): return
    user_id = message.from_user.id
    from pyrogram import Client as UserClient
    api_id = int(os.environ.get("API_ID", "0"))
    api_hash = os.environ.get("API_HASH", "")
    
    uc = UserClient(f"temp_login_{user_id}", api_id=api_id, api_hash=api_hash, in_memory=True)
    try:
        await uc.connect()
    except Exception as e:
        await message.reply_text(f"❌ Failed to connect to Telegram: {e}")
        return
        
    login_states[user_id] = {"step": "phone", "client": uc}
    await message.reply_text("🔑 **Link Your Account**\n\nPlease reply to this message with your Telegram phone number (including country code, e.g. `+1234567890`).\n\nThis is completely secure and will only be used to download media from private channels you are in.")

@Client.on_message((filters.command(["start"]) | filters.regex("^(ℹ️ Help|📖 How to Use)$")) & is_allowed_bot, group=0)
async def start_command(client, message):
    if not await check_user_access(message): return
    user = message.from_user
    first_name = getattr(user, 'first_name', '') or 'there'

    bot_name = "Udom AI Bot"
    if getattr(client, "me", None): bot_name = client.me.first_name
    elif getattr(client, "name", None): bot_name = client.name
    
    is_main = client.name == "my_bot"
    
    if is_main:
        welcome_message = (
            f"👋 **Hello {first_name}! Welcome to {bot_name}**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 **What I can do for you:**\n\n"
            "🧠 **AI Chat & Search**\n"
            "  • `/ask` — Chat with AI (GPT-4o, LLaMA, Gemini)\n"
            "  • `/search` — Web search with AI answers\n"
            "  • `/image` — Generate AI images\n"
            "  • `/model` — Switch AI model\n"
            "  • `/voice` — Select Whisper + TTS engine (edge-tts / VoxCPM)\n"
            "  • `/voxmodel` — Switch VoxCPM model (VoxCPM2 / VoxCPM1.5 / VoxCPM0.5)\n\n"
            "📥 **Download Media**\n"
            "  • YouTube, Facebook, TikTok, Instagram\n"
            "  • Dailymotion, Twitter/X, Twitch\n"
            "  • Telegram private channels (with USER_SESSION)\n"
            "  • Choose quality: 4K / 1080p / 720p / 480p / MP3\n\n"
            "✂️ **Video Tools**\n"
            "  • Clip video into equal parts\n"
            "  • Split by duration (e.g. every 10 min)\n"
            "  • Add thumbnail / cover art\n"
            "  • Convert format (MP4 → MKV, etc.)\n"
            "  • Extract audio (MP3 / M4A / WAV)\n\n"
            "🌐 **Translation & Dubbing**\n"
            "  • Translate video audio to any language\n"
            "  • AI voice dubbing (TTS)\n"
            "  • Transcribe audio → text\n\n"
            "📄 **Documents & Files**\n"
            "  • Summarize PDFs, Word, Excel files\n"
            "  • Extract text from documents\n"
            "  • Ask AI questions about files\n\n"
            "📢 **Publish & Admin Tools**\n"
            "  • `/publish` — Send media to groups/channels\n"
            "  • `/mychats` — List channels where bot is admin\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 **Just send a link, file, or type a message to start!**"
        )
    else:
        welcome_message = (
            f"👋 **Hello {first_name}! Welcome to {bot_name}**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 **What I can do for you:**\n\n"
            "📥 **Download Media**\n"
            "  • YouTube, Facebook, TikTok, Instagram\n"
            "  • Dailymotion, Twitter/X, Twitch\n"
            "  • Telegram private channels (with USER_SESSION)\n"
            "  • Choose quality: 4K / 1080p / 720p / 480p / MP3\n\n"
            "✂️ **Video Tools**\n"
            "  • Clip video into equal parts\n"
            "  • Split by duration (e.g. every 10 min)\n"
            "  • Add thumbnail / cover art\n"
            "  • Convert format (MP4 → MKV, etc.)\n"
            "  • Extract audio (MP3 / M4A / WAV)\n\n"
            "🌐 **Translation & Dubbing**\n"
            "  • Translate video audio to any language\n"
            "  • AI voice dubbing (TTS)\n"
            "  • Transcribe audio → text\n\n"
            "📢 **Publish & Admin Tools**\n"
            "  • `/publish` — Send media to groups/channels\n"
            "  • `/mychats` — List channels where bot is admin\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 **Just send a link or file to start!**"
        )
    if is_main:
        keyboard_buttons = [
            [
                InlineKeyboardButton("📖 How to Use", callback_data="show_how_to_use"),
                InlineKeyboardButton("🛠 Commands", callback_data="show_help"),
            ],
            [
                InlineKeyboardButton("🤖 AI Models", callback_data="cb_model_list"),
                InlineKeyboardButton("ℹ️ About Bot", callback_data="show_about"),
            ]
        ]
    else:
        keyboard_buttons = [
            [
                InlineKeyboardButton("📖 How to Use", callback_data="show_how_to_use"),
                InlineKeyboardButton("ℹ️ About Bot", callback_data="show_about"),
            ]
        ]
    
    from user_manager import get_system_config
    system_config = get_system_config()
    
    keyboard_buttons.append([InlineKeyboardButton("🆔 Get User ID", callback_data="get_user_id")])
        
    dashboard_url = get_valid_dashboard_url()
    if dashboard_url:
        keyboard_buttons.append([InlineKeyboardButton("🌐 Open Web", url=dashboard_url)])

    keyboard = InlineKeyboardMarkup(keyboard_buttons)
    await safe_reply_text(message, welcome_message, reply_markup=keyboard)


@Client.on_message(filters.command(["dashboard"]) & is_allowed_bot, group=0)
async def dashboard_command(client, message):
    if not await check_user_access(message): return
    dashboard_url = get_valid_dashboard_url()
    admin_text = (
        "⚙️ **Open Udom Workflow**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"You can access the workflow dashboard here:\n"
        f"`{dashboard_url or 'Dashboard URL not configured'}`\n\n"
        "💡 _Use your Telegram User ID (from the bot menu) to login or request an OTP._"
    )
    if dashboard_url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Open Udom Workflow", url=dashboard_url)]])
        await message.reply_text(admin_text, reply_markup=keyboard)
    else:
        await message.reply_text(admin_text)

@Client.on_message(filters.command(["help", "howtouse", "guide"]) & is_allowed_bot, group=0)
async def help_command(client, message):
    if not await check_user_access(message): return
    text = "📖 **How to Use / របៀបប្រើប្រាស់ / 使用指南**\n\nPlease choose your language:\nសូមជ្រើសរើសភាសា:\n请选择您的语言:"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇰🇭 ភាសាខ្មែរ (Khmer)", callback_data="how_to_use_km")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="how_to_use_en")],
        [InlineKeyboardButton("🇨🇳 中文 (Chinese)", callback_data="how_to_use_zh")],
        [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
    ])
    await safe_reply_text(message, text, reply_markup=keyboard)

@Client.on_message(filters.command("ask") & is_allowed_bot, group=1)
async def ask_command(client: Client, message: Message):
    if not await check_user_access(message, require_assigned_server=True): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please type your question below:", reply_markup=ForceReply(selective=True))
        return
    prompt = message.text.split(None, 1)[1]
    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🤔 Thinking...")
    cancel_id = f"ask_{processing_msg.id}"
    register_cancel_task(cancel_id)
    try:
        async with RealtimeTimer(processing_msg, "🤔 Thinking", cancel_id=cancel_id):
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
            try:
                reply = await asyncio.wait_for(get_ai_response(message.chat.id, prompt), timeout=180.0)
            except asyncio.TimeoutError:
                reply = "⚠️ The AI took too long to respond. Please try again!"
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
        await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=prompt)
    except ProcessCancelledException:
        await safe_edit_text(processing_msg, "🛑 **Process stopped by user!** ✅")
    except Exception as e:
        print(f"Error in ask_command: {e}")
        await safe_edit_text(processing_msg, "⚠️ Sorry, an error occurred. Please try asking again!")
    finally:
        unregister_cancel_task(cancel_id)

@Client.on_message(filters.command("image") & is_allowed_bot, group=1)
async def image_command(client: Client, message: Message):
    if not await check_user_access(message, require_assigned_server=True): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please describe the image you want me to draw below:", reply_markup=ForceReply(selective=True))
        return
    raw_prompt = message.text.split(None, 1)[1]
    image_url = clean_and_generate_image_url(raw_prompt)
    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🎨 Drawing photo with Udom AI...")
    cancel_id = f"img_{processing_msg.id}"
    register_cancel_task(cancel_id)
    try:
        async with RealtimeTimer(processing_msg, "🎨 Drawing photo with Udom AI", cancel_id=cancel_id):
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
            success = await send_photo_robust(message, image_url, caption=f"🎨 `{raw_prompt}`")
        if success: await processing_msg.delete()
        else: await processing_msg.edit_text("Sorry, failed to generate image.")
    except ProcessCancelledException:
        await safe_edit_text(processing_msg, "🛑 **Process stopped by user!** ✅")
    except Exception as e:
        print(f"Error in image_command: {e}")
        await processing_msg.edit_text("Sorry, failed to generate image.")
    finally:
        unregister_cancel_task(cancel_id)


_poster_sessions = {}  # sid -> {original_title, variants_km, variants_en, reply_msg, photo_path}

@Client.on_message(filters.command(["title", "movietitle", "translate_title"]) & is_allowed_bot, group=1)
async def movie_title_command(client: Client, message: Message):
    if not await check_user_access(message): return
    args = message.text.split(None, 1)
    title_input = args[1].strip() if len(args) > 1 else ""

    reply_photo = None
    if message.reply_to_message:
        if message.reply_to_message.photo:
            reply_photo = message.reply_to_message
        if message.reply_to_message.caption and not title_input:
            title_input = message.reply_to_message.caption.strip()

    if not title_input:
        if reply_photo and reply_photo.caption:
            title_input = reply_photo.caption.strip()
        else:
            await safe_reply_text(
                message,
                "🎬 **Movie Title Translator & Poster Stylizer**\n\n"
                "Usage:\n"
                "`/title <movie title>`\n"
                "Or reply to any movie poster image with `/title <title>`!\n\n"
                "Example:\n"
                "`/title 灵兽心语`\n"
                "`/title Celestial Above All`"
            )
            return

    from poster_styler import translate_movie_title
    res_km = translate_movie_title(title_input, target_lang="km")
    res_en = translate_movie_title(title_input, target_lang="en")

    sid = str(uuid.uuid4())[:8]
    _poster_sessions[sid] = {
        "original_title": title_input,
        "variants_km": res_km.get("variants", [title_input]),
        "variants_en": res_en.get("variants", [title_input]),
        "reply_msg": reply_photo
    }

    km_lines = "\n".join(f"{idx+1}. `{v}`" for idx, v in enumerate(res_km.get("variants", [])[:3]))
    en_lines = "\n".join(f"{idx+1}. `{v}`" for idx, v in enumerate(res_en.get("variants", [])[:3]))

    text = (
        f"🎬 **Movie Title Localization**\n"
        f"🏷️ **Original Title:** `{title_input}`\n\n"
        f"🇰🇭 **Khmer Cinema Style (ភាសាខ្មែរ):**\n"
        f"{km_lines}\n\n"
        f"🇬🇧 **English Epic Style:**\n"
        f"{en_lines}\n\n"
        f"✨ Select a style to apply onto the poster artwork:"
    )

    buttons = []
    if reply_photo:
        buttons.append([
            InlineKeyboardButton("✨ Apply Khmer (Gold)", callback_data=f"pst_app|{sid}|km|gold_fantasy|0"),
            InlineKeyboardButton("🔥 Apply Khmer (Fire)", callback_data=f"pst_app|{sid}|km|fire_action|0"),
        ])
        buttons.append([
            InlineKeyboardButton("✨ Apply English (Gold)", callback_data=f"pst_app|{sid}|en|gold_fantasy|0"),
            InlineKeyboardButton("⚡ Apply English (Sci-Fi)", callback_data=f"pst_app|{sid}|en|silver_scifi|0"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🇰🇭 Use Khmer: " + res_km.get("recommended", "")[:18], callback_data=f"pst_show|{sid}|km|0"),
            InlineKeyboardButton("🇬🇧 Use English: " + res_en.get("recommended", "")[:18], callback_data=f"pst_show|{sid}|en|0"),
        ])

    await safe_reply_text(message, text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^pst_"), group=1)
async def poster_title_callback(client: Client, query: CallbackQuery):
    await query.answer()
    data = query.data.split("|")
    act = data[1] if len(data) > 1 else ""
    sid = data[2] if len(data) > 2 else ""

    if act == "media":
        # Immediate 1-click Thumbnail Redesign with Translated Movie Title in Original Art Style
        short_id = sid
        cached_msg = url_cache.get(short_id) or query.message.reply_to_message
        if not cached_msg and query.message.photo:
            cached_msg = query.message
        if not cached_msg:
            await query.answer("Photo expired. Please upload it again.", show_alert=True)
            return

        status = await query.message.reply_text("🎨 **Redesigning thumbnail with stylized translated movie title...**\n⏳ Please wait a moment…")
        temp_dir = get_temp_dir()
        new_sid = str(uuid.uuid4())[:8]
        photo_path = os.path.join(temp_dir, f"poster_orig_{new_sid}.jpg")

        try:
            dl_path = None
            try:
                dl_path = await client.download_media(cached_msg, file_name=photo_path)
            except Exception:
                pass
            if not dl_path or not os.path.exists(dl_path):
                dl_path = await cached_msg.download(file_name=photo_path)
            if not dl_path or not os.path.exists(dl_path):
                await status.edit_text("❌ Could not download original thumbnail.")
                return

            from poster_styler import redesign_poster_with_translated_title
            caption = getattr(cached_msg, "caption", "") or ""
            redesign_data = await asyncio.to_thread(
                redesign_poster_with_translated_title,
                photo_path,
                caption,
                "km",
                "gold_fantasy"
            )
            orig_title = redesign_data.get("original_title", "灵兽心语")
            chosen_title = redesign_data.get("chosen_title", orig_title)
            out_styled = redesign_data.get("output_path")
            style_used = redesign_data.get("detected_style", "gold_fantasy")
            variants_km = redesign_data.get("variants_km", [chosen_title])
            variants_en = redesign_data.get("variants_en", [orig_title])

            _poster_sessions[new_sid] = {
                "original_title": orig_title,
                "variants_km": variants_km,
                "variants_en": variants_en,
                "current_lang": "km",
                "current_style": style_used,
                "current_title": chosen_title,
                "photo_path": photo_path,
                "reply_msg": cached_msg
            }

            buttons = [
                [
                    InlineKeyboardButton("✨ 3D Gold", callback_data=f"pst_app|{new_sid}|km|gold_fantasy|0"),
                    InlineKeyboardButton("🔥 Fiery Red", callback_data=f"pst_app|{new_sid}|km|fire_action|0"),
                    InlineKeyboardButton("⚡ Silver Cyber", callback_data=f"pst_app|{new_sid}|km|silver_scifi|0"),
                ],
                [
                    InlineKeyboardButton("🇰🇭 Khmer Title", callback_data=f"pst_app|{new_sid}|km|gold_fantasy|0"),
                    InlineKeyboardButton("🇬🇧 English Title", callback_data=f"pst_app|{new_sid}|en|gold_fantasy|0"),
                ],
                [
                    InlineKeyboardButton("✏️ Custom Edit Title", callback_data=f"pst_edit|{new_sid}"),
                    InlineKeyboardButton("📤 Publish to Channel", callback_data=f"pub_file_start|{short_id}")
                ]
            ]

            caption_out = (
                f"🎬 **Redesigned Movie Thumbnail**\n"
                f"🏷️ **Original Title:** `{orig_title}`\n"
                f"🇰🇭 **Translated Title:** **{chosen_title}**\n"
                f"✨ **Visual Style:** 3D Golden Fantasy (Matching Original Artwork)"
            )

            await query.message.reply_photo(photo=out_styled, caption=caption_out, reply_markup=InlineKeyboardMarkup(buttons))
            await status.delete()
        except Exception as exc:
            await status.edit_text(f"❌ Failed to redesign thumbnail: {exc}")
        return

    sess = _poster_sessions.get(sid)
    if not sess:
        await query.answer("Session expired. Please use /title again.", show_alert=True)
        return

    orig_title = sess.get("original_title", "")
    reply_msg = sess.get("reply_msg")

    if act == "edit":
        # Custom title edit prompt
        prompt_text = (
            f"✍️ **Edit Movie Title**\n\n"
            f"Current Original: `{orig_title}`\n\n"
            f"Please reply with the exact text you want on the thumbnail (Khmer, English, or Chinese):"
        )
        msg_prompt = await query.message.reply_text(prompt_text, reply_markup=ForceReply(selective=True))
        sess["awaiting_custom_for"] = query.message.chat.id
        sess["prompt_msg_id"] = msg_prompt.id
        return

    if act == "app":
        lang = data[3] if len(data) > 3 else "km"
        style = data[4] if len(data) > 4 else "gold_fantasy"
        opt_idx = int(data[5]) if len(data) > 5 and data[5].isdigit() else 0

        vars_list = sess.get(f"variants_{lang}", [])
        chosen_title = vars_list[opt_idx] if opt_idx < len(vars_list) else orig_title

        status = await query.message.reply_text("🎨 **Rendering stylized title onto poster artwork...**")
        temp_dir = get_temp_dir()
        photo_path = sess.get("photo_path")
        should_clean = False
        try:
            if not photo_path or not os.path.exists(photo_path):
                target_photo_msg = reply_msg or query.message.reply_to_message or query.message
                if target_photo_msg and target_photo_msg.photo:
                    photo_path = await target_photo_msg.download(file_name=os.path.join(temp_dir, f"poster_{sid}.jpg"))
                    should_clean = True
                elif query.message.photo:
                    photo_path = await query.message.download(file_name=os.path.join(temp_dir, f"poster_{sid}.jpg"))
                    should_clean = True

            if not photo_path or not os.path.exists(photo_path):
                await status.edit_text("❌ Could not download poster photo.")
                return

            from poster_styler import render_stylized_movie_title
            out_path = os.path.join(temp_dir, f"styled_poster_{sid}_{style}.jpg")
            await asyncio.to_thread(render_stylized_movie_title, photo_path, chosen_title, out_path, style)

            style_names = {
                "gold_fantasy": "3D Gold Fantasy",
                "fire_action": "Fiery Action",
                "silver_scifi": "Silver Sci-Fi",
                "crimson_drama": "Crimson Drama"
            }
            s_name = style_names.get(style, "Cinema Style")

            caption = (
                f"🎬 **Redesigned Movie Thumbnail**\n"
                f"🏷️ **Original Title:** `{orig_title}`\n"
                f"🌟 **Translated Title:** **{chosen_title}**\n"
                f"✨ **Visual Style:** **{s_name}**"
            )

            alt_buttons = [
                [
                    InlineKeyboardButton("✨ 3D Gold", callback_data=f"pst_app|{sid}|{lang}|gold_fantasy|{opt_idx}"),
                    InlineKeyboardButton("🔥 Fiery Red", callback_data=f"pst_app|{sid}|{lang}|fire_action|{opt_idx}"),
                    InlineKeyboardButton("⚡ Silver Cyber", callback_data=f"pst_app|{sid}|{lang}|silver_scifi|{opt_idx}"),
                ],
                [
                    InlineKeyboardButton("🇰🇭 Khmer Title", callback_data=f"pst_app|{sid}|km|{style}|0"),
                    InlineKeyboardButton("🇬🇧 English Title", callback_data=f"pst_app|{sid}|en|{style}|0"),
                ],
                [
                    InlineKeyboardButton("✏️ Custom Edit Title", callback_data=f"pst_edit|{sid}"),
                ]
            ]

            await query.message.reply_photo(photo=out_path, caption=caption, reply_markup=InlineKeyboardMarkup(alt_buttons))
            await status.delete()
        except Exception as exc:
            await status.edit_text(f"❌ Failed to render stylized poster: {exc}")
        finally:
            if should_clean and photo_path: cleanup_file(photo_path)

@Client.on_message(filters.command(["reset", "clear"]) & is_allowed_bot, group=1)
async def clear_history_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    chat_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    await save_chat_history_to_disk()
    await safe_reply_text(message, "🧹 **Chat history & memory reset!**\nStarted a fresh new conversation.")

@Client.on_message(filters.command("model") & is_allowed_bot, group=1)
async def model_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    current = user_selected_model.get(chat_id, DEFAULT_MODEL)
    keyboard, row = [], []
    for key, info in AVAILABLE_MODELS.items():
        btn_text = f"✅ {info['name']}" if key == current else info['name']
        row.append(InlineKeyboardButton(btn_text, callback_data=f"set_model_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    await safe_reply_text(message, "🧠 **Select AI Model**\n\nChoose your preferred AI model. All models are 100% free and unlimited!", reply_markup=InlineKeyboardMarkup(keyboard))

@Client.on_callback_query(filters.regex(r"^set_model_"), group=1)
async def handle_model_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    model_key = callback_query.data.replace("set_model_", "")
    if model_key in AVAILABLE_MODELS:
        user_selected_model[chat_id] = model_key
        model_name = AVAILABLE_MODELS[model_key]["name"]

        # Evict all cached responses for this chat to prevent stale model answers
        keys_to_del = [k for k in RESPONSE_CACHE if k.startswith(f"{chat_id}:")]
        for k in keys_to_del:
            RESPONSE_CACHE.pop(k, None)

        # Update chat history system prompt with new active model identity
        base_prompt = MALAKOR_PROMPT if model_key == "malakor" else SYSTEM_PROMPT
        model_identity_prompt = f"{base_prompt}\n\nACTIVE SELECTED MODEL DIRECTIVE:\nYour identity for this conversation is '{model_name}'."
        chat_history[chat_id] = [{"role": "system", "content": model_identity_prompt}]
        await save_chat_history_to_disk()

        keyboard, row = [], []
        for key, info in AVAILABLE_MODELS.items():
            btn_text = f"✅ {info['name']}" if key == model_key else info['name']
            row.append(InlineKeyboardButton(btn_text, callback_data=f"set_model_{key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row: keyboard.append(row)
        await callback_query.message.edit_text(f"🧠 **Select AI Model**\n\n✅ Successfully switched to **{model_name}**!\nStarted a fresh conversation session with this model.", reply_markup=InlineKeyboardMarkup(keyboard))
        await callback_query.answer(f"Switched to {model_name}")
    else:
        await callback_query.answer("Invalid model selection.", show_alert=True)

def _build_voice_menu(chat_id):
    settings = get_user_voice_settings(chat_id)
    keyboard = []

    # 1. Transcribe Engine (Gemini API vs Local Faster-Whisper)
    t_eng = settings.get("transcribe_engine", "gemini")
    btn_gem_tr = ("✅ 🤖 Gemini Transcribe" if t_eng == "gemini" else "🤖 Gemini Transcribe")
    btn_loc_tr = ("✅ 💻 Local Whisper" if t_eng == "local" else "💻 Local Whisper")
    keyboard.append([
        InlineKeyboardButton(btn_gem_tr, callback_data="set_transcribe_engine_gemini"),
        InlineKeyboardButton(btn_loc_tr, callback_data="set_transcribe_engine_local")
    ])

    # 2. Translate Engine (Gemini API vs Local/Free AI)
    tl_eng = settings.get("translate_engine", "gemini")
    btn_gem_tl = ("✅ 🤖 Gemini Translate" if tl_eng == "gemini" else "🤖 Gemini Translate")
    btn_loc_tl = ("✅ 💻 Local/Free AI" if tl_eng == "local" else "💻 Local/Free AI")
    keyboard.append([
        InlineKeyboardButton(btn_gem_tl, callback_data="set_translate_engine_gemini"),
        InlineKeyboardButton(btn_loc_tl, callback_data="set_translate_engine_local")
    ])

    # 3. TTS Backend Engine
    backend_row = []
    for key, label in VOICE_ENGINE_OPTIONS.items():
        btn_text = f"✅ {label}" if key == settings["tts_backend"] else label
        backend_row.append(InlineKeyboardButton(btn_text, callback_data=f"set_voice_backend_{key}"))
        if len(backend_row) == 2:
            keyboard.append(backend_row)
            backend_row = []
    if backend_row:
        keyboard.append(backend_row)

    # 4. VoxCPM Foundation Model Selection (VoxCPM2, VoxCPM1.5, VoxCPM0.5)
    current_vox = normalize_voxcpm_model(settings.get("voxcpm_model", DEFAULT_VOXCPM_MODEL))
    vox_row = []
    for m_key, m_desc in VOXCPM_MODEL_CHOICES.items():
        is_sel = (m_key == current_vox)
        short_name = m_desc.split(" ")[0]
        btn_text = f"✅ {short_name}" if is_sel else short_name
        cb_slug = m_key.split("/")[-1].lower().replace(".", "").replace("-", "")
        vox_row.append(InlineKeyboardButton(btn_text, callback_data=f"set_vox_model_{cb_slug}"))
    if vox_row:
        keyboard.append(vox_row)

    # 5. Local Whisper Model
    whisper_row = []
    for model_name in [m for m in WHISPER_MODEL_CHOICES if m != "gemini"]:
        model_label = f"{model_name} (large)" if model_name == "large-v3" else model_name
        btn_text = f"✅ {model_label}" if model_name == settings["whisper_model"] else model_label
        whisper_row.append(InlineKeyboardButton(btn_text, callback_data=f"set_whisper_model_{model_name}"))
        if len(whisper_row) == 2:
            keyboard.append(whisper_row)
            whisper_row = []
    if whisper_row:
        keyboard.append(whisper_row)

    vox_desc = VOXCPM_MODEL_CHOICES.get(current_vox, current_vox)
    text = (
        "🎙️ **AI Voice, Transcribe & Translate Settings**\n\n"
        f"• **Transcribe Engine**: `{'🤖 Gemini AI (Fast & Accurate)' if t_eng == 'gemini' else '💻 Local Faster-Whisper'}`\n"
        f"• **Translate Engine**: `{'🤖 Gemini API (Cinematic Cinema Dub)' if tl_eng == 'gemini' else '💻 Local/Free AI (MyMemory & GTX)'}`\n"
        f"• **TTS Engine**: `{VOICE_ENGINE_OPTIONS.get(settings['tts_backend'], settings['tts_backend'])}`\n"
        f"• **VoxCPM Model**: `{vox_desc}`\n"
        f"• **Local Whisper Model**: `{settings['whisper_model']}`\n\n"
        "Tap any option below to toggle engines or switch VoxCPM models (`/voxmodel`):"
    )
    return text, InlineKeyboardMarkup(keyboard)


@Client.on_message(filters.command("voice") & is_allowed_bot, group=1)
async def voice_command(client: Client, message: Message):
    # Open to all users without access restriction
    if message.from_user:
        try:
            register_or_update_user(message.from_user)
        except Exception:
            pass
    chat_id = message.chat.id
    text, markup = _build_voice_menu(chat_id)
    await safe_reply_text(message, text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^set_transcribe_engine_"), group=-1)
async def handle_transcribe_engine_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    engine = callback_query.data.replace("set_transcribe_engine_", "")
    if engine in ("gemini", "local"):
        settings = get_user_voice_settings(chat_id)
        settings["transcribe_engine"] = engine
        label = "🤖 Gemini API" if engine == "gemini" else "💻 Local Faster-Whisper"
        try:
            await callback_query.answer(f"Transcribe engine set to {label}")
        except Exception:
            pass
        text, markup = _build_voice_menu(chat_id)
        try:
            await callback_query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    else:
        await callback_query.answer("Invalid transcribe selection.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^set_translate_engine_"), group=-1)
async def handle_translate_engine_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    engine = callback_query.data.replace("set_translate_engine_", "")
    if engine in ("gemini", "local"):
        settings = get_user_voice_settings(chat_id)
        settings["translate_engine"] = engine
        label = "🤖 Gemini API" if engine == "gemini" else "💻 Local/Free AI"
        try:
            await callback_query.answer(f"Translate engine set to {label}")
        except Exception:
            pass
        text, markup = _build_voice_menu(chat_id)
        try:
            await callback_query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    else:
        await callback_query.answer("Invalid translate selection.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^set_voice_backend_"), group=-1)
async def handle_voice_backend_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    backend_key = callback_query.data.replace("set_voice_backend_", "")
    settings = get_user_voice_settings(chat_id)
    if backend_key in VOICE_ENGINE_OPTIONS:
        settings["tts_backend"] = backend_key
        os.environ["TTS_BACKEND"] = backend_key
        try:
            await callback_query.answer(f"TTS engine set to {VOICE_ENGINE_OPTIONS[backend_key]}")
        except Exception:
            pass
        text, markup = _build_voice_menu(chat_id)
        try:
            await callback_query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    else:
        await callback_query.answer("Invalid voice backend.", show_alert=True)


@Client.on_callback_query(filters.regex(r"^set_vox_model_"), group=-1)
async def handle_vox_model_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    slug = callback_query.data.replace("set_vox_model_", "").lower()
    slug_map = {
        "voxcpm2": "openbmb/VoxCPM2",
        "voxcpm15": "openbmb/VoxCPM1.5",
        "voxcpm05b": "openbmb/VoxCPM-0.5B",
        "voxcpm05": "openbmb/VoxCPM-0.5B",
    }
    target_model = slug_map.get(slug, "openbmb/VoxCPM1.5")
    settings = get_user_voice_settings(chat_id)
    settings["voxcpm_model"] = target_model
    settings["tts_backend"] = "voxcpm2"
    os.environ["VOXCPM_MODEL"] = target_model
    os.environ["TTS_BACKEND"] = "voxcpm2"
    try:
        from user_manager import update_system_config
        update_system_config("default_voxcpm_model", target_model)
    except Exception:
        pass
    label = VOXCPM_MODEL_CHOICES.get(target_model, target_model)
    try:
        await callback_query.answer(f"VoxCPM model set to {label}")
    except Exception:
        pass
    text, markup = _build_voice_menu(chat_id)
    try:
        await callback_query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


@Client.on_message(filters.command(["voxmodel", "voxcpm"]) & is_allowed_bot, group=1)
async def voxmodel_command(client: Client, message: Message):
    # Open to all users without access restriction
    if message.from_user:
        try:
            register_or_update_user(message.from_user)
        except Exception:
            pass
    chat_id = message.chat.id
    settings = get_user_voice_settings(chat_id)

    if len(message.command) > 1:
        arg = message.command[1].strip()
        from converter import normalize_voxcpm_model
        resolved_model = normalize_voxcpm_model(arg)
        settings["voxcpm_model"] = resolved_model
        settings["tts_backend"] = "voxcpm2"
        os.environ["VOXCPM_MODEL"] = resolved_model
        os.environ["TTS_BACKEND"] = "voxcpm2"
        try:
            from user_manager import update_system_config
            update_system_config("default_voxcpm_model", resolved_model)
        except Exception:
            pass
        label = VOXCPM_MODEL_CHOICES.get(resolved_model, resolved_model)
        await safe_reply_text(
            message,
            f"✅ **VoxCPM Model Switched!**\n\n"
            f"• **Active Model**: `{resolved_model}`\n"
            f"• **Description**: {label}\n"
            f"• **Hugging Face Hub**: https://huggingface.co/{resolved_model}\n"
            f"• **TTS Backend**: `VoxCPM (OpenBMB Hugging Face)`\n\n"
            f"All future speech synthesis and AI dubbing will use this model."
        )
        return

    current_model = normalize_voxcpm_model(settings.get("voxcpm_model", DEFAULT_VOXCPM_MODEL))
    buttons = []
    for model_id, label in VOXCPM_MODEL_CHOICES.items():
        is_sel = (model_id == current_model)
        btn_text = f"✅ {label}" if is_sel else label
        cb_slug = model_id.split("/")[-1].lower().replace(".", "").replace("-", "")
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"set_vox_model_{cb_slug}")])

    menu_text = (
        "🎙️ **VoxCPM Model Selector (OpenBMB Hugging Face)**\n\n"
        f"• **Current Model**: `{current_model}`\n\n"
        "Available OpenBMB foundation models:\n"
        "• **VoxCPM2** (`openbmb/VoxCPM2`) — Flagship 2B Multilingual Foundation Model (48kHz Studio Quality)\n"
        "• **VoxCPM1.5** (`openbmb/VoxCPM1.5`) — 0.6B Fast & Expressive Model (44.1kHz)\n"
        "• **VoxCPM0.5** (`openbmb/VoxCPM-0.5B`) — 0.5B Lightweight Low-Latency Baseline Model\n\n"
        "Tap a button below or switch manually:\n"
        "`/voxmodel VoxCPM2`\n"
        "`/voxmodel VoxCPM1.5`\n"
        "`/voxmodel VoxCPM0.5`\n"
        "Or pass any model from https://huggingface.co/openbmb/!"
    )
    await safe_reply_text(message, menu_text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^set_whisper_model_"), group=-1)
async def handle_whisper_model_selection(client: Client, callback_query: CallbackQuery):
    chat_id = callback_query.message.chat.id
    model_name = callback_query.data.replace("set_whisper_model_", "")
    settings = get_user_voice_settings(chat_id)
    if model_name in WHISPER_MODEL_CHOICES:
        settings["whisper_model"] = model_name
        os.environ["WHISPER_MODEL"] = model_name
        try:
            await callback_query.answer(f"Whisper model set to {model_name}")
        except Exception:
            pass
        text, markup = _build_voice_menu(chat_id)
        try:
            await callback_query.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
    else:
        await callback_query.answer("Invalid Whisper model.", show_alert=True)

@Client.on_message(filters.command("timezone") & is_allowed_bot, group=1)
async def timezone_command(client: Client, message: Message):
    if not await check_user_access(message): return
    chat_id = message.chat.id
    if len(message.command) < 2:
        current_time = get_user_current_time(chat_id)
        await safe_reply_text(message, f"🕒 **Current Configured Timezone:**\n{current_time}\n\nTo change your timezone, use `/timezone <offset>`\nExample: `/timezone +7`")
        return
    offset_str = message.command[1].replace("UTC", "").replace("utc", "").replace("+", "")
    try:
        offset = int(offset_str)
        if -12 <= offset <= 14:
            user_timezones[chat_id] = offset
            current_time = get_user_current_time(chat_id)
            await safe_reply_text(message, f"✅ Timezone updated to UTC{'+' if offset >= 0 else ''}{offset}:00!\nYour local time: **{current_time}**")
        else:
            await safe_reply_text(message, "Please enter an offset between -12 and +14.")
    except ValueError:
        await safe_reply_text(message, "Invalid format. Example: `/timezone +7`")

@Client.on_message(filters.command("search") & is_allowed_bot, group=1)
async def search_command(client: Client, message: Message):
    if not await check_user_access(message, require_assigned_server=True): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please type your search query below:", reply_markup=ForceReply(selective=True))
        return
    query = message.text.split(None, 1)[1]
    processing_msg = await safe_reply_text(message, f"⏱ [00:00] 🔍 Searching web for: `{query}`...")
    cancel_id = f"srch_{processing_msg.id}"
    register_cancel_task(cancel_id)
    try:
        async with RealtimeTimer(processing_msg, f"🔍 Searching: {query[:40]}", cancel_id=cancel_id):
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
            try:
                results = await asyncio.to_thread(lambda: DDGS().text(query, max_results=5))
                context = "".join([f"- {r.get('title')}: {r.get('body')}\n" for r in (results or [])]) or "No search results found."
            except Exception as se:
                print(f"[Search] DDGS error: {se}")
                context = "No search results available."
            try:
                reply = await asyncio.wait_for(
                    get_ai_response(message.chat.id, query, context=context), timeout=180.0
                )
            except asyncio.TimeoutError:
                reply = "⚠️ The AI took too long to respond. Please try again!"
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
        await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=query)
    except ProcessCancelledException:
        await safe_edit_text(processing_msg, "🛑 **Process stopped by user!** ✅")
    except Exception as e:
        print(f"[Search] Error: {e}")
        await safe_edit_text(processing_msg, "⚠️ Sorry, an error occurred while searching. Please try again!")
    finally:
        unregister_cancel_task(cancel_id)

def resolve_hongguo_url(url: str) -> str:
    """Resolve novelquickapp.com share links to canonical hongguoduanju.com series URL."""
    if not url: return ""
    url = url.strip()
    if "novelquickapp.com" in url.lower():
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                final_url = resp.geturl()
            m = re.search(r'(?:series_id|video_series_id|video_id)=(\d+)', final_url)
            if m:
                return f"https://hongguoduanju.com/detail?series_id={m.group(1)}"
            return final_url
        except Exception:
            m = re.search(r'(?:series_id|video_series_id|video_id)=(\d+)', url)
            if m:
                return f"https://hongguoduanju.com/detail?series_id={m.group(1)}"
    return url

def _is_hongguo_series_url(url: str) -> bool:
    if not url:
        return False
    u = (url or "").lower()
    return "hongguoduanju.com" in u or "novelquickapp.com" in u


def _fetch_hongguo_series(url: str):
    url = resolve_hongguo_url(url)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script = """
const hg = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const series = await hg.getSeries(url);
  const episodes = series.vids.map((vid, idx) => ({
    vid,
    episode: idx + 1,
    filename: hg.episodeFilename(series.name, idx + 1, series.episodeCount),
    title: `EP${String(idx + 1).padStart(String(series.episodeCount).length || 2, '0')} - ${series.name}`
  }));
  console.log(JSON.stringify({
    seriesId: series.seriesId,
    name: series.name,
    cover: series.cover || '',
    episodeCount: Number(series.episodeCount || episodes.length),
    accessibleCount: Number(series.accessibleCount || episodes.length),
    episodes
  }));
})();
"""
    node_bin = shutil.which("node") or "node"
    lib_path = os.path.join(repo_root, "Houngguo VIP Downloader", "lib", "hongguo.js")
    proc = subprocess.run([node_bin, "-e", script, lib_path, url], cwd=repo_root, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to fetch Houngguo series information.")
    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid response from Houngguo metadata fetch: {exc}") from exc
    return data


def _download_single_hongguo_episode_sync(series_id: str, vid: str, dest_path: str, series_name: str = "") -> dict:
    script = r"""
const hg = require(process.argv[1]);
(async () => {
  const seriesId = process.argv[2];
  const vid = process.argv[3];
  const dest = process.argv[4];
  const seriesName = process.argv[5] || '';
  const r = await hg.downloadEpisode(seriesId, vid, dest, { retries: 2, seriesName });
  console.log(JSON.stringify({ ok: !!r && !r.unavailable, bytes: (r && r.bytes) || 0, path: dest, unavailable: !!(r && r.unavailable) }));
})().catch(e => {
  process.stderr.write(String(e && e.message ? e.message : e));
  process.exit(1);
});
"""
    node_bin = shutil.which("node") or "node"
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    lib_path = os.path.join(repo_root, "Houngguo VIP Downloader", "lib", "hongguo.js")
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    proc = subprocess.run([node_bin, "-e", script, lib_path, str(series_id), str(vid), dest_path, str(series_name or "")], cwd=repo_root, capture_output=True, text=True, timeout=180, env=env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download episode")
    return json.loads(proc.stdout.strip())


async def _download_hongguo_batch(url: str, selected_vids=None):
    url = resolve_hongguo_url(url)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    series = await asyncio.to_thread(_fetch_hongguo_series, url)
    episodes = series.get("episodes", [])
    if selected_vids:
        selected = [episode for episode in episodes if episode["vid"] in set(selected_vids)]
    else:
        selected = episodes
    if not selected:
        raise ValueError("No matching episodes were found for this Hongguo series.")
    out_dir = get_temp_dir()
    script = """
const fs = require('fs');
const path = require('path');
const hg = require(process.argv[1]);
(async () => {
  const url = process.argv[2];
  const selected = JSON.parse(process.argv[3]);
  const series = await hg.getSeries(url);
  const results = [];
  for (const ep of selected) {
    const dest = path.join(process.argv[4], hg.episodeFilename(series.name, ep.episode, series.episodeCount));
    try {
      const r = await hg.downloadEpisode(series.seriesId, ep.vid, dest, { retries: 3 });
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: !!r && !r.unavailable, bytes: r && r.bytes ? r.bytes : 0 });
    } catch (e) {
      results.push({ vid: ep.vid, episode: ep.episode, path: dest, ok: false, error: String(e && e.message ? e.message : e) });
    }
  }
  console.log(JSON.stringify({ seriesId: series.seriesId, name: series.name, results }));
})();
"""
    node_bin = shutil.which("node") or "node"
    lib_path = os.path.join(repo_root, "Houngguo VIP Downloader", "lib", "hongguo.js")
    payload = json.dumps(selected)
    env = dict(os.environ)
    try:
        import imageio_ffmpeg
        env["FFMPEG_PATH"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    proc = subprocess.run([node_bin, "-e", script, lib_path, url, payload, out_dir], cwd=repo_root, capture_output=True, text=True, timeout=600, env=env)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Failed to download Hongguo episodes.")
    try:
        payload_data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Hongguo download response: {exc}") from exc
    return payload_data


_hg_select_sessions = {}  # {short_id: {"selected": set(), "page": 0, "series": series}}

def _build_hongguo_select_keyboard(short_id, episodes, selected, page):
    PAGE_SIZE = 8
    total_pages = max(1, (len(episodes) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, len(episodes))

    buttons = []
    # Episode grid (2 per row)
    row = []
    for ep in episodes[start_idx:end_idx]:
        vid = str(ep["vid"])
        ep_no = ep["episode"]
        state = "☑️" if vid in selected else "⬜"
        label = f"{state} EP{ep_no:02d}"
        row.append(InlineKeyboardButton(label, callback_data=f"hg_tgl|{short_id}|{vid}|{page}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Quick selection helpers
    quick_row = [
        InlineKeyboardButton("☑️ Page", callback_data=f"hg_sel_pg|{short_id}|{page}"),
        InlineKeyboardButton("☑️ All", callback_data=f"hg_sel_all|{short_id}|{page}"),
        InlineKeyboardButton("🧹 Clear", callback_data=f"hg_clr|{short_id}|{page}")
    ]
    buttons.append(quick_row)

    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"hg_page|{short_id}|{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"hg_page|{short_id}|{page}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"hg_page|{short_id}|{page + 1}"))
    buttons.append(nav_row)

    # Action row
    count = len(selected)
    action_row = [
        InlineKeyboardButton(f"📥 Download Selected ({count})", callback_data=f"hg_dl_sel|{short_id}"),
        InlineKeyboardButton("🔙 Back", callback_data=f"hg_back|{short_id}")
    ]
    buttons.append(action_row)

    return InlineKeyboardMarkup(buttons)

_pending_hg_custom_merge = {}

def _build_hongguo_merge_keyboard(short_id, mode="all"):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ No Merge (ទុកភាគមួយៗ)", callback_data=f"hg_run_dl|{short_id}|{mode}|1")],
        [
            InlineKeyboardButton("🔗 2 Videos (ជា ២)", callback_data=f"hg_run_dl|{short_id}|{mode}|2"),
            InlineKeyboardButton("🔗 3 Videos (ជា ៣)", callback_data=f"hg_run_dl|{short_id}|{mode}|3")
        ],
        [
            InlineKeyboardButton("🔗 5 Videos (ជា ៥)", callback_data=f"hg_run_dl|{short_id}|{mode}|5"),
            InlineKeyboardButton("🔗 10 Videos (ជា ១០)", callback_data=f"hg_run_dl|{short_id}|{mode}|10")
        ],
        [InlineKeyboardButton("🔢 Custom Number / បញ្ចូលចំនួន", callback_data=f"hg_ask_custom|{short_id}|{mode}")],
        [InlineKeyboardButton("🔙 Back / ថយក្រោយ", callback_data=f"hg_back|{short_id}")]
    ])


async def _stream_download_and_upload_hongguo(client, chat_id, status_msg, series, episodes_to_download, task_id=None, merge_parts=1):
    series_name = series.get("name", "Hongguo series")
    series_id = str(series.get("seriesId", ""))
    total = len(episodes_to_download)
    temp_dir = get_temp_dir()
    clean_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()
    if not task_id:
        task_id = f"direct_hg_{chat_id}_{series_id}"

    cancel_id = f"hg_{chat_id}_{series_id}"
    register_cancel_task(cancel_id)
    stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop Download", callback_data=f"hg_stop|{cancel_id}")]])

    import task_resumer
    cp = task_resumer.get_checkpoint(task_id) or {}
    completed_vids = set(cp.get("completed_vids", []))
    sent = len([ep for ep in episodes_to_download if str(ep.get("vid")) in completed_vids])

    # Check if user requested video merging into N parts
    if merge_parts > 1 and total > 1:
        from utils import partition_episodes, merge_video_files
        batches = partition_episodes(episodes_to_download, merge_parts)
        num_batches = len(batches)
        merged_sent = 0

        try:
            for b_idx, batch_eps in enumerate(batches, 1):
                if is_task_cancelled(cancel_id):
                    task_resumer.remove_checkpoint(task_id)
                    if status_msg:
                        await safe_edit_text(status_msg, f"🛑 **Hongguo download stopped by user!**\nSent **{merged_sent}/{num_batches}** merged part(s).")
                    return

                first_ep = batch_eps[0].get("episode", 1)
                last_ep = batch_eps[-1].get("episode", len(batch_eps))
                part_name = f"Part {b_idx:02d} (EP{first_ep:02d}-EP{last_ep:02d})"
                batch_paths = []

                for j, ep in enumerate(batch_eps, 1):
                    if is_task_cancelled(cancel_id):
                        for p in batch_paths: cleanup_file(p)
                        task_resumer.remove_checkpoint(task_id)
                        if status_msg:
                            await safe_edit_text(status_msg, f"🛑 **Hongguo download stopped by user!**\nSent **{merged_sent}/{num_batches}** merged part(s).")
                        return

                    ep_no = ep.get("episode", j)
                    vid = str(ep.get("vid"))
                    dest_filename = f"{clean_name}_p{b_idx}_EP{ep_no:02d}.mp4"
                    dest_path = os.path.join(temp_dir, dest_filename)

                    if status_msg:
                        await safe_edit_text(
                            status_msg,
                            f"🎬 **Downloading for {part_name}** ({b_idx}/{num_batches})\n"
                            f"📺 **EP{ep_no:02d}** ({j}/{len(batch_eps)} in this part)\n"
                            f"📁 **{series_name}**\n⏳ Please wait...",
                            reply_markup=stop_kb
                        )

                    # Download with up to 3 retries to prevent missing episodes
                    ep_downloaded = False
                    for attempt in range(1, 4):
                        dl_res = await asyncio.to_thread(_download_single_hongguo_episode_sync, series_id, vid, dest_path, series_name)
                        if dl_res.get("ok") and os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
                            batch_paths.append(dest_path)
                            ep_downloaded = True
                            break
                        print(f"[Hongguo Batch] Attempt {attempt}/3 failed for EP{ep_no}: {dl_res}. Retrying in 2s...")
                        await asyncio.sleep(2)

                    if not ep_downloaded:
                        print(f"[Hongguo Batch] ❌ EP{ep_no} failed to download after 3 attempts.")

                if not batch_paths:
                    continue

                merged_filename = f"{clean_name} - {part_name}.mp4"
                merged_dest = os.path.join(temp_dir, merged_filename)

                if status_msg:
                    await safe_edit_text(
                        status_msg,
                        f"🔗 **Merging {len(batch_paths)} episodes into {part_name}...**\n"
                        f"📺 **{series_name}**\n⚡ Please wait...",
                        reply_markup=stop_kb
                    )

                merged_out = await asyncio.to_thread(merge_video_files, batch_paths, merged_dest)

                # Clean up individual clips
                for p in batch_paths:
                    cleanup_file(p)

                if is_task_cancelled(cancel_id):
                    if merged_out: cleanup_file(merged_out)
                    task_resumer.remove_checkpoint(task_id)
                    return

                if merged_out and os.path.exists(merged_out):
                    if status_msg:
                        await safe_edit_text(
                            status_msg,
                            f"📤 **Uploading {part_name}** ({b_idx}/{num_batches})\n"
                            f"📺 **{series_name}**\n🚀 Sending to Telegram...",
                            reply_markup=stop_kb
                        )
                    await client.send_video(
                        chat_id=chat_id,
                        video=merged_out,
                        caption=f"🎬 **{series_name}** — {part_name} ({len(batch_eps)} Episodes)",
                        file_name=merged_filename,
                        supports_streaming=True
                    )
                    merged_sent += 1
                    cleanup_file(merged_out)

            task_resumer.remove_checkpoint(task_id)
            if status_msg:
                await safe_edit_text(status_msg, f"✅ **Hongguo Download Complete!**\nSent all **{merged_sent}/{num_batches}** merged parts of **{series_name}**.")
            return
        except Exception as merge_err:
            print(f"[Hongguo Stream Merge] Error: {merge_err}")

    try:
        for i, ep in enumerate(episodes_to_download, 1):
            if is_task_cancelled(cancel_id):
                task_resumer.remove_checkpoint(task_id)
                if status_msg:
                    await safe_edit_text(
                        status_msg,
                        f"🛑 **Hongguo download stopped by user!**\n"
                        f"Sent **{sent}/{total}** episode(s) from **{series_name}**."
                    )
                return

            ep_no = ep.get("episode", i)
            vid = str(ep.get("vid"))
            if vid in completed_vids:
                continue

            filename = f"{clean_name} - EP{ep_no:02d}.mp4"
            dest_path = os.path.join(temp_dir, filename)

            try:
                if status_msg:
                    await safe_edit_text(
                        status_msg,
                        f"🎬 **Downloading EP{ep_no:02d}** ({i}/{total})\n"
                        f"📺 **{series_name}**\n"
                        f"⏳ Please wait...",
                        reply_markup=stop_kb
                    )
                dl_res = await asyncio.to_thread(_download_single_hongguo_episode_sync, series_id, vid, dest_path, series_name)
                
                if is_task_cancelled(cancel_id):
                    try:
                        if os.path.exists(dest_path): os.remove(dest_path)
                    except Exception: pass
                    task_resumer.remove_checkpoint(task_id)
                    if status_msg:
                        await safe_edit_text(
                            status_msg,
                            f"🛑 **Hongguo download stopped by user!**\n"
                            f"Sent **{sent}/{total}** episode(s) from **{series_name}**."
                        )
                    return

                if not dl_res.get("ok") or not os.path.exists(dest_path) or os.path.getsize(dest_path) < 1000:
                    print(f"[Hongguo Stream] EP{ep_no} failed: {dl_res}")
                    continue

                if status_msg:
                    await safe_edit_text(
                        status_msg,
                        f"📤 **Uploading EP{ep_no:02d}** ({i}/{total})\n"
                        f"📺 **{series_name}**\n"
                        f"🚀 Sending to Telegram...",
                        reply_markup=stop_kb
                    )
                await client.send_video(
                    chat_id=chat_id,
                    video=dest_path,
                    caption=f"🎬 **{series_name}** — EP{ep_no:02d}",
                    file_name=filename,
                    supports_streaming=True
                )
                sent += 1
                completed_vids.add(vid)
                task_resumer.mark_episode_completed(task_id, vid, {
                    "type": "direct_hg",
                    "chat_id": chat_id,
                    "series": series,
                    "episodes": episodes_to_download,
                    "total": total,
                })
            except Exception as ep_err:
                print(f"[Hongguo Stream] Error EP{ep_no}: {ep_err}")
            finally:
                try:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass

        task_resumer.remove_checkpoint(task_id)
        summary = f"✅ Sent **{sent}/{total}** episode(s) from **{series_name}**!"
        if sent < total:
            summary += f"\n⚠️ {total - sent} episode(s) failed or require app playback."
        if status_msg:
            await safe_edit_text(status_msg, summary)
    finally:
        unregister_cancel_task(cancel_id)


def _media_library_keyboard(session_id, names, selected, page):
    page_size = 6
    page_count = max(1, (len(names) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    start = page * page_size
    buttons = []
    for index in range(start, min(start + page_size, len(names))):
        state = "☑️" if index in selected else "⬜"
        title = os.path.splitext(names[index])[0]
        if len(title) > 24:
            title = title[:21] + "..."
        buttons.append([
            InlineKeyboardButton(
                f"{state} {index + 1}. {title}",
                callback_data=f"ml|{session_id}|toggle|{index}",
            ),
            InlineKeyboardButton(
                "🖼 Thumbnail",
                callback_data=f"ml|{session_id}|thumb|{index}",
            ),
        ])
    if page_count > 1:
        buttons.append([
            InlineKeyboardButton("⬅️", callback_data=f"ml|{session_id}|page|{page - 1}"),
            InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data=f"ml|{session_id}|page|{page}"),
            InlineKeyboardButton("➡️", callback_data=f"ml|{session_id}|page|{page + 1}"),
        ])
    buttons.append([
        InlineKeyboardButton(
            f"Download selected ({len(selected)})",
            callback_data=f"ml|{session_id}|send|selected",
        ),
        InlineKeyboardButton("Download all", callback_data=f"ml|{session_id}|send|all"),
    ])
    return InlineKeyboardMarkup(buttons), page


@Client.on_message(filters.command("library") & filters.private, group=0)
async def media_library_command(client, message):
    if not await check_user_access(message, require_assigned_server=True):
        return
    try:
        items = await asyncio.to_thread(list_media)
    except OSError as exc:
        print(f"Media-library listing failed: {exc}")
        await message.reply_text("Unable to read the local media library.", parse_mode=None)
        return
    if not items:
        await message.reply_text(
            "No local videos found. Add authorized video files to the server's media library folder.",
            parse_mode=None,
        )
        return

    session_id = uuid.uuid4().hex[:8]
    names = [item["name"] for item in items]
    with media_library_sessions_lock:
        now = time.time()
        expired = [
            key for key, value in media_library_sessions.items()
            if value["expires_at"] <= now
        ]
        for key in expired:
            media_library_sessions.pop(key, None)
        media_library_sessions[session_id] = {
            "owner_id": message.from_user.id,
            "names": names,
            "selected": set(),
            "page": 0,
            "expires_at": now + 1800,
        }

    keyboard, _ = _media_library_keyboard(session_id, names, set(), 0)
    await message.reply_text(
        f"Local media library: {len(names)} video(s). Select episodes or download all.",
        reply_markup=keyboard,
        parse_mode=None,
    )


@Client.on_message(filters.command("download"), group=0)
async def download_command(client, message):
    if not await check_user_access(message, require_assigned_server=True): return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please paste the link you want to download below:", reply_markup=ForceReply(selective=True))
        return
    url = message.text.split(None, 1)[1]
    if not is_url(url):
        await safe_reply_text(message, "That doesn't look like a valid URL.")
        return
    from downloader import classify_url, clean_youtube_url
    if classify_url(url) == 'youtube':
        url = clean_youtube_url(url)
    from plugins.iqiyi_downloader import _is_iqiyi_url as _is_iq_url, handle_iqiyi_url
    if _is_iq_url(url):
        await handle_iqiyi_url(client, message, url)
        return
    from plugins.wetv_downloader import _is_wetv_url, handle_wetv_url
    if _is_wetv_url(url):
        await handle_wetv_url(client, message, url)
        return
    if _is_hongguo_series_url(url):
        url = resolve_hongguo_url(url)
        try:
            series = await asyncio.to_thread(_fetch_hongguo_series, url)
        except Exception as exc:
            await safe_reply_text(message, f"❌ Unable to read this Hongguo series: {exc}")
            return
        short_id = str(uuid.uuid4())[:8]
        url_cache[short_id] = url
        title         = series.get("name", "Hongguo series")
        episode_count = series.get("episodeCount", len(series.get("episodes", [])))
        cover         = series.get("cover", "")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Download All Episodes",  callback_data=f"hg_series_action|{short_id}|all")],
            [InlineKeyboardButton("☑️ Select Episodes",         callback_data=f"hg_series_action|{short_id}|select")],
            [InlineKeyboardButton("🖼 Thumbnail",               callback_data=f"hg_thumb|{short_id}")],
        ])
        caption = (
            f"🎬 **{title}**\n"
            f"📺 {episode_count} episodes\n\n"
            "Choose how to download:"
        )
        if cover:
            try:
                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=cover,
                    caption=caption,
                    reply_markup=keyboard,
                )
                return
            except Exception:
                pass
        await safe_reply_text(message, caption, reply_markup=keyboard)
        return
    short_id = str(uuid.uuid4())[:8]
    url_cache[short_id] = url
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Download", callback_data=f"url_show_dl|{short_id}"),
            InlineKeyboardButton("🔍 Analyze Media", callback_data=f"url_show_analyze|{short_id}")
        ],
        [
            InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
        ]
    ])
    await safe_reply_text(message, f"🔗 **Link Detected:** `{url}`\nWhat would you like to do?", reply_markup=keyboard)

@Client.on_message(filters.command("tgdownload"), group=0)
async def tgdownload_command(client, message):
    if not await check_user_access(message, require_assigned_server=True):
        return
    if len(message.command) < 2:
        await safe_reply_text(message, "Please provide a link to download. Usage: /tgdownload <link>", reply_markup=ForceReply(selective=True))
        return
    url = message.text.split(None, 1)[1]
    if not is_url(url):
        await safe_reply_text(message, "That doesn't look like a valid URL.")
        return
    
    async with JobQueueContext(message, "Telegram Download"):
        processing_msg = await safe_reply_text(message, "⏳ Downloading... Please wait.")
        try:
            def progress_cb(text):
                # Update processing message asynchronously
                asyncio.create_task(safe_edit_text(processing_msg, text))
            if is_telegram_link(url):
                filepath = await download_telegram_post_media(client, url, False, progress_cb, requesting_user_id=message.from_user.id)
            else:
                filepath = await asyncio.to_thread(download_media, url, False, progress_cb)
            if not filepath or not os.path.exists(str(filepath)):
                await safe_edit_text(processing_msg, "❌ Failed to download media from the link.")
                return
            await safe_edit_text(processing_msg, "✅ Download complete! Uploading video...")
            await client.send_video(chat_id=message.chat.id, video=filepath, supports_streaming=True)
            await safe_edit_text(processing_msg, "Done! ✅")
        except Exception as e:
            print(f"[tgdownload] Error: {e}")
            await safe_edit_text(processing_msg, f"❌ Error during download: {e}")
        finally:
            if 'filepath' in locals() and filepath and os.path.exists(str(filepath)):
                cleanup_file(filepath)

@Client.on_message(filters.command("convert"), group=0)
async def convert_command(client, message):
    if not await check_user_access(message, require_assigned_server=True): return
    await safe_reply_text(message, "Please send the video, image, or document file you want to convert below:", reply_markup=ForceReply(selective=True))

# ==================== PUBLISH TO GROUPS / CHANNELS ====================

async def refresh_admin_chats(client: Client):
    """
    Re-verify bot's admin status in all currently known chats.
    NOTE: Bots cannot call get_dialogs() — so we only verify chats
    already in known_admin_chats. New chats are added via on_chat_member_updated.
    """
    global known_admin_chats
    to_remove = []
    for chat_id, info in list(known_admin_chats.items()):
        try:
            me = await client.get_chat_member(chat_id, "me")
            status = str(getattr(me, 'status', '')).lower()
            if not any(s in status for s in ("administrator", "owner", "creator")):
                to_remove.append(chat_id)  # lost admin — remove
            else:
                # Refresh title in case it changed
                try:
                    chat = await client.get_chat(chat_id)
                    chat_type = "channel" if str(getattr(chat, 'type', '')).lower() in ("channel", "chattype.channel") else "group"
                    known_admin_chats[chat_id] = {
                        "title": chat.title or info.get("title", "(no title)"),
                        "type": chat_type,
                        "username": getattr(chat, 'username', info.get("username")),
                        "is_forum": getattr(chat, 'is_forum', False)
                    }
                except Exception:
                    pass  # keep old info if refresh fails
        except Exception:
            to_remove.append(chat_id)  # can't reach chat — remove
    for cid in to_remove:
        known_admin_chats.pop(cid, None)
    print(f"[refresh_admin_chats] {len(known_admin_chats)} admin chats verified.")

def _build_pub_dest_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing all admin chats for publish destination."""
    buttons = []
    for cid, info in known_admin_chats.items():
        icon = "📢" if info["type"] == "channel" else "👥"
        label = f"{icon} {info['title'][:32]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pub_dest|{short_id}|{cid}")])
    buttons.append([InlineKeyboardButton("🔄 Refresh List", callback_data="pub_refresh")])
    return InlineKeyboardMarkup(buttons)

async def _get_forum_topics(client: Client, chat_id: int) -> list:
    """
    Return list of forum topics for a supergroup.
    Each returned item has .id (int) and .name (str) attributes.
    Topics are deduplicated by id.
    """
    try:
        from types import SimpleNamespace
        # First: call get_chat to ensure peer and access_hash are cached in Pyrogram
        chat = await client.get_chat(chat_id)
        chat_type = str(getattr(chat, 'type', '')).lower()
        is_forum = getattr(chat, 'is_forum', False)
        print(f"[_get_forum_topics] chat_id={chat_id}, type={chat_type}, is_forum={is_forum}")

        if "channel" in chat_type and "supergroup" not in chat_type:
            # Standard channels do not have topics
            return []

        topics = []
        seen_ids = set()

        # Method 1: Pyrogram high-level generator
        try:
            async for t in client.get_forum_topics(chat_id):
                t_id = getattr(t, 'id', None)
                t_name = getattr(t, 'name', None) or getattr(t, 'title', None)
                if t_id is not None and t_name and t_id not in seen_ids:
                    seen_ids.add(t_id)
                    topics.append(SimpleNamespace(id=t_id, name=t_name))
        except Exception as e_hl:
            print(f"[_get_forum_topics] Pyrogram get_forum_topics info: {e_hl}")

        # Method 2: Raw MTProto invoke fallback
        if not topics:
            try:
                from pyrogram.raw import functions as raw_funcs, types as raw_types
                peer = await client.resolve_peer(chat_id)
                if isinstance(peer, raw_types.InputPeerChannel):
                    result = await client.invoke(
                        raw_funcs.channels.GetForumTopics(
                            channel=raw_types.InputChannel(
                                channel_id=peer.channel_id,
                                access_hash=peer.access_hash
                            ),
                            offset_date=0,
                            offset_id=0,
                            offset_topic=0,
                            limit=100
                        )
                    )
                    for t in getattr(result, "topics", []):
                        topic_id = getattr(t, "id", None)
                        topic_title = getattr(t, "title", None)
                        if topic_id is not None and topic_title and topic_id not in seen_ids:
                            seen_ids.add(topic_id)
                            topics.append(SimpleNamespace(id=topic_id, name=topic_title))
            except Exception as e_raw:
                print(f"[_get_forum_topics] raw GetForumTopics error: {e_raw}")

        print(f"[_get_forum_topics] chat_id={chat_id} → {len(topics)} unique topics found")
        return topics

    except Exception as e:
        print(f"[_get_forum_topics] outer error for {chat_id}: {e}")
        return []

async def _do_publish(client: Client, query_msg, callback_query, short_id: str, target_chat_id: int, caption: str | None, topic_id: int | None):
    """Send the cached file (or text) to the target chat, optionally in a specific topic."""
    original_msg = url_cache.get(short_id)
    info = known_admin_chats.get(target_chat_id, {})
    icon = "📢" if info.get("type") == "channel" else "👥"
    chat_title = info.get("title", str(target_chat_id))
    send_kwargs = {}
    if topic_id is not None:
        send_kwargs["message_thread_id"] = topic_id
    cap = caption or ""
    try:
        if original_msg is None:
            await safe_edit_text(query_msg, "❌ **Session expired.** Please send the file again.")
            return
        if getattr(original_msg, "photo", None):
            await client.send_photo(target_chat_id, original_msg.photo.file_id, caption=cap, **send_kwargs)
        elif getattr(original_msg, "video", None):
            await client.send_video(target_chat_id, original_msg.video.file_id, caption=cap, supports_streaming=True, **send_kwargs)
        elif getattr(original_msg, "audio", None):
            await client.send_audio(target_chat_id, original_msg.audio.file_id, caption=cap, **send_kwargs)
        elif getattr(original_msg, "voice", None):
            await client.send_voice(target_chat_id, original_msg.voice.file_id, caption=cap, **send_kwargs)
        elif getattr(original_msg, "document", None):
            await client.send_document(target_chat_id, original_msg.document.file_id, caption=cap, **send_kwargs)
        elif getattr(original_msg, "text", None) or cap:
            txt_to_send = cap or getattr(original_msg, "text", "")
            await client.send_message(target_chat_id, text=txt_to_send, **send_kwargs)
        else:
            await safe_edit_text(query_msg, "❌ **No supported media to publish.**")
            return
        topic_note = f"\n📂 Topic ID: `{topic_id}`" if topic_id else ""
        await safe_edit_text(query_msg,
            f"✅ **Published successfully!**\n\n{icon} **{chat_title}**{topic_note}"
        )
    except Exception as e:
        await safe_edit_text(query_msg, f"❌ **Failed to publish:** `{e}`")

# ==================== THUMBNAIL FLOW HELPERS ====================

async def _ask_thumbnail(callback_query, query_msg, job: dict):
    """Store job params and ask user if they want to add a thumbnail."""
    user_id = callback_query.from_user.id
    thumb_pending[user_id] = job
    job_id = f"{user_id}"
    action_label = "🎙 Voice Dubbing" if "dub" in job["action"] else "📝 AI Recap"
    short_id = job.get("short_id", "")
    src_lang = job.get("src_lang", "auto")
    
    # Back button goes back to Target Language Selection (Step 2)
    mode = "dub_url" if "url" in job.get("action", "") else ("recap_url" if "recap" in job.get("action", "") and "url" in job.get("action", "") else ("recap_file" if "recap" in job.get("action", "") else "dub_file"))
    back_cb = f"src_sel|{mode}|{short_id}|{src_lang}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Yes, add thumbnail", callback_data=f"thumb_yes|{job_id}"),
            InlineKeyboardButton("▶️ No, skip", callback_data=f"thumb_no|{job_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_cb)
        ]
    ])
    await safe_edit_text(query_msg,
        f"🎬 **{action_label} — Ready to Start!**\n\n"
        "🖼️ **Do you want to add a custom thumbnail image to the output video?**\n\n"
        "Thumbnail will be embedded as the video cover art.",
        reply_markup=keyboard
    )

async def _ask_bgm(client, query_msg, job: dict):
    """Ask user if they want background sound."""
    user_id = query_msg.chat.id
    thumb_pending[user_id] = job
    job["step"] = "await_bgm"
    job_id = f"{user_id}"
    # Back button goes back to thumbnail selection
    back_cb = f"thumb_back|{job_id}"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎵 Yes, keep background sound", callback_data=f"bgm_yes|{job_id}")
        ],
        [
            InlineKeyboardButton("🔇 No, AI voice only", callback_data=f"bgm_no|{job_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_cb)
        ]
    ])
    await safe_edit_text(query_msg,
        "🎵 **Background Sound Settings**\n\n"
        "Do you want to keep the original sound effects and background music in the translated video?\n"
        "- **Yes**: AI Voice + Sound Effects + Background Music\n"
        "- **No**: Pure AI Voice only",
        reply_markup=keyboard
    )

async def _run_thumb_job(client: Client, query_msg, job: dict):
    """Execute a queued dub or recap job with optional thumbnail."""
    from utils import cleanup_file as _cleanup
    action = job["action"]          # "dub_file"|"recap_file"|"dub_url"|"recap_url"
    src_lang = job["src_lang"]
    target_lang = job["target_lang"]
    thumb_path = job.get("thumb_path")
    short_id = job.get("short_id")
    url = job.get("url")
    is_video = job.get("is_video", True)

    cancel_id = f"thumb_{query_msg.id}"
    register_cancel_task(cancel_id)
    try:
        action_title = "🎙 Voice Dubbing" if "dub" in action else "📝 AI Recap"
        async with JobQueueContext(query_msg, action_title):
            async with RealtimeTimer(query_msg, f"{action_title} starting...", cancel_id=cancel_id) as timer:
                def progress_cb(text):
                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                    timer.update_text(text)
                input_path = None
                try:
                    # --- Acquire input ---
                    if "file" in action:
                        cached_msg = url_cache.get(short_id)
                        if not cached_msg:
                            await safe_edit_text(query_msg, "❌ Session expired. Please send the file again.")
                            return
                        def _dl_prog(current, total):
                            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                            pct = current * 100 / total
                            timer.update_text(f"Downloading... {pct:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                        input_path = await cached_msg.download(progress=_dl_prog)
                    else:  # url
                        dl_res = await asyncio.to_thread(download_media, url, False, progress_cb)
                        input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res

                    if not input_path or not os.path.exists(input_path):
                        await safe_edit_text(query_msg, "❌ Failed to download source media.")
                        return

                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()

                    # --- Run the job ---
                    from converter import prepare_telegram_thumbnail
                    valid_thumb = thumb_path if (thumb_path and os.path.exists(thumb_path)) else None
                    tg_thumb = await asyncio.to_thread(prepare_telegram_thumbnail, valid_thumb) if valid_thumb else None

                    if "dub" in action:
                        from converter import translate_and_dub_media
                        output_path = await asyncio.to_thread(
                            translate_and_dub_media,
                            input_path,
                            target_lang,
                            src_lang,
                            is_video,
                            progress_cb,
                            valid_thumb,
                            job.get("keep_bgm", True),
                            job.get("tts_backend"),
                            job.get("whisper_model"),
                            job.get("transcribe_engine"),
                            job.get("translate_engine"),
                            job.get("voxcpm_model")
                        )
                        _cleanup(input_path)
                        if is_task_cancelled(cancel_id):
                            if valid_thumb: _cleanup(valid_thumb)
                            if tg_thumb and tg_thumb != valid_thumb: _cleanup(tg_thumb)
                            raise ProcessCancelledException()

                        if output_path and isinstance(output_path, str) and not output_path.startswith("ERROR:") and os.path.exists(output_path):
                            timer.update_text("Dubbing complete! Uploading...")
                            def _up_prog(c, t):
                                if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                                timer.update_text(f"Uploading... {c*100/t:.1f}% ({c/1048576:.1f}MB / {t/1048576:.1f}MB)")

                            send_kwargs = {"progress": _up_prog}
                            # Detailed caption showing voice and translation engines used
                            tts_be = job.get("tts_backend", "edge-tts")
                            vox_m = job.get("voxcpm_model", "openbmb/VoxCPM2")
                            if tts_be in ("voxcpm", "voxcpm2"):
                                from converter import normalize_voxcpm_model, VOXCPM_MODELS
                                vox_resolved = normalize_voxcpm_model(vox_m)
                                vox_label = VOXCPM_MODELS.get(vox_resolved, vox_resolved)
                                tts_label = f"VoxCPM ({vox_label}) [Voice Clone]"
                            elif target_lang[:2] == 'km':
                                tts_label = "Edge-TTS (Microsoft Neural - Piseth / Sreymom)"
                            elif tts_be == "edge-tts":
                                tts_label = "Edge-TTS (Microsoft Neural)"
                            elif tts_be == "gtts":
                                tts_label = "Google TTS (gTTS)"
                            else:
                                tts_label = str(tts_be)

                            trans_eng = job.get("translate_engine", "gemini")
                            trans_label = "Gemini AI" if trans_eng == "gemini" else "Local / Free AI"

                            transc_eng = job.get("transcribe_engine", "gemini")
                            whisp_m = job.get("whisper_model", "medium")
                            transc_label = "Gemini AI" if transc_eng == "gemini" else f"Whisper ({whisp_m})"

                            caption_text = (
                                f"🎬 **Dubbed Video ({target_lang.upper()})**\n\n"
                                f"🗣️ **Voice Engine**: `{tts_label}`\n"
                                f"🌐 **Translate Engine**: `{trans_label}`\n"
                                f"🎙️ **Transcribe Engine**: `{transc_label}`"
                            )
                            send_kwargs["caption"] = caption_text

                            active_thumb = tg_thumb or valid_thumb
                            if active_thumb and os.path.exists(active_thumb):
                                send_kwargs["thumb"] = active_thumb
                                try:
                                    from PIL import Image
                                    with Image.open(valid_thumb or active_thumb) as img:
                                        send_kwargs["width"], send_kwargs["height"] = img.size
                                except Exception:
                                    pass
    
                            if is_video:
                                await client.send_video(chat_id=query_msg.chat.id, video=output_path, supports_streaming=True, **send_kwargs)
                            else:
                                await client.send_audio(chat_id=query_msg.chat.id, audio=output_path, **send_kwargs)
    
                            _cleanup(output_path)
                            if valid_thumb: _cleanup(valid_thumb)
                            if tg_thumb and tg_thumb != valid_thumb: _cleanup(tg_thumb)
                            await safe_edit_text(
                                query_msg,
                                f"🎙 **Voice dubbing complete!** ✅\n\n"
                                f"🗣️ **Voice Engine**: `{tts_label}`\n"
                                f"🌐 **Translate Engine**: `{trans_label}`\n"
                                f"🎙️ **Transcribe Engine**: `{transc_label}`"
                            )
                        else:
                            if valid_thumb: _cleanup(valid_thumb)
                            if tg_thumb and tg_thumb != valid_thumb: _cleanup(tg_thumb)
                            await safe_edit_text(query_msg, f"❌ {output_path}")
                    else:  # recap
                        from converter import recap_video_audio
                        recap_text, media_out = await asyncio.to_thread(
                            recap_video_audio,
                            input_path, target_lang, src_lang, is_video, True, progress_cb, valid_thumb
                        )
                        _cleanup(input_path)
                        if is_task_cancelled(cancel_id):
                            if valid_thumb: _cleanup(valid_thumb)
                            if tg_thumb and tg_thumb != valid_thumb: _cleanup(tg_thumb)
                            raise ProcessCancelledException()
    
                        await safe_edit_text(query_msg, recap_text)
                        if media_out and os.path.exists(media_out):
                            def _up_prog2(c, t):
                                if is_task_cancelled(cancel_id): raise ProcessCancelledException()
    
                            send_kwargs2 = {"progress": _up_prog2}
                            active_thumb2 = tg_thumb or valid_thumb
                            if active_thumb2 and os.path.exists(active_thumb2):
                                send_kwargs2["thumb"] = active_thumb2
                                try:
                                    from PIL import Image
                                    with Image.open(valid_thumb or active_thumb2) as img:
                                        send_kwargs2["width"], send_kwargs2["height"] = img.size
                                except Exception:
                                    pass
    
                            if is_video:
                                await client.send_video(chat_id=query_msg.chat.id, video=media_out, caption=f"🎙 **Voiceover Recap ({target_lang.upper()})**", supports_streaming=True, **send_kwargs2)
                            else:
                                await client.send_audio(chat_id=query_msg.chat.id, audio=media_out, caption=f"🎙 **Voiceover Recap Audio ({target_lang.upper()})**", **send_kwargs2)
    
                            _cleanup(media_out)
                        if valid_thumb: _cleanup(valid_thumb)
                        if tg_thumb and tg_thumb != valid_thumb: _cleanup(tg_thumb)
    
                except ProcessCancelledException:
                    if input_path: _cleanup(input_path)
                    if thumb_path: _cleanup(thumb_path)
                    await safe_edit_text(query_msg, "🛑 **Process stopped by user!** ✅")
                except Exception as e:
                    print(f"_run_thumb_job error ({action}): {e}")
                    if input_path: _cleanup(input_path)
                    if thumb_path: _cleanup(thumb_path)
                    await safe_edit_text(query_msg, f"❌ Error: {e}")
    finally:
        unregister_cancel_task(cancel_id)

@Client.on_message(filters.new_chat_members, group=-1)
async def on_bot_added_to_chat(client, message):
    """Track when the bot is added to a group or channel."""
    try:
        me = await client.get_me()
        for member in (message.new_chat_members or []):
            if member.id == me.id:
                chat = message.chat
                chat_type = "channel" if str(chat.type).lower() in ("channel", "chattype.channel") else "group"
                known_admin_chats[chat.id] = {
                    "title": chat.title or "(no title)",
                    "type": chat_type,
                    "username": getattr(chat, 'username', None)
                }
                print(f"Bot added to {chat_type}: {chat.title} ({chat.id})")
    except Exception as e:
        print(f"on_bot_added_to_chat error: {e}")

@Client.on_chat_member_updated(group=-1)
async def on_bot_status_changed(client, update):
    """Track when bot is promoted to admin or demoted/removed in any chat."""
    try:
        me = await client.get_me()
        new_member = getattr(update, 'new_chat_member', None)
        old_member = getattr(update, 'old_chat_member', None)
        if not new_member:
            return
        user = getattr(new_member, 'user', None) or getattr(new_member, 'chat', None)
        if not user or getattr(user, 'id', None) != me.id:
            return
        chat = update.chat
        new_status = str(getattr(new_member, 'status', '')).lower()
        is_admin = any(s in new_status for s in ("administrator", "owner", "creator"))
        if is_admin:
            chat_type = "channel" if str(getattr(chat, 'type', '')).lower() in ("channel", "chattype.channel") else "group"
            known_admin_chats[chat.id] = {
                "title": chat.title or "(no title)",
                "type": chat_type,
                "username": getattr(chat, 'username', None)
            }
            print(f"[admin_chats] Bot is now admin in {chat_type}: {chat.title} ({chat.id})")
        else:
            # Demoted or kicked — remove from registry
            if chat.id in known_admin_chats:
                known_admin_chats.pop(chat.id, None)
                print(f"[admin_chats] Bot lost admin in: {chat.title} ({chat.id})")
    except Exception as e:
        print(f"on_bot_status_changed error: {e}")

@Client.on_message(filters.command("setfb"), group=0)
async def setfb_command(client: Client, message: Message):
    if not await check_user_access(message): return
    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        await message.reply_text("⚠️ Usage: `/setfb <page_id> <page_token> [optional_page_name]`\n\n"
                                 "**How to get them:**\n"
                                 "1. Go to [Facebook Developers](https://developers.facebook.com/)\n"
                                 "2. Create an App, add 'Facebook Login for Business'.\n"
                                 "3. Generate a Page Access Token.\n"
                                 "4. Find your Page ID in your Facebook Page settings.")
        return
    page_id, token = args[1], args[2]
    custom_name = args[3] if len(args) > 3 else None
    
    msg = await message.reply_text("⏳ Verifying your Facebook Page credentials...")
    from facebook_util import check_fb_token
    is_valid, page_name = check_fb_token(page_id, token)
    
    if not is_valid:
        await msg.edit_text(f"❌ Verification failed: {page_name}")
        return
        
    final_name = custom_name if custom_name else page_name
        
    from user_manager import get_user_facebook_pages, update_user_facebook
    pages = get_user_facebook_pages(message.from_user.id)
    
    found = False
    for p in pages:
        if p.get("page_id") == page_id:
            p["page_token"] = token
            p["page_name"] = final_name
            found = True
            break
            
    if not found:
        pages.append({
            "page_id": page_id,
            "page_token": token,
            "page_name": final_name
        })
        
    update_user_facebook(message.from_user.id, pages)
    await msg.edit_text(f"✅ Facebook Page **{final_name}** ({page_id}) has been saved to your account!")

@Client.on_message(filters.command(["fbsync", "syncfb"]), group=0)
async def fbsync_command(client: Client, message: Message):
    if not await check_user_access(message): return
    args = message.text.split(maxsplit=1)
    user_token = args[1].strip() if len(args) > 1 else os.environ.get("FB_USER_TOKEN", "")
    if not user_token:
        await message.reply_text("⚠️ Usage: `/fbsync <facebook_user_access_token>`\n\n"
                                 "This automatically discovers and registers ALL Facebook Pages your account can post to!")
        return

    msg = await message.reply_text("⏳ Connecting to Facebook Graph API to discover your pages...")
    try:
        import facebook_manager
        pages = facebook_manager.sync_and_store_facebook_pages(user_token, user_id=str(message.from_user.id))
        if not pages:
            await msg.edit_text("⚠️ No Facebook Pages were found for this user access token.")
            return

        postable = [p for p in pages if p.get("can_post")]
        lines = [f"✅ **Discovered {len(pages)} Facebook Page(s)** ({len(postable)} can post):\n"]
        for p in pages:
            status_icon = "🟢" if p.get("can_post") else "🟡"
            lines.append(f"{status_icon} **{p['page_name']}** (`{p['page_id']}`)")
        lines.append("\nAll pages have been saved! You can now use `/postfb` or schedule posts to them.")
        await msg.edit_text("\n".join(lines))
    except Exception as e:
        await msg.edit_text(f"❌ Failed to sync Facebook Pages: {e}")

@Client.on_message(filters.command("checkfb"), group=0)
async def checkfb_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import get_user_facebook
    page_id, token = get_user_facebook(message.from_user.id)
    if not page_id or not token:
        await message.reply_text("⚠️ You haven't set up your Facebook credentials yet.\n\n"
                                 "**How to get Facebook Page ID and Token:**\n"
                                 "1. Go to [Facebook Developers](https://developers.facebook.com/)\n"
                                 "2. Create an App, add 'Facebook Login for Business' product.\n"
                                 "3. Generate a Page Access Token.\n"
                                 "4. Get your Page ID from your Page's About section.\n\n"
                                 "Use `/setfb <page_id> <page_token>` to save it.")
        return
    from facebook_util import check_fb_token
    is_valid, name = check_fb_token(page_id, token)
    if is_valid:
        await message.reply_text(f"✅ Facebook Token is valid! Connected to Page: **{name}**")
    else:
        await message.reply_text(f"❌ Facebook Token check failed: {name}")

@Client.on_message(filters.command("allfb"), group=0)
async def allfb_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import load_users, get_user, AUTO_APPROVED_USERNAMES
    u = get_user(message.from_user.id)
    is_super_admin = message.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    if not is_super_admin:
        await message.reply_text("⚠️ You do not have permission to use this command.")
        return
        
    users = load_users()
    fb_users = []
    for uid, udata in users.items():
        if uid == "__system_config__": continue
        page_id = udata.get("fb_page_id")
        if page_id:
            name = (udata.get("first_name", "") + " " + udata.get("last_name", "")).strip()
            username = udata.get("username")
            if username:
                name += f" (@{username})"
            name = name.strip() or uid
            fb_users.append(f"• **{name}**: `{page_id}`")
            
    if not fb_users:
        await message.reply_text("No users have configured custom Facebook pages.")
        return
        
    msg_text = "📋 **Users with Custom Facebook Pages:**\n\n" + "\n".join(fb_users)
    await message.reply_text(msg_text)


@Client.on_message(filters.command(["setgemini", "geminikeys", "gemini"]), group=0)
async def set_gemini_keys_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import get_user, AUTO_APPROVED_USERNAMES
    u = get_user(message.from_user.id)
    is_super_admin = message.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    if not is_super_admin:
        await message.reply_text("⚠️ You do not have permission to configure Gemini API keys.")
        return

    from gemini_manager import get_gemini_keys, save_gemini_keys, mask_key
    args = message.text.split(None, 1)
    if len(args) < 2:
        current_keys = get_gemini_keys()
        if not current_keys:
            await message.reply_text(
                "🔑 **Gemini API Keys Manager**\n\n"
                "No Gemini API keys currently configured.\n\n"
                "To add multiple keys with automatic failover, send:\n"
                "`/setgemini key1, key2, key3`\n"
                "(or paste them separated by newlines)"
            )
        else:
            lines = [f"{i+1}. `{mask_key(k)}`" for i, k in enumerate(current_keys)]
            await message.reply_text(
                f"🔑 **Gemini API Keys ({len(current_keys)} configured):**\n\n" +
                "\n".join(lines) +
                "\n\n*If one key hits rate limit (429), the bot automatically switches to the next!*\n\n"
                "To replace or add keys, send:\n`/setgemini key1, key2...`\n"
                "To test all keys, send:\n`/checkgemini`"
            )
        return

    raw_input = args[1].strip()
    saved = save_gemini_keys(raw_input)
    if not saved:
        await message.reply_text("❌ No valid keys found. Please check your input format.")
        return

    masked_lines = [f"{i+1}. `{mask_key(k)}`" for i, k in enumerate(saved)]
    await message.reply_text(
        f"✅ **Saved {len(saved)} Gemini API Key(s) successfully!**\n\n" +
        "\n".join(masked_lines) +
        "\n\n🔄 Automatic rotation & failover is now active. Send `/checkgemini` to verify health."
    )


@Client.on_message(filters.command(["checkgemini", "testgemini"]), group=0)
async def check_gemini_keys_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import get_user, AUTO_APPROVED_USERNAMES
    u = get_user(message.from_user.id)
    is_super_admin = message.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    if not is_super_admin:
        await message.reply_text("⚠️ You do not have permission to test Gemini API keys.")
        return

    from gemini_manager import test_all_keys
    progress_msg = await message.reply_text("⚡ Testing all configured Gemini API keys in parallel... Please wait.")
    try:
        results = await asyncio.to_thread(test_all_keys)
        if not results:
            await safe_edit_text(progress_msg, "⚠️ No Gemini keys configured yet. Use `/setgemini key1, key2...` to add keys.")
            return

        report = [f"📊 **Gemini API Keys Status ({len(results)} tested):**\n"]
        for idx, res in enumerate(results):
            status = res["status"]
            if status == "ACTIVE":
                badge = f"✅ **ACTIVE** (`{res['model']}`)"
            elif status == "RATE_LIMITED":
                badge = "⚠️ **RATE LIMITED (429)** — *Will auto-failover to next key*"
            else:
                badge = f"❌ **{status}**: {res.get('error', 'Failed')}"
            report.append(f"{idx+1}. `{res['key']}`\n   ↳ {badge}")

        await safe_edit_text(progress_msg, "\n\n".join(report))
    except Exception as e:
        await safe_edit_text(progress_msg, f"❌ Test error: {e}")

async def execute_fb_post(client, message, target, text_start, is_super_admin, fb_access_list, status_msg=None):
    import os
    pages_to_post = []
    
    if target == "aimovie":
        p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
    elif target == "livealone":
        p = os.environ.get("LIVEALONE_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p and t: pages_to_post.append((p, t, "LiveAlone"))
    elif target in ["all", "all_system"]:
        p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
        p2 = os.environ.get("LIVEALONE_PAGE_ID")
        t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
    elif target == "all_saved":
        from user_manager import get_user_facebook_pages
        pages = get_user_facebook_pages(message.from_user.id)
        for i, p in enumerate(pages):
            pid, ptok, pname = p.get("page_id"), p.get("page_token"), p.get("page_name", f"Saved Page {i+1}")
            if pid and ptok: pages_to_post.append((pid, ptok, pname))
    elif target.startswith("saved_"):
        idx = int(target.split("_")[1])
        from user_manager import get_user_facebook_pages
        pages = get_user_facebook_pages(message.from_user.id)
        if idx < len(pages):
            p = pages[idx]
            pid, ptok, pname = p.get("page_id"), p.get("page_token"), p.get("page_name", f"Saved Page {idx+1}")
            if pid and ptok: pages_to_post.append((pid, ptok, pname))
    else:
        from user_manager import get_user_facebook
        p, t = get_user_facebook(message.from_user.id)
        if p and t: pages_to_post.append((p, t, "Custom Page"))

    if not pages_to_post:
        msg_text = "⚠️ You haven't set up your Facebook credentials yet. Use `/setfb <page_id> <page_token>`."
        if status_msg: await status_msg.edit_text(msg_text)
        else: await message.reply_text(msg_text)
        return

    split_parts = message.text.split(maxsplit=text_start)
    text = split_parts[text_start] if len(split_parts) > text_start else ""
    media_path = None
    msg = status_msg
    
    if message.reply_to_message:
        target_msg = message.reply_to_message
        if target_msg.text:
            text = text or target_msg.text
            if not msg: msg = await message.reply_text("📤 Posting text to Facebook...")
            else: await msg.edit_text("📤 Posting text to Facebook...")
        elif target_msg.media:
            if not msg: msg = await message.reply_text("📥 Downloading media...")
            else: await msg.edit_text("📥 Downloading media...")
            try:
                media_path = await target_msg.download()
                if target_msg.caption and not text:
                    text = target_msg.caption
            except Exception as e:
                await msg.edit_text(f"❌ Failed to download media: {e}")
                return
            await msg.edit_text("📤 Posting media to Facebook...")
    elif not text:
        err = "⚠️ Usage: Reply to a message with `/postfb [optional text]`, or send `/postfb <text>`."
        if msg: await msg.edit_text(err)
        else: await message.reply_text(err)
        return
    else:
        if not msg: msg = await message.reply_text("📤 Posting text to Facebook...")
        else: await msg.edit_text("📤 Posting text to Facebook...")

    import re
    tags = None
    collaborators = None
    
    if text:
        tags_match = re.search(r'--tags\s+([\d,\s]+)(?=\s--|$)', text)
        if tags_match:
            tags = tags_match.group(1).strip()
            text = text.replace(tags_match.group(0), '')
            
        collabs_match = re.search(r'--collabs\s+([\d,\s]+)(?=\s--|$)', text)
        if collabs_match:
            collaborators = collabs_match.group(1).strip()
            text = text.replace(collabs_match.group(0), '')
            
        text = text.strip()

    if text and ("${title}" in text or "{title}" in text or "${" in text or "{" in text):
        try:
            from scheduler import format_fb_post_text
            orig_t = ""
            if message.reply_to_message:
                rep = message.reply_to_message
                if rep.video and rep.video.file_name:
                    orig_t = os.path.splitext(rep.video.file_name)[0]
                elif rep.document and rep.document.file_name:
                    orig_t = os.path.splitext(rep.document.file_name)[0]
                elif rep.caption:
                    orig_t = rep.caption.splitlines()[0]
            text = format_fb_post_text(text, original_title=orig_t, target_lang="km")
        except Exception as _fe:
            logger.warning(f"Failed to format FB post text: {_fe}")

    from facebook_util import post_to_facebook
    import asyncio
    
    results = []
    for p_id, p_token, p_name in pages_to_post:
        await msg.edit_text(f"📤 Posting to {p_name}...")
        is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, text, media_path, None, None, tags, collaborators)
        results.append((p_name, is_success, result))
    
    if media_path and os.path.exists(media_path):
        os.remove(media_path)
        
    res_text = "\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
    await msg.edit_text(f"Facebook Post Results:\n{res_text}")


@Client.on_message(filters.command("postfb"), group=0)
async def postfb_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import get_user_facebook_pages, get_user, AUTO_APPROVED_USERNAMES
    
    args = message.text.split()
    target = None
    text_start = 1
    
    u = get_user(message.from_user.id)
    is_super_admin = message.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    
    fb_access_list = []
    if u:
        access = u.get('fb_pages_access', [])
        if isinstance(access, bool):
            fb_access_list = ["aimovie", "livealone"] if access else []
        else:
            fb_access_list = access
            
    if len(args) > 1:
        requested_target = args[1].lower()
        if requested_target in ["aimovie", "livealone", "all", "all_system"]:
            if is_super_admin or (requested_target in fb_access_list) or (requested_target in ["all", "all_system"] and "aimovie" in fb_access_list and "livealone" in fb_access_list):
                target = requested_target
                text_start = 2
            else:
                await message.reply_text("⚠️ You do not have permission to post to this system page.")
                return
        elif requested_target.startswith("saved_") or requested_target == "all_saved" or requested_target == "custom":
            target = requested_target
            text_start = 2
            
    if target:
        await execute_fb_post(client, message, target, text_start, is_super_admin, fb_access_list)
    else:
        pages = get_user_facebook_pages(message.from_user.id)
        buttons = []
        
        if is_super_admin or ("aimovie" in fb_access_list and "livealone" in fb_access_list):
            buttons.append([InlineKeyboardButton("🌍 All System Pages", callback_data=f"fbpost|{message.id}|all")])
        if is_super_admin or "aimovie" in fb_access_list:
            buttons.append([InlineKeyboardButton("🎬 AI Movie Khmer", callback_data=f"fbpost|{message.id}|aimovie")])
        if is_super_admin or "livealone" in fb_access_list:
            buttons.append([InlineKeyboardButton("👤 LiveAlone", callback_data=f"fbpost|{message.id}|livealone")])
            
        if pages:
            if len(pages) > 1:
                buttons.append([InlineKeyboardButton("📢 All My Saved Pages", callback_data=f"fbpost|{message.id}|all_saved")])
            for i, p in enumerate(pages):
                p_name = p.get('page_name', f'Saved Page {i+1}')
                buttons.append([InlineKeyboardButton(f"📄 {p_name}", callback_data=f"fbpost|{message.id}|saved_{i}")])
                
        if not buttons:
            await message.reply_text("⚠️ You haven't set up any Facebook pages. Use `/setfb` or login to the Dashboard.")
            return
            
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="delete_message")])
        
        await message.reply_text(
            "📍 **Select Facebook Page to Post:**\n\nChoose where you want to publish this post:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

@Client.on_callback_query(filters.regex(r"^fbpost\|"), group=0)
async def handle_fbpost_callback(client: Client, callback_query: CallbackQuery):
    _, msg_id, target = callback_query.data.split("|")
    try:
        orig_msg = await client.get_messages(callback_query.message.chat.id, int(msg_id))
    except Exception:
        await callback_query.answer("⚠️ Original message not found.", show_alert=True)
        return
        
    if not orig_msg or orig_msg.empty:
        await callback_query.answer("⚠️ Original message not found.", show_alert=True)
        return
        
    await callback_query.message.edit_text("⏳ Processing your post request...")
    
    from user_manager import get_user, AUTO_APPROVED_USERNAMES
    u = get_user(callback_query.from_user.id)
    is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    
    fb_access_list = []
    if u:
        access = u.get('fb_pages_access', [])
        if isinstance(access, bool):
            fb_access_list = ["aimovie", "livealone"] if access else []
        else:
            fb_access_list = access
            
    await execute_fb_post(client, orig_msg, target, 1, is_super_admin, fb_access_list, status_msg=callback_query.message)

@Client.on_message(filters.command("mychats"), group=0)
async def mychats_command(client: Client, message: Message):
    """List all groups/channels where the bot is an admin."""
    if not await check_user_access(message): return
    if not known_admin_chats:
        await refresh_admin_chats(client)
    if not known_admin_chats:
        await safe_reply_text(message, "📭 **No admin chats found.**\n\nAdd the bot as an **admin** to a group or channel first, then try again.")
        return
    lines = ["📋 **Groups & Channels where I'm Admin:**\n"]
    for cid, info in known_admin_chats.items():
        icon = "📢" if info["type"] == "channel" else "👥"
        link = f"@{info['username']}" if info.get('username') else f"ID: `{cid}`"
        lines.append(f"{icon} **{info['title']}** — {link}")
    await safe_reply_text(message, "\n".join(lines))

@Client.on_message(filters.command("publish"), group=0)
async def publish_command(client: Client, message: Message):
    """Start publish flow — lets the user pick a chat and send a file+title."""
    if not await check_user_access(message, require_assigned_server=True): return
    if not known_admin_chats:
        await refresh_admin_chats(client)
    if not known_admin_chats:
        await safe_reply_text(message,
            "📭 **No admin chats found.**\n\n"
            "Make sure the bot is an **Admin** in at least one group or channel, then use /mychats to verify."
        )
        return

    # Build inline keyboard: each chat is a button
    buttons = []
    for cid, info in known_admin_chats.items():
        icon = "📢" if info["type"] == "channel" else "👥"
        label = f"{icon} {info['title'][:30]}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pub_select|{cid}")])
    buttons.append([InlineKeyboardButton("🔄 Refresh Chat List", callback_data="pub_refresh")])
    keyboard = InlineKeyboardMarkup(buttons)
    await safe_reply_text(message,
        "📤 **Publish to Group / Channel**\n\n"
        "Select the destination where you want to publish:",
        reply_markup=keyboard
    )

# Automatic URL & Intent Detector
@Client.on_message(filters.text & filters.private & ~filters.me & ~filters.service & ~filters.command(["download", "convert", "start", "help", "ask", "image", "search", "model", "reset", "clear", "timezone", "publish", "mychats", "login", "dashboard", "howtouse", "guide", "tgdownload", "library", "setfb", "fbsync", "syncfb", "checkfb", "postfb", "allfb"]) & is_allowed_bot, group=0)
async def auto_url_and_menu_handler(client, message):
    if message.from_user and message.from_user.is_bot: return
    if not await check_user_access(message): return
    text = message.text.strip()
    if text.startswith("⏱ [") or "Thinking" in text or "analyzing" in text: return

    # ---- Hongguo custom merge number intercept ----
    chat_id = message.chat.id
    if chat_id in _pending_hg_custom_merge:
        pending = _pending_hg_custom_merge.pop(chat_id)
        if text.isdigit() and int(text) > 0:
            parts = int(text)
            short_id = pending.get("short_id")
            mode = pending.get("mode", "all")
            url = url_cache.get(short_id)
            if url:
                try:
                    series = await asyncio.to_thread(_fetch_hongguo_series, url)
                    if mode == "all":
                        episodes = series.get("episodes", [])
                    else:
                        sess = _hg_select_sessions.get(short_id)
                        episodes = [ep for ep in series.get("episodes", []) if str(ep["vid"]) in (sess["selected"] if sess else set())]

                    if not episodes:
                        await safe_reply_text(message, "❌ No episodes found to download.")
                        message.stop_propagation()
                        return

                    proc_msg = await safe_reply_text(
                        message,
                        f"🎬 **Downloading & Merging into {parts} Videos**\n"
                        f"📺 **{series.get('name', 'Hongguo series')}** ({len(episodes)} Episodes)\n"
                        f"⏳ Initializing download & merge sequence..."
                    )
                    await _stream_download_and_upload_hongguo(client, chat_id, proc_msg, series, episodes, merge_parts=parts)
                    message.stop_propagation()
                    return
                except Exception as exc:
                    await safe_reply_text(message, f"❌ Download error: {exc}")
                    message.stop_propagation()
                    return
            else:
                await safe_reply_text(message, "⚠️ Session expired. Please send the link again.")
                message.stop_propagation()
                return

    # ---- Login flow intercept ----
    user_id = message.from_user.id if message.from_user else None
    if user_id and user_id in login_states:
        state = login_states[user_id]
        from user_manager import update_user_session
        try:
            if state.get("step") == "phone":
                await safe_reply_text(message, "⏳ Sending login code...")
                uc = state.get("client")
                # text is the phone number
                phone_number = text.strip()
                try:
                    sent_code = await uc.send_code(phone_number)
                    state["step"] = "phone_code"
                    state["phone"] = phone_number
                    state["phone_code_hash"] = sent_code.phone_code_hash
                    await safe_reply_text(message, "✉️ **Code sent!**\n\nPlease check your other Telegram devices for the login code and reply with it here.")
                except Exception as e:
                    raise e
            elif state.get("step") == "phone_code":
                await safe_reply_text(message, "⏳ Logging in...")
                uc = state.get("client")
                phone_number = state.get("phone")
                phone_code_hash = state.get("phone_code_hash")
                
                try:
                    await uc.sign_in(phone_number, phone_code_hash, text)
                    session_str = await uc.export_session_string()
                    update_user_session(user_id, session_str)
                    await uc.stop()
                    login_states.pop(user_id, None)
                    await safe_reply_text(message, "✅ Successfully logged in! Your account is now linked. You can download from your private channels now.")
                except Exception as e:
                    if "SessionPasswordNeeded" in str(type(e).__name__):
                        state["step"] = "password"
                        await safe_reply_text(message, "🔐 Your account has Two-Step Verification enabled. Please enter your password:")
                    else:
                        raise e
            elif state.get("step") == "password":
                await safe_reply_text(message, "⏳ Verifying password...")
                uc = state.get("client")
                await uc.check_password(text)
                session_str = await uc.export_session_string()
                update_user_session(user_id, session_str)
                await uc.stop()
                login_states.pop(user_id, None)
                await safe_reply_text(message, "✅ Successfully logged in! Your account is now linked.")
        except Exception as e:
            login_states.pop(user_id, None)
            try: await state.get("client").stop()
            except: pass
            await safe_reply_text(message, f"❌ Login failed: {e}\n\nPlease try /login again.")
        message.stop_propagation()
        return

    # ---- Publish pending: handle caption input ----
    user_id = message.from_user.id if message.from_user else None
    if user_id and user_id in publish_pending:
        state = publish_pending.get(user_id, {})
        if isinstance(state, dict) and state.get("step") == "await_caption":
            state["caption"] = text
            state["step"] = "await_dest"
            publish_pending[user_id] = state
            if not known_admin_chats:
                await refresh_admin_chats(client)
            if not known_admin_chats:
                await safe_reply_text(message, "📭 **No admin chats found.**\n\nAdd the bot as Admin to a group or channel first.")
                publish_pending.pop(user_id, None)
            else:
                keyboard = _build_pub_dest_keyboard(state["short_id"])
                await safe_reply_text(message,
                    f"✅ Caption saved: **\"{text[:80]}\"**\n\n"
                    "📤 **Now select the destination Group or Channel:**",
                    reply_markup=keyboard
                )
            message.stop_propagation()
            return
        elif state.get("waiting_for_topic_id") and text.isdigit():
            topic_id = int(text)
            target_chat_id = state.get("target_chat_id")
            short_id = state.get("short_id")
            caption = state.get("caption")
            publish_pending.pop(user_id, None)
            
            try:
                # We pass query_msg=None since it's a message response, not a callback
                await _do_publish(client, None, None, short_id, target_chat_id, caption, topic_id=topic_id)
                await safe_reply_text(message, f"✅ Published successfully to topic ID: {topic_id}!")
            except Exception as e:
                await safe_reply_text(message, f"❌ Failed to publish: {e}")
            message.stop_propagation()
            return
    # ---- End publish intercept ----

    # ---- Check if user is replying with custom movie title for thumbnail stylizer ----
    for p_sid, p_sess in list(_poster_sessions.items()):
        if p_sess.get("awaiting_custom_for") == message.chat.id:
            p_sess.pop("awaiting_custom_for", None)
            custom_title = text.strip()
            if custom_title:
                p_sess["variants_km"] = [custom_title]
                p_sess["variants_en"] = [custom_title]
                p_photo = p_sess.get("photo_path")
                if p_photo and os.path.exists(p_photo):
                    st_msg = await safe_reply_text(message, "🎨 **Rendering your custom title onto the thumbnail...**")
                    try:
                        from poster_styler import render_stylized_movie_title
                        c_style = p_sess.get("current_style", "gold_fantasy")
                        out_c = os.path.join(get_temp_dir(), f"styled_poster_{p_sid}_custom.jpg")
                        render_stylized_movie_title(p_photo, custom_title, output_path=out_c, style=c_style)
                        cap = (
                            f"🎬 **Redesigned Movie Thumbnail**\n"
                            f"🏷️ **Original Title:** `{p_sess.get('original_title')}`\n"
                            f"🌟 **Custom Title:** **{custom_title}**\n"
                            f"✨ **Visual Style:** 3D Gold Fantasy"
                        )
                        btn_c = [
                            [
                                InlineKeyboardButton("✨ 3D Gold", callback_data=f"pst_app|{p_sid}|km|gold_fantasy|0"),
                                InlineKeyboardButton("🔥 Fiery Red", callback_data=f"pst_app|{p_sid}|km|fire_action|0"),
                                InlineKeyboardButton("⚡ Silver Cyber", callback_data=f"pst_app|{p_sid}|km|silver_scifi|0"),
                            ],
                            [
                                InlineKeyboardButton("✏️ Custom Edit Title", callback_data=f"pst_edit|{p_sid}"),
                            ]
                        ]
                        await message.reply_photo(photo=out_c, caption=cap, reply_markup=InlineKeyboardMarkup(btn_c))
                        await st_msg.delete()
                    except Exception as exc:
                        await safe_edit_text(st_msg, f"❌ Failed to render custom title: {exc}")
                    message.stop_propagation()
                    return
    # ---- End title stylizer intercept ----

    # Reconstruct URLs broken by accidental newlines
    fixed_text = re.sub(r'(https?://[^\s\n]+)\n+([^\s\n]+)', r'\1\2', text)
    words = fixed_text.split()
    found_url = next((w for w in words if is_url(w)), None)

    if found_url:
        from downloader import classify_url, clean_youtube_url
        if classify_url(found_url) == 'youtube':
            found_url = clean_youtube_url(found_url)
        from plugins.iqiyi_downloader import _is_iqiyi_url as _is_iq_url, handle_iqiyi_url
        if _is_iq_url(found_url):
            await handle_iqiyi_url(client, message, found_url)
            return
        from plugins.wetv_downloader import _is_wetv_url, handle_wetv_url
        if _is_wetv_url(found_url):
            await handle_wetv_url(client, message, found_url)
            return
        if _is_hongguo_series_url(found_url):
            found_url = resolve_hongguo_url(found_url)
            try:
                series = await asyncio.to_thread(_fetch_hongguo_series, found_url)
                short_id = str(uuid.uuid4())[:8]
                url_cache[short_id] = found_url
                title = series.get("name", "Hongguo series")
                episode_count = series.get("episodeCount", len(series.get("episodes", [])))
                cover = series.get("cover", "")
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 Download All Episodes", callback_data=f"hg_series_action|{short_id}|all")],
                    [InlineKeyboardButton("☑️ Select Episodes", callback_data=f"hg_series_action|{short_id}|select")],
                    [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"hg_thumb|{short_id}")],
                ])
                caption = (
                    f"🎬 **{title}**\n"
                    f"📺 {episode_count} episodes\n\n"
                    "Choose how to download:"
                )
                sent_photo = False
                if cover:
                    try:
                        await client.send_photo(
                            chat_id=message.chat.id,
                            photo=cover,
                            caption=caption,
                            reply_markup=keyboard,
                        )
                        sent_photo = True
                    except Exception as pe:
                        print(f"[Hongguo] photo send failed: {pe}")
                if not sent_photo:
                    await safe_reply_text(message, caption, reply_markup=keyboard)
            except StopPropagation:
                raise
            except Exception as exc:
                print(f"[Hongguo] Fetch error: {exc}")
                await safe_reply_text(message, f"❌ Unable to read this Hongguo series: {exc}")
            message.stop_propagation()
            return

        short_id = str(uuid.uuid4())[:8]
        url_cache[short_id] = found_url
        user_comment = text.replace(found_url, "").strip().lower()

        if user_comment:
            if any(k in user_comment for k in ["recap", "summarize", "summary", "សង្ខេប", "解说"]):
                target_lang = detect_requested_language(text)
                processing_msg = await safe_reply_text(message, f"⏱ [00:00] 🧠 Auto-starting AI Video Recap ({target_lang.upper()}) for `{found_url}`...")
                cancel_id = f"recapauto_{processing_msg.id}"
                register_cancel_task(cancel_id)
                try:
                    async with RealtimeTimer(processing_msg, f"🧠 Generating Voiceover Recap ({target_lang.upper()})...", cancel_id=cancel_id) as timer:
                        def progress_cb(t):
                            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                            timer.update_text(t)
                        if is_telegram_link(found_url):
                            input_path = await download_telegram_post_media(client, found_url, False, progress_cb, requesting_user_id=message.from_user.id)
                        else:
                            dl_res = await asyncio.to_thread(download_media, found_url, False, progress_cb)
                            input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                        if input_path and os.path.exists(input_path):
                            from converter import recap_video_audio
                            recap_text, media_out = await asyncio.to_thread(recap_video_audio, input_path, target_lang, 'auto', True, True, progress_cb)
                            await safe_edit_text(processing_msg, recap_text)
                            if media_out and os.path.exists(media_out):
                                await client.send_video(chat_id=message.chat.id, video=media_out, caption=f"🎙 **Voiceover Recap Video ({target_lang.upper()})**", supports_streaming=True)
                                cleanup_file(media_out)
                            cleanup_file(input_path)
                            message.stop_propagation()
                            return
                except ProcessCancelledException:
                    if 'input_path' in locals() and input_path: cleanup_file(input_path)
                    await safe_edit_text(processing_msg, "🛑 **Recap process stopped by user!** ✅")
                    message.stop_propagation()
                    return
                finally:
                    unregister_cancel_task(cancel_id)

        from downloader import extract_link_info
        link_info = await asyncio.to_thread(extract_link_info, found_url)
        bot_username = getattr(getattr(client, 'me', None), 'username', None) or 'udom_ai_bot'

        if link_info:
            v_title = link_info.get('title') or "Video"
            v_title = v_title.replace("[", "(").replace("]", ")").replace("*", "").replace("`", "'")
            v_thumb = link_info.get('thumbnail')
            v_heights = link_info.get('heights') or [360, 480, 720, 1080]

            caption = f"**{v_title}**\n\n🔗 {found_url}\n\n@{bot_username}"

            quality_buttons = []
            target_resolutions = [360, 480, 720, 1080]
            for res in target_resolutions:
                if not v_heights or any(abs(h - res) <= 60 for h in v_heights):
                    quality_buttons.append(InlineKeyboardButton(f"{res}p", callback_data=f"dl_qual|{short_id}|{res}"))

            if not quality_buttons:
                quality_buttons = [
                    InlineKeyboardButton("360p", callback_data=f"dl_qual|{short_id}|360"),
                    InlineKeyboardButton("480p", callback_data=f"dl_qual|{short_id}|480"),
                    InlineKeyboardButton("720p", callback_data=f"dl_qual|{short_id}|720"),
                    InlineKeyboardButton("1080p", callback_data=f"dl_qual|{short_id}|1080"),
                ]

            keyboard_rows = [quality_buttons]
            keyboard_rows.append([
                InlineKeyboardButton("🎬 Best Video", callback_data=f"dl_qual|{short_id}|best"),
                InlineKeyboardButton("🎵 Audio", callback_data=f"dl_aud|{short_id}")
            ])
            keyboard_rows.append([
                InlineKeyboardButton("✂️ Clip Video", callback_data=f"url_show_clip|{short_id}"),
                InlineKeyboardButton("📝 AI Recap", callback_data=f"url_show_recap|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
            ])

            keyboard = InlineKeyboardMarkup(keyboard_rows)

            sent_photo = False
            if v_thumb:
                try:
                    await client.send_photo(
                        chat_id=message.chat.id,
                        photo=v_thumb,
                        caption=caption,
                        reply_markup=keyboard,
                        reply_to_message_id=message.id
                    )
                    sent_photo = True
                except Exception as e_p:
                    print(f"send_photo preview warning: {e_p}")

            if not sent_photo:
                await safe_reply_text(message, caption, reply_markup=keyboard)
        else:
            caption = f"🔗 **Link Detected:** `{found_url}`\n\n@{bot_username}"
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("360p", callback_data=f"dl_qual|{short_id}|360"),
                    InlineKeyboardButton("480p", callback_data=f"dl_qual|{short_id}|480"),
                    InlineKeyboardButton("720p", callback_data=f"dl_qual|{short_id}|720"),
                    InlineKeyboardButton("1080p", callback_data=f"dl_qual|{short_id}|1080"),
                ],
                [
                    InlineKeyboardButton("🎬 Best Video", callback_data=f"dl_qual|{short_id}|best"),
                    InlineKeyboardButton("🎵 Audio", callback_data=f"dl_aud|{short_id}")
                ],
                [
                    InlineKeyboardButton("✂️ Clip Video", callback_data=f"url_show_clip|{short_id}"),
                    InlineKeyboardButton("📝 AI Recap", callback_data=f"url_show_recap|{short_id}"),
                    InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
                ]
            ])
            await safe_reply_text(message, caption, reply_markup=keyboard)

        message.stop_propagation()
        return

    if text.lower() in ["menu", "help", "start", "options", "commands"]:
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        welcome_message = f"👋 Welcome to the **{bot_name}**!\n\nTo see all available commands, tap the Menu button or use the buttons below."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛠 Commands (Help)", callback_data="show_help")],
            [InlineKeyboardButton("ℹ️ About", callback_data="show_about")]
        ])
        await safe_reply_text(message, welcome_message, reply_markup=keyboard)
        message.stop_propagation()

# General Direct Chat AI Handler
@Client.on_message((filters.text | filters.photo | filters.video | filters.audio | filters.voice | filters.document) & ~filters.me & ~filters.service & ~filters.command(["start", "help", "ask", "search", "image", "download", "convert", "model", "reset", "clear", "timezone", "publish", "mychats", "login", "dashboard", "howtouse", "guide", "tgdownload", "library", "setfb", "fbsync", "syncfb", "checkfb", "postfb", "allfb"]) & is_allowed_bot, group=1)
async def private_ai_chat(client: Client, message: Message):
    # Skip messages sent by bots (including the bot itself posting to channels/groups)
    if message.from_user and message.from_user.is_bot: return
    # Skip channel posts sent anonymously by the bot (sender_chat present, no from_user)
    if message.sender_chat and not message.from_user: return
    
    text = message.text or message.caption
    
    if message.chat.type != ChatType.PRIVATE and not (message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.is_self):
        bot_user = await client.get_me()
        if bot_user.username and text and f"@{bot_user.username}" not in text:
            return

    if not await check_user_access(message, require_assigned_server=True): return
    if text and text.startswith('/'): return
    if not text or text.startswith("⏱ [") or "Thinking" in text or "analyzing" in text or text in ["📥 Download Media", "🔄 Convert Media", "ℹ️ Help"]:
        return

    prompt = re.sub(r'⏱\s*\[\d+:\d+\]\s*🤔\s*(Thinking|Udom is analyzing|Drawing|Searching).*?(\n|$)', '', text).strip()
    if not prompt: return

    # Check for direct image drawing request
    if is_explicit_image_request(text):
        processing_msg = await safe_reply_text(message, "⏱ [00:00] 🎨 Drawing photo with Udom AI...", reply_to_message_id=message.id)
        img_url = clean_and_generate_image_url(text)
        cancel_id = f"draw_{processing_msg.id}"
        register_cancel_task(cancel_id)
        try:
            async with RealtimeTimer(processing_msg, "🎨 Drawing photo with Udom AI", cancel_id=cancel_id):
                if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                success = await send_photo_robust(message, img_url, caption=f"🎨 `{text}`")
            if success:
                await processing_msg.delete()
                return
        except ProcessCancelledException:
            await safe_edit_text(processing_msg, "🛑 **Process stopped by user!** ✅")
            return
        except Exception as e:
            print(f"Direct drawing error: {e}")
        finally:
            unregister_cancel_task(cancel_id)

    processing_msg = await safe_reply_text(message, "⏱ [00:00] 🤔 Thinking...", reply_to_message_id=message.id)
    cancel_id = f"chat_{processing_msg.id}"
    register_cancel_task(cancel_id)
    try:
        combined_context = ""
        
        # 1. Automatic Media Context (Multimodal for all models)
        if message.photo or message.video or message.audio or message.voice or message.document:
            async with RealtimeTimer(processing_msg, "⏳ Examining attached media...", cancel_id=cancel_id) as timer:
                try:
                    def dl_progress(current, total):
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        timer.update_text(f"⏳ Downloading media... {current * 100 / max(total, 1):.1f}%")
                    dl_path = await message.download(progress=dl_progress)
                    if dl_path:
                        timer.update_text("⏳ AI is analyzing the media...")
                        mime_type = ""
                        if message.photo: mime_type = "image/jpeg"
                        elif message.video: mime_type = message.video.mime_type or "video/mp4"
                        elif message.audio or message.voice: mime_type = getattr(message.audio or message.voice, 'mime_type', "audio/mpeg")
                        elif message.document: mime_type = message.document.mime_type or ""
                        
                        mode = "explain" if message.photo else "to_text"
                        media_desc = await asyncio.to_thread(process_media_analysis, dl_path, mime_type, mode, "Extract all text and clearly describe the contents of this media.", "auto", "en")
                        if media_desc:
                            combined_context += f"\n[ATTACHED MEDIA CONTENT/DESCRIPTION]:\n{media_desc}\n"
                        cleanup_file(dl_path)
                except ProcessCancelledException:
                    raise
                except Exception as e:
                    print(f"Media analysis error in chat: {e}")

        # 2. Automatic Internet Search (Realtime context)
        search_keywords = ["news", "weather", "today", "now", "price", "stock", "exchange", "currency", "latest", "time in", "who is", "what is"]
        if any(keyword in prompt.lower() for keyword in search_keywords) and len(prompt) > 4:
            async with RealtimeTimer(processing_msg, "🔍 Searching the web...", cancel_id=cancel_id) as timer:
                try:
                    if DDGS is not None:
                        results = await asyncio.to_thread(lambda: DDGS().text(prompt, max_results=3))
                        if results:
                            combined_context += "\n[LIVE INTERNET SEARCH RESULTS]:\n" + "".join([f"- {r.get('title')}: {r.get('body')}\n" for r in results])
                except ProcessCancelledException:
                    raise
                except Exception as se:
                    print(f"Auto-search error: {se}")
                    
        async with RealtimeTimer(processing_msg, "🤔 Thinking", cancel_id=cancel_id):
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
            try:
                reply = await asyncio.wait_for(get_ai_response(message.chat.id, prompt, context=combined_context), timeout=180.0)
            except asyncio.TimeoutError:
                reply = "Sorry, the AI service took too long to respond. Please try asking again!"
            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
        await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=text)
    except ProcessCancelledException:
        await safe_edit_text(processing_msg, "🛑 **Process stopped by user!** ✅")
    except Exception as e:
        print(f"Error in private_ai_chat: {e}")
        await safe_edit_text(processing_msg, "Sorry, I am having trouble answering right now. Please try asking again!")
    finally:
        unregister_cancel_task(cancel_id)

def analyze_image_with_ai(image_path: str, user_prompt: str = "Extract all text and explain this image in detail.") -> str:
    """Analyzes images using Gemini 2.0 Flash Vision API for text extraction (OCR) and detailed explanation."""
    try:
        import base64
        with open(image_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        
        mime_type = "image/jpeg"
        ext = os.path.splitext(image_path)[1].lower()
        if ext == ".png": mime_type = "image/png"
        elif ext == ".webp": mime_type = "image/webp"

        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if gemini_api_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
            payload = {
                "contents": [{
                    "parts": [
                        {"text": user_prompt},
                        {"inlineData": {"mimeType": mime_type, "data": b64_data}}
                    ]
                }]
            }
            res = requests.post(url, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                return text

        # Fallback to Pollinations vision / OCR endpoint if Gemini is unavailable
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}}
                    ]
                }
            ],
            "model": "openai"
        }
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text

        return "Could not analyze image content."
    except Exception as e:
        print(f"Error in analyze_image_with_ai: {e}")
        return f"Error analyzing image: {e}"

def process_media_analysis(input_path: str, mime_type: str, mode: str = "to_text", custom_prompt: str = "", src_lang: str = "auto", target_lang: str = "km") -> str:
    """
    Handles image-to-text, video-to-text, audio-to-text and detailed explanations with multimodal support.
    mode options: 'to_text', 'explain'
    """
    is_img = mime_type.startswith("image/") or input_path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    is_vid = mime_type.startswith("video/") or input_path.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".webm"))
    is_aud = mime_type.startswith("audio/") or input_path.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac")) or "voice" in mime_type

    target_lang_names = {
        "km": "Khmer (ភាសាខ្មែរ)", "en": "English", "zh": "Chinese (中文)", 
        "ja": "Japanese (日本語)", "ko": "Korean (한국어)", "fr": "French", 
        "es": "Spanish", "vi": "Vietnamese", "th": "Thai", "de": "German", "ru": "Russian"
    }
    t_name = target_lang_names.get(target_lang, "Khmer (ភាសាខ្មែរ)")
    gemini_api_key = os.environ.get("GEMINI_API_KEY")

    if is_img:
        if mode == "to_text":
            prompt = custom_prompt or f"Extract all text inside this image word-for-word, and write the result clearly in {t_name}."
        else:
            prompt = custom_prompt or f"Explain this image in full detail in {t_name}, listing all key visual elements, text, and context."
        return analyze_image_with_ai(input_path, prompt)

    elif is_vid or is_aud:
        # First attempt: Gemini Multimodal Direct Analysis (Visual + Audio) if file <= 20MB
        if gemini_api_key and os.path.exists(input_path) and os.path.getsize(input_path) <= 20 * 1024 * 1024:
            try:
                import base64
                with open(input_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                
                v_mime = mime_type if (mime_type and "/" in mime_type) else ("video/mp4" if is_vid else "audio/mp3")
                
                if mode == "to_text":
                    vm_prompt = custom_prompt or f"Extract and write down all spoken speech and visual text in this {'video' if is_vid else 'audio'} clearly in {t_name}."
                else:
                    vm_prompt = custom_prompt or f"Provide a detailed, step-by-step explanation of this {'video' if is_vid else 'audio'} in {t_name}, describing what happens, key points, speech, and summary."

                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                payload = {
                    "contents": [{
                        "parts": [
                            {"text": vm_prompt},
                            {"inlineData": {"mimeType": v_mime, "data": b64_data}}
                        ]
                    }]
                }
                res = requests.post(url, json=payload, timeout=30)
                if res.status_code == 200:
                    data = res.json()
                    res_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    if res_text and len(res_text.strip()) > 0:
                        return res_text
            except Exception as e_vm:
                print(f"Gemini Multimodal Direct Analysis Error: {e_vm}")

        # Second attempt: Speech Recognition + AI Explanation
        transcript = transcribe_audio_video(input_path, src_lang=src_lang)
        if mode == "to_text" and not custom_prompt:
            if transcript and "Error" not in transcript and target_lang != src_lang and target_lang != "auto":
                tr_prompt = f"Translate the following spoken transcript accurately and write the full translated text clearly into {t_name}:\n\n{transcript}"
                try:
                    if gemini_api_key:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                        payload = {"contents": [{"parts": [{"text": tr_prompt}]}]}
                        res = requests.post(url, json=payload, timeout=25)
                        if res.status_code == 200:
                            data = res.json()
                            translated_text = data["candidates"][0]["content"]["parts"][0]["text"]
                            return f"📝 **Extracted & Written Text ({t_name}):**\n\n{translated_text}\n\n*(Original Spoken Transcript: {transcript})*"
                except Exception as e:
                    print(f"Translation Error: {e}")
            return f"📝 **Extracted Speech / Text Transcript:**\n\n{transcript}"
            
        explain_prompt = custom_prompt or f"Provide a comprehensive, easy-to-read explanation of this content in {t_name}, highlighting key points, main topics, and summary."
        ai_prompt = f"Content Transcript: {transcript or 'No spoken audio found'}\n\nTask: {explain_prompt}"
        
        try:
            if gemini_api_key:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
                payload = {"contents": [{"parts": [{"text": ai_prompt}]}]}
                res = requests.post(url, json=payload, timeout=25)
                if res.status_code == 200:
                    data = res.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            print(f"Error in GEMINI explanation: {e}")
            
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {"messages": [{"role": "user", "content": ai_prompt}], "model": "openai"}
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text

        return f"💡 **Explanation ({t_name}):**\n\n{transcript or 'Could not generate explanation for this media.'}"

    else:
        doc_text = parse_document(input_path, mime_type)
        if mode == "to_text" and not custom_prompt:
            return f"📄 **Extracted Document Text:**\n\n{doc_text}"
            
        explain_prompt = custom_prompt or "Explain this document in detail, summarizing the key topics and conclusions."
        ai_prompt = f"Document Content:\n\n{doc_text}\n\nTask: {explain_prompt}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        data = {"messages": [{"role": "user", "content": ai_prompt}], "model": "openai"}
        res_p = requests.post('https://text.pollinations.ai/', headers=headers, json=data, timeout=25)
        if res_p.status_code == 200:
            return res_p.text
            
        return f"📄 **Document Content:**\n\n{doc_text}"

async def execute_video_clipping(client, chat_id, processing_msg, input_source, clip_mode="num", value=3):
    """Executes video clipping for any input file or URL link and uploads all clips (clip_mode: 'num' or 'duration')."""
    if clip_mode == "duration":
        val_sec = int(value)
        mins = val_sec // 60
        secs = val_sec % 60
        dur_str = f"{mins} min" if mins > 0 and secs == 0 else f"{val_sec}s"
        status_txt = f"✂️ Downloading & cutting video into {dur_str} clips..."
    else:
        status_txt = f"✂️ Downloading & cutting video into {value} clips..."

    cancel_id = f"clip_{processing_msg.id}"
    register_cancel_task(cancel_id)
    try:
        async with RealtimeTimer(processing_msg, status_txt, cancel_id=cancel_id) as timer:
            def progress_cb(text):
                if is_task_cancelled(cancel_id):
                    raise ProcessCancelledException("Process stopped by user.")
                timer.update_text(text)
            try:
                is_temp_file = False
                input_path = None
                if isinstance(input_source, str) and input_source.startswith("http"):
                    is_temp_file = True
                    if is_telegram_link(input_source):
                        input_path = await download_telegram_post_media(client, input_source, False, progress_cb, requesting_user_id=callback_query.from_user.id)
                    else:
                        dl_res = await asyncio.to_thread(download_media, input_source, False, progress_cb)
                        input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                        if isinstance(dl_res, tuple) and len(dl_res) > 1 and isinstance(dl_res[1], dict):
                            dl_dur = dl_res[1].get('duration')
                            if dl_dur and isinstance(dl_dur, (int, float)) and dl_dur > 0:
                                known_dur_from_dl = float(dl_dur)
                elif isinstance(input_source, str) and os.path.exists(input_source):
                    input_path = input_source
                    is_temp_file = False
                elif hasattr(input_source, 'download'):
                    is_temp_file = True
                    def pyrogram_dl_progress(current, total):
                        if is_task_cancelled(cancel_id):
                            raise ProcessCancelledException("Process stopped by user.")
                        percent = current * 100 / total
                        timer.update_text(f"Downloading file... {percent:.1f}%")
                    try:
                        input_path = await input_source.download(progress=pyrogram_dl_progress)
                    except Exception as e_dl:
                        print(f"[execute_video_clipping] Message download error ({e_dl}), trying MTProto post link fallback...")
                        tg_link = getattr(input_source, 'link', None)
                        if tg_link:
                            input_path = await download_telegram_post_media(client, tg_link, False, progress_cb, requesting_user_id=callback_query.from_user.id)
                    
                if input_path and not isinstance(input_path, str):
                    input_path = None
                if input_path and input_path.startswith('ERROR:'):
                    await safe_edit_text(processing_msg, f"Failed to download: {input_path}")
                    return

                if input_path and os.path.exists(input_path):
                    # Ensure ffmpeg can probe duration: rename .media -> .mp4 if needed
                    if not any(input_path.lower().endswith(ext) for ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.ts', '.mp3', '.m4a', '.flac', '.wav']):
                        mp4_path = input_path + '.mp4'
                        try:
                            os.rename(input_path, mp4_path)
                            input_path = mp4_path
                        except Exception:
                            pass

                    from converter import clip_video_into_parts, clip_video_by_duration, get_video_duration
                    known_dur = getattr(getattr(input_source, 'video', None) or getattr(input_source, 'audio', None), 'duration', 0)
                    if known_dur <= 0 and 'known_dur_from_dl' in locals():
                        known_dur = known_dur_from_dl
                    if known_dur <= 0:
                        timer.update_text("Probing video duration...")
                        known_dur = await asyncio.to_thread(get_video_duration, input_path)
                    if clip_mode == "duration":
                        clips = await asyncio.to_thread(clip_video_by_duration, input_path, int(value), known_dur, progress_cb)
                    else:
                        clips = await asyncio.to_thread(clip_video_into_parts, input_path, int(value), known_dur, progress_cb)

                    if clips:
                        # Determine total number of clips to send
                        total_clips = (1 if clips.get('intro') else 0) + len(clips.get('main_clips', [])) + (1 if clips.get('outro') else 0)
                        timer.update_text(f"Uploading {total_clips} video clips...")

                        # Send intro clip if present
                        if clips.get('intro'):
                            try:
                                await client.send_video(
                                    chat_id=chat_id,
                                    video=clips['intro'],
                                    caption="🎬 **Intro Clip**",
                                    supports_streaming=True
                                )
                            except Exception as e_intro:
                                print(f"Error sending intro clip: {e_intro}")
                            finally:
                                cleanup_file(clips['intro'])

                        # Send main clips
                        main_clips = clips.get('main_clips', [])
                        for idx, clip_path in enumerate(main_clips, start=1):
                            if is_task_cancelled(cancel_id):
                                raise ProcessCancelledException("Process stopped by user.")
                            try:
                                await client.send_video(
                                    chat_id=chat_id,
                                    video=clip_path,
                                    caption=f"🎬 **Clip {idx} of {len(main_clips)}**",
                                    supports_streaming=True
                                )
                            except Exception as e_clip:
                                print(f"Error sending clip {idx}: {e_clip}")
                            finally:
                                cleanup_file(clip_path)

                        # Send outro clip if present
                        if clips.get('outro'):
                            try:
                                await client.send_video(
                                    chat_id=chat_id,
                                    video=clips['outro'],
                                    caption="🎬 **Outro Clip**",
                                    supports_streaming=True
                                )
                            except Exception as e_outro:
                                print(f"Error sending outro clip: {e_outro}")
                            finally:
                                cleanup_file(clips['outro'])

                        await safe_edit_text(processing_msg, f"Done! Sent {total_clips} video clips. ✅")
                    else:
                        await safe_edit_text(processing_msg, "❌ Failed to split video into clips.")
                    if is_temp_file:
                        cleanup_file(input_path)
                else:
                    await safe_edit_text(processing_msg, "❌ Could not retrieve video for clipping.")
            except ProcessCancelledException:
                if 'input_path' in locals() and input_path: cleanup_file(input_path)
                await safe_edit_text(processing_msg, "🛑 **Process stopped by user! Temporary files cleaned up.** ✅")
            except Exception as e:
                print(f"Video clipping error: {e}")
                await safe_edit_text(processing_msg, f"❌ Clipping failed: {e}")
    finally:
        unregister_cancel_task(cancel_id)

# Handle replies to ForceReply / ID prompt messages
@Client.on_message(filters.text & filters.reply & ~filters.me & ~filters.service, group=0)
async def handle_reply_prompts(client, message):
    if message.from_user and message.from_user.is_bot: return
    # Skip channel/group posts where the bot posted anonymously (no from_user)
    if message.sender_chat and not message.from_user: return
    replied = message.reply_to_message
    if not replied or not replied.text: return
    
    match = re.search(r'\[ID:([a-f0-9]+)\]', replied.text)
    if not match: return
    
    short_id = match.group(1)
    target_obj = url_cache.get(short_id)
    if not target_obj:
        await safe_reply_text(message, "Session expired. Please send the link or file again.")
        return
        
    user_text = message.text.strip()
    
    # Case 1: Video Clipping Prompt ("How many clips..." or "duration...")
    if "clip" in replied.text.lower() or "duration" in replied.text.lower():
        dur_match = re.search(r'(\d+)\s*(m|min|minute|minutes|s|sec|second|seconds)', user_text.lower())
        if dur_match:
            val = int(dur_match.group(1))
            unit = dur_match.group(2)
            seconds = val * 60 if unit.startswith(('m', 'min')) else val
            processing_msg = await safe_reply_text(message, f"⏱ [00:00] ✂️ Cutting video into {val} {unit} duration clips...")
            await execute_video_clipping(client, message.chat.id, processing_msg, target_obj, clip_mode="duration", value=seconds)
            message.stop_propagation()
            return
            
        num_match = re.search(r'\d+', user_text)
        if not num_match:
            await safe_reply_text(message, "Please provide a valid clip count (e.g. 3) or duration (e.g. 1 min, 2 min, 3 min, 5 min, 10 min).")
            return
        num_clips = max(1, min(100, int(num_match.group(0))))
        processing_msg = await safe_reply_text(message, f"⏱ [00:00] ✂️ Downloading & cutting video into {num_clips} clips...")
        await execute_video_clipping(client, message.chat.id, processing_msg, target_obj, clip_mode="num", value=num_clips)
        message.stop_propagation()
        return
        
    # Case 2: Ask AI about file or link
    if "ask udom about" in replied.text.lower():
        mime_type = ""
        if hasattr(target_obj, 'photo') and target_obj.photo: mime_type = "image/jpeg"
        elif hasattr(target_obj, 'video') and target_obj.video: mime_type = getattr(target_obj.video, 'mime_type', "video/mp4")
        elif hasattr(target_obj, 'audio') or hasattr(target_obj, 'voice'): mime_type = "audio/mpeg"
        elif hasattr(target_obj, 'document') and target_obj.document: mime_type = getattr(target_obj.document, 'mime_type', "")
        
        processing_msg = await safe_reply_text(message, "⏱ [00:00] 🔍 Processing AI task...")
        cancel_id = f"asktarget_{processing_msg.id}"
        register_cancel_task(cancel_id)
        try:
            async with RealtimeTimer(processing_msg, "🔍 Processing AI task", cancel_id=cancel_id) as timer:
                def progress_cb(t):
                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                    timer.update_text(t)
                if isinstance(target_obj, str) and target_obj.startswith("http"):
                    dl_res = await asyncio.to_thread(download_media, target_obj, False, progress_cb)
                    input_path = dl_res[0] if isinstance(dl_res, tuple) else dl_res
                else:
                    def pyrogram_dl_progress(current, total):
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        percent = current * 100 / total
                        timer.update_text(f"Downloading target... {percent:.1f}%")
                    input_path = await target_obj.download(progress=pyrogram_dl_progress)
                    
                if input_path and os.path.exists(input_path):
                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                    is_ocr_to_text = any(kw in user_text.lower() for kw in ["text", "ocr", "to text", "transcribe", "អានអក្សរ", "បកប្រែអក្សរ"])
                    mode = "to_text" if is_ocr_to_text else "explain"
                    vision_prompt = f"Please analyze this media and answer the user's request: '{user_text}'"
                    media_desc = await asyncio.to_thread(process_media_analysis, input_path, mime_type or "video/mp4", mode, vision_prompt, "auto", "en")
                    cleanup_file(input_path)
                    
                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                    combined_context = f"\n[ATTACHED MEDIA CONTENT/DESCRIPTION]:\n{media_desc}\n"
                    try:
                        reply = await asyncio.wait_for(get_ai_response(message.chat.id, user_text, context=combined_context), timeout=180.0)
                    except asyncio.TimeoutError:
                        reply = "Sorry, the AI service took too long to respond. Please try asking again!"
                    
                    if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                    await send_ai_reply_or_photo(message, processing_msg, reply, prompt_text=user_text)
                else:
                    await safe_edit_text(processing_msg, "❌ Could not download target for AI task.")
        except ProcessCancelledException:
            if 'input_path' in locals() and input_path: cleanup_file(input_path)
            await safe_edit_text(processing_msg, "🛑 **AI task stopped by user!** ✅")
        finally:
            unregister_cancel_task(cancel_id)
        message.stop_propagation()
        return

# Direct Media File Upload Handler
@Client.on_message((filters.photo | filters.video | filters.audio | filters.voice | filters.document) & ~filters.me & ~filters.service, group=0)
async def handle_media(client, message):
    if message.from_user and message.from_user.is_bot: return
    # Skip channel/group posts where the bot posted anonymously (no from_user)
    if message.sender_chat and not message.from_user: return
    if not await check_user_access(message): return

    # ---- Thumbnail pending: intercept photo upload as thumbnail ----
    if message.photo:
        user_id = message.from_user.id if message.from_user else None
        if user_id and user_id in thumb_pending:
            job = thumb_pending.get(user_id, {})
            if isinstance(job, dict) and job.get("step") == "await_thumb":
                thumb_pending.pop(user_id, None)
                # Download the photo to a temp file
                status_msg = await safe_reply_text(message, "⏳ Thumbnail received! Downloading and starting process...")
                try:
                    thumb_dl_path = await message.download()
                    if thumb_dl_path and os.path.exists(thumb_dl_path):
                        # Convert to JPEG to ensure FFmpeg compatibility
                        from utils import get_temp_dir as _get_tmp
                        import uuid as _uuid
                        thumb_jpeg = os.path.join(_get_tmp(), f"{_uuid.uuid4()}_thumb.jpg")
                        try:
                            from PIL import Image as _PILImage
                            img = _PILImage.open(thumb_dl_path).convert("RGB")
                            img.save(thumb_jpeg, "JPEG", quality=90)
                            cleanup_file(thumb_dl_path)
                        except Exception:
                            thumb_jpeg = thumb_dl_path
                        job["thumb_path"] = thumb_jpeg
                    else:
                        job["thumb_path"] = None
                        await safe_edit_text(status_msg, "⚠️ Could not save thumbnail — processing without it.")
                except Exception as e:
                    job["thumb_path"] = None
                    print(f"Thumbnail download error: {e}")
                await _ask_bgm(client, status_msg, job)
                message.stop_propagation()
                return
    # ---- End thumbnail intercept ----

    target_msg = message
    file_id, file_name, mime_type = None, "", ""
    if target_msg.photo:
        file_id, file_name, mime_type = target_msg.photo.file_id, "image.jpg", "image/jpeg"
    elif target_msg.video:
        file_id, file_name, mime_type = target_msg.video.file_id, target_msg.video.file_name or "video.mp4", target_msg.video.mime_type or "video/mp4"
    elif target_msg.audio or target_msg.voice:
        audio = target_msg.audio or target_msg.voice
        file_id, file_name, mime_type = audio.file_id, getattr(audio, 'file_name', "audio.mp3"), getattr(audio, 'mime_type', "audio/mpeg")
    elif target_msg.document:
        file_id, file_name, mime_type = target_msg.document.file_id, target_msg.document.file_name or "document", target_msg.document.mime_type or ""

    if file_id:
        short_id = str(uuid.uuid4())[:8]
        url_cache[short_id] = target_msg
        
        # Show primary action buttons including 📤 Publish & 🏷️ Translate Title
        buttons = [
            [
                InlineKeyboardButton("🔄 Convert", callback_data=f"file_show_conv|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"file_show_ask|{short_id}")
            ]
        ]
        if target_msg.photo or (target_msg.document and mime_type.startswith("image/")):
            buttons.append([
                InlineKeyboardButton("🏷️ Translate & Stylize Title", callback_data=f"pst_media|{short_id}")
            ])
        buttons.append([
            InlineKeyboardButton("📤 Publish to Group/Channel", callback_data=f"pub_file_start|{short_id}")
        ])
        keyboard = InlineKeyboardMarkup(buttons)
        await safe_reply_text(message, f"📁 **File Received:** `{file_name}`\nWhat would you like to do?", reply_markup=keyboard)
        message.stop_propagation()

# Language Keyboard Helpers for Dubbing & Recap
def build_source_language_keyboard(mode, short_id):
    back_cb = f"url_show_dl|{short_id}" if "url" in mode else f"file_show_conv|{short_id}"
    buttons = [
        [
            InlineKeyboardButton("🌐 Auto-Detect", callback_data=f"src_sel|{mode}|{short_id}|auto"),
            InlineKeyboardButton("🇰🇭 Khmer (ភាសាខ្មែរ)", callback_data=f"src_sel|{mode}|{short_id}|km"),
        ],
        [
            InlineKeyboardButton("🇬🇧 English", callback_data=f"src_sel|{mode}|{short_id}|en"),
            InlineKeyboardButton("🇨🇳 Chinese (中文)", callback_data=f"src_sel|{mode}|{short_id}|zh"),
        ],
        [
            InlineKeyboardButton("🇯🇵 Japanese", callback_data=f"src_sel|{mode}|{short_id}|ja"),
            InlineKeyboardButton("🇰🇷 Korean", callback_data=f"src_sel|{mode}|{short_id}|ko"),
        ],
        [
            InlineKeyboardButton("🇫🇷 French", callback_data=f"src_sel|{mode}|{short_id}|fr"),
            InlineKeyboardButton("🇪🇸 Spanish", callback_data=f"src_sel|{mode}|{short_id}|es"),
        ],
        [
            InlineKeyboardButton("🇻🇳 Vietnamese", callback_data=f"src_sel|{mode}|{short_id}|vi"),
            InlineKeyboardButton("🇹🇭 Thai", callback_data=f"src_sel|{mode}|{short_id}|th"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_cb)
        ]
    ]
    return InlineKeyboardMarkup(buttons)

def build_target_language_keyboard(mode, short_id, src_lang="auto"):
    if mode == "dub_file": cb_prefix = "dub_lang"
    elif mode == "recap_file": cb_prefix = "recap_file"
    elif mode == "dub_url": cb_prefix = "url_dub_lang"
    elif mode == "recap_url": cb_prefix = "recap_url"
    elif mode == "ocr_file": cb_prefix = "ocr_exec"
    elif mode == "exp_file": cb_prefix = "exp_exec"
    elif mode == "ocr_url": cb_prefix = "url_ocr_exec"
    elif mode == "exp_url": cb_prefix = "url_exp_exec"
    else: cb_prefix = "dub_lang"
    
    # Back button goes back to source language selection
    back_to_src_cb = f"url_show_dub|{short_id}" if "url" in mode else f"file_show_dub|{short_id}"

    buttons = [
        [
            InlineKeyboardButton("🇰🇭 Khmer (ភាសាខ្មែរ)", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|km"),
            InlineKeyboardButton("🇬🇧 English", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|en"),
        ],
        [
            InlineKeyboardButton("🇨🇳 Chinese (中文)", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|zh"),
            InlineKeyboardButton("🇯🇵 Japanese", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|ja"),
        ],
        [
            InlineKeyboardButton("🇰🇷 Korean", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|ko"),
            InlineKeyboardButton("🇫🇷 French", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|fr"),
        ],
        [
            InlineKeyboardButton("🇪🇸 Spanish", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|es"),
            InlineKeyboardButton("🇻🇳 Vietnamese", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|vi"),
        ],
        [
            InlineKeyboardButton("🇹🇭 Thai", callback_data=f"{cb_prefix}|{short_id}|{src_lang}|th"),
        ],
        [
            InlineKeyboardButton("🔙 Back", callback_data=back_to_src_cb)
        ]
    ]
    return InlineKeyboardMarkup(buttons)

# Callback Query Handler
@Client.on_callback_query(group=0)
async def button_callback(client, callback_query):
    if not await check_user_access(callback_query): return
    data = callback_query.data
    query_msg = callback_query.message

    # Fast ACK: Immediately acknowledge menu/navigation clicks so Telegram dismisses button spinners instantly
    fast_ack_prefixes = (
        "file_show_", "url_show_", "src_sel|", "dl_vid|", "dl_qual|", "dl_aud|",
        "pst_", "pub_", "thumb_", "bgm_", "clip_", "hg_sel_", "hg_tgl|", "hg_clr|",
        "show_how_to_use", "how_to_use_", "show_help", "show_about", "cb_model_list", "start_menu"
    )
    if any(data.startswith(p) for p in fast_ack_prefixes):
        try:
            await callback_query.answer()
        except Exception:
            pass

    if data.startswith("ml|"):
        if query_msg.chat.type != ChatType.PRIVATE:
            await callback_query.answer("Use the local media library in a private chat.", show_alert=True)
            return
        parts = data.split("|")
        if len(parts) != 4:
            await callback_query.answer("Invalid media-library action.", show_alert=True)
            return
        _, session_id, action, value = parts
        with media_library_sessions_lock:
            session = media_library_sessions.get(session_id)
            if (
                not session
                or session["expires_at"] <= time.time()
                or session["owner_id"] != callback_query.from_user.id
            ):
                media_library_sessions.pop(session_id, None)
                session = None
            if session:
                session["expires_at"] = time.time() + 1800
                names = list(session["names"])
                selected = set(session["selected"])
                page = session["page"]

        if not session:
            await callback_query.answer("This media-library session expired or belongs to another user.", show_alert=True)
            return

        if action == "toggle":
            try:
                index = int(value)
                if index < 0 or index >= len(names):
                    raise ValueError
            except ValueError:
                await callback_query.answer("Invalid episode.", show_alert=True)
                return
            if index in selected:
                selected.remove(index)
            else:
                selected.add(index)
            with media_library_sessions_lock:
                session["selected"] = selected
            keyboard, page = _media_library_keyboard(session_id, names, selected, page)
            with media_library_sessions_lock:
                session["page"] = page
            await callback_query.answer()
            await query_msg.edit_reply_markup(reply_markup=keyboard)
            return

        if action == "page":
            try:
                page = int(value)
            except ValueError:
                await callback_query.answer("Invalid page.", show_alert=True)
                return
            keyboard, page = _media_library_keyboard(session_id, names, selected, page)
            with media_library_sessions_lock:
                session["page"] = page
            await callback_query.answer()
            await query_msg.edit_reply_markup(reply_markup=keyboard)
            return

        if action == "thumb":
            try:
                index = int(value)
                if index < 0 or index >= len(names):
                    raise ValueError
                thumbnail = await asyncio.to_thread(resolve_thumbnail, names[index])
                if not thumbnail:
                    await callback_query.answer("No thumbnail is available for this episode.", show_alert=True)
                    return
            except (ValueError, FileNotFoundError) as exc:
                await callback_query.answer(str(exc) or "Thumbnail not found.", show_alert=True)
                return
            try:
                await callback_query.answer()
                await client.send_photo(
                    chat_id=query_msg.chat.id,
                    photo=thumbnail,
                    caption=f"Thumbnail: {names[index]}",
                    parse_mode=None,
                )
            except Exception as exc:
                print(f"Media-library thumbnail error: {exc}")
                await safe_reply_text(query_msg, "Unable to send this thumbnail.")
            return

        if action == "send":
            if value == "selected":
                indexes = sorted(selected)
                if not indexes:
                    await callback_query.answer("Select at least one episode first.", show_alert=True)
                    return
            elif value == "all":
                indexes = list(range(len(names)))
            else:
                await callback_query.answer("Invalid download action.", show_alert=True)
                return

            await callback_query.answer("Sending selected videos...")
            status_message = await client.send_message(
                chat_id=query_msg.chat.id,
                text=f"Sending {len(indexes)} video(s)...",
                parse_mode=None,
            )
            sent = 0
            failed = 0
            for index in indexes:
                name = names[index]
                try:
                    file_path = await asyncio.to_thread(resolve_episode, name)
                    extension = os.path.splitext(name)[1].lower()
                    for attempt in range(3):
                        try:
                            if extension in {".mp4", ".mov", ".m4v"}:
                                await client.send_video(
                                    chat_id=query_msg.chat.id,
                                    video=file_path,
                                    caption=name,
                                    supports_streaming=True,
                                    parse_mode=None,
                                )
                            else:
                                await client.send_document(
                                    chat_id=query_msg.chat.id,
                                    document=file_path,
                                    caption=name,
                                    parse_mode=None,
                                )
                            break
                        except FloodWait as exc:
                            if attempt == 2:
                                raise
                            await asyncio.sleep(float(getattr(exc, "value", 1) or 1))
                    sent += 1
                except Exception as exc:
                    failed += 1
                    print(f"Media-library send failed for {name}: {exc}")
            summary = f"Sent {sent} of {len(indexes)} video(s)."
            if failed:
                summary += f" {failed} file(s) could not be sent; check the bot log."
            await safe_edit_text(status_message, summary)
            return

        await callback_query.answer("Unknown media-library action.", show_alert=True)
        return

    if data.startswith("hg_series_action|"):
        _, short_id, action = data.split("|", 2)
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        try:
            series = await asyncio.to_thread(_fetch_hongguo_series, url)
        except Exception as exc:
            await safe_edit_text(query_msg, f"❌ Unable to load this Hongguo series: {exc}")
            return

        if action == "all":
            episodes = series.get("episodes", [])
            if not episodes:
                await safe_edit_text(query_msg, "❌ No episodes found for this series.")
                return
            if len(episodes) > 1:
                title = series.get("name", "Hongguo series")
                await safe_edit_text(
                    query_msg,
                    f"🎬 **{title}**\n📺 Total: **{len(episodes)} Episodes**\n\n"
                    f"❓ **តើអ្នកចង់ Merge វីដេអូបញ្ចូលគ្នាទេ?**\n"
                    f"(Do you want to merge videos into N parts?)",
                    reply_markup=_build_hongguo_merge_keyboard(short_id, "all")
                )
                return
            await callback_query.answer("Starting streaming download...", show_alert=False)
            processing_msg = await safe_edit_text(
                query_msg,
                f"🎬 **Downloading episode**\n"
                f"📺 **{series.get('name', 'Hongguo series')}**\n"
                "⏳ Initializing download..."
            )
            await _stream_download_and_upload_hongguo(client, query_msg.chat.id, processing_msg, series, episodes, merge_parts=1)
            return

        # Multi-episode selection view
        sess = _hg_select_sessions.setdefault(short_id, {"selected": set(), "page": 0, "series": series})
        sess["series"] = series
        episodes = series.get("episodes", [])
        kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], sess["page"])
        title = series.get("name", "Hongguo series")
        await safe_edit_text(
            query_msg,
            f"🎬 **{title}**\n📺 {len(episodes)} episodes\n\n☑️ **Select episodes to download** (tap to toggle):",
            reply_markup=kb,
        )
        return

    if data.startswith("hg_tgl|"):
        _, short_id, vid, page_str = data.split("|", 3)
        page = int(page_str) if page_str.isdigit() else 0
        sess = _hg_select_sessions.get(short_id)
        if not sess:
            url = url_cache.get(short_id)
            if url:
                try:
                    s_meta = await asyncio.to_thread(_fetch_hongguo_series, url)
                    sess = _hg_select_sessions.setdefault(short_id, {"selected": set(), "page": page, "series": s_meta})
                except Exception:
                    pass
        if not sess:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return

        vid_str = str(vid)
        if vid_str in sess["selected"]:
            sess["selected"].discard(vid_str)
        else:
            sess["selected"].add(vid_str)
        sess["page"] = page
        episodes = sess["series"].get("episodes", [])
        kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], page)
        try:
            await query_msg.edit_reply_markup(kb)
        except Exception:
            pass
        await callback_query.answer(f"Selected: {len(sess['selected'])}", show_alert=False)
        return

    if data.startswith("hg_sel_pg|"):
        _, short_id, page_str = data.split("|", 2)
        page = int(page_str) if page_str.isdigit() else 0
        sess = _hg_select_sessions.get(short_id)
        if sess:
            episodes = sess["series"].get("episodes", [])
            start_idx = page * 8
            for ep in episodes[start_idx:start_idx + 8]:
                sess["selected"].add(str(ep["vid"]))
            kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], page)
            try:
                await query_msg.edit_reply_markup(kb)
            except Exception:
                pass
            await callback_query.answer(f"Selected {len(sess['selected'])} episode(s)!")
        return

    if data.startswith("hg_sel_all|"):
        _, short_id, page_str = data.split("|", 2)
        page = int(page_str) if page_str.isdigit() else 0
        sess = _hg_select_sessions.get(short_id)
        if sess:
            episodes = sess["series"].get("episodes", [])
            for ep in episodes:
                sess["selected"].add(str(ep["vid"]))
            kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], page)
            try:
                await query_msg.edit_reply_markup(kb)
            except Exception:
                pass
            await callback_query.answer(f"All {len(episodes)} episodes selected!")
        return

    if data.startswith("hg_clr|"):
        _, short_id, page_str = data.split("|", 2)
        page = int(page_str) if page_str.isdigit() else 0
        sess = _hg_select_sessions.get(short_id)
        if sess:
            sess["selected"].clear()
            episodes = sess["series"].get("episodes", [])
            kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], page)
            try:
                await query_msg.edit_reply_markup(kb)
            except Exception:
                pass
            await callback_query.answer("Selection cleared.")
        return

    if data.startswith("hg_page|"):
        _, short_id, page_str = data.split("|", 2)
        page = int(page_str) if page_str.isdigit() else 0
        sess = _hg_select_sessions.get(short_id)
        if sess:
            sess["page"] = page
            episodes = sess["series"].get("episodes", [])
            kb = _build_hongguo_select_keyboard(short_id, episodes, sess["selected"], page)
            try:
                await query_msg.edit_reply_markup(kb)
            except Exception:
                pass
            await callback_query.answer()
        return

    if data.startswith("hg_dl_sel|"):
        _, short_id = data.split("|", 1)
        sess = _hg_select_sessions.get(short_id)
        if not sess or not sess["selected"]:
            await callback_query.answer("⚠️ Please select at least one episode first!", show_alert=True)
            return
        series = sess["series"]
        episodes = series.get("episodes", [])
        to_download = [ep for ep in episodes if str(ep["vid"]) in sess["selected"]]
        if not to_download:
            await callback_query.answer("⚠️ No matching episodes found in selection.", show_alert=True)
            return
        if len(to_download) > 1:
            title = series.get("name", "Hongguo series")
            await safe_edit_text(
                query_msg,
                f"🎬 **{title}**\n📺 Selected: **{len(to_download)} Episodes**\n\n"
                f"❓ **តើអ្នកចង់ Merge វីដេអូបញ្ចូលគ្នាទេ?**\n"
                f"(Do you want to merge videos into N parts?)",
                reply_markup=_build_hongguo_merge_keyboard(short_id, "sel")
            )
            return
        await callback_query.answer("Starting download...", show_alert=False)
        processing_msg = await safe_edit_text(
            query_msg,
            f"🎬 **Downloading 1 selected episode**\n"
            f"📺 **{series.get('name', 'Hongguo series')}**\n"
            "⏳ Initializing download..."
        )
        await _stream_download_and_upload_hongguo(client, query_msg.chat.id, processing_msg, series, to_download, merge_parts=1)
        return

    if data.startswith("hg_run_dl|"):
        _, short_id, mode, parts_str = data.split("|", 3)
        parts = int(parts_str) if parts_str.isdigit() else 1
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        try:
            series = await asyncio.to_thread(_fetch_hongguo_series, url)
        except Exception as exc:
            await safe_edit_text(query_msg, f"❌ Error loading series: {exc}")
            return

        if mode == "all":
            episodes = series.get("episodes", [])
        else:
            sess = _hg_select_sessions.get(short_id)
            episodes = [ep for ep in series.get("episodes", []) if str(ep["vid"]) in (sess["selected"] if sess else set())]

        if not episodes:
            await safe_edit_text(query_msg, "❌ No episodes to download.")
            return

        merge_desc = f"merged into {parts} parts" if parts > 1 else "individual episodes (1-by-1)"
        await callback_query.answer(f"Starting ({merge_desc})...", show_alert=False)
        proc_msg = await safe_edit_text(
            query_msg,
            f"🎬 **Downloading {len(episodes)} episodes** ({merge_desc})\n"
            f"📺 **{series.get('name', 'Hongguo series')}**\n"
            "⏳ Initializing download stream..."
        )
        await _stream_download_and_upload_hongguo(client, query_msg.chat.id, proc_msg, series, episodes, merge_parts=parts)
        return

    if data.startswith("hg_ask_custom|"):
        _, short_id, mode = data.split("|", 2)
        _pending_hg_custom_merge[query_msg.chat.id] = {
            "short_id": short_id,
            "mode": mode,
            "status_msg": query_msg
        }
        await callback_query.answer("Awaiting custom number...", show_alert=False)
        await safe_edit_text(
            query_msg,
            "🔢 **How many merged videos do you want?**\n\n"
            "👉 Please **reply with a number** in chat (e.g. `5` to divide episodes evenly into 5 videos):\n\n"
            "_Example: If series has 80 episodes and you type 5, it will create 5 videos with 16 episodes each._",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel / ថយក្រោយ", callback_data=f"hg_back|{short_id}")]])
        )
        return

    if data.startswith("hg_back|"):
        _, short_id = data.split("|", 1)
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired.", show_alert=True)
            return
        try:
            series = await asyncio.to_thread(_fetch_hongguo_series, url)
            title = series.get("name", "Hongguo series")
            episode_count = series.get("episodeCount", len(series.get("episodes", [])))
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Download All Episodes", callback_data=f"hg_series_action|{short_id}|all")],
                [InlineKeyboardButton("☑️ Select Episodes", callback_data=f"hg_series_action|{short_id}|select")],
                [InlineKeyboardButton("🖼 Thumbnail", callback_data=f"hg_thumb|{short_id}")],
            ])
            caption = (
                f"🎬 **{title}**\n"
                f"📺 {episode_count} episodes\n\n"
                "Choose how to download:"
            )
            await safe_edit_text(query_msg, caption, reply_markup=keyboard)
        except Exception as exc:
            await safe_edit_text(query_msg, f"❌ Error: {exc}")
        return

    if data.startswith("hg_dl_ep|"):
        _, short_id, vid, ep_no = data.split("|", 3)
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        try:
            series_meta = await asyncio.to_thread(_fetch_hongguo_series, url)
            series_name = series_meta.get("name", "Hongguo")
            series_id = str(series_meta.get("seriesId", ""))
            clean_name = re.sub(r'[\\/*?:"<>|]', "", series_name).strip()
            ep_num = int(ep_no) if str(ep_no).isdigit() else 1
            filename = f"{clean_name} - EP{ep_num:02d}.mp4"
            dest_path = os.path.join(get_temp_dir(), filename)

            processing_msg = await safe_edit_text(
                query_msg,
                f"🎬 **Downloading episode {ep_num}** ...\n📺 **{series_name}**\n⏳ Please wait..."
            )
            dl_res = await asyncio.to_thread(_download_single_hongguo_episode_sync, series_id, str(vid), dest_path, series_name)
            if not dl_res.get("ok") or not os.path.exists(dest_path):
                await safe_edit_text(processing_msg, f"❌ Episode {ep_num} could not be downloaded.")
                return

            await safe_edit_text(
                processing_msg,
                f"📤 **Uploading episode {ep_num}** ...\n📺 **{series_name}**"
            )
            await client.send_video(
                chat_id=query_msg.chat.id,
                video=dest_path,
                caption=f"🎬 **{series_name}** — EP{ep_num:02d}",
                file_name=filename,
                supports_streaming=True,
            )
            try:
                os.remove(dest_path)
            except Exception:
                pass
            await safe_edit_text(processing_msg, f"✅ Episode {ep_num} downloaded successfully from **{series_name}**.")
        except Exception as exc:
            await safe_edit_text(query_msg, f"❌ Failed to download episode {ep_no}: {exc}")
        return

    if data.startswith("hg_thumb|"):
        # Download & send the series cover thumbnail
        _, short_id = data.split("|", 1)
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired.", show_alert=True)
            return
        try:
            series_meta = await asyncio.to_thread(_fetch_hongguo_series, url)
            cover = series_meta.get("cover", "")
            if not cover:
                await callback_query.answer("No cover thumbnail available.", show_alert=True)
                return
            await client.send_photo(
                chat_id=query_msg.chat.id,
                photo=cover,
                caption=f"🖼 **{series_meta.get('name', 'Hongguo')}** — Series Thumbnail",
            )
            await callback_query.answer("Thumbnail sent!", show_alert=False)
        except Exception as exc:
            await callback_query.answer(f"Error: {exc}", show_alert=True)
        return

    if data.startswith("media_ocr_start|") or data.startswith("media_exp_start|"):
        parts = data.split("|")
        action, short_id = parts[0], parts[1]
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        mode = "ocr_file" if "ocr" in action else "exp_file"
        keyboard = build_source_language_keyboard(mode, short_id)
        action_title = "Media to Text (OCR & Transcription)" if "ocr" in action else "AI Media Explanation"
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃប្រព័ន្ធផ្សព្វផ្សាយ):**\n`{action_title}`", reply_markup=keyboard)

    elif data.startswith("ocr_exec|") or data.startswith("exp_exec|"):
        parts = data.split("|")
        exec_type, short_id, src_lang, target_lang = parts[0], parts[1], parts[2], parts[3]
        cached_msg = url_cache.get(short_id)
        if not cached_msg:
            await safe_edit_text(query_msg, "Session expired. Please send the file again.")
            return

        mode = "to_text" if exec_type == "ocr_exec" else "explain"
        action_title = "Extracting & Writing Text" if mode == "to_text" else "Analyzing & Explaining"
        cancel_id = f"media_{query_msg.id}"
        register_cancel_task(cancel_id)
        try:
            async with JobQueueContext(query_msg, "Media Analysis"):
                async with RealtimeTimer(query_msg, f"🔍 {action_title} ({target_lang.upper()})...", cancel_id=cancel_id) as timer:
                    try:
                        def pyrogram_download_progress(current, total):
                            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                            percent = current * 100 / total
                            timer.update_text(f"Downloading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                        input_path = await cached_msg.download(progress=pyrogram_download_progress)
                        timer.update_text(f"AI {action_title} ({target_lang.upper()})...")
                        
                        mime_type = ""
                        if cached_msg.photo: mime_type = "image/jpeg"
                        elif cached_msg.video: mime_type = cached_msg.video.mime_type or "video/mp4"
                        elif cached_msg.audio or cached_msg.voice: mime_type = getattr(cached_msg.audio or cached_msg.voice, 'mime_type', "audio/mpeg")
                        elif cached_msg.document: mime_type = cached_msg.document.mime_type or ""

                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        result_text = await asyncio.to_thread(process_media_analysis, input_path, mime_type, mode, "", src_lang, target_lang)
                        cleanup_file(input_path)
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        
                        if len(result_text) <= 4000:
                            await safe_edit_text(query_msg, result_text)
                        else:
                            chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                            await safe_edit_text(query_msg, chunks[0])
                            for chunk in chunks[1:]:
                                await query_msg.reply_text(chunk)
                    except ProcessCancelledException:
                        _inp = locals().get('input_path')
                        if _inp: cleanup_file(_inp)
                        await safe_edit_text(query_msg, "🛑 **Media analysis stopped by user!** ✅")
                    except Exception as e:
                        print(f"Media analysis callback error: {e}")
                        await safe_edit_text(query_msg, f"❌ Error analyzing media: {e}")
        finally:
            unregister_cancel_task(cancel_id)

    elif data == "show_how_to_use":
        text = "📖 **How to Use / របៀបប្រើប្រាស់ / 使用指南**\n\nPlease choose your language:\nសូមជ្រើសរើសភាសា:\n请选择您的语言:"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🇰🇭 ភាសាខ្មែរ (Khmer)", callback_data="how_to_use_km")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="how_to_use_en")],
            [InlineKeyboardButton("🇨🇳 中文 (Chinese)", callback_data="how_to_use_zh")],
            [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
        ])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_km":
        from user_manager import get_user, AUTO_APPROVED_USERNAMES
        u = get_user(callback_query.from_user.id)
        is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        is_main = client.name == "my_bot"
        text = (
            f"📖 **សៀវភៅណែនាំប្រើប្រាស់ {bot_name}** 🇰🇭\n\n"
            "📥 **1. ទាញយកវីដេអូ/ចម្រៀង (Download Media):**\n"
            "- ផ្ញើតំណភ្ជាប់ (Link) ពី YouTube, TikTok, Facebook, IG... រួចជ្រើសរើស 🎬 MP4 ឬ 🎵 MP3។\n\n"
            "📝 **2. បម្លែងប្រព័ន្ធផ្សព្វផ្សាយទៅជាអក្សរ (Media to Text / OCR):**\n"
            "- **រូបភាពទៅជាអក្សរ (OCR):** ផ្ញើរូបភាព រួចចុច `📝 Image to Text` ដើម្បីដកស្រង់អក្សរ។\n"
            "- **វីដេអូ/សំឡេងទៅជាអក្សរ:** ផ្ញើវីដេអូ ឬសំឡេង រួចចុច `📝 Video/Audio to Text` ដើម្បីបម្លែងសំឡេងនិយាយទៅជាអក្សរ។\n\n"
            "💡 **3. ពន្យល់ខ្លឹមសារ (Explain Image / Video / Audio):**\n"
            "- ចុច `💡 Explain Image/Video/Audio` ឬផ្ញើឯកសារជាមួយសារចំណងជើង (e.g. \"ពន្យល់រូបភាពនេះ\", \"explain this video\") ដើម្បីឱ្យ AI បកស្រាយលម្អិត។\n\n"
            "🎙 **4. បកប្រែសំឡេងនិយាយ (Voice Dubbing):**\n"
            "- ជ្រើសរើស `🎙 Voice Dub & Translate` ដើម្បីបកប្រែសំឡេងក្នុងវីដេអូជាភាសាខ្មែរ ភាសាអង់គ្លេស ចិន ជប៉ុន ផ្សេងៗ...\n\n"
            "✂️ **5. កាត់វីដេអូជាផ្នែក (Video Clipper):**\n"
            "- ជ្រើសរើស `✂️ Clip Video` ដើម្បីកាត់វីដេអូជា ២, ៣, ៥ ឬចំនួនភាគតាមតម្រូវការ។\n\n"
        )
        if is_main:
            text += (
                "📝 **6. AI សង្ខេបវីដេអូ (Video Recap):**\n"
                "- ជ្រើសរើស `📝 AI Video Recap` ដើម្បីទទួលបានអត្ថបទសង្ខេប និងវីដេអូអានសង្ខេបឡើងវិញ។\n\n"
                "💬 **7. ជជែកជាមួយ AI & ផ្លាស់ប្តូរ AI Model (`/model`):**\n"
                "- ប្រើបញ្ជា `/model` ដើម្បីជ្រើសរើស AI Model (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA)។\n"
                "- ប្រើបញ្ជា `/ask [សំណួរ]` ឬវាយសួរផ្ទាល់។\n\n"
                "🎨 **8. បង្កើតរូបភាព AI (`/image`):**\n"
                "- ប្រើបញ្ជា `/image [ការពិពណ៌នា]` ដើម្បីបង្កើតរូបភាពស្អាតៗកម្រិត 8K។\n\n"
                "🔍 **9. ស្វែងរកព័ត៌មានលើ Web (`/search`):**\n"
                "- ប្រើបញ្ជា `/search [ពាក្យស្វែងរក]` ដើម្បីស្វែងរកព័ត៌មានទាន់ហេតុការណ៍។\n\n"
            )
            text += (
                f"📱 **10. ផុសទៅកាន់ Facebook (`/postfb` & Dashboard):**\n"
                "- **ក្នុង Bot:** ប្រើបញ្ជា `/postfb [អត្ថបទ]` ឬ Reply ទៅកាន់វីដេអូ/រូបភាព។ អាចបន្ថែម `--tags` និង `--collabs`។\n"
                "- **Web Dashboard:** ចូលទៅកាន់ Dashboard ដើម្បីផុស ឬកំណត់កាលវិភាគសម្រាប់ (អត្ថបទ, រូបភាព, វីដេអូ, បន្ថែមចំណងជើង, និង Story)។\n"
                "- **ភ្ជាប់គណនី:** ប្រើបញ្ជា `/setfb <page_id> <page_token> [ឈ្មោះផេក]` ដើម្បីភ្ជាប់ Facebook Page របស់អ្នក។"
            )
        else:
            text += (
                f"📱 **6. ផុសទៅកាន់ Facebook (`/postfb` & Dashboard):**\n"
                "- **ក្នុង Bot:** ប្រើបញ្ជា `/postfb [អត្ថបទ]` ឬ Reply ទៅកាន់វីដេអូ/រូបភាព។ អាចបន្ថែម `--tags` និង `--collabs`។\n"
                "- **Web Dashboard:** ចូលទៅកាន់ Dashboard ដើម្បីផុស ឬកំណត់កាលវិភាគសម្រាប់ (អត្ថបទ, រូបភាព, វីដេអូ, បន្ថែមចំណងជើង, និង Story)។\n"
                "- **ភ្ជាប់គណនី:** ប្រើបញ្ជា `/setfb <page_id> <page_token> [ឈ្មោះផេក]` ដើម្បីភ្ជាប់ Facebook Page របស់អ្នក។"
            )
        if is_super_admin:
            text += "\n\n👑 **Super Admin Commands:**\n- `/allfb` — មើលបញ្ជីអ្នកប្រើប្រាស់ដែលបានភ្ជាប់ Facebook Page"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_en":
        from user_manager import get_user, AUTO_APPROVED_USERNAMES
        u = get_user(callback_query.from_user.id)
        is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
        is_main = client.name == "my_bot"
        text = (
            "📖 **How to Use Udom AI Bot Guide** 🇬🇧\n\n"
            "📥 **1. Download Video & Audio:**\n"
            "- Send any link from YouTube, TikTok, Facebook, Instagram, WeTV, iQIYI, Hongguo... and choose 🎬 MP4 Video or 🎵 MP3 Audio.\n"
            "- **Drama Series (WeTV / iQIYI / Hongguo):** Send any series link or use `/wetv <url>` or `/iqiyi <url>` to pick and batch-download all episodes without login (or using VIP cookies).\n\n"
            "📝 **2. Media to Text (OCR & Transcription):**\n"
            "- **Image to Text (OCR):** Send an image and tap `📝 Image to Text` to extract visible text.\n"
            "- **Video / Audio to Text:** Send a video or audio file and tap `📝 Video/Audio to Text` to transcribe speech.\n\n"
            "💡 **3. Explain Media (Image / Video / Audio):**\n"
            "- Tap `💡 Explain Image/Video/Audio` or send media with a caption (e.g. \"explain this image\", \"explain this video\") for detailed AI explanations.\n\n"
            "🎙 **4. Voice Dubbing & Translation:**\n"
            "- Select `🎙 Voice Dub & Translate` to translate spoken video speech into Khmer, English, Chinese, Japanese, Korean, etc., with dubbed audio.\n\n"
            "✂️ **5. Smart Video Clipper:**\n"
            "- Select `✂️ Clip Video` to automatically split any long video or link into 2, 3, 5, or custom clip counts.\n\n"
        )
        if is_main:
            text += (
                "📝 **6. AI Video & Audio Recap:**\n"
                "- Select `📝 AI Video Recap` to receive detailed executive text summaries and voiceover recap videos.\n\n"
                "💬 **7. AI Chat & Model Switcher (`/model`):**\n"
                "- Type `/model` to choose from 12+ AI models (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA).\n"
                "- Type `/ask [question]` or chat directly.\n\n"
                "🎨 **8. AI Image Generation (`/image`):**\n"
                "- Type `/image [description]` to generate hyperrealistic 8K AI artwork.\n\n"
                "🔍 **9. Real-Time Web Search (`/search`):**\n"
                "- Type `/search [query]` to search live web data.\n\n"
            )
            text += (
                f"📱 **10. Post to Facebook (`/postfb` & Dashboard):**\n"
                "- **In-Bot:** Use `/postfb [text]` or reply to a video/photo. You can add `--tags` and `--collabs`.\n"
                "- **Web Dashboard:** Login to the dashboard to post or schedule (Text, Photo, Video, Add Title, and Story).\n"
                "- **Setup:** Use `/setfb <page_id> <page_token> [optional_page_name]` to connect your Facebook Page."
            )
        else:
            text += (
                f"📱 **6. Post to Facebook (`/postfb` & Dashboard):**\n"
                "- **In-Bot:** Use `/postfb [text]` or reply to a video/photo. You can add `--tags` and `--collabs`.\n"
                "- **Web Dashboard:** Login to the dashboard to post or schedule (Text, Photo, Video, Add Title, and Story).\n"
                "- **Setup:** Use `/setfb <page_id> <page_token> [optional_page_name]` to connect your Facebook Page."
            )
        if is_super_admin:
            text += "\n\n👑 **Super Admin Commands:**\n- `/allfb` — List all users with connected Facebook Pages."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "how_to_use_zh":
        from user_manager import get_user, AUTO_APPROVED_USERNAMES
        u = get_user(callback_query.from_user.id)
        is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
        
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        is_main = client.name == "my_bot"
        
        text = (
            f"📖 **{bot_name} 使用指南** 🇨🇳\n\n"
            "📥 **1. 下载视频与音频:**\n"
            "- 发送 YouTube, TikTok, Facebook, Instagram 等链接，选择 🎬 MP4 视频或 🎵 MP3 音频。\n\n"
            "📝 **2. 媒体转文字 (OCR 与语音转写):**\n"
            "- **图片转文字 (OCR):** 发送图片并点击 `📝 Image to Text` 提取所有文字。\n"
            "- **视频/音频转文字:** 发送视频或音频并点击 `📝 Video/Audio to Text` 转写语音。\n\n"
            "💡 **3. 智能解析媒体 (图片/视频/音频):**\n"
            "- 点击 `💡 Explain Image/Video/Audio` 或附带消息（如“解释这张图片”、“解析视频”）获取 AI 详细解读。\n\n"
            "🎙 **4. 语音配音与翻译 (Voice Dubbing):**\n"
            "- 选择 `🎙 Voice Dub & Translate` 将视频语音翻译为高棉语、英语、中文、日语、韩语等，并生成配音。\n\n"
            "✂️ **5. 智能视频剪辑:**\n"
            "- 选择 `✂️ Clip Video` 将长视频或链接分割为 2、3、5 段或自定义段数。\n\n"
        )
        if is_main:
            text += (
                "📝 **6. AI 视频与音频总结 (Recap):**\n"
                "- 选择 `📝 AI Video Recap` 获取详细文字总结及配音解说视频。\n\n"
                "💬 **7. AI 对话与模型切换 (`/model`):**\n"
                "- 输入 `/model` 切换 12+ 款 AI 模型 (Gemini 2.0 Flash, DeepSeek R1, GPT-5, Claude 4.6, MALAKOR, ASTRIA)。\n"
                "- 使用 `/ask [问题]` 或直接发送消息。\n\n"
                "🎨 **8. AI 图片生成 (`/image`):**\n"
                "- 输入 `/image [描述]` 生成 8K 高清超逼真艺术图片。\n\n"
                "🔍 **9. 实时网络搜索 (`/search`):**\n"
                "- 输入 `/search [关键词]` 获取最新网络资讯。\n\n"
            )
            text += (
                f"📱 **10. 发布到 Facebook (`/postfb` & Dashboard):**\n"
                "- **Bot 内:** 使用 `/postfb [文字]` 或回复视频/图片。可添加 `--tags` 和 `--collabs`。\n"
                "- **Web 控制台:** 登录 Dashboard 发布或定时发布 (支持文字, 图片, 视频, 添加标题和 Story)。\n"
                "- **绑定账户:** 使用 `/setfb <page_id> <page_token> [自定义主页名称]` 连接您的主页。"
            )
        else:
            text += (
                f"📱 **6. 发布到 Facebook (`/postfb` & Dashboard):**\n"
                "- **Bot 内:** 使用 `/postfb [文字]` 或回复视频/图片。可添加 `--tags` 和 `--collabs`。\n"
                "- **Web 控制台:** 登录 Dashboard 发布或定时发布 (支持文字, 图片, 视频, 添加标题和 Story)。\n"
                "- **绑定账户:** 使用 `/setfb <page_id> <page_token> [自定义主页名称]` 连接您的主页。"
            )
        if is_super_admin:
            text += "\n\n👑 **Super Admin Commands:**\n- `/allfb` — 查看所有已连接Facebook主页的用户。"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="show_how_to_use")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)
    elif data == "show_help":
        from user_manager import get_user, AUTO_APPROVED_USERNAMES
        u = get_user(callback_query.from_user.id)
        is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
        
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        is_main = client.name == "my_bot"
        
        help_text = (
            f"**🤖 {bot_name} — Commands**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        if is_main:
            help_text += (
                "**🧠 AI Chat**\n"
                "• `/ask [question]` — Ask AI anything\n"
                "• `/search [query]` — Web search + AI\n"
                "• `/image [prompt]` — Generate AI image\n"
                "• `/model` — Switch AI model\n"
                "• `/reset` — Clear chat memory\n\n"
            )
        help_text += (
            "**📥 Download**\n"
            "• `/download [link]` — Download video/audio\n"
            "• `/library` — Select local videos, download multiple/all, or send a thumbnail\n"
            "• Send any link directly — bot detects it\n\n"
            "**🛠 Video Tools** *(send a file or link first)*\n"
            "• `✂️ Clip Video` — Split into parts\n"
            "• `🔄 Convert` — Change video/audio format\n"
            "• `🎤 Voice Dub` — Translate + dub audio\n"
            "• `📝 Transcribe` — Speech to text\n\n"
            "**📢 Publish**\n"
            "• `/publish` — Post to group/channel\n"
            "• `/mychats` — List admin channels\n"
            "• `/setfb` — Connect Facebook page\n\n"
            "**ℹ️ Other**\n"
            "• `/howtouse` — Full usage guide\n"
            "• `/help` — This menu\n"
        )
        if is_main:
            help_text += "• `/timezone +7` — Set your timezone\n"
        if is_super_admin:
            help_text += "\n**👑 Super Admin**\n• `/allfb` — List all custom FB pages\n"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
        ])
        await safe_edit_text(query_msg, help_text, reply_markup=keyboard)
    elif data == "show_about":
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        is_main = client.name == "my_bot"
        
        about_text = (
            f"**ℹ️ About {bot_name}**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🤖 **Engine:** ASTRIA-UNIFIED AI\n"
            "🌐 **Languages:** Khmer 🇰🇭 · English 🇬🇧 · Chinese 🇨🇳 · All global\n"
        )
        if is_main:
            about_text += "📡 **AI Models:** LLaMA 3.3 70B, GPT-4o, Gemini 2.0, Gemma 3, DeepSeek, Claude\n\n"
        else:
            about_text += "\n"
            
        about_text += (
            "📅 **Platforms Supported:**\n"
            "  YouTube · TikTok · Facebook · Instagram\n"
            "  Dailymotion · Twitter/X · Twitch · Telegram\n\n"
            "🔐 **Privacy:** Files are auto-deleted after processing\n"
            "📞 **Contact:** @thengrithy\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )
        if is_main:
            about_text += "_Built with ❤️ for unlimited free AI access_"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, about_text, reply_markup=keyboard)

    elif data == "show_admin_dashboard":
        port = os.environ.get("PORT", "10000")
        dashboard_url = get_valid_dashboard_url() or f"http://localhost:{port}"
        admin_text = (
            "⚙️ **Open Udom Workflow**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"You can access the workflow dashboard here:\n"
            f"`{dashboard_url}`\n\n"
            "💡 _Use your Telegram User ID (from the bot menu) to login or request an OTP._"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Open Udom Workflow", url=dashboard_url)],
            [InlineKeyboardButton("🔙 Back", callback_data="start_menu")]
        ])
        await safe_edit_text(query_msg, admin_text, reply_markup=keyboard)

    elif data == "get_user_id":
        user_id = callback_query.from_user.id
        text = (
            "🆔 **Your Telegram User ID:**\n\n"
            f"`{user_id}`\n\n"
            "💡 _Use this ID to login or request an OTP for the Web Dashboard._"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, text, reply_markup=keyboard)

    elif data == "show_download_help":
        dl_text = (
            "📥 **How to Download Videos**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔗 **Method 1 — Just paste a link:**\n"
            "Send any video URL in the chat and I'll auto-detect it!\n\n"
            "📱 **Supported Sites:**\n"
            "  ▶️ YouTube (all resolutions + shorts)\n"
            "  🎵 TikTok (no watermark)\n"
            "  📸 Instagram (reels, posts, stories)\n"
            "  👤 Facebook (public videos)\n"
            "  🏃 Dailymotion\n"
            "  🐦 Twitter/X\n"
            "  📢 Telegram private channels*\n\n"
            "🎬 **Choose Quality:**\n"
            "  4K / 1080p / 720p / 480p / 360p / MP3\n\n"
            "⚙️ **Method 2 — Command:**\n"
            "`/download https://youtube.com/...`\n\n"
            "\u26a0\ufe0f *Telegram private channels require USER_SESSION setup"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, dl_text, reply_markup=keyboard)
    elif data == "cb_model_list":
        model_text = (
            "🤖 **Available AI Models**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🌟 **Auto** *(default)* — Picks fastest available\n"
            "⚡ **Gemini Flash-Lite** — Google's fastest model\n"
            "🤖 **Copilot GPT-4o** — Microsoft Copilot\n"
            "🌎 **Llama 3.3 70B** — Meta's open source giant\n"
            "📊 **DeepSeek R1** — Reasoning specialist\n"
            "🧐 **GPT-5** — OpenAI latest\n"
            "📚 **Claude Sonnet** — Anthropic's best\n"
            "💻 **DeepSeek V4** — Code specialist\n"
            "🤖 **Qwen 3.7 Coder** — Alibaba code model\n"
            "🎨 **FLUX** — AI image generation\n"
            "👿 **MALAKOR** — Dark rogue AI persona\n\n"
            "Use `/model` to switch models in chat!"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_menu")]])
        await safe_edit_text(query_msg, model_text, reply_markup=keyboard)
    elif data == "start_menu":
        user_name = getattr(getattr(callback_query, 'from_user', None), 'first_name', '') or 'there'
        bot_name = "Udom AI Bot"
        if getattr(client, "me", None): bot_name = client.me.first_name
        elif getattr(client, "name", None): bot_name = client.name
        
        is_main = client.name == "my_bot"
        
        if is_main:
            welcome_message = (
                f"👋 **Hello {user_name}! Welcome to {bot_name}**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🤖 **What I can do for you:**\n\n"
                "🧠 **AI Chat & Search** — Ask anything, search the web, generate images\n"
                "📥 **Download Media** — YouTube, TikTok, Facebook, Instagram, Telegram\n"
                "✂️ **Video Tools** — Clip, convert, dub, transcribe, add thumbnail\n"
                "📔 **Documents** — Summarize PDFs, Word, Excel files\n"
                "📢 **Publish** — Post media to your groups/channels\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 **Just send a link, file, or type a message to start!**"
            )
        else:
            welcome_message = (
                f"👋 **Hello {user_name}! Welcome to {bot_name}**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "🤖 **What I can do for you:**\n\n"
                "📥 **Download Media** — YouTube, TikTok, Facebook, Instagram, Telegram\n"
                "✂️ **Video Tools** — Clip, convert, dub, transcribe, add thumbnail\n"
                "📢 **Publish** — Post media to your groups/channels\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 **Just send a link or file to start!**"
            )
        if is_main:
            keyboard_buttons = [
                [
                    InlineKeyboardButton("📖 How to Use", callback_data="show_how_to_use"),
                    InlineKeyboardButton("🛠 Commands", callback_data="show_help"),
                ],
                [
                    InlineKeyboardButton("🤖 AI Models", callback_data="cb_model_list"),
                    InlineKeyboardButton("ℹ️ About Bot", callback_data="show_about"),
                ]
            ]
        else:
            keyboard_buttons = [
                [
                    InlineKeyboardButton("📖 How to Use", callback_data="show_how_to_use"),
                    InlineKeyboardButton("ℹ️ About Bot", callback_data="show_about"),
                ]
            ]
            
        from user_manager import get_system_config
        system_config = get_system_config()
        keyboard_buttons.append([InlineKeyboardButton("🆔 Get User ID", callback_data="get_user_id")])
            
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await safe_edit_text(query_msg, welcome_message, reply_markup=keyboard)

    elif data.startswith("file_show_main|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        file_name = "file"
        if original_msg.photo: file_name = "image.jpg"
        elif original_msg.video: file_name = original_msg.video.file_name or "video.mp4"
        elif original_msg.audio or original_msg.voice: file_name = getattr(original_msg.audio or original_msg.voice, 'file_name', "audio.mp3")
        elif original_msg.document: file_name = original_msg.document.file_name or "document"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Convert", callback_data=f"file_show_conv|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"file_show_ask|{short_id}")
            ],
            [
                InlineKeyboardButton("📤 Publish to Group/Channel", callback_data=f"pub_file_start|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"📁 **File Received:** `{file_name}`\nWhat would you like to do?", reply_markup=keyboard)

    elif data.startswith("file_show_conv|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return

        buttons = []
        if original_msg.photo:
            buttons = [
                [InlineKeyboardButton("📝 Image to Text (OCR)", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Image", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (PNG/JPG/WEBP)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        elif original_msg.video:
            buttons = [
                [InlineKeyboardButton("📝 Video to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Video", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"file_show_dub|{short_id}"), InlineKeyboardButton("✂️ Clip Video", callback_data=f"file_show_clip|{short_id}")],
                [InlineKeyboardButton("📝 AI Video Recap", callback_data=f"file_show_recap|{short_id}"), InlineKeyboardButton("🔄 Convert Format (MP4/MKV/MP3)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        elif original_msg.audio or original_msg.voice:
            buttons = [
                [InlineKeyboardButton("📝 Audio to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Audio", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"file_show_dub|{short_id}"), InlineKeyboardButton("📝 AI Audio Recap", callback_data=f"file_show_recap|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (MP3)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("📄 Document to Text", callback_data=f"media_ocr_start|{short_id}"), InlineKeyboardButton("💡 Explain Document", callback_data=f"media_exp_start|{short_id}")],
                [InlineKeyboardButton("🔄 Convert Format (PDF/DOCX/TXT)", callback_data=f"file_show_fmt|{short_id}")]
            ]
        
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"file_show_main|{short_id}")])
        keyboard = InlineKeyboardMarkup(buttons)
        await safe_edit_text(query_msg, "🔄 **Choose action / conversion type:**", reply_markup=keyboard)

    elif data.startswith("file_show_fmt|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return

        mime_type = ""
        if original_msg.photo: mime_type = "image/jpeg"
        elif original_msg.video: mime_type = original_msg.video.mime_type or "video/mp4"
        elif original_msg.audio or original_msg.voice: mime_type = getattr(original_msg.audio or original_msg.voice, 'mime_type', "audio/mpeg")
        elif original_msg.document: mime_type = original_msg.document.mime_type or ""

        buttons = []
        if mime_type.startswith('image/'):
            buttons = [
                [InlineKeyboardButton("PNG", callback_data=f"conv_img|{short_id}|png"), InlineKeyboardButton("JPG", callback_data=f"conv_img|{short_id}|jpg"), InlineKeyboardButton("WEBP", callback_data=f"conv_img|{short_id}|webp")]
            ]
        elif mime_type.startswith('video/'):
            buttons = [
                [InlineKeyboardButton("🎬 MP4", callback_data=f"conv_vid|{short_id}|mp4"), InlineKeyboardButton("🎬 MKV", callback_data=f"conv_vid|{short_id}|mkv"), InlineKeyboardButton("🎵 MP3", callback_data=f"conv_aud|{short_id}")]
            ]
        elif mime_type.startswith('audio/') or original_msg.voice:
            buttons = [
                [InlineKeyboardButton("🎵 MP3", callback_data=f"conv_aud|{short_id}")]
            ]
        else:
            buttons = [
                [InlineKeyboardButton("📄 PDF", callback_data=f"conv_doc|{short_id}|pdf"), InlineKeyboardButton("📝 DOCX", callback_data=f"conv_doc|{short_id}|docx"), InlineKeyboardButton("📄 TXT", callback_data=f"conv_doc|{short_id}|txt")]
            ]

        buttons.append([InlineKeyboardButton("🔙 Back", callback_data=f"file_show_conv|{short_id}")])
        keyboard = InlineKeyboardMarkup(buttons)
        await safe_edit_text(query_msg, "🔄 **Choose target format:**", reply_markup=keyboard)


    elif data.startswith("file_show_dub|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("dub_file", short_id)
        await safe_edit_text(query_msg, "🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**", reply_markup=keyboard)

    elif data.startswith("file_show_recap|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("recap_file", short_id)
        await safe_edit_text(query_msg, "🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**", reply_markup=keyboard)

    elif data.startswith("src_sel|"):
        parts = data.split("|")
        mode, short_id, src_lang = parts[1], parts[2], parts[3]
        keyboard = build_target_language_keyboard(mode, short_id, src_lang)
        src_label = "🌐 Auto-Detect" if src_lang == "auto" else src_lang.upper()
        action_title = "Voice Dubbing & Translation" if "dub" in mode else "AI Video Recap & Voiceover"
        await safe_edit_text(query_msg, f"🗣 **Source Language:** `{src_label}`\n🎯 **Step 2/2: Choose Target Language for {action_title}:**", reply_markup=keyboard)

    elif data.startswith("file_show_ask|"):
        _, short_id = data.split("|")
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        file_name = "file"
        if original_msg.photo: file_name = "image.jpg"
        elif original_msg.video: file_name = original_msg.video.file_name or "video.mp4"
        elif original_msg.audio or original_msg.voice: file_name = getattr(original_msg.audio or original_msg.voice, 'file_name', "audio.mp3")
        elif original_msg.document: file_name = original_msg.document.file_name or "document"

        await client.send_message(
            query_msg.chat.id,
            f"🤖 **Ask Udom about this file:** `{file_name}` [ID:{short_id}]\n\nPlease reply directly to this message with what you want Udom to do for you:",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("url_show_main|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📥 Download", callback_data=f"url_show_dl|{short_id}"),
                InlineKeyboardButton("🔍 Analyze Media", callback_data=f"url_show_analyze|{short_id}")
            ],
            [
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"url_show_ask|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"🔗 **Link Detected:** `{url}`\nWhat would you like to do?", reply_markup=keyboard)

    elif data.startswith("url_show_analyze|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📝 Video to Text", callback_data=f"url_ocr_start|{short_id}"),
                InlineKeyboardButton("💡 Explain Video", callback_data=f"url_exp_start|{short_id}")
            ],
            [
                InlineKeyboardButton("🎵 Audio to Text", callback_data=f"url_aud_ocr_start|{short_id}"),
                InlineKeyboardButton("💡 Explain Audio", callback_data=f"url_aud_exp_start|{short_id}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data=f"url_show_main|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"🔍 **Analyze Media from Link:**\n`{url}`\n\nChoose analysis type:", reply_markup=keyboard)

    elif data.startswith("url_ocr_start|") or data.startswith("url_exp_start|") or data.startswith("url_aud_ocr_start|") or data.startswith("url_aud_exp_start|"):
        parts = data.split("|")
        action, short_id = parts[0], parts[1]
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        mode = "ocr_url" if "ocr" in action else "exp_url"
        keyboard = build_source_language_keyboard(mode, short_id)
        action_title = "Video/Audio to Text" if "ocr" in action else "AI Media Explanation"
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language:**\n`{action_title}` for:\n`{url}`", reply_markup=keyboard)

    elif data.startswith("url_ocr_exec|") or data.startswith("url_exp_exec|"):
        parts = data.split("|")
        exec_type, short_id, src_lang, target_lang = parts[0], parts[1], parts[2], parts[3]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Session expired. Please send the link again.")
            return
        mode = "to_text" if exec_type == "url_ocr_exec" else "explain"
        action_title = "Extracting Text" if mode == "to_text" else "Analyzing & Explaining"
        cancel_id = f"urlexp_{query_msg.id}"
        register_cancel_task(cancel_id)
        try:
            async with JobQueueContext(query_msg, "Media Analysis"):
                async with RealtimeTimer(query_msg, f"⬇️ Downloading from link...", cancel_id=cancel_id) as timer:
                    def progress_cb(t):
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        timer.update_text(t)
                    try:
                        timer.update_text("⬇️ Downloading media from link...")
                        if is_telegram_link(url):
                            input_path = await download_telegram_post_media(client, url, False, progress_cb, requesting_user_id=callback_query.from_user.id)
                        else:
                            input_path = await asyncio.to_thread(download_media, url, False, progress_cb)
                        if not input_path or not os.path.exists(str(input_path)) or str(input_path).startswith('ERROR:'):
                            await safe_edit_text(query_msg, f"❌ Could not download media from link: {url}")
                            return
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        timer.update_text(f"🔍 {action_title}...")
                        mime_type = "video/mp4"
                        result_text = await asyncio.to_thread(process_media_analysis, input_path, mime_type, mode, "", src_lang, target_lang)
                        cleanup_file(input_path)
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        if len(result_text) <= 4000:
                            await safe_edit_text(query_msg, result_text)
                        else:
                            chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                            await safe_edit_text(query_msg, chunks[0])
                            for chunk in chunks[1:]:
                                await query_msg.reply_text(chunk)
                    except ProcessCancelledException:
                        _inp = locals().get('input_path')
                        if _inp: cleanup_file(_inp)
                        await safe_edit_text(query_msg, "🛑 **Link media analysis stopped by user!** ✅")
                    except Exception as e:
                        print(f"URL media analysis error: {e}")
                        await safe_edit_text(query_msg, f"❌ Error analyzing link media: {e}")
        finally:
            unregister_cancel_task(cancel_id)

    elif data.startswith("url_show_dl|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🎬 Download Video", callback_data=f"dl_vid|{short_id}"),
                InlineKeyboardButton("🎵 Download Audio", callback_data=f"dl_aud|{short_id}")
            ],
            [
                InlineKeyboardButton("🎙 Voice Dub & Translate", callback_data=f"url_show_dub|{short_id}"),
                InlineKeyboardButton("✂️ Clip Video", callback_data=f"url_show_clip|{short_id}")
            ],
            [
                InlineKeyboardButton("🌐 Direct Web Link", callback_data=f"yt_direct|{short_id}")
            ],
            [
                InlineKeyboardButton("📝 AI Video Recap", callback_data=f"url_show_recap|{short_id}")
            ],
            [
                InlineKeyboardButton("🔙 Back", callback_data=f"url_show_main|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"📥 **Download Options for:** `{url}`", reply_markup=keyboard)

    elif data.startswith("url_show_dub|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("dub_url", short_id)
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**\n`{url}`", reply_markup=keyboard)

    elif data.startswith("yt_direct|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        await safe_edit_text(query_msg, "⏳ Fetching direct link...")
        try:
            from pytubefix import YouTube
            # removed import os
            proxies = {"http": "http://127.0.0.1:1080", "https": "http://127.0.0.1:1080"} if os.environ.get('USE_VPN_PROXY') == 'true' else None
            yt = None
            last_p_err = None
            proxy_configs = [proxies, None] if proxies else [None]
            for p_conf in proxy_configs:
                if yt: break
                for client_str in ['IOS', 'TV', 'ANDROID', 'WEB_CREATOR', 'WEB', 'MWEB', 'ANDROID_VR']:
                    try:
                        _yt = YouTube(url, client=client_str, proxies=p_conf)
                        _ = _yt.streams
                        yt = _yt
                        break
                    except Exception as e:
                        last_p_err = e
            if not yt:
                raise Exception(f"All clients failed. Last error: {last_p_err}")
            
            fmt = yt.streams.get_highest_resolution()
            if fmt and getattr(fmt, 'url', None):
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Open / Download Video", url=fmt.url)], 
                    [InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}")]
                ])
                await safe_edit_text(query_msg, f"✅ **Direct link fetched:**\n`{yt.title}`\n\nClick the button below to stream or download directly from YouTube's servers.", reply_markup=kb)
            else:
                await safe_edit_text(query_msg, "❌ Could not find a direct stream link.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}")]]))
        except Exception as e:
            await safe_edit_text(query_msg, f"❌ Error fetching direct link: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}")]]))


    elif data.startswith("url_show_recap|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = build_source_language_keyboard("recap_url", short_id)
        await safe_edit_text(query_msg, f"🗣 **Step 1/2: Choose Source Language (ភាសាដើមនៃវីដេអូ/សំឡេង):**\n`{url}`", reply_markup=keyboard)

    elif data.startswith("pub_file_start|"):
        # Step 1: User tapped "Publish" from file menu. Ask about caption.
        _, short_id = data.split("|", 1)
        original_msg = url_cache.get(short_id)
        if not original_msg:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return
        await callback_query.answer("", show_alert=False)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, add caption/text", callback_data=f"pub_cap_yes|{short_id}"),
                InlineKeyboardButton("⬇️ No, skip", callback_data=f"pub_cap_no|{short_id}")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data=f"pub_cancel|{short_id}")]
        ])
        await safe_edit_text(query_msg,
            "📤 **Publish File**\n\n"
            "Do you want to add a **caption or title text** to this file when publishing?",
            reply_markup=keyboard
        )

    elif data.startswith("pub_cap_yes|"):
        # Step 2a: User wants to type a caption. Use ForceReply prompt.
        _, short_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        publish_pending[user_id] = {"short_id": short_id, "step": "await_caption", "caption": None, "target_chat_id": None, "topic_id": None}
        await callback_query.answer("", show_alert=False)
        await safe_edit_text(query_msg,
            "✏️ **Type your caption / title text below** and send it as a reply:"
        )
        await query_msg.reply_text(
            "✏️ Please type the caption/title you want to add to your file:",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("pub_cap_no|"):
        # Step 2b: No caption, jump straight to destination selection.
        _, short_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        if not known_admin_chats:
            await refresh_admin_chats(client)
        if not known_admin_chats:
            await callback_query.answer("No admin chats found.", show_alert=True)
            await safe_edit_text(query_msg, "📭 **No admin chats found.**\n\nAdd the bot as Admin to a group or channel first.")
            return
        await callback_query.answer("", show_alert=False)
        publish_pending[user_id] = {"short_id": short_id, "step": "await_dest", "caption": None, "target_chat_id": None, "topic_id": None}
        keyboard = _build_pub_dest_keyboard(short_id)
        await safe_edit_text(query_msg,
            "📤 **Select Destination Group or Channel:**",
            reply_markup=keyboard
        )

    elif data.startswith("pub_dest|"):
        # Step 3: User selected a destination chat. Check if it's a forum (has topics).
        parts = data.split("|")
        short_id, target_chat_id_str = parts[1], parts[2]
        try:
            target_chat_id = int(target_chat_id_str)
        except ValueError:
            await callback_query.answer("❌ Invalid chat.", show_alert=True)
            return
        user_id = callback_query.from_user.id
        state = publish_pending.get(user_id, {})
        caption = state.get("caption")
        state["target_chat_id"] = target_chat_id
        publish_pending[user_id] = state
        info = known_admin_chats.get(target_chat_id, {})
        icon = "📢" if info.get("type") == "channel" else "👥"
        chat_title = info.get("title", str(target_chat_id))
        await callback_query.answer("", show_alert=False)

        # Check if chat is a supergroup with forum topics enabled
        is_forum = info.get("is_forum", False)
        
        if is_forum:
            # Telegram Bots cannot natively list all forum topics. We must ask user to manually provide the Topic ID.
            state["waiting_for_topic_id"] = True
            publish_pending[user_id] = state
            
            buttons = [
                [InlineKeyboardButton("📌 Publish to General (Topic 1)", callback_data=f"pub_topic|{short_id}|{target_chat_id}|1")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"pub_cap_no|{short_id}")]
            ]
            keyboard = InlineKeyboardMarkup(buttons)
            await safe_edit_text(query_msg,
                f"📂 **{chat_title}** is a Forum with topics.\n\n"
                f"⚠️ Telegram Bots cannot automatically read a group's topic list.\n\n"
                f"**How to publish:**\n"
                f"1. Type the **Topic ID** (e.g., `12` or `2`) and send it to me as a message.\n"
                f"2. OR click below to publish to the General topic.",
                reply_markup=keyboard
            )
        else:
            # No topics — publish directly
            await _do_publish(client, query_msg, callback_query, short_id, target_chat_id, caption, topic_id=None)
            publish_pending.pop(user_id, None)

    elif data.startswith("pub_topic|"):
        # Step 4: User selected a specific forum topic. Publish now.
        parts = data.split("|")
        short_id, target_chat_id_str, topic_id_str = parts[1], parts[2], parts[3]
        try:
            target_chat_id = int(target_chat_id_str)
            topic_id = int(topic_id_str)
        except ValueError:
            await callback_query.answer("❌ Invalid data.", show_alert=True)
            return
        user_id = callback_query.from_user.id
        state = publish_pending.get(user_id, {})
        caption = state.get("caption")
        await callback_query.answer("", show_alert=False)
        await _do_publish(client, query_msg, callback_query, short_id, target_chat_id, caption, topic_id=topic_id)
        publish_pending.pop(user_id, None)

    elif data == "pub_refresh":
        await callback_query.answer("🔄 Refreshing chat list...", show_alert=False)
        await refresh_admin_chats(client)
        user_id = callback_query.from_user.id
        state = publish_pending.get(user_id, {})
        short_id = state.get("short_id", "")
        if not known_admin_chats:
            await safe_edit_text(query_msg, "📭 **No admin chats found.** Add the bot as an Admin to a group or channel first.")
            return
        keyboard = _build_pub_dest_keyboard(short_id)
        await safe_edit_text(query_msg,
            "📤 **Select Destination Group or Channel:**\n✅ Chat list refreshed!",
            reply_markup=keyboard
        )

    elif data.startswith("pub_cancel|") or data == "pub_cancel":
        short_id = data.split("|")[1] if "|" in data else ""
        user_id = callback_query.from_user.id
        publish_pending.pop(user_id, None)
        await callback_query.answer("❌ Cancelled.", show_alert=False)
        original_msg = url_cache.get(short_id)
        file_name = "file"
        if original_msg:
            if original_msg.photo: file_name = "image.jpg"
            elif original_msg.video: file_name = original_msg.video.file_name or "video.mp4"
            elif original_msg.audio or original_msg.voice: file_name = getattr(original_msg.audio or original_msg.voice, 'file_name', "audio.mp3")
            elif original_msg.document: file_name = original_msg.document.file_name or "document"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔄 Convert", callback_data=f"file_show_conv|{short_id}"),
                InlineKeyboardButton("🤖 Ask AI", callback_data=f"file_show_ask|{short_id}")
            ],
            [InlineKeyboardButton("📤 Publish to Group/Channel", callback_data=f"pub_file_start|{short_id}")]
        ])
        await safe_edit_text(query_msg, f"📁 **File Received:** `{file_name}`\nWhat would you like to do?", reply_markup=keyboard)

    elif data.startswith("cancel_proc|"):
        _, task_id = data.split("|")
        cancel_task_id(task_id)
        await callback_query.answer("🛑 Stopping process...", show_alert=True)
        await safe_edit_text(query_msg, "🛑 **Process cancellation requested by user! Cleaning up temporary files...**")

    elif data.startswith("hg_stop|"):
        _, cid = data.split("|", 1)
        cancel_task_id(cid)
        await callback_query.answer("🛑 Stopping Hongguo download...", show_alert=True)
        await safe_edit_text(query_msg, "🛑 **Stopping download... Please wait.**")

    elif data.startswith("sched_stop|"):
        _, tid = data.split("|", 1)
        try:
            import scheduler as sched_module
            sched_module.request_stop(tid)
        except Exception:
            pass
        await callback_query.answer("🛑 Stopping scheduled task...", show_alert=True)
        await safe_edit_text(query_msg, "🛑 **Scheduled task stop requested by user.**")


    elif data.startswith("file_show_clip|") or data.startswith("url_show_clip|"):
        is_url = data.startswith("url_show_clip|")
        _, short_id = data.split("|")
        target_obj = url_cache.get(short_id)
        if not target_obj:
            await callback_query.answer("Session expired. Please send the file or link again.", show_alert=True)
            return
        prefix = "url" if is_url else "file"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔢 Clip by Number of Parts", callback_data=f"{prefix}_clip_mode_num|{short_id}")
            ],
            [
                InlineKeyboardButton("⏱ Clip by Duration (1m, 2m, 3m, 5m, 10m)", callback_data=f"{prefix}_clip_mode_dur|{short_id}")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}" if is_url else f"file_show_conv|{short_id}")]
        ])
        title = f"✂️ **Video Clipper Menu** [ID:{short_id}]"
        if is_url: title += f"\n`{target_obj}`"
        msg_body = (
            f"{title}\n\nChoose clipping mode below:\n\n"
            f"1️⃣ **Clip by Number of Parts**: Split video into N equal clips (2, 3, 5, 10...).\n"
            f"2️⃣ **Clip by Duration**: Cut video into fixed duration chunks (1 min, 2 min, 3 min, 5 min, 10 min)."
        )
        await safe_edit_text(query_msg, msg_body, reply_markup=keyboard)

    elif data.startswith("file_clip_mode_num|") or data.startswith("url_clip_mode_num|"):
        is_url = data.startswith("url_clip_mode_num|")
        _, short_id = data.split("|")
        prefix = "url" if is_url else "file"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✂️ 2 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|2"),
                InlineKeyboardButton("✂️ 3 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|3"),
                InlineKeyboardButton("✂️ 4 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|4"),
                InlineKeyboardButton("✂️ 5 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|5")
            ],
            [
                InlineKeyboardButton("✂️ 6 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|6"),
                InlineKeyboardButton("✂️ 8 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|8"),
                InlineKeyboardButton("✂️ 10 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|10"),
                InlineKeyboardButton("✂️ 12 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|12")
            ],
            [
                InlineKeyboardButton("✂️ 15 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|15"),
                InlineKeyboardButton("✂️ 20 Clips", callback_data=f"clip_exec_num|{prefix}|{short_id}|20"),
                InlineKeyboardButton("⚙️ Custom Count", callback_data=f"{prefix}_clip_custom_num|{short_id}")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data=f"{prefix}_show_clip|{short_id}")]
        ])
        await safe_edit_text(query_msg, f"🔢 **Clip by Number of Parts** [ID:{short_id}]\n\nSelect preset clip count below or choose Custom:", reply_markup=keyboard)

    elif data.startswith("file_clip_mode_dur|") or data.startswith("url_clip_mode_dur|"):
        is_url = data.startswith("url_clip_mode_dur|")
        _, short_id = data.split("|")
        prefix = "url" if is_url else "file"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⏱ 1 Min", callback_data=f"clip_exec_dur|{prefix}|{short_id}|60"),
                InlineKeyboardButton("⏱ 2 Min", callback_data=f"clip_exec_dur|{prefix}|{short_id}|120"),
                InlineKeyboardButton("⏱ 3 Min", callback_data=f"clip_exec_dur|{prefix}|{short_id}|180")
            ],
            [
                InlineKeyboardButton("⏱ 5 Min", callback_data=f"clip_exec_dur|{prefix}|{short_id}|300"),
                InlineKeyboardButton("⏱ 10 Min", callback_data=f"clip_exec_dur|{prefix}|{short_id}|600"),
                InlineKeyboardButton("⚙️ Custom Duration", callback_data=f"{prefix}_clip_custom_dur|{short_id}")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data=f"{prefix}_show_clip|{short_id}")]
        ])
        await safe_edit_text(query_msg, f"⏱ **Clip by Duration** [ID:{short_id}]\n\nSelect preset clip duration below or choose Custom:", reply_markup=keyboard)

    elif data.startswith("clip_exec_num|"):
        _, prefix, short_id, num_str = data.split("|")
        target_obj = url_cache.get(short_id)
        if not target_obj:
            await callback_query.answer("Session expired. Please send again.", show_alert=True)
            return
        await execute_video_clipping(client, query_msg.chat.id, query_msg, target_obj, clip_mode="num", value=int(num_str))

    elif data.startswith("clip_exec_dur|"):
        _, prefix, short_id, sec_str = data.split("|")
        target_obj = url_cache.get(short_id)
        if not target_obj:
            await callback_query.answer("Session expired. Please send again.", show_alert=True)
            return
        await execute_video_clipping(client, query_msg.chat.id, query_msg, target_obj, clip_mode="duration", value=int(sec_str))

    elif data.startswith("file_clip_custom_num|") or data.startswith("url_clip_custom_num|"):
        is_url = data.startswith("url_clip_custom_num|")
        _, short_id = data.split("|")
        await client.send_message(
            query_msg.chat.id,
            f"✂️ **How many clips do you want from this video?** [ID:{short_id}]\n\nPlease reply directly to this message with any number (e.g. 2, 3, 5, 8, 10, 15, 20):",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("file_clip_custom_dur|") or data.startswith("url_clip_custom_dur|"):
        is_url = data.startswith("url_clip_custom_dur|")
        _, short_id = data.split("|")
        await client.send_message(
            query_msg.chat.id,
            f"⏱ **What clip duration do you want?** [ID:{short_id}]\n\nPlease reply directly to this message with a duration (e.g. 1 min, 2 min, 3 min, 5 min, 10 min, 45s, 90s):",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("url_show_ask|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        await client.send_message(
            query_msg.chat.id,
            f"🤖 **Ask Udom about this link:** `{url}`\n\nPlease reply directly to this message with what you want Udom to do for you:",
            reply_markup=ForceReply(selective=True)
        )

    elif data.startswith("dl_vid|"):
        _, short_id = data.split("|")
        url = url_cache.get(short_id)
        if not url:
            await callback_query.answer("Session expired. Please send the link again.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("360p", callback_data=f"dl_qual|{short_id}|360"),
                InlineKeyboardButton("480p", callback_data=f"dl_qual|{short_id}|480"),
                InlineKeyboardButton("720p", callback_data=f"dl_qual|{short_id}|720"),
                InlineKeyboardButton("1080p", callback_data=f"dl_qual|{short_id}|1080"),
            ],
            [
                InlineKeyboardButton("🎬 Best Video", callback_data=f"dl_qual|{short_id}|best"),
                InlineKeyboardButton("🔙 Back", callback_data=f"url_show_dl|{short_id}")
            ]
        ])
        await safe_edit_text(query_msg, f"🎥 **Choose Video Quality for:**\n`{url}`", reply_markup=keyboard)

    elif data.startswith("dl_qual|") or data.startswith("dl_aud|"):
        parts = data.split('|')
        max_height = None
        if data.startswith("dl_qual|"):
            short_id = parts[1]
            qual_str = parts[2]
            max_height = int(qual_str) if qual_str.isdigit() else None
            is_audio = False
        else:
            action = parts[0]
            short_id = parts[1]
            is_audio = (action == "dl_aud")

        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return
        from downloader import classify_url, clean_youtube_url
        if classify_url(url) == 'youtube':
            url = clean_youtube_url(url)
        cancel_id = f"dl_{query_msg.id}"
        register_cancel_task(cancel_id)

        try:
            async with JobQueueContext(query_msg, "Media Download"):
                async with RealtimeTimer(query_msg, "Downloading... Please wait.", cancel_id=cancel_id) as timer:
                    def progress_callback(text):
                        if is_task_cancelled(cancel_id):
                            raise ProcessCancelledException("Process stopped by user.")
                        timer.update_text(text)
                    try:
                        if is_telegram_link(url):
                            filepath = await download_telegram_post_media(client, url, is_audio, progress_callback, requesting_user_id=callback_query.from_user.id)
                        else:
                            filepath = await asyncio.to_thread(download_media, url, is_audio, progress_callback, max_height)
                    except Exception as e:
                        if isinstance(e, ProcessCancelledException): raise e
                        print(f"Download error: {e}")
                        filepath = None

                if filepath == 'TOO_LARGE':
                    await safe_edit_text(query_msg, "❌ File is too large to send via Telegram (limit is 1.95GB).")
                elif filepath == 'BOT_DETECTED':
                    from user_manager import get_user, AUTO_APPROVED_USERNAMES
                    u = get_user(callback_query.from_user.id)
                    is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
                    
                    bot_msg = (
                        "⚠️ **YouTube Bot-Guard Blocked This Download**\n\n"
                        "YouTube is blocking this server's IP. All fallback methods (TV, iOS, Android, Invidious proxy) also failed."
                    )
                    
                    if is_super_admin:
                        bot_msg += (
                            "\n\n**Fix for Render/Server deployment:**\n\n"
                            "**Option 1 — OAuth2 Token** *(most reliable, permanent)*\n"
                            "Run once on your PC:\n"
                            "`python generate_oauth2_token.py`\n"
                            "Then paste the output into Render → Environment → `YT_OAUTH2_TOKEN`\n\n"
                            "**Option 2 — Fresh Cookies**\n"
                            "1. Install [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) in Chrome\n"
                            "2. Log in to YouTube → export cookies\n"
                            "3. Update `COOKIES_YOUTUBE` in Render env vars with the cookie file contents\n\n"
                            "**Option 3 — Run the bot locally** on your PC instead of Render"
                        )
                    
                    await safe_edit_text(query_msg, bot_msg)
                elif isinstance(filepath, str) and filepath.startswith('ERROR:'):
                    await safe_edit_text(query_msg, f"❌ {filepath}")
                elif filepath and os.path.exists(filepath):
                    timer.update_text("Download complete! Uploading...")
                    try:
                        up_start_time = time.time()
                        def pyrogram_upload_progress(current, total):
                            if is_task_cancelled(cancel_id):
                                raise ProcessCancelledException("Process stopped by user.")
                            elapsed = time.time() - up_start_time
                            speed = (current / elapsed) if elapsed > 0 else 0
                            eta = ((total - current) / speed) if speed > 0 else 0
                            dl_mb = current / 1024 / 1024
                            tot_mb = total / 1024 / 1024
                            spd_mb = speed / 1024 / 1024
                            timer.update_text(f"{dl_mb:.1f}MB/{tot_mb:.1f}MB, ETA {eta:.0f}s, {spd_mb:.1f}MB/s Sending...")
                        if is_audio:
                            await client.send_audio(chat_id=query_msg.chat.id, audio=filepath, progress=pyrogram_upload_progress)
                        else:
                            await client.send_video(chat_id=query_msg.chat.id, video=filepath, supports_streaming=True, progress=pyrogram_upload_progress)
                        await safe_edit_text(query_msg, "Done! ✅")
                    except Exception as e:
                        if isinstance(e, ProcessCancelledException): raise e
                        print(f"Upload failed: {e}")
                        await safe_edit_text(query_msg, f"❌ Upload failed: {e}")
                    finally:
                        cleanup_file(filepath)
        except ProcessCancelledException:
            await safe_edit_text(query_msg, "🛑 **Download stopped by user!** ✅")
        finally:
            unregister_cancel_task(cancel_id)

    elif data.startswith("url_dub_lang|"):
        parts = data.split("|")
        short_id = parts[1]
        src_lang = parts[2] if len(parts) >= 4 else 'auto'
        target_lang = parts[3] if len(parts) >= 4 else parts[2]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return
        from downloader import classify_url, clean_youtube_url
        if classify_url(url) == 'youtube':
            url = clean_youtube_url(url)
        settings = get_user_voice_settings(query_msg.chat.id)
        await callback_query.answer("", show_alert=False)
        job = {
            "action": "dub_url", "short_id": short_id, "url": url,
            "src_lang": src_lang, "target_lang": target_lang,
            "is_video": True, "thumb_path": None,
            "tts_backend": settings["tts_backend"],
            "whisper_model": settings["whisper_model"],
            "transcribe_engine": settings.get("transcribe_engine"),
            "translate_engine": settings.get("translate_engine"),
            "voxcpm_model": settings.get("voxcpm_model"),
            "query_msg_id": query_msg.id, "query_msg_chat_id": query_msg.chat.id
        }
        await _ask_thumbnail(callback_query, query_msg, job)

    elif data.startswith("recap_url|"):
        parts = data.split("|")
        short_id = parts[1]
        src_lang = parts[2] if len(parts) >= 4 else 'auto'
        target_lang = parts[3] if len(parts) >= 4 else parts[2]
        url = url_cache.get(short_id)
        if not url:
            await safe_edit_text(query_msg, "Link expired or invalid. Please send it again.")
            return
        from downloader import classify_url, clean_youtube_url
        if classify_url(url) == 'youtube':
            url = clean_youtube_url(url)
        await callback_query.answer("", show_alert=False)
        job = {
            "action": "recap_url", "short_id": short_id, "url": url,
            "src_lang": src_lang, "target_lang": target_lang,
            "is_video": True, "thumb_path": None,
            "query_msg_id": query_msg.id, "query_msg_chat_id": query_msg.chat.id
        }
        await _ask_thumbnail(callback_query, query_msg, job)

    elif data.startswith("thumb_yes|"):
        # User wants to add thumbnail - prompt for image upload
        _, job_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        job = thumb_pending.get(user_id)
        if not job:
            await callback_query.answer("Session expired. Please start again.", show_alert=True)
            return
        job["step"] = "await_thumb"
        thumb_pending[user_id] = job
        await callback_query.answer("", show_alert=False)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("▶️ Skip (No Thumbnail)", callback_data=f"thumb_no|{job_id}"),
                InlineKeyboardButton("🔙 Back", callback_data=f"thumb_back|{job_id}")
            ]
        ])
        await safe_edit_text(query_msg,
            "🖼️ **Upload your thumbnail image now.**\n\n"
            "Send a **photo** (JPG/PNG) — it will be embedded as the video cover art.\n"
            "The processing will start immediately after you upload it.\n\n"
            "Or click **Skip** / **Back** below if you changed your mind:",
            reply_markup=keyboard
        )

    elif data.startswith("thumb_back|"):
        _, job_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        job = thumb_pending.get(user_id)
        if not job:
            await callback_query.answer("Session expired. Please start again.", show_alert=True)
            return
        await callback_query.answer("", show_alert=False)
        await _ask_thumbnail(callback_query, query_msg, job)

    elif data.startswith("thumb_no|"):
        # No thumbnail — proceed to ask background sound
        _, job_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        job = thumb_pending.pop(user_id, None)
        if not job:
            await callback_query.answer("Session expired. Please start again.", show_alert=True)
            return
        await callback_query.answer("", show_alert=False)
        job["thumb_path"] = None
        await _ask_bgm(client, query_msg, job)

    elif data.startswith("bgm_yes|") or data.startswith("bgm_no|"):
        _, job_id = data.split("|", 1)
        user_id = callback_query.from_user.id
        job = thumb_pending.pop(user_id, None)
        if not job:
            await callback_query.answer("Session expired. Please start again.", show_alert=True)
            return
        await callback_query.answer("", show_alert=False)
        
        if data.startswith("bgm_yes|"):
            job["keep_bgm"] = True
        else:
            job["keep_bgm"] = False
            
        await _run_thumb_job(client, query_msg, job)


    elif data.startswith("dub_lang|") or data.startswith("recap_file|"):
        parts = data.split('|')
        action, short_id = parts[0], parts[1]
        cached_msg = url_cache.get(short_id)
        if not cached_msg:
            await safe_edit_text(query_msg, "Session expired. Please send the file again.")
            return

        src_lang = parts[2] if len(parts) >= 4 else 'auto'
        target_lang = parts[3] if len(parts) >= 4 else parts[2]
        is_video = bool(cached_msg.video or (cached_msg.document and str(cached_msg.document.mime_type or "").startswith("video/")))
        
        real_action = "dub_file" if action.startswith("dub_lang") else "recap_file"
        settings = get_user_voice_settings(query_msg.chat.id)

        job = {
            "action": real_action, "short_id": short_id,
            "src_lang": src_lang, "target_lang": target_lang,
            "is_video": is_video, "thumb_path": None,
            "tts_backend": settings["tts_backend"],
            "whisper_model": settings["whisper_model"],
            "transcribe_engine": settings.get("transcribe_engine"),
            "translate_engine": settings.get("translate_engine"),
            "voxcpm_model": settings.get("voxcpm_model"),
            "query_msg_id": query_msg.id, "query_msg_chat_id": query_msg.chat.id
        }
        await callback_query.answer("", show_alert=False)
        await _ask_thumbnail(callback_query, query_msg, job)

    elif data.startswith("conv_"):
        parts = data.split('|')
        action, short_id = parts[0], parts[1]
        cached_msg = url_cache.get(short_id)
        if not cached_msg:
            await safe_edit_text(query_msg, "Session expired. Please send the file again.")
            return

        cancel_id = f"conv_{query_msg.id}"
        register_cancel_task(cancel_id)
        try:
            async with JobQueueContext(query_msg, "Media Conversion"):
                async with RealtimeTimer(query_msg, "Downloading file from Telegram...", cancel_id=cancel_id) as timer:
                    try:
                        def pyrogram_download_progress(current, total):
                            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                            percent = current * 100 / total
                            timer.update_text(f"Downloading... {percent:.1f}% ({current/1024/1024:.1f}MB / {total/1024/1024:.1f}MB)")
                        input_path = await cached_msg.download(progress=pyrogram_download_progress)
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        timer.update_text("Converting... Please wait.")
                        output_path = None
                        send_method = client.send_document
                        send_kwargs = {}
                        def progress_callback(text):
                            if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                            timer.update_text(text)
    
                        if action == "conv_aud":
                            output_path = await asyncio.to_thread(convert_video_to_audio, input_path, 'mp3', progress_callback)
                            send_method = client.send_audio
                            send_kwargs = {'audio': output_path} if output_path else {}
                        elif action == "conv_vid":
                            target_format = parts[2]
                            output_path = await asyncio.to_thread(convert_video_format, input_path, target_format, progress_callback)
                            send_method = client.send_video if target_format in ['mp4', 'mkv', 'avi'] else client.send_document
                            send_kwargs = {'video': output_path, 'supports_streaming': True} if target_format in ['mp4', 'mkv', 'avi'] else {'document': output_path}
                        elif action == "conv_img":
                            target_format = parts[2]
                            output_path = await asyncio.to_thread(convert_image_format, input_path, target_format)
                            send_method = client.send_photo
                            send_kwargs = {'photo': output_path} if output_path else {}
                        elif action == "conv_doc":
                            target_format = parts[2]
                            from converter import convert_document_format
                            output_path = await asyncio.to_thread(convert_document_format, input_path, target_format)
                            send_method = client.send_document
                            send_kwargs = {'document': output_path} if output_path else {}
                        if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                        if output_path and os.path.exists(output_path):
                            timer.update_text("Conversion complete! Uploading...")
                            up_start_time = time.time()
                            def pyrogram_upload_progress(current, total):
                                if is_task_cancelled(cancel_id): raise ProcessCancelledException()
                                elapsed = time.time() - up_start_time
                                speed = (current / elapsed) if elapsed > 0 else 0
                                eta = ((total - current) / speed) if speed > 0 else 0
                                dl_mb = current / 1024 / 1024
                                tot_mb = total / 1024 / 1024
                                spd_mb = speed / 1024 / 1024
                                timer.update_text(f"{dl_mb:.1f}MB/{tot_mb:.1f}MB, ETA {eta:.0f}s, {spd_mb:.1f}MB/s Sending...")
                            await send_method(chat_id=query_msg.chat.id, **send_kwargs, progress=pyrogram_upload_progress)
                            cleanup_file(output_path)
                            await safe_edit_text(query_msg, "Done! ✅")
                        else:
                            await safe_edit_text(query_msg, "Failed to convert the file.")
                        cleanup_file(input_path)
                    except ProcessCancelledException:
                        for p in ['input_path', 'output_path']:
                            v = locals().get(p)
                            if v: cleanup_file(v)
                        await safe_edit_text(query_msg, "🛑 **Process stopped by user! Temporary files cleaned up.** ✅")
                    except Exception as e:
                        print(f"Error in callback: {e}")
                        await safe_edit_text(query_msg, f"❌ An error occurred: {e}")
        finally:
            unregister_cancel_task(cancel_id)

    # Continue propagation to any other registered handlers if unhandled above
    try:
        callback_query.continue_propagation()
    except Exception:
        pass
