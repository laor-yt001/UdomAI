# 🎬 UdomAI Telegram Bot & Distributed Multi-Server Cluster

<div align="center">

[![Storage Vault](https://img.shields.io/badge/📦_Code_Storage-laor--yt001%2FUdomAI--Bot-blue?style=for-the-badge&logo=github)](https://github.com/laor-yt001/UdomAI-Bot)
[![Runner Cluster](https://img.shields.io/badge/🚀_Runners-udom--bot1%20(main%2C%20server1%2C%20server2)-success?style=for-the-badge&logo=githubactions)](https://github.com/udom-bot1/main)
[![Telegram Bot](https://img.shields.io/badge/🤖_Main_Bot-@UdomAI__Registration__bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)](https://t.me/UdomAI_Registration_bot)
[![Python Version](https://img.shields.io/badge/python-3.10%20%7C%203.11-brightgreen?style=for-the-badge&logo=python)](https://python.org)

### 🌟 Cinema-Grade Video Dubbing • Multilingual AI Translation • Poster Stylizer • Short Drama Downloader • Cloud Cluster

</div>

---

## 🏗️ Decoupled Architecture (Storage vs Runners)

To protect system integrity, prevent token exposure, and eliminate workflow conflicts, this platform is divided into two distinct components:

```
┌────────────────────────────────────────────────────────────────────────┐
│                   📦 SOURCE CODE STORAGE VAULT                         │
│               https://github.com/laor-yt001/UdomAI-Bot                 │
├────────────────────────────────────────────────────────────────────────┤
│ • Pure application source code & algorithms                            │
│ • ZERO .yml GitHub Actions workflows (never executes actions directly) │
│ • ZERO plaintext tokens, keys, or cookies                              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
       Cloud Runners pull latest    │  via STORAGE_PAT dynamically
       code into memory on boot     │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   🚀 CLUSTER RUNNERS (udom-bot1)                       │
├───────────────────────────────────┬────────────────────────────────────┤
│  udom-bot1/main                   │  Main Registration & Web Admin     │
│  udom-bot1/server1                │  Dedicated Worker Node 1           │
│  udom-bot1/server2                │  Dedicated Worker Node 2           │
├───────────────────────────────────┴────────────────────────────────────┤
│ • Contains ONLY .github/workflows/run-bot.yml & README                 │
│ • Stores NO application source code                                    │
│ • Securely backed by 25 encrypted GitHub Repository Secrets           │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🌟 Key Features

### 1. 🎙️ Cinema-Grade Multilingual Dubbing & Lip Timing
- **35-Second Chunking Pipeline:** Transcribes speech in 35s audio slices with 3s overlap via `gemini-3.5-flash`, capturing 100% of dialogue exchanges with zero token cutoff.
- **Accurate Character Gender Matching:** Detects character gender and validates it via FFT fundamental frequency pitch slicing:
  - **Male Characters:** Voice synthesized with `km-KH-PisethNeural` (or target male voice).
  - **Female Characters & Spirits:** Voice synthesized with `km-KH-SreymomNeural` (or target female voice).
- **Sample-Accurate PCM Timeline (Zero Drift):** Assembles syntheses into a 44.1kHz stereo audio buffer before lossless FFmpeg muxing, ensuring voice timing stays locked to mouth movement.
- **Demucs v4 AI Stem Separation:** Discards original vocal audio while preserving ambient background music and sound effects (SFX).

### 2. 🎨 Movie Poster & Title Stylizer Engine
- **Artwork Inpainting:** Locates and erases original Chinese/English movie titles and watermarks from poster art.
- **Poetic Title Localization:** Automatically translates drama titles into cinema-grade localized variants via Gemini (e.g., `灵兽心语` $\to$ `ភាសាដួងចិត្តសត្វទេព` / *Whispers of the Spirit Beasts*).
- **3D Embossed Typography:** Renders multi-layered 3D styling matching original poster aesthetics:
  - **3D Gold Fantasy:** Deep inner bevel, specular shine, and ambient ember glow.
  - **Fiery Red Action:** Crimson gradients with flame aura.
  - **Silver Cyber:** Metallic sci-fi chrome with cyan highlights.

### 3. ⏱️ Anti-Freeze Real-Time Progress Tracker
- **FloodWait Elimination:** Intelligent 3.5s–4.0s rate limiting on Telegram message edits prevents anti-spam throttling and eliminates frozen counters (`គាំង`).
- **Live Background Stage Details:** Displays exact current background actions:
  - `🎙️ [Transcribing] Gemini analyzing: Chunk 2/4 (35s - 70s)...`
  - `🎵 [Separating BGM] Demucs AI isolating music & sound effects...`
  - `🗣️ [Voice Dubbing] Synthesizing voices: 18/39 lines (KM)...`
  - `🔗 [Merging] MPEG-TS Lossless Concatenation (EP01-EP10)...`

### 4. 📺 Multi-Part Drama Downloader & Merging
- **Supported Platforms:** Hongguo Short Drama, WeTV VIP, iQIYI VIP, YouTube, TikTok, and direct video links.
- **Interactive Batch Merging:** Download entire drama seasons and merge them into seamless parts (e.g. merge 80 episodes into 5 parts) with automatic HEVC/H.264 bitstream filtering.

---

## 🔐 Environment Variables & Repository Secrets Mapping

All tokens, cookies, and keys are stored **strictly in local `.env`** and **GitHub Repository Secrets**. No secrets exist in the git history or code files.

| Environment Variable | GitHub Repository Secret | Required By | Description |
| :--- | :--- | :---: | :--- |
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN` | `main` | Telegram Bot Token from [@BotFather](https://t.me/botfather) |
| `API_ID` | `API_ID` | All Runners | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | `API_HASH` | All Runners | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `GEMINI_API_KEY` | `GEMINI_API_KEY` | All Runners | Google Gemini API Key for speech transcription and title localization |
| `PAT` / `GITHUB_PAT` | `PAT` | All Runners | Runner Operations PAT for `udom-bot1` (syncing databases/state) |
| `STORAGE_PAT` | `STORAGE_PAT` | All Runners | Read-only clone token for `laor-yt001/UdomAI-Bot` |
| `REDIS_URL` | `REDIS_URL` | All Runners | Redis connection URL for task queue and distributed locks |
| `HF_TOKEN` | `HF_TOKEN` | Optional | Hugging Face Access Token for VoxCPM2 voice models |
| `DASHBOARD_PASSWORD` | `DASHBOARD_PASSWORD` | `main` | Super Admin Web Dashboard login password |
| `NGROK_AUTHTOKEN` | `NGROK_AUTHTOKEN` | Optional | ngrok auth token for public Web Dashboard tunneling |
| `WG_PRIVATE_KEY` | `WG_PRIVATE_KEY` | All Runners | WireGuard / Cloudflare WARP client private key |
| `WG_PEER_PUBLIC_KEY` | `WG_PEER_PUBLIC_KEY` | All Runners | WireGuard / Cloudflare WARP server public key |
| `COOKIES_FACEBOOK` | `COOKIES_FACEBOOK` | Optional | Netscape format cookies for Facebook video uploading |
| `COOKIES_YOUTUBE` | `COOKIES_YOUTUBE` | Optional | Netscape format cookies to bypass YouTube bot detection |
| `YT_OAUTH2_TOKEN` | `YT_OAUTH2_TOKEN` | Optional | YouTube OAuth2 session token |

---

## 🛠️ Local Development & Quick Start

### 1. Clone Source Code
```bash
git clone https://github.com/laor-yt001/UdomAI-Bot.git
cd UdomAI-Bot
```

### 2. Setup Virtual Environment & Dependencies
```bash
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure `.env`
Create `.env` in the root folder using the template provided above.

### 4. Run Locally
```bash
# Run in Main Server mode:
python main.py

# Run in Dedicated Worker mode:
set SERVER_ID=server1
python main.py
```

---

## 📁 Repository Directory Layout

```
D:\My-Project\Telegram Bot\UdomAI-Bot-main (6)\
├── Udom-main/                      <-- Project Source Code (ZERO .yml files)
│   ├── .env                        <-- Local gitignored environment secrets
│   ├── main.py                     <-- Bot launcher & cluster coordinator
│   ├── converter.py                <-- Dubbing, TTS, Demucs & video pipeline
│   ├── downloader.py               <-- Hongguo, WeTV, iQIYI, YouTube downloader
│   ├── poster_styler.py            <-- 3D Movie Poster & Typography engine
│   ├── gemini_manager.py           <-- Multi-key rotation & failover manager
│   ├── utils.py                    <-- HEVC/MPEG-TS video merger & audio DSP
│   ├── plugins/                    <-- Telegram command handlers & UI modules
│   └── requirements.txt            <-- Python dependencies
│
└── Udom-Actions-Runners/           <-- Cloud Runners (YML Workflows Only)
    ├── .github/workflows/
    │   └── run-bot.yml             <-- Runner workflow pulling from storage
    ├── README.md                   <-- Runner overview & repository links
    └── deploy_runners.py           <-- Instant deploy tool for udom-bot1 repos
```

---

## 📄 License & Attribution
- Built with ❤️ by **UdomAI Engineering**.
- Powered by Google Gemini, Faster-Whisper, Demucs v4, Pyrogram, and Edge-TTS.
"# UdomAI" 
