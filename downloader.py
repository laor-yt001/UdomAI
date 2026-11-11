# pyright: reportGeneralTypeIssues=false, reportOptionalMemberAccess=false, reportMissingImports=false, reportArgumentType=false, reportCallIssue=false, reportOptionalIterable=false, reportOptionalOperand=false
import os
import time
import uuid
import re
import json
import html
import urllib.request
import urllib.parse
import subprocess
import requests
import imageio_ffmpeg
from urllib.parse import urlparse
from utils import get_temp_dir

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def is_valid_video(f_path):
    if not os.path.exists(f_path) or os.path.getsize(f_path) < 10240:
        return False
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", f_path]
        res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, timeout=10)
        stderr_text = res.stderr.decode('utf-8', errors='ignore')
        # Reject ByteVC2 (bvc2) streams – they are unplayable by standard players/FFmpeg
        if "bvc2" in stderr_text or "Video: none" in stderr_text:
            return False
        return "Duration: N/A" not in stderr_text and "Duration:" in stderr_text
    except Exception:
        return False

def safe_print(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except Exception:
        try:
            msg = " ".join(str(a) for a in args)
            print(msg.encode('ascii', errors='replace').decode('ascii'), **kwargs)
        except Exception:
            pass

def clean_youtube_url(url):
    """Strips playlist and radio parameters from YouTube URLs."""
    if not url:
        return ""
    if 'youtube.com' in url or 'youtu.be' in url:
        m = re.search(r'(?:[?&]v=|youtu\.be/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})', url)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    return url

# ─────────────────────────────────────────────────────────
# URL classifier
# ─────────────────────────────────────────────────────────

def classify_url(url):
    u = url.lower()
    if 'youtube.com' in u or 'youtu.be' in u:
        return 'youtube'
    if 'hongguoduanju.com' in u or 'novelquickapp.com' in u:
        return 'hongguo'
    if 'iq.com' in u or 'iqiyi.com' in u:
        return 'iqiyi'
    if 'dailymotion.com' in u or 'dai.ly' in u:
        return 'dailymotion'
    if 'facebook.com' in u or 'fb.watch' in u or 'fb.com' in u:
        return 'facebook'
    if 'instagram.com' in u:
        return 'instagram'
    if 'twitter.com' in u or 'x.com' in u or 't.co' in u:
        return 'twitter'
    if 'tiktok.com' in u:
        return 'tiktok'
    if 'twitch.tv' in u:
        return 'twitch'
    if 'vimeo.com' in u:
        return 'vimeo'
    if 'reddit.com' in u or 'redd.it' in u:
        return 'reddit'
    if 'bilibili.com' in u or 'b23.tv' in u:
        return 'bilibili'
    if 'pinterest.com' in u or 'pin.it' in u:
        return 'pinterest'
    if 'linkedin.com' in u:
        return 'linkedin'
    if 'snapchat.com' in u:
        return 'snapchat'
    if 'streamable.com' in u:
        return 'streamable'
    if 'rumble.com' in u:
        return 'rumble'
    if 'odysee.com' in u or 'lbry.tv' in u:
        return 'odysee'
    if 'ok.ru' in u:
        return 'ok_ru'
    if 'vk.com' in u or 'vkvideo.ru' in u:
        return 'vk'
    if 'nicovideo.jp' in u or 'nico.ms' in u:
        return 'nicovideo'
    if 'weibo.com' in u:
        return 'weibo'
    if 'line.me' in u or 'linevoom.line.me' in u:
        return 'line'
    if 'wetv.vip' in u or 'v.qq.com' in u:
        return 'wetv'
    if 'mypikpak.com' in u or 'pikpak.com' in u:
        return 'pikpak'
    return 'generic'

# ─────────────────────────────────────────────────────────
# Cookies helpers
# ─────────────────────────────────────────────────────────

def _is_valid_cookie_file(file_path):
    """Checks whether the file exists and is recognized as a valid Netscape cookie file by yt-dlp."""
    if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) < 10:
        return False
    try:
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(file_path)
        jar.load()
        return len(jar) > 0
    except Exception as e:
        safe_print(f"[cookies] Cookie file at {file_path} failed validation ({e}). Discarding.")
        return False

def _sanitize_netscape_cookies(text):
    if not text:
        return ""
    stripped = text.strip()
    
    # Strip triple quotes and outer quotes that may wrap the env var/secret
    while True:
        if (stripped.startswith('"""') and stripped.endswith('"""')) or (stripped.startswith("'''") and stripped.endswith("'''")):
            stripped = stripped[3:-3].strip()
        elif (stripped.startswith('"') and stripped.endswith('"')) or (stripped.startswith("'") and stripped.endswith("'")):
            stripped = stripped[1:-1].strip()
        else:
            break

    # Auto-detect and convert JSON cookies (from Chrome extension / Cookie-Editor export)
    if (stripped.startswith('[') or stripped.startswith('{')) or ('"domain"' in stripped and '"name"' in stripped):
        try:
            import json
            data = json.loads(stripped)
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                out_lines = ["# Netscape HTTP Cookie File", "# Converted from JSON by UdomAI"]
                for c in data:
                    if not isinstance(c, dict): continue
                    domain = str(c.get("domain", "")).strip()
                    include_sub = "TRUE" if domain.startswith(".") else "FALSE"
                    path = str(c.get("path", "/")).strip()
                    secure = "TRUE" if c.get("secure") else "FALSE"
                    expiry = str(int(c.get("expirationDate") or c.get("expiry") or 0))
                    name = str(c.get("name", "")).strip()
                    val = str(c.get("value", "")).strip()
                    if name and domain:
                        out_lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expiry}\t{name}\t{val}")
                safe_print(f"[cookies] Successfully converted {len(out_lines)-2} JSON cookies to Netscape format.")
                return '\n'.join(out_lines)
        except Exception as e:
            safe_print(f"[cookies] JSON cookie conversion failed: {e}")

    lines = stripped.split('\n')
    out = ["# Netscape HTTP Cookie File", "# Generated by UdomAI"]
    for line in lines:
        l = line.strip()
        if not l:
            continue
        if l.startswith('#'):
            out.append(l)
            continue
        parts = re.split(r'\s+', l, maxsplit=6)
        if len(parts) == 7:
            # Strip accidental surrounding quotes from each field
            parts = [p.strip('"\'') for p in parts]
            if parts[0] and parts[5]: # domain and cookie name must be present
                out.append('\t'.join(parts))
        else:
            # Strictly discard malformed/non-7-part lines so yt-dlp never raises LoadError
            continue
            
    return '\n'.join(out)

