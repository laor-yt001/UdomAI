import os
import json
import base64
import time
import logging
import threading
import urllib.request
import urllib.error

logger = logging.getLogger("github_sync")

# Direct opener to ensure GitHub API requests bypass any local dead proxy
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_PAT = "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"
DEFAULT_REPOS = [
    "doai-bot/main",
    "doai-bot/server1",
    "doai-bot/server2",
]

def get_github_pat():
    raw = os.environ.get("GITHUB_PAT") or os.environ.get("PAT") or os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or DEFAULT_PAT
    if not raw:
        return ""
    return str(raw).strip().strip('"\'').replace("\r", "").replace("\n", "").strip()

def get_headers():
    pat = get_github_pat()
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "UdomAI-Sync",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache"
    }
    if pat:
        clean_pat = pat.strip().strip('"\'').replace("\r", "").replace("\n", "").strip()
        headers["Authorization"] = f"token {clean_pat}"
    return headers

def fetch_file_from_github(repo, path):
    """Fetches text content and sha of a file from a GitHub repository."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers=get_headers())
    try:
        with _direct_opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = base64.b64decode(data["content"]).decode("utf-8")
            return content, data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        logger.warning(f"GitHub API error fetching {path} from {repo}: {e}")
        return None, None
    except Exception as e:
        logger.warning(f"Error fetching {path} from {repo}: {e}")
        return None, None

def push_file_to_github(filepath, commit_msg=None, repos=None, blocking=False):
    """
    Pushes a local file's content to GitHub repositories via the GitHub Contents API.
    If blocking=True, executes synchronously in the caller's thread to guarantee persistence.
    Otherwise runs asynchronously in a background daemon thread.
    """
    if repos is None:
        repos = DEFAULT_REPOS
    if commit_msg is None:
        commit_msg = f"sync: update {os.path.basename(filepath)} [skip ci]"

    if not os.path.exists(filepath):
        return

    try:
        with open(filepath, "rb") as f:
            raw_bytes = f.read()
    except Exception as e:
        logger.error(f"Failed to read {filepath} for GitHub push: {e}")
        return

    content_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    rel_path = os.path.basename(filepath)

    def _do_push():
        for repo in repos:
            for attempt in range(2):
                try:
                    # 1. Fetch current sha
                    _, sha = fetch_file_from_github(repo, rel_path)
                    url = f"https://api.github.com/repos/{repo}/contents/{rel_path}"
                    payload = {
                        "message": commit_msg,
                        "content": content_b64,
                        "branch": "main"
                    }
                    if sha:
                        payload["sha"] = sha
                    
                    body_bytes = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(url, data=body_bytes, headers=get_headers(), method="PUT")
                    with _direct_opener.open(req, timeout=15) as resp:
                        if resp.status in (200, 201):
                            logger.info(f"Successfully synced {rel_path} to {repo} (HTTP {resp.status})")
                            break
                        else:
                            logger.warning(f"Unexpected response syncing {rel_path} to {repo}: {resp.status}")
                except urllib.error.HTTPError as h_err:
                    if h_err.code == 409 and attempt == 0:
                        time.sleep(1)
                        continue
                    logger.warning(f"Failed syncing {rel_path} to {repo}: {h_err}")
                except Exception as push_err:
                    logger.warning(f"Failed syncing {rel_path} to {repo}: {push_err}")
                    break

    if blocking:
        _do_push()
    else:
        t = threading.Thread(target=_do_push, daemon=True)
        t.start()

def update_repo_about(repo="udom-bot1/main", homepage_url=None, description=None):
    """Updates the GitHub repository homepage and description via GitHub REST API."""
    if not homepage_url and not description:
        return False
    pat = get_github_pat()
    if not pat:
        return False
    url = f"https://api.github.com/repos/{repo}"
    payload = {}
    if homepage_url:
        payload["homepage"] = homepage_url
    if description:
        payload["description"] = description
    
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=get_headers(), method="PATCH")
    try:
        with _direct_opener.open(req, timeout=10) as resp:
            if resp.status in (200, 201):
                logger.info(f"Updated GitHub repo About for {repo}: {payload}")
                return True
    except Exception as e:
        logger.warning(f"Failed updating GitHub repo About for {repo}: {e}")
    return False

def sync_pull_file(filepath, repo="udom-bot1/main"):
    """
    Pulls latest file from GitHub. If content is different, updates local file on disk.
    Returns True if updated.
    """
    rel_path = os.path.basename(filepath)
    remote_content, _ = fetch_file_from_github(repo, rel_path)
    if remote_content is None:
        return False

    local_content = ""
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                local_content = f.read()
        except Exception:
            pass

    if remote_content.strip() != local_content.strip():
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(remote_content)
            logger.info(f"Pulled updated {rel_path} from {repo}")
            return True
        except Exception as e:
            logger.error(f"Error writing pulled {rel_path}: {e}")
    return False

_sync_thread = None

def _sync_worker(interval):
    files_to_sync = ["users_data.json", "schedules.json"]
    while True:
        try:
            for fname in files_to_sync:
                updated = sync_pull_file(fname)
                if updated and fname == "users_data.json":
                    # Reconcile custom bots if users or assigned servers changed
                    try:
                        import custom_bots_manager
                        if hasattr(custom_bots_manager, "main_loop") and custom_bots_manager.main_loop:
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                custom_bots_manager.start_all_approved_bots(),
                                custom_bots_manager.main_loop
                            )
                    except Exception as e:
                        logger.debug(f"Bot reconciliation error: {e}")
        except Exception as e:
            logger.debug(f"Sync loop error: {e}")
        time.sleep(interval)

def start_github_sync_daemon(interval=20):
    global _sync_thread
    if _sync_thread and _sync_thread.is_alive():
        return
    _sync_thread = threading.Thread(target=_sync_worker, args=(interval,), daemon=True)
    _sync_thread.start()
    logger.info(f"GitHub real-time sync daemon started (interval={interval}s).")
