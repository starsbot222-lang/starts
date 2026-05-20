import asyncio
import logging
import os
import sqlite3
from datetime import datetime
import pytz

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ===================== SOZLAMALAR =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6102256074"))
DB_PATH = "/tmp/bot.db"
TIMEZONE = pytz.timezone("Asia/Tashkent")
# ======================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ===================== VAQT TEKSHIRISH =====================
def is_working_hours():
    now = datetime.now(TIMEZONE)
    return 20 <= now.hour < 24


# ===================== DATABASE =====================
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            full_name      TEXT,
            balance        REAL DEFAULT 0,
            referred_by    INTEGER DEFAULT NULL,
            referral_count INTEGER DEFAULT 0,
            joined_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id   TEXT UNIQUE,
            channel_name TEXT,
            channel_link TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            amount      REAL,
            type        TEXT,
            description TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            username   TEXT,
            full_name  TEXT,
            gift_name  TEXT,
            gift_emoji TEXT,
            gift_stars INTEGER,
            status     TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("INSERT OR IGNORE INTO settings VALUES ('referral_stars', '0.25')")
    c.execute("INSERT OR IGNORE INTO settings VALUES ('subscribe_stars', '0.10')")
    conn.commit()
    conn.close()


def get_setting(key):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def set_setting(key, value):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def add_user(user_id, username, full_name, referred_by=None):
    conn = db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
        (user_id, username, full_name, referred_by)
    )
    conn.commit()
    conn.close()


def add_balance(user_id, amount, desc=""):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    c.execute(
        "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
        (user_id, amount, "credit", desc)
    )
    conn.commit()
    conn.close()


def deduct_balance(user_id, amount, desc=""):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    c.execute(
        "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
        (user_id, amount, "debit", desc)
    )
    conn.commit()
    conn.close()


def get_balance(user_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return round(row[0], 2) if row else 0


def get_channels():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM channels")
    rows = c.fetchall()
    conn.close()
    return rows


def add_channel(channel_id, name, link):
    conn = db()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO channels (channel_id, channel_name, channel_link) VALUES (?,?,?)",
        (channel_id, name, link)
    )
    conn.commit()
    conn.close()


def remove_channel(channel_id):
    conn = db()
    c = conn.cursor()
    c.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()


def get_stats():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("SELECT SUM(balance) FROM users")
    total_balance = c.fetchone()[0] or 0
    conn.close()
    return total, round(total_balance, 2)


def get_all_users():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_order(user_id, username, full_name, gift_name, gift_emoji, gift_stars):
    conn = db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO orders (user_id, username, full_name, gift_name, gift_emoji, gift_stars) VALUES (?,?,?,?,?,?)",
        (user_id, username, full_name, gift_name, gift_emoji, gift_stars)
    )
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id


def complete_order(order_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE orders SET status='done' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()


def get_pending_orders():
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created_at ASC")
    rows = c.fetchall()
    conn.close()
    return rows


# ===================== GIFTS =====================
GIFTS = [
    {"emoji": "💝", "name": "Heart",   "stars": 15},
    {"emoji": "🧸", "name": "Bear",    "stars": 15},
    {"emoji": "🎁", "name": "Present", "stars": 25},
    {"emoji": "🌹", "name": "Rose",    "stars": 25},
    {"emoji": "🎂", "name": "Cake",    "stars": 50},
    {"emoji": "💐", "name": "Bouquet", "stars": 50},
    {"emoji": "🚀", "name": "Rocket",  "stars": 50},
    {"emoji": "🏆", "name": "Trophy",  "stars": 100},
    {"emoji": "💍", "name": "Ring",    "stars": 100},
    {"emoji": "💎", "name": "Diamond", "stars": 100},
]


# ===================== STATES =====================
class AdminStates(StatesGroup):
    add_channel         = State()
    set_referral_stars  = State()
    set_subscribe_stars = State()
    broadcast           = State()
    add_balance_input   = State()
    deduct_balance_input = State()


# ===================== BOT & ROUTER =====================
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())
router = Router()


