import os
import psycopg2
from psycopg2 import pool
import time
import threading
import telebot
from telebot import types

# ============================================================
# CONFIG
# ============================================================
API_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
DATABASE_URL = os.getenv("DATABASE_URL")

if API_TOKEN == "YOUR_BOT_TOKEN_HERE":
    print("WARNING: Set BOT_TOKEN before starting the bot.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add a Railway PostgreSQL service and connect its DATABASE_URL variable.")

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

# ============================================================
# DATABASE
# ============================================================
DB_LOCK = threading.Lock()

def db_connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=30)

def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Small PostgreSQL helper kept separate so existing bot features stay unchanged."""
    with DB_LOCK:
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            result = cur.fetchone() if fetchone else (cur.fetchall() if fetchall else None)
            if commit:
                conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def init_db():
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'active'
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id TEXT UNIQUE,
                title TEXT,
                link TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS live_map (
                admin_message_id INTEGER PRIMARY KEY,
                user_id INTEGER
            )
        """)

        defaults = [
            ("fj_text", "नमस्ते <b>{first_name}</b>\n\nसभी चैनल जोड़ें!"),
            ("fj_media_id", "NONE"),
            ("fj_media_type", "NONE"),
            ("fj_verify_btn", "#g जाँच करें / Try Again"),

            ("start_text", "👋 स्वागत है <b>{first_name}</b>!"),
            ("start_media_id", "NONE"),
            ("start_media_type", "NONE"),
            ("start_buttons", "#p सहायता | https://t.me/\n#g मुख्य चैनल | https://t.me/"),
            ("start_pin", "NO"),

            ("bcast_text", "📢 <b>नई सूचना!</b>"),
            ("bcast_media_id", "NONE"),
            ("bcast_media_type", "NONE"),
            ("bcast_buttons", "#l विशेष ऑफर | https://t.me/"),
            ("bcast_pin", "NO")
        ]

        for k, v in defaults:
            c.execute(
                "INSERT INTO settings (key, val) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )

        conn.commit()
        conn.close()

init_db()

def get_set(key):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT val FROM settings WHERE key=%s", (key,))
        row = c.fetchone()
        conn.close()
    return row[0] if row else "NONE"

def set_val(key, val):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO settings (key, val) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET val=EXCLUDED.val",
            (key, str(val))
        )
        conn.commit()
        conn.close()

def add_user(user_id):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (user_id, status) VALUES (%s, 'active') ON CONFLICT (user_id) DO NOTHING",
            (user_id,)
        )
        c.execute(
            "UPDATE users SET status='active' WHERE user_id=%s",
            (user_id,)
        )
        conn.commit()
        conn.close()

def get_channels():
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT id, channel_id, title, link FROM channels ORDER BY id")
        rows = c.fetchall()
        conn.close()
    return rows

# ============================================================
# HELPERS
# ============================================================
def get_button_style_and_text(text):
    """
    Parse native Telegram button styles.

    #r = red (danger)
    #g = green (success)
    #b = blue (primary)

    #l and #p are kept for compatibility and map to blue,
    because Telegram currently exposes only red, green and blue
    button backgrounds through the Bot API.
    """
    if not text:
        return "", None

    raw = text.strip()
    style_map = {
        "#r": "danger",
        "#g": "success",
        "#b": "primary",
        "#l": "primary",
        "#p": "primary",
    }

    lower = raw.lower()
    for code, style in style_map.items():
        if lower.startswith(code):
            return raw[len(code):].strip(), style

    return raw, None

def parse_colors(text):
    """Backward-compatible helper: remove a color prefix from text."""
    clean_text, _ = get_button_style_and_text(text)
    return clean_text

def make_url_button(title, url):
    """Create a native Telegram styled inline URL button."""
    clean_title, style = get_button_style_and_text(title)
    kwargs = {
        "text": clean_title,
        "url": url.strip(),
    }
    if style:
        kwargs["style"] = style
    return types.InlineKeyboardButton(**kwargs)

def make_callback_button(title, callback_data, default_style=None):
    """Create a callback button; a #r/#g/#b prefix controls its color."""
    clean_title, style = get_button_style_and_text(title)
    kwargs = {
        "text": clean_title,
        "callback_data": callback_data,
    }
    if style or default_style:
        kwargs["style"] = style or default_style
    return types.InlineKeyboardButton(**kwargs)

