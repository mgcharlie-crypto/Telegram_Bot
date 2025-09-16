import telebot
from telebot import types
import sqlite3
import threading
import time
import os
import random
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = 7402783150

if not TOKEN:
    print("❌ Error: BOT_TOKEN environment variable is required")
    exit(1)
    
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ================= DATABASE =================
conn = sqlite3.connect("bot.db", check_same_thread=False)
cursor = conn.cursor()

# Original tables
cursor.execute("CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY)")
cursor.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS nicknames (user_id INTEGER PRIMARY KEY, nickname TEXT)")

# New tables for enhanced features
cursor.execute("CREATE TABLE IF NOT EXISTS love_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS admin_limits (user_id INTEGER PRIMARY KEY, daily_limit INTEGER DEFAULT 100, used_today INTEGER DEFAULT 0, last_reset DATE)")
cursor.execute("CREATE TABLE IF NOT EXISTS banned_admins (user_id INTEGER PRIMARY KEY, banned_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

# Music System tables
cursor.execute("""
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT DEFAULT ''
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS musics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    artist TEXT DEFAULT '',
    file_id TEXT,
    folder_id INTEGER,
    FOREIGN KEY(folder_id) REFERENCES folders(id)
)
""")

# Group tracking table
cursor.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        chat_type TEXT NOT NULL,
        title TEXT,
        username TEXT,
        member_count INTEGER DEFAULT 0,
        bot_joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT 1
    )
""")

conn.commit()

# ================= STATES =================
running_threads = {}  # Fight mode threads
ghost_targets = {}   # {chat_id: {target_id: True}}
speed_delay = 1
troll_targets = {}    # {chat_id: {user_id: template_index}}
funny_pairs = {}      # {chat_id: (id1, id2)}
love_targets = {}     # {chat_id: {user_id: template_index}}
love_troll_targets = {}  # {chat_id: {user_id: template_index}}
love_funny_pairs = {}    # {chat_id: (id1, id2)}
secret_monitoring = {}   # {chat_id: True}
hide_targets = {}        # {chat_id: set(user_ids)}

# Music System states
current_play = {}  # chat_id : music_id
playlist = {}      # chat_id : list of music_ids

# ================= HELPERS =================
def is_owner(uid):
    return uid == OWNER_ID

def is_admin(uid):
    if is_banned_admin(uid):
        return False
    cursor.execute("SELECT id FROM admins WHERE id=?", (uid,))
    return cursor.fetchone() is not None or is_owner(uid)

def add_admin_db(uid):
    cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (uid,))
    conn.commit()

def remove_admin_db(uid):
    cursor.execute("DELETE FROM admins WHERE id=?", (uid,))
    conn.commit()

def add_message_template(text):
    cursor.execute("INSERT INTO messages (text) VALUES (?)", (text,))
    conn.commit()

def list_message_templates():
    cursor.execute("SELECT id, text FROM messages")
    return cursor.fetchall()

def remove_message(mid):
    cursor.execute("DELETE FROM messages WHERE id=?", (mid,))
    conn.commit()

def set_nickname(user_id, nickname):
    cursor.execute("INSERT OR REPLACE INTO nicknames (user_id,nickname) VALUES (?,?)", (user_id, nickname))
    conn.commit()

def remove_nickname(user_id):
    cursor.execute("DELETE FROM nicknames WHERE user_id=?", (user_id,))
    conn.commit()

def get_nickname(user_id):
    cursor.execute("SELECT nickname FROM nicknames WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def mention(user_id, name):
    return f"<a href='tg://user?id={user_id}'>{name}</a>"

# ================= LOVE MESSAGE HELPERS =================
def add_love_message(text):
    cursor.execute("INSERT INTO love_messages (text) VALUES (?)", (text,))
    conn.commit()

def list_love_messages():
    cursor.execute("SELECT id, text FROM love_messages")
    return cursor.fetchall()

def remove_love_message(mid):
    cursor.execute("DELETE FROM love_messages WHERE id=?", (mid,))
    conn.commit()

# ================= MUSIC SYSTEM HELPERS =================
def owner_only(func):
    """Decorator for owner-only commands"""
    def wrapper(message):
        if message.from_user.id != OWNER_ID:
            return bot.reply_to(message, "❌ Owner သီးသန့်ပါ မင်းသုံးလို့မရဘူး")
        return func(message)
    return wrapper

def admin_or_owner_only(func):
    """Decorator for admin or owner commands"""
    def wrapper(message):
        if not is_admin(message.from_user.id):
            return bot.reply_to(message, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
        return func(message)
    return wrapper

def get_user_permission_status(user_id):
    """Get user permission status for display"""
    if user_id == OWNER_ID:
        return "👑 Owner"
    elif is_admin(user_id):
        return "🛡️ Admin"
    else:
        return "👤 Member"

# ================= ADMIN MANAGEMENT HELPERS =================
def is_banned_admin(uid):
    cursor.execute("SELECT user_id FROM banned_admins WHERE user_id=?", (uid,))
    return cursor.fetchone() is not None

def ban_admin(uid):
    cursor.execute("INSERT OR IGNORE INTO banned_admins (user_id) VALUES (?)", (uid,))
    conn.commit()

def unban_admin(uid):
    cursor.execute("DELETE FROM banned_admins WHERE user_id=?", (uid,))
    conn.commit()

def set_admin_limit(uid, limit):
    cursor.execute("INSERT OR REPLACE INTO admin_limits (user_id, daily_limit, used_today, last_reset) VALUES (?, ?, 0, date('now'))", (uid, limit))
    conn.commit()

def remove_admin_limit(uid):
    cursor.execute("DELETE FROM admin_limits WHERE user_id=?", (uid,))
    conn.commit()

# ================= GROUP TRACKING HELPERS =================
def track_chat(chat):
    """Track or update chat information"""
    try:
        # Get member count if possible
        member_count = 0
        try:
            if chat.type in ['group', 'supergroup']:
                member_count = bot.get_chat_member_count(chat.id)
        except:
            pass
            
        # Insert or update chat info
        cursor.execute("""
            INSERT OR REPLACE INTO chats 
            (chat_id, chat_type, title, username, member_count, last_seen, is_active) 
            VALUES (?, ?, ?, ?, ?, datetime('now'), 1)
        """, (chat.id, chat.type, chat.title, chat.username, member_count))
        conn.commit()
    except Exception as e:
        print(f"Error tracking chat: {e}")

def get_all_bot_users():
    """Get all users who have interacted with the bot"""
    try:
        # Get unique users from multiple sources
        users = set()
        
        # From admins
        cursor.execute("SELECT id FROM admins")
        admin_users = cursor.fetchall()
        for user in admin_users:
            users.add(user[0])
        
        # From nicknames
        cursor.execute("SELECT user_id FROM nicknames")
        nickname_users = cursor.fetchall()
        for user in nickname_users:
            users.add(user[0])
        
        # From troll targets (active users)
        for chat_targets in troll_targets.values():
            for user_id in chat_targets.keys():
                users.add(user_id)
                
        # From love targets
        for chat_targets in love_targets.values():
            for user_id in chat_targets.keys():
                users.add(user_id)
        
        return list(users)
    except:
        return []

def get_tracked_chats():
    """Get all tracked chats"""
    cursor.execute("SELECT chat_id, chat_type, title, username, member_count, bot_joined_date, last_seen, is_active FROM chats ORDER BY last_seen DESC")
    return cursor.fetchall()

# ================= EVENT HANDLERS ===========
@bot.message_handler(content_types=['new_chat_members'])
def welcome_new_member(message):
    """Handle new members joining the chat"""
    try:
        # Track the chat
        track_chat(message.chat)
        
        # Check if welcome mode is enabled
        if not globals().get('welcome_mode_enabled', False):
            return
            
        for new_member in message.new_chat_members:
            if not new_member.is_bot:  # Don't welcome bots
                name = new_member.first_name
                username = f"@{new_member.username}" if new_member.username else name
                
                welcome_message = globals().get('welcome_text', "🎉 ကြိုဆိုပါတယ် {name}! Group ကို လာရောက်ပါရှင့်အတွက် ကျေးဇူးတင်ပါတယ်။")
                welcome_message = welcome_message.replace("{name}", name).replace("{username}", username)
                
                bot.reply_to(message, welcome_message)
    except Exception as e:
        print(f"Error in welcome handler: {e}")

# ================= COMMANDS =================
@bot.message_handler(commands=['start'])
def startdeftyd(m):
    track_chat(m.chat)
    user_status = get_user_permission_status(m.from_user.id)
    bot.reply_to(m, f"💞 Bot စတင်ပြီးပါပြီ /help ကြည့်ပါ\nမသိတာရှိရင် Ownerကိုလာမေးပါ @mgcharlie\n\n{user_status}")

@bot.message_handler(commands=['help'])
def help_cmd(m):
    user_status = get_user_permission_status(m.from_user.id)
    text = f"""📋 Members Commands 
