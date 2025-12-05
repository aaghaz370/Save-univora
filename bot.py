"""
RATNA-STYLE TELEGRAM EXTRACT BOT
- Telethon + FastAPI + Uvicorn
- Batch extraction with queue + progress bar
- Private / restricted channels via user login (session)
- Advanced settings:
    - SETCHATID   (/setchatid)
    - SETRENAME   (/setrename)
    - CAPTION     (/setcaption)   -> supports {original} and {tag}
    - REPLACEWORDS (/setreplace)  -> old => new mappings
    - RESET       (/resetsettings)
- Premium system:
    - OWNER_ID + /add userID -> 1000 batch
    - Others -> 3 batch
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
# users: per-user settings + state
users = {}      # {user_id: {chat_id, state, temp, caption, rename_tag, replace_words, thumb_id}}
sessions = {}   # {user_id: session_string}
premium = {OWNER_ID}  # Premium users (1000 batch)
queue = deque()       # Download queue
active = {}           # Active downloads -> {task_id: {cur, tot, spd, uid}}

def get_user(uid: int):
    """
    Get or init user config.
    New keys:
        caption: custom caption template (supports {original}, {tag})
        rename_tag: e.g. "@YourChannel"
        replace_words: dict {old: new} for caption replacement
        thumb_id: reserved for future custom thumbnail usage
    """
    if uid not in users:
        users[uid] = {
            'chat_id': None,
            'state': None,
            'temp': {},
            'caption': None,
            'rename_tag': None,
            'replace_words': {},
            'thumb_id': None,
        }
    return users[uid]

# ============= TELEGRAM =============
bot = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

async def get_client(uid: int):
    """
    Get user client (logged-in session) or fall back to bot.
    This is exactly how RATNA-style private extraction works:
    - If user session is authorized and member of private channel -> can fetch posts.
    - Else -> use bot (only for public/accessible chats).
    """
    if uid in sessions:
        try:
            c = TelegramClient(StringSession(sessions[uid]), API_ID, API_HASH)
            await c.connect()
            if await c.is_user_authorized():
                return c
        except Exception as e:
            logger.error(f"get_client: Error using user session for {uid}: {e}")
    return bot

# ============= FASTAPI =============
app = FastAPI()

@app.get("/health")
def health():
    """
    Health endpoint for Render + UptimeRobot.
    """
    return {"status": "ok", "queue": len(queue), "active": len(active)}

def run_api():
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv('PORT', 8080)), log_level="error")

# Start FastAPI in background thread
Thread(target=run_api, daemon=True).start()

# ============= HELPERS =============

def parse_link(link: str):
    """
    Parse Telegram post link.
    Supports:
        https://t.me/c/<internal_id>/<msg_id>
        https://t.me/<username>/<msg_id>
    Returns: (chat, msg_id) or None
    """
    m = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if m:
        return (int(f"-100{m.group(1)}"), int(m.group(2)))
    m = re.search(r't\.me/([^/]+)/(\d+)', link)
    if m:
        return (m.group(1), int(m.group(2)))
    return None

def progress_bar(cur: int, tot: int, speed: float):
    """
    Make nice ASCII progress box like RATNA.
    """
    pct = (cur / tot * 100) if tot > 0 else 0
    bars = int(pct / 10)
    bar = "♦" * bars + "◇" * (10 - bars)
    mb_cur = cur / 1e6
    mb_tot = tot / 1e6
    kb_s = speed / 1024
    eta = (tot - cur) / speed if speed > 0 else 0
    eta_m = int(eta / 60)
    eta_s = int(eta % 60)

    return f"""╭─────────────────────╮
