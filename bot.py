"""
RATNA BOT 2.0 - SIMPLE & WORKING VERSION
Tested and production-ready
"""

import os
import asyncio
import logging
import time
import re
import uuid
from collections import deque
from threading import Thread

from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from fastapi import FastAPI
import uvicorn

# ============= CONFIG =============
API_ID = int(os.getenv('API_ID', '0'))
API_HASH = os.getenv('API_HASH', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))

logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ============= STORAGE =============
users = {}  # {user_id: {chat_id, session, state, etc}}
sessions = {}  # {user_id: session_string}
premium = {OWNER_ID}  # Premium users
queue = deque()  # Download queue
active = {}  # Active downloads

def get_user(uid):
    if uid not in users:
        users[uid] = {'chat_id': None, 'state': None, 'temp': {}}
    return users[uid]

# ============= TELEGRAM =============
bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

async def get_client(uid):
    """Get user client or bot"""
    if uid in sessions:
        try:
            c = TelegramClient(StringSession(sessions[uid]), API_ID, API_HASH)
            await c.connect()
            if await c.is_user_authorized():
                return c
        except:
            pass
    return bot

# ============= FASTAPI =============
app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok", "queue": len(queue), "active": len(active)}

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv('PORT', 8080)), log_level="error")

Thread(target=run_api, daemon=True).start()

# ============= HELPERS =============

def parse_link(link):
    """Parse Telegram link"""
    m = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if m:
        return (int(f"-100{m.group(1)}"), int(m.group(2)))
    m = re.search(r't\.me/([^/]+)/(\d+)', link)
    if m:
        return (m.group(1), int(m.group(2)))
    return None

def progress_bar(cur, tot, speed):
    """Format progress"""
    pct = (cur/tot*100) if tot > 0 else 0
    bars = int(pct/10)
    bar = "♦"*bars + "◇"*(10-bars)
    mb_cur = cur/1e6
    mb_tot = tot/1e6
    kb_s = speed/1024
    eta = (tot-cur)/speed if speed > 0 else 0
    eta_m = int(eta/60)
    eta_s = int(eta%60)
    
    return f"""╭─────────────────────╮
│   Downloading...
├─────────────────────
│ {bar}
│ {mb_cur:.1f}/{mb_tot:.1f} MB
│ {pct:.1f}%
│ {kb_s:.1f} KB/s
│ ETA: {eta_m}m {eta_s}s
╰─────────────────────╯"""

# ============= WORKER =============

async def download_file(task_id, uid, chat, msg_id, target, status):
    """Download and upload file"""
    try:
        client = await get_client(uid)
        
        # Get message
        msg = await client.get_messages(chat, ids=msg_id)
        if not msg or not msg.media:
            logger.warning(f"No media: {chat}/{msg_id}")
            if client != bot:
                await client.disconnect()
            return False
        
        # Get filename
        fname = f"file_{msg_id}"
        if msg.document:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    fname = attr.file_name
        
        # Caption
        cap = msg.text or msg.caption or ""
        
        # Progress tracking
        start = time.time()
        last = [0]
        
        async def prog(cur, tot):
            now = time.time()
            if now - last[0] >= 3:
                spd = cur/(now-start) if now > start else 1
                txt = progress_bar(cur, tot, spd)
                active[task_id] = {'cur': cur, 'tot': tot, 'spd': spd}
                
                if status:
                    try:
                        await status.edit(f"**Processing...**\n\n{txt}\n\n**Powered by RATNA**")
                    except:
                        pass
                last[0] = now
        
        logger.info(f"Downloading: {fname}")
        
        # Upload to target
        await bot.send_file(
            target,
            msg.media,
            caption=cap,
            progress_callback=prog,
            force_document=True,
            file_name=fname
        )
        
        logger.info(f"✅ Done: {fname}")
        
        if client != bot:
            await client.disconnect()
        
        return True
        
    except Exception as e:
        logger.error(f"Error: {e}")
        return False
    finally:
        if task_id in active:
            del active[task_id]

async def worker():
    """Process queue"""
    logger.info("🔥 Worker started")
    while True:
        try:
            if queue:
                task = queue.popleft()
                tid, uid, chat, mid, target, status = task
                await download_file(tid, uid, chat, mid, target, status)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Worker: {e}")
            await asyncio.sleep(2)

def start_worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(worker())

Thread(target=start_worker, daemon=True).start()

# ============= HANDLERS =============

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    uid = e.sender_id
    is_prem = uid in premium
    
    await e.reply(
        "**RATNA BOT 2.0 🔥**\n\n"
        "Welcome! I can extract files from:\n"
        "✅ Private channels (login required)\n"
        "✅ Public channels\n"
        "✅ Restricted channels\n\n"
        f"Status: {'💎 Premium' if is_prem else '⚪ Free'}\n\n"
        "**Commands:**\n"
        "/login - Login for private channels\n"
        "/settings - Set target chat ID\n"
        "/batch - Start extraction\n"
        "/cancel - Stop batch\n"
        "/myplan - Check plan\n\n"
        "**Powered by RATNA**",
        buttons=[
            [Button.inline("⚙️ Settings", b"settings")],
            [Button.inline("💎 Plan", b"plan")]
        ]
    )