/start – Bot စတင်ခြင်း
/help – Help menu ပြသခြင်း
/time – လက်ရှိအချိန် ကြည့်ရန်
/info – Group အချက်အလက် ကြည့်ရန်
/id – User ID ကြည့်ရန် (reply )
/music – မင်းလေးနားဆင်ဖို့ သီချင်းတွေကြည့်မယ်
/random – Random music ဖွင့်ရန်၊ကြိုက်တာဖွင့်ဖို့

🔏 Admin Commands Mode - /admincmd
🔑 Owner Commands Mode - /ownercmd

Your Status: {user_status}"""
    bot.reply_to(m, text)

@bot.message_handler(commands=['admincmd'])
def admin_help(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    user_status = get_user_permission_status(m.from_user.id)
    text = f"""🛡️ Admin Commands
😈 Tarzan Suppression (တောသားနှိမ်နင်းရေး)
/topics - တောသားတွေကိုနှိမ်နင်းဖို့နည်းလမ်းများ

💞 ရည်းစားစကားပြောကြမယ်
/lovecmd - Love commands များကြည့်ရန်

🎵 Music System (သီချင်းနားထောင်ကြမယ်)
/song - မင်းလေးနားဆင်ဖို့ cmd တွေအကုန်ရှိပါတယ်

Your Status: {user_status}"""
    bot.reply_to(m, text)

# ================= MESSAGE TEMPLATES =================
@bot.message_handler(commands=['add_message'])
def add_message_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    if m.reply_to_message:
        text_to_add = m.reply_to_message.text
    else:
        args = m.text.split(maxsplit=1)
        if len(args) < 2:
            return bot.reply_to(m, "❌ /add_message message_text သုံးပါ")
        text_to_add = args[1]
    add_message_template(text_to_add)
    bot.reply_to(m, f"✔️ Message template ထည့်ပြီးပါပြီ: {text_to_add}")

@bot.message_handler(commands=['list_message'])
def list_message_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    templates = list_message_templates()
    if not templates:
        return bot.reply_to(m, "❌ Template မရှိပါ")
    text = "📝 <b>Fight Template List:</b>\n"
    for t in templates:
        text += f"{t[0]}. {t[1]}\n"
    bot.reply_to(m, text)

@bot.message_handler(commands=['remove_message'])
def remove_message_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /remove_message id1,id2,... format ဖြင့် template ID တွေကို ထည့်ပါ")
    try:
        # Split comma separated message IDs
        message_ids = [int(mid) for mid in args[1].split(',')]
        for mid in message_ids:
            remove_message(mid)  # Remove each message template by ID
        bot.reply_to(m, f"✔️ Template ID(s) {', '.join(map(str, message_ids))} ဖျက်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error: Invalid message ID(s)")

# ================= NICKNAME =================
@bot.message_handler(commands=['name'])
def set_name_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split(maxsplit=2)
    if len(args) < 3:
        return bot.reply_to(m, "❌ /name id/username nickname သုံးပါ")
    try:
        uid = int(args[1]) if args[1].isdigit() else bot.get_chat(args[1]).id
        set_nickname(uid, args[2])
        bot.reply_to(m, f"✔️ {args[2]} ကို nickname သတ်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error")

@bot.message_handler(commands=['remove_name'])
def remove_name_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /remove_name id/username သုံးပါ")
    try:
        uid = int(args[1]) if args[1].isdigit() else bot.get_chat(args[1]).id
        remove_nickname(uid)
        bot.reply_to(m, f"✔️ {uid} nickname ဖျက်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error")

# ================= LOVE SYSTEM COMMANDS =================
@bot.message_handler(commands=['lovecmd'])
def love_cmd_help(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ Admin သီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    text = """💞 <b>Love Commands</b>

💕 <b>Love Message Management</b>
/add_love_message - Love message template ထည့်ရန်
/list_love_messages - Love message templates ကြည့်ရန်
/remove_love_message id - Love message template ဖျက်ရန်

💖 <b>Love Modes</b>
/love id - User ကို အချစ်စကားများပြောမယ်
/love_troll id - Love troll mode စတင်
/love_funny id1 id2 - နှစ်ယောက်ကို love funny mode
/stoplove - Love modes အားလုံးရပ်ရန်

📊 <b>Love Templates</b>
- Reply message နှင့် /add_love_message သုံးပါ
- Love messages အတွက် {user1}, {user2} placeholders သုံးနိုင်ပါတယ်"""
    bot.reply_to(m, text)

@bot.message_handler(commands=['add_love_message'])
def add_love_message_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    if m.reply_to_message:
        text_to_add = m.reply_to_message.text
    else:
        args = m.text.split(maxsplit=1)
        if len(args) < 2:
            return bot.reply_to(m, "❌ /add_love_message message_text သုံးပါ")
        text_to_add = args[1]
    add_love_message(text_to_add)
    bot.reply_to(m, f"💞 Love message template ထည့်ပြီးပါပြီ: {text_to_add}")

@bot.message_handler(commands=['list_love_messages'])
def list_love_messages_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    templates = list_love_messages()
    if not templates:
        return bot.reply_to(m, "❌ Love template မရှိပါ")
    text = "💞 <b>Love Template List:</b>\n"
    for t in templates:
        text += f"{t[0]}. {t[1]}\n"
    bot.reply_to(m, text)

@bot.message_handler(commands=['remove_love_message'])
def remove_love_message_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /remove_love_message id သုံးပါ")
    try:
        mid = int(args[1])
        remove_love_message(mid)
        bot.reply_to(m, f"💞 Love template {mid} ဖျက်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error: Invalid love message ID")

@bot.message_handler(commands=['love'])
def love_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /love id1 id2 ... သုံးပါ")
    
    chat_id = m.chat.id
    love_templates = [row[1] for row in list_love_messages()]
    if not love_templates:
        return bot.reply_to(m, "❌ Love template မရှိပါ - /add_love_message နှင့် templates ထည့်ပါ")
    
    tid = f"love_{chat_id}"
    running_threads[tid] = True

    target_index = {}
    for target in args:
        try:
            uid = int(target) if target.isdigit() else bot.get_chat(target).id
            target_index[uid] = 0
        except:
            continue

    def love_loop():
        while running_threads.get(tid, False):
            for uid in target_index.keys():
                try:
                    idx = target_index[uid] % len(love_templates)
                    template = love_templates[idx]
                    name = get_nickname(uid) or bot.get_chat(uid).first_name
                    bot.send_message(chat_id, f"{mention(uid, name)} 💞 {template}")
                    target_index[uid] += 1
                    time.sleep(speed_delay)
                except:
                    continue
    
    threading.Thread(target=love_loop, daemon=True).start()
    bot.reply_to(m, "💗 ကိုယ်မင်းကိုဘယ်လောက်ချစ်ကြောင်းပြောပြမယ်💞😜")

@bot.message_handler(commands=['love_troll'])
def love_troll_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /love_troll id1 id2 ... သုံးပါ")
    
    chat_id = m.chat.id
    love_templates = [row[1] for row in list_love_messages()]
    if not love_templates:
        return bot.reply_to(m, "❌ Love template မရှိပါ - /add_love_message နှင့် templates ထည့်ပါ")
        
    love_troll_targets.setdefault(chat_id, {})
    
    for a in args:
        try:
            uid = int(a) if a.isdigit() else bot.get_chat(a).id
            if uid not in love_troll_targets[chat_id]:
                love_troll_targets[chat_id][uid] = 0
        except:
            continue
    
    bot.reply_to(m, "💞မင်းရဲ့အနားမှာအမြဲရှိနေတယ် မင်းစာတကြောင်းရေးရင် ကိုယ်တကြောင်းရေးမယ်😜💞")

@bot.message_handler(commands=['love_funny'])
def love_funny_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()
    if len(args) < 3:
        return bot.reply_to(m, "❌ /love_funny user1_id user2_id သုံးပါ")
    
    chat_id = m.chat.id
    try:
        user1_id, user2_id = int(args[1]), int(args[2])
        love_funny_pairs[chat_id] = (user1_id, user2_id)
        
        try:
            user1_name = get_nickname(user1_id) or bot.get_chat(user1_id).first_name
            user2_name = get_nickname(user2_id) or bot.get_chat(user2_id).first_name
            bot.reply_to(m, f"မင်းတို့နှစ်ဦးကြားချစ်ခြင်းမေတ္တာတွေဘဲရှိပါစေ💕")
        except:
            bot.reply_to(m, f"မင်းတို့နှစ်ဦးကြားချစ်ခြင်းမေတ္တာတွေဘဲရှိပါစေ💕")
    except:
        bot.reply_to(m, "❌ Error: Invalid user IDs")

@bot.message_handler(commands=['stoplove'])
def stop_love_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")

    chat_id = m.chat.id
    tid = f"love_{chat_id}"
    if tid in running_threads:
        running_threads[tid] = False
        del running_threads[tid]

    love_targets.pop(chat_id, None)
    love_troll_targets.pop(chat_id, None)
    love_funny_pairs.pop(chat_id, None)

    bot.reply_to(m, "💞 Love modes အားလုံးရပ်ပြီးပါပြီ")

@bot.message_handler(commands=['topics'])
def topics_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    user_status = get_user_permission_status(m.from_user.id)
    text = f"""🛡️ Admin Commands