│   Downloading...
├─────────────────────
│ {bar}
│ {mb_cur:.1f}/{mb_tot:.1f} MB
│ {pct:.1f}%
│ {kb_s:.1f} KB/s
│ ETA: {eta_m}m {eta_s}s
╰─────────────────────╯"""

def apply_caption_logic(uid: int, original_caption: str) -> str:
    """
    Apply user settings on caption:
    1) replace_words -> text replace
    2) caption template with {original} and {tag}
    3) if no template but rename_tag set -> append tag
    """
    u = get_user(uid)
    cap = original_caption or ""

    # 1) Replace words
    replace_map = u.get('replace_words') or {}
    for old, new in replace_map.items():
        if old:
            cap = cap.replace(old, new)

    tag = u.get('rename_tag') or ""

    # 2) Caption template
    template = u.get('caption')
    if template:
        final_cap = template.replace('{original}', cap).replace('{tag}', tag)
        return final_cap

    # 3) If no template but tag present -> append
    if tag:
        if cap:
            return f"{cap}\n\n{tag}"
        else:
            return tag

    # Default: just processed caption
    return cap

# ============= WORKER =============

async def download_file(task_id: str, uid: int, chat, msg_id: int, target, status_msg):
    """
    Download & upload a single message's media.
    Uses user session when possible, else bot.
    Does NOT store big files on disk, just streams via Telethon.
    """
    client = None
    try:
 
logger.info(f"[{task_id}] Starting upload to target {target}")

        # 🔥 IMPORTANT FIX:
        # Jis client se message fetch kiya (user session ya bot),
        # Usi client se upload bhi karenge.
        # - Private channel read + upload => user account
        # - Public / normal cases => bot
        upload_client = client  # client already user ya bot hoga

        try:
            uploaded_msg = await upload_client.send_file(
                target,
                msg.media,
                caption=final_caption,
                progress_callback=prog,
                force_document=True,
                file_name=fname
            )
            logger.info(f"[{task_id}] ✅ Upload successful, Msg ID: {uploaded_msg.id}")
        except Exception as upload_err:
            logger.error(f"[{task_id}] ❌ Upload failed: {upload_err}")
            if status_msg:
                try:
                    await status_msg.edit(
                        f"❌ **Upload failed for message `{msg_id}`**\n\n"
                        f"Reason: `{upload_client.__class__.__name__}` error\n"
                        f"Details console log mein dekho.\n\n"
                        "**Powered by RATNA**"
                    )
                except Exception:
                    pass

            if upload_client != bot:
                try:
                    await upload_client.disconnect()
                except:
                    pass
            return False


        # Determine filename
        fname = f"file_{msg_id}"
        if msg.document:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    fname = attr.file_name

        size_bytes = msg.file.size if msg.file else 0
        logger.info(f"[{task_id}] File: {fname}, Size: {size_bytes} bytes")

        # Prepare caption
        original_cap = msg.text or msg.caption or ""
        final_caption = apply_caption_logic(uid, original_cap)

        # Progress tracking
        start = time.time()
        last = [0]

        async def prog(cur, tot):
            now = time.time()
            if now - last[0] >= 3:
                spd = cur / (now - start) if now > start else 1
                active[task_id] = {'cur': cur, 'tot': tot, 'spd': spd, 'uid': uid}

                pct = (cur / tot * 100) if tot > 0 else 0
                logger.info(f"[{task_id}] Progress: {pct:.1f}% ({cur}/{tot} bytes) @ {spd/1024:.1f} KB/s")

                if status_msg:
                    try:
                        txt = progress_bar(cur, tot, spd)
                        await status_msg.edit(
                            f"**Downloading: {fname[:40]}**\n\n{txt}\n\n**Powered by RATNA**"
                        )
                    except Exception as e:
                        logger.error(f"[{task_id}] Status update error: {e}")
                last[0] = now

        logger.info(f"[{task_id}] Starting upload to target {target}")

        # Upload using bot (not user client) so everything goes from your bot to target chat
        try:
            uploaded_msg = await bot.send_file(
                target,
                msg.media,
                caption=final_caption,
                progress_callback=prog,
                force_document=True,
                file_name=fname
            )
            logger.info(f"[{task_id}] ✅ Upload successful, Msg ID: {uploaded_msg.id}")
        except Exception as upload_err:
            logger.error(f"[{task_id}] ❌ Upload failed: {upload_err}")
            if client != bot:
                try:
                    await client.disconnect()
                except:
                    pass
            return False

        if client != bot:
            try:
                await client.disconnect()
            except:
                pass

        return True

    except Exception as e:
        logger.error(f"[{task_id}] ❌ Fatal error: {e}", exc_info=True)
        if client and client != bot:
            try:
                await client.disconnect()
            except:
                pass
        return False
    finally:
        if task_id in active:
            del active[task_id]


async def worker():
    """
    Download queue worker – runs inside bot's event loop.
    Processes tasks one-by-one, others stay queued.
    """
    logger.info("🔥 Worker started")
    while True:
        try:
            if queue:
                task = queue.popleft()
                tid, uid, chat, mid, target, status = task
                await download_file(tid, uid, chat, mid, target, status)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(2)

# ============= HANDLERS =============

@bot.on(events.NewMessage(pattern='/start'))
async def cmd_start(e):
    uid = e.sender_id
    is_prem = uid in premium

    await e.reply(
        "**RATNA STYLE BOT 🔥**\n\n"
        "I can extract files from:\n"
        "✅ Private channels (after /login & if your account is member)\n"
        "✅ Public channels\n"
        "✅ Restricted channels (where your account has access)\n\n"
        f"Status: {'💎 Premium (1000 batch)' if is_prem else '⚪ Free (3 batch)'}\n\n"
        "**Main Commands:**\n"
        "/login - Login for private channels\n"
        "/settings - Show settings help\n"
        "/setchatid - Set target chat ID\n"
        "/setcaption - Set custom caption template\n"
        "/setrename - Set rename tag (e.g. @YourChannel)\n"
        "/setreplace - Configure word replacements\n"
        "/resetsettings - Reset all settings\n"
        "/batch - Start extraction\n"
        "/cancel - Cancel your batch\n"
        "/myplan - Check your plan\n\n"
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

    # Build short preview
    cap_preview = u['caption'] or "Default (original caption)"
    tag_preview = u['rename_tag'] or "Not set"
    rep_map = u['replace_words'] or {}
    rep_preview = ", ".join([f"'{k}'→'{v}'" for k, v in rep_map.items()]) or "None"

    await e.edit(
        "⚙️ **Settings Overview**\n\n"
        f"Chat ID: `{u['chat_id'] or 'Not set'}`\n"
        f"Caption template: `{cap_preview}`\n"
        f"Rename tag: `{tag_preview}`\n"
        f"Replace words: {rep_preview}\n\n"
        "**Commands:**\n"
        "`/setchatid` - set target chat\n"
        "`/setcaption` - set caption template\n"
        "`/setrename` - set rename tag\n"
        "`/setreplace` - set word replacements\n"
        "`/resetsettings` - reset everything",
        buttons=[[Button.inline("🔙 Back", b"back")]]
    )

@bot.on(events.CallbackQuery(pattern=b"plan"))
async def cb_plan(e):
    await e.answer()
    uid = e.sender_id
    is_prem = uid in premium

    if is_prem:
        txt = "💎 **Premium Active**\n\nBatch limit: 1000 files\nSpeed: Fast\nAll features unlocked."
    else:
        txt = "⚪ **Free Plan**\n\nBatch limit: 3 files\nSpeed: Standard."
    await e.edit(txt, buttons=[[Button.inline("🔙 Back", b"back")]])

@bot.on(events.CallbackQuery(pattern=b"back"))
async def cb_back(e):
    await e.answer()
    uid = e.sender_id
    is_prem = uid in premium

    await e.edit(
        f"**RATNA STYLE BOT 🔥**\n\n"
        f"Status: {'💎 Premium (1000 batch)' if is_prem else '⚪ Free (3 batch)'}\n\n"
        "Choose option:",
        buttons=[
            [Button.inline("⚙️ Settings", b"settings")],
            [Button.inline("💎 Plan", b"plan")]
        ]
    )

# ---------- SETTINGS COMMANDS ----------

@bot.on(events.NewMessage(pattern='/settings'))
async def cmd_settings(e):
    uid = e.sender_id
    u = get_user(uid)
    cap_preview = u['caption'] or "Default (original caption)"
    tag_preview = u['rename_tag'] or "Not set"
    rep_map = u['replace_words'] or {}
    rep_preview = ", ".join([f"'{k}'→'{v}'" for k, v in rep_map.items()]) or "None"

    await e.reply(
        "⚙️ **Settings**\n\n"
        f"Current Chat ID: `{u['chat_id'] or 'Not set'}`\n"
        f"Caption template: `{cap_preview}`\n"
        f"Rename tag: `{tag_preview}`\n"
        f"Replace words: {rep_preview}\n\n"
        "**Sub-commands:**\n"
        "`/setchatid` - Set target chat ID\n"
        "`/setcaption` - Set caption template (supports {original} and {tag})\n"
        "`/setrename` - Set rename tag (e.g., @YourChannel)\n"
        "`/setreplace` - Configure word replacements\n"
        "`/resetsettings` - Reset all settings"
    )

@bot.on(events.NewMessage(pattern='/setchatid'))
async def cmd_setchatid(e):
    uid = e.sender_id
    u = get_user(uid)
    u['state'] = 'wait_chatid'
    await e.reply(
        "📌 **Set Chat ID**\n\n"
        "Send the target chat ID:\n"
        "Example: `-1001234567890`"
    )

@bot.on(events.NewMessage(pattern='/setcaption'))
async def cmd_setcaption(e):
    uid = e.sender_id
    u = get_user(uid)
    u['state'] = 'wait_caption'
    await e.reply(
        "📝 **Set Caption Template**\n\n"
        "You can use:\n"
        "`{original}` = original caption\n"
        "`{tag}` = your rename tag\n\n"
        "Example:\n"
        "`{original}\\n\\nUploaded by {tag}`"
    )

@bot.on(events.NewMessage(pattern='/setrename'))
async def cmd_setrename(e):
    uid = e.sender_id
    u = get_user(uid)
    u['state'] = 'wait_rename'
    await e.reply(
        "🏷 **Set Rename Tag**\n\n"
        "Example:\n"
        "`@YourChannel` or `Uploaded by @YourChannel`"
    )

@bot.on(events.NewMessage(pattern='/setreplace'))
async def cmd_setreplace(e):
    uid = e.sender_id
    u = get_user(uid)
    u['state'] = 'wait_replace'
    await e.reply(
        "🔤 **Set Replace Words**\n\n"
        "Format (use `=>` and `||`):\n"
        "`old1 => new1 || old2 => new2`\n\n"
        "Example:\n"
        "`t.me/oldchannel => t.me/newchannel || OldName => NewName`"
    )

@bot.on(events.NewMessage(pattern='/resetsettings'))
async def cmd_resetsettings(e):
    uid = e.sender_id
    u = get_user(uid)
    u['caption'] = None
    u['rename_tag'] = None
    u['replace_words'] = {}
    u['thumb_id'] = None
    u['state'] = None
    await e.reply("♻️ **All settings reset to default!**")

# ---------- LOGIN / BATCH / PLAN / ADMIN ----------

@bot.on(events.NewMessage(pattern='/login'))
async def cmd_login(e):
    u = get_user(e.sender_id)
    u['state'] = 'wait_phone'
    await e.reply(
        "🔐 **Login**\n\n"
        "Step 1: Send phone number (with country code)\n"
        "Example: `+919876543210`"
    )

@bot.on(events.NewMessage(pattern='/logout'))
async def cmd_logout(e):
    uid = e.sender_id
    if uid in sessions:
        del sessions[uid]
        await e.reply("✅ Logged out!")
    else:
        await e.reply("⚠️ Not logged in.")

@bot.on(events.NewMessage(pattern='/batch'))
async def cmd_batch(e):
    uid = e.sender_id
    u = get_user(uid)

    if not u['chat_id']:
        await e.reply("⚠️ First set target chat ID: `/setchatid` or `/settings`")
        return

    u['state'] = 'wait_link'
    await e.reply("🔗 **Send post link:**\n\nExample: `https://t.me/channel/123`")

@bot.on(events.NewMessage(pattern='/cancel'))
async def cmd_cancel(e):
    uid = e.sender_id
    removed = 0

    # Remove tasks of this user from queue
    new_q = deque()
    for task in queue:
        if task[1] != uid:
            new_q.append(task)
        else:
            removed += 1

    queue.clear()
    queue.extend(new_q)

    await e.reply(f"✅ Cancelled! Removed {removed} queued tasks.")

@bot.on(events.NewMessage(pattern='/myplan'))
async def cmd_myplan(e):
    uid = e.sender_id
    is_prem = uid in premium

    if is_prem:
        await e.reply("💎 **Premium Active**\n\nBatch: 1000 files\nSpeed: Fast\nAll features unlocked.")
    else:
        await e.reply("⚪ **Free Plan**\n\nBatch: 3 files\nSpeed: Standard.")

@bot.on(events.NewMessage(pattern=r'^/add (\d+)$'))
async def cmd_add(e):
    if e.sender_id != OWNER_ID:
        return
    uid = int(e.pattern_match.group(1))
    premium.add(uid)
    await e.reply(f"✅ Added `{uid}` to premium (1000 batch).")

@bot.on(events.NewMessage(pattern=r'^/rem (\d+)$'))
async def cmd_rem(e):
    if e.sender_id != OWNER_ID:
        return
    uid = int(e.pattern_match.group(1))
    if uid in premium and uid != OWNER_ID:
        premium.discard(uid)
        await e.reply(f"✅ Removed `{uid}` from premium.")

@bot.on(events.NewMessage(pattern='/stats'))
async def cmd_stats(e):
    if e.sender_id != OWNER_ID:
        return

    await e.reply(
        "📊 **Stats**\n\n"
        f"Users: {len(users)}\n"
        f"Premium: {len(premium)}\n"
        f"Queue: {len(queue)}\n"
        f"Active: {len(active)}\n"
        f"Sessions: {len(sessions)}"
    )

# ============= MESSAGE HANDLER (STATE MACHINE) =============

@bot.on(events.NewMessage)
async def msg_handler(e):
    uid = e.sender_id
    txt = e.text.strip() if e.text else ""
    u = get_user(uid)
    state = u.get('state')

    if not state:
        return

    # ---------- LOGIN FLOW ----------
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
            await e.reply("📱 **OTP sent!**\n\nStep 2: Send OTP like: `1 2 3 4 5`")
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
            await e.reply("❌ Invalid OTP! Try `/login` again.")
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

    # ---------- SETTINGS FLOW ----------
    elif state == 'wait_chatid':
        try:
            cid = int(txt)
            u['chat_id'] = cid
            u['state'] = None
            await e.reply(
                f"✅ **Chat ID set!**\n\n"
                f"Target: `{cid}`\n\n"
                "Now use `/batch` to start extraction."
            )
        except Exception:
            await e.reply("❌ Invalid! Send number like: `-1001234567890`")

    elif state == 'wait_caption':
        # Save caption template
        u['caption'] = txt
        u['state'] = None
        await e.reply(
            "✅ **Caption template saved!**\n\n"
            "Remember you can use `{original}` and `{tag}` placeholders."
        )

    elif state == 'wait_rename':
        u['rename_tag'] = txt
        u['state'] = None
        await e.reply(f"✅ **Rename tag set to:** `{txt}`")

    elif state == 'wait_replace':
        # Parse "old => new || old2 => new2"
        mapping = {}
        parts = [p.strip() for p in txt.split("||") if p.strip()]
        for part in parts:
            if "=>" in part:
                old, new = part.split("=>", 1)
                old = old.strip()
                new = new.strip()
                if old:
                    mapping[old] = new

        u['replace_words'] = mapping
        u['state'] = None

        preview = ", ".join([f"'{k}'→'{v}'" for k, v in mapping.items()]) or "None"
        await e.reply(f"✅ **Replace words updated:** {preview}")

    # ---------- BATCH FLOW ----------
    elif state == 'wait_link':
        parsed = parse_link(txt)
        if not parsed:
            await e.reply("❌ Invalid link!\nSend Telegram post link like: `https://t.me/c/...` or `https://t.me/channel/123`")
            return

        u['batch_chat'], u['batch_start'] = parsed
        u['state'] = 'wait_count'

        max_lim = 1000 if uid in premium else 3
        await e.reply(f"**How many files?**\n\nMax: `{max_lim}`")

    elif state == 'wait_count':
        try:
            count = int(txt)
            max_lim = 1000 if uid in premium else 3

            if count > max_lim:
                await e.reply(f"❌ Max `{max_lim}`! Check your plan: `/myplan`")
                return

            u['state'] = None
            target = u['chat_id']
            chat = u['batch_chat']
            start_id = u['batch_start']

            status = await e.reply(
                f"**Batch started ⚡**\n\n"
                f"Processing: 0/{count}\n\n"
                f"**Powered by RATNA**"
            )

            # Add tasks to queue
            for i in range(count):
                tid = str(uuid.uuid4())
                mid = start_id + i
                queue.append((tid, uid, chat, mid, target, status))

            logger.info(f"Added {count} tasks for user {uid}")

            # Monitor progress
            completed = 0
            last_completed = -1

            while completed < count:
                await asyncio.sleep(5)

                in_queue = sum(1 for t in queue if t[1] == uid)
                in_active = sum(1 for t_id, data in active.items() if data.get('uid') == uid)

                completed = count - in_queue - in_active

                # Progress of current active download
                progress_text = ""
                for t_id, data in active.items():
                    if data.get('uid') == uid:
                        cur = data.get('cur', 0)
                        tot = data.get('tot', 1)
                        spd = data.get('spd', 0)
                        progress_text = f"\n\n{progress_bar(cur, tot, spd)}"
                        break

                if completed != last_completed or progress_text:
                    try:
                        msg_text = (
                            f"**Batch started ⚡**\n\n"
                            f"Completed: {completed}/{count}\n"
                            f"Queue: {in_queue}\n"
                            f"Active: {in_active}"
                            f"{progress_text}\n\n"
                            f"**Powered by RATNA**"
                        )
                        await status.edit(msg_text)
                        last_completed = completed
                    except Exception as ex:
                        logger.error(f"Status update failed: {ex}")

                # Safety: if nothing left in queue/active but completed < count, break
                if in_queue == 0 and in_active == 0:
                    break

            # Final summary
            try:
                await status.edit(
                    "✅ **Batch completed!**\n\n"
                    f"Total requested: {count}\n"
                    f"Approx successful: {completed}\n"
                    f"Approx failed: {count - completed}\n\n"
                    f"Check your channel: `{target}`\n\n"
                    "**Powered by RATNA**"
                )
            except Exception:
                pass

        except ValueError:
            await e.reply("❌ Invalid number! Send a valid integer.")

# ============= MAIN =============

def main():
    logger.info("=" * 50)
    logger.info("🚀 RATNA STYLE BOT STARTED")
    logger.info("=" * 50)
    logger.info(f"Owner: {OWNER_ID}")
    logger.info(f"Premium users: {len(premium)}")
    logger.info("✅ All systems ready")
    logger.info("=" * 50)

    # Start worker
    bot.loop.create_task(worker())

    # Run bot
    bot.run_until_disconnected()

if __name__ == '__main__':
    main()
