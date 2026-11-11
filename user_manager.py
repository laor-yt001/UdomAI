import os
import time
import json
import threading
from datetime import datetime, timezone, timedelta

import base64
import requests

from dotenv import load_dotenv

load_dotenv()

if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    USERS_FILE = "/var/data/users_data.json"
else:
    USERS_FILE = os.path.join(os.path.dirname(__file__), "users_data.json")
_lock = threading.Lock()

# Predefined admin usernames automatically granted initial approval
AUTO_APPROVED_USERNAMES = ["udomai", "thengrithy"]

import redis

REDIS_URL = os.environ.get("REDIS_URL")
redis_client = None
if REDIS_URL:
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        # Test connection
        redis_client.ping()
        print("Connected to Redis for user_manager! ✅")
    except Exception as e:
        print(f"Failed to connect to Redis: {e}")
        redis_client = None

def load_users():
    with _lock:
        if redis_client:
            try:
                data = redis_client.get("users_data")
                if data:
                    return json.loads(data)
            except Exception as e:
                print(f"Error loading users from Redis: {e}")
                
        # Fallback to local file if Redis is missing or empty
        if not os.path.exists(USERS_FILE) or os.path.getsize(USERS_FILE) < 5:
            return {}
            
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                file_data = json.load(f)
                # Auto-migrate to Redis if possible
                if redis_client and file_data:
                    try:
                        redis_client.set("users_data", json.dumps(file_data, ensure_ascii=False))
                        print("Migrated local users_data.json to Redis! ✅")
                    except Exception as e:
                        print(f"Error migrating to Redis: {e}")
                return file_data
        except Exception as e:
            print(f"Error loading users from file: {e}")
            return {}

def save_users(users, sync_github=True, blocking_sync=False):
    with _lock:
        # Save to Redis
        if redis_client:
            try:
                redis_client.set("users_data", json.dumps(users, ensure_ascii=False))
            except Exception as e:
                print(f"Error saving users to Redis: {e}")
                
        # Also backup to local file just in case
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving users to file: {e}")

    # Real-time push to GitHub repositories so all worker servers receive updates immediately
    if sync_github:
        try:
            import github_sync
            github_sync.push_file_to_github(USERS_FILE, "chore: sync users_data.json [skip ci]", blocking=blocking_sync)
        except Exception as gh_e:
            print(f"Error syncing users_data.json to GitHub: {gh_e}")

def get_user(user_id):
    users = load_users()
    return users.get(str(user_id))

def is_user_approved(user_id):
    user = get_user(user_id)
    if not user:
        return True
    return user.get("status") != "BLOCKED"

def register_or_update_user(tg_user):
    """
    Registers a new user (default status = APPROVED so all functions work)
    or updates their name/username/last_active.
    """
    if not tg_user:
        return {}
        
    user_id = str(tg_user.id)
    users = load_users()
    now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    
    username = getattr(tg_user, 'username', '') or ''
    first_name = getattr(tg_user, 'first_name', '') or ''
    last_name = getattr(tg_user, 'last_name', '') or ''
    
    is_new = user_id not in users
    if is_new:
        role = "SUPER_ADMIN" if username and username.lower() in [u.lower() for u in AUTO_APPROVED_USERNAMES] else "USER"
        status = "APPROVED" if role == "SUPER_ADMIN" else "PENDING"
        users[user_id] = {
            "user_id": tg_user.id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "status": status,
            "role": role,
            "assigned_server": "main",
            "joined_at": now_str,
            "last_active": now_str,
            "request_count": 1
        }
    else:
        users[user_id]["username"] = username
        users[user_id]["first_name"] = first_name
        users[user_id]["last_name"] = last_name
        
        # Ensure backwards compatibility for role and assigned_server
        if "role" not in users[user_id]:
            users[user_id]["role"] = "SUPER_ADMIN" if username and username.lower() in [u.lower() for u in AUTO_APPROVED_USERNAMES] else "USER"
        if "assigned_server" not in users[user_id]:
            users[user_id]["assigned_server"] = "main"
        if users[user_id].get("status") == "BLOCKED" and username and username.lower() in [u.lower() for u in AUTO_APPROVED_USERNAMES]:
            users[user_id]["status"] = "APPROVED"
        elif "status" not in users[user_id]:
            users[user_id]["status"] = "PENDING"  # Default to PENDING if status was missing
        users[user_id]["last_active"] = now_str
        users[user_id]["request_count"] = users[user_id].get("request_count", 0) + 1
        
    save_users(users, sync_github=is_new)
    return users[user_id]