😈 Tarzan Suppression (တောသားနှိမ်နင်းရေး)
/id - user id ကြည့်မယ် (reply ထောက်ပါ)
/fight (id) - တောသားတွေကိုနှိမ်နင်းမယ်
/stopall – Fight/Troll/Funny /hideရပ်ရန်
/add_message – Fight message template ထည့်ရန်
/troll (id) - တောသားတွေကို troll
/funny id1 id2 - တောသားနှစ်ကောင်ကိုရန်တိုက်ပေး
/name id/ - တောသားတွေကိုနာမည်ပေး
/remove_name id/ - တောသားနာမည်ဖျက်
/hide id - တောသားအရေးမပေးဘူးအကုန်ဖျက်
/topics - တောသားတွေကိုနှိမ်နင်းဖို့နည်းလမ်းများ

Your Status: {user_status}"""
    bot.reply_to(m, text)

@bot.message_handler(commands=['song'])
def song_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    user_status = get_user_permission_status(m.from_user.id)
    text = f"""👥 Member Commands:
/music - Interactive music library browser
/play [id|title] - Play specific music
/random - Play random music
/search [query] - Search music by title/artist
/music_info [id] - Get detailed music info
/folder_info [id] - Get folder details

🛡️ Admin Commands:
/add_music [folder_id] [title] [artist] - Add music (reply to audio)
/remove_music [id] - Remove music by ID
/edit_music [id] [title] [artist] - Edit music info
/music_stats - View music statistics

👑 Owner Commands:
/create_folder [name] [description] - Create music folder
/edit_folder [id] [name] [description] - Edit folder
/delete_folder [id] - Delete folder (with confirmation)
/folder_list - List all folders with management options
/music_admin - Advanced music management panel

💝 မင်းလေးအတွက် ချစ်ခြင်းမေတ္တာနှင့်တကွ သီချင်းလေးတွေ