def build_keyboard_from_string(btn_str):
    """
    Build configurable URL buttons from one button per line:

        #g 🟢 GREEN BUTTON | https://example.com
        #b 🔵 BLUE BUTTON  | https://example.com
        #r 🔴 RED BUTTON   | https://example.com

    The #r/#g/#b prefix changes the actual Telegram button background;
    it is not shown in the final button text.
    """
    markup = types.InlineKeyboardMarkup(row_width=2)

    if not btn_str or btn_str == "NONE":
        return markup

    for line in btn_str.strip().splitlines():
        if "|" not in line:
            continue

        title, url = line.split("|", 1)
        title = title.strip()
        url = url.strip()

        if title and url:
            markup.add(make_url_button(title, url))

    return markup

def is_admin(user_id):
    return user_id == ADMIN_ID

def get_unjoined_channels(user_id):
    channels = get_channels()
    unjoined = []

    for ch in channels:
        try:
            member = bot.get_chat_member(chat_id=ch[1], user_id=user_id)

            # These statuses count as joined.
            joined_statuses = ("member", "administrator", "creator")

            if member.status not in joined_statuses:
                unjoined.append(ch)

        except Exception:
            # If bot cannot check the channel, keep it in the
            # force-join list instead of incorrectly approving.
            unjoined.append(ch)

    return unjoined

# ============================================================
# USER PAGES
# ============================================================
def send_force_join_page(chat_id, user):
    unjoined = get_unjoined_channels(user.id)

    txt = get_set("fj_text").replace(
        "{first_name}", user.first_name or "User"
    )

    m_id = get_set("fj_media_id")
    m_type = get_set("fj_media_type")

    verify_txt = get_set("fj_verify_btn")

    markup = types.InlineKeyboardMarkup(row_width=2)

    btns = []
    for ch in unjoined:
        title = ch[2]
        link = ch[3]

        if title and link:
            btns.append(
                make_url_button(title, link)
            )

    if btns:
        markup.add(*btns)

    verify_title, verify_style = get_button_style_and_text(verify_txt)
    verify_kwargs = {
        "text": verify_title,
        "callback_data": "verify_force_join",
        "style": verify_style or "primary",
    }
    markup.add(types.InlineKeyboardButton(**verify_kwargs))

    try:
        if m_type == "photo" and m_id != "NONE":
            bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        elif m_type == "video" and m_id != "NONE":
            bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        else:
            bot.send_message(
                chat_id,
                txt,
                reply_markup=markup
            )

    except Exception:
        bot.send_message(
            chat_id,
            txt,
            reply_markup=markup
        )

def send_start_page(chat_id, user):
    txt = get_set("start_text").replace(
        "{first_name}", user.first_name or "User"
    )

    m_id = get_set("start_media_id")
    m_type = get_set("start_media_type")

    markup = build_keyboard_from_string(
        get_set("start_buttons")
    )

    try:
        if m_type == "photo" and m_id != "NONE":
            msg = bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        elif m_type == "video" and m_id != "NONE":
            msg = bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        else:
            msg = bot.send_message(
                chat_id,
                txt,
                reply_markup=markup
            )

        if get_set("start_pin") == "YES":
            try:
                bot.pin_chat_message(
                    chat_id,
                    msg.message_id
                )
            except Exception:
                pass

    except Exception as e:
        bot.send_message(
            chat_id,
            "❌ Start page error: " + str(e)
        )

# ============================================================
# START / ADMIN COMMANDS
# ============================================================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id
    add_user(user_id)

    # STRICT FLOW:
    # 1) Check Force Join FIRST.
    # 2) If even one configured channel is not joined, NEVER show welcome.
    # 3) Welcome/Start page is shown only after successful verification.
    unjoined = get_unjoined_channels(user_id)

    if len(unjoined) > 0:
        send_force_join_page(
            message.chat.id,
            message.from_user
        )
        return

    # Only reached when all configured channels are joined.
    send_start_page(
        message.chat.id,
        message.from_user
    )

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if is_admin(message.from_user.id):
        show_admin_main(message.chat.id)