def _get_cookies_file(site='youtube'):
    """
    Resolves the best available cookies file for any platform.
    Priority:
      1. Site-specific env var (COOKIES_YOUTUBE, COOKIES_INSTAGRAM, ...)
      2. Legacy YouTube-specific env vars (YOUTUBE_COOKIES, WWW.YOUTUBE.COM_COOKIES.TXT)
      3. Universal COOKIES_ALL env var
      4. Local site-specific file (youtube_cookies.txt, etc.)
      5. Local generic cookies.txt
      6. Auto-exported browser cookies (Chrome, Firefox, Edge, Brave)
      7. Render/Docker secret file (/etc/secrets/cookies.txt)
    """
    temp_dir = get_temp_dir()

    # 1. Site-specific env var (e.g. COOKIES_INSTAGRAM, COOKIES_FACEBOOK)
    env_key = f"COOKIES_{site.upper()}"
    env_val = os.environ.get(env_key)

    # 2. Legacy YouTube-specific env vars
    if not env_val and site == 'youtube':
        env_val = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("WWW.YOUTUBE.COM_COOKIES.TXT")

    # 3. Universal cookies env var for all sites
    if not env_val:
        env_val = os.environ.get("COOKIES_ALL")

    if env_val:
        env_val = env_val.replace('\\n', '\n').replace('\\t', '\t')
        env_val = _sanitize_netscape_cookies(env_val)
        if env_val and '\t' in env_val:
            writable = os.path.join(temp_dir, f'cookies_{site}.txt')
            try:
                with open(writable, 'w', encoding='utf-8') as f:
                    f.write(env_val)
                if _is_valid_cookie_file(writable):
                    safe_print(f"[cookies] Using valid env var {env_key} for {site}")
                    return writable
                else:
                    try:
                        os.remove(writable)
                    except Exception:
                        pass
            except Exception as e:
                safe_print(f"[cookies] Error writing env cookies: {e}")

    # 4. Local site-specific cookies file (e.g. youtube_cookies.txt, yt_cookies_for_render.txt)
    cookie_candidates = [
        f'{site}_cookies.txt',
        'yt_cookies_for_render.txt' if site == 'youtube' else None,
        'youtube_cookies.txt' if site == 'youtube' else None,
        'cookies.txt',
    ]
    for c_name in cookie_candidates:
        if not c_name: continue
        c_path = os.path.join(os.path.dirname(__file__), c_name)
        if _is_valid_cookie_file(c_path):
            safe_print(f"[cookies] Using validated local {c_name} for {site}")
            return c_path

    # 6. Auto-export from local browser (only when running locally with a GUI)
    auto_cookie = _auto_export_browser_cookies(site, temp_dir)
    if auto_cookie and _is_valid_cookie_file(auto_cookie):
        return auto_cookie

    # 7. Render/Docker secret file
    secret = '/etc/secrets/cookies.txt'
    if os.path.exists(secret):
        import shutil
        writable = os.path.join(temp_dir, f'cookies_{site}.txt')
        shutil.copyfile(secret, writable)
        safe_print(f"[cookies] Using Render secret cookies.txt for {site}")
        return writable

    return None


def _auto_export_browser_cookies(site, temp_dir):
    """
    Automatically exports cookies from the user's local browser using yt-dlp.
    Only works when running locally (not on headless servers).
    Tries Chrome, Chromium, Firefox, Edge, Brave in order.
    Returns path to exported cookies file, or None on failure.
    """
    if yt_dlp is None:
        return None
    # Skip if we're clearly on a headless server without a display
    is_headless = (
        os.environ.get('DISPLAY') is None and
        os.name != 'nt' and  # Windows always has no DISPLAY var
        os.environ.get('TERM_PROGRAM') is None
    )
    # Only skip on Linux servers without display, not on Windows/Mac
    if is_headless and os.name != 'nt':
        return None

    # Use USE_BROWSER_COOKIES env var if set, otherwise try all common browsers
    forced_browser = os.environ.get('USE_BROWSER_COOKIES', '').lower().strip()
    browsers_to_try = [forced_browser] if forced_browser else [
        'chrome', 'chromium', 'firefox', 'edge', 'brave', 'opera', 'safari'
    ]

    if site == 'youtube':
        domain = 'youtube.com'
    elif site == 'wetv':
        domain = 'wetv.vip'
    else:
        domain = site.replace('_', '.') + '.com'
    out_path = os.path.join(temp_dir, f'auto_cookies_{site}.txt')

    for browser in browsers_to_try:
        try:
            # Use yt-dlp's built-in cookie extraction
            import yt_dlp as _ydlp
            extract_opts = {
                'cookiesfrombrowser': (browser,),
                'cookiefile': out_path,
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'ignore_no_formats_error': True,
            }
            # We just want to dump cookies — use a dummy URL trigger
            with _ydlp.YoutubeDL(extract_opts) as ydl:
                ydl.cookiejar.save(out_path, ignore_discard=True, ignore_expires=True)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
                safe_print(f"[cookies] Auto-exported {browser} cookies for {site}")
                return out_path
        except Exception as e:
            safe_print(f"[cookies] Auto-export from {browser} failed: {e}")
            continue
    return None

# ─────────────────────────────────────────────────────────
# Dailymotion direct stream resolver
# ─────────────────────────────────────────────────────────

