import os
import json
import time
import asyncio
import threading
import requests
import urllib.parse
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAILURE_LOG_FILE = os.path.join(BASE_DIR, "failure_log.json")
if os.path.exists("/var/data") and os.path.isdir("/var/data"):
    STRATEGY_FILE = "/var/data/strategy.json"
else:
    STRATEGY_FILE = os.path.join(BASE_DIR, "strategy.json")
PATCHES_DIR = os.path.join(BASE_DIR, "patches")

os.makedirs(PATCHES_DIR, exist_ok=True)
_log_lock = threading.Lock()

# Initial Default Strategy
DEFAULT_STRATEGY = {
    "version": 1,
    "last_updated": datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S"),
    "provider_order": {
        "auto": ["gemini", "pollinations", "g4f", "ddgs"],
        "gemini": ["gemini-2.0-flash", "gemini-2.0-flash-thinking-exp", "gemini-1.5-pro", "gemini-1.5-flash"],
        "pollinations": ["openai", "qwen-coder", "llama", "deepseek"],
        "g4f": ["gpt-4o", "claude-3.5-sonnet", "deepseek-v3", "gpt-3.5-turbo"],
        "ddgs": ["gpt-4o-mini"]
    },
    "provider_health": {
        "gemini": {"success_rate": 100.0, "total_calls": 0, "failures": 0, "status": "HEALTHY"},
        "pollinations": {"success_rate": 100.0, "total_calls": 0, "failures": 0, "status": "HEALTHY"},
        "g4f": {"success_rate": 100.0, "total_calls": 0, "failures": 0, "status": "HEALTHY"},
        "ddgs": {"success_rate": 100.0, "total_calls": 0, "failures": 0, "status": "HEALTHY"}
    },
    "timeout_config": {
        "gemini": 6,
        "pollinations": 12,
        "g4f": 15,
        "ddgs": 10
    }
}

def load_strategy():
    if not os.path.exists(STRATEGY_FILE):
        save_strategy(DEFAULT_STRATEGY)
        return DEFAULT_STRATEGY
    try:
        with open(STRATEGY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[SelfImprover] Error loading strategy: {e}")
        return DEFAULT_STRATEGY

def save_strategy(strategy_data):
    try:
        strategy_data["last_updated"] = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
        with open(STRATEGY_FILE, "w", encoding="utf-8") as f:
            json.dump(strategy_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SelfImprover] Error saving strategy: {e}")

class SelfImprover:
    def __init__(self):
        self.strategy = load_strategy()

    def log_result(self, provider: str, model_id: str, success: bool, error_msg: str = ""):
        def _log():
            with _log_lock:
                logs = []
                if os.path.exists(FAILURE_LOG_FILE):
                    try:
                        with open(FAILURE_LOG_FILE, "r", encoding="utf-8") as f:
                            logs = json.load(f)
                    except Exception:
                        logs = []

                # Keep last 500 logs max
                now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
                logs.append({
                    "timestamp": now_str,
                    "provider": provider,
                    "model_id": model_id,
                    "success": success,
                    "error": error_msg[:300] if error_msg else ""
                })
                if len(logs) > 500:
                    logs = logs[-500:]

                try:
                    with open(FAILURE_LOG_FILE, "w", encoding="utf-8") as f:
                        json.dump(logs, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    print(f"[SelfImprover] Log save error: {e}")

            # Update live health stat
            health = self.strategy["provider_health"].setdefault(provider, {"success_rate": 100.0, "total_calls": 0, "failures": 0, "status": "HEALTHY"})
            health["total_calls"] += 1
            if not success:
                health["failures"] += 1

            if health["total_calls"] > 0:
                health["success_rate"] = round(((health["total_calls"] - health["failures"]) / health["total_calls"]) * 100, 1)

            if health["success_rate"] < 50.0 and health["total_calls"] >= 5:
                health["status"] = "DEGRADED"
            elif health["success_rate"] < 20.0 and health["total_calls"] >= 5:
                health["status"] = "CRITICAL"
            else:
                health["status"] = "HEALTHY"

            save_strategy(self.strategy)

        threading.Thread(target=_log, daemon=True).start()

    def analyze_and_optimize(self):
        """Analyze recent failures and dynamically re-order providers & adjust timeouts."""
        print("[SelfImprover] Running self-improvement cycle...")
        with _log_lock:
            if not os.path.exists(FAILURE_LOG_FILE):
                return
            try:
                with open(FAILURE_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                return

        if not logs:
            return

        # Calculate provider stats over the last 100 calls
        recent = logs[-100:]
        provider_stats = {}
        for entry in recent:
            p = entry["provider"]
            if p not in provider_stats:
                provider_stats[p] = {"total": 0, "success": 0, "errors": []}
            provider_stats[p]["total"] += 1
            if entry["success"]:
                provider_stats[p]["success"] += 1
            elif entry["error"]:
                provider_stats[p]["errors"].append(entry["error"])

        # Re-sort 'auto' provider order by success rate descending
        auto_list = self.strategy["provider_order"]["auto"]
        def get_score(p_name):
            st = provider_stats.get(p_name)
            if not st or st["total"] == 0:
                return 100.0
            return (st["success"] / st["total"]) * 100.0

        sorted_auto = sorted(auto_list, key=get_score, reverse=True)
        self.strategy["provider_order"]["auto"] = sorted_auto

        # If a provider consistently yields 400/404 or API changes, try auto-patching
        for p_name, st in provider_stats.items():
            if st["total"] >= 5 and (st["success"] / st["total"]) < 0.2:
                print(f"[SelfImprover] Provider {p_name} is severely failing. Triggering AI auto-patch...")
                self._attempt_auto_patch(p_name, st["errors"])

        save_strategy(self.strategy)
        print(f"[SelfImprover] Strategy optimized! Current provider order: {sorted_auto}")

    def _attempt_auto_patch(self, provider_name: str, recent_errors: list):
        """Uses Gemini AI to generate a python patch for a failing provider and auto-applies it."""
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return

        prompt = f"""You are an autonomous AI self-repair engineer for a Telegram Bot.
The AI provider '{provider_name}' is failing repeatedly with the following error messages:
{json.dumps(recent_errors[:5], indent=2)}

Please write a Python patch function or logic adjustment to fix or bypass this failure.
Return ONLY valid Python code block with comments explaining the fix."""

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
            res = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=15)
            res_json = res.json()
            if "candidates" in res_json and res_json["candidates"]:
                patch_code = res_json["candidates"][0]["content"]["parts"][0]["text"]
                timestamp = int(time.time())
                patch_filename = f"patch_{provider_name}_{timestamp}.py"
                patch_path = os.path.join(PATCHES_DIR, patch_filename)
                with open(patch_path, "w", encoding="utf-8") as f:
                    f.write(patch_code)
                print(f"[SelfImprover] Auto-generated and applied patch saved to {patch_path}! ✅")
        except Exception as e:
            print(f"[SelfImprover] Auto-patch generation failed: {e}")

    def start_background_loop(self, interval_seconds=600):
        """Runs the self-improvement analysis every 10 minutes continuously."""
        def _loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    self.analyze_and_optimize()
                except Exception as e:
                    print(f"[SelfImprover] Background loop error: {e}")

        threading.Thread(target=_loop, daemon=True).start()

# Global instance
improver = SelfImprover()
