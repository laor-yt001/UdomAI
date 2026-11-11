"""
gemini_manager.py
=================
Multi-key rotation and automatic failover manager for Google Gemini API keys.
Supports loading keys from environment variables, .env, and persisted system config.
If any key hits rate limits (429), quota limits, or network errors, it seamlessly
fails over to the next configured key.
"""

import os
import re
import json
import logging
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

logger = logging.getLogger(__name__)

# Working Gemini model list in order of active availability and high RPM limits
GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest"
]

def mask_key(k: str) -> str:
    """Mask key for safe display in logs and UI (e.g. AIza...xyz123)."""
    if not k:
        return ""
    if len(k) <= 8:
        return "***"
    return f"{k[:4]}...{k[-4:]}"

def get_gemini_keys() -> list[str]:
    """
    Returns an ordered list of unique Gemini API keys.
    Collects keys from:
      1. Persisted system config in user_manager (database/users.json)
      2. Environment variable GEMINI_API_KEYS (comma or newline separated)
      3. Environment variable GEMINI_API_KEY (comma or newline separated)
    """
    raw_keys = []

    # 1. Check persisted system config
    try:
        from user_manager import get_system_config
        cfg = get_system_config()
        saved = cfg.get("gemini_api_keys")
        if isinstance(saved, list):
            raw_keys.extend(saved)
        elif isinstance(saved, str):
            raw_keys.append(saved)
    except Exception:
        pass

    # 2. Check environment variables
    env_keys_multi = os.environ.get("GEMINI_API_KEYS", "")
    if env_keys_multi:
        raw_keys.append(env_keys_multi)

    env_key_single = os.environ.get("GEMINI_API_KEY", "")
    if env_key_single:
        raw_keys.append(env_key_single)

    # 3. Parse, clean, and deduplicate
    keys = []
    seen = set()
    for item in raw_keys:
        if not item:
            continue
        # Split by comma, semicolon, newline, or whitespace
        parts = re.split(r'[,;\n\r\t]+', str(item))
        for p in parts:
            p = p.strip().strip('"').strip("'")
            if p and len(p) >= 15 and p not in seen:
                seen.add(p)
                keys.append(p)

    return keys

def save_gemini_keys(keys_input) -> list[str]:
    """
    Saves a list or string of Gemini API keys into persistent system config.
    Returns the parsed list of valid keys.
    """
    parsed = []
    seen = set()
    if isinstance(keys_input, str):
        parts = re.split(r'[,;\n\r\t]+', keys_input)
    elif isinstance(keys_input, (list, tuple)):
        parts = []
        for x in keys_input:
            parts.extend(re.split(r'[,;\n\r\t]+', str(x)))
    else:
        parts = []

    for p in parts:
        p = p.strip().strip('"').strip("'")
        if p and len(p) >= 15 and p not in seen:
            seen.add(p)
            parsed.append(p)

    try:
        from user_manager import update_system_config
        update_system_config("gemini_api_keys", parsed)
    except Exception as e:
        logger.error(f"[gemini_manager] Failed to save keys to system config: {e}")

    # Update active environment variable
    if parsed:
        os.environ["GEMINI_API_KEY"] = parsed[0]
        os.environ["GEMINI_API_KEYS"] = "\n".join(parsed)

    return parsed

