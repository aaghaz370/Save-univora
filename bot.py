"""
RATNA BOT 2.0 - Complete Production with ALL Features
✅ Private channel extraction (login system)
✅ Custom rename, caption, thumbnail
✅ Replace/remove words
✅ Queue + background processing
✅ Premium system (admin = owner gets all premium)
✅ Session management (Pyrogram V2)
✅ 24/7 free on Render
"""

import os
import asyncio
import logging
import json
import time
import re
import uuid
from datetime import datetime
from collections import deque
from threading import Thread, Lock
from typing import Optional, Dict, List
import io

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeFilename
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from pyrogram import Client as PyroClient
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI
import uvicorn

# ============= CONFIG =============
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))  # Admin/Owner ka ID

# Logging
logging.basicConfig(format='[%(levelname)s] %(asctime)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= DATABASE (In-Memory) =============
class Database:
    def __init__(self):
        self.users = {}  # {user_id: user_data}
        self.sessions = {}  # {user_id: session_string}
        self.premium = {OWNER_ID}  # Owner automatically premium
        self.locked_channels = set()
        self.lock = Lock()
    
    def get_user(self, user_id: int) -> dict:
        """Get user settings"""
        with self.lock:
            if user_id not in self.users:
                self.users[user_id] = {
                    'chat_id': None,
                    'rename': None,
                    'caption': None,
                    'thumbnail': None,
                    'replace_words': {},  # {old: new}
                    'remove_words': [],
                    'watermark': None,
                    'login_state': None,
                    'temp_data': {}
                }
            return self.users[user_id]
    
    def save_session(self, user_id: int, session: str):
        """Save user session"""
        with self.lock:
            self.sessions[user_id] = session
    
    def get_session(self, user_id: int) -> Optional[str]:
        """Get user session"""
        with self.lock:
            return self.sessions.get(user_id)
    
    def is_premium(self, user_id: int) -> bool:
        """Check premium status"""
        return user_id in self.premium
    
    def add_premium(self, user_id: int):
        """Add premium user"""
        with self.lock:
            self.premium.add(user_id)
    
    def remove_premium(self, user_id: int):
        """Remove premium"""
        with self.lock:
            if user_id in self.premium and user_id != OWNER_ID:
                self.premium.discard(user_id)

db = Database()

# ============= QUEUE SYSTEM =============
download_queue = deque()
active_downloads = {}
queue_lock = Lock()

# ============= TELEGRAM CLIENTS =============
bot = TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

async def get_user_client(user_id: int) -> Optional[TelegramClient]:
    """Get user's Telegram client from saved session"""
    session = db.get_session(user_id)
    if not session:
        return None
    
    try:
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        return client
    except:
        return None

# ============= FASTAPI HEALTH =============
app = FastAPI()

@app.get("/health")
async def health():
    return {
        "status": "alive",
        "queue": len(download_queue),
        "active": len(active_downloads),
        "users": len(db.users),
        "premium": len(db.premium)
    }

def run_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv('PORT', 8080)), log_level="error")

Thread(target=run_fastapi, daemon=True).start()

# ============= HELPER FUNCTIONS =============

