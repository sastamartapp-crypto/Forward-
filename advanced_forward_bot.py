"""
Advanced Forward Bot — PERSONAL USE ONLY
==========================================
Architecture:
  - Telethon userbot client = YOUR OWN Telegram account (you log in once with
    your own phone + your own OTP). This client does the actual forwarding.
  - python-telegram-bot (PTB) = a control-panel bot that YOU talk to, to
    configure sources/destinations/filters/etc.

SECURITY: Only the Telegram user ID set in OWNER_ID (.env) can use this bot.
Every other user is rejected immediately. Do not share your bot token or
OWNER_ID setup with anyone — this gives full control of your Telegram account.

Setup:
  1. pip install -r requirements.txt
  2. Get API_ID / API_HASH from https://my.telegram.org
  3. Create a control bot via @BotFather, get BOT_TOKEN
  4. Get your own numeric Telegram user ID (e.g. via @userinfobot)
  5. Fill these into a .env file (see .env.example)
  6. python advanced_forward_bot.py
  7. In Telegram, open your control bot, send /start, follow the
     phone -> OTP -> (2FA password if enabled) login flow. This logs in
     YOUR OWN account inside the userbot client (session saved locally in
     forward_bot.db so you don't have to log in again on restart).
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityUrl, MessageEntityTextUrl

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.environ.get("DB_PATH", "forward_bot.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("forward_bot")

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            session_string TEXT
        );
        CREATE TABLE IF NOT EXISTS channels (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER UNIQUE NOT NULL,
            source_title TEXT,
            enabled INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS destinations (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_row_id INTEGER NOT NULL,
            dest_id INTEGER NOT NULL,
            dest_title TEXT,
            FOREIGN KEY (channel_row_id) REFERENCES channels(row_id)
        );
        CREATE TABLE IF NOT EXISTS config (
            channel_row_id INTEGER PRIMARY KEY,
            config_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stats (
            channel_row_id INTEGER PRIMARY KEY,
            total_forwarded INTEGER DEFAULT 0,
            total_skipped INTEGER DEFAULT 0,
            last_forwarded_at INTEGER
        );
        """
    )
    conn.commit()
    conn.close()


DEFAULT_CONFIG = {
    "media": {
        "photo": 1, "video": 1, "voice": 1, "audio": 1,
        "document": 1, "sticker": 1, "gif": 1, "video_message": 1,
    },
    "link_block": {"block_all": 0, "block_www": 0, "block_tme": 0, "whitelist": [], "mode": "skip"},
    "control": {
        "copy": 1, "silent": 0, "pin": 0, "no_caption": 0,
        "dup_check": 1, "media_only": 0, "text_only": 0,
        "delay": 0, "header": "", "footer": "", "auto_delete": 0,
        "min_length": 0,
    },
    "filters": {"block_keywords": []},
    "replace": {},
}


def _deep_merge_defaults(cfg: dict) -> dict:
    for section, defaults in DEFAULT_CONFIG.items():
        if section not in cfg:
            cfg[section] = json.loads(json.dumps(defaults))
        elif isinstance(defaults, dict):
            for k, v in defaults.items():
                cfg[section].setdefault(k, v)
    return cfg