def show_admin_main(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "⚙️ स्टार्ट",
            callback_data="menu_start"
        ),
        types.InlineKeyboardButton(
            "🔐 फ़ोर्स ज्वाइन",
            callback_data="menu_fj"
        ),
        types.InlineKeyboardButton(
            "📢 प्रसारण",
            callback_data="menu_bc"
        ),
        types.InlineKeyboardButton(
            "📺 चैनल",
            callback_data="menu_channels"
        ),
        types.InlineKeyboardButton(
            "📊 आंकड़े",
            callback_data="menu_stats"
        )
    )

    bot.send_message(
        chat_id,
        "👑 <b>एडमिन कंट्रोल पैनल</b>",
        reply_markup=markup
    )

# ============================================================
# ADMIN KEYBOARDS
# ============================================================
def get_modular_keyboard(prefix, is_bcast=False):
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "🖼️ Media",
            callback_data=prefix + "_edit_media"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_media"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "abc Text",
            callback_data=prefix + "_edit_text"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_text"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🔘 Buttons",
            callback_data=prefix + "_edit_btn"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_btn"
        )
    )

    pin_key = "bcast_pin" if is_bcast else "start_pin"
    pin_status = (
        "✅ YES"
        if get_set(pin_key) == "YES"
        else "❌ NO"
    )

    markup.add(
        types.InlineKeyboardButton(
            "📌 Pin",
            callback_data=prefix + "_toggle_pin"
        ),
        types.InlineKeyboardButton(
            pin_status,
            callback_data=prefix + "_toggle_pin"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "👀 Full preview",
            callback_data=prefix + "_full_prev"
        )
    )

    if is_bcast:
        markup.add(
            types.InlineKeyboardButton(
                "⬅️ Back",
                callback_data="adm_home"
            ),
            types.InlineKeyboardButton(
                "Next ➡️",
                callback_data="bc_next_send"
            )
        )
    else:
        markup.add(
            types.InlineKeyboardButton(
                "🏠 Menu",
                callback_data="adm_home"
            ),
            types.InlineKeyboardButton(
                "⬅️ Back",
                callback_data="adm_home"
            )
        )

    return markup

def get_fj_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "🖼️ Media",
            callback_data="fj_edit_media"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_media"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "📝 Text",
            callback_data="fj_edit_text"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_text"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🔘 Verify Button",
            callback_data="fj_edit_btn"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_btn"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "👀 Full preview",
            callback_data="fj_full_prev"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🏠 Menu",
            callback_data="adm_home"
        ),
        types.InlineKeyboardButton(
            "⬅️ Back",
            callback_data="adm_home"
        )
    )

    return markup