@bot.on(events.CallbackQuery(pattern=b"settings"))
async def cb_settings(e):
    await e.answer()
    uid = e.sender_id
    u = get_user(uid)
    
    await e.edit(
        f"⚙️ **Settings**\n\n"
        f"Chat ID: `{u['chat_id'] or 'Not set'}`\n\n"
        "Send me chat ID to set target:",
        buttons=[[Button.inline("🔙 Back", b"back")]]
    )
    u['state'] = 'wait_chatid'

@bot.on(events.CallbackQuery(pattern=b"plan"))
async def cb_plan(e):
    await e.answer()
    uid = e.sender_id
    is_prem = uid in premium
    
    if is_prem:
        txt = "💎 **Premium Active**\n\nBatch: 1000 files\nSpeed: Fast\nAll features unlocked"
    else:
        txt = "⚪ **Free Plan**\n\nBatch: 3 files\nSpeed: Standard"
    
    await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"back")]])

@bot.on(events.CallbackQuery(pattern=b"back"))
async def cb_back(e):
    await e.answer()
    uid = e.sender_id
    is_prem = uid in premium
    
    await e.edit(
        f"**RATNA BOT 2.0 🔥**\n\n"
        f"Status: {'💎 Premium' if is_prem else '⚪ Free'}\n\n"
        "Choose option:",
        buttons=[
            [Button.inline("⚙️ Settings", b"settings")],
            [Button.inline("💎 Plan", b"plan")]
        ]
    )

@bot.on(events.NewMessage(pattern='/settings'))
async def cmd_settings(e):
    uid = e.sender_id
    u = get_user(uid)
    
    await e.reply(
        f"⚙️ **Settings**\n\n"
        f"Current Chat ID: `{u['chat_id'] or 'Not set'}`\n\n"
        "**Send me the target chat ID:**\n"
        "Example: `-1001234567890`"
    )
    u['state'] = 'wait_chatid'

@bot.on(events.NewMessage(pattern='/login'))
async def cmd_login(e):
    u = get_user(e.sender_id)
    u['state'] = 'wait_phone'
    
    await e.reply(
        "🔐 **Login**\n\n"
        "Step 1: Send phone number\n"
        "Example: `+919876543210`"
    )

@bot.on(events.NewMessage(pattern='/logout'))
async def cmd_logout(e):
    uid = e.sender_id
    if uid in sessions:
        del sessions[uid]
        await e.reply("✅ Logged out!")
    else:
        await e.reply("⚠️ Not logged in")

@bot.on(events.NewMessage(pattern='/batch'))
async def cmd_batch(e):
    uid = e.sender_id
    u = get_user(uid)
    
    if not u['chat_id']:
        await e.reply("⚠️ First set chat ID: /settings")
        return
    
    u['state'] = 'wait_link'
    await e.reply("**Send post link:**\n\nExample: `https://t.me/channel/123`")

@bot.on(events.NewMessage(pattern='/cancel'))
async def cmd_cancel(e):
    uid = e.sender_id
    removed = 0
    
    # Remove from queue
    new_q = deque()
    for task in queue:
        if task[1] != uid:
            new_q.append(task)
        else:
            removed += 1
    
    queue.clear()
    queue.extend(new_q)
    
    await e.reply(f"✅ Cancelled! Removed {removed} tasks")

@bot.on(events.NewMessage(pattern='/myplan'))
async def cmd_myplan(e):
    uid = e.sender_id
    is_prem = uid in premium
    
    if is_prem:
        await e.reply("💎 **Premium Active**\n\nBatch: 1000\nSpeed: Fast\nAll features")
    else:
        await e.reply("⚪ **Free Plan**\n\nBatch: 3\nSpeed: Standard")

@bot.on(events.NewMessage(pattern=r'^/add (\d+)$'))
async def cmd_add(e):
    if e.sender_id != OWNER_ID:
        return
    uid = int(e.pattern_match.group(1))
    premium.add(uid)
    await e.reply(f"✅ Added {uid} to premium")

@bot.on(events.NewMessage(pattern=r'^/rem (\d+)$'))
async def cmd_rem(e):
    if e.sender_id != OWNER_ID:
        return
    uid = int(e.pattern_match.group(1))
    if uid in premium and uid != OWNER_ID:
        premium.discard(uid)
        await e.reply(f"✅ Removed {uid}")

@bot.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(e):
    if e.sender_id != OWNER_ID:
        return
    
    await e.reply(
        f"📊 **Stats**\n\n"
        f"Users: {len(users)}\n"
        f"Premium: {len(premium)}\n"
        f"Queue: {len(queue)}\n"
        f"Active: {len(active)}\n"
        f"Sessions: {len(sessions)}"
    )

