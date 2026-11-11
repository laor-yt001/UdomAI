"""
export_yt_cookies.py — Export YouTube cookies from your local browser to youtube_cookies.txt

Usage:
    python export_yt_cookies.py [browser]

    browser: chrome (default), firefox, edge, brave, chromium, opera, safari

This script exports your YouTube login cookies from your local browser
so the bot can use them to bypass YouTube bot-guard on any machine (local or server).

Steps:
    1. Log in to YouTube in your browser first
    2. Run this script: python export_yt_cookies.py
    3. Copy the generated youtube_cookies.txt to the bot server (same folder as bot.py)
    4. Restart the bot

The bot automatically picks up youtube_cookies.txt from its folder.
"""

import sys
import os
import subprocess

def export_cookies(browser='chrome'):
    out_file = os.path.join(os.path.dirname(__file__), 'youtube_cookies.txt')
    print(f"[*] Exporting YouTube cookies from {browser} -> {out_file}")
    
    try:
        # Use yt-dlp's built-in --cookies-from-browser feature
        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--cookies-from-browser', browser,
            '--cookies', out_file,
            '--skip-download',
            '--quiet',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ'  # dummy video to trigger cookie dump
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if os.path.exists(out_file) and os.path.getsize(out_file) > 100:
            print(f"[✓] Success! Cookies exported to: {out_file}")
            print(f"[✓] File size: {os.path.getsize(out_file):,} bytes")
            print()
            print("Next steps:")
            print(f"  - If bot is running on THIS machine: restart the bot. It will auto-load youtube_cookies.txt")
            print(f"  - If bot is on a SERVER: copy youtube_cookies.txt to the bot folder on the server, then restart")
            return True
        else:
            print(f"[✗] Export failed or empty file. stderr: {result.stderr}")
            return False
    except Exception as e:
        print(f"[✗] Error: {e}")
        return False


def try_all_browsers():
    browsers = ['chrome', 'chromium', 'firefox', 'edge', 'brave', 'opera']
    for browser in browsers:
        print(f"\n[*] Trying browser: {browser}")
        if export_cookies(browser):
            return True
        print(f"    → {browser} failed, trying next...")
    print("\n[✗] All browsers failed. Make sure you are logged in to YouTube in at least one browser.")
    return False


if __name__ == '__main__':
    browser = sys.argv[1].lower() if len(sys.argv) > 1 else None
    
    print("=" * 60)
    print("  YouTube Cookie Exporter for UdomAI Bot")
    print("=" * 60)
    print()
    print("Make sure you are LOGGED IN to YouTube in your browser before running this.")
    print()
    
    if browser:
        success = export_cookies(browser)
    else:
        # Try chrome first, then all others
        success = export_cookies('chrome')
        if not success:
            print("\n[!] Chrome failed. Trying other browsers...")
            success = try_all_browsers()
    
    if not success:
        print()
        print("Manual alternative:")
        print("  1. Install the 'Get cookies.txt LOCALLY' Chrome extension")
        print("     https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc")
        print("  2. Go to https://www.youtube.com and log in")
        print("  3. Click the extension and export cookies")
        print("  4. Save the file as 'youtube_cookies.txt' in the bot folder")
        sys.exit(1)
    
    sys.exit(0)