# ============================================================
# CALLBACKS
# ============================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    # --------------------------------------------------------
    # Force Join Verification
    # --------------------------------------------------------
    if call.data == "verify_force_join":
        unjoined = get_unjoined_channels(user_id)

        if not unjoined:
            bot.answer_callback_query(
                call.id,
                "✅ सभी चैनल ज्वाइन हैं! Welcome!",
                show_alert=True
            )

            try:
                bot.delete_message(
                    chat_id,
                    call.message.message_id
                )
            except Exception:
                pass

            # Welcome is allowed ONLY after the fresh Force-Join check above.
            send_start_page(
                chat_id,
                call.from_user
            )

        else:
            bot.answer_callback_query(
                call.id,
                "❌ सभी चैनल ज्वाइन करें!",
                show_alert=True
            )

            send_force_join_page(
                chat_id,
                call.from_user
            )

        return

    # Everything below is admin-only.
    if not is_admin(user_id):
        bot.answer_callback_query(
            call.id,
            "❌ Admin only"
        )
        return

    # --------------------------------------------------------
    # Admin Home
    # --------------------------------------------------------
    if call.data == "adm_home":
        bot.answer_callback_query(call.id)
        show_admin_main(chat_id)

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------
    elif call.data == "menu_stats":
        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM users")
            tot = c.fetchone()[0]

            c.execute(
                "SELECT COUNT(*) FROM users WHERE status='active'"
            )
            act = c.fetchone()[0]

            c.execute(
                "SELECT COUNT(*) FROM users WHERE status='blocked'"
            )
            blk = c.fetchone()[0]

            conn.close()

        txt = (
            "📊 <b>बोट आंकड़े:</b>\n\n"
            f"👥 कुल यूज़र्स: {tot}\n"
            f"🟢 सक्रिय: {act}\n"
            f"🔴 ब्लॉक: {blk}"
        )

        bot.send_message(chat_id, txt)

    # --------------------------------------------------------
    # START SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_start":
        bot.send_message(
            chat_id,
            "⚙️ <b>Start Settings</b>",
            reply_markup=get_modular_keyboard("st")
        )

    elif call.data == "st_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ फोटो/वीडियो भेजें (या NONE लिखें):"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "start"
        )

    elif call.data == "st_see_media":
        bot.send_message(
            chat_id,
            "🖼️ Media Type: " +
            str(get_set("start_media_type"))
        )

    elif call.data == "st_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ Start Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_start_text
        )

    elif call.data == "st_see_text":
        bot.send_message(
            chat_id,
            "📝 टेक्स्ट:\n\n" +
            str(get_set("start_text"))
        )

    elif call.data == "st_edit_btn":
        guide = (
            "🔘 बटन लिखें (हर बटन नई लाइन में):\n\n"
            "<code>#r Join | https://t.me/example</code>\n"
            "<code>#g Main Channel | https://t.me/example</code>"
        )

        m = bot.send_message(
            chat_id,
            guide
        )

        bot.register_next_step_handler(
            m,
            save_start_buttons
        )

    elif call.data == "st_see_btn":
        bot.send_message(
            chat_id,
            "🔘 बटन:\n\n" +
            str(get_set("start_buttons"))
        )

    elif call.data == "st_toggle_pin":
        cur = get_set("start_pin")
        set_val(
            "start_pin",
            "NO" if cur == "YES" else "YES"
        )

        bot.send_message(
            chat_id,
            "📌 पिन: " +
            str(get_set("start_pin"))
        )

    elif call.data == "st_full_prev":
        send_start_page(
            chat_id,
            call.from_user
        )

    # --------------------------------------------------------
    # FORCE JOIN SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_fj":
        bot.send_message(
            chat_id,
            "🔐 <b>Force Join Settings</b>",
            reply_markup=get_fj_keyboard()
        )

    elif call.data == "fj_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ FJ मीडिया भेजें:"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "fj"
        )

    elif call.data == "fj_see_media":
        bot.send_message(
            chat_id,
            "🖼️ FJ Media Type: " +
            str(get_set("fj_media_type"))
        )

    elif call.data == "fj_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ FJ Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_fj_text
        )

    elif call.data == "fj_see_text":
        bot.send_message(
            chat_id,
            "📝 FJ Text:\n\n" +
            str(get_set("fj_text"))
        )

    elif call.data == "fj_edit_btn":
        guide = (
            "🔘 Verify Button नाम लिखें:\n"
            "<code>#g जाँच करें / Try Again</code>"
        )

        m = bot.send_message(
            chat_id,
            guide
        )

        bot.register_next_step_handler(
            m,
            save_fj_button
        )

    elif call.data == "fj_see_btn":
        bot.send_message(
            chat_id,
            "🔘 Verify Button:\n" +
            str(get_set("fj_verify_btn"))
        )

    elif call.data == "fj_full_prev":
        send_force_join_page(
            chat_id,
            call.from_user
        )

    # --------------------------------------------------------
    # BROADCAST SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_bc":
        bot.send_message(
            chat_id,
            "📢 <b>Broadcast Settings</b>",
            reply_markup=get_modular_keyboard(
                "bc",
                is_bcast=True
            )
        )

    elif call.data == "bc_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ प्रसारण मीडिया भेजें:"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "bcast"
        )

    elif call.data == "bc_see_media":
        bot.send_message(
            chat_id,
            "🖼️ Broadcast Media Type: " +
            str(get_set("bcast_media_type"))
        )

    elif call.data == "bc_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ Broadcast Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_bcast_text
        )

    elif call.data == "bc_see_text":
        bot.send_message(
            chat_id,
            "📝 Broadcast Text:\n\n" +
            str(get_set("bcast_text"))
        )

    elif call.data == "bc_edit_btn":
        m = bot.send_message(
            chat_id,
            "🔘 Broadcast Buttons लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_bcast_buttons
        )

    elif call.data == "bc_see_btn":
        bot.send_message(
            chat_id,
            "🔘 Broadcast Buttons:\n\n" +
            str(get_set("bcast_buttons"))
        )

    elif call.data == "bc_toggle_pin":
        cur = get_set("bcast_pin")
        set_val(
            "bcast_pin",
            "NO" if cur == "YES" else "YES"
        )

        bot.send_message(
            chat_id,
            "📌 पिन: " +
            str(get_set("bcast_pin"))
        )

    elif call.data == "bc_full_prev":
        send_broadcast_preview(chat_id)

    elif call.data == "bc_next_send":
        # Run broadcast in a separate thread so the bot
        # continues receiving updates.
        bot.answer_callback_query(
            call.id,
            "🚀 Broadcast शुरू हो रहा है..."
        )

        threading.Thread(
            target=execute_broadcast,
            args=(chat_id,),
            daemon=True
        ).start()

    # --------------------------------------------------------
    # CHANNELS
    # --------------------------------------------------------
    elif call.data == "menu_channels":
        show_channels(chat_id)

    elif call.data == "add_ch_prompt":
        msg = (
            "➕ चैनल इस प्रारूप में भेजें:\n\n"
            "<code>-1001234567890 | #g चैनल नाम | https://t.me/example</code>\n\n"
            "Bot को उस channel का admin बनाना जरूरी है।"
        )

        m = bot.send_message(chat_id, msg)

        bot.register_next_step_handler(
            m,
            process_add_ch
        )

    elif call.data.startswith("del_ch_"):
        ch_db_id = call.data.replace(
            "del_ch_", ""
        )

        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()
            c.execute(
                "DELETE FROM channels WHERE id=%s",
                (ch_db_id,)
            )
            conn.commit()
            conn.close()

        bot.send_message(
            chat_id,
            "✅ चैनल हटा दिया गया!"
        )

        show_channels(chat_id)

