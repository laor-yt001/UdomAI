import os
import logging
import threading
import inspect
from typing import Optional
import requests
from dotenv import load_dotenv
from pyrogram.client import Client
from utils import get_temp_dir, start_auto_cleanup_routine
from dashboard_url import DEFAULT_NGROK_URL, fetch_ngrok_dashboard_url

# Global Pyrogram app instance for Telegram client
app: Optional[Client] = None

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress Hugging Face Hub unauthenticated warning logs
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)

from web_dashboard import run_dashboard_server
from self_improver import improver
import scheduler

def main():
    """Start the bot."""
    load_dotenv()
    
    # Ensure WireGuard / Cloudflare WARP proxy is initialized and enforced for all outbound traffic
    try:
        import wireguard_setup
        wireguard_setup.setup_and_start_wireguard()
        wireguard_setup.enforce_wireguard_in_python()
    except Exception as wg_err:
        logger.warning(f"WireGuard startup check warning: {wg_err}")
    
    # Enable optional async IO mode via environment variable
    ENABLE_ASYNC_IO = os.getenv('ENABLE_ASYNC_IO', 'false').lower() == 'true'
    
    # Start web management dashboard server immediately so Render health checks succeed
    threading.Thread(target=run_dashboard_server, daemon=False).start()

    # Prioritize active NGROK_DASHBOARD_URL or BACKEND_URL from environment/actions
    env_dashboard_url = os.environ.get("NGROK_DASHBOARD_URL") or os.environ.get("BACKEND_URL")
    if env_dashboard_url and env_dashboard_url.strip():
        os.environ["NGROK_DASHBOARD_URL"] = env_dashboard_url.strip()
        logger.info("Using ngrok dashboard URL from environment: %s", env_dashboard_url.strip())
    else:
        ngrok_endpoint = os.environ.get("NGROK_URL", DEFAULT_NGROK_URL).strip()
        try:
            dashboard_url = fetch_ngrok_dashboard_url(ngrok_endpoint)
            os.environ["NGROK_DASHBOARD_URL"] = dashboard_url
            logger.info("Using ngrok dashboard URL: %s", dashboard_url)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Unable to resolve ngrok dashboard URL from %s: %s", ngrok_endpoint, exc)
    
    # Auto-update GitHub repository About section with live backend URL
    active_backend = os.environ.get("NGROK_DASHBOARD_URL") or os.environ.get("BACKEND_URL")
    if active_backend and active_backend.strip().startswith("http"):
        try:
            import github_sync
            repo_name = os.environ.get("GITHUB_REPO", "udom01/main")
            github_sync.update_repo_about(
                repo=repo_name,
                homepage_url=active_backend.strip(),
                description=f"⚡ UdomAI Bot & Web Dashboard | Live: {active_backend.strip()}"
            )
        except Exception as e:
            logger.warning(f"Could not update GitHub repo About: {e}")
    
    # Start autonomous self-improvement background loop (runs analysis & optimization every 10 minutes)
    improver.start_background_loop(interval_seconds=600)
    
    # Start auto background temp file purging routine
    start_auto_cleanup_routine()

    # Start real-time GitHub sync daemon and perform initial immediate sync
    try:
        import github_sync
        github_sync.sync_pull_file("users_data.json")
        github_sync.sync_pull_file("schedules.json")
        github_sync.start_github_sync_daemon(interval=15)
    except Exception as gh_sync_err:
        logger.warning(f"Failed to start github sync daemon: {gh_sync_err}")

    server_id = os.environ.get("SERVER_ID", "main")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")

    if not api_id or not api_hash:
        logger.error("API_ID and API_HASH are required for Pyrogram. Please add them to your environment variables.")
        return

    # For the main server, TELEGRAM_BOT_TOKEN is required to run the main bot.
    # For worker servers (e.g. server1), TELEGRAM_BOT_TOKEN is optional because worker servers host custom user bots.
    if server_id == "main" and (not token or token == "your_bot_token_here"):
        logger.error("No valid Telegram Bot Token found for Main Server. Please update .env or repository secrets.")
        return

    # Ensure temp dir exists
    get_temp_dir()

    import asyncio
    import concurrent.futures
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if ENABLE_ASYNC_IO:
        cpu = os.cpu_count() or 2
        max_workers = cpu * 2
    else:
        max_workers = 64
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=max_workers))

    global app
    if server_id == "main" and token and token != "your_bot_token_here":
        logger.info("Initializing Main Bot client...")
        import wireguard_setup
        wireguard_setup.clean_proxy_env_if_dead()
        proxy_cfg = wireguard_setup.get_pyrogram_proxy()
        client_kwargs = {
            "name": "my_bot",
            "bot_token": token,
            "api_id": int(api_id),
            "api_hash": api_hash,
            "plugins": dict(root="plugins"),
            "workers": 16
        }
        if proxy_cfg:
            client_kwargs["proxy"] = proxy_cfg
        app = Client(**client_kwargs)
    else:
        app = None
        logger.info(f"Main bot client skipped on '{server_id}' (dedicated custom bots & scheduler worker node).")

    # NOTE: scheduler.run_scheduler_loop() is intentionally started AFTER app.start()
    # so the Pyrogram client is already connected when scheduled tasks fire.
    # (Moved below; set_scheduler_loop only pre-registers the loop reference.)
    scheduler.set_scheduler_loop(loop)

    import time
    from pyrogram.errors import FloodWait

    if server_id == "main" and app:
        # Startup with exponential backoff on FloodWait, and auto-fallback to direct connection if proxy fails
        max_retries = 5
        retry = 0
        while True:
            try:
                start_res = app.start()
                if inspect.isawaitable(start_res):
                    loop.run_until_complete(start_res)
                logger.info("🎉 Pyrogram Main Bot connected successfully!")
                # ✅ Start scheduler AFTER bot is fully connected — prevents race condition
                # where scheduled tasks fire before app.start() completes.
                scheduler.run_scheduler_loop(app)
                break
            except FloodWait as e:
                wait_time = getattr(e, "value", 60)
                backoff = min(wait_time * (2 ** retry), 300)
                logger.warning(f"⚠️ Telegram FloodWait during startup! Sleeping {backoff}s before retrying...")
                time.sleep(backoff + 2)
                retry += 1
                if retry >= max_retries:
                    logger.error("Maximum FloodWait retries reached. Exiting.")
                    raise
            except Exception as conn_err:
                if proxy_cfg:
                    logger.warning(f"⚠️ Pyrogram proxy connection failed ({conn_err}). Removing proxy and falling back to clean direct connection...")
                    proxy_cfg = None
                    client_kwargs.pop("proxy", None)
                    app = Client(**client_kwargs)
                    time.sleep(2)
                    continue
                retry += 1
                if retry >= max_retries:
                    logger.error(f"Failed to connect to Telegram after {max_retries} retries: {conn_err}")
                    raise
                logger.warning(f"Telegram connection error ({conn_err}). Retrying in 5s (attempt {retry}/{max_retries})...")
                time.sleep(5)
    else:
        logger.info(f"Skipping Main Bot startup (SERVER_ID is '{server_id}').")

    async def worker_startup():
        from pyrogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
        from plugins.core import start_shared_user_client
        try:
            base_commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("download", "Download media from a link"),
                BotCommand("convert", "Convert a media file"),
                BotCommand("image", "Generate an image with AI"),
                BotCommand("ask", "Ask the AI a question"),
                BotCommand("search", "Search the web"),
                BotCommand("publish", "📤 Publish file/text to a group or channel"),
                BotCommand("mychats", "📋 List groups/channels where I'm admin"),
                BotCommand("howtouse", "How to Use guide (Khmer & English)"),
                BotCommand("help", "Show help info")
            ]

            admin_commands = base_commands + [
                BotCommand("dashboard", "⚙️ Open Web Dashboard"),
                BotCommand("setfb", "Configure Facebook Page"),
                BotCommand("checkfb", "Check Facebook config"),
                BotCommand("postfb", "Post to Facebook"),
                BotCommand("allfb", "List all users with custom FB pages (Super Admin)")
            ]

            # Set default commands for everyone if connected
            if server_id == "main" and app and getattr(app, "is_connected", False):
                await app.set_bot_commands(base_commands, scope=BotCommandScopeDefault())

            # Set extended commands for SUPER_ADMINs
            if server_id == "main" and app and getattr(app, "is_connected", False):
                try:
                    from user_manager import load_users
                    users = load_users()
                    for uid_str, udata in users.items():
                        if uid_str != "__system_config__" and udata.get("role") == "SUPER_ADMIN":
                            try:
                                await app.set_bot_commands(admin_commands, scope=BotCommandScopeChat(int(uid_str)))
                            except Exception as inner_e:
                                logger.warning(f"Failed to set admin commands for {uid_str}: {inner_e}")
                except Exception as e:
                    logger.warning(f"Error iterating users for admin commands: {e}")
                logger.info("Bot commands set successfully!")
            else:
                logger.info(f"Skipped setting main bot commands because SERVER_ID is '{server_id}'")
        except Exception as e:
            logger.warning(f"Skipped setting bot commands: {e}")

        # Start shared user client for private channel downloads (if USER_SESSION set)
        user_session = os.environ.get("USER_SESSION") or os.environ.get("STRING_SESSION")
        if user_session:
            await start_shared_user_client(api_id, api_hash, user_session)
            logger.info("Shared user client started for private channel downloads.")
        else:
            logger.info("USER_SESSION not set — private channel downloads disabled (bot must be a member).")

        # Pre-load admin chats list on startup if app connected
        if app and getattr(app, "is_connected", False):
            try:
                from plugins.core import refresh_admin_chats
                await refresh_admin_chats(app)
                logger.info("Admin chats refreshed on startup.")
            except Exception as e:
                logger.warning(f"Could not refresh admin chats on startup: {e}")

        # Start all approved custom bots for this server!
        import custom_bots_manager
        custom_bots_manager.main_loop = loop
        await custom_bots_manager.start_all_approved_bots()

        # Start the scheduler loop on worker servers (or main if app is not used)
        if server_id != "main" or not app:
            logger.info(f"Starting scheduler loop on '{server_id}' worker server...")
            scheduler.run_scheduler_loop(app)

        # Check and resume unfinished bot tasks from previous actions / runs
        if app and getattr(app, "is_connected", False):
            try:
                from task_resumer import resume_unfinished_bot_tasks
                await resume_unfinished_bot_tasks(app)
            except Exception as e:
                logger.warning(f"Failed to auto-resume unfinished bot tasks: {e}")

    loop.run_until_complete(worker_startup())

    if app and getattr(app, "is_connected", False):
        from pyrogram.methods.utilities.idle import idle
        try:
            idle_res = idle()
            if inspect.isawaitable(idle_res):
                loop.run_until_complete(idle_res)
        finally:
            # Clean up custom bots
            import custom_bots_manager
            loop.run_until_complete(custom_bots_manager.stop_all_bots())

            # Clean up shared user client on shutdown
            from plugins.core import stop_shared_user_client
            loop.run_until_complete(stop_shared_user_client())

            if app is not None and getattr(app, "is_connected", False):
                try:
                    stop_res = app.stop()
                    if inspect.isawaitable(stop_res):
                        loop.run_until_complete(stop_res)
                except Exception:
                    pass
    else:
        logger.info(f"🚀 Worker server '{server_id}' is actively running (listening for custom bots & scheduler tasks)...")
        try:
            loop.run_forever()
        finally:
            import custom_bots_manager
            loop.run_until_complete(custom_bots_manager.stop_all_bots())
            from plugins.core import stop_shared_user_client
            loop.run_until_complete(stop_shared_user_client())

if __name__ == "__main__":
    main()
