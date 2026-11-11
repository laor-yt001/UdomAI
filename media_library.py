import os
import re
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _library_root():
    configured = os.environ.get("MEDIA_LIBRARY_DIR")
    if configured:
        root = os.path.realpath(os.path.expanduser(configured))
    elif os.path.isdir("/var/data"):
        root = os.path.join("/var/data", "media_library")
    else:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "media_library")
    os.makedirs(root, exist_ok=True)
    return root


def _safe_file_path(name, extensions):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 255
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise ValueError("Invalid media filename.")

    root = os.path.realpath(_library_root())
    candidate = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError("Media filename is outside the library.")
    if Path(candidate).suffix.lower() not in extensions:
        raise ValueError("Unsupported media file type.")
    if not os.path.isfile(candidate):
        raise FileNotFoundError("Media file was not found.")
    return candidate


def resolve_episode(name):
    return _safe_file_path(name, VIDEO_EXTENSIONS)


def resolve_thumbnail(name):
    video_path = resolve_episode(name)
    root = _library_root()
    stem = Path(video_path).stem
    for extension in IMAGE_EXTENSIONS:
        candidate = os.path.realpath(os.path.join(root, stem + extension))
        if os.path.commonpath((os.path.realpath(root), candidate)) != os.path.realpath(root):
            continue
        if os.path.isfile(candidate):
            return candidate
    return None


def _natural_sort_key(value):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
    )


def list_media():
    root = _library_root()
    items = []
    for entry in os.scandir(root):
        if not entry.is_file(follow_symlinks=True):
            continue
        if Path(entry.name).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        try:
            path = resolve_episode(entry.name)
            thumbnail = resolve_thumbnail(entry.name)
            items.append({
                "name": entry.name,
                "size": os.path.getsize(path),
                "has_thumbnail": thumbnail is not None,
            })
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda item: _natural_sort_key(item["name"]))
