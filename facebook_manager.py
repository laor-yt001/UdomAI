import os
import json
import logging
import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import uuid

logger = logging.getLogger(__name__)

FB_GRAPH_VERSION = "v19.0"
FB_GRAPH_BASE = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"
FB_VIDEO_BASE = f"https://graph-video.facebook.com/{FB_GRAPH_VERSION}"


def get_stored_facebook_tokens() -> dict:
    """Returns stored Facebook App ID, App Token, and User Token from env or config."""
    return {
        "app_id": os.environ.get("FB_APP_ID", "1125811570343815").strip(),
        "app_token": os.environ.get("FB_APP_TOKEN", "1125811570343815|2Colub46gwdyh8-2zHuLDbJOLF4").strip(),
        "user_token": os.environ.get("FB_USER_TOKEN", "").strip(),
    }


def fetch_user_pages(user_access_token: str) -> list[dict]:
    """
    Fetches all Facebook Pages that this user access token manages.
    Returns list of dicts:
      [
        {
          "page_id": "...",
          "page_name": "...",
          "page_token": "...",
          "category": "...",
          "can_post": True,
          "tasks": ["CREATE_CONTENT", "MANAGE", ...]
        }
      ]
    """
    if not user_access_token or not user_access_token.strip():
        logger.warning("[facebook] No user access token provided.")
        return []

    token = user_access_token.strip()
    url = f"{FB_GRAPH_BASE}/me/accounts?fields=id,name,access_token,tasks,category&access_token={urllib.parse.quote(token)}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UdomAI-Bot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_pages = data.get("data", [])
            pages = []
            for p in raw_pages:
                tasks = p.get("tasks", [])
                can_post = any(t in tasks for t in ("CREATE_CONTENT", "MANAGE", "MODERATE_COMMENTS")) or not tasks
                pages.append({
                    "page_id": str(p.get("id")),
                    "page_name": p.get("name", "Unnamed Page"),
                    "page_token": p.get("access_token", ""),
                    "category": p.get("category", ""),
                    "tasks": tasks,
                    "can_post": can_post,
                })
            logger.info(f"[facebook] Discovered {len(pages)} Facebook pages.")
            return pages
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        logger.error(f"[facebook] HTTP error fetching pages ({e.code}): {err_msg}")
        raise RuntimeError(f"Facebook Graph API Error ({e.code}): {err_msg}")
    except Exception as e:
        logger.error(f"[facebook] Failed to fetch pages: {e}")
        raise RuntimeError(f"Failed to communicate with Facebook: {e}")


def post_to_facebook(page_id: str, page_token: str, message: str = "", media_path: str | None = None) -> dict:
    """
    Posts text, photo, or video to a Facebook Page using its Page Access Token.
    Returns: {"success": True, "post_id": "..."} or raises RuntimeError.
    """
    if not page_id or not page_token:
        raise ValueError("Both page_id and page_token are required to post.")

    page_id = str(page_id).strip()
    page_token = str(page_token).strip()

    import requests

    headers = {"User-Agent": "UdomAI-Bot/1.0"}
    
    # 1. Video Upload
    if media_path and os.path.exists(media_path) and media_path.lower().endswith(('.mp4', '.mkv', '.mov', '.avi', '.webm')):
        url = f"{FB_VIDEO_BASE}/{page_id}/videos"
        with open(media_path, "rb") as f:
            files = {"source": f}
            data = {
                "access_token": page_token,
                "description": message or "",
                "title": (message[:60] if message else "Video Post"),
            }
            resp = requests.post(url, data=data, files=files, headers=headers, timeout=300)
            res_json = resp.json()
            if resp.status_code == 200 and "id" in res_json:
                return {"success": True, "id": res_json["id"], "type": "video"}
            raise RuntimeError(f"Facebook Video Post Failed: {res_json.get('error', {}).get('message', resp.text)}")

    # 2. Photo Upload
    if media_path and os.path.exists(media_path) and media_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
        url = f"{FB_GRAPH_BASE}/{page_id}/photos"
        with open(media_path, "rb") as f:
            files = {"source": f}
            data = {
                "access_token": page_token,
                "caption": message or "",
            }
            resp = requests.post(url, data=data, files=files, headers=headers, timeout=60)
            res_json = resp.json()
            if resp.status_code == 200 and "id" in res_json:
                return {"success": True, "id": res_json["id"], "type": "photo"}
            raise RuntimeError(f"Facebook Photo Post Failed: {res_json.get('error', {}).get('message', resp.text)}")

    # 3. Text Feed Post
    url = f"{FB_GRAPH_BASE}/{page_id}/feed"
    data = {
        "access_token": page_token,
        "message": message or "Update from UdomAI",
    }
    resp = requests.post(url, data=data, headers=headers, timeout=30)
    res_json = resp.json()
    if resp.status_code == 200 and "id" in res_json:
        return {"success": True, "id": res_json["id"], "type": "feed"}
    raise RuntimeError(f"Facebook Feed Post Failed: {res_json.get('error', {}).get('message', resp.text)}")


def sync_and_store_facebook_pages(user_access_token: str, user_id: str | None = None) -> list[dict]:
    """
    Discovers all pages from Facebook and registers them into system config & user config.
    """
    pages = fetch_user_pages(user_access_token)
    if not pages:
        return []

    from user_manager import update_system_config, get_user_facebook_pages, update_user_facebook

    # Store in system config
    update_system_config("discovered_facebook_pages", pages)
    update_system_config("last_fb_user_token", user_access_token)

    # Store for specific user if provided
    if user_id:
        existing = get_user_facebook_pages(str(user_id))
        existing_pids = {p.get("page_id") for p in existing}
        for p in pages:
            if p.get("can_post") and p["page_id"] not in existing_pids:
                existing.append({
                    "page_id": p["page_id"],
                    "page_token": p["page_token"],
                    "page_name": p["page_name"]
                })
        update_user_facebook(str(user_id), existing)

    return pages
