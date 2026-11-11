import os
import asyncio
import logging
from pyrogram import Client

logger = logging.getLogger(__name__)

# Map of bot_token -> active Pyrogram Client instance
active_custom_bots = {}
main_loop = None

def get_active_custom_bots():
    return active_custom_bots

async def start_custom_bot(user_id, bot_token):
    """Start a Pyrogram client for a custom bot in the background."""
    if bot_token in active_custom_bots:
        logger.info(f"Custom bot {bot_token[:10]}... is already running.")
        return

    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")

    if not api_id or not api_hash:
        logger.error("Cannot start custom bot: API_ID or API_HASH not found in environment.")
        return

    try:
        # We uniquely name the session using the bot token's prefix
        session_name = f"custom_bot_{bot_token[:10]}"
        import wireguard_setup
        proxy_cfg = wireguard_setup.get_pyrogram_proxy()
        client_kwargs = {
            "name": session_name,
            "bot_token": bot_token,
            "api_id": int(api_id),
            "api_hash": api_hash,
            "plugins": dict(root="plugins"), # Inherit plugins, but will be restricted in core.py
            "in_memory": True, # Use in-memory session to prevent db lock issues
            "workers": 16
        }
        if proxy_cfg:
            client_kwargs["proxy"] = proxy_cfg
        client = Client(**client_kwargs)
        await client.start()
        
        try:
            from pyrogram.types import BotCommand, BotCommandScopeDefault
            custom_bot_commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("download", "Download media from a link"),
                BotCommand("convert", "Convert a media file"),
                BotCommand("publish", "📤 Publish file/text to a group or channel"),
                BotCommand("mychats", "📋 List groups/channels where I'm admin"),
                BotCommand("howtouse", "How to Use guide (Khmer & English)"),
                BotCommand("help", "Show help info")
            ]
            await client.set_bot_commands(custom_bot_commands, scope=BotCommandScopeDefault())
        except Exception as cmd_e:
            logger.warning(f"Could not set commands for custom bot {bot_token[:10]}: {cmd_e}")
            
        active_custom_bots[bot_token] = client
        me = await client.get_me()
        logger.info(f"Successfully started custom bot: @{me.username} (User: {user_id})")
    except Exception as e:
        logger.error(f"Failed to start custom bot {bot_token[:10]}: {e}")


async def stop_custom_bot(bot_token):
    """Stop a running custom Pyrogram client."""
    client = active_custom_bots.get(bot_token)
    if client:
        try:
            await client.stop()
            logger.info(f"Successfully stopped custom bot {bot_token[:10]}")
        except Exception as e:
            logger.error(f"Error stopping custom bot {bot_token[:10]}: {e}")
        finally:
            active_custom_bots.pop(bot_token, None)


async def start_all_approved_bots():
    """Reads all users and starts approved custom bots based on SERVER_ID and OTP config."""
    from user_manager import load_users, get_system_config
    
    users = load_users()
    tasks = []
    
    server_id = os.environ.get("SERVER_ID", "main")
    system_config = get_system_config()
    otp_bot_token = system_config.get("otp_bot_token")
    
    valid_tokens_for_this_server = set()

    for user_id_str, user_data in users.items():
        if user_id_str == "__system_config__":
            continue
            
        custom_bots = user_data.get("telegram_bots", [])
        assigned_server = user_data.get("assigned_server", "main")
        
        for bot in custom_bots:
            if bot.get("status") == "APPROVED":
                bot_token = bot.get("bot_token")
                if bot_token:
                    is_otp_bot = (bot_token == otp_bot_token)
                    should_start = False
                    
                    if is_otp_bot:
                        if server_id == "main":
                            should_start = True
                        else:
                            logger.info(f"Skipping OTP bot on non-main server ({server_id})")
                    else:
                        if assigned_server == server_id:
                            should_start = True
                        else:
                            logger.info(f"Skipping custom bot {bot_token[:10]}... (assigned to {assigned_server}, this is {server_id})")
                            
                    if should_start:
                        valid_tokens_for_this_server.add(bot_token)
                        tasks.append(start_custom_bot(user_id_str, bot_token))
                    
    # Reconcile: Stop any bot that is currently running on this server but no longer assigned here
    for running_token in list(active_custom_bots.keys()):
        if running_token not in valid_tokens_for_this_server:
            logger.info(f"Stopping custom bot {running_token[:10]}... because it is no longer assigned to {server_id}")
            tasks.append(stop_custom_bot(running_token))

    if tasks:
        logger.info(f"Starting/reconciling custom bots on server {server_id}...")
        await asyncio.gather(*tasks, return_exceptions=True)
    else:
        logger.info(f"No custom bot changes on server {server_id}.")


async def stop_all_bots():
    """Stops all running custom bots cleanly."""
    tasks = [stop_custom_bot(token) for token in list(active_custom_bots.keys())]
    if tasks:
        logger.info(f"Stopping {len(tasks)} custom bots...")
        await asyncio.gather(*tasks, return_exceptions=True)