def toggle_user_status(user_id, status=None):
    user_id = str(user_id)
    users = load_users()
    if user_id in users:
        if status in ["APPROVED", "BLOCKED"]:
            users[user_id]["status"] = status
        else:
            users[user_id]["status"] = "BLOCKED" if users[user_id].get("status") == "APPROVED" else "APPROVED"
        save_users(users, sync_github=True)
        return users[user_id]
    return None

def update_user_role(user_id, role):
    user_id = str(user_id)
    users = load_users()
    if user_id in users and role in ["SUPER_ADMIN", "ADMIN", "USER"]:
        users[user_id]["role"] = role
        save_users(users, sync_github=True)
        return users[user_id]
    return None

def toggle_fb_page_access(user_id, page_id):
    user_id = str(user_id)
    users = load_users()
    if user_id in users:
        access = users[user_id].get("fb_pages_access", [])
        if isinstance(access, bool):
            access = ["aimovie", "livealone"] if access else []
        elif not isinstance(access, list):
            access = []
            
        if page_id in access:
            access.remove(page_id)
        else:
            access.append(page_id)
            
        users[user_id]["fb_pages_access"] = access
        save_users(users, sync_github=True)
        return users[user_id]
    return None

def update_user_assigned_server(user_id, server_id):
    user_id = str(user_id)
    users = load_users()
    if user_id in users:
        users[user_id]["assigned_server"] = server_id
        save_users(users, sync_github=True, blocking_sync=True)
        return users[user_id]
    return None