# ============================================================
# ADMIN INPUT SAVE FUNCTIONS
# ============================================================
def save_media(message, prefix):
    if message.photo:
        set_val(
            prefix + "_media_id",
            message.photo[-1].file_id
        )
        set_val(
            prefix + "_media_type",
            "photo"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ फोटो सुरक्षित!"
        )

    elif message.video:
        set_val(
            prefix + "_media_id",
            message.video.file_id
        )
        set_val(
            prefix + "_media_type",
            "video"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ वीडियो सुरक्षित!"
        )

    elif message.text and message.text.strip().upper() == "NONE":
        set_val(
            prefix + "_media_id",
            "NONE"
        )
        set_val(
            prefix + "_media_type",
            "NONE"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ मीडिया हटा दिया गया!"
        )

    else:
        bot.send_message(
            ADMIN_ID,
            "❌ कृपया photo, video या NONE भेजें।"
        )

def save_start_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    set_val("start_text", message.text)
    bot.send_message(
        ADMIN_ID,
        "✅ Start Text अपडेट!"
    )

def save_fj_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    set_val("fj_text", message.text)
    bot.send_message(
        ADMIN_ID,
        "✅ Force Join Text अपडेट!"
    )

def save_fj_button(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    set_val(
        "fj_verify_btn",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Verify Button अपडेट!"
    )

def save_start_buttons(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ Buttons text भेजें।"
        )
        return

    set_val(
        "start_buttons",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Start Buttons अपडेट!"
    )

def save_bcast_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    set_val(
        "bcast_text",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Broadcast Text अपडेट!"
    )

def save_bcast_buttons(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ Buttons text भेजें।"
        )
        return

    set_val(
        "bcast_buttons",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Broadcast Buttons अपडेट!"
    )

# ============================================================
# CHANNEL MANAGEMENT
# ============================================================
def process_add_ch(message):
    try:
        if not message.text:
            raise ValueError()

        parts = [
            x.strip()
            for x in message.text.split("|")
        ]

        if len(parts) != 3:
            raise ValueError()

        cid, title, link = parts

        if not cid or not title or not link:
            raise ValueError()

        # Validate that Telegram can access the channel.
        try:
            bot.get_chat(cid)
        except Exception:
            bot.send_message(
                ADMIN_ID,
                "⚠️ Channel ID check नहीं हो सका। "
                "सुनिश्चित करें कि Bot channel में admin है।"
            )

        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()

            c.execute(
                """
                INSERT INTO channels
                (channel_id, title, link)
                VALUES (%s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                SET title=EXCLUDED.title, link=EXCLUDED.link
                """,
                (cid, title, link)
            )

            conn.commit()
            conn.close()

        bot.send_message(
            ADMIN_ID,
            "✅ चैनल जुड़ गया: " +
            parse_colors(title)
        )

        show_channels(ADMIN_ID)

    except Exception:
        bot.send_message(
            ADMIN_ID,
            "❌ गलत फॉर्मेट!\n\n"
            "सही:\n"
            "<code>ID | Title | Link</code>"
        )

def show_channels(chat_id):
    channels = get_channels()

    txt = "📢 <b>चैनल सूची:</b>\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)

    if channels:
        for ch in channels:
            title = parse_colors(ch[2])

            txt += (
                f"• <b>{title}</b>\n"
                f"ID: <code>{ch[1]}</code>\n"
                f"🔗 {ch[3]}\n\n"
            )

            markup.add(
                types.InlineKeyboardButton(
                    "❌ हटाएँ " + title,
                    callback_data="del_ch_" + str(ch[0])
                )
            )

    else:
        txt += "कोई चैनल नहीं है।"

    markup.add(
        types.InlineKeyboardButton(
            "➕ नया चैनल जोड़ें",
            callback_data="add_ch_prompt"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🏠 Menu",
            callback_data="adm_home"
        )
    )

    bot.send_message(
        chat_id,
        txt,
        reply_markup=markup
    )

# ============================================================
# BROADCAST
# ============================================================
def send_broadcast_preview(chat_id):
    txt = get_set("bcast_text")
    m_id = get_set("bcast_media_id")
    m_type = get_set("bcast_media_type")

    markup = build_keyboard_from_string(
        get_set("bcast_buttons")
    )

    try:
        if m_type == "photo" and m_id != "NONE":
            bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        elif m_type == "video" and m_id != "NONE":
            bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup
            )

        else:
            bot.send_message(
                chat_id,
                txt,
                reply_markup=markup
            )

    except Exception as e:
        bot.send_message(
            chat_id,
            "❌ Preview error: " + str(e)
        )

