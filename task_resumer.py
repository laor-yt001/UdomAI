"""
Task Resumer & Checkpoint Manager
=================================
Ensures that all bot, web, and scheduled automation tasks persist their progress.
When a GitHub Actions runner, container, or server restarts or ends,
the new runner automatically resumes any unfinished tasks from the exact
episode or step where the previous runner stopped.
"""

import os
import json
import logging
import threading
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

_lock = threading.Lock()
ACTIVE_TASKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "active_tasks.json")


def _get_redis():
    try:
        from user_manager import redis_client
        return redis_client
    except Exception:
        return None


def load_all_checkpoints() -> Dict[str, Any]:
    with _lock:
        r = _get_redis()
        if r:
            try:
                data = r.get("active_tasks_checkpoints")
                if data:
                    return json.loads(data)
            except Exception as e:
                logger.warning(f"Error loading checkpoints from Redis: {e}")

        if os.path.exists(ACTIVE_TASKS_FILE):
            try:
                with open(ACTIVE_TASKS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error reading {ACTIVE_TASKS_FILE}: {e}")
        return {}


def save_checkpoint(task_id: str, data: Dict[str, Any]):
    if not task_id:
        return
    with _lock:
        current = {}
        if os.path.exists(ACTIVE_TASKS_FILE):
            try:
                with open(ACTIVE_TASKS_FILE, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = {}

        current[str(task_id)] = data

        # Write to file
        try:
            with open(ACTIVE_TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Error saving {ACTIVE_TASKS_FILE}: {e}")

        # Sync to Redis
        r = _get_redis()
        if r:
            try:
                r.set("active_tasks_checkpoints", json.dumps(current, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"Error saving checkpoints to Redis: {e}")


def get_checkpoint(task_id: str) -> Dict[str, Any] | None:
    checkpoints = load_all_checkpoints()
    return checkpoints.get(str(task_id))


def remove_checkpoint(task_id: str):
    if not task_id:
        return
    with _lock:
        current = {}
        if os.path.exists(ACTIVE_TASKS_FILE):
            try:
                with open(ACTIVE_TASKS_FILE, "r", encoding="utf-8") as f:
                    current = json.load(f)
            except Exception:
                current = {}

        if str(task_id) in current:
            del current[str(task_id)]

        try:
            with open(ACTIVE_TASKS_FILE, "w", encoding="utf-8") as f:
                json.dump(current, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Error writing {ACTIVE_TASKS_FILE}: {e}")

        r = _get_redis()
        if r:
            try:
                r.set("active_tasks_checkpoints", json.dumps(current, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"Error deleting checkpoint from Redis: {e}")


def mark_episode_completed(task_id: str, vid: str, metadata: Dict[str, Any] = None):
    """Mark an episode vid as finished for task_id and persist immediately."""
    cp = get_checkpoint(task_id) or {}
    completed = cp.get("completed_vids", [])
    if str(vid) not in completed:
        completed.append(str(vid))
    cp["completed_vids"] = completed
    cp["status"] = "in_progress"
    if metadata:
        cp.update(metadata)
    save_checkpoint(task_id, cp)


def has_unfinished_tasks() -> bool:
    """Returns True if there are any active checkpoints waiting to be completed."""
    cps = load_all_checkpoints()
    for tid, data in cps.items():
        if data.get("status") in ("in_progress", "Running", "pending"):
            return True
    return False


async def resume_unfinished_bot_tasks(client):
    """Auto-resume direct bot streaming downloads that were interrupted by an action ending."""
    import asyncio
    checkpoints = load_all_checkpoints()
    for task_id, data in list(checkpoints.items()):
        if data.get("type") == "direct_hg" and data.get("status") == "in_progress":
            chat_id = data.get("chat_id")
            series = data.get("series")
            episodes = data.get("episodes")
            if chat_id and series and episodes:
                try:
                    logger.info(f"Resuming direct bot download for chat {chat_id}: {series.get('name')}")
                    status_msg = await client.send_message(
                        chat_id,
                        f"🔄 **Resuming unfinished download from previous run:**\n"
                        f"📺 **{series.get('name')}**\n"
                        f"⏳ Resuming download..."
                    )
                    from plugins.core import _stream_download_and_upload_hongguo
                    asyncio.create_task(_stream_download_and_upload_hongguo(client, chat_id, status_msg, series, episodes, task_id=task_id))
                except Exception as e:
                    logger.warning(f"Failed to resume direct bot task {task_id}: {e}")