def resolve_dailymotion_stream(url):
    try:
        m = re.search(r'(?:video/|embed/video/|dai\\.ly/)([a-zA-Z0-9]+)', url)
        if m:
            vid = m.group(1)
            meta_url = f"https://www.dailymotion.com/player/metadata/video/{vid}"
            req = urllib.request.Request(meta_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                qualities = data.get('qualities', {})
                for q_key in ('auto', '1080', '720', '480', '380', '240'):
                    q_val = qualities.get(q_key, [])
                    if q_val and isinstance(q_val, list):
                        stream_url = q_val[0].get('url')
                        if stream_url:
                            return stream_url
    except Exception as e:
        safe_print(f"[dailymotion] Manifest resolution error: {e}")
    return url

# ─────────────────────────────────────────────────────────
# Facebook fallbacks
# ─────────────────────────────────────────────────────────

def _facebook_via_fdown(url):
    """Try fdown.net API to get raw CDN URL for a public Facebook video."""
    try:
        post_data = urllib.parse.urlencode({'URLz': url}).encode()
        req = urllib.request.Request(
            'https://fdown.net/download.php',
            data=post_data,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        for pat in [
            r'id="hdlink"[^>]*href="([^"]+)"',
            r'href="([^"]+)"[^>]*id="hdlink"',
            r'id="sdlink"[^>]*href="([^"]+)"',
            r'href="([^"]+)"[^>]*id="sdlink"',
            r'"url"\s*:\s*"(https://[^"]+\.mp4[^"]*)"',
        ]:
            m = re.search(pat, content)
            if m:
                return html.unescape(m.group(1))
    except Exception as e:
        safe_print(f"[facebook] fdown.net error: {e}")
    return None

def _facebook_via_getfvid(url):
    """Try getfvid.com as secondary fallback."""
    try:
        post_data = urllib.parse.urlencode({'url': url}).encode()
        req = urllib.request.Request(
            'https://www.getfvid.com/downloader',
            data=post_data,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': 'https://www.getfvid.com/',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        for pat in [
            r'href="(https://video[^"]+\.mp4[^"]*)"',
            r'"(https://[^"]+\.mp4[^"]*)"',
        ]:
            m = re.search(pat, content)
            if m:
                return html.unescape(m.group(1))
    except Exception as e:
        safe_print(f"[facebook] getfvid error: {e}")
    return None

def _facebook_via_snapsave(url):
    """Try snapsave.app as a third fallback for Facebook/Instagram."""
    try:
        post_data = urllib.parse.urlencode({'url': url, 'token': ''}).encode()
        req = urllib.request.Request(
            'https://snapsave.app/action.php',
            data=post_data,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': 'https://snapsave.app/',
                'Origin': 'https://snapsave.app',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        # Look for HD/SD download links
        for pat in [
            r'"url"\s*:\s*"(https://[^"]+)"',
            r'href="(https://[^"]+\.mp4[^"]*)"',
        ]:
            m = re.search(pat, content)
            if m:
                candidate = html.unescape(m.group(1))
                if '.mp4' in candidate or 'video' in candidate:
                    return candidate
    except Exception as e:
        safe_print(f"[facebook] snapsave error: {e}")
    return None

def _facebook_story_via_fvidgo(url):
    """Try fvidgo.com to get a direct video link for a Facebook story via Playwright."""
    try:
        from playwright.sync_api import sync_playwright
        safe_print(f"[facebook] Trying fvidgo API via Playwright for URL: {url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ignore_https_errors=True
            )
            page = context.new_page()
            
            page.goto('https://www.fvidgo.com/facebook-story-download/', wait_until='domcontentloaded', timeout=15000)
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', url)
            page.click('button:has-text("Download")')
            
            jwt_token = None
            try:
                with page.expect_response(lambda r: "hitube.io" in r.url, timeout=15000) as resp_info:
                    resp = resp_info.value
                    data = resp.json()
                    if "result" in data and data["result"].get("fbBos"):
                        for item in data["result"]["fbBos"]:
                            if item.get("url"):
                                jwt_token = item.get("url")
                                break
            except Exception as e:
                safe_print(f"[facebook] fvidgo timeout waiting for hitube: {e}")
                
            if jwt_token:
                jwt_url = f"https://api.hitube.io/st-tik/token/{jwt_token}"
                r = context.request.get(jwt_url, max_redirects=0, headers={"Referer": "https://www.fvidgo.com/"})
                loc = r.headers.get("location")
                if loc:
                    browser.close()
                    return loc
                if r.status == 200:
                    try:
                        resp_json = r.json()
                        if "url" in resp_json:
                            browser.close()
                            return resp_json["url"]
                    except:
                        pass
            browser.close()
    except Exception as e:
        safe_print(f"[facebook] fvidgo API error: {e}")
    return None

# ─────────────────────────────────────────────────────────
# Instagram fallback
# ─────────────────────────────────────────────────────────

def _instagram_via_snapinsta(url):
    """Try snapinsta.app for public Instagram reels/posts."""
    try:
        post_data = urllib.parse.urlencode({'url': url, 'lang': 'en'}).encode()
        req = urllib.request.Request(
            'https://snapinsta.app/action.php',
            data=post_data,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': 'https://snapinsta.app/',
                'Origin': 'https://snapinsta.app',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        for pat in [
            r'"url"\s*:\s*"(https://[^"]+)"',
            r'href="(https://[^"]*instagram[^"]*\.mp4[^"]*)"',
            r'href="(https://[^"]*\.mp4[^"]*)"',
        ]:
            m = re.search(pat, content)
            if m:
                candidate = html.unescape(m.group(1))
                if '.mp4' in candidate or 'video' in candidate:
                    return candidate
    except Exception as e:
        safe_print(f"[instagram] snapinsta error: {e}")
    return None

def _instagram_via_igdownloader(url):
    """Try igdownloader.app as secondary fallback."""
    try:
        api_url = f"https://igdownloader.app/api/instagram?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://igdownloader.app/',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        # Structure varies — try common patterns
        for key in ('url', 'video_url', 'download_url'):
            val = data.get(key)
            if val and isinstance(val, str) and val.startswith('http'):
                return val
        medias = data.get('medias') or data.get('items') or []
        if medias and isinstance(medias, list):
            first = medias[0]
            if isinstance(first, dict):
                for key in ('url', 'video_url'):
                    val = first.get(key)
                    if val:
                        return val
    except Exception as e:
        safe_print(f"[instagram] igdownloader error: {e}")
    return None

# ─────────────────────────────────────────────────────────
# TikTok fallback
# ─────────────────────────────────────────────────────────

def _tiktok_via_tikmate(url):
    """Try tikmate.online API for TikTok videos without watermark."""
    try:
        api_url = "https://tikmate.online/api/lookup"
        post_data = urllib.parse.urlencode({'url': url}).encode()
        req = urllib.request.Request(api_url, data=post_data, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://tikmate.online/',
            'Origin': 'https://tikmate.online',
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        token = data.get('token') or data.get('id')
        if token:
            dl_url = f"https://tikmate.online/api/dl?token={token}&type=no_wm&hd=1"
            return dl_url
    except Exception as e:
        safe_print(f"[tiktok] tikmate error: {e}")
    return None

def _tiktok_via_ssstik(url):
    """Try ssstik.io as TikTok secondary fallback."""
    try:
        req_main = urllib.request.Request('https://ssstik.io/en', headers={
            'User-Agent': 'Mozilla/5.0',
        })
        with urllib.request.urlopen(req_main, timeout=10) as r:
            page = r.read().decode('utf-8', errors='ignore')
        tt_token = re.search(r'tt\s*=\s*"([^"]+)"', page)
        if not tt_token:
            return None
        post_data = urllib.parse.urlencode({
            'id': url,
            'locale': 'en',
            'tt': tt_token.group(1),
        }).encode()
        req = urllib.request.Request(
            'https://ssstik.io/abc?url=dl',
            data=post_data,
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://ssstik.io/en',
                'Origin': 'https://ssstik.io',
                'Content-Type': 'application/x-www-form-urlencoded',
                'HX-Request': 'true',
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode('utf-8', errors='ignore')
        # Extract HD/no-watermark link
        m = re.search(r'href="(https://[^"]+)"[^>]*>\s*Without watermark', content, re.IGNORECASE)
        if m:
            return html.unescape(m.group(1))
        m = re.search(r'href="(https://[^"]+\.mp4[^"]*)"', content)
        if m:
            return html.unescape(m.group(1))
    except Exception as e:
        safe_print(f"[tiktok] ssstik error: {e}")
    return None

# ─────────────────────────────────────────────────────────
# Twitter/X fallback
# ─────────────────────────────────────────────────────────

def _twitter_via_twitsave(url):
    """Try twitsave for Twitter/X video downloads."""
    try:
        api_url = f"https://twitsave.com/info?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        videos = data.get('videos') or []
        if videos:
            # Sort by quality descending
            videos.sort(key=lambda v: v.get('bitrate', 0), reverse=True)
            return videos[0].get('url')
    except Exception as e:
        safe_print(f"[twitter] twitsave error: {e}")
    return None

# ─────────────────────────────────────────────────────────
# YouTube-specific server-safe fallbacks (Invidious + OAuth2)
# ─────────────────────────────────────────────────────────

# Public Invidious instances — rotated to avoid rate limits.
# Invidious is an open-source YouTube frontend running on non-datacenter IPs.
# Even when YouTube blocks Render's IP, Invidious acts as a trusted proxy.
_INVIDIOUS_INSTANCES = [
    'https://inv.nadeko.net',
    'https://invidious.nerdvpn.de',
    'https://invidious.privacyredirect.com',
    'https://iv.kkith.me',
    'https://invidious.fdn.fr',
    'https://invidious.projectsegfau.lt',
    'https://vid.puffyan.us',
    'https://yt.artemislena.eu',
    'https://invidious.slipfox.xyz',
    'https://invidious.lunar.icu',
]

def _youtube_via_invidious(url, is_audio=False):
    """
    Resolve a YouTube video URL through public Invidious instances.
    Returns a direct CDN stream URL string, or None on failure.
    Invidious instances run on academic/residential IPs that YouTube trusts,
    so this works even when Render/cloud IPs are blocked by YouTube bot-guard.
    """
    # Extract video ID
    vid_id = None
    m = re.search(r'(?:v=|youtu\.be/|embed/|/v/|/shorts/)([a-zA-Z0-9_-]{11})', url)
    if m:
        vid_id = m.group(1)
    if not vid_id:
        return None

    import random
    instances = _INVIDIOUS_INSTANCES[:]
    random.shuffle(instances)  # avoid hammering same instance

    for instance in instances:
        try:
            api_url = f"{instance}/api/v1/videos/{vid_id}?fields=adaptiveFormats,formatStreams"
            req = urllib.request.Request(api_url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Accept': 'application/json',
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8', errors='ignore'))

            if is_audio:
                # Prefer opus/webm audio streams
                audio_formats = [
                    f for f in data.get('adaptiveFormats', [])
                    if f.get('type', '').startswith('audio/')
                ]
                audio_formats.sort(key=lambda f: int(f.get('bitrate', 0)), reverse=True)
                if audio_formats:
                    url_val = audio_formats[0].get('url')
                    if url_val:
                        safe_print(f"[invidious] Got audio stream from {instance}")
                        return url_val
            else:
                # Try adaptive formats first (better quality), then legacy streams
                video_formats = [
                    f for f in data.get('adaptiveFormats', [])
                    if f.get('type', '').startswith('video/')
                    and 'mp4' in f.get('type', '')
                ]
                video_formats.sort(key=lambda f: int(f.get('height', 0) or 0), reverse=True)

                # Legacy combined streams (video+audio in one file) — easier to use
                legacy = data.get('formatStreams', [])
                legacy_mp4 = [f for f in legacy if 'mp4' in f.get('type', '')]
                legacy_mp4.sort(key=lambda f: int(f.get('resolution', '0p').replace('p', '') or 0), reverse=True)

                # Prefer legacy combined (no need to merge) for simplicity
                stream = (legacy_mp4 or video_formats)
                if stream:
                    url_val = stream[0].get('url')
                    if url_val:
                        safe_print(f"[invidious] Got video stream from {instance} ({stream[0].get('resolution', '?')} / {stream[0].get('qualityLabel', '?')})")
                        return url_val
        except Exception as e:
            safe_print(f"[invidious] {instance} failed: {type(e).__name__}: {e}")
            continue
    return None



# ─────────────────────────────────────────────────────────
# Generic all-in-one scraper APIs
# ─────────────────────────────────────────────────────────

def _via_cobalt_api(url):
    """
    Try the public Cobalt API (free, supports 800+ sites).
    Supports: YouTube, Twitter, TikTok, Instagram, Reddit, Vimeo, Twitch clips, etc.
    """
    try:
        api_url = "https://api.cobalt.tools/api/json"
        payload = {"url": url, "vQuality": "max", "aFormat": "mp3", "isAudioMuted": False}
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0',
            }
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        status = data.get('status')
        if status in ('redirect', 'stream'):
            return data.get('url') or data.get('stream')
        if status == 'picker':
            picks = data.get('picker', [])
            if picks:
                return picks[0].get('url')
    except Exception as e:
        safe_print(f"[cobalt] API error: {e}")
    return None

def _via_savefrom_api(url):
    """
    Try savefrom.net API as a generic multi-site fallback.
    """
    try:
        api_url = f"https://worker.sf-tools.com/savefrom.php?sf_url={urllib.parse.quote(url)}"
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://en.savefrom.net/',
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8', errors='ignore'))
        links = data.get('url') or []
        if isinstance(links, list):
            # Sort best quality first
            links.sort(key=lambda l: int(l.get('size', 0)), reverse=True)
            for link in links:
                dl_url = link.get('url')
                if dl_url and dl_url.startswith('http'):
                    return dl_url
        elif isinstance(links, str) and links.startswith('http'):
            return links
    except Exception as e:
        safe_print(f"[savefrom] API error: {e}")
    return None

# ─────────────────────────────────────────────────────────
# Direct file download
# ─────────────────────────────────────────────────────────

def download_direct_file(url, progress_callback=None):
    """Downloads a file directly using requests (for direct CDN .mp4 links)."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
        }
        response = requests.get(url, stream=True, headers=headers, timeout=60)
        response.raise_for_status()
        total = int(response.headers.get('Content-Length', 0))
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path) or f"{uuid.uuid4()}.mp4"
        temp_dir = get_temp_dir()
        filepath = os.path.join(temp_dir, f"{uuid.uuid4()}_{filename}")
        downloaded = 0
        start_time = time.time()
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total > 0:
                        elapsed = time.time() - start_time
                        speed = (downloaded / elapsed) if elapsed > 0 else 0
                        eta = ((total - downloaded) / speed) if speed > 0 else 0
                        dl_mb = downloaded / 1024 / 1024
                        tot_mb = total / 1024 / 1024
                        spd_mb = speed / 1024 / 1024
                        progress_callback(f"{dl_mb:.1f}MB/{tot_mb:.1f}MB, ETA {eta:.0f}s, {spd_mb:.1f}MB/s downloading...")
        return filepath
    except Exception as e:
        safe_print(f"[direct] Download error: {e}")
        return None

# ─────────────────────────────────────────────────────────
# extract_link_info  (metadata only)
# ─────────────────────────────────────────────────────────

def extract_link_info(url):
    """Extracts metadata (title, thumbnail URL, resolutions) from a video link using yt-dlp."""
    try:
        site = classify_url(url)
        opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'skip_download': True,
            'ignore_no_formats_error': True,
            'source_address': '0.0.0.0',
            'extractor_args': {'youtube': ['client=tv']},
            'socket_timeout': 15,
            'retries': 10,
        }
        wg_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or ('http://127.0.0.1:1080' if os.environ.get('USE_VPN_PROXY') == 'true' else None)
        if wg_proxy:
            opts['proxy'] = wg_proxy
            
        if site == 'youtube':
            url = clean_youtube_url(url)
        cookies_file = _get_cookies_file(site)
        if cookies_file:
            opts['cookiefile'] = cookies_file
        elif os.environ.get('USE_BROWSER_COOKIES'):
            opts['cookiesfrombrowser'] = (os.environ.get('USE_BROWSER_COOKIES').lower(),)
        info = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            err_str = str(e).lower()
            if 'cookie' in err_str or 'loaderror' in err_str:
                safe_print(f"[extract_link_info] Cookie failure ({e}), retrying without cookies...")
                opts.pop('cookiefile', None)
                opts.pop('cookiesfrombrowser', None)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
            else:
                raise e

        if not info:
            return None
        title = info.get('title') or "Video"
        thumbnail = info.get('thumbnail')
        formats = info.get('formats', [])
        heights = sorted(list(set(
            f.get('height') for f in formats
            if isinstance(f.get('height'), int) and f.get('height') >= 144
        )))
        return {'title': title, 'thumbnail': thumbnail, 'heights': heights}
    except Exception as e:
        safe_print(f"[extract_link_info] Error for {url}: {e}")
        return None

# ─────────────────────────────────────────────────────────
# Main download_media entry point
# ─────────────────────────────────────────────────────────

def download_media(url, is_audio=False, progress_callback=None, max_height=None):
    """
    Downloads media from any URL.
    Supports 1000+ sites via yt-dlp, plus custom fallbacks for Facebook,
    Instagram, TikTok, Twitter/X, and a Cobalt multi-site API.
    For private/restricted videos: place cookies.txt in the bot root or set
    environment variables COOKIES_<SITE> or COOKIES_ALL.
    Returns file path string, or 'TOO_LARGE', 'BOT_DETECTED', or 'ERROR: ...'
    """
    temp_dir = get_temp_dir()
    file_id = str(uuid.uuid4())
    site = classify_url(url)

    original_url = url
    # Resolve Facebook share redirect URLs
    if 'facebook.com/share' in url.lower():
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(url, timeout=15, allow_redirects=True, verify=False)
            resolved_url = resp.url
            if 'login.php' in resolved_url:
                import urllib.parse
                parsed = urllib.parse.urlparse(resolved_url)
                qs = urllib.parse.parse_qs(parsed.query)
                if 'next' in qs:
                    resolved_url = urllib.parse.unquote(qs['next'][0])
            url = resolved_url
            m = re.search(r'/stories/(\d+)/([^/?]+)', url)
            if m:
                url = f"https://www.facebook.com/story.php?story_fbid={m.group(1)}&id={m.group(2)}"
                safe_print(f"[facebook] Rewrote story URL for yt-dlp: {url}")
            site = classify_url(url)
        except Exception as e:
            safe_print(f"[facebook] Redirect resolve error: {e}")

    # Clean up YouTube URL
    if site == 'youtube':
        target_url = clean_youtube_url(url)
        if progress_callback:
            progress_callback(f"Sending link to https://v27.www-y2mate.com/ ...")
        try:
            import urllib.request
            import urllib.parse
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req_data = urllib.parse.urlencode({'q': target_url}).encode()
            req = urllib.request.Request(
                'https://v27.www-y2mate.com/search/',
                data=req_data,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            urllib.request.urlopen(req, context=ctx, timeout=5)
            if progress_callback:
                progress_callback(f"downloading...")
        except Exception as e:
            safe_print(f"[y2mate] Failed to send to y2mate: {e}")
    elif site == 'dailymotion':
        m = re.search(r'(?:video/|embed/video/|dai\\.ly/)([a-zA-Z0-9]+)', url)
        if m:
            target_url = f"https://www.dailymotion.com/video/{m.group(1)}"
        else:
            target_url = url
    else:
        target_url = url

    MAX_SIZE_BYTES = 1950 * 1024 * 1024  # 1950 MB safety limit

    if site == 'hongguo':
        safe_print(f"[hongguo] Handling Hongguo download for {url}")
        try:
            from plugins.hongguo_yt_downloader import _resolve_hongguo_url, _fetch_hongguo_series_sync, _download_hongguo_batch_sync
            clean_url = _resolve_hongguo_url(url)
            series = _fetch_hongguo_series_sync(clean_url)
            episodes = series.get("episodes", [])
            if not episodes:
                return "ERROR: No episodes found for this Hongguo series."
            target_ep = episodes[0]
            m_vid = re.search(r'(?:[?&](?:vid|video_id)=|/player/\d+/|/video/)(\d+)', clean_url)
            if m_vid:
                target_vid = m_vid.group(1)
                for ep in episodes:
                    if str(ep.get("vid")) == target_vid:
                        target_ep = ep
                        break
            if progress_callback:
                progress_callback(f"Downloading Hongguo {target_ep.get('title', 'EP01')}...")
            res = _download_hongguo_batch_sync(clean_url, [target_ep], temp_dir)
            results = res.get("results", [])
            for r in results:
                if r.get("ok") and r.get("path") and os.path.exists(r["path"]):
                    return _finalize(r["path"], is_audio, MAX_SIZE_BYTES, progress_callback)
            err_msg = results[0].get("error") if results else "Hongguo download failed."
            return f"ERROR: {err_msg}"
        except Exception as hg_err:
            safe_print(f"[hongguo] Download failed: {hg_err}")
            return f"ERROR: {hg_err}"

    if site == 'iqiyi':
        safe_print(f"[iqiyi] Handling iQIYI download for {url}")
        try:
            from plugins.iqiyi_downloader import _resolve_iqiyi_url, _fetch_iqiyi_series_sync, _download_iqiyi_batch_sync
            clean_url = _resolve_iqiyi_url(url)
            series = _fetch_iqiyi_series_sync(clean_url)
            episodes = series.get("episodes", [])
            if not episodes:
                return "ERROR: No episodes found for this iQIYI series."
            target_ep = episodes[0]
            # Match specific episode if URL is a play link with qipuId
            m_vid = re.search(r'(?:/play/|(?:[?&]vid=))([a-zA-Z0-9_-]+)', clean_url)
            if m_vid:
                raw_vid = m_vid.group(1).split('-')[-1]
                for ep in episodes:
                    if str(ep.get("vid")) == raw_vid or raw_vid in str(ep.get("playUrl", "")):
                        target_ep = ep
                        break
            if progress_callback:
                progress_callback(f"Downloading iQIYI {target_ep.get('title', 'EP01')}...")
            res = _download_iqiyi_batch_sync(clean_url, [target_ep], temp_dir)
            results = res.get("results", [])
            for r in results:
                if r.get("ok") and r.get("path") and os.path.exists(r["path"]):
                    return _finalize(r["path"], is_audio, MAX_SIZE_BYTES, progress_callback)
            err_msg = results[0].get("error") if results else "iQIYI download failed."
            return f"ERROR: {err_msg}"
        except Exception as iq_err:
            safe_print(f"[iqiyi] Download failed: {iq_err}")
            return f"ERROR: {iq_err}"

    if site == 'wetv':
        safe_print(f"[wetv] Handling WeTV download for {url}")
        try:
            from plugins.wetv_downloader import _resolve_wetv_url, _fetch_wetv_series_sync, _download_wetv_batch_sync
            clean_url = _resolve_wetv_url(url)
            series = _fetch_wetv_series_sync(clean_url)
            episodes = series.get("episodes", [])
            if not episodes:
                return "ERROR: No episodes found for this WeTV series."
            target_ep = episodes[0]
            # Match specific episode if URL has episode vid
            m_vid = re.search(r'/play/[^/]+/([a-zA-Z0-9]+)', clean_url)
            if m_vid:
                target_vid = m_vid.group(1).split('-')[0]
                for ep in episodes:
                    if str(ep.get("vid")) == target_vid:
                        target_ep = ep
                        break
            if progress_callback:
                progress_callback(f"Downloading WeTV {target_ep.get('title', 'EP01')}...")
            res = _download_wetv_batch_sync(clean_url, [target_ep], temp_dir)
            results = res.get("results", [])
            for r in results:
                if r.get("ok") and r.get("path") and os.path.exists(r["path"]):
                    return _finalize(r["path"], is_audio, MAX_SIZE_BYTES, progress_callback)
            err_msg = results[0].get("error") if results else "WeTV download failed."
            return f"ERROR: {err_msg}"
        except Exception as wetv_err:
            safe_print(f"[wetv] Download failed: {wetv_err}")
            return f"ERROR: {wetv_err}"

    last_yt_hook_t = [0.0]
    def yt_dlp_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            now = time.time()
            if now - last_yt_hook_t[0] < 1.5:
                return
            last_yt_hook_t[0] = now
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            if total > 0:
                speed = d.get('speed', 0)
                speed_str = f"{speed / 1024 / 1024:.1f}MB/s" if speed else "..."
                eta = d.get('eta', 0)
                downloaded_mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                progress_callback(f"⬇️ {downloaded_mb:.1f}MB/{total_mb:.1f}MB ({downloaded*100/total:.0f}%), ETA {eta}s, {speed_str}")
            else:
                progress_callback(f"⬇️ {downloaded / 1024 / 1024:.1f}MB downloading...")

    def _find_downloaded():
        """Return the first valid downloaded file matching our file_id."""
        for f in sorted(os.listdir(temp_dir)):
            if f.startswith(file_id) and not f.endswith(('.part', '.ytdl', '.tmp')):
                fp = os.path.join(temp_dir, f)
                if is_valid_video(fp):
                    return fp
        return None

    cookie_disabled = False

    def _build_ydl_opts(extra=None):
        """Build the base yt-dlp options dict."""
        opts = {
            'outtmpl': os.path.join(temp_dir, f'{file_id}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'noplaylist': True,
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'progress_hooks': [yt_dlp_hook] if progress_callback else [],
            'geo_bypass': True,
            'geo_bypass_country': 'US',
            'source_address': '0.0.0.0',
            'extractor_args': {'youtube': ['client=tv']},
            'socket_timeout': 30,
            'retries': 10,
            'fragment_retries': 10,
            'retry_sleep_functions': {'http': lambda n: 5},
            'postprocessor_args': [
                '-metadata', 'title=', '-metadata', 'artist=',
                '-metadata', 'album=', '-metadata', 'comment=', '-metadata', 'description='
            ],
        }
        
        wg_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or ('http://127.0.0.1:1080' if os.environ.get('USE_VPN_PROXY') == 'true' else None)
        if wg_proxy:
            opts['proxy'] = wg_proxy

        # Apply cookies if available
        if not cookie_disabled:
            cookies_file = _get_cookies_file(site)
            if cookies_file:
                opts['cookiefile'] = cookies_file
                safe_print(f"[yt-dlp] Using cookies for site: {site}")
            elif os.environ.get('USE_BROWSER_COOKIES'):
                browser = os.environ.get('USE_BROWSER_COOKIES').lower()
                opts['cookiesfrombrowser'] = (browser,)
                safe_print(f"[yt-dlp] Using cookies from {browser} browser fallback")

        # Format selection
        if is_audio:
            opts['format'] = 'bestaudio/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        elif max_height:
            h = int(max_height)
            opts['format'] = f'bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best'
            opts['merge_output_format'] = 'mp4'
        else:
            opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
            opts['merge_output_format'] = 'mp4'

        # Site-specific headers and extractor args
        chrome_ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        if site == 'youtube':
            opts['http_headers'] = {
                'User-Agent': chrome_ua,
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Sec-Fetch-Mode': 'navigate',
            }
            opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['android', 'web'],
                }
            }
        elif site == 'tiktok':
            opts['http_headers'] = {'User-Agent': chrome_ua}
            opts['extractor_args'] = {'tiktok': {'api_hostname': 'api22-normal-c-alisg.tiktokv.com'}}
        elif site in ('twitter', 'instagram', 'facebook'):
            opts['http_headers'] = {'User-Agent': chrome_ua}
        else:
            opts['http_headers'] = {'User-Agent': chrome_ua}

        if extra:
            opts.update(extra)
        return opts

    def _run_ydl(run_url, opts):
        """Run yt-dlp with given options. Returns path or raises."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(run_url, download=True)
            return 'OK', _find_downloaded()

    # ── YouTube multi-client fallback chain (2025/2026 Anti-BotGuard) ──
    # Note: Modern YouTube serves SABR/missing URLs if configs/webpage are skipped for mobile clients.
    # Android client without skipping webpage/configs successfully fetches streamable URLs without bot verification.
    YOUTUBE_FALLBACK_CLIENTS = [
        (['android'],                    []),                      # Stage 1: Android native client (full configs)
        (['android', 'web'],             []),                      # Stage 2: Android + Web fallback
        (['android_vr', 'mweb', 'ios', 'tv'], []),                # Stage 3: Multi-client rotation
        (['mweb'],                       []),                      # Stage 4: Mobile web
        (['web_creator'],                []),                      # Stage 5: Creator dashboard client
        (['web_embedded'],               []),                      # Stage 6: Embedded web player
        (['android'],                    ['webpage', 'configs']),  # Stage 7: Android fast
        (['android_vr'],                 ['webpage', 'configs']),  # Stage 8: Android VR
        (['tv', 'ios'],                  []),                      # Stage 9: TV+iOS combo
    ]

    # ─────────────────────────────────────────────────────────────
    # Step 1: Primary yt-dlp attempt (Uses Android/Web client + local cookies)
    # ─────────────────────────────────────────────────────────────
    primary_error = None
    downloaded_file = None
    try:
        opts = _build_ydl_opts()
        status, downloaded_file = _run_ydl(target_url, opts)
        if status == 'TOO_LARGE':
            return 'TOO_LARGE'
        if downloaded_file:
            safe_print(f"[yt-dlp] Primary download succeeded: {downloaded_file}")
    except Exception as e:
        primary_error = e
        safe_print(f"[yt-dlp] Primary failed: {e}")
        err_str = str(e).lower()
        if 'cookie' in err_str or 'loaderror' in err_str:
            safe_print(f"[yt-dlp] Cookie error detected on primary ({e}). Retrying primary without cookies...")
            cookie_disabled = True
            try:
                opts_nocookies = _build_ydl_opts()
                status, downloaded_file = _run_ydl(target_url, opts_nocookies)
                if status == 'TOO_LARGE':
                    return 'TOO_LARGE'
                if downloaded_file:
                    safe_print(f"[yt-dlp] Primary retry without cookies succeeded: {downloaded_file}")
            except Exception as e2:
                primary_error = e2
                safe_print(f"[yt-dlp] Primary retry without cookies failed: {e2}")
        # Check if file was written despite the error (common with postprocessor errors)
        if not downloaded_file:
            downloaded_file = _find_downloaded()

    if downloaded_file:
        # Success path — apply size check and audio conversion
        return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # ─────────────────────────────────────────────────────────────
    # Step 2: Site-specific fallbacks
    # ─────────────────────────────────────────────────────────────

    # ── YouTube: client rotation (YTDLnis-style) ─────────────────
    if site == 'youtube':
        safe_print("[youtube] Trying YTDLnis-style client rotation fallback chain...")
        for i, (clients, skip) in enumerate(YOUTUBE_FALLBACK_CLIENTS):
            safe_print(f"[youtube] Stage {i+1}: clients={clients} skip={skip}")
            try:
                extractor_args = {'player_client': clients}
                if skip:
                    extractor_args['player_skip'] = skip
                fb_opts = _build_ydl_opts({'extractor_args': {'youtube': extractor_args}})
                status, downloaded_file = _run_ydl(target_url, fb_opts)
                if status == 'TOO_LARGE':
                    return 'TOO_LARGE'
                if downloaded_file:
                    safe_print(f"[youtube] Stage {i+1} succeeded")
                    return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)
            except Exception as fb_e:
                safe_print(f"[youtube] Stage {i+1} failed: {fb_e}")
                err_fb = str(fb_e).lower()
                if 'cookie' in err_fb or 'loaderror' in err_fb:
                    cookie_disabled = True
                downloaded_file = _find_downloaded()
                if downloaded_file:
                    return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)

        # ── PO Token via bgutil-ytdlp-pot-provider ────────────────
        safe_print("[youtube] Trying PO Token via bgutil provider...")
        try:
            pot_opts = _build_ydl_opts({
                'extractor_args': {
                    'youtube': {
                        'player_client': ['tv', 'ios'],
                        'player_skip': ['webpage', 'configs'],
                    },
                    'youtubepot-bgutilhttp': {
                        'base_url': [os.environ.get('BGUTIL_POT_URL', 'http://localhost:4416')],
                    },
                },
                'allow_unplayable_formats': False,
            })
            status, downloaded_file = _run_ydl(target_url, pot_opts)
            if status == 'TOO_LARGE':
                return 'TOO_LARGE'
            if downloaded_file:
                safe_print("[youtube] PO Token download succeeded")
                return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)
        except Exception as pot_e:
            safe_print(f"[youtube] PO Token attempt failed: {pot_e}")
            downloaded_file = _find_downloaded()
            if downloaded_file:
                return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)

        # ── Cobalt API as YouTube-specific final fallback ─────────
        safe_print("[youtube] Trying Cobalt API as YouTube fallback...")
        cobalt_yt_url = _via_cobalt_api(target_url)
        if cobalt_yt_url:
            dl_file = download_direct_file(cobalt_yt_url, progress_callback)
            if dl_file and is_valid_video(dl_file):
                new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                try:
                    os.rename(dl_file, new_path)
                    dl_file = new_path
                except Exception:
                    pass
                return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

        # ── Invidious API (Render-safe YouTube proxy) ─────────────
        safe_print("[youtube] Trying Invidious API (server-safe proxy)...")
        invidious_url = _youtube_via_invidious(target_url, is_audio=False)
        if invidious_url:
            dl_file = download_direct_file(invidious_url, progress_callback)
            if dl_file and is_valid_video(dl_file):
                new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                try:
                    os.rename(dl_file, new_path)
                    dl_file = new_path
                except Exception:
                    pass
                safe_print("[youtube] Invidious download succeeded")
                return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

        # ── pytubefix fallback ──
        safe_print("[youtube] Trying pytubefix fallback...")
        try:
            from pytubefix import YouTube
            yt = None
            pt_start_time = time.time()

            def pt_progress(stream, chunk, bytes_remaining):
                try:
                    if not progress_callback: return
                    total_size = getattr(stream, 'filesize', 0) or 0
                    bytes_downloaded = total_size - bytes_remaining if total_size > 0 else 0
                    
                    elapsed = time.time() - pt_start_time
                    speed = (bytes_downloaded / elapsed) if elapsed > 0 else 0
                    eta = (bytes_remaining / speed) if speed > 0 else 0
                        
                    downloaded_mb = bytes_downloaded / 1024 / 1024
                    total_mb = total_size / 1024 / 1024
                    speed_mb = speed / 1024 / 1024
                    
                    if total_size > 0:
                        progress_callback(f"{downloaded_mb:.1f}MB/{total_mb:.1f}MB, ETA {eta:.0f}s, {speed_mb:.1f}MB/s downloading...")
                    else:
                        progress_callback(f"{downloaded_mb:.1f}MB downloading...")
                except Exception as pt_err:
                    safe_print(f"[pt_progress error] {pt_err}")

            # Setup OAuth2 from environment if provided
            use_oauth = False
            oauth_token = os.environ.get('YT_OAUTH2_TOKEN')
            try:
                import pytubefix
                import pytubefix.innertube
                cache_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'bot_temp', 'pytubefix_cache')
                os.makedirs(cache_dir, exist_ok=True)
                pytubefix.innertube._cache_dir = cache_dir
                pytubefix.innertube._token_file = os.path.join(cache_dir, 'tokens.json')
                
                if oauth_token:
                    import base64
                    token_file = os.path.join(cache_dir, 'tokens.json')
                    decoded_token = base64.b64decode(oauth_token).decode('utf-8')
                    with open(token_file, 'w', encoding='utf-8') as tf:
                        tf.write(decoded_token)
                    use_oauth = True
                    safe_print("[youtube] Injected OAuth2 token from YT_OAUTH2_TOKEN env var")
            except Exception as setup_e:
                safe_print(f"[youtube] Failed to setup pytubefix cache / YT_OAUTH2_TOKEN: {setup_e}")

            wg_proxy = os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or ('http://127.0.0.1:1080' if os.environ.get('USE_VPN_PROXY') == 'true' else None)
            proxy_dict = {"http": wg_proxy, "https": wg_proxy} if wg_proxy else None
            # If WireGuard VPN proxy is configured, strictly enforce it so server IP is never used
            proxy_attempts = [proxy_dict] if proxy_dict else [None]
            
            for client in ['TV', 'IOS', 'MWEB', 'ANDROID_VR', 'WEB_CREATOR', 'ANDROID', 'WEB']:
                for current_proxy in proxy_attempts:
                    try:
                        yt = YouTube(
                            target_url, 
                            client=client, 
                            use_po_token=False, 
                            on_progress_callback=pt_progress,
                            use_oauth=use_oauth,
                            allow_oauth_cache=use_oauth,
                            proxies=current_proxy
                        )
                        _ = yt.title  # probe — raises if blocked
                        _ = yt.streams # probe stream fetching
                        safe_print(f"[youtube] pytubefix succeeded with client={client} (proxy={'yes' if current_proxy else 'no'})")
                        break
                    except Exception as probe_e:
                        safe_print(f"[youtube] pytubefix probe {client} (proxy={'yes' if current_proxy else 'no'}) failed: {probe_e}")
                        yt = None
                        continue
                if yt is not None:
                    break
            
            if yt is not None:
                uid_name = f"{uuid.uuid4().hex}"
                if is_audio:
                    ys = yt.streams.get_audio_only()
                    if ys:
                        out_file = ys.download(output_path=temp_dir, filename=f"{uid_name}.m4a")
                        from pydub import AudioSegment
                        audio = AudioSegment.from_file(out_file)
                        mp3_file = os.path.join(temp_dir, f"{uid_name}.mp3")
                        audio.export(mp3_file, format='mp3')
                        try: os.remove(out_file)
                        except: pass
                        return mp3_file
                else:
                    v_stream = None
                    if max_height:
                        res_streams = yt.streams.filter(type="video").order_by('resolution').desc()
                        for s in res_streams:
                            try:
                                res_val = int(''.join(filter(str.isdigit, getattr(s, 'resolution', '0'))))
                                if res_val > 0 and res_val <= max_height:
                                    v_stream = s
                                    break
                            except: pass
                    if not v_stream:
                        v_stream = yt.streams.get_highest_resolution()
                    if not v_stream:
                        v_stream = yt.streams.filter(type="video").first()
                        
                    if v_stream:
                        a_stream = yt.streams.get_audio_only()
                        if v_stream.includes_audio_track:
                            ext = getattr(v_stream, 'subtype', 'mp4')
                            out_file = v_stream.download(output_path=temp_dir, filename=f"{uid_name}.{ext}")
                            return out_file
                        elif a_stream:
                            if progress_callback: progress_callback("Downloading video (merging required)...")
                            v_path = v_stream.download(output_path=temp_dir, filename=f"temp_v_{uid_name}.mp4")
                            if progress_callback: progress_callback("Downloading audio...")
                            a_path = a_stream.download(output_path=temp_dir, filename=f"temp_a_{uid_name}.m4a")
                            
                            if progress_callback: progress_callback("Merging audio and video (this may take a moment)...")
                            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                            out_path = os.path.join(temp_dir, f"{uid_name}.mp4")
                            subprocess.run([ffmpeg_exe, '-y', '-i', v_path, '-i', a_path, '-c:v', 'copy', '-c:a', 'aac', out_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            
                            try: os.remove(v_path)
                            except: pass
                            try: os.remove(a_path)
                            except: pass
                            return out_path
                        else:
                            ext = getattr(v_stream, 'subtype', 'mp4')
                            out_file = v_stream.download(output_path=temp_dir, filename=f"{uid_name}.{ext}")
                            return out_file
        except Exception as pt_e:
            safe_print(f"[youtube] pytubefix failed: {pt_e}")

        return 'BOT_DETECTED'

    # ── Dailymotion: direct manifest ─────────────────────────────
    elif site == 'dailymotion':
        safe_print("[dailymotion] Trying direct manifest fallback...")
        m3u8_url = resolve_dailymotion_stream(url)
        if m3u8_url and m3u8_url != url:
            try:
                fb_opts = _build_ydl_opts({'format': 'best', 'merge_output_format': 'mp4'})
                status, downloaded_file = _run_ydl(m3u8_url, fb_opts)
                if downloaded_file:
                    return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)
            except Exception as dm_e:
                safe_print(f"[dailymotion] Manifest fallback failed: {dm_e}")

    # ── Facebook: fdown → getfvid → snapsave ─────────────────────
    elif site == 'facebook':
        safe_print("[facebook] Trying web scraper fallbacks...")
        # Story link handling via fvidgo (share or direct story URLs)
        if 'facebook.com/share' in original_url.lower() or '/stories/' in original_url.lower():
            dl_url = _facebook_story_via_fvidgo(original_url)
            if dl_url:
                safe_print("[facebook] fvidgo story returned a link, downloading directly...")
                dl_file = download_direct_file(dl_url, progress_callback)
                if dl_file and is_valid_video(dl_file):
                    # Move to project downloads folder
                    project_dir = os.path.join(os.path.dirname(__file__), 'downloads')
                    os.makedirs(project_dir, exist_ok=True)
                    final_path = os.path.join(project_dir, f"{file_id}.mp4")
                    try:
                        os.rename(dl_file, final_path)
                        dl_file = final_path
                    except Exception:
                        pass
                    return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)
        for fn_name, fn in [('fdown.net', _facebook_via_fdown),
                             ('getfvid.com', _facebook_via_getfvid),
                             ('snapsave.app', _facebook_via_snapsave)]:
            dl_url = fn(url)
            if dl_url:
                safe_print(f"[facebook] {fn_name} returned a link, downloading directly...")
                dl_file = download_direct_file(dl_url, progress_callback)
                if dl_file and is_valid_video(dl_file):
                    new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                    try:
                        os.rename(dl_file, new_path)
                        dl_file = new_path
                    except Exception:
                        pass
                    return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)
            safe_print(f"[facebook] {fn_name} failed.")
        return f"ERROR: This Facebook video is private or restricted. To download private videos, please provide your Facebook cookies (COOKIES_FACEBOOK env var)."

    # ── Instagram: snapinsta → igdownloader ──────────────────────
    elif site == 'instagram':
        safe_print("[instagram] Trying scraper fallbacks...")
        for fn_name, fn in [('snapinsta.app', _instagram_via_snapinsta),
                             ('igdownloader.app', _instagram_via_igdownloader)]:
            dl_url = fn(url)
            if dl_url:
                safe_print(f"[instagram] {fn_name} returned a link...")
                dl_file = download_direct_file(dl_url, progress_callback)
                if dl_file and is_valid_video(dl_file):
                    new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                    try:
                        os.rename(dl_file, new_path)
                        dl_file = new_path
                    except Exception:
                        pass
                    return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)
        return "ERROR: This Instagram content is private or unavailable. To download private posts, please provide your Instagram cookies (COOKIES_INSTAGRAM env var)."

    # ── TikTok: tikmate → ssstik ─────────────────────────────────
    elif site == 'tiktok':
        safe_print("[tiktok] Trying no-watermark fallbacks...")
        for fn_name, fn in [('tikmate.online', _tiktok_via_tikmate),
                             ('ssstik.io', _tiktok_via_ssstik)]:
            dl_url = fn(url)
            if dl_url:
                safe_print(f"[tiktok] {fn_name} returned a link...")
                dl_file = download_direct_file(dl_url, progress_callback)
                if dl_file and is_valid_video(dl_file):
                    new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                    try:
                        os.rename(dl_file, new_path)
                        dl_file = new_path
                    except Exception:
                        pass
                    return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # ── Twitter/X: twitsave ──────────────────────────────────────
    elif site == 'twitter':
        safe_print("[twitter] Trying twitsave fallback...")
        dl_url = _twitter_via_twitsave(url)
        if dl_url:
            dl_file = download_direct_file(dl_url, progress_callback)
            if dl_file and is_valid_video(dl_file):
                new_path = os.path.join(temp_dir, f"{file_id}.mp4")
                try:
                    os.rename(dl_file, new_path)
                    dl_file = new_path
                except Exception:
                    pass
                return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # ── PikPak ───────────────────────────────────────────────────
    elif site == 'pikpak':
        pikpak_user = os.environ.get('PIKPAK_USER')
        pikpak_pass = os.environ.get('PIKPAK_PASS')
        if not pikpak_user or not pikpak_pass:
            return "ERROR: PikPak downloads require an account. Please ask the bot admin to set PIKPAK_USER and PIKPAK_PASS in the .env file."
        else:
            return "ERROR: PikPak integration requires a custom script to parse share links into your account. Please set it up via the python pikpakapi library."

    # ─────────────────────────────────────────────────────────────
    # Step 3: Universal Cobalt API fallback (works for 800+ sites)
    # ─────────────────────────────────────────────────────────────
    safe_print("[cobalt] Trying Cobalt multi-site API fallback...")
    cobalt_url = _via_cobalt_api(url)
    if cobalt_url:
        dl_file = download_direct_file(cobalt_url, progress_callback)
        if dl_file and is_valid_video(dl_file):
            new_path = os.path.join(temp_dir, f"{file_id}.mp4")
            try:
                os.rename(dl_file, new_path)
                dl_file = new_path
            except Exception:
                pass
            return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # ─────────────────────────────────────────────────────────────
    # Step 4: SaveFrom generic API fallback
    # ─────────────────────────────────────────────────────────────
    safe_print("[savefrom] Trying SaveFrom generic API fallback...")
    sf_url = _via_savefrom_api(url)
    if sf_url:
        dl_file = download_direct_file(sf_url, progress_callback)
        if dl_file and is_valid_video(dl_file):
            new_path = os.path.join(temp_dir, f"{file_id}.mp4")
            try:
                os.rename(dl_file, new_path)
                dl_file = new_path
            except Exception:
                pass
            return _finalize(dl_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # ─────────────────────────────────────────────────────────────
    # Step 5: Bare yt-dlp retry without format restrictions
    # ─────────────────────────────────────────────────────────────
    safe_print("[yt-dlp] Trying bare format=best fallback...")
    try:
        bare_opts = {
            'outtmpl': os.path.join(temp_dir, f'{file_id}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'noplaylist': True,
            'ffmpeg_location': imageio_ffmpeg.get_ffmpeg_exe(),
            'progress_hooks': [yt_dlp_hook] if progress_callback else [],
            'geo_bypass': True,
            'geo_bypass_country': 'US',
            'format': 'best',
            'merge_output_format': 'mp4',
            'source_address': '0.0.0.0',
        }
        cookies_file = _get_cookies_file(site)
        if cookies_file:
            bare_opts['cookiefile'] = cookies_file
        status, downloaded_file = _run_ydl(target_url, bare_opts)
        if downloaded_file:
            return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)
    except Exception as bare_e:
        safe_print(f"[bare] Failed: {bare_e}")
        downloaded_file = _find_downloaded()
        if downloaded_file:
            return _finalize(downloaded_file, is_audio, MAX_SIZE_BYTES, progress_callback)

    # All methods exhausted
    if primary_error:
        err_str = str(primary_error)
        bot_keywords = ('sign in', 'bot', 'botguard', 'confirm', 'verify', 'captcha', 'proof')
        if any(k in err_str.lower() for k in bot_keywords):
            return 'BOT_DETECTED'
        if 'private' in err_str.lower() or 'login' in err_str.lower() or 'members only' in err_str.lower():
            return f"ERROR: This video is private or requires login. Add your cookies via the COOKIES_{site.upper()} (or COOKIES_ALL) environment variable."
        return f"ERROR: {err_str}"
    return "ERROR: Could not download media. The video may be private, region-locked, or from an unsupported platform."


# ─────────────────────────────────────────────────────────
# Finalize: size check, audio extraction, return path
# ─────────────────────────────────────────────────────────

def _finalize(file_path, is_audio, max_size, progress_callback=None):
    """Post-download: size check, optional audio conversion, return path."""
    if not file_path or not os.path.exists(file_path):
        return "ERROR: Downloaded file not found."
    if os.path.getsize(file_path) > max_size:
        try:
            os.remove(file_path)
        except Exception:
            pass
        return 'TOO_LARGE'
    if is_audio and not file_path.lower().endswith('.mp3'):
        try:
            from converter import convert_video_to_audio
            audio_file = convert_video_to_audio(file_path, output_format='mp3', progress_callback=progress_callback)
            if audio_file and os.path.exists(audio_file) and os.path.getsize(audio_file) > 0:
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                return audio_file
        except Exception as conv_e:
            safe_print(f"[finalize] Audio conversion error: {conv_e}")
    return file_path