def get_config(channel_row_id: int) -> dict:
    conn = db()
    row = conn.execute(
        "SELECT config_json FROM config WHERE channel_row_id=?", (channel_row_id,)
    ).fetchone()
    conn.close()
    if not row:
        save_config(channel_row_id, DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return _deep_merge_defaults(json.loads(row[0]))


def save_config(channel_row_id: int, cfg: dict):
    conn = db()
    conn.execute(
        "INSERT INTO config (channel_row_id, config_json) VALUES (?,?) "
        "ON CONFLICT(channel_row_id) DO UPDATE SET config_json=excluded.config_json",
        (channel_row_id, json.dumps(cfg)),
    )
    conn.commit()
    conn.close()


def add_channel(source_id: int, title: str) -> int:
    conn = db()
    conn.execute(
        "INSERT INTO channels (source_id, source_title) VALUES (?,?) "
        "ON CONFLICT(source_id) DO UPDATE SET source_title=excluded.source_title",
        (source_id, title),
    )
    conn.commit()
    row_id = conn.execute(
        "SELECT row_id FROM channels WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    conn.close()
    get_config(row_id)
    return row_id


def list_channels():
    conn = db()
    rows = conn.execute("SELECT row_id, source_id, source_title, enabled FROM channels").fetchall()
    conn.close()
    return rows


def get_channel(row_id: int):
    conn = db()
    row = conn.execute(
        "SELECT row_id, source_id, source_title, enabled FROM channels WHERE row_id=?", (row_id,)
    ).fetchone()
    conn.close()
    return row


def delete_channel(row_id: int):
    conn = db()
    conn.execute("DELETE FROM destinations WHERE channel_row_id=?", (row_id,))
    conn.execute("DELETE FROM config WHERE channel_row_id=?", (row_id,))
    conn.execute("DELETE FROM channels WHERE row_id=?", (row_id,))
    conn.commit()
    conn.close()


def set_channel_enabled(row_id: int, enabled: bool):
    conn = db()
    conn.execute("UPDATE channels SET enabled=? WHERE row_id=?", (1 if enabled else 0, row_id))
    conn.commit()
    conn.close()


def rename_channel(row_id: int, title: str):
    conn = db()
    conn.execute("UPDATE channels SET source_title=? WHERE row_id=?", (title, row_id))
    conn.commit()
    conn.close()


def add_destination(channel_row_id: int, dest_id: int, title: str):
    conn = db()
    conn.execute(
        "INSERT INTO destinations (channel_row_id, dest_id, dest_title) VALUES (?,?,?)",
        (channel_row_id, dest_id, title),
    )
    conn.commit()
    conn.close()


def list_destinations(channel_row_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT row_id, dest_id, dest_title FROM destinations WHERE channel_row_id=?",
        (channel_row_id,),
    ).fetchall()
    conn.close()
    return rows


def delete_destination(dest_row_id: int):
    conn = db()
    conn.execute("DELETE FROM destinations WHERE row_id=?", (dest_row_id,))
    conn.commit()
    conn.close()


def get_session_string() -> Optional[str]:
    conn = db()
    row = conn.execute("SELECT session_string FROM session WHERE id=1").fetchone()
    conn.close()
    return row[0] if row else None


def save_session_string(s: str):
    conn = db()
    conn.execute(
        "INSERT INTO session (id, session_string) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET session_string=excluded.session_string",
        (s,),
    )
    conn.commit()
    conn.close()


init_db()

# ----------------------------------------------------------------------------
# Userbot (Telethon) runtime
# ----------------------------------------------------------------------------

userbot: Optional[TelegramClient] = None
login_clients: dict[int, TelegramClient] = {}  # transient clients during login flow
recent_hashes: dict[int, list] = {}  # channel_row_id -> recent text hashes for dup_check

MEDIA_KEYS_ORDER = [
    ("photo", "🖼 Photo"),
    ("video", "🎬 Video"),
    ("video_message", "⭕ Video Message"),
    ("voice", "🎤 Voice Message"),
    ("audio", "🎵 Audio / Music"),
    ("document", "📄 Document / File"),
    ("sticker", "🌟 Sticker"),
    ("gif", "🎞 GIF / Animation"),
]


def extract_links(message) -> list:
    links = []
    text = message.message or ""
    if message.entities:
        for ent in message.entities:
            if isinstance(ent, MessageEntityUrl):
                links.append(text[ent.offset: ent.offset + ent.length])
            elif isinstance(ent, MessageEntityTextUrl):
                links.append(ent.url)
    # also catch plain www./http links not tagged as entities
    for m in re.findall(r"(https?://\S+|www\.\S+|t\.me/\S+)", text):
        links.append(m)
    return links


def message_media_kind(message) -> Optional[str]:
    if message.photo:
        return "photo"
    if message.video_note:
        return "video_message"
    if message.voice:
        return "voice"
    if message.audio:
        return "audio"
    if message.sticker:
        return "sticker"
    if message.gif:
        return "gif"
    if message.video:
        return "video"
    if message.document:
        return "document"
    return None


def should_skip(message, cfg: dict, channel_row_id: int) -> Optional[str]:
    """Returns a reason string if message should be skipped, else None."""
    media_kind = message_media_kind(message)
    has_media = media_kind is not None
    text = message.message or ""

    ctrl = cfg["control"]
    if ctrl.get("media_only") and not has_media:
        return "media_only"
    if ctrl.get("text_only") and has_media:
        return "text_only"

    if media_kind and not cfg["media"].get(media_kind, 1):
        return f"media_blocked:{media_kind}"

    lb = cfg["link_block"]
    links = extract_links(message)
    if links:
        whitelisted = lambda l: any(w in l for w in lb.get("whitelist", []))
        if lb.get("block_all") and not all(whitelisted(l) for l in links):
            return "link_block_all"
        if lb.get("block_www") and any("www." in l and not whitelisted(l) for l in links):
            return "link_block_www"
        if lb.get("block_tme") and any("t.me" in l and not whitelisted(l) for l in links):
            return "link_block_tme"

    for kw in cfg["filters"].get("block_keywords", []):
        if kw.lower() in text.lower():
            return f"keyword_blocked:{kw}"

    if ctrl.get("dup_check") and text:
        h = hash(text.strip().lower())
        hist = recent_hashes.setdefault(channel_row_id, [])
        if h in hist:
            return "duplicate"
        hist.append(h)
        if len(hist) > 200:
            del hist[: len(hist) - 200]

    return None


def apply_text_transform(text: str, cfg: dict) -> str:
    if not text:
        return text
    for old, new in cfg.get("replace", {}).items():
        text = text.replace(old, new)
    ctrl = cfg["control"]
    header = ctrl.get("header") or ""
    footer = ctrl.get("footer") or ""
    parts = []
    if header:
        parts.append(header)
    parts.append(text)
    if footer:
        parts.append(footer)
    return "\n".join(parts)


async def deliver(message, channel_row_id: int):
    cfg = get_config(channel_row_id)
    skip_reason = should_skip(message, cfg, channel_row_id)
    if skip_reason:
        log.info("Skipping message %s: %s", message.id, skip_reason)
        return

    ctrl = cfg["control"]
    delay = ctrl.get("delay", 0)
    if delay:
        await asyncio.sleep(delay)

    dests = list_destinations(channel_row_id)
    if not dests:
        return

    caption = message.message or ""
    if not ctrl.get("no_caption"):
        caption = apply_text_transform(caption, cfg)
    else:
        caption = ""

    for _, dest_id, _ in dests:
        try:
            sent = None
            if ctrl.get("copy", 1):
                # re-upload / copy: strips "Forwarded from" tag
                if message.media:
                    sent = await userbot.send_file(
                        dest_id, message.media, caption=caption, silent=bool(ctrl.get("silent")),
                    )
                else:
                    sent = await userbot.send_message(
                        dest_id, caption, silent=bool(ctrl.get("silent")),
                    )
            else:
                sent = await userbot.forward_messages(dest_id, message)
            if ctrl.get("pin") and sent:
                target = sent[0] if isinstance(sent, list) else sent
                await userbot.pin_message(dest_id, target, notify=False)
        except Exception as e:
            log.error("Failed delivering to %s: %s", dest_id, e)


def register_live_handler():
    @userbot.on(events.NewMessage())
    async def handler(event):
        for row_id, source_id, title, enabled in list_channels():
            if event.chat_id == source_id and enabled:
                await deliver(event.message, row_id)


async def start_userbot_from_session():
    global userbot
    session_str = get_session_string()
    if not session_str:
        return False
    userbot = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await userbot.start()
    register_live_handler()
    log.info("Userbot started from saved session.")
    return True


async def old_messages_forward(channel_row_id: int, count: Optional[int], status_cb=None):
    row = get_channel(channel_row_id)
    if not row:
        return
    _, source_id, _, _ = row
    sent_count = 0
    kwargs = {} if count is None else {"limit": count}
    async for message in userbot.iter_messages(source_id, reverse=True, **kwargs):
        await deliver(message, channel_row_id)
        sent_count += 1
        if status_cb and sent_count % 25 == 0:
            await status_cb(sent_count)
    if status_cb:
        await status_cb(sent_count, done=True)


# ----------------------------------------------------------------------------
# PTB control bot — UI state & helpers
# ----------------------------------------------------------------------------

user_state: dict[int, dict] = {}  # transient input state, keyed by telegram user id


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != OWNER_ID:
            return
        return await func(update, context)
    return wrapper


def main_menu_kb():
    rows = []
    for row_id, source_id, title, enabled in list_channels():
        dot = "🟢" if enabled else "⚪"
        rows.append([InlineKeyboardButton(f"{dot} {title}", callback_data=f"chan:{row_id}")])
    rows.append([InlineKeyboardButton("➕ Add Channel", callback_data="add_channel")])
    rows.append([InlineKeyboardButton("🚪 Logout", callback_data="logout")])
    return InlineKeyboardMarkup(rows)


def channel_menu_kb(row_id: int):
    _, source_id, title, enabled = get_channel(row_id)
    dests = list_destinations(row_id)
    toggle = "🔴 Turn OFF" if enabled else "🟢 Turn ON"
    rows = [
        [InlineKeyboardButton(f"📤 {len(dests)} Dest", callback_data=f"dest:{row_id}")],
        [InlineKeyboardButton(toggle, callback_data=f"toggle:{row_id}"),
         InlineKeyboardButton("✏️ Rename", callback_data=f"rename:{row_id}")],
        [InlineKeyboardButton("⚙️ Control", callback_data=f"control:{row_id}"),
         InlineKeyboardButton("🎬 Media", callback_data=f"media:{row_id}")],
        [InlineKeyboardButton("🔗 Link Block", callback_data=f"links:{row_id}"),
         InlineKeyboardButton("✂️ Filter", callback_data=f"filter:{row_id}")],
        [InlineKeyboardButton("🔄 Replace", callback_data=f"replace:{row_id}")],
        [InlineKeyboardButton("📨 Old Messages Forward", callback_data=f"oldfwd:{row_id}")],
        [InlineKeyboardButton("🗑 Delete Channel", callback_data=f"delchan:{row_id}")],
        [InlineKeyboardButton("‹ Back", callback_data="home")],
    ]
    return InlineKeyboardMarkup(rows)


def control_menu_kb(row_id: int):
    cfg = get_config(row_id)
    c = cfg["control"]

    def dot(key):
        return "🟢" if c.get(key) else "🔴"

    rows = [
        [InlineKeyboardButton(f"{dot('copy')} Copy", callback_data=f"ctrl:{row_id}:copy"),
         InlineKeyboardButton(f"{dot('silent')} Silent", callback_data=f"ctrl:{row_id}:silent"),
         InlineKeyboardButton(f"{dot('pin')} Pin", callback_data=f"ctrl:{row_id}:pin")],
        [InlineKeyboardButton(f"{dot('no_caption')} No Caption", callback_data=f"ctrl:{row_id}:no_caption"),
         InlineKeyboardButton(f"{dot('dup_check')} Dup Check", callback_data=f"ctrl:{row_id}:dup_check")],
        [InlineKeyboardButton(f"{dot('media_only')} Media Only", callback_data=f"ctrl:{row_id}:media_only"),
         InlineKeyboardButton(f"{dot('text_only')} Text Only", callback_data=f"ctrl:{row_id}:text_only")],
        [InlineKeyboardButton(f"⏱ Delay: {c.get('delay',0)}s", callback_data=f"setdelay:{row_id}")],
        [InlineKeyboardButton(f"📝 Header: {c.get('header') or '—'}", callback_data=f"setheader:{row_id}")],
        [InlineKeyboardButton(f"📝 Footer: {c.get('footer') or '—'}", callback_data=f"setfooter:{row_id}")],
        [InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def media_menu_kb(row_id: int):
    cfg = get_config(row_id)
    m = cfg["media"]
    rows = []
    for key, label in MEDIA_KEYS_ORDER:
        dot = "🟢" if m.get(key, 1) else "🔴"
        rows.append([InlineKeyboardButton(f"{dot} {label}", callback_data=f"media_t:{row_id}:{key}")])
    rows.append([InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")])
    return InlineKeyboardMarkup(rows)


def links_menu_kb(row_id: int):
    cfg = get_config(row_id)
    lb = cfg["link_block"]
    rows = [
        [InlineKeyboardButton(f"{'🟢' if lb.get('block_all') else '⚪'} Block ALL Links",
                               callback_data=f"link_t:{row_id}:block_all")],
        [InlineKeyboardButton(f"{'🟢' if lb.get('block_www') else '⚪'} Block www.",
                               callback_data=f"link_t:{row_id}:block_www")],
        [InlineKeyboardButton(f"{'🟢' if lb.get('block_tme') else '⚪'} Block t.me",
                               callback_data=f"link_t:{row_id}:block_tme")],
        [InlineKeyboardButton(f"✅ Whitelist Links ({len(lb.get('whitelist',[]))})",
                               callback_data=f"whitelist:{row_id}")],
        [InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def oldfwd_menu_kb(row_id: int):
    rows = [
        [InlineKeyboardButton("100", callback_data=f"old:{row_id}:100"),
         InlineKeyboardButton("200", callback_data=f"old:{row_id}:200")],
        [InlineKeyboardButton("500", callback_data=f"old:{row_id}:500"),
         InlineKeyboardButton("1000", callback_data=f"old:{row_id}:1000")],
        [InlineKeyboardButton("🔢 Custom Amount", callback_data=f"old_custom:{row_id}")],
        [InlineKeyboardButton("📤 All Messages", callback_data=f"old:{row_id}:all")],
        [InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")],
    ]
    return InlineKeyboardMarkup(rows)


# ----------------------------------------------------------------------------
# PTB handlers — login flow
# ----------------------------------------------------------------------------

@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if userbot is None:
        user_state[OWNER_ID] = {"action": "awaiting_phone"}
        await update.message.reply_text(
            "📱 *Phone Number Login*\n\n"
            "Enter your phone number with country code:\n"
            "Example: `+919876543210`\n\n"
            "/cancel to abort",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚡ Advanced Forward Bot\nChoose a channel or add a new one:",
                                          reply_markup=main_menu_kb())


@owner_only
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_state.pop(OWNER_ID, None)
    cli = login_clients.pop(OWNER_ID, None)
    if cli:
        await cli.disconnect()
    await update.message.reply_text("Cancelled.")


@owner_only
async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if userbot is None:
        await update.message.reply_text("Pehle /start se login karo.")
        return
    await update.message.reply_text("⚡ Advanced Forward Bot", reply_markup=main_menu_kb())


async def handle_login_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict):
    global userbot
    text = update.message.text.strip()
    action = state["action"]

    if action == "awaiting_phone":
        phone = text
        cli = TelegramClient(StringSession(), API_ID, API_HASH)
        await cli.connect()
        try:
            await cli.send_code_request(phone)
        except Exception as e:
            await update.message.reply_text(f"Error sending code: {e}")
            await cli.disconnect()
            return
        login_clients[OWNER_ID] = cli
        user_state[OWNER_ID] = {"action": "awaiting_code", "phone": phone}
        await update.message.reply_text(
            "✅ *OTP Sent!*\n\n"
            "➡️ Check your Telegram app\n\n"
            "⚠️ *OTP seedha mat likhein!*\n\n"
            "Inmein se koi bhi format use karo:\n"
            "`1-2-3-4-5-6`\n`1.2.3.4.5.6`\n`1a2a3a4a5a6`\n\n"
            "/cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if action == "awaiting_code":
        code = re.sub(r"[^0-9]", "", text)
        cli = login_clients.get(OWNER_ID)
        phone = state["phone"]
        try:
            await cli.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            user_state[OWNER_ID] = {"action": "awaiting_password", "phone": phone}
            await update.message.reply_text("🔒 2FA enabled. Apna cloud password bhejo:\n/cancel")
            return
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            await update.message.reply_text(f"❌ Invalid/expired code: {e}. /start se phir try karo.")
            user_state.pop(OWNER_ID, None)
            return
        await finish_login(update, cli)
        return

    if action == "awaiting_password":
        cli = login_clients.get(OWNER_ID)
        try:
            await cli.sign_in(password=text)
        except Exception as e:
            await update.message.reply_text(f"❌ Wrong password: {e}")
            return
        await finish_login(update, cli)
        return


async def finish_login(update: Update, cli: TelegramClient):
    global userbot
    session_str = cli.session.save()
    save_session_string(session_str)
    userbot = cli
    register_live_handler()
    user_state.pop(OWNER_ID, None)
    login_clients.pop(OWNER_ID, None)
    me = await userbot.get_me()
    await update.message.reply_text(
        f"✅ *Login Successful!*\n👤 {me.first_name} (@{me.username})\n\n"
        f"Ab /menu se channels add karo.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ----------------------------------------------------------------------------
# PTB handlers — text input router (non-login states)
# ----------------------------------------------------------------------------

@owner_only
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = user_state.get(OWNER_ID)
    if not state:
        return
    action = state["action"]

    if action in ("awaiting_phone", "awaiting_code", "awaiting_password"):
        await handle_login_text(update, context, state)
        return

    if action == "awaiting_source_input":
        await resolve_and_add_source(update)
        return

    if action == "awaiting_dest_input":
        await resolve_and_add_dest(update, state["row_id"])
        return

    if action == "awaiting_rename":
        rename_channel(state["row_id"], update.message.text.strip())
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text("✅ Renamed.", reply_markup=channel_menu_kb(state["row_id"]))
        return

    if action == "awaiting_old_custom":
        try:
            n = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Number bhejo, e.g. 350")
            return
        user_state.pop(OWNER_ID, None)
        await run_old_forward(update, state["row_id"], n)
        return

    if action == "awaiting_delay":
        try:
            n = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("Seconds mein number bhejo, e.g. 5")
            return
        cfg = get_config(state["row_id"])
        cfg["control"]["delay"] = n
        save_config(state["row_id"], cfg)
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text("✅ Delay set.", reply_markup=control_menu_kb(state["row_id"]))
        return

    if action in ("awaiting_header", "awaiting_footer"):
        key = "header" if action == "awaiting_header" else "footer"
        cfg = get_config(state["row_id"])
        cfg["control"][key] = update.message.text
        save_config(state["row_id"], cfg)
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text("✅ Saved.", reply_markup=control_menu_kb(state["row_id"]))
        return

    if action == "awaiting_filter_kw":
        cfg = get_config(state["row_id"])
        cfg["filters"]["block_keywords"].append(update.message.text.strip())
        save_config(state["row_id"], cfg)
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text(
            f"✅ Keyword block added. Total: {len(cfg['filters']['block_keywords'])}"
        )
        return

    if action == "awaiting_replace_from":
        user_state[OWNER_ID] = {"action": "awaiting_replace_to", "row_id": state["row_id"],
                                 "from": update.message.text}
        await update.message.reply_text("Ab replacement text bhejo (jo isse replace hoga):")
        return

    if action == "awaiting_replace_to":
        cfg = get_config(state["row_id"])
        cfg["replace"][state["from"]] = update.message.text
        save_config(state["row_id"], cfg)
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text("✅ Replace rule added.")
        return

    if action == "awaiting_whitelist":
        cfg = get_config(state["row_id"])
        cfg["link_block"]["whitelist"].append(update.message.text.strip())
        save_config(state["row_id"], cfg)
        user_state.pop(OWNER_ID, None)
        await update.message.reply_text("✅ Added to whitelist.")
        return


async def resolve_entity_from_message(update: Update):
    """Accepts @username, numeric id, t.me link, or a forwarded message."""
    msg = update.message
    if msg.forward_from_chat:
        chat = msg.forward_from_chat
        entity = await userbot.get_entity(chat.id)
        return entity
    text = msg.text.strip()
    try:
        entity = await userbot.get_entity(text)
        return entity
    except Exception:
        return None


async def resolve_and_add_source(update: Update):
    entity = await resolve_entity_from_message(update)
    if not entity:
        await update.message.reply_text(
            "❌ Channel nahi mila. @username bhejo ya us channel se ek message forward karo."
        )
        return
    title = getattr(entity, "title", None) or getattr(entity, "first_name", str(entity.id))
    row_id = add_channel(entity.id, title)
    user_state.pop(OWNER_ID, None)
    await update.message.reply_text(
        f"✅ Source added: {title}\n\nAb destination add karo:",
        reply_markup=channel_menu_kb(row_id),
    )


async def resolve_and_add_dest(update: Update, row_id: int):
    entity = await resolve_entity_from_message(update)
    if not entity:
        await update.message.reply_text(
            "❌ Channel nahi mila. @username bhejo ya us channel se ek message forward karo."
        )
        return
    title = getattr(entity, "title", None) or getattr(entity, "first_name", str(entity.id))
    add_destination(row_id, entity.id, title)
    user_state.pop(OWNER_ID, None)
    await update.message.reply_text(
        f"✅ Destination added: {title}\n🎉 Forwarding active!",
        reply_markup=channel_menu_kb(row_id),
    )


# ----------------------------------------------------------------------------
# PTB handlers — callback router
# ----------------------------------------------------------------------------

@owner_only
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global userbot
    query = update.callback_query
    await query.answer()
    data = query.data

    if userbot is None and data != "home":
        await query.edit_message_text("Pehle /start se login karo.")
        return

    if data == "home":
        await query.edit_message_text("⚡ Advanced Forward Bot", reply_markup=main_menu_kb())
        return

    if data == "add_channel":
        user_state[OWNER_ID] = {"action": "awaiting_source_input"}
        await query.edit_message_text(
            "📡 Source channel add karo:\n\n"
            "Us channel se koi message yaha forward karo, *ya* uska @username bhejo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "logout":
        if userbot:
            await userbot.log_out()
            userbot = None
        save_session_string("")
        await query.edit_message_text("🚪 Logged out. /start se phir login karo.")
        return

    if data.startswith("chan:"):
        row_id = int(data.split(":")[1])
        _, _, title, enabled = get_channel(row_id)
        await query.edit_message_text(
            f"📡 {title}\nStatus: {'🟢 ON' if enabled else '⚪ OFF'}",
            reply_markup=channel_menu_kb(row_id),
        )
        return

    if data.startswith("toggle:"):
        row_id = int(data.split(":")[1])
        _, _, _, enabled = get_channel(row_id)
        set_channel_enabled(row_id, not enabled)
        await query.edit_message_text("Updated.", reply_markup=channel_menu_kb(row_id))
        return

    if data.startswith("rename:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_rename", "row_id": row_id}
        await query.edit_message_text("✏️ Naya naam bhejo:")
        return

    if data.startswith("delchan:"):
        row_id = int(data.split(":")[1])
        delete_channel(row_id)
        await query.edit_message_text("🗑 Deleted.", reply_markup=main_menu_kb())
        return

    if data.startswith("dest:"):
        row_id = int(data.split(":")[1])
        dests = list_destinations(row_id)
        rows = [[InlineKeyboardButton(f"❌ {t}", callback_data=f"deldest:{row_id}:{d}")] for d, _, t in
                [(r[0], r[1], r[2]) for r in dests]]
        rows.append([InlineKeyboardButton("➕ Add Destination", callback_data=f"adddest:{row_id}")])
        rows.append([InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")])
        await query.edit_message_text(
            f"📤 Destinations ({len(dests)}):", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if data.startswith("adddest:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_dest_input", "row_id": row_id}
        await query.edit_message_text(
            "📤 Destination add karo:\n\nUs channel se message forward karo, ya @username bhejo."
        )
        return

    if data.startswith("deldest:"):
        _, row_id, dest_row = data.split(":")
        delete_destination(int(dest_row))
        await query.edit_message_text("✅ Removed.", reply_markup=channel_menu_kb(int(row_id)))
        return

    if data.startswith("control:"):
        row_id = int(data.split(":")[1])
        await query.edit_message_text("⚙️ Control settings:", reply_markup=control_menu_kb(row_id))
        return

    if data.startswith("ctrl:"):
        _, row_id, key = data.split(":")
        row_id = int(row_id)
        cfg = get_config(row_id)
        cfg["control"][key] = 0 if cfg["control"].get(key) else 1
        save_config(row_id, cfg)
        await query.edit_message_text("⚙️ Control settings:", reply_markup=control_menu_kb(row_id))
        return

    if data.startswith("setdelay:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_delay", "row_id": row_id}
        await query.edit_message_text("⏱ Delay in seconds bhejo (e.g. 5):")
        return

    if data.startswith("setheader:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_header", "row_id": row_id}
        await query.edit_message_text("📝 Header text bhejo (ya '-' clear karne ke liye):")
        return

    if data.startswith("setfooter:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_footer", "row_id": row_id}
        await query.edit_message_text("📝 Footer text bhejo (ya '-' clear karne ke liye):")
        return

    if data.startswith("media:"):
        row_id = int(data.split(":")[1])
        await query.edit_message_text("🎬 Media Control:", reply_markup=media_menu_kb(row_id))
        return

    if data.startswith("media_t:"):
        _, row_id, key = data.split(":")
        row_id = int(row_id)
        cfg = get_config(row_id)
        cfg["media"][key] = 0 if cfg["media"].get(key, 1) else 1
        save_config(row_id, cfg)
        await query.edit_message_text("🎬 Media Control:", reply_markup=media_menu_kb(row_id))
        return

    if data.startswith("links:"):
        row_id = int(data.split(":")[1])
        await query.edit_message_text("🔗 Link Block settings:", reply_markup=links_menu_kb(row_id))
        return

    if data.startswith("link_t:"):
        _, row_id, key = data.split(":")
        row_id = int(row_id)
        cfg = get_config(row_id)
        cfg["link_block"][key] = 0 if cfg["link_block"].get(key) else 1
        save_config(row_id, cfg)
        await query.edit_message_text("🔗 Link Block settings:", reply_markup=links_menu_kb(row_id))
        return

    if data.startswith("whitelist:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_whitelist", "row_id": row_id}
        await query.edit_message_text("✅ Whitelist mein add karne ke liye domain/word bhejo (e.g. youtube.com):")
        return

    if data.startswith("filter:"):
        row_id = int(data.split(":")[1])
        cfg = get_config(row_id)
        kws = cfg["filters"]["block_keywords"]
        rows = [[InlineKeyboardButton(f"❌ {k}", callback_data=f"delkw:{row_id}:{i}")] for i, k in enumerate(kws)]
        rows.append([InlineKeyboardButton("➕ Add Keyword", callback_data=f"addkw:{row_id}")])
        rows.append([InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")])
        await query.edit_message_text(f"✂️ Blocked keywords ({len(kws)}):", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("addkw:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_filter_kw", "row_id": row_id}
        await query.edit_message_text("✂️ Block karne wala keyword/phrase bhejo:")
        return

    if data.startswith("delkw:"):
        _, row_id, idx = data.split(":")
        row_id = int(row_id)
        cfg = get_config(row_id)
        try:
            cfg["filters"]["block_keywords"].pop(int(idx))
            save_config(row_id, cfg)
        except IndexError:
            pass
        await query.edit_message_text("Updated.", reply_markup=channel_menu_kb(row_id))
        return

    if data.startswith("replace:"):
        row_id = int(data.split(":")[1])
        cfg = get_config(row_id)
        rules = cfg["replace"]
        rows = [[InlineKeyboardButton(f"❌ {k} ➜ {v}", callback_data=f"delrep:{row_id}:{k}")]
                for k, v in rules.items()]
        rows.append([InlineKeyboardButton("➕ Add Replace Rule", callback_data=f"addrep:{row_id}")])
        rows.append([InlineKeyboardButton("‹ Back", callback_data=f"chan:{row_id}")])
        await query.edit_message_text(f"🔄 Replace rules ({len(rules)}):", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("addrep:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_replace_from", "row_id": row_id}
        await query.edit_message_text("🔄 Konsa text replace karna hai? (original text bhejo):")
        return

    if data.startswith("delrep:"):
        _, row_id, key = data.split(":", 2)
        row_id = int(row_id)
        cfg = get_config(row_id)
        cfg["replace"].pop(key, None)
        save_config(row_id, cfg)
        await query.edit_message_text("Updated.", reply_markup=channel_menu_kb(row_id))
        return

    if data.startswith("oldfwd:"):
        row_id = int(data.split(":")[1])
        await query.edit_message_text("📨 Kitne messages forward karne hain?", reply_markup=oldfwd_menu_kb(row_id))
        return

    if data.startswith("old_custom:"):
        row_id = int(data.split(":")[1])
        user_state[OWNER_ID] = {"action": "awaiting_old_custom", "row_id": row_id}
        await query.edit_message_text("🔢 Number bhejo (e.g. 350):")
        return

    if data.startswith("old:"):
        _, row_id, count = data.split(":")
        row_id = int(row_id)
        n = None if count == "all" else int(count)
        await query.edit_message_text(f"⏳ Forwarding {count} messages... ye background mein chalega.")
        asyncio.create_task(run_old_forward_cb(update, row_id, n))
        return


async def run_old_forward(update: Update, row_id: int, n: Optional[int]):
    await update.message.reply_text(f"⏳ Forwarding {n or 'all'} messages...")
    await old_messages_forward(row_id, n)
    await update.message.reply_text("✅ Old messages forward complete!")


async def run_old_forward_cb(update: Update, row_id: int, n: Optional[int]):
    chat_id = update.effective_chat.id

    async def status(count, done=False):
        if done:
            await context_bot.bot.send_message(chat_id, f"✅ Done! {count} messages forwarded.")
        else:
            await context_bot.bot.send_message(chat_id, f"⏳ {count} messages forwarded so far...")

    await old_messages_forward(row_id, n, status_cb=status)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

context_bot: Application = None


async def post_init(app: Application):
    await start_userbot_from_session()


def main():
    global context_bot
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    context_bot = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