def remove_user(user_id):
    user_id = str(user_id)
    users = load_users()
    if user_id in users:
        del users[user_id]
        save_users(users, sync_github=True)
        
        # Clear schedules
        try:
            from scheduler import clear_user_schedules
            clear_user_schedules(user_id)
        except Exception as e:
            print(f"Error clearing schedules for user {user_id}: {e}")
            
        # Clear chat history (memory & disk)
        try:
            import json, os
            import plugins.core
            
            # Clear from memory if loaded
            if hasattr(plugins.core, 'chat_history'):
                if int(user_id) in plugins.core.chat_history:
                    del plugins.core.chat_history[int(user_id)]
                if user_id in plugins.core.chat_history:
                    del plugins.core.chat_history[user_id]
                    
            # Clear from disk directly
            if os.path.exists("/var/data") and os.path.isdir("/var/data"):
                hist_file = "/var/data/chat_history.json"
            else:
                hist_file = os.path.join(os.path.dirname(__file__), "chat_history.json")
            if os.path.exists(hist_file):
                with open(hist_file, 'r', encoding='utf-8') as f:
                    hist = json.load(f)
                
                removed = False
                for k in [user_id, int(user_id)]:
                    if str(k) in hist:
                        del hist[str(k)]
                        removed = True
                        
                if removed:
                    with open(hist_file, 'w', encoding='utf-8') as f:
                        json.dump(hist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error clearing chat history for user {user_id}: {e}")
            
        return True
    return False

def get_system_config():
    users = load_users()
    return users.get("__system_config__", {})

def update_system_config(key, value):
    users = load_users()
    if "__system_config__" not in users:
        users["__system_config__"] = {}
    users["__system_config__"][key] = value
    save_users(users, sync_github=True)
    return users["__system_config__"]

def update_user_session(user_id, session_string):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        users[user_id_str]["session_string"] = session_string
        save_users(users, sync_github=True)

def get_user_session(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        return users[user_id_str].get("session_string")
    return None

def update_user_facebook(user_id, pages):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        users[user_id_str]["fb_pages"] = pages
        # Clear legacy fields so they don't mistakenly fall back
        if "fb_page_id" in users[user_id_str]:
            del users[user_id_str]["fb_page_id"]
        if "fb_token" in users[user_id_str]:
            del users[user_id_str]["fb_token"]
        save_users(users, sync_github=True)

def get_user_facebook(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        user_data = users[user_id_str]
        if "fb_pages" in user_data and len(user_data["fb_pages"]) > 0:
            first_page = user_data["fb_pages"][0]
            return first_page.get("page_id"), first_page.get("page_token")
        elif "fb_page_id" in user_data and "fb_token" in user_data:
            return user_data["fb_page_id"], user_data["fb_token"]

    # Fallback to system default page if configured
    p_live = os.environ.get("LIVEALONE_PAGE_ID")
    t_live = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
    if p_live and t_live:
        return p_live, t_live

    p_ai = os.environ.get("AIMOVIEKHMER_PAGE_ID")
    t_ai = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
    if p_ai and t_ai:
        return p_ai, t_ai

    return None, None

def get_user_facebook_pages(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        user_data = users[user_id_str]
        if "fb_pages" in user_data:
            return user_data["fb_pages"]
        elif "fb_page_id" in user_data and "fb_token" in user_data:
            return [{"page_id": user_data["fb_page_id"], "page_token": user_data["fb_token"], "page_name": "Legacy Page"}]
    return []


def delete_user_facebook_page(user_id, page_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        user_data = users[user_id_str]
        changed = False
        if "fb_pages" in user_data:
            initial_len = len(user_data["fb_pages"])
            user_data["fb_pages"] = [p for p in user_data["fb_pages"] if p.get("page_id") != page_id]
            if len(user_data["fb_pages"]) != initial_len:
                changed = True
        if "fb_page_id" in user_data and user_data["fb_page_id"] == page_id:
            del user_data["fb_page_id"]
            if "fb_token" in user_data:
                del user_data["fb_token"]
            changed = True
        
        if changed:
            save_users(users, sync_github=True)
            return True
    return False



def get_user_telegram_bots(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        return users[user_id_str].get("telegram_bots", [])
    return []

def add_user_telegram_bot(user_id, bot_data):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        if "telegram_bots" not in users[user_id_str]:
            users[user_id_str]["telegram_bots"] = []
        if "status" not in bot_data:
            bot_data["status"] = "PENDING"
        bot_data["can_send_otp"] = False
        users[user_id_str]["telegram_bots"].append(bot_data)
        save_users(users, sync_github=True)
        return True
    return False

def update_user_telegram_bot(user_id, bot_token, updates):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users and "telegram_bots" in users[user_id_str]:
        for bot in users[user_id_str]["telegram_bots"]:
            if bot.get("bot_token") == bot_token:
                bot.update(updates)
                save_users(users, sync_github=True)
                return True
    return False

def get_active_user_bot(user_id):
    """Return the first approved custom bot token for a user, or None."""
    bots = get_user_telegram_bots(user_id)
    for bot in bots:
        if bot.get("status") == "APPROVED":
            return bot
    return None

def get_default_schedule_bot(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        return users[user_id_str].get("default_schedule_bot", "main_bot")
    return "main_bot"

def set_default_schedule_bot(user_id, bot_token):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        users[user_id_str]["default_schedule_bot"] = bot_token
        save_users(users, sync_github=True)
        return True
    return False

def set_user_max_custom_bots(user_id, max_bots):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        users[user_id_str]["max_custom_bots"] = int(max_bots)
        save_users(users, sync_github=True)
        return users[user_id_str]["max_custom_bots"]
    return 1

def delete_user_telegram_bot(user_id, bot_token):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users and "telegram_bots" in users[user_id_str]:
        original_len = len(users[user_id_str]["telegram_bots"])
        users[user_id_str]["telegram_bots"] = [
            b for b in users[user_id_str]["telegram_bots"] 
            if b.get("bot_token") != bot_token
        ]
        if len(users[user_id_str]["telegram_bots"]) < original_len:
            save_users(users, sync_github=True)
            return True
    return False