def post_gemini_generate(prompt_or_contents, model="gemini-flash-lite-latest", timeout=25, generation_config=None) -> str | None:
    """
    Calls Google Gemini API with automatic failover across all configured keys and models.
    Supports generation_config (maxOutputTokens, temperature, etc.).
    Returns the generated response text or None if all keys failed.
    """
    keys = get_gemini_keys()
    if not keys:
        logger.warning("[gemini_manager] No Gemini API keys configured.")
        return None

    # Prepare payload
    if isinstance(prompt_or_contents, str):
        payload = {"contents": [{"parts": [{"text": prompt_or_contents}]}]}
    elif isinstance(prompt_or_contents, list):
        payload = {"contents": prompt_or_contents}
    elif isinstance(prompt_or_contents, dict):
        payload = dict(prompt_or_contents)
    else:
        payload = {"contents": [{"parts": [{"text": str(prompt_or_contents)}]}]}

    if generation_config and isinstance(generation_config, dict):
        payload["generationConfig"] = generation_config

    # Candidate models starting with requested model
    candidate_models = [model] + [m for m in GEMINI_MODELS if m != model]

    for k_idx, key in enumerate(keys):
        masked = mask_key(key)
        for cand_model in candidate_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:generateContent?key={key}"
            try:
                res = requests.post(url, json=payload, timeout=timeout)
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "").strip()
                            if text:
                                return text
                elif res.status_code == 429:
                    logger.warning(f"[gemini_manager] Key {masked} rate-limited (429) on {cand_model}. Trying next key...")
                    break  # Key is quota/rate limited, jump immediately to next key
                elif res.status_code in (400, 403):
                    logger.warning(f"[gemini_manager] Key {masked} returned {res.status_code} on {cand_model}. Trying next key...")
                    break  # Invalid key or permissions error, jump to next key
                elif res.status_code == 404:
                    # Model not found on this endpoint, try next candidate model
                    continue
                elif res.status_code == 503:
                    logger.warning(f"[gemini_manager] Key {masked} service unavailable (503) on {cand_model}. Trying next key...")
                    break  # Server temporarily overloaded, try next key immediately
                else:
                    logger.warning(f"[gemini_manager] Key {masked} error {res.status_code} on {cand_model}.")
            except Exception as e:
                logger.warning(f"[gemini_manager] Key {masked} connection error ({e}) on {cand_model}.")
                break  # Connection error for this key, try next key

    logger.error("[gemini_manager] All configured Gemini API keys failed.")
    return None

def post_gemini_audio(audio_path: str, prompt: str, model="gemini-flash-lite-latest", timeout=60, generation_config=None) -> str | None:
    """
    Sends an audio file along with a prompt to Gemini API with multi-key rotation and failover.
    Supports .mp3, .wav, .m4a, .aac, .ogg.
    """
    if not audio_path or not os.path.exists(audio_path):
        logger.error(f"[gemini_manager] Audio file not found: {audio_path}")
        return None

    import base64
    ext = os.path.splitext(audio_path)[1].lower()
    mime_map = {
        ".mp3": "audio/mp3",
        ".wav": "audio/wav",
        ".m4a": "audio/m4a",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac"
    }
    mime_type = mime_map.get(ext, "audio/mp3")

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
            if not audio_bytes:
                logger.error("[gemini_manager] Audio file is empty.")
                return None
            b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    except Exception as e:
        logger.error(f"[gemini_manager] Failed to read audio file: {e}")
        return None

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_audio
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }
    return post_gemini_generate(payload, model=model, timeout=timeout, generation_config=generation_config)

def test_all_keys() -> list[dict]:
    """
    Tests all configured Gemini API keys and returns detailed status for each.
    """
    keys = get_gemini_keys()
    results = []
    if not keys:
        return results

    test_payload = {"contents": [{"parts": [{"text": "Hello"}]}]}
    for k in keys:
        masked = mask_key(k)
        item = {"key": masked, "full_key": k, "status": "UNKNOWN", "model": "gemini-flash-lite-latest", "error": None}
        for cand_model in GEMINI_MODELS[:3]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{cand_model}:generateContent?key={k}"
            try:
                res = requests.post(url, json=test_payload, timeout=8)
                if res.status_code == 200:
                    item["status"] = "ACTIVE"
                    item["model"] = cand_model
                    break
                elif res.status_code == 429:
                    item["status"] = "RATE_LIMITED"
                    item["error"] = "Quota exceeded or rate-limited (429)"
                    break
                elif res.status_code in (400, 403):
                    item["status"] = "INVALID"
                    item["error"] = f"Authentication error ({res.status_code})"
                    break
                else:
                    item["status"] = f"HTTP_{res.status_code}"
                    item["error"] = res.text[:100]
            except Exception as e:
                item["status"] = "NETWORK_ERROR"
                item["error"] = str(e)
                break
        results.append(item)
    return results

