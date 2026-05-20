import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = "8910662045:AAEMfDVkTGcUlATtEFRTzF2tDoRzWQIRw3M"
ADMIN_ID = 6102256074

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def db():
    return sqlite3.connect("bot.db")

def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        balance REAL DEFAULT 0,
        referred_by INTEGER DEFAULT NULL,
        referral_count INTEGER DEFAULT 0,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id TEXT UNIQUE,
        channel_name TEXT,
        channel_link TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        type TEXT,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
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
    c.execute("INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
              (user_id, username, full_name, referred_by))
    conn.commit()
    conn.close()

def add_balance(user_id, amount, desc=""):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    c.execute("INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
              (user_id, amount, "credit", desc))
    conn.commit()
    conn.close()

def deduct_balance(user_id, amount, desc=""):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (amount, user_id))
    c.execute("INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,?,?)",
              (user_id, amount, "debit", desc))
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
    c.execute("INSERT OR IGNORE INTO channels (channel_id, channel_name, channel_link) VALUES (?,?,?)",
              (channel_id, name, link))
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

class AdminStates(StatesGroup):
    add_channel = State()
    set_referral_stars = State()
    set_subscribe_stars = State()
    broadcast = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

GIFTS = [
    {"id": "TgGift15Stars1",  "emoji": "💝", "name": "Heart",    "stars": 15},
    {"id": "TgGift15Stars2",  "emoji": "🧸", "name": "Bear",     "stars": 15},
    {"id": "TgGift25Stars1",  "emoji": "🎁", "name": "Present",  "stars": 25},
    {"id": "TgGift25Stars2",  "emoji": "🌹", "name": "Rose",     "stars": 25},
    {"id": "TgGift50Stars1",  "emoji": "🎂", "name": "Cake",     "stars": 50},
    {"id": "TgGift50Stars2",  "emoji": "💐", "name": "Bouquet",  "stars": 50},
    {"id": "TgGift50Stars3",  "emoji": "🚀", "name": "Rocket",   "stars": 50},
    {"id": "TgGift100Stars1", "emoji": "🏆", "name": "Trophy",   "stars": 100},
    {"id": "TgGift100Stars2", "emoji": "💍", "name": "Ring",     "stars": 100},
    {"id": "TgGift100Stars3", "emoji": "💎", "name": "Diamond",  "stars": 100},
]

def main_menu(user_id):
    buttons = [
        [InlineKeyboardButton(text="⭐ Balansim", callback_data="balance"),
         InlineKeyboardButton(text="👥 Referral", callback_data="referral")],
        [InlineKeyboardButton(text="🎁 Gift sotib olish", callback_data="buy_gift")],
        [InlineKeyboardButton(text="📢 Kanallarga obuna", callback_data="channels")],
    ]
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🔧 Admin panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Kanal qoshish", callback_data="admin_add_channel")],
        [InlineKeyboardButton(text="➖ Kanal ochirish", callback_data="admin_remove_channel")],
        [InlineKeyboardButton(text="⭐ Referral stars sozla", callback_data="admin_set_referral")],
        [InlineKeyboardButton(text="⭐ Obuna stars sozla", callback_data="admin_set_subscribe")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📣 Xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")],
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

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    args = message.text.split()
    referred_by = None
    is_new = get_user(user_id) is None

    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id and get_user(ref_id):
                referred_by = ref_id
        except:
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
        except:
            pass

    balance = get_balance(user_id)
    ref_stars = get_setting("referral_stars") or "0.25"
    sub_stars = get_setting("subscribe_stars") or "0.10"

    await message.answer(
        f"⭐ <b>Stars Gift Bot</b> ga xush kelibsiz!\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"📌 <b>Qanday ishlaydi?</b>\n"
        f"• Do'stingizni taklif qiling → <b>+{ref_stars}⭐</b>\n"
        f"• Kanallarga obuna bo'ling → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'plab 🎁 Gift sotib oling!\n\n"
        f"Quyidagi menyu orqali boshqaring 👇",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "balance")
async def show_balance(call: CallbackQuery):
    user_id = call.from_user.id
    balance = get_balance(user_id)
    user = get_user(user_id)
    ref_count = user[5] if user else 0
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    await call.message.edit_text(
        f"💰 <b>Balansingiz: {balance}⭐</b>\n\n"
        f"👥 Taklif qilganlar: <b>{ref_count} kishi</b>\n\n"
        f"🔗 Referral linkingiz:\n<code>{ref_link}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")]
        ]),
        parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data == "referral")
async def show_referral(call: CallbackQuery):
    user_id = call.from_user.id
    user = get_user(user_id)
    ref_count = user[5] if user else 0
    ref_stars = get_setting("referral_stars") or "0.25"
    balance = get_balance(user_id)
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
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