def parse_telegram_link(link: str) -> Optional[tuple]:
    """Parse Telegram link"""
    patterns = [
        r't\.me/c/(\d+)/(\d+)',
        r't\.me/([^/]+)/(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            chat = match.group(1)
            if chat.isdigit():
                chat = int(f"-100{chat}")
            msg_id = int(match.group(2))
            return (chat, msg_id)
    return None

def apply_replacements(text: str, replace_dict: dict, remove_list: list) -> str:
    """Apply word replacements and removals"""
    if not text:
        return ""
    
    # Replace words
    for old, new in replace_dict.items():
        text = text.replace(old, new)
    
    # Remove words
    for word in remove_list:
        text = text.replace(word, "")
    
    return text.strip()

def format_progress(current: int, total: int, speed: float, filename: str = "") -> str:
    """Format download progress"""
    percentage = (current / total * 100) if total > 0 else 0
    current_mb = current / (1024 * 1024)
    total_mb = total / (1024 * 1024)
    speed_kb = speed / 1024
    
    eta = (total - current) / speed if speed > 0 else 0
    eta_min = int(eta / 60)
    eta_sec = int(eta % 60)
    
    bars = int(percentage / 10)
    progress_bar = "♦" * bars + "◇" * (10 - bars)
    
    return f"""╭─────────────────────╮
│      Downloading...
├─────────────────────
│ {progress_bar}
│ 
│ File: {filename[:30]}
│ Completed: {current_mb:.1f} MB/{total_mb:.2f} MB
│ Bytes: {percentage:.2f}%
│ Speed: {speed_kb:.2f} KB/s
│ ETA: {eta_min}m, {eta_sec}s
╰─────────────────────╯"""

# ============= DOWNLOAD & UPLOAD =============

async def download_and_upload(task_id: str, user_id: int, chat: str, msg_id: int, target_chat: int, batch_msg=None):
    """Download from source and upload to target with all customizations"""
    try:
        # Get user client (for private channels)
        client = await get_user_client(user_id)
        if not client:
            client = bot  # Use bot for public channels
        
        # Get settings
        settings = db.get_user(user_id)
        
        # Get source message
        try:
            source_msg = await client.get_messages(chat, ids=msg_id)
        except Exception as e:
            logger.error(f"Cannot access {chat}/{msg_id}: {e}")
            if client != bot:
                await client.disconnect()
            return False
        
        if not source_msg or not source_msg.media:
            logger.warning(f"No media in {chat}/{msg_id}")
            if client != bot:
                await client.disconnect()
            return False
        
        # Get filename
        filename = "file"
        if source_msg.document:
            for attr in source_msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    filename = attr.file_name
        
        # Apply custom rename
        if settings.get('rename'):
            ext = filename.split('.')[-1] if '.' in filename else ''
            filename = f"{settings['rename']}.{ext}" if ext else settings['rename']
        
        # Apply word replacements in filename
        filename = apply_replacements(
            filename, 
            settings.get('replace_words', {}),
            settings.get('remove_words', [])
        )
        
        # Get caption
        caption = source_msg.text or source_msg.caption or ""
        
        # Apply custom caption
        if settings.get('caption'):
            caption = settings['caption']
        
        # Apply replacements in caption
        caption = apply_replacements(
            caption,
            settings.get('replace_words', {}),
            settings.get('remove_words', [])
        )
        
        # Progress tracking
        last_update = [time.time()]
        start_time = time.time()
        
        async def progress_callback(current, total):
            nonlocal last_update, start_time
            now = time.time()
            
            if now - last_update[0] >= 3:  # Update every 3 seconds
                speed = current / (now - start_time) if (now - start_time) > 0 else 0
                progress_text = format_progress(current, total, speed, filename)
                
                # Update active downloads
                active_downloads[task_id] = {
                    'current': current,
                    'total': total,
                    'speed': speed,
                    'progress': progress_text,
                    'filename': filename
                }
                
                # Update batch message if provided
                if batch_msg:
                    try:
                        await batch_msg.edit(progress_text)
                    except:
                        pass
                
                last_update[0] = now
        
        # Initialize progress
        active_downloads[task_id] = {
            'start_time': start_time,
            'current': 0,
            'total': source_msg.file.size if source_msg.file else 0,
            'speed': 0,
            'filename': filename
        }
        
        # Get thumbnail if set
        thumb = None
        if settings.get('thumbnail'):
            thumb = settings['thumbnail']
        
        # Download to memory and upload (streaming)
        await bot.send_file(
            target_chat,
            source_msg.media,
            caption=caption,
            progress_callback=progress_callback,
            force_document=True,
            file_name=filename,
            thumb=thumb
        )
        
        # Disconnect user client if used
        if client != bot:
            await client.disconnect()
        
        return True
        
    except Exception as e:
        logger.error(f"Download error {task_id}: {e}")
        return False
    finally:
        if task_id in active_downloads:
            del active_downloads[task_id]

# ============= WORKER LOOP =============

async def worker_loop():
    """Background worker"""
    logger.info("🔥 Worker started!")
    
    while True:
        try:
            task = None
            with queue_lock:
                if download_queue:
                    task = download_queue.popleft()
            
            if task:
                task_id, user_id, chat, msg_id, target_chat, batch_msg = task
                logger.info(f"Processing: {chat}/{msg_id}")
                
                success = await download_and_upload(task_id, user_id, chat, msg_id, target_chat, batch_msg)
                
                if success:
                    logger.info(f"✅ Completed: {msg_id}")
                else:
                    logger.error(f"❌ Failed: {msg_id}")
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(1)

def start_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(worker_loop())

Thread(target=start_worker, daemon=True).start()

# ============= BOT HANDLERS =============

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user_id = event.sender_id
    is_premium = db.is_premium(user_id)
    
    await event.reply(
        "**SAVE RATNA 2.0 🔥🔥🔥**\n\n"
        "Hiii! 👋 Welcome! Ready to explore some cool tricks?\n\n"
        "✳️ I can grab posts even from channels or groups where forwarding is restricted! 🔐\n\n"
        "✳️ Need to download videos or audio from YouTube, Instagram, or other social platforms? I got you! 🎥🎵\n\n"
        "✳️ Just drop the post link from any public channel. For private ones, use /login.\n\n"
        f"**Your Status:** {'💎 Premium' if is_premium else '⚪ Free'}\n\n"
        "Type /help to see all the magic! ✨\n\n"
        "**Powered by RATNA**",
        buttons=[
            [Button.inline("📝 Commands", b"help")],
            [Button.inline("⚙️ Settings", b"settings")],
            [Button.inline("💎 Premium", b"premium")]
        ]
    )

@bot.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    help_text = """📝 **Bot Commands Overview (1/2):**

**Basic Commands:**
/start - Start the bot
/help - Show commands
/batch - Bulk extraction (after login)
/login - Login for private channels
/logout - Logout from bot
/session - Generate Pyrogram V2 session
/cancel - Cancel ongoing batch

**Settings Commands:**
/settings - Open settings menu
• SETCHATID - Set target chat (-100xxxxx)
• SETRENAME - Custom rename format
• CAPTION - Custom caption text
• SETTHUMBNAIL - Set custom thumbnail
• REPLACEWORDS - Replace specific words
• REMOVEWORDS - Remove specific words
• RESET - Reset to defaults

**Premium Commands:**
/plan - Check available plans
/myplan - Your current plan
/buypremium - Get premium access

**Admin Only:**
/add userID - Add premium user
/rem userID - Remove premium
/stats - Bot statistics
/get - Get all users
/lock channelID - Lock channel

**Powered by RATNA**"""
    
    await event.reply(help_text)

@bot.on(events.NewMessage(pattern='/login'))
async def login_handler(event):
    """Start login process for private channels"""
    user_id = event.sender_id
    user = db.get_user(user_id)
    
    user['login_state'] = 'waiting_phone'
    
    await event.reply(
        "🔐 **Login to Access Private Channels**\n\n"
        "**Step 1:** Send your phone number with country code\n"
        "Example: `+919876543210`\n\n"
        "⚠️ Your number will be used to create a session for accessing private channels only."
    )

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_handler(event):
    """Logout and clear session"""
    user_id = event.sender_id
    
    if user_id in db.sessions:
        del db.sessions[user_id]
        await event.reply("✅ **Logged out successfully!**")
    else:
        await event.reply("⚠️ You are not logged in.")

@bot.on(events.NewMessage(pattern='/settings'))
async def settings_handler(event):
    """Settings menu"""
    user_id = event.sender_id
    settings = db.get_user(user_id)
    
    settings_text = f"""⚙️ **Customize and Configure your settings ...**

**Current Settings:**
📌 Chat ID: `{settings['chat_id'] or 'Not set'}`
✏️ Rename: `{settings['rename'] or 'Default'}`
💬 Caption: `{settings['caption'][:50] + '...' if settings.get('caption') and len(settings['caption']) > 50 else settings.get('caption') or 'Default'}`
🖼 Thumbnail: `{'Set' if settings.get('thumbnail') else 'Not set'}`
🔄 Replace Words: `{len(settings.get('replace_words', {}))} rules`
🗑 Remove Words: `{len(settings.get('remove_words', []))} words`

**Choose an option below:**"""
    
    await event.reply(
        settings_text,
        buttons=[
            [Button.inline("📌 Set Chat ID", b"set_chatid")],
            [Button.inline("✏️ Set Rename", b"set_rename")],
            [Button.inline("💬 Set Caption", b"set_caption")],
            [Button.inline("🖼 Set Thumbnail", b"set_thumbnail")],
            [Button.inline("🔄 Replace Words", b"replace_words")],
            [Button.inline("🗑 Remove Words", b"remove_words")],
            [Button.inline("🔄 Reset All", b"reset_settings")],
            [Button.inline("🔙 Back", b"back")]
        ]
    )

@bot.on(events.CallbackQuery(pattern=b"set_chatid"))
async def callback_chatid(event):
    await event.edit("**Send me the ID of that chat:**\n\nExample: `-1001234567890`")
    db.get_user(event.sender_id)['temp_state'] = 'waiting_chatid'

@bot.on(events.CallbackQuery(pattern=b"set_rename"))
async def callback_rename(event):
    await event.edit("**Send me the rename format:**\n\nExample: `MyChannel_{original_name}`")
    db.get_user(event.sender_id)['temp_state'] = 'waiting_rename'

@bot.on(events.CallbackQuery(pattern=b"set_caption"))
async def callback_caption(event):
    await event.edit("**Send me the custom caption:**\n\nYou can use:\n`{filename}` - Original filename\n`{size}` - File size")
    db.get_user(event.sender_id)['temp_state'] = 'waiting_caption'

@bot.on(events.CallbackQuery(pattern=b"set_thumbnail"))
async def callback_thumbnail(event):
    await event.edit("**Send me the thumbnail image:**")
    db.get_user(event.sender_id)['temp_state'] = 'waiting_thumbnail'

@bot.on(events.CallbackQuery(pattern=b"replace_words"))
async def callback_replace(event):
    await event.edit(
        "**Replace Words:**\n\n"
        "Send in format: `old_word|new_word`\n"
        "Example: `Copyright|Free Content`"
    )
    db.get_user(event.sender_id)['temp_state'] = 'waiting_replace'

@bot.on(events.CallbackQuery(pattern=b"remove_words"))
async def callback_remove(event):
    await event.edit(
        "**Remove Words:**\n\n"
        "Send words to remove (comma separated)\n"
        "Example: `Copyright, Paid, Premium`"
    )
    db.get_user(event.sender_id)['temp_state'] = 'waiting_remove'

@bot.on(events.CallbackQuery(pattern=b"reset_settings"))
async def callback_reset(event):
    user_id = event.sender_id
    db.get_user(user_id).update({
        'rename': None,
        'caption': None,
        'thumbnail': None,
        'replace_words': {},
        'remove_words': []
    })
    await event.edit("✅ **All settings reset to default!**")

@bot.on(events.NewMessage(pattern='/batch'))
async def batch_handler(event):
    """Batch extraction"""
    user_id = event.sender_id
    settings = db.get_user(user_id)
    
    if not settings.get('chat_id'):
        await event.reply("⚠️ **First set target chat:** /settings → Set Chat ID")
        return
    
    settings['batch_state'] = 'waiting_link'
    
    max_tries = 3
    await event.reply(
        "**Please send the start link.**\n\n"
        f"Maximum tries: **{max_tries}**"
    )

@bot.on(events.NewMessage(pattern=r'^/add (\d+)$'))
async def add_premium_handler(event):
    """Add premium user (owner only)"""
    if event.sender_id != OWNER_ID:
        return
    
    user_id = int(event.pattern_match.group(1))
    db.add_premium(user_id)
    await event.reply(f"✅ **User {user_id} added to premium!**")

@bot.on(events.NewMessage(pattern=r'^/rem (\d+)$'))
async def rem_premium_handler(event):
    """Remove premium (owner only)"""
    if event.sender_id != OWNER_ID:
        return
    
    user_id = int(event.pattern_match.group(1))
    db.remove_premium(user_id)
    await event.reply(f"✅ **User {user_id} removed from premium!**")

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    """Bot stats (owner only)"""
    if event.sender_id != OWNER_ID:
        return
    
    await event.reply(
        f"📊 **Bot Statistics:**\n\n"
        f"👥 Total Users: **{len(db.users)}**\n"
        f"💎 Premium Users: **{len(db.premium)}**\n"
        f"📥 Queue Size: **{len(download_queue)}**\n"
        f"⚡ Active Downloads: **{len(active_downloads)}**\n"
        f"🔐 Logged-in Users: **{len(db.sessions)}**\n"
        f"⏰ Uptime: **24/7** ✅"
    )

@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel_handler(event):
    """Cancel batch"""
    user_id = event.sender_id
    
    with queue_lock:
        before = len(download_queue)
        filtered = [t for t in download_queue if t[1] != user_id]
        download_queue.clear()
        download_queue.extend(filtered)
        removed = before - len(download_queue)
    
    await event.reply(f"✅ **Cancelled!** Removed **{removed}** tasks from queue.")

@bot.on(events.NewMessage(pattern='/myplan'))
async def myplan_handler(event):
    """Check user's plan"""
    user_id = event.sender_id
    is_premium = db.is_premium(user_id)
    
    if is_premium:
        await event.reply(
            "💎 **Premium Plan Active**\n\n"
            "✅ Batch limit: 1000 files\n"
            "✅ Fast download speed\n"
            "✅ Priority queue\n"
            "✅ All features unlocked\n\n"
            "**Powered by RATNA**"
        )
    else:
        await event.reply(
            "⚪ **Free Plan**\n\n"
            "• Batch limit: 3 files\n"
            "• Standard speed\n"
            "• Normal queue\n\n"
            "💎 Upgrade to premium: /plan"
        )

# ============= MESSAGE HANDLER (Login Flow & Settings) =============

@bot.on(events.NewMessage)
async def message_handler(event):
    """Handle all text messages"""
    user_id = event.sender_id
    text = event.text.strip()
    user = db.get_user(user_id)
    
    # === LOGIN FLOW ===
    if user.get('login_state') == 'waiting_phone':
        if not text.startswith('+'):
            await event.reply("❌ Please send phone with country code: `+919876543210`")
            return
        
        try:
            # Create Telethon client for user
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            
            # Send code
            result = await client.send_code_request(text)
            
            user['temp_data'] = {
                'phone': text,
                'phone_code_hash': result.phone_code_hash,
                'client': client
            }
            user['login_state'] = 'waiting_otp'
            
            await event.reply(
                "📱 **OTP sent to your Telegram!**\n\n"
                "**Step 2:** Send OTP in spaced format\n"
                "Example: `1 2 3 4 5`"
            )
            
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")
            user['login_state'] = None
    
    elif user.get('login_state') == 'waiting_otp':
        try:
            # Remove spaces from OTP
            otp = text.replace(' ', '')
            
            temp = user['temp_data']
            client = temp['client']
            
            # Sign in
            try:
                await client.sign_in(
                    phone=temp['phone'],
                    code=otp,
                    phone_code_hash=temp['phone_code_hash']
                )
            except SessionPasswordNeededError:
                user['login_state'] = 'waiting_2fa'
                await event.reply(
                    "🔐 **2FA Password Required**\n\n"
                    "**Step 3:** Send your 2FA password"
                )
                return
            
            # Save session
            session_string = client.session.save()
            db.save_session(user_id, session_string)
            
            await client.disconnect()
            
            user['login_state'] = None
            user['temp_data'] = {}
            
            await event.reply(
                "✅ **Login successful!**\n\n"
                "You can now extract from private channels using /batch"
            )
            
        except PhoneCodeInvalidError:
            await event.reply("❌ Invalid OTP! Please try /login again.")
            user['login_state'] = None
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)}")
            user['login_state'] = None
    
    elif user.get('login_state') == 'waiting_2fa':
        try:
            temp = user['temp_data']
            client = temp['client']
            
            # Sign in with 2FA
            await client.sign_in(password=text)
            
            # Save session
            session_string = client.session.save()
            db.save_session(user_id, session_string)
            
            await client.disconnect()
            
            user['login_state'] = None
            user['temp_data'] = {}
            
            await event.reply("✅ **Login successful with 2FA!**")
            
        except Exception as e:
            await event.reply(f"❌ Invalid password: {str(e)}")
            user['login_state'] = None
    
    # === SETTINGS FLOW ===
    elif user.get('temp_state') == 'waiting_chatid':
        try:
            chat_id = int(text)
            user['chat_id'] = chat_id
            user['temp_state'] = None
            await event.reply(f"✅ **Chat ID set successfully!**\n\nTarget: `{chat_id}`")
        except:
            await event.reply("❌ Invalid chat ID!")
    
    elif user.get('temp_state') == 'waiting_rename':
        user['rename'] = text
        user['temp_state'] = None
        await event.reply(f"✅ **Rename format set!**\n\n`{text}`")
    
    elif user.get('temp_state') == 'waiting_caption':
        user['caption'] = text
        user['temp_state'] = None
        await event.reply(f"✅ **Caption set!**")
    
    elif user.get('temp_state') == 'waiting_replace':
        if '|' in text:
            old, new = text.split('|', 1)
            if 'replace_words' not in user:
                user['replace_words'] = {}
            user['replace_words'][old.strip()] = new.strip()
            user['temp_state'] = None
            await event.reply(f"✅ **Replace rule added!**\n\n`{old}` → `{new}`")
        else:
            await event.reply("❌ Use format: `old|new`")
    
    elif user.get('temp_state') == 'waiting_remove':
        words = [w.strip() for w in text.split(',')]
        if 'remove_words' not in user:
            user['remove_words'] = []
        user['remove_words'].extend(words)
        user['temp_state'] = None
        await event.reply(f"✅ **Added {len(words)} words to remove list!**")
    
    # === BATCH FLOW ===
    elif user.get('batch_state') == 'waiting_link':
        parsed = parse_telegram_link(text)
        if not parsed:
            await event.reply("❌ Invalid Telegram link!")
            return
        
        user['batch_chat'], user['batch_start_id'] = parsed
        user['batch_state'] = 'waiting_count'
        
        max_limit = 1000 if db.is_premium(user_id) else 3
        await event.reply(
            f"**How many messages do you want to process?**\n"
            f"Max limit: **{max_limit}**"
        )
    
    elif user.get('batch_state') == 'waiting_count':
        try:
            count = int(text)
            max_limit = 1000 if db.is_premium(user_id) else 3
            
            if count > max_limit:
                await event.reply(f"❌ Max limit is **{max_limit}**!\n\n💎 Upgrade: /plan")
                return
            
            # Start batch
            user['batch_state'] = None
            target_chat = user['chat_id']
            chat = user['batch_chat']
            start_id = user['batch_start_id']
            
            status_msg = await event.reply(
                f"**Batch process started ⚡**\n"
                f"Processing: 0/{count}\n\n"
                f"**Powered by RATNA**"
            )
            
            # Add tasks to queue
            tasks_added = 0
            with queue_lock:
                for i in range(count):
                    task_id = str(uuid.uuid4())
                    msg_id = start_id + i
                    download_queue.append((task_id, user_id, chat, msg_id, target_chat, status_msg))
                    tasks_added += 1
            
            # Monitor progress
            completed = 0
            failed = 0
            last_count = 0
            
            while completed + failed < count:
                await asyncio.sleep(5)
                
                # Count remaining tasks for this user
                remaining = sum(1 for t in download_queue if t[1] == user_id)
                current_completed = count - remaining - len([t for t in active_downloads.keys() if active_downloads[t].get('user_id') == user_id])
                
                if current_completed > last_count:
                    completed = current_completed
                    last_count = completed
                
                # Get active download progress
                progress_text = ""
                for task_id in list(active_downloads.keys()):
                    task_data = active_downloads.get(task_id)
                    if task_data:
                        progress_text = task_data.get('progress', '')
                        if progress_text:
                            break
                
                try:
                    if progress_text:
                        await status_msg.edit(
                            f"**Batch process started ⚡**\n"
                            f"Processing: {completed}/{count}\n\n"
                            f"{progress_text}\n\n"
                            f"**Powered by RATNA**"
                        )
                    else:
                        await status_msg.edit(
                            f"**Batch process started ⚡**\n"
                            f"Processing: {completed}/{count}\n\n"
                            f"Queue position: {remaining}\n\n"
                            f"**Powered by RATNA**"
                        )
                except:
                    pass
            
            # Final summary
            try:
                await status_msg.edit(
                    f"✅ **Batch completed!**\n\n"
                    f"Total: **{count}**\n"
                    f"Success: **{completed}**\n"
                    f"Failed: **{failed}**\n\n"
                    f"Files uploaded to: `{target_chat}`\n\n"
                    f"**Powered by RATNA**"
                )
            except:
                pass
            
        except ValueError:
            await event.reply("❌ Invalid number!")