Your Status: {user_status}"""
    bot.reply_to(m, text)

# Music folder management
@bot.message_handler(commands=['create_folder'])
@owner_only
def create_folder_cmd(message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /create_folder FolderName [Description]")
    name = args[1].strip()
    description = args[2].strip() if len(args) > 2 else ''
    try:
        cursor.execute("INSERT INTO folders (name, description) VALUES (?, ?)", (name, description))
        conn.commit()
        bot.reply_to(message, f"✅ Folder <b>{name}</b> created")
    except sqlite3.IntegrityError:
        bot.reply_to(message, "⚠️ Folder already exists")

@bot.message_handler(commands=['folder_list'])
@owner_only
def folder_list_cmd(message):
    cursor.execute("SELECT id,name,description FROM folders")
    folders = cursor.fetchall()
    if not folders:
        return bot.reply_to(message, "⚠️ No folders")
    markup = types.InlineKeyboardMarkup()
    for f in folders:
        markup.add(types.InlineKeyboardButton(f"{f[0]}: {f[1]}", callback_data=f"folder_owner:{f[0]}"))
    bot.send_message(message.chat.id, "📂 Folder List:", reply_markup=markup)

@bot.message_handler(commands=['add_music'])
@admin_or_owner_only
def add_music_cmd(message):
    if not message.reply_to_message or not message.reply_to_message.audio:
        return bot.reply_to(message, "❌ Reply an audio with /add_music FolderID [Title] [Artist]")
    args = message.text.split(maxsplit=3)
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /add_music FolderID [Title] [Artist]")
    try:
        folder_id = int(args[1])
    except:
        return bot.reply_to(message, "⚠️ Invalid FolderID")
    cursor.execute("SELECT id FROM folders WHERE id=?", (folder_id,))
    if not cursor.fetchone():
        return bot.reply_to(message, "⚠️ Folder not found")
    
    file_id = message.reply_to_message.audio.file_id
    title = args[2] if len(args) > 2 else (message.reply_to_message.audio.title or "Unknown")
    artist = args[3] if len(args) > 3 else (message.reply_to_message.audio.performer or "Unknown Artist")
    
    cursor.execute("INSERT INTO musics (title,artist,file_id,folder_id) VALUES (?,?,?,?)", (title,artist,file_id,folder_id))
    conn.commit()
    bot.reply_to(message, f"✅ Added <b>{title}</b> by {artist} to Folder ID {folder_id}")

@bot.message_handler(commands=['remove_music'])
@owner_only
def remove_music_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /remove_music id1,id2,...")
    ids = [int(x) for x in args[1].split(",")]
    for mid in ids:
        cursor.execute("DELETE FROM musics WHERE id=?", (mid,))
    conn.commit()
    bot.reply_to(message, f"🗑 Removed music IDs: {', '.join(map(str,ids))}")

@bot.message_handler(commands=['edit_folder'])
@owner_only
def edit_folder_cmd(message):
    args = message.text.split(maxsplit=3)
    if len(args) < 3:
        return bot.reply_to(message, "❌ Usage: /edit_folder id NewName [NewDescription]")
    folder_id, new_name = int(args[1]), args[2]
    new_description = args[3] if len(args) > 3 else ''
    cursor.execute("UPDATE folders SET name=?, description=? WHERE id=?", (new_name, new_description, folder_id))
    conn.commit()
    bot.reply_to(message, f"✏️ Folder ID {folder_id} updated")

@bot.message_handler(commands=['delete_folder'])
@owner_only
def delete_folder_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /delete_folder id")
    folder_id = int(args[1])
    cursor.execute("DELETE FROM musics WHERE folder_id=?", (folder_id,))
    cursor.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()
    bot.reply_to(message, f"🗑 Folder ID {folder_id} and its musics deleted")

# Member music commands
@bot.message_handler(commands=['music'])
def music_menu_cmd(message):
    cursor.execute("SELECT id,name,description FROM folders")
    folders = cursor.fetchall()
    if not folders:
        return bot.reply_to(message, "⚠️ No folders")
    markup = types.InlineKeyboardMarkup()
    for f in folders:
        desc = f" - {f[2]}" if f[2] else ""
        markup.add(types.InlineKeyboardButton(f"{f[1]}{desc}", callback_data=f"folder_member:{f[0]}"))
    bot.send_message(message.chat.id, "📂 Select a folder:", reply_markup=markup)

@bot.message_handler(commands=['play'])
def play_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /play id|title")
    param = args[1]
    if param.isdigit():
        mid = int(param)
        cursor.execute("SELECT file_id,title,artist FROM musics WHERE id=?", (mid,))
        row = cursor.fetchone()
        if row:
            bot.send_audio(message.chat.id, row[0], caption=f"🎵 {row[1]} - {row[2]}")
            current_play[message.chat.id] = mid
        else:
            bot.reply_to(message, "⚠️ Music not found")
    else:
        cursor.execute("SELECT id,file_id,title,artist FROM musics WHERE title LIKE ? OR artist LIKE ?", (f"%{param}%", f"%{param}%"))
        row = cursor.fetchone()
        if row:
            bot.send_audio(message.chat.id, row[1], caption=f"🎵 {row[2]} - {row[3]}")
            current_play[message.chat.id] = row[0]
        else:
            bot.reply_to(message, "⚠️ Music not found")

@bot.message_handler(commands=['random'])
def random_music_cmd(message):
    cursor.execute("SELECT id,file_id,title,artist FROM musics ORDER BY RANDOM() LIMIT 1")
    row = cursor.fetchone()
    if row:
        bot.send_audio(message.chat.id, row[1], caption=f"🎵 {row[2]} - {row[3]}")
        current_play[message.chat.id] = row[0]
    else:
        bot.reply_to(message, "⚠️ No musics found")

@bot.message_handler(commands=['search'])
def search_music_cmd(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /search query")
    query = args[1]
    cursor.execute("SELECT id,title,artist FROM musics WHERE title LIKE ? OR artist LIKE ? LIMIT 10", (f"%{query}%", f"%{query}%"))
    results = cursor.fetchall()
    if not results:
        return bot.reply_to(message, "⚠️ No music found")
    text = "🔍 Search Results:\n"
    for r in results:
        text += f"{r[0]} - {r[1]} by {r[2]}\n"
    text += "\nUse /play <id> to play a song"
    bot.reply_to(message, text)

@bot.message_handler(commands=['music_info'])
def music_info_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /music_info id")
    try:
        mid = int(args[1])
        cursor.execute("SELECT m.id,m.title,m.artist,f.name FROM musics m JOIN folders f ON m.folder_id=f.id WHERE m.id=?", (mid,))
        row = cursor.fetchone()
        if row:
            text = f"🎵 <b>Music Info</b>\nID: {row[0]}\nTitle: {row[1]}\nArtist: {row[2]}\nFolder: {row[3]}"
            bot.reply_to(message, text)
        else:
            bot.reply_to(message, "⚠️ Music not found")
    except:
        bot.reply_to(message, "❌ Invalid music ID")

@bot.message_handler(commands=['folder_info'])
def folder_info_cmd(message):
    args = message.text.split()
    if len(args) < 2:
        return bot.reply_to(message, "❌ Usage: /folder_info id")
    try:
        fid = int(args[1])
        cursor.execute("SELECT name,description FROM folders WHERE id=?", (fid,))
        folder = cursor.fetchone()
        if not folder:
            return bot.reply_to(message, "⚠️ Folder not found")
        cursor.execute("SELECT COUNT(*) FROM musics WHERE folder_id=?", (fid,))
        count = cursor.fetchone()[0]
        text = f"📂 <b>Folder Info</b>\nName: {folder[0]}\nDescription: {folder[1] or 'No description'}\nMusic Count: {count}"
        bot.reply_to(message, text)
    except:
        bot.reply_to(message, "❌ Invalid folder ID")

@bot.message_handler(commands=['next'])
def next_music_cmd(message):
    chat_id = message.chat.id
    cursor.execute("SELECT id,file_id,title,artist FROM musics ORDER BY RANDOM() LIMIT 1")
    row = cursor.fetchone()
    if row:
        bot.send_audio(chat_id, row[1], caption=f"⏭️ {row[2]} - {row[3]}")
        current_play[chat_id] = row[0]
    else:
        bot.reply_to(message, "⚠️ No musics found")

@bot.message_handler(commands=['music_list'])
def music_list_cmd(message):
    cursor.execute("SELECT id,title,artist,folder_id FROM musics")
    musics = cursor.fetchall()
    if not musics:
        return bot.reply_to(message, "⚠️ No musics")
    text = "🎶 Music List:\n"
    for m in musics:
        text += f"{m[0]} – {m[1]} by {m[2]} (Folder {m[3]})\n"
    bot.send_message(message.chat.id, text)

@bot.message_handler(commands=['music_stats'])
@admin_or_owner_only
def music_stats_cmd(message):
    cursor.execute("SELECT COUNT(*) FROM folders")
    folder_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM musics")
    music_count = cursor.fetchone()[0]
    text = f"📊 <b>Music Statistics</b>\n\n📂 Total Folders: {folder_count}\n🎵 Total Musics: {music_count}"
    bot.reply_to(message, text)

# Missing admin commands
@bot.message_handler(commands=['hide'])
def hide_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")

    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /hide id1 id2 ... သုံးပါ")

    chat_id = m.chat.id
    hide_targets.setdefault(chat_id, set())

    added = 0
    for a in args:
        try:
            uid = int(a) if a.isdigit() else bot.get_chat(a).id
            hide_targets[chat_id].add(uid)
            added += 1
        except:
            continue

    bot.reply_to(m, f"😈 {added} ယောက်ကို hide mode ထဲထည့်ပြီးပါပြီ")
@bot.message_handler(commands=['use'])
def use_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    if m.reply_to_message:
        user_id = m.reply_to_message.from_user.id
        user_name = get_nickname(user_id) or m.reply_to_message.from_user.first_name
        bot.reply_to(m, f"🎯 Using {mention(user_id, user_name)}")
    else:
        bot.reply_to(m, "❌ Reply လုပ်ပြီး /use သုံးပါ")

@bot.message_handler(commands=['id'])
def id_cmd(m):
    if m.reply_to_message:
        user_id = m.reply_to_message.from_user.id
        user_name = get_nickname(user_id) or m.reply_to_message.from_user.first_name
        username = f"@{m.reply_to_message.from_user.username}" if m.reply_to_message.from_user.username else "No username"
        bot.reply_to(m, f"🆔 <b>User Info:</b>\nName: {user_name}\nID: <code>{user_id}</code>\nUsername: {username}")
    else:
        bot.reply_to(m, f"🆔 <b>Your Info:</b>\nName: {m.from_user.first_name}\nID: <code>{m.from_user.id}</code>\nUsername: @{m.from_user.username or 'No username'}")

@bot.message_handler(commands=['info'])
def info_cmd(m):
    if m.reply_to_message:
        user_id = m.reply_to_message.from_user.id
        user_name = get_nickname(user_id) or m.reply_to_message.from_user.first_name
        username = f"@{m.reply_to_message.from_user.username}" if m.reply_to_message.from_user.username else "No username"
        user_status = get_user_permission_status(user_id)
        bot.reply_to(m, f"ℹ️ <b>User Details:</b>\nName: {user_name}\nID: <code>{user_id}</code>\nUsername: {username}\nStatus: {user_status}")
    else:
        user_status = get_user_permission_status(m.from_user.id)
        bot.reply_to(m, f"ℹ️ <b>Your Details:</b>\nName: {m.from_user.first_name}\nID: <code>{m.from_user.id}</code>\nUsername: @{m.from_user.username or 'No username'}\nStatus: {user_status}")

@bot.message_handler(commands=['time'])
def time_cmd(m):
    try:
        import pytz
        myanmar_tz = pytz.timezone('Asia/Yangon')
        current_time = datetime.now(myanmar_tz).strftime("%Y-%m-%d %H:%M:%S")
        bot.reply_to(m, f"🕐 <b>Myanmar Time:</b>\n{current_time}")
    except ImportError:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bot.reply_to(m, f"🕐 <b>Current Time:</b>\n{current_time}")

@bot.message_handler(commands=['edit_music'])
@admin_or_owner_only
def edit_music_cmd(m):
    """Edit music metadata (Admin only)"""
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    args = m.text.split(maxsplit=3)
    if len(args) < 3:
        return bot.reply_to(m, "❌ /edit_music [music_id] [new_title] [new_artist] သုံးပါ")
    
    try:
        music_id = int(args[1])
        new_title = args[2]
        new_artist = args[3] if len(args) > 3 else ''
        
        cursor.execute("UPDATE musics SET title=?, artist=? WHERE id=?", (new_title, new_artist, music_id))
        if cursor.rowcount > 0:
            conn.commit()
            bot.reply_to(m, f"✅ Music ID {music_id} updated successfully")
        else:
            bot.reply_to(m, "❌ Music not found")
    except ValueError:
        bot.reply_to(m, "❌ Invalid music ID")
    except Exception as e:
        bot.reply_to(m, f"❌ Error updating music: {str(e)}")

@bot.message_handler(commands=['music_admin'])
@owner_only
def music_admin_cmd(m):
    """Advanced music management panel (Owner only)"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    cursor.execute("SELECT COUNT(*) FROM folders")
    folder_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM musics")
    music_count = cursor.fetchone()[0]
    
    text = f"""🎵 <b>Music Admin Panel</b>

📊 Statistics:
• Total Folders: {folder_count}
• Total Musics: {music_count}

🛠️ Available Commands:
/create_folder [name] [desc] - Create new folder
/edit_folder [id] [name] [desc] - Edit folder
/delete_folder [id] - Delete folder
/add_music [folder_id] - Add music (reply audio)
/remove_music [id] - Remove music
/edit_music [id] [title] [artist] - Edit music
/music_stats - View detailed statistics
/folder_list - Manage all folders

📁 Quick Actions:
• Use /folder_list to manage folders
• Use /music_list to view all music
• All music operations require proper permissions"""
    
    bot.reply_to(m, text)