@router.callback_query(F.data == "channels")
async def show_channels(call: CallbackQuery):
    channels = get_channels()
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
    channels = get_channels()
    total = round(sub_stars * len(channels), 2)
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (total, user_id))
    c.execute("INSERT INTO transactions (user_id, amount, type, description) VALUES (?,?,'subscribe','Obuna bonusi')",
              (user_id, total))
    conn.commit()
    conn.close()
    balance = get_balance(user_id)
    await call.answer(f"🎉 +{total}⭐ qo'shildi! Balans: {balance}⭐", show_alert=True)

@router.callback_query(F.data == "buy_gift")
async def buy_gift_menu(call: CallbackQuery):
    user_id = call.from_user.id
    balance = get_balance(user_id)
    await call.message.edit_text(
        f"🎁 <b>Gift sotib olish</b>\n\n"
        f"💰 Balans: <b>{balance}⭐</b>\n\n"
        f"✅ = Sotib olish mumkin\n"
        f"❌ = Stars yetarli emas\n\n"
        f"Qaysi giftni xohlaysiz? 👇",
        reply_markup=gifts_keyboard(balance),
        parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("buyg_"))
async def select_gift(call: CallbackQuery):
    user_id = call.from_user.id
    idx = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift = GIFTS[idx]
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
        f"Sotib olishni tasdiqlaysizmi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, olaman!", callback_data=f"confirmg_{idx}"),
                InlineKeyboardButton(text="❌ Yo'q", callback_data="buy_gift")
            ]
        ]),
        parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data.startswith("confirmg_"))
async def confirm_gift(call: CallbackQuery):
    user_id = call.from_user.id
    idx = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift = GIFTS[idx]
    balance = get_balance(user_id)
    if balance < gift["stars"]:
        await call.answer("❌ Stars yetarli emas!", show_alert=True)
        return
    deduct_balance(user_id, gift["stars"], f"Gift: {gift['name']}")
    try:
        await bot.send_gift(user_id=user_id, gift_id=gift["id"])
        new_balance = get_balance(user_id)
        await call.message.edit_text(
            f"🎉 <b>Tabriklaymiz!</b>\n\n"
            f"{gift['emoji']} <b>{gift['name']}</b> giftingiz yuborildi!\n\n"
            f"💰 Qolgan balans: <b>{new_balance}⭐</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        add_balance(user_id, gift["stars"], "Gift xatoligi - qaytarildi")
        await call.message.edit_text(
            f"⚠️ <b>Gift yuborishda xatolik!</b>\n\nStars qaytarildi. Admin bilan bog'laning.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")]
            ]),
            parse_mode="HTML"
        )
        await bot.send_message(ADMIN_ID, f"⚠️ Gift xatoligi!\nUser: {user_id}\nGift: {gift['id']}\nXato: {e}")
    await call.answer()

@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
    balance = get_balance(user_id)
    ref_stars = get_setting("referral_stars") or "0.25"
    sub_stars = get_setting("subscribe_stars") or "0.10"
    await call.message.edit_text(
        f"⭐ <b>Stars Gift Bot</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"• Do'st taklif → <b>+{ref_stars}⭐</b>\n"
        f"• Kanal obuna → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'pla → 🎁 Gift ol!",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )
    await call.answer()

# ===== ADMIN =====
@router.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await call.message.edit_text("🔧 <b>Admin Panel</b>", reply_markup=admin_keyboard(), parse_mode="HTML")
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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_panel")]
        ]),
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
    await call.message.edit_text("➖ <b>Qaysi kanalni o'chirish?</b>",
                                  reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_panel")]
        ]),
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
    except:
        await message.answer("❌ Raqam kiriting! Masalan: 0.25")

@router.callback_query(F.data == "admin_set_subscribe")
async def admin_set_subscribe(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = get_setting("subscribe_stars")
    await state.set_state(AdminStates.set_subscribe_stars)
    await call.message.edit_text(
        f"⭐ <b>Obuna stars miqdori</b>\n\nHozirgi: <b>{current}⭐</b>\n\nYangi miqdor kiriting:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_panel")]
        ]),
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
    except:
        await message.answer("❌ Raqam kiriting! Masalan: 0.10")

@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    total_users, total_balance = get_stats()
    ref_stars = get_setting("referral_stars")
    sub_stars = get_setting("subscribe_stars")
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{total_users}</b>\n"
        f"💰 Jami balanslar: <b>{total_balance}⭐</b>\n\n"
        f"⚙️ Sozlamalar:\n"
        f"• Referral: <b>{ref_stars}⭐</b>\n"
        f"• Obuna: <b>{sub_stars}⭐</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")]
        ]),
        parse_mode="HTML"
    )
    await call.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📣 <b>Broadcast</b>\n\nBarcha foydalanuvchilarga yuboriladigan xabarni yozing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor", callback_data="admin_panel")]
        ]),
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
        except:
            failed += 1
    await message.answer(
        f"✅ Yuborildi!\n\n✅ Muvaffaqiyatli: {sent}\n❌ Xato: {failed}",
        reply_markup=admin_keyboard()
    )

async def main():
    init_db()
    dp.include_router(router)
    logger.info("✅ Bot ishga tushdi!")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