# ===================== KEYBOARDS =====================
def main_menu(user_id):
    buttons = [
        [
            InlineKeyboardButton(text="⭐ Balansim",      callback_data="balance"),
            InlineKeyboardButton(text="👥 Referral",      callback_data="referral"),
        ],
        [InlineKeyboardButton(text="🎁 Gift olish",       callback_data="buy_gift")],
        [InlineKeyboardButton(text="📢 Kanallarga obuna", callback_data="channels")],
        [InlineKeyboardButton(text="📋 Transaksiyalar",   callback_data="transactions")],
        [InlineKeyboardButton(text="⏰ Ish vaqti",        callback_data="work_hours")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🔧 Admin panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qo'shish",       callback_data="admin_add_channel")],
        [InlineKeyboardButton(text="➖ Kanal o'chirish",      callback_data="admin_remove_channel")],
        [InlineKeyboardButton(text="⭐ Referral stars sozla", callback_data="admin_set_referral")],
        [InlineKeyboardButton(text="⭐ Obuna stars sozla",    callback_data="admin_set_subscribe")],
        [InlineKeyboardButton(text="💰 Balans qo'shish",      callback_data="admin_add_balance")],
        [InlineKeyboardButton(text="💸 Balans ayirish",       callback_data="admin_deduct_balance")],
        [InlineKeyboardButton(text="📦 Buyurtmalar",          callback_data="admin_orders")],
        [InlineKeyboardButton(text="📊 Statistika",           callback_data="admin_stats")],
        [InlineKeyboardButton(text="📣 Xabar yuborish",       callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar",     callback_data="admin_users")],
        [InlineKeyboardButton(text="🔙 Ortga",                callback_data="back_main")],
    ])


def gifts_keyboard(user_balance):
    buttons = []
    row = []
    for i, gift in enumerate(GIFTS):
        mark = "✅" if user_balance >= gift["stars"] else "❌"
        btn = InlineKeyboardButton(
            text=f"{gift['emoji']} {gift['stars']}⭐ {mark}",
            callback_data=f"buyg_{i}"
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def channels_keyboard():
    channels = get_channels()
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(text=f"📢 {ch[2]}", url=ch[3])])
    buttons.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_kb(cb="back_main"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Ortga", callback_data=cb)]
    ])


# ===================== HELPERS =====================
async def check_subscription(user_id):
    channels = get_channels()
    if not channels:
        return True, []
    not_subbed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch[1], user_id)
            if member.status in ["left", "kicked", "banned"]:
                not_subbed.append(ch)
        except Exception as e:
            logger.warning(f"Kanal tekshirishda xato {ch[1]}: {e}")
    return len(not_subbed) == 0, not_subbed


# ===================== HANDLERS =====================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id   = message.from_user.id
    username  = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    args      = message.text.split()
    referred_by = None
    is_new    = get_user(user_id) is None

    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id and get_user(ref_id):
                referred_by = ref_id
        except Exception:
            pass

    add_user(user_id, username, full_name, referred_by)

    if is_new and referred_by:
        ref_stars = float(get_setting("referral_stars") or 0.25)
        add_balance(referred_by, ref_stars, f"Referral: {full_name}")
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (referred_by,))
        conn.commit()
        conn.close()
        try:
            await bot.send_message(
                referred_by,
                f"🎉 Do'stingiz <b>{full_name}</b> botga qo'shildi!\n"
                f"➕ Hisobingizga <b>+{ref_stars}⭐</b> qo'shildi!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    balance   = get_balance(user_id)
    ref_stars = get_setting("referral_stars") or "0.25"
    sub_stars = get_setting("subscribe_stars") or "0.10"

    await message.answer(
        f"⭐ <b>Stars Gift Bot</b> ga xush kelibsiz!\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"📌 <b>Qanday ishlaydi?</b>\n"
        f"• Do'stingizni taklif qiling → <b>+{ref_stars}⭐</b>\n"
        f"• Kanallarga obuna bo'ling → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'plab 🎁 Gift oling!\n\n"
        f"⏰ <b>Muhim:</b> Gift olish faqat <b>har kuni soat 20:00 — 00:00</b> da ishlaydi!\n\n"
        f"Quyidagi menyu orqali boshqaring 👇",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "work_hours")
async def work_hours_info(call: CallbackQuery):
    now = datetime.now(TIMEZONE)
    if is_working_hours():
        status = "🟢 <b>Hozir ish vaqti!</b> Gift buyurtma bera olasiz."
    else:
        status = "🔴 <b>Hozir ish vaqti emas.</b>\nSoat 20:00 dan keyin keling!"
    await call.message.edit_text(
        f"⏰ <b>Ish vaqti: 20:00 — 00:00</b>\n\n"
        f"{status}\n\n"
        f"🕐 Hozirgi vaqt: <b>{now.strftime('%H:%M')}</b>",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "balance")
async def show_balance(call: CallbackQuery):
    user_id   = call.from_user.id
    balance   = get_balance(user_id)
    user      = get_user(user_id)
    ref_count = user[5] if user else 0
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
    await call.message.edit_text(
        f"💰 <b>Balansingiz: {balance}⭐</b>\n\n"
        f"👥 Taklif qilganlar: <b>{ref_count} kishi</b>\n\n"
        f"🔗 Referral linkingiz:\n<code>{ref_link}</code>",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "referral")
async def show_referral(call: CallbackQuery):
    user_id   = call.from_user.id
    user      = get_user(user_id)
    ref_count = user[5] if user else 0
    ref_stars = get_setting("referral_stars") or "0.25"
    balance   = get_balance(user_id)
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
    await call.message.edit_text(
        f"👥 <b>Referral tizimi</b>\n\n"
        f"🔗 Sizning linkingiz:\n<code>{ref_link}</code>\n\n"
        f"🎁 Har bir do'st uchun: <b>+{ref_stars}⭐</b>\n"
        f"👤 Taklif qilganlar: <b>{ref_count} kishi</b>\n"
        f"💰 Balans: <b>{balance}⭐</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="📤 Do'stlarga yuborish",
                url=f"https://t.me/share/url?url={ref_link}&text=⭐ Bu bot orqali bepul Gift oling!"
            )],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "transactions")