# ================= MISSING COMMANDS FIXES =================
@bot.message_handler(commands=['gp_list'])
def gp_list_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    chats = get_tracked_chats()
    if not chats:
        return bot.reply_to(m, "⚠️ No groups tracked yet")
    
    text = "📋 <b>Group List (Detailed)</b>\n\n"
    for chat in chats:
        chat_id, chat_type, title, username, member_count, joined_date, last_seen, is_active = chat
        status = "🟢 Active" if is_active else "🔴 Inactive"
        username_text = f"@{username}" if username else "No username"
        text += f"🏢 <b>{title or 'Unknown'}</b>\n"
        text += f"ID: <code>{chat_id}</code>\n"
        text += f"Type: {chat_type.capitalize()}\n"
        text += f"Username: {username_text}\n"
        text += f"Members: {member_count}\n"
        text += f"Status: {status}\n"
        text += f"Joined: {joined_date}\n"
        text += f"Last Seen: {last_seen}\n\n"
    
    bot.reply_to(m, text)

@bot.message_handler(commands=['shutdown'])
def shutdown_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    # Stop all running threads
    for tid in list(running_threads.keys()):
        running_threads[tid] = False
    running_threads.clear()
    
    # Clear all targets
    troll_targets.clear()
    funny_pairs.clear()
    love_targets.clear()
    love_troll_targets.clear()
    love_funny_pairs.clear()
    hide_targets.clear()
    secret_monitoring.clear()
    
    bot.reply_to(m, "🚫 Bot is shutting down... ချာလီဆိုတဲ့ကောင်လီးဘဲ🥴")
    
    # Actually shutdown the bot
    import sys
    sys.exit(0)

@bot.message_handler(commands=['preview'])
def preview_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    user_status = get_user_permission_status(m.from_user.id)
    
    # Get detailed bot statistics
    cursor.execute("SELECT COUNT(*) FROM admins")
    admin_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM messages")
    message_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM love_messages")
    love_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM chats WHERE is_active=1")
    active_chats = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM folders")
    folder_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM musics")
    music_count = cursor.fetchone()[0]
    
    # Count active modes
    active_fight_threads = len([tid for tid in running_threads.keys() if 'fight_' in tid and running_threads[tid]])
    active_trolls = sum(len(targets) for targets in troll_targets.values())
    active_love_trolls = sum(len(targets) for targets in love_troll_targets.values())
    hidden_users = sum(len(users) for users in hide_targets.values())
    
    text = f"""📊 <b>Bot Preview (Detailed Status)</b>

🛡️ <b>Administration</b>
Admins: {admin_count}
Active Chats: {active_chats}
Hidden Users: {hidden_users}
Secret Monitoring: {len(secret_monitoring)} chats

🎵 <b>Music System</b>
Folders: {folder_count}
Total Music: {music_count}

📝 <b>Templates</b>
Fight Messages: {message_count}
Love Messages: {love_count}

⚔️ <b>Active Modes</b>
Fight Threads: {active_fight_threads}
Troll Targets: {active_trolls}
Love Troll Targets: {active_love_trolls}
Funny Pairs: {len(funny_pairs)}
Love Funny Pairs: {len(love_funny_pairs)}

👤 <b>Your Status:</b> {user_status}

🤖 Bot running smoothly! ကွင်းစာညညားး အလုပ်လုပ်နေ😎"""
    
    bot.reply_to(m, text)

@bot.message_handler(commands=['dashboard'])
def dashboard_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    # Get comprehensive dashboard data
    cursor.execute("SELECT COUNT(*) FROM admins")
    admin_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM banned_admins")
    banned_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM chats")
    total_chats = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM chats WHERE is_active=1")
    active_chats = cursor.fetchone()[0]
    
    cursor.execute("SELECT SUM(member_count) FROM chats WHERE is_active=1")
    total_members = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT COUNT(*) FROM admin_limits")
    limited_admins = cursor.fetchone()[0]
    
    # Get system status
    active_fight_threads = len([tid for tid in running_threads.keys() if 'fight_' in tid and running_threads[tid]])
    active_modes = {
        'trolls': sum(len(targets) for targets in troll_targets.values()),
        'funny_pairs': len(funny_pairs),
        'love_trolls': sum(len(targets) for targets in love_troll_targets.values()),
        'love_funny_pairs': len(love_funny_pairs),
        'hidden_users': sum(len(users) for users in hide_targets.values()),
        'secret_monitoring': len(secret_monitoring)
    }
    
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="dash_stats"),
        types.InlineKeyboardButton("🛡️ Admins", callback_data="dash_admins")
    )
    markup.row(
        types.InlineKeyboardButton("🎵 Music", callback_data="dash_music"),
        types.InlineKeyboardButton("📝 Templates", callback_data="dash_templates")
    )
    markup.row(
        types.InlineKeyboardButton("🚫 Emergency Stop", callback_data="dash_emergency")
    )
    
    text = f"""📊 <b>Owner Dashboard (Comprehensive)</b>

📈 <b>System Overview</b>
Total Chats: {total_chats} ({active_chats} active)
Total Members: {total_members:,}
Admins: {admin_count} ({banned_count} banned)
Limited Admins: {limited_admins}

⚙️ <b>Active Systems</b>
Fight Threads: {active_fight_threads}
Troll Targets: {active_modes['trolls']}
Funny Pairs: {active_modes['funny_pairs']}
Love Trolls: {active_modes['love_trolls']}
Love Funny: {active_modes['love_funny_pairs']}
Hidden Users: {active_modes['hidden_users']}
Secret Monitoring: {active_modes['secret_monitoring']} chats

🔋 <b>Settings</b>
Speed Delay: {speed_delay}s
Welcome Mode: {'ON' if welcome_mode_enabled else 'OFF'}
Speed Permission: {'ON' if speed_permission_enabled else 'OFF'}

👑 Owner ID: {OWNER_ID}"""
    
    bot.send_message(m.chat.id, text, reply_markup=markup)

@bot.message_handler(commands=['upload'])
def upload_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    if m.reply_to_message:
        # Broadcast the replied message to all active chats
        chats = get_tracked_chats()
        if not chats:
            return bot.reply_to(m, "⚠️ No active chats to broadcast to")
        
        success_count = 0
        fail_count = 0
        
        for chat in chats:
            chat_id = chat[0]
            if chat[7]:  # is_active
                try:
                    if m.reply_to_message.text:
                        bot.send_message(chat_id, m.reply_to_message.text)
                    elif m.reply_to_message.photo:
                        bot.send_photo(chat_id, m.reply_to_message.photo[-1].file_id, 
                                     caption=m.reply_to_message.caption)
                    elif m.reply_to_message.video:
                        bot.send_video(chat_id, m.reply_to_message.video.file_id,
                                     caption=m.reply_to_message.caption)
                    elif m.reply_to_message.audio:
                        bot.send_audio(chat_id, m.reply_to_message.audio.file_id,
                                     caption=m.reply_to_message.caption)
                    elif m.reply_to_message.document:
                        bot.send_document(chat_id, m.reply_to_message.document.file_id,
                                        caption=m.reply_to_message.caption)
                    success_count += 1
                except:
                    fail_count += 1
        
        bot.reply_to(m, f"📤 Broadcast complete!\n✅ Success: {success_count}\n❌ Failed: {fail_count}")
    else:
        bot.reply_to(m, "❌ Reply to a message to broadcast it to all groups")