def execute_broadcast(admin_chat_id):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        users = [r[0] for r in c.fetchall()]
        conn.close()

    txt = get_set("bcast_text")
    m_id = get_set("bcast_media_id")
    m_type = get_set("bcast_media_type")

    markup = build_keyboard_from_string(
        get_set("bcast_buttons")
    )

    do_pin = get_set("bcast_pin")

    succ = 0
    fail = 0
    start_t = time.time()

    bot.send_message(
        admin_chat_id,
        "🚀 <b>प्रसारण शुरू...</b>\n"
        f"कुल यूज़र्स: {len(users)}"
    )

    for u in users:
        try:
            if m_type == "photo" and m_id != "NONE":
                m = bot.send_photo(
                    u,
                    m_id,
                    caption=txt,
                    reply_markup=markup
                )

            elif m_type == "video" and m_id != "NONE":
                m = bot.send_video(
                    u,
                    m_id,
                    caption=txt,
                    reply_markup=markup
                )

            else:
                m = bot.send_message(
                    u,
                    txt,
                    reply_markup=markup
                )

            if do_pin == "YES":
                try:
                    bot.pin_chat_message(
                        u,
                        m.message_id
                    )
                except Exception:
                    pass

            with DB_LOCK:
                conn = db_connect()
                conn.execute(
                    "UPDATE users SET status='active' WHERE user_id=%s",
                    (u,)
                )
                conn.commit()
                conn.close()

            succ += 1

        except Exception:
            fail += 1

            with DB_LOCK:
                conn = db_connect()
                conn.execute(
                    "UPDATE users SET status='blocked' WHERE user_id=%s",
                    (u,)
                )
                conn.commit()
                conn.close()

        # Telegram flood-control safety.
        time.sleep(0.05)

    elapsed = round(
        time.time() - start_t,
        2
    )

    rep = (
        "📢 <b>प्रसारण पूरा!</b>\n\n"
        f"🟢 सफल: {succ}\n"
        f"🔴 विफल: {fail}\n"
        f"⏱️ समय: {elapsed} सेकंड"
    )

    bot.send_message(
        admin_chat_id,
        rep
    )

