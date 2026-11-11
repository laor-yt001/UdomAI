"""
Generate a Pyrogram String Session for USER_SESSION.
Run this ONCE, paste the output into .env as: USER_SESSION=...
The account used MUST be a member of the private channels you want to download from.
"""
import asyncio
from pyrogram import Client
from dotenv import load_dotenv
import os

load_dotenv()

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

async def main():
    print("=" * 60)
    print("  UdomAI Bot - Generate USER_SESSION String")
    print("=" * 60)
    print("This will generate a session string for your Telegram account.")
    print("The account MUST be a member of private channels/groups.")
    print("")

    async with Client("gen_session", api_id=API_ID, api_hash=API_HASH) as app:
        session_string = await app.export_session_string()
        me = await app.get_me()
        print(f"\n✅ Session generated for: {me.first_name} (@{me.username or me.id})")
        print("\nAdd this line to your .env file:\n")
        print(f"USER_SESSION={session_string}")
        print("\n⚠️  Keep this secret! Anyone with this string can access your account.")

asyncio.run(main())