@bot.message_handler(commands=['adminlist'])
def adminlist_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    # Get all admins
    cursor.execute("SELECT id FROM admins")
    admin_ids = [row[0] for row in cursor.fetchall()]
    
    if not admin_ids:
        return bot.reply_to(m, "⚠️ No admins found")
    
    text = "🛡️ <b>Admin List (Detailed)</b>\n\n"
    
    for i, admin_id in enumerate(admin_ids, 1):
        try:
            # Check if banned
            cursor.execute("SELECT banned_date FROM banned_admins WHERE user_id=?", (admin_id,))
            banned_info = cursor.fetchone()
            
            # Check limits
            cursor.execute("SELECT daily_limit, used_today, last_reset FROM admin_limits WHERE user_id=?", (admin_id,))
            limit_info = cursor.fetchone()
            
            # Get admin info
            try:
                user = bot.get_chat(admin_id)
                name = user.first_name
                username = f"@{user.username}" if user.username else "No username"
            except:
                name = "Unknown"
                username = "No username"
            
            status = "🚫 Banned" if banned_info else "✅ Active"
            limit_text = "No limit" if not limit_info else f"{limit_info[1]}/{limit_info[0]} today"
            
            text += f"{i}. <b>{name}</b>\n"
            text += f"ID: <code>{admin_id}</code>\n"
            text += f"Username: {username}\n"
            text += f"Status: {status}\n"
            text += f"Limit: {limit_text}\n\n"
            
        except Exception as e:
            text += f"{i}. <b>Error loading admin {admin_id}</b>\n\n"
    
    text += f"\n👑 <b>Owner:</b> {OWNER_ID}\n"
    text += f"📊 <b>Total Admins:</b> {len(admin_ids)}"
    
    bot.reply_to(m, text)

@bot.message_handler(commands=['admin_unlimit'])
def admin_unlimit_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /admin_unlimit user_id သုံးပါ")
    
    try:
        user_id = int(args[1])
        remove_admin_limit(user_id)
        bot.reply_to(m, f"✅ Admin {user_id} ကို limit ဖျက်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Invalid user ID")

@bot.message_handler(commands=['admin_limit'])
def admin_limit_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    args = m.text.split()
    if len(args) < 3:
        return bot.reply_to(m, "❌ /admin_limit user_id daily_limit သုံးပါ")
    
    try:
        user_id = int(args[1])
        limit = int(args[2])
        set_admin_limit(user_id, limit)
        bot.reply_to(m, f"✅ Admin {user_id} ကို daily limit {limit} သတ်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Invalid user ID or limit")

@bot.message_handler(commands=['ban_admin'])
def ban_admin_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /ban_admin user_id သုံးပါ")
    
    try:
        user_id = int(args[1])
        if user_id == OWNER_ID:
            return bot.reply_to(m, "❌ Owner ကို ban မလုပ်ဘူး")
        
        ban_admin(user_id)
        bot.reply_to(m, f"✅ Admin {user_id} ကို ban လုပ်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Invalid user ID")