# ============================================================
# LIVE USER CHAT
# ============================================================
def check_user_msg(message):
    if message.chat.type != "private":
        return False

    if is_admin(message.from_user.id):
        return False

    if message.text and message.text.startswith("/"):
        return False

    return True

def send_temporary_success(chat_id):
    """Show a temporary success message and remove it after 3 seconds."""
    try:
        sent = bot.send_message(
            chat_id,
            "✉️ <b>MESSAGE SENT SUCCESSFULLY</b>"
        )

        def delete_after_3_seconds():
            try:
                bot.delete_message(chat_id, sent.message_id)
            except Exception:
                pass

        threading.Timer(3.0, delete_after_3_seconds).start()
    except Exception:
        pass


def copy_user_message_to_admin(message):
    """Copy user content to admin, save reply mapping, then confirm success to user."""
    try:
        # Copy keeps the message clean and avoids showing
        # the Telegram "Forwarded from" header.
        # Add a direct profile button so admin can open the user's
        # Telegram profile from the received message.
        profile_markup = types.InlineKeyboardMarkup(row_width=1)
        profile_markup.add(
            types.InlineKeyboardButton(
                "👤 Open User Profile",
                url=f"tg://user?id={message.from_user.id}"
            )
        )

        admin_msg = bot.copy_message(
            ADMIN_ID,
            message.chat.id,
            message.message_id,
            reply_markup=profile_markup
        )

        with DB_LOCK:
            conn = db_connect()
            conn.execute(
                """
                INSERT INTO live_map
                (admin_message_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (admin_message_id) DO UPDATE
                SET user_id=EXCLUDED.user_id
                """,
                (admin_msg.message_id, message.from_user.id)
            )
            conn.commit()
            conn.close()

        info = (
            f"👤 <b>User:</b> "
            f"{message.from_user.first_name or 'User'}\n"
            f"🆔 <code>{message.from_user.id}</code>"
        )

        bot.send_message(
            ADMIN_ID,
            info,
            reply_to_message_id=admin_msg.message_id
        )

        # Only show success after the message was actually copied to the admin.
        send_temporary_success(message.chat.id)

    except Exception:
        try:
            bot.send_message(
                message.chat.id,
                "❌ <b>MESSAGE COULD NOT BE SENT</b>\nPlease try again."
            )
        except Exception:
            pass

        try:
            bot.send_message(
                ADMIN_ID,
                "📩 User message received.\n"
                f"User ID: <code>{message.from_user.id}</code>"
            )
        except Exception:
            pass

@bot.message_handler(
    func=check_user_msg,
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "sticker",
        "voice",
        "audio",
        "animation",
        "contact",
        "location"
    ]
)
def user_live_chat(message):
    add_user(message.from_user.id)
    copy_user_message_to_admin(message)

# ============================================================
# ADMIN LIVE CHAT REPLY
# ============================================================
@bot.message_handler(
    func=lambda m: (
        m.chat.type == "private"
        and is_admin(m.from_user.id)
        and m.reply_to_message is not None
    ),
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "sticker",
        "voice",
        "audio",
        "animation",
        "contact",
        "location"
    ]
)
def admin_reply_to_user(message):
    replied_id = message.reply_to_message.message_id

    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute(
            "SELECT user_id FROM live_map WHERE admin_message_id=%s",
            (replied_id,)
        )
        row = c.fetchone()
        conn.close()

    if not row:
        bot.send_message(
            ADMIN_ID,
            "❌ इस reply का user mapping नहीं मिला।"
        )
        return

    user_id = row[0]

    try:
        bot.copy_message(
            user_id,
            ADMIN_ID,
            message.message_id
        )

        bot.send_message(
            ADMIN_ID,
            "✅ Reply user को भेज दिया गया।"
        )

    except Exception as e:
        bot.send_message(
            ADMIN_ID,
            "❌ User को reply नहीं भेज सका:\n" +
            str(e)
        )

# ============================================================
# ERROR / POLLING
# ============================================================
def run_bot():
    print("========================================")
    print("Telegram bot is starting...")
    print("========================================")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )

        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)

if __name__ == "__main__":
    run_bot()