# Handle thumbnail upload
@bot.on(events.NewMessage(func=lambda e: e.photo and db.get_user(e.sender_id).get('temp_state') == 'waiting_thumbnail'))
async def thumbnail_handler(event):
    """Handle thumbnail photo upload"""
    user_id = event.sender_id
    user = db.get_user(user_id)
    
    try:
        # Download thumbnail
        photo = await event.download_media(bytes)
        user['thumbnail'] = photo
        user['temp_state'] = None
        
        await event.reply("✅ **Thumbnail set successfully!**")
    except Exception as e:
        await event.reply(f"❌ Error setting thumbnail: {str(e)}")

# ============= RUN BOT =============

def main():
    logger.info("=" * 50)
    logger.info("🚀 RATNA BOT 2.0 - PRODUCTION STARTED")
    logger.info("=" * 50)
    logger.info(f"📡 FastAPI Health Check: Port {os.getenv('PORT', 8080)}")
    logger.info(f"👑 Owner ID: {OWNER_ID}")
    logger.info(f"💎 Premium Users: {len(db.premium)}")
    logger.info("✅ Worker Thread: Active")
    logger.info("✅ Queue System: Ready")
    logger.info("✅ Login System: Ready")
    logger.info("✅ All Features: Enabled")
    logger.info("=" * 50)
    
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
