import os
import json
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv
from flask import Flask, render_template, Response
from telegram import Bot, Update
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes

load_dotenv()

app = Flask(__name__)

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is missing! Please set it in your .env file.")

CHANNELS_FILE = "channels.json"
DB_NAME = "tracker.db"

# Lock for thread-safe file operations
file_lock = threading.Lock()


# --- CHANNELS JSON HELPERS ---
def load_channels():
    with file_lock:
        if os.path.exists(CHANNELS_FILE):
            try:
                with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading {CHANNELS_FILE}: {e}")
        return []


def save_channels(channels):
    with file_lock:
        with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(channels, f, indent=2)


def add_or_update_channel(chat_id, title, username, added_by_user):
    channels = load_channels()
    # Check if already registered
    existing = next((ch for ch in channels if str(ch["chat_id"]) == str(chat_id)), None)

    if existing:
        existing["name"] = title
        existing["username"] = username if username else existing.get("username", "")
        if added_by_user:
            existing["owner"] = added_by_user
    else:
        channels.append({
            "name": title,
            "chat_id": chat_id,
            "username": username if username else "",
            "owner": added_by_user if added_by_user else ""
        })

    save_channels(channels)


def remove_channel(chat_id):
    channels = load_channels()
    channels = [ch for ch in channels if str(ch["chat_id"]) != str(chat_id)]
    save_channels(channels)


# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            last_seen TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


init_db()


def record_user_activity(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now = datetime.utcnow()
    cursor.execute('''
        INSERT INTO bot_users (user_id, last_seen) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen=?
    ''', (user_id, now, now))
    conn.commit()
    conn.close()


def get_user_analytics():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*) FROM bot_users')
    unique_users = cursor.fetchone()[0]

    since = datetime.utcnow() - timedelta(hours=24)
    cursor.execute('SELECT COUNT(*) FROM bot_users WHERE last_seen >= ?', (since,))
    active_users = cursor.fetchone()[0]

    conn.close()
    return unique_users, active_users


# --- TELEGRAM BOT AUTO-DISCOVERY HANDLERS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        record_user_activity(update.effective_user.id)
    await update.message.reply_text("Bot active! Add me as an admin to your channels to auto-track them.")


async def track_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fired automatically when bot is added/removed as admin in a channel/group."""
    result = update.my_chat_member
    if not result:
        return

    chat = result.chat
    new_status = result.new_chat_member.status
    added_by = result.from_user.username if result.from_user else ""

    if new_status in ["administrator", "member"]:
        # Bot was added to channel
        print(f"[AUTO-DISCOVERY] Added to channel: {chat.title} ({chat.id})")
        add_or_update_channel(
            chat_id=chat.id,
            title=chat.title or "Untitled",
            username=chat.username or "",
            added_by_user=added_by
        )
    elif new_status in ["left", "kicked"]:
        # Bot was removed from channel
        print(f"[AUTO-DISCOVERY] Removed from channel: {chat.title} ({chat.id})")
        remove_channel(chat.id)


def start_bot_polling():
    """Runs the Telegram Bot event loop in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start_command))
    tg_app.add_handler(ChatMemberHandler(track_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    print("Starting Telegram Bot listener for auto-discovery...")
    loop.run_until_complete(tg_app.initialize())
    loop.run_until_complete(tg_app.start())
    loop.run_until_complete(tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES))


# Start background bot listener
bot_thread = threading.Thread(target=start_bot_polling, daemon=True)
bot_thread.start()


# --- DASHBOARD LOGIC ---
async def fetch_channel_counts():
    accounts = load_channels()
    bot = Bot(token=BOT_TOKEN)
    channel_results = []
    total_subs = 0

    for acc in accounts:
        chat_id = acc.get("chat_id")
        name = acc.get("name", "Unknown Channel")
        username = acc.get("username", "")
        owner = acc.get("owner", "")

        try:
            count = await bot.get_chat_member_count(chat_id=chat_id)
            total_subs += count
        except Exception as e:
            print(f"Failed to fetch count for '{name}' ({chat_id}): {e}")
            count = "Error"

        # Format Telegram links
        if username:
            channel_url = f"https://t.me/{username}"
        elif str(chat_id).startswith("-100"):
            channel_url = "#"  # Private group/channel
        else:
            channel_url = f"https://t.me/{str(chat_id).replace('@', '')}"

        owner_url = f"https://t.me/{owner}" if owner else "#"

        channel_results.append({
            "name": name,
            "count": count,
            "channel_url": channel_url,
            "owner_url": owner_url
        })

    return channel_results, total_subs, len(accounts)


# --- FLASK ROUTES ---
@app.route('/')
def index():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        channel_data, total_subscribers, total_channels = loop.run_until_complete(fetch_channel_counts())
    finally:
        loop.close()

    unique_users, active_users = get_user_analytics()

    return render_template(
        'index.html',
        channels=channel_data,
        total_channels=total_channels,
        total_subscribers=total_subscribers,
        unique_users=unique_users,
        active_users=active_users
    )


@app.route('/export')
def export_csv():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        channel_data, _, _ = loop.run_until_complete(fetch_channel_counts())
    finally:
        loop.close()

    def generate_csv():
        yield "Channel Name,Subscribers,Channel Link,Owner Contact Link\n"
        for ch in channel_data:
            subs = ch['count'] if ch['count'] != "Error" else 0
            yield f'"{ch["name"]}",{subs},"{ch["channel_url"]}","{ch["owner_url"]}"\n'

    return Response(
        generate_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=telegram_channels_export.csv"}
    )


if __name__ == '__main__':
    port = int(os.getenv("PORT", 8000))
    app.run(debug=True, host='0.0.0.0', port=port, use_reloader=False)