#!/usr/bin/env python3
# bot.py - Bypass Structure Maker Bot (Termux ready)
# Requires: pip install pyTelegramBotAPI

import os
import sqlite3
import time
import logging
from typing import List
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
)

# ----------------- CONFIG -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8104847586:AAH22P0YIDtm02mNVzw10GcKc7TabfGka20")
OWNER_ID = int(os.environ.get("OWNER_ID", "5730398152"))  # change to your telegram id
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", "@SRC_HUB")  # channel username to check

if BOT_TOKEN == "REPLACE_WITH_YOUR_TOKEN":
    raise SystemExit("Set BOT_TOKEN in env or edit script before running.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
logging.basicConfig(level=logging.INFO)

DB_PATH = "bot_data.db"

# ----------------- DB SETUP -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        tg_id INTEGER UNIQUE,
        username TEXT,
        full_name TEXT,
        first_seen INTEGER,
        structures_count INTEGER DEFAULT 0
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS structures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_tg_id INTEGER,
        text TEXT,
        created_at INTEGER,
        saved INTEGER DEFAULT 0,
        FOREIGN KEY(user_tg_id) REFERENCES users(tg_id)
    )""")
    conn.commit()
    conn.close()

init_db()

# ----------------- IN-MEM STATE (temporary flows) -----------------
# Keep minimal state to guide interactive flows. Persist outputs to DB.
user_state = {}
# state example:
# user_state[user_id] = {
#    "flow": "simple_single" | "simple_multi" | "hook",
#    "step": 1,
#    "offsets": [],
#    "selected_struct_type": None, # "PATCH_LIB" or "MemoryPatch" or "HOOK_LIB"
#    "selected_lib": None,
#    "connect_params": None
# }

# ----------------- HELPERS -----------------
def db_conn():
    return sqlite3.connect(DB_PATH)

def ensure_user_record(tg_user):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT tg_id FROM users WHERE tg_id = ?", (tg_user.id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (tg_id, username, full_name, first_seen, structures_count) VALUES (?, ?, ?, ?, ?)",
            (tg_user.id, tg_user.username or "", (tg_user.first_name or "") + (" " + (tg_user.last_name or "") if tg_user.last_name else ""), int(time.time()), 0)
        )
        conn.commit()
    else:
        # update username/fullname if changed
        cur.execute("UPDATE users SET username=?, full_name=? WHERE tg_id=?",
                    (tg_user.username or "", (tg_user.first_name or "") + (" " + (tg_user.last_name or "") if tg_user.last_name else ""), tg_user.id))
        conn.commit()
    conn.close()

def increment_user_struct_count(tg_id, amount=1):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET structures_count = structures_count + ? WHERE tg_id = ?", (amount, tg_id))
    conn.commit()
    conn.close()

def save_structure_to_db(tg_id, text, saved=1):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO structures (user_tg_id, text, created_at, saved) VALUES (?, ?, ?, ?)",
                (tg_id, text, int(time.time()), saved))
    conn.commit()
    conn.close()
    increment_user_struct_count(tg_id, 1)

def get_user_saved_structures(tg_id) -> List[dict]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, text, created_at FROM structures WHERE user_tg_id = ? ORDER BY created_at DESC", (tg_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "text": r[1], "created_at": r[2]} for r in rows]

def get_total_stats():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM structures")
    total_structs = cur.fetchone()[0]
    conn.close()
    return total_users, total_structs

def check_channel_membership(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        # statuses: 'member', 'creator', 'administrator'
        return member.status in ['member', 'creator', 'administrator']
    except Exception as e:
        logging.info("Membership check failed: %s", e)
        # If bot cannot check (e.g. wrong channel username), default True to avoid blocking
        return False

def format_struct_output(text: str):
    return f"✅ Generated Structure\n\n<pre>{text}</pre>"

def generate_patch_lib(libname, offsets: List[str], hex_bytes="00 20 70 47"):
    lines = []
    for off in offsets:
        lines.append(f'PATCH_LIB("{libname}", {off}, "{hex_bytes}");')
    return "\n".join(lines)

def generate_memory_patch(libname, offsets: List[str], hex_bytes="73 6F 6E 52 65"):
    lines = []
    for off in offsets:
        lines.append(f'MemoryPatch::createWithHex("{libname}",{off}, "{hex_bytes}").Modify();')
    return "\n".join(lines)

def generate_hook_lib(libname, offset, params: List[str]):
    params_str = ", ".join(params) if params else ""
    return f'HOOK_LIB("{libname}", {offset}, {params_str});'

# ----------------- KEYBOARDS -----------------
def start_inline_keyboard(joined=False):
    ik = InlineKeyboardMarkup()
    if not joined:
        ik.add(InlineKeyboardButton("Join Channel ✅", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
        ik.add(InlineKeyboardButton("I've Joined — Continue", callback_data="joined_check"))
    else:
        ik.row(
            InlineKeyboardButton("Simple Structure", callback_data="simple_structure"),
            InlineKeyboardButton("Hook Structure", callback_data="hook_structure")
        )
        ik.row(
            InlineKeyboardButton("Settings", callback_data="settings"),
            InlineKeyboardButton("Bot Information", callback_data="bot_info")
        )
    return ik

def simple_choice_kb():
    ik = InlineKeyboardMarkup()
    ik.row(InlineKeyboardButton("Single Offset", callback_data="simple_single"),
           InlineKeyboardButton("Multi Offset", callback_data="simple_multi"))
    return ik

def struct_type_kb():
    ik = InlineKeyboardMarkup()
    ik.row(InlineKeyboardButton("PATCH LIB", callback_data="stype_patch"),
           InlineKeyboardButton("Memory Patch", callback_data="stype_memory"))
    return ik

def lib_choice_kb():
    ik = InlineKeyboardMarkup()
    ik.row(InlineKeyboardButton("UE4", callback_data="lib_ue4"),
           InlineKeyboardButton("Anogs", callback_data="lib_anogs"),
           InlineKeyboardButton("Anort", callback_data="lib_anort"))
    return ik

def save_inline_kb(struct_db_id=None, already_saved=False):
    ik = InlineKeyboardMarkup()
    if not already_saved:
        ik.add(InlineKeyboardButton("Save ✅", callback_data=f"save_struct:{struct_db_id or 'pending'}"))
    else:
        ik.add(InlineKeyboardButton("Saved ✅", callback_data="noop"))
    return ik

# ----------------- COMMANDS -----------------
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    ensure_user_record(msg.from_user)
    # check join
    joined = check_channel_membership(msg.from_user.id)
    if not joined:
        text = ("👋 Welcome!\n\n"
                "Before using the bot, please join our channel.\n"
                "Tap the button below, join the channel and then press \"I've Joined — Continue\".")
        bot.send_message(msg.chat.id, text, reply_markup=start_inline_keyboard(joined=False))
        return
    # show profile page with photo
    send_profile_page(msg.chat.id, msg.from_user.id)

def send_profile_page(chat_id, user_id):
    try:
        user = bot.get_chat(user_id)
    except Exception:
        user = None

    # user profile photo (get first)
    photo_file_id = None
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            photo_file_id = photos.photos[0][0].file_id
    except Exception as e:
        logging.info("Failed to get profile photo: %s", e)

    nickname = (user.first_name or "") + (" " + (user.last_name or "") if user and user.last_name else "")
    username = f"@{user.username}" if user and user.username else "—"
    uid = user.id if user else user_id

    text = (
        f"👤 Nick name : {nickname}\n"
        f"👤 Username : {username}\n"
        f"👤 ID : {uid}\n"
        "----------------------------------------\n"
        f"Channel : {CHANNEL_USERNAME}\n"
    )

    kb = start_inline_keyboard(joined=True)
    if photo_file_id:
        bot.send_photo(chat_id, photo_file_id, caption=text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    user_id = call.from_user.id
    data = call.data or ""
    # Quick "I've Joined — Continue" check
    if data == "joined_check":
        if check_channel_membership(user_id):
            bot.edit_message_text("Thanks — you joined! Here's your profile.", call.message.chat.id, call.message.message_id)
            send_profile_page(call.message.chat.id, user_id)
        else:
            bot.answer_callback_query(call.id, "You haven't joined the channel yet — please join first.", show_alert=True)
        return

    if data == "simple_structure":
        bot.send_message(call.message.chat.id, "✨ Single Offset For Only 1 Offset\n✨ Multi Offset For Multiple Offsets\n\n🤖 Choice Option:", reply_markup=simple_choice_kb())
        return

    if data == "simple_single":
        user_state[user_id] = {"flow": "simple_single", "step": 1, "offsets": []}
        bot.send_message(call.message.chat.id, "✨ Single Offset selected.\n\nSend the offset now (e.g. 0xc23fa50):", reply_markup=InlineKeyboardMarkup())
        return

    if data == "simple_multi":
        user_state[user_id] = {"flow": "simple_multi", "step": 1, "offsets": []}
        bot.send_message(call.message.chat.id, "✨ Multi Offset selected.\n\nSend all offsets separated by newline. Example:\n0xCA9C6F0\n0xc23fa50\n0xY825FS0", reply_markup=InlineKeyboardMarkup())
        return

    if data == "hook_structure":
        user_state[user_id] = {"flow": "hook", "step": 1, "offsets": []}
        bot.send_message(call.message.chat.id, "⭐ Hook Structure selected.\n\nSend the offset now (e.g. 0xc23fa50):")
        return

    if data == "settings":
        # show settings + saved structures button
        structures = get_user_saved_structures(user_id)
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT structures_count, first_seen FROM users WHERE tg_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        structs_count = row[0] if row else 0
        first_seen = time.strftime("%Y-%m-%d", time.localtime(row[1])) if row and row[1] else "—"
        text = f"👤 Your Settings\n\nTotal generated structures: {structs_count}\nUsing since: {first_seen}\n\nSaved Structures: {len(structures)}"
        ik = InlineKeyboardMarkup()
        ik.add(InlineKeyboardButton("View Saved Structures", callback_data="view_saved"))
        ik.add(InlineKeyboardButton("Back", callback_data="back_to_profile"))
        bot.send_message(call.message.chat.id, text, reply_markup=ik)
        return

    if data == "view_saved":
        structures = get_user_saved_structures(user_id)
        if not structures:
            bot.send_message(call.message.chat.id, "No saved structures yet.")
            return
        for s in structures:
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["created_at"]))
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("Delete", callback_data=f"delstruct:{s['id']}"))
            bot.send_message(call.message.chat.id, f"🗂 Saved on {created}\n\n<pre>{s['text']}</pre>", reply_markup=kb)
        return

    if data.startswith("delstruct:"):
        sid = int(data.split(":",1)[1])
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM structures WHERE id = ?", (sid,))
        conn.commit()
        conn.close()
        bot.answer_callback_query(call.id, "Deleted.")
        return

    if data == "bot_info":
        text = ("🤖 Bot : Bypass Structure Maker Bot\n"
                "👤 Founder : @XTHrlen\n"
                "👤 Developer : @XTHrlen\n"
                "🔎 Bot Created : Tue, 30 September\n"
                f"🎀 Telegram Channel : {CHANNEL_USERNAME}\n"
                "✨ Website : srchub.kesug.com")
        bot.send_message(call.message.chat.id, text)
        return

    if data.startswith("stype_"):
        # select structure type for simple flows
        stype = data.split("_",1)[1]
        # store in state and ask lib
        cur_state = user_state.get(user_id)
        if not cur_state:
            bot.send_message(call.message.chat.id, "Session expired — start again.")
            return
        cur_state['selected_struct_type'] = 'PATCH_LIB' if stype == 'patch' else 'MemoryPatch'
        user_state[user_id] = cur_state
        bot.send_message(call.message.chat.id, "💫 UE4 - ( libUE4.so )\n💫 Anogs - ( libanogs.so )\n💫 Anort - ( libanort.so )\n\n🤖 Choice Option :", reply_markup=lib_choice_kb())
        return

    if data.startswith("lib_"):
        # user selected lib (UE4, Anogs, Anort)
        libkey = data.split("_",1)[1]
        libmap = {"ue4": "libUE4.so", "anogs": "libanogs.so", "anort": "libanort.so"}
        libname = libmap.get(libkey, "libUE4.so")
        cur_state = user_state.get(user_id)
        if not cur_state:
            bot.send_message(call.message.chat.id, "Session expired — start again.")
            return

        cur_state['selected_lib'] = libname
        flow = cur_state.get("flow")
        # generate based on flow
        if flow == "simple_single":
            # expecting exactly one offset in offsets list
            offsets = cur_state.get("offsets", [])
            if not offsets:
                bot.send_message(call.message.chat.id, "Offset missing. Send the offset first.")
                return
            if cur_state.get('selected_struct_type') == 'PATCH_LIB':
                text = generate_patch_lib(libname, offsets)
            else:
                text = generate_memory_patch(libname, offsets)
            # save to DB as unsaved (saved=0) initially but we'll show Save button
            save_structure_to_db(user_id, text, saved=0)
            # get last inserted id
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT id FROM structures WHERE user_tg_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
            last = cur.fetchone()
            conn.close()
            last_id = last[0] if last else None
            bot.send_message(call.message.chat.id, format_struct_output(text), reply_markup=save_inline_kb(struct_db_id=last_id, already_saved=False))
            # clear state
            user_state.pop(user_id, None)
            return

        if flow == "simple_multi":
            offsets = cur_state.get("offsets", [])
            if not offsets:
                bot.send_message(call.message.chat.id, "Offsets missing. Send the offsets first.")
                return
            if cur_state.get('selected_struct_type') == 'PATCH_LIB':
                text = generate_patch_lib(libname, offsets)
            else:
                text = generate_memory_patch(libname, offsets)
            save_structure_to_db(user_id, text, saved=0)
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT id FROM structures WHERE user_tg_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
            last = cur.fetchone()
            conn.close()
            last_id = last[0] if last else None
            bot.send_message(call.message.chat.id, format_struct_output(text), reply_markup=save_inline_kb(struct_db_id=last_id, already_saved=False))
            user_state.pop(user_id, None)
            return

        if flow == "hook":
            offsets = cur_state.get("offsets", [])
            if not offsets:
                bot.send_message(call.message.chat.id, "Offset missing. Send the offset first.")
                return
            offset = offsets[0]
            params = cur_state.get("connect_params", [])
            text = generate_hook_lib(libname, offset, params)
            save_structure_to_db(user_id, text, saved=0)
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("SELECT id FROM structures WHERE user_tg_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
            last = cur.fetchone()
            conn.close()
            last_id = last[0] if last else None
            bot.send_message(call.message.chat.id, format_struct_output(text), reply_markup=save_inline_kb(struct_db_id=last_id, already_saved=False))
            user_state.pop(user_id, None)
            return

    if data.startswith("save_struct:"):
        sid = data.split(":",1)[1]
        # mark saved in DB
        try:
            conn = db_conn()
            cur = conn.cursor()
            # if pending -> mark last structure for user as saved
            if sid == 'pending':
                cur.execute("UPDATE structures SET saved = 1 WHERE user_tg_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
            else:
                cur.execute("UPDATE structures SET saved = 1 WHERE id = ?", (int(sid),))
            conn.commit()
            conn.close()
            bot.answer_callback_query(call.id, "Saved to your account.")
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=save_inline_kb(already_saved=True))
        except Exception as e:
            logging.exception(e)
            bot.answer_callback_query(call.id, "Failed to save.")
        return

    if data == "back_to_profile":
        send_profile_page(call.message.chat.id, call.from_user.id)
        return

    if data == "noop":
        bot.answer_callback_query(call.id, "No action.")
        return

# ----------------- MESSAGE HANDLER (text inputs) -----------------
@bot.message_handler(func=lambda m: True)
def all_text_handler(m):
    uid = m.from_user.id
    text = m.text or ""
    # Owner command short access
    if text.strip().lower() == "/ownercmd" and m.from_user.id == OWNER_ID:
        total_users, total_structs = get_total_stats()
        # daily users approx: users created in last 24h
        conn = db_conn()
        cur = conn.cursor()
        day_ts = int(time.time()) - 24*3600
        cur.execute("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (day_ts,))
        daily = cur.fetchone()[0]
        conn.close()
        ik = InlineKeyboardMarkup()
        ik.add(InlineKeyboardButton("Check Users", callback_data="owner_check_users"),
               InlineKeyboardButton("Back", callback_data="back_to_profile"))
        bot.send_message(m.chat.id, f"🤖 Hi My Leader 👋\n\n📉 Total User : {total_users}\n📉 Daily User : {daily}\n📉 Total Struct : {total_structs}", reply_markup=ik)
        return

    # owner_check_users via message input (we'll also accept inline)
    if text.isdigit() and m.from_user.id == OWNER_ID and text != "0":
        # show that many profiles
        n = int(text)
        conn = db_conn()
        cur = conn.cursor()
        cur.execute("SELECT tg_id, username, full_name FROM users ORDER BY first_seen DESC LIMIT ?", (n,))
        rows = cur.fetchall()
        conn.close()
        lines = []
        for i, r in enumerate(rows, start=1):
            uname = f"@{r[1]}" if r[1] else f"{generate_random_code_for_user(r[0])} {r[2][:30]}"
            lines.append(f"👤 {i} : {uname}")
        bot.send_message(m.chat.id, "👤 Total User Profile:\n\n" + "\n".join(lines))
        return

    # handle interactive flows
    st = user_state.get(uid)
    if st:
        flow = st.get("flow")
        if flow == "simple_single" and st.get("step") == 1:
            # expect single offset
            offset = text.strip().split()[0]
            st['offsets'] = [offset]
            st['step'] = 2
            user_state[uid] = st
            bot.send_message(m.chat.id, " 🎀 Patch Lib Like This (PATCH_LIB)\n🎀 Memory Patch like This (MemoryPatch)\n\n🤖 Choice Option :", reply_markup=struct_type_kb())
            return

        if flow == "simple_multi" and st.get("step") == 1:
            # expect multiple offsets newline separated
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            st['offsets'] = lines
            st['step'] = 2
            user_state[uid] = st
            bot.send_message(m.chat.id, "🎀 Patch Lib Like This (PATCH_LIB)\n🎀 Memory Patch like This (MemoryPatch)\n\n🤖 Choice Option :", reply_markup=struct_type_kb())
            return

        if flow == "hook":
            step = st.get("step")
            if step == 1:
                # got offset
                offset = text.strip().split()[0]
                st['offsets'] = [offset]
                st['step'] = 2
                user_state[uid] = st
                bot.send_message(m.chat.id, "⭐ Send connect params separated by comma (example: connect1,connect2):")
                return
            elif step == 2:
                params = [p.strip() for p in text.split(",") if p.strip()]
                st['connect_params'] = params
                st['step'] = 3
                user_state[uid] = st
                bot.send_message(m.chat.id, "💫 UE4 - ( libUE4.so )\n💫 Anogs - ( libanogs.so )\n💫 Anort - ( libanort.so )\n\n🤖 Choice Option :", reply_markup=lib_choice_kb())
                return

    # if nothing matched, show help / start hint
    bot.send_message(m.chat.id, "Use /start to begin or tap the menu. If owner, send /ownercmd.")

# helper to make 4-word code if username missing
def generate_random_code_for_user(tg_id):
    # deterministic simple code
    return f"U{str(tg_id)[-4:]}"

# ----------------- EXTRA: handle owner_check_users callback
@bot.callback_query_handler(func=lambda c: c.data == "owner_check_users")
def owner_check_users_cb(call):
    if call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Not allowed.")
        return
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    total = cur.fetchone()[0]
    cur.execute("SELECT tg_id, username, full_name FROM users ORDER BY first_seen DESC LIMIT 7")
    rows = cur.fetchall()
    conn.close()
    lines = []
    for i, r in enumerate(rows, start=1):
        uname = f"@{r[1]}" if r[1] else f"{generate_random_code_for_user(r[0])} {r[2][:20]}"
        lines.append(f"👤 {i} : {uname}")
    bot.send_message(call.message.chat.id, f"👤 Total User Profile : {total}\n\n" + "\n".join(lines))

# ----------------- START POLLING -----------------
if __name__ == "__main__":
    logging.info("Bot started.")
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout = 5)
    except Exception as e:
        logging.exception("Bot crashed: %s", e)
