import time
import json
import base64
import os
import urllib.request
import urllib.parse

CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
SCOPE = "https://www.googleapis.com/auth/youtube"

def main():
    print("Requesting device authorization code from Google...")
    data = {
        "client_id": CLIENT_ID,
        "scope": SCOPE
    }
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/device/code",
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req)
    resp_data = json.loads(resp.read().decode("utf-8"))

    device_code = resp_data["device_code"]
    user_code = resp_data["user_code"]
    verification_url = resp_data["verification_url"]
    interval = resp_data.get("interval", 5)
    expires_in = resp_data.get("expires_in", 1800)

    print("\n" + "=" * 60)
    print(f"👉 Please open: {verification_url}")
    print(f"👉 Enter code:  {user_code}")
    print("=" * 60 + "\n")
    print(f"Polling Google every {interval}s for authorization...")

    with open("oauth_prompt.txt", "w", encoding="utf-8") as f:
        f.write(f"URL: {verification_url}\nCODE: {user_code}\n")

    start_time = int(time.time() - 30)
    deadline = time.time() + expires_in

    token_data = None
    while time.time() < deadline:
        time.sleep(interval)
        token_req_data = {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        }
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=json.dumps(token_req_data).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        try:
            resp = urllib.request.urlopen(req)
            token_data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            try:
                err_json = json.loads(err_body)
                err_code = err_json.get("error")
                if err_code == "authorization_pending":
                    print(".", end="", flush=True)
                    continue
                elif err_code == "slow_down":
                    time.sleep(interval + 2)
                    continue
                else:
                    print(f"\n[X] OAuth error: {err_code}")
                    return
            except Exception:
                print(f"\n[X] HTTP Error {e.code}: {err_body}")
                return

    if not token_data or "access_token" not in token_data:
        print("\n[X] Authorization timed out or failed.")
        return

    print("\n[OK] Authorization successful!")

    # Format tokens for pytubefix
    cache_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), "bot_temp", "pytubefix_cache")
    os.makedirs(cache_dir, exist_ok=True)
    token_file = os.path.join(cache_dir, "tokens.json")

    pytube_tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", ""),
        "expires": start_time + token_data.get("expires_in", 3600)
    }

    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(pytube_tokens, f)

    token_json_str = json.dumps(pytube_tokens)
    encoded_token = base64.b64encode(token_json_str.encode("utf-8")).decode("utf-8")

    with open("yt_token_result.txt", "w", encoding="utf-8") as f:
        f.write(encoded_token)

    # Update .env
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    env_line = f"YT_OAUTH2_TOKEN={encoded_token}\n"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        updated = False
        for line in lines:
            if line.startswith("YT_OAUTH2_TOKEN="):
                new_lines.append(env_line)
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(env_line)
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print("Updated .env with YT_OAUTH2_TOKEN!")

    print("\n=======================================================")
    print("YT_OAUTH2_TOKEN successfully generated!")
    print(encoded_token)
    print("=======================================================")

if __name__ == "__main__":
    main()