# ================= OWNER COMMANDS =================
@bot.message_handler(commands=['ownercmd'])
def owner_help(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    user_status = get_user_permission_status(m.from_user.id)
    text = f"""👑 <b>Owner Commands</b>

🛡️ <b>Admin Management</b>
/add_admin id - Add admin
/remove_admin id - Remove admin
/adminlist - View all admins
/ban_admin id - Ban admin
/unban_admin id - Unban admin
/admin_limit id limit - Set daily limit
/admin_unlimit id - Remove limit

📊 <b>System Control</b>
/dashboard - System dashboard
/preview - Bot status preview
/gp_list - Group list (detailed)
/shutdown - Shutdown bot
/upload - Broadcast message

🎵 <b>Music Management</b>
/create_folder name - Create music folder
/folder_list - Manage folders
/delete_folder id - Delete folder

📝 <b>Content Management</b>
/add_message - Add fight template
/add_love_message - Add love template
/list_message - View fight templates
/list_love_messages - View love templates

⚙️ <b>Settings</b>
/speed_on - Enable speed for admins
/speed_off - Disable speed for admins
/welcome - Toggle welcome mode
/welcome_text text - Set welcome message

Your Status: {user_status}"""
    bot.reply_to(m, text)

# Music System Callback Handlers
@bot.callback_query_handler(func=lambda c:c.data.startswith("folder_member:"))
def folder_member_callback(call):
    folder_id = int(call.data.split(":")[1])
    cursor.execute("SELECT id,title,artist FROM musics WHERE folder_id=?", (folder_id,))
    musics = cursor.fetchall()
    if not musics:
        return bot.answer_callback_query(call.id, "⚠️ No musics in this folder")
    markup = types.InlineKeyboardMarkup()
    for m in musics:
        display_name = f"{m[1]} - {m[2]}" if m[2] else m[1]
        markup.add(types.InlineKeyboardButton(display_name, callback_data=f"play:{m[0]}"))
    bot.send_message(call.message.chat.id, "🎶 Musics:", reply_markup=markup)

@bot.callback_query_handler(func=lambda c:c.data.startswith("folder_owner:"))
def folder_owner_callback(call):
    folder_id = int(call.data.split(":")[1])
    cursor.execute("SELECT id,title,artist FROM musics WHERE folder_id=?", (folder_id,))
    musics = cursor.fetchall()
    if not musics:
        return bot.answer_callback_query(call.id, "⚠️ No musics in this folder")
    text = "🎶 Musics in this folder:\n"
    for m in musics:
        text += f"{m[0]} – {m[1]} by {m[2]}\n"
    bot.send_message(call.message.chat.id, text)

@bot.callback_query_handler(func=lambda c:c.data.startswith("play:"))
def play_callback(call):
    mid = int(call.data.split(":")[1])
    cursor.execute("SELECT file_id,title,artist FROM musics WHERE id=?", (mid,))
    row = cursor.fetchone()
    if row:
        chat_id = call.message.chat.id
        current_play[chat_id] = mid
        bot.send_audio(chat_id, row[0], caption=f"🎵 {row[1]} - {row[2]}")
        bot.answer_callback_query(call.id, f"▶️ Playing {row[1]}")
    else:
        bot.answer_callback_query(call.id, "⚠️ Music not found")

# ================= SPECIAL FEATURES =================

@bot.message_handler(commands=['unhide'])
def unhide_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /unhide id1 id2 ... သုံးပါ")
    
    chat_id = m.chat.id
    if chat_id not in hide_targets:
        return bot.reply_to(m, "❌ Hidden users မရှိပါ")
    
    unhidden_count = 0
    for a in args:
        try:
            uid = int(a) if a.isdigit() else bot.get_chat(a).id
            if uid in hide_targets[chat_id]:
                hide_targets[chat_id].remove(uid)
                unhidden_count += 1
        except:
            continue
    
    if not hide_targets[chat_id]:
        del hide_targets[chat_id]
    
    bot.reply_to(m, f"👁️ {unhidden_count} user(s) ကို unhide လုပ်ပြီးပါပြီ")

@bot.message_handler(commands=['secret_monitor'])
def secret_monitor_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    chat_id = m.chat.id
    if chat_id in secret_monitoring:
        del secret_monitoring[chat_id]
        bot.reply_to(m, "🕵️ Secret monitoring OFF လုပ်ပြီးပါပြီ")
    else:
        secret_monitoring[chat_id] = True
        bot.reply_to(m, "🕵️ Secret monitoring ON လုပ်ပြီးပါပြီ (messages တွေ owner ဆီ forward ဖြစ်မယ်)")

@bot.message_handler(commands=['stop_secret'])
def stop_secret_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    
    chat_id = m.chat.id
    if chat_id in secret_monitoring:
        del secret_monitoring[chat_id]
        bot.reply_to(m, "🕵️ Secret monitoring ရပ်ပြီးပါပြီ")
    else:
        bot.reply_to(m, "❌ Secret monitoring မရှိပါ")

# ================= GROUP LIST AND LOGS =================
    
    bot.reply_to(m, text)


@bot.message_handler(commands=['show_adminId'])
def show_admin_id(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    cursor.execute("SELECT id FROM admins")
    rows = cursor.fetchall()
    if not rows:
        return bot.reply_to(m, "❌ Admin မရှိပါ")
    text = "👑 <b>Admin ID အသေးစိတ်:</b>\n"
    for uid in rows:
        try:
            user_info = bot.get_chat(uid[0])
            user_name = get_nickname(uid[0]) or user_info.first_name
            username = f"@{user_info.username}" if user_info.username else "No username"
            banned = "🚫" if is_banned_admin(uid[0]) else "✅"
            text += f"{banned} <b>{user_name}</b>\n"
            text += f"├ ID: <code>{uid[0]}</code>\n"
            text += f"├ Username: {username}\n"
            text += f"└ Mention: {mention(uid[0], user_name)}\n\n"
        except:
            text += f"❓ Unknown Admin: <code>{uid[0]}</code>\n\n"
    bot.reply_to(m, text)

@bot.message_handler(commands=['add_admin'])
def add_admin_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /add_admin id သုံးပါ")
    try:
        add_admin_db(int(args[1]))
        bot.reply_to(m, "✔️ Admin ထည့်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error")

@bot.message_handler(commands=['remove_admin'])
def remove_admin_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /remove_admin id သုံးပါ")
    try:
        admin_id = int(args[1])
        remove_admin_db(admin_id)
        bot.reply_to(m, f"✔️ Admin {admin_id} ဖယ်ရှားပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error")




@bot.message_handler(commands=['unban_admin'])
def unban_admin_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    args = m.text.split()
    if len(args) < 2:
        return bot.reply_to(m, "❌ /unban_admin id သုံးပါ")
    try:
        admin_id = int(args[1])
        unban_admin(admin_id)
        bot.reply_to(m, f"✔️ Admin {admin_id} ban ဖြုတ်ပြီးပါပြီ")
    except:
        bot.reply_to(m, "❌ Error")

@bot.message_handler(commands=['remove_adminlist'])
def remove_admin_list(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    cursor.execute("SELECT user_id FROM banned_admins")
    banned = cursor.fetchall()
    if not banned:
        return bot.reply_to(m, "❌ Banned admin မရှိပါ")
    text = "🚫 <b>Banned Admin List:</b>\n"
    for uid in banned:
        try:
            user_info = bot.get_chat(uid[0])
            text += f"• {user_info.first_name} - <code>{uid[0]}</code>\n"
        except:
            text += f"• Unknown - <code>{uid[0]}</code>\n"
    bot.reply_to(m, text)

@bot.message_handler(commands=['broadcast'])
def broadcast_cmd(m):
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    # Get ALL bot users (not just troll targets)
    all_users = get_all_bot_users()
    if not all_users:
        return bot.reply_to(m, "❌ No bot users found for broadcast")
    
    if m.reply_to_message:
        # Handle different message types
        replied_msg = m.reply_to_message
        success_count = 0
        
        for user_id in all_users:
            try:
                if replied_msg.photo:
                    bot.send_photo(user_id, replied_msg.photo[-1].file_id, caption=replied_msg.caption)
                elif replied_msg.video:
                    bot.send_video(user_id, replied_msg.video.file_id, caption=replied_msg.caption)
                elif replied_msg.document:
                    bot.send_document(user_id, replied_msg.document.file_id, caption=replied_msg.caption)
                elif replied_msg.audio:
                    bot.send_audio(user_id, replied_msg.audio.file_id, caption=replied_msg.caption)
                elif replied_msg.sticker:
                    bot.send_sticker(user_id, replied_msg.sticker.file_id)
                else:
                    bot.send_message(user_id, replied_msg.text or replied_msg.caption or "📢 Broadcast Message")
                success_count += 1
            except:
                continue
                
        bot.reply_to(m, f"📢 Broadcast completed: {success_count}/{len(all_users)} users")
    else:
        args = m.text.split(maxsplit=1)
        if len(args) < 2:
            return bot.reply_to(m, "❌ /broadcast message သုံးပါ သို့မဟုတ် media reply လုပ်ပါ")
        text = args[1]
        success_count = 0
        for user_id in all_users:
            try:
                bot.send_message(user_id, text)
                success_count += 1
            except:
                continue
        bot.reply_to(m, f"📢 Text broadcast: {success_count}/{len(all_users)} users")

    success_count = 0
    
    # Try to forward to users first, then copy content if forward fails
    for user_id in all_users:
        try:
            # Try forwarding first
            bot.forward_message(user_id, m.chat.id, replied_msg.message_id)
            success_count += 1
        except:
            # If forward fails, try sending content directly
            try:
                if replied_msg.photo:
                    bot.send_photo(user_id, replied_msg.photo[-1].file_id, caption=replied_msg.caption)
                elif replied_msg.video:
                    bot.send_video(user_id, replied_msg.video.file_id, caption=replied_msg.caption)
                elif replied_msg.document:
                    bot.send_document(user_id, replied_msg.document.file_id, caption=replied_msg.caption)
                elif replied_msg.audio:
                    bot.send_audio(user_id, replied_msg.audio.file_id, caption=replied_msg.caption)
                elif replied_msg.sticker:
                    bot.send_sticker(user_id, replied_msg.sticker.file_id)
                else:
                    bot.send_message(user_id, replied_msg.text or "📤 Uploaded Content")
                success_count += 1
            except:
                continue
    
    bot.reply_to(m, f"📤 Upload completed: {success_count}/{len(all_users)} users")

@bot.message_handler(commands=['speed'])
def speed_cmd(m):
    global speed_delay
    if not (is_owner(m.from_user.id) or (is_admin(m.from_user.id) and speed_permission_enabled)):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူး")

    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, f"⚡ Current speed: {speed_delay} sec per message")
    try:
        speed_delay = float(args[0])
        bot.reply_to(m, f"⚡ Speed set to {speed_delay} sec per message")
    except:
        bot.reply_to(m, "❌ Error")



# ================= FIGHT =================
def send_fight_message(chat_id, uid, template):
    name = get_nickname(uid) or bot.get_chat(uid).first_name
    bot.send_message(chat_id, f"{mention(uid, name)} : {template}")

@bot.message_handler(commands=['fight'])
def fight_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /fight id1 id2 ... သုံးပါ")
    chat_id = m.chat.id
    templates = [row[1] for row in list_message_templates()]
    if not templates:
        return bot.reply_to(m, "❌ Template မရှိပါ")
    
    tid = f"fight_{chat_id}"
    running_threads[tid] = True

    target_index = {}
    for target in args:
        try:
            uid = int(target) if target.isdigit() else bot.get_chat(target).id
            target_index[uid] = 0
        except:
            continue

    def fight_loop():
        while running_threads.get(tid, False):
            for uid in target_index.keys():
                try:
                    idx = target_index[uid] % len(templates)
                    template = templates[idx]
                    send_fight_message(chat_id, uid, template)
                    target_index[uid] += 1
                    time.sleep(speed_delay)
                except:
                    continue
    threading.Thread(target=fight_loop, daemon=True).start()
    bot.reply_to(m, "⚔️ စောက်တောသားတွေကိုစတင်ဆုံးမပါပြီ😈")

# ================= TROLL =================
@bot.message_handler(commands=['troll'])
def troll_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if not args:
        return bot.reply_to(m, "❌ /troll id1 id2 ... သုံးပါ")
    chat_id = m.chat.id
    troll_targets.setdefault(chat_id, {})
    for a in args:
        try:
            uid = int(a) if a.isdigit() else bot.get_chat(a).id
            if uid not in troll_targets[chat_id]:
                troll_targets[chat_id][uid] = 0
        except:
            continue
    bot.reply_to(m, "တောသားကိုစTrollပါပြီ 😈")

# ================= FUNNY =================
@bot.message_handler(commands=['funny'])
def funny_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")
    args = m.text.split()[1:]
    if len(args) < 2:
        return bot.reply_to(m, "❌ /funny id1 id2 သုံးပါ")
    try:
        id1 = int(args[0]) if args[0].isdigit() else bot.get_chat(args[0]).id
        id2 = int(args[1]) if args[1].isdigit() else bot.get_chat(args[1]).id
        funny_pairs[m.chat.id] = (id1, id2)
        bot.reply_to(m, f"တောသားနှစ်ကောင်ကိုရန်တိုက်ပါပြီ: {id1} > {id2}")
    except:
        bot.reply_to(m, "❌ Error")

# ================= STOP ALL =================
@bot.message_handler(commands=['stopall'])
def stop_all_cmd(m):
    if not is_admin(m.from_user.id):
        return bot.reply_to(m, "❌ မင်းသုံးခွင့်မရှိဘူးတောသား")

    chat_id = m.chat.id

    # Fight threads ရပ်ရန် (ဒီ Group ထဲကပဲ ရပ်မယ်)
    to_stop = [tid for tid in list(running_threads.keys()) if tid.endswith(f"_{chat_id}")]
    for tid in to_stop:
        running_threads[tid] = False
        del running_threads[tid]

    # Troll mode ရပ်ရန် (ဒီ Group ထဲ)
    troll_targets.pop(chat_id, None)

    # Funny mode ရပ်ရန် (ဒီ Group ထဲ)
    funny_pairs.pop(chat_id, None)

    # Hide targets ရှင်းရန် (ဒီ Group ထဲ)
    hide_targets.pop(chat_id, None)

    bot.reply_to(m, "⚔️ စောက်တောသားတွေကိုဆုံးမလို့ပြီးပါပြီ😈")
    
# Global speed permission flag
speed_permission_enabled = False

@bot.message_handler(commands=['speed_on'])
def speed_on_cmd(m):
    """Enable speed command for admins"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    global speed_permission_enabled
    speed_permission_enabled = True
    bot.reply_to(m, "⚡ Speed permission ကို Admin တွေအတွက် ON လုပ်လိုက်ပြီ")

@bot.message_handler(commands=['speed_off'])
def speed_off_cmd(m):
    """Disable speed command for admins"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    global speed_permission_enabled
    speed_permission_enabled = False
    bot.reply_to(m, "⚡ Speed permission ကို Admin တွေအတွက် OFF လုပ်လိုက်ပြီ")

# Global welcome settings
welcome_mode_enabled = False
welcome_text = "🎉 ကြိုဆိုပါတယ် {name}! Group ကို လာရောက်ပါရှင့်အတွက် ကျေးဇူးတင်ပါတယ်။"

@bot.message_handler(commands=['welcome'])
def welcome_cmd(m):
    """Toggle welcome mode"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    global welcome_mode_enabled
    welcome_mode_enabled = not welcome_mode_enabled
    
    status = "ON" if welcome_mode_enabled else "OFF"
    bot.reply_to(m, f"🎉 Welcome Mode ကို {status} လုပ်လိုက်ပြီ")

@bot.message_handler(commands=['welcome_mode'])
def welcome_mode_cmd(m):
    """Check welcome mode status"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    status = "ON" if welcome_mode_enabled else "OFF"
    text = f"🎉 <b>Welcome Mode Status:</b> {status}\n\n"
    text += f"📝 <b>Current Welcome Text:</b>\n{welcome_text}"
    bot.reply_to(m, text)

@bot.message_handler(commands=['welcome_text'])
def welcome_text_cmd(m):
    """Change welcome text"""
    if not is_owner(m.from_user.id):
        return bot.reply_to(m, "❌ Owner ချာလီသီးသန့်ပါ မင်းသုံးလို့မရဘူး")
    
    args = m.text.split(maxsplit=1)
    if len(args) < 2:
        return bot.reply_to(m, "❌ /welcome_text new_welcome_message သုံးပါ\n\nplaceholders: {name}, {username}")
    
    global welcome_text
    welcome_text = args[1]
    bot.reply_to(m, f"✅ Welcome text ကို ပြောင်းလိုက်ပြီ:\n{welcome_text}")

# ================= AUTO REPLY HANDLER =================
@bot.message_handler(func=lambda m: True)
def handle_auto_reply(m):
    chat_id = m.chat.id
    uid = m.from_user.id
    name = get_nickname(uid) or m.from_user.first_name
    
    # Track the chat for analytics
    track_chat(m.chat)
    
    # ---- HIDE TARGETS - DELETE MESSAGES ----
    if chat_id in hide_targets and uid in hide_targets[chat_id]:
        try:
            bot.delete_message(chat_id, m.message_id)
            return  # Stop processing this message
        except Exception as e:
            # If can't delete (no admin rights), continue with other processing
            pass
    
    # ---- SECRET MONITORING ----
    if chat_id in secret_monitoring and uid != OWNER_ID:
        try:
            # Forward message to owner
            forward_text = f"🕵️ <b>Secret Monitor</b>\n🏷️ Chat: {m.chat.title or 'Unknown'}\n👤 User: {mention(uid, name)}\n💬 Message: {m.text or 'Media/Other'}"
            bot.send_message(OWNER_ID, forward_text)
        except:
            pass
    
    # ---- LOVE TROLL MODE ----
    if chat_id in love_troll_targets and uid in love_troll_targets[chat_id]:
        love_templates = [row[1] for row in list_love_messages()]
        if love_templates:
            idx = love_troll_targets[chat_id][uid] % len(love_templates)
            template = love_templates[idx]
            bot.reply_to(m, f"{mention(uid, name)} 💕 {template} ချစ်တယ်နော် 😘")
            love_troll_targets[chat_id][uid] += 1
    
    # ---- LOVE FUNNY MODE ----
    if chat_id in love_funny_pairs:
        id1, id2 = love_funny_pairs[chat_id]
        if uid == id1 or uid == id2:
            other_id = id2 if uid == id1 else id1
            try:
                other_name = get_nickname(other_id) or bot.get_chat(other_id).first_name
                love_messages = [
                    f"{mention(uid, name)} က {mention(other_id, other_name)} ကို '{m.text}' လို့ချစ်စကားပြောနေတယ် 💖",
                    f"{mention(other_id, other_name)} ရေ... {mention(uid, name)} က မင်းကိုချစ်တဲ့အကြောင်း '{m.text}' လို့ပြောနေတယ်နော် 💝",
                    f"အချစ်သံတွဲလေး {mention(uid, name)} နဲ့ {mention(other_id, other_name)} တို့ရဲ့ ချစ်ခြင်းမေတ္တာက '{m.text}' 💕"
                ]
                bot.reply_to(m, love_messages[love_funny_pairs[chat_id][0] % len(love_messages)])
            except:
                pass

    # ---- TROLL MODE ----
    if chat_id in troll_targets and uid in troll_targets[chat_id]:
        templates = [row[1] for row in list_message_templates()]
        if templates:
            idx = troll_targets[chat_id][uid] % len(templates)
            template = templates[idx]
            bot.reply_to(m, f"{mention(uid, name)} : {template}")
            troll_targets[chat_id][uid] += 1

    # ---- FUNNY MODE ----
    if chat_id in funny_pairs:
        id1, id2 = funny_pairs[chat_id]
        if uid == id1 or uid == id2:
            other_id = id2 if uid == id1 else id1
            try:
                other_name = get_nickname(other_id) or bot.get_chat(other_id).first_name
                bot.reply_to(m, f"{mention(uid, name)} ဒီစောက်တောသားက {mention(other_id, other_name)} မင်းကို '{m.text}' လို့ပြောနေတယ် ငြိမ်ခံမနေနဲ့ ပြန်ပြောလေမအေလိုးတောသား😈")
            except:
                pass

# ================= RUN BOT =================
if __name__ == "__main__":
    print("🤖 Bot is running...")
    print(f"👑 Owner ID: {OWNER_ID}")
    print("🔧 All features loaded successfully!")
    bot.infinity_polling()