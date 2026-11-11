import re

with open(r'd:\My-Project\Telegram Bot\UdomAI-Bot-main (2)\UdomAI-Bot-main\plugins\core.py', 'r', encoding='utf-8') as f:
    content = f.read()

target_start = '@Client.on_message(filters.command("postfb"), group=0)'
target_end = '@Client.on_message(filters.command("mychats"), group=0)'

start_idx = content.find(target_start)
end_idx = content.find(target_end)

if start_idx == -1 or end_idx == -1:
    print("Could not find start or end index")
    exit(1)

new_logic = '''async def execute_fb_post(client, message, target, text_start, is_super_admin, fb_access_list, status_msg=None):
    import os
    pages_to_post = []
    
    if target == "aimovie":
        p = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p and t: pages_to_post.append((p, t, "AI Movie Khmer"))
    elif target == "livealone":
        p = os.environ.get("LIVEALONE_PAGE_ID")
        t = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p and t: pages_to_post.append((p, t, "LiveAlone"))
    elif target in ["all", "all_system"]:
        p1 = os.environ.get("AIMOVIEKHMER_PAGE_ID")
        t1 = os.environ.get("FACEBOOK_PAGE_AIMOVIEKHMER_TOKEN")
        if p1 and t1: pages_to_post.append((p1, t1, "AI Movie Khmer"))
        p2 = os.environ.get("LIVEALONE_PAGE_ID")
        t2 = os.environ.get("FACEBOOK_PAGE_LIVEALONE_TOKEN")
        if p2 and t2: pages_to_post.append((p2, t2, "LiveAlone"))
    elif target == "all_saved":
        from user_manager import get_user_facebook_pages
        pages = get_user_facebook_pages(message.from_user.id)
        for i, p in enumerate(pages):
            pid, ptok, pname = p.get("page_id"), p.get("page_token"), p.get("page_name", f"Saved Page {i+1}")
            if pid and ptok: pages_to_post.append((pid, ptok, pname))
    elif target.startswith("saved_"):
        idx = int(target.split("_")[1])
        from user_manager import get_user_facebook_pages
        pages = get_user_facebook_pages(message.from_user.id)
        if idx < len(pages):
            p = pages[idx]
            pid, ptok, pname = p.get("page_id"), p.get("page_token"), p.get("page_name", f"Saved Page {idx+1}")
            if pid and ptok: pages_to_post.append((pid, ptok, pname))
    else:
        from user_manager import get_user_facebook
        p, t = get_user_facebook(message.from_user.id)
        if p and t: pages_to_post.append((p, t, "Custom Page"))

    if not pages_to_post:
        msg_text = "⚠️ You haven't set up your Facebook credentials yet. Use `/setfb <page_id> <page_token>`."
        if status_msg: await status_msg.edit_text(msg_text)
        else: await message.reply_text(msg_text)
        return

    split_parts = message.text.split(maxsplit=text_start)
    text = split_parts[text_start] if len(split_parts) > text_start else ""
    media_path = None
    msg = status_msg
    
    if message.reply_to_message:
        target_msg = message.reply_to_message
        if target_msg.text:
            text = text or target_msg.text
            if not msg: msg = await message.reply_text("📤 Posting text to Facebook...")
            else: await msg.edit_text("📤 Posting text to Facebook...")
        elif target_msg.media:
            if not msg: msg = await message.reply_text("📥 Downloading media...")
            else: await msg.edit_text("📥 Downloading media...")
            try:
                media_path = await target_msg.download()
                if target_msg.caption and not text:
                    text = target_msg.caption
            except Exception as e:
                await msg.edit_text(f"❌ Failed to download media: {e}")
                return
            await msg.edit_text("📤 Posting media to Facebook...")
    elif not text:
        err = "⚠️ Usage: Reply to a message with `/postfb [optional text]`, or send `/postfb <text>`."
        if msg: await msg.edit_text(err)
        else: await message.reply_text(err)
        return
    else:
        if not msg: msg = await message.reply_text("📤 Posting text to Facebook...")
        else: await msg.edit_text("📤 Posting text to Facebook...")

    import re
    tags = None
    collaborators = None
    
    if text:
        tags_match = re.search(r'--tags\s+([\d,\s]+)(?=\s--|$)', text)
        if tags_match:
            tags = tags_match.group(1).strip()
            text = text.replace(tags_match.group(0), '')
            
        collabs_match = re.search(r'--collabs\s+([\d,\s]+)(?=\s--|$)', text)
        if collabs_match:
            collaborators = collabs_match.group(1).strip()
            text = text.replace(collabs_match.group(0), '')
            
        text = text.strip()

    from facebook_util import post_to_facebook
    import asyncio
    
    results = []
    for p_id, p_token, p_name in pages_to_post:
        await msg.edit_text(f"📤 Posting to {p_name}...")
        is_success, result = await asyncio.to_thread(post_to_facebook, p_id, p_token, text, media_path, None, None, tags, collaborators)
        results.append((p_name, is_success, result))
    
    if media_path and os.path.exists(media_path):
        os.remove(media_path)
        
    res_text = "\\n".join([f"{'✅' if r[1] else '❌'} {r[0]}: {r[2] if not r[1] else 'Post ID: ' + str(r[2])}" for r in results])
    await msg.edit_text(f"Facebook Post Results:\\n{res_text}")


@Client.on_message(filters.command("postfb"), group=0)
async def postfb_command(client: Client, message: Message):
    if not await check_user_access(message): return
    from user_manager import get_user_facebook_pages, get_user, AUTO_APPROVED_USERNAMES
    
    args = message.text.split()
    target = None
    text_start = 1
    
    u = get_user(message.from_user.id)
    is_super_admin = message.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    
    fb_access_list = []
    if u:
        access = u.get('fb_pages_access', [])
        if isinstance(access, bool):
            fb_access_list = ["aimovie", "livealone"] if access else []
        else:
            fb_access_list = access
            
    if len(args) > 1:
        requested_target = args[1].lower()
        if requested_target in ["aimovie", "livealone", "all", "all_system"]:
            if is_super_admin or (requested_target in fb_access_list) or (requested_target in ["all", "all_system"] and "aimovie" in fb_access_list and "livealone" in fb_access_list):
                target = requested_target
                text_start = 2
            else:
                await message.reply_text("⚠️ You do not have permission to post to this system page.")
                return
        elif requested_target.startswith("saved_") or requested_target == "all_saved" or requested_target == "custom":
            target = requested_target
            text_start = 2
            
    if target:
        await execute_fb_post(client, message, target, text_start, is_super_admin, fb_access_list)
    else:
        pages = get_user_facebook_pages(message.from_user.id)
        buttons = []
        
        if is_super_admin or ("aimovie" in fb_access_list and "livealone" in fb_access_list):
            buttons.append([InlineKeyboardButton("🌍 All System Pages", callback_data=f"fbpost|{message.id}|all")])
        if is_super_admin or "aimovie" in fb_access_list:
            buttons.append([InlineKeyboardButton("🎬 AI Movie Khmer", callback_data=f"fbpost|{message.id}|aimovie")])
        if is_super_admin or "livealone" in fb_access_list:
            buttons.append([InlineKeyboardButton("👤 LiveAlone", callback_data=f"fbpost|{message.id}|livealone")])
            
        if pages:
            if len(pages) > 1:
                buttons.append([InlineKeyboardButton("📢 All My Saved Pages", callback_data=f"fbpost|{message.id}|all_saved")])
            for i, p in enumerate(pages):
                p_name = p.get('page_name', f'Saved Page {i+1}')
                buttons.append([InlineKeyboardButton(f"📄 {p_name}", callback_data=f"fbpost|{message.id}|saved_{i}")])
                
        if not buttons:
            await message.reply_text("⚠️ You haven't set up any Facebook pages. Use `/setfb` or login to the Dashboard.")
            return
            
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="delete_message")])
        
        await message.reply_text(
            "📍 **Select Facebook Page to Post:**\\n\\nChoose where you want to publish this post:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

@Client.on_callback_query(filters.regex(r"^fbpost\\|"), group=0)
async def handle_fbpost_callback(client: Client, callback_query: CallbackQuery):
    _, msg_id, target = callback_query.data.split("|")
    try:
        orig_msg = await client.get_messages(callback_query.message.chat.id, int(msg_id))
    except Exception:
        await callback_query.answer("⚠️ Original message not found.", show_alert=True)
        return
        
    if not orig_msg or orig_msg.empty:
        await callback_query.answer("⚠️ Original message not found.", show_alert=True)
        return
        
    await callback_query.message.edit_text("⏳ Processing your post request...")
    
    from user_manager import get_user, AUTO_APPROVED_USERNAMES
    u = get_user(callback_query.from_user.id)
    is_super_admin = callback_query.from_user.username in AUTO_APPROVED_USERNAMES or (u and u.get('role') == 'SUPER_ADMIN')
    
    fb_access_list = []
    if u:
        access = u.get('fb_pages_access', [])
        if isinstance(access, bool):
            fb_access_list = ["aimovie", "livealone"] if access else []
        else:
            fb_access_list = access
            
    await execute_fb_post(client, orig_msg, target, 1, is_super_admin, fb_access_list, status_msg=callback_query.message)

'''

new_content = content[:start_idx] + new_logic + content[end_idx:]

with open(r'd:\My-Project\Telegram Bot\UdomAI-Bot-main (2)\UdomAI-Bot-main\plugins\core.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("File patched successfully!")