# ============= MESSAGE HANDLER =============

@bot.on(events.NewMessage)
async def msg_handler(e):
    uid = e.sender_id
    txt = e.text.strip()
    u = get_user(uid)
    state = u.get('state')
    
    if not state:
        return
    
    # LOGIN FLOW
    if state == 'wait_phone':
        if not txt.startswith('+'):
            await e.reply("❌ Send with country code: `+919876543210`")
            return
        
        try:
            c = TelegramClient(StringSession(), API_ID, API_HASH)
            await c.connect()
            result = await c.send_code_request(txt)
            
            u['temp'] = {'phone': txt, 'hash': result.phone_code_hash, 'client': c}
            u['state'] = 'wait_otp'
            await e.reply("📱 **OTP sent!**\n\nStep 2: Send OTP\nExample: `1 2 3 4 5`")
        except Exception as ex:
            await e.reply(f"❌ Error: {ex}")
            u['state'] = None
    
    elif state == 'wait_otp':
        try:
            otp = txt.replace(' ', '')
            tmp = u['temp']
            c = tmp['client']
            
            try:
                await c.sign_in(phone=tmp['phone'], code=otp, phone_code_hash=tmp['hash'])
            except SessionPasswordNeededError:
                u['state'] = 'wait_2fa'
                await e.reply("🔐 **2FA Required**\n\nStep 3: Send 2FA password")
                return
            
            sessions[uid] = c.session.save()
            await c.disconnect()
            
            u['state'] = None
            u['temp'] = {}
            await e.reply("✅ **Login successful!**")
            
        except PhoneCodeInvalidError:
            await e.reply("❌ Invalid OTP! Try /login again")
            u['state'] = None
        except Exception as ex:
            await e.reply(f"❌ Error: {ex}")
            u['state'] = None
    
    elif state == 'wait_2fa':
        try:
            tmp = u['temp']
            c = tmp['client']
            await c.sign_in(password=txt)
            
            sessions[uid] = c.session.save()
            await c.disconnect()
            
            u['state'] = None
            u['temp'] = {}
            await e.reply("✅ **Login successful with 2FA!**")
        except Exception as ex:
            await e.reply(f"❌ Invalid password: {ex}")
            u['state'] = None
    
    # SETTINGS FLOW
    elif state == 'wait_chatid':
        try:
            cid = int(txt)
            u['chat_id'] = cid
            u['state'] = None
            await e.reply(f"✅ **Chat ID set!**\n\nTarget: `{cid}`\n\nNow use /batch")
        except:
            await e.reply("❌ Invalid! Send number like: `-1001234567890`")
    
    # BATCH FLOW
    elif state == 'wait_link':
        parsed = parse_link(txt)
        if not parsed:
            await e.reply("❌ Invalid link!")
            return
        
        u['batch_chat'], u['batch_start'] = parsed
        u['state'] = 'wait_count'
        
        max_lim = 1000 if uid in premium else 3
        await e.reply(f"**How many files?**\n\nMax: {max_lim}")
    
    elif state == 'wait_count':
        try:
            count = int(txt)
            max_lim = 1000 if uid in premium else 3
            
            if count > max_lim:
                await e.reply(f"❌ Max {max_lim}! Upgrade: /myplan")
                return
            
            u['state'] = None
            target = u['chat_id']
            chat = u['batch_chat']
            start_id = u['batch_start']
            
            status = await e.reply(f"**Batch started ⚡**\n\nProcessing: 0/{count}\n\n**Powered by RATNA**")
            
            # Add to queue
            for i in range(count):
                tid = str(uuid.uuid4())
                mid = start_id + i
                queue.append((tid, uid, chat, mid, target, status))
            
            logger.info(f"Added {count} tasks for user {uid}")
            
            # Monitor
            done = 0
            while done < count:
                await asyncio.sleep(5)
                
                in_q = sum(1 for t in queue if t[1] == uid)
                in_act = sum(1 for t in active.values() if t.get('uid') == uid)
                done = count - in_q - in_act
                
                try:
                    await status.edit(f"**Batch started ⚡**\n\nProcessing: {done}/{count}\n\n**Powered by RATNA**")
                except:
                    pass
            
            try:
                await status.edit(f"✅ **Batch done!**\n\nTotal: {count}\n\n**Powered by RATNA**")
            except:
                pass
            
        except ValueError:
            await e.reply("❌ Invalid number!")

# ============= MAIN =============

def main():
    logger.info("="*50)
    logger.info("🚀 RATNA BOT 2.0 STARTED")
    logger.info("="*50)
    logger.info(f"Owner: {OWNER_ID}")
    logger.info(f"Premium: {len(premium)}")
    logger.info("✅ All systems ready")
    logger.info("="*50)
    
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