async def show_transactions(call: CallbackQuery):
    user_id = call.from_user.id
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT amount, type, description, created_at FROM transactions "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (user_id,)
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        await call.answer("Hozircha transaksiyalar yo'q!", show_alert=True)
        return
    text = "📋 <b>So'nggi 10 ta transaksiya:</b>\n\n"
    for r in rows:
        sign = "➕" if r[1] == "credit" else "➖"
        text += f"{sign} <b>{r[0]}⭐</b> — {r[2]}\n<i>{r[3]}</i>\n\n"
    await call.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "channels")
async def show_channels(call: CallbackQuery):
    channels  = get_channels()
    if not channels:
        await call.answer("Hozircha kanallar yo'q!", show_alert=True)
        return
    sub_stars = get_setting("subscribe_stars") or "0.10"
    await call.message.edit_text(
        f"📢 <b>Kanallarga obuna bo'ling</b>\n\n"
        f"Har bir kanal uchun: <b>+{sub_stars}⭐</b>\n\n"
        f"Kanallarga obuna bo'lib tekshiring 👇",
        reply_markup=channels_keyboard(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    user_id = call.from_user.id
    is_subbed, not_subbed = await check_subscription(user_id)
    if not is_subbed:
        names = "\n".join([f"❌ {ch[2]}" for ch in not_subbed])
        await call.answer(f"Quyidagi kanallarga obuna bo'ling:\n{names}", show_alert=True)
        return
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM transactions WHERE user_id=? AND type='subscribe'", (user_id,))
    already = c.fetchone()[0]
    conn.close()
    if already > 0:
        await call.answer("✅ Siz allaqachon obuna bonusini oldingiz!", show_alert=True)
        return
    sub_stars = float(get_setting("subscribe_stars") or 0.10)
    channels  = get_channels()
    total     = round(sub_stars * len(channels), 2)
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (total, user_id))
    c.execute(
        "INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,'subscribe','Obuna bonusi')",
        (user_id, total)
    )
    conn.commit()
    conn.close()
    balance = get_balance(user_id)
    await call.answer(f"🎉 +{total}⭐ qo'shildi! Balans: {balance}⭐", show_alert=True)


@router.callback_query(F.data == "buy_gift")
async def buy_gift_menu(call: CallbackQuery):
    # Ish vaqtini tekshirish
    if not is_working_hours():
        now = datetime.now(TIMEZONE)
        await call.answer(
            f"⏰ Gift olish faqat soat 20:00 — 00:00 da!\n"
            f"Hozirgi vaqt: {now.strftime('%H:%M')}\n"
            f"Kechqurun keling! 🌙",
            show_alert=True
        )
        return

    user_id = call.from_user.id
    balance = get_balance(user_id)
    await call.message.edit_text(
        f"🎁 <b>Gift olish</b>\n\n"
        f"💰 Balans: <b>{balance}⭐</b>\n\n"
        f"✅ = Sotib olish mumkin\n"
        f"❌ = Stars yetarli emas\n\n"
        f"⏰ Buyurtma qabul: <b>20:00 — 00:00</b>\n\n"
        f"Qaysi giftni xohlaysiz? 👇",
        reply_markup=gifts_keyboard(balance),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("buyg_"))
async def select_gift(call: CallbackQuery):
    if not is_working_hours():
        await call.answer("⏰ Faqat soat 20:00 — 00:00 da buyurtma bera olasiz!", show_alert=True)
        return

    user_id = call.from_user.id
    idx     = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift    = GIFTS[idx]
    balance = get_balance(user_id)
    if balance < gift["stars"]:
        await call.answer(
            f"❌ Stars yetarli emas!\nKerak: {gift['stars']}⭐\nBalans: {balance}⭐",
            show_alert=True
        )
        return
    await call.message.edit_text(
        f"🎁 <b>{gift['emoji']} {gift['name']}</b>\n\n"
        f"💰 Narxi: <b>{gift['stars']}⭐</b>\n"
        f"💳 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"Buyurtma berasizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, buyurtma beraman!", callback_data=f"confirmg_{idx}"),
                InlineKeyboardButton(text="❌ Yo'q", callback_data="buy_gift")
            ]
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("confirmg_"))
async def confirm_gift(call: CallbackQuery):
    if not is_working_hours():
        await call.answer("⏰ Faqat soat 20:00 — 00:00 da buyurtma bera olasiz!", show_alert=True)
        return

    user_id   = call.from_user.id
    idx       = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift      = GIFTS[idx]
    balance   = get_balance(user_id)
    if balance < gift["stars"]:
        await call.answer("❌ Stars yetarli emas!", show_alert=True)
        return

    user      = get_user(user_id)
    username  = user[1] or ""
    full_name = user[2] or ""
    uname_display = f"@{username}" if username else full_name

    # Balansdan ayirish
    deduct_balance(user_id, gift["stars"], f"Gift buyurtma: {gift['name']}")

    # Buyurtmani saqlash
    order_id = add_order(user_id, username, full_name, gift["name"], gift["emoji"], gift["stars"])

    # Adminga xabar yuborish
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Yangi gift buyurtma!</b>\n\n"
            f"🆔 Buyurtma #{order_id}\n"
            f"👤 Foydalanuvchi: {uname_display}\n"
            f"🪪 ID: <code>{user_id}</code>\n"
            f"🎁 Gift: {gift['emoji']} {gift['name']}\n"
            f"⭐ Miqdor: {gift['stars']} stars\n"
            f"🕐 Vaqt: {datetime.now(TIMEZONE).strftime('%H:%M')}\n\n"
            f"✅ Giftni yuborish uchun {uname_display} ga {gift['stars']} ta stars gifti yuboring!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Bajarildi",
                    callback_data=f"done_order_{order_id}_{user_id}"
                )]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin xabar yuborishda xato: {e}")

    new_balance = get_balance(user_id)
    await call.message.edit_text(
        f"✅ <b>Buyurtmangiz qabul qilindi!</b>\n\n"
        f"🎁 {gift['emoji']} <b>{gift['name']}</b>\n"
        f"⭐ {gift['stars']} stars\n\n"
        f"⏳ Admin soat <b>20:00 — 00:00</b> oralig'ida sizga gift yuboradi.\n\n"
        f"💰 Qolgan balans: <b>{new_balance}⭐</b>",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


# Admin buyurtmani bajarildiga belgilash
@router.callback_query(F.data.startswith("done_order_"))
async def done_order(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    parts    = call.data.split("_")
    order_id = int(parts[2])
    user_id  = int(parts[3])

    complete_order(order_id)

    # Foydalanuvchiga xabar
    try:
        await bot.send_message(
            user_id,
            f"🎉 <b>Giftingiz yuborildi!</b>\n\n"
            f"Admin tomonidan sizga gift yuborildi.\n"
            f"Tekshiring! ✅",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>BAJARILDI</b>",
        parse_mode="HTML"
    )
    await call.answer("✅ Buyurtma bajarildi deb belgilandi!")


@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id   = call.from_user.id
    balance   = get_balance(user_id)
    ref_stars = get_setting("referral_stars") or "0.25"
    sub_stars = get_setting("subscribe_stars") or "0.10"
    await call.message.edit_text(
        f"⭐ <b>Stars Gift Bot</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"• Do'st taklif → <b>+{ref_stars}⭐</b>\n"
        f"• Kanal obuna → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'pla → 🎁 Gift ol!\n\n"
        f"⏰ Gift olish vaqti: <b>20:00 — 00:00</b>",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )
    await call.answer()


# ===================== ADMIN HANDLERS =====================

@router.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await call.message.edit_text("🔧 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_orders")
async def admin_orders(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    orders = get_pending_orders()
    if not orders:
        await call.answer("Kutayotgan buyurtmalar yo'q!", show_alert=True)
        return
    text = "📦 <b>Kutayotgan buyurtmalar:</b>\n\n"
    buttons = []
    for o in orders:
        uname = f"@{o[2]}" if o[2] else o[3]
        text += f"#{o[0]} — {uname} — {o[5]} {o[4]} ({o[6]}⭐)\n"
        buttons.append([InlineKeyboardButton(
            text=f"✅ #{o[0]} — {uname} — {o[5]} {o[6]}⭐",
            callback_data=f"done_order_{o[0]}_{o[1]}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_channel)
    await call.message.edit_text(
        "➕ <b>Kanal qo'shish</b>\n\n"
        "Quyidagi formatda yuboring:\n"
        "<code>@username | Kanal nomi | https://t.me/username</code>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.add_channel)
async def process_add_channel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = [p.strip() for p in message.text.split("|")]
        if len(parts) != 3:
            await message.answer("❌ Format noto'g'ri!\n\n<code>@username | Nomi | Link</code>", parse_mode="HTML")
            return
        channel_id, name, link = parts
        add_channel(channel_id, name, link)
        await state.clear()
        await message.answer(f"✅ Kanal qo'shildi!\n📢 {name}", reply_markup=admin_keyboard())
    except Exception as e:
        await message.answer(f"❌ Xatolik: {e}")


@router.callback_query(F.data == "admin_remove_channel")
async def admin_remove_channel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    channels = get_channels()
    if not channels:
        await call.answer("Kanallar yo'q!", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {ch[2]}", callback_data=f"delch_{ch[1]}")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(
        "➖ <b>Qaysi kanalni o'chirish?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("delch_"))
async def delete_channel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    channel_id = call.data[6:]
    remove_channel(channel_id)
    await call.answer(f"✅ O'chirildi: {channel_id}", show_alert=True)
    channels = get_channels()
    if not channels:
        await call.message.edit_text("✅ Barcha kanallar o'chirildi.", reply_markup=admin_keyboard())
        return
    buttons = [[InlineKeyboardButton(text=f"🗑 {ch[2]}", callback_data=f"delch_{ch[1]}")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == "admin_set_referral")
async def admin_set_referral(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = get_setting("referral_stars")
    await state.set_state(AdminStates.set_referral_stars)
    await call.message.edit_text(
        f"⭐ <b>Referral stars miqdori</b>\n\nHozirgi: <b>{current}⭐</b>\n\nYangi miqdor kiriting:",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_referral_stars)
async def process_referral_stars(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip())
        set_setting("referral_stars", val)
        await state.clear()
        await message.answer(f"✅ Referral stars: <b>{val}⭐</b>", reply_markup=admin_keyboard(), parse_mode="HTML")
    except Exception:
        await message.answer("❌ Raqam kiriting! Masalan: 0.25")


@router.callback_query(F.data == "admin_set_subscribe")
async def admin_set_subscribe(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = get_setting("subscribe_stars")
    await state.set_state(AdminStates.set_subscribe_stars)
    await call.message.edit_text(
        f"⭐ <b>Obuna stars miqdori</b>\n\nHozirgi: <b>{current}⭐</b>\n\nYangi miqdor kiriting:",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_subscribe_stars)
async def process_subscribe_stars(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip())
        set_setting("subscribe_stars", val)
        await state.clear()
        await message.answer(f"✅ Obuna stars: <b>{val}⭐</b>", reply_markup=admin_keyboard(), parse_mode="HTML")
    except Exception:
        await message.answer("❌ Raqam kiriting! Masalan: 0.10")


@router.callback_query(F.data == "admin_add_balance")
async def admin_add_balance(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_balance_input)
    await call.message.edit_text(
        "💰 <b>Balans qo'shish</b>\n\n"
        "Formatda yuboring:\n"
        "<code>USER_ID MIQDOR</code>\n\n"
        "Masalan: <code>123456789 10.5</code>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.add_balance_input)
async def process_add_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.strip().split()
        uid   = int(parts[0])
        amt   = float(parts[1])
        if not get_user(uid):
            await message.answer("❌ Foydalanuvchi topilmadi!")
            return
        add_balance(uid, amt, "Admin tomonidan qo'shildi")
        await state.clear()
        new_bal = get_balance(uid)
        await message.answer(
            f"✅ {uid} ga <b>+{amt}⭐</b> qo'shildi!\nYangi balans: <b>{new_bal}⭐</b>",
            reply_markup=admin_keyboard(),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(uid, f"💰 Hisobingizga <b>+{amt}⭐</b> qo'shildi!", parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        await message.answer("❌ Format: <code>USER_ID MIQDOR</code>", parse_mode="HTML")


@router.callback_query(F.data == "admin_deduct_balance")
async def admin_deduct_balance(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.deduct_balance_input)
    await call.message.edit_text(
        "💸 <b>Balans ayirish</b>\n\n"
        "Formatda yuboring:\n"
        "<code>USER_ID MIQDOR</code>\n\n"
        "Masalan: <code>123456789 5.0</code>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.deduct_balance_input)
async def process_deduct_balance(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.strip().split()
        uid   = int(parts[0])
        amt   = float(parts[1])
        if not get_user(uid):
            await message.answer("❌ Foydalanuvchi topilmadi!")
            return
        deduct_balance(uid, amt, "Admin tomonidan ayirildi")
        await state.clear()
        new_bal = get_balance(uid)
        await message.answer(
            f"✅ {uid} dan <b>-{amt}⭐</b> ayirildi!\nYangi balans: <b>{new_bal}⭐</b>",
            reply_markup=admin_keyboard(),
            parse_mode="HTML"
        )
    except Exception:
        await message.answer("❌ Format: <code>USER_ID MIQDOR</code>", parse_mode="HTML")


@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    total_users, total_balance = get_stats()
    ref_stars = get_setting("referral_stars")
    sub_stars = get_setting("subscribe_stars")
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM transactions WHERE type='credit'")
    total_credits = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders WHERE status='done'")
    total_gifts = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM orders WHERE status='pending'")
    pending_gifts = c.fetchone()[0]
    conn.close()
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
        f"💰 Jami balanslar: <b>{total_balance}⭐</b>\n"
        f"📈 Jami kreditlar: <b>{total_credits}</b>\n"
        f"🎁 Bajarilgan buyurtmalar: <b>{total_gifts}</b>\n"
        f"⏳ Kutayotgan buyurtmalar: <b>{pending_gifts}</b>\n\n"
        f"⚙️ Sozlamalar:\n"
        f"• Referral: <b>{ref_stars}⭐</b>\n"
        f"• Obuna: <b>{sub_stars}⭐</b>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "admin_users")
async def admin_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT user_id, username, full_name, balance, referral_count FROM users "
        "ORDER BY balance DESC LIMIT 15"
    )
    rows = c.fetchall()
    conn.close()
    text = "👥 <b>Top 15 foydalanuvchi (balans bo'yicha):</b>\n\n"
    for i, r in enumerate(rows, 1):
        uname = f"@{r[1]}" if r[1] else r[2]
        text += f"{i}. {uname} — <b>{round(r[3],2)}⭐</b> | 👥{r[4]}\n"
    await call.message.edit_text(text, reply_markup=back_kb("admin_panel"), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📣 <b>Broadcast</b>\n\nBarcha foydalanuvchilarga yuboriladigan xabarni yozing:",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    users = get_all_users()
    sent, failed = 0, 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📣 {message.text}", parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.3)  # Spam bo'lmasin uchun
        except Exception:
            failed += 1
    await message.answer(
        f"✅ Yuborildi!\n\n✅ Muvaffaqiyatli: {sent}\n❌ Xato: {failed}",
        reply_markup=admin_keyboard()
    )


# ===================== MAIN =====================
async def main():
    init_db()
    dp.include_router(router)

    async def health(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Web server port {port} da ishga tushdi!")
    logger.info("✅ Bot ishga tushdi!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
