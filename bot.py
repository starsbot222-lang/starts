import asyncio
import logging
import os
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

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

# ===================== SOZLAMALAR =====================
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID      = int(os.environ.get("ADMIN_ID", "6102256074"))
MONGO_URL     = os.environ.get("MONGO_URL", "mongodb+srv://user:pass@cluster.mongodb.net/starsbot")
SUPPORT_GROUP = os.environ.get("SUPPORT_GROUP", "https://t.me/FreeStarsbotInfo")
TIMEZONE      = pytz.timezone("Asia/Tashkent")
DB_NAME       = os.environ.get("DB_NAME", "starsbot")

MIN_REFERRALS_FOR_GIFT = 3
# ======================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ===================== MongoDB =====================
client = AsyncIOMotorClient(
    MONGO_URL,
    serverSelectionTimeoutMS=10000,
    connectTimeoutMS=10000,
    socketTimeoutMS=10000,
)
mdb = client[DB_NAME]

users            = mdb["users"]
channels         = mdb["channels"]
settings_col     = mdb["settings"]
transactions     = mdb["transactions"]
orders           = mdb["orders"]
admin_logs       = mdb["admin_logs"]
referral_hourly  = mdb["referral_hourly"]
support_cooldown = mdb["support_cooldown"]
channel_bonus    = mdb["user_channel_bonus"]


async def init_db():
    try:
        await client.admin.command("ping")
        logger.info("✅ MongoDB ulanishi tasdiqlandi!")
    except Exception as e:
        logger.critical(f"❌ MongoDB ulanishda xato: {e}")
        raise

    await settings_col.update_one(
        {"key": "referral_stars"},
        {"$setOnInsert": {"key": "referral_stars", "value": "0.25"}},
        upsert=True
    )
    await settings_col.update_one(
        {"key": "subscribe_stars"},
        {"$setOnInsert": {"key": "subscribe_stars", "value": "0.10"}},
        upsert=True
    )

    await users.create_index("user_id", unique=True)
    await channels.create_index("channel_id", unique=True)
    await transactions.create_index("user_id")
    await transactions.create_index("created_at")
    await orders.create_index("status")
    await orders.create_index("created_at")
    await channel_bonus.create_index(
        [("user_id", 1), ("channel_id", 1)], unique=True
    )
    await referral_hourly.create_index(
        "created_at",
        expireAfterSeconds=86400
    )
    await support_cooldown.create_index(
        "last_sent_at",
        expireAfterSeconds=7200
    )
    logger.info("✅ Indekslar tayyor!")


# ===================== YORDAMCHI FUNKSIYALAR =====================

def is_working_hours() -> bool:
    now = datetime.now(TIMEZONE)
    return 20 <= now.hour < 24


async def get_setting(key: str):
    doc = await settings_col.find_one({"key": key})
    return doc["value"] if doc else None


async def set_setting(key: str, value):
    await settings_col.update_one(
        {"key": key},
        {"$set": {"value": str(value)}},
        upsert=True
    )


async def admin_log(admin_id: int, action: str, details: str = ""):
    await admin_logs.insert_one({
        "admin_id": admin_id,
        "action": action,
        "details": details,
        "created_at": datetime.utcnow()
    })


async def get_user(user_id: int):
    return await users.find_one({"user_id": user_id})


async def add_user(user_id: int, username: str, full_name: str, referred_by=None):
    await users.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "full_name": full_name,
            },
            "$setOnInsert": {
                "user_id": user_id,
                "balance": 0.0,
                "referred_by": referred_by,
                "referral_count": 0,
                "last_order_time": None,
                "joined_at": datetime.utcnow()
            }
        },
        upsert=True
    )


async def add_balance(user_id: int, amount: float, desc: str = ""):
    await users.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": amount}}
    )
    await transactions.insert_one({
        "user_id": user_id,
        "amount": amount,
        "type": "credit",
        "description": desc,
        "created_at": datetime.utcnow()
    })


async def deduct_balance(user_id: int, amount: float, desc: str = "") -> bool:
    result = await users.find_one_and_update(
        {
            "user_id": user_id,
            "balance": {"$gte": amount}
        },
        {"$inc": {"balance": -amount}},
        return_document=ReturnDocument.AFTER
    )
    if not result:
        return False
    await transactions.insert_one({
        "user_id": user_id,
        "amount": amount,
        "type": "debit",
        "description": desc,
        "created_at": datetime.utcnow()
    })
    return True


async def get_balance(user_id: int) -> float:
    user = await users.find_one({"user_id": user_id})
    return round(user.get("balance", 0), 2) if user else 0.0


async def get_channels():
    return await channels.find().to_list(length=100)


async def add_channel(channel_id: str, name: str, link: str):
    await channels.update_one(
        {"channel_id": channel_id},
        {"$setOnInsert": {
            "channel_id": channel_id,
            "channel_name": name,
            "channel_link": link
        }},
        upsert=True
    )


async def remove_channel(channel_id: str):
    await channels.delete_one({"channel_id": channel_id})


async def get_stats():
    total_users   = await users.count_documents({})
    bal_agg       = await users.aggregate(
        [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
    ).to_list(1)
    total_balance = round(bal_agg[0]["total"], 2) if bal_agg else 0
    total_credits = await transactions.count_documents({"type": "credit"})
    total_gifts   = await orders.count_documents({"status": "done"})
    pending_gifts = await orders.count_documents({"status": "pending"})
    return total_users, total_balance, total_credits, total_gifts, pending_gifts


async def get_all_user_ids():
    cursor = users.find({}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]


async def add_order(user_id, username, full_name, gift_name, gift_emoji, gift_stars):
    result = await orders.insert_one({
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "gift_name": gift_name,
        "gift_emoji": gift_emoji,
        "gift_stars": gift_stars,
        "status": "pending",
        "created_at": datetime.utcnow()
    })
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"last_order_time": datetime.utcnow()}}
    )
    return str(result.inserted_id)


async def complete_order(order_id: str):
    await orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "done"}}
    )


async def get_pending_orders():
    return await orders.find({"status": "pending"}).sort("created_at", 1).to_list(length=100)


async def check_referral_abuse(referred_by: int) -> bool:
    hour_key = datetime.now(TIMEZONE).strftime("%Y%m%d%H")
    doc = await referral_hourly.find_one_and_update(
        {"referrer_id": referred_by, "hour_key": hour_key},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"created_at": datetime.utcnow()}
        },
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return doc["count"] <= 5


async def check_order_cooldown(user_id: int) -> bool:
    user = await users.find_one({"user_id": user_id})
    if not user or not user.get("last_order_time"):
        return True
    try:
        last = user["last_order_time"]
        diff = (datetime.utcnow() - last).total_seconds()
        return diff >= 15
    except Exception:
        return True


async def check_support_cooldown(user_id: int):
    doc = await support_cooldown.find_one({"user_id": user_id})
    if not doc or not doc.get("last_sent_at"):
        return True, 0
    try:
        last = doc["last_sent_at"]
        diff = (datetime.utcnow() - last).total_seconds()
        if diff >= 3600:
            return True, 0
        minutes_left = int((3600 - diff) / 60) + 1
        return False, minutes_left
    except Exception:
        return True, 0


async def update_support_cooldown(user_id: int):
    await support_cooldown.update_one(
        {"user_id": user_id},
        {"$set": {"last_sent_at": datetime.utcnow()}},
        upsert=True
    )


async def check_subscription(user_id: int):
    chs = await get_channels()
    if not chs:
        return True, []
    not_subbed = []
    for ch in chs:
        try:
            cid    = int(ch["channel_id"])
            member = await bot.get_chat_member(cid, user_id)
            if member.status in ["left", "kicked", "banned"]:
                not_subbed.append(ch)
        except Exception as e:
            logger.warning(f"Kanal tekshirishda xato {ch['channel_id']}: {e}")
            not_subbed.append(ch)
    return len(not_subbed) == 0, not_subbed


async def get_referral_count(user_id: int) -> int:
    user = await users.find_one({"user_id": user_id})
    return user.get("referral_count", 0) if user else 0


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
    add_channel_link     = State()
    set_referral_stars   = State()
    set_subscribe_stars  = State()
    broadcast            = State()
    add_balance_input    = State()
    deduct_balance_input = State()


class UserStates(StatesGroup):
    support_message = State()


# ===================== BOT & ROUTER =====================
bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()

# Simple in-memory cache for admin users pagination
_users_cache: list = []


# ===================== KLAVIATURALAR =====================
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="⭐ Balansim",      callback_data="balance"),
            InlineKeyboardButton(text="👥 Referral",      callback_data="referral"),
        ],
        [InlineKeyboardButton(text="🎁 Gift olish",       callback_data="buy_gift")],
        [InlineKeyboardButton(text="📢 Kanallarga obuna", callback_data="channels")],
        [InlineKeyboardButton(text="📋 Transaksiyalar",   callback_data="transactions")],
        [InlineKeyboardButton(text="⏰ Ish vaqti",        callback_data="work_hours")],
        [InlineKeyboardButton(text="🆘 Yordam / Muammo", callback_data="support")],
    ]
    
    if user_id == ADMIN_ID:
        buttons.append([InlineKeyboardButton(text="🔧 Admin panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_keyboard() -> InlineKeyboardMarkup:
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


async def gifts_keyboard(user_balance: float) -> InlineKeyboardMarkup:
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


def back_kb(cb: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Ortga", callback_data=cb)]
    ])


# ===================== KANAL YORDAMCHILARI =====================
async def resolve_channel(link_or_username: str):
    raw = link_or_username.strip()
    if raw.lstrip("-").isdigit():
        try:
            chat = await bot.get_chat(int(raw))
            return str(chat.id), chat.title or raw
        except Exception as e:
            logger.warning(f"Kanal ID bo'yicha topilmadi: {raw} — {e}")
            return None, None

    if raw.startswith("https://t.me/"):
        username = "@" + raw.split("t.me/")[-1].split("/")[0]
    elif raw.startswith("t.me/"):
        username = "@" + raw.split("t.me/")[-1].split("/")[0]
    elif raw.startswith("@"):
        username = raw
    else:
        username = "@" + raw

    try:
        chat = await bot.get_chat(username)
        return str(chat.id), chat.title or username
    except Exception as e:
        logger.warning(f"Kanal topilmadi: {username} — {e}")
        return None, None


# ===================== CHANNELS RENDER HELPER =====================
async def render_channels_menu(user_id: int, message) -> None:
    chs = await get_channels()
    sub_stars = await get_setting("subscribe_stars") or "0.10"

    if not chs:
        await message.edit_text(
            "📢 Hozircha kanallar yo'q.",
            reply_markup=back_kb()
        )
        return

    buttons = []
    for ch in chs:
        try:
            cid = int(ch["channel_id"])
            member = await bot.get_chat_member(cid, user_id)
            is_member = member.status not in ["left", "kicked", "banned"]
        except Exception:
            is_member = False

        bonus_doc = await channel_bonus.find_one({
            "user_id": user_id,
            "channel_id": ch["channel_id"]
        })

        if is_member and bonus_doc:
            icon = "✅"
        elif is_member and not bonus_doc:
            icon = "✅"
        else:
            icon = "❌"

        # Kanal va tekshirish tugmasi YONMA-YON (bir qatorda)
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {ch['channel_name']}",
                url=ch["channel_link"]
            ),
            InlineKeyboardButton(
                text="🔄 Tekshirish",
                callback_data=f"checksub_{ch['channel_id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(
        text="🔄 Hammasini tekshirish",
        callback_data="check_sub"
    )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")])

    await message.edit_text(
        f"📢 <b>Kanallarga obuna bo'ling</b>\n\n"
        f"Har bir kanal uchun: <b>+{sub_stars}⭐</b>\n\n"
        f"✅ = Obuna bo'lgansiz\n"
        f"❌ = Obuna bo'lmagansiz\n\n"
        f"Kanal nomiga bosib obuna bo'ling,\n"
        f"keyin <b>🔄 Tekshirish</b> tugmasini bosing 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
# ===================== HANDLERS =====================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id   = message.from_user.id
    username  = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    args      = message.text.split()

    is_new = (await get_user(user_id)) is None
    referred_by = None

    if len(args) > 1:
        try:
            ref_id = int(args[1])
            if ref_id != user_id and is_new:
                referrer = await get_user(ref_id)
                if referrer:
                    referred_by = ref_id
        except Exception:
            pass

    await add_user(user_id, username, full_name, referred_by)

    if is_new and referred_by:
        if await check_referral_abuse(referred_by):
            ref_stars = float(await get_setting("referral_stars") or 0.25)
            await add_balance(referred_by, ref_stars, f"Referral: {full_name}")
            await users.update_one(
                {"user_id": referred_by},
                {"$inc": {"referral_count": 1}}
            )
            try:
                await bot.send_message(
                    referred_by,
                    f"🎉 Do'stingiz <b>{full_name}</b> botga qo'shildi!\n"
                    f"➕ Hisobingizga <b>+{ref_stars}⭐</b> qo'shildi!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ <b>Shubhali referral faoliyat!</b>\n\n"
                    f"Referrer ID: <code>{referred_by}</code>\n"
                    f"1 soatda 5+ yangi foydalanuvchi qo'shdi. Stars berilmadi.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    balance   = await get_balance(user_id)
    ref_stars = await get_setting("referral_stars") or "0.25"
    sub_stars = await get_setting("subscribe_stars") or "0.10"

    await message.answer(
        f"⭐ <b>Stars Gift Bot</b> ga xush kelibsiz!\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"📌 <b>Qanday ishlaydi?</b>\n"
        f"• Do'stingizni taklif qiling → <b>+{ref_stars}⭐</b>\n"
        f"• Kanallarga obuna bo'ling → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'plab 🎁 Gift oling!\n\n"
        f"⏰ <b>Muhim:</b> Gift olish faqat <b>har kuni soat 20:00 — 00:00</b> da ishlaydi!\n"
        f"👥 <b>Gift olish uchun kamida {MIN_REFERRALS_FOR_GIFT} ta do'st taklif qiling!</b>\n\n"
        f"Quyidagi menyu orqali boshqaring 👇",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "work_hours")
async def work_hours_info(call: CallbackQuery):
    now = datetime.now(TIMEZONE)
    status = (
        "🟢 <b>Hozir ish vaqti!</b> Gift buyurtma bera olasiz."
        if is_working_hours() else
        "🔴 <b>Hozir ish vaqti emas.</b>\nSoat 20:00 dan keyin keling!"
    )
    await call.message.edit_text(
        f"⏰ <b>Ish vaqti: 20:00 — 00:00</b>\n\n{status}\n\n"
        f"🕐 Hozirgi vaqt: <b>{now.strftime('%H:%M')}</b>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "balance")
async def show_balance(call: CallbackQuery):
    user_id   = call.from_user.id
    balance   = await get_balance(user_id)
    user      = await get_user(user_id)
    ref_count = user.get("referral_count", 0) if user else 0
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"

    ref_status = (
        f"✅ Gift olish mumkin (👥 {ref_count}/{MIN_REFERRALS_FOR_GIFT})"
        if ref_count >= MIN_REFERRALS_FOR_GIFT
        else f"❌ Gift uchun yana {MIN_REFERRALS_FOR_GIFT - ref_count} ta do'st kerak"
    )

    await call.message.edit_text(
        f"💰 <b>Balansingiz: {balance}⭐</b>\n\n"
        f"👥 Taklif qilganlar: <b>{ref_count}/{MIN_REFERRALS_FOR_GIFT} kishi</b>\n"
        f"{ref_status}\n\n"
        f"🔗 Referral linkingiz:\n<code>{ref_link}</code>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "referral")
async def show_referral(call: CallbackQuery):
    user_id   = call.from_user.id
    user      = await get_user(user_id)
    ref_count = user.get("referral_count", 0) if user else 0
    ref_stars = await get_setting("referral_stars") or "0.25"
    balance   = await get_balance(user_id)
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"

    remaining = max(0, MIN_REFERRALS_FOR_GIFT - ref_count)
    progress_text = (
        f"✅ <b>Gift olish huquqingiz bor!</b>"
        if remaining == 0
        else f"⏳ Gift olish uchun yana <b>{remaining} ta do'st</b> taklif qiling"
    )

    await call.message.edit_text(
        f"👥 <b>Referral tizimi</b>\n\n"
        f"🔗 Sizning linkingiz:\n<code>{ref_link}</code>\n\n"
        f"🎁 Har bir do'st uchun: <b>+{ref_stars}⭐</b>\n"
        f"👤 Taklif qilganlar: <b>{ref_count}/{MIN_REFERRALS_FOR_GIFT} kishi</b>\n"
        f"💰 Balans: <b>{balance}⭐</b>\n\n"
        f"{progress_text}",
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
    txs = await transactions.find(
        {"user_id": user_id}
    ).sort("created_at", -1).limit(10).to_list(length=10)
    if not txs:
        await call.answer("Hozircha transaksiyalar yo'q!", show_alert=True)
        return
    text = "📋 <b>So'nggi 10 ta transaksiya:</b>\n\n"
    for r in txs:
        sign = "➕" if r["type"] == "credit" else "➖"
        dt   = r["created_at"].strftime("%Y-%m-%d %H:%M") if r.get("created_at") else ""
        text += f"{sign} <b>{r['amount']}⭐</b> — {r.get('description', '')}\n<i>{dt}</i>\n\n"
    await call.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


# ===================== CHANNELS =====================

@router.callback_query(F.data == "channels")
async def show_channels(call: CallbackQuery):
    chs = await get_channels()
    if not chs:
        await call.answer("Hozircha kanallar yo'q!", show_alert=True)
        return
    await call.answer()
    await render_channels_menu(call.from_user.id, call.message)


# ✅ YANGI: Har bir kanal uchun alohida tekshirish
@router.callback_query(F.data.startswith("checksub_"))
async def check_single_sub(call: CallbackQuery):
    user_id    = call.from_user.id
    channel_id_str = call.data[len("checksub_"):]
    sub_stars  = float(await get_setting("subscribe_stars") or 0.10)

    ch = await channels.find_one({"channel_id": channel_id_str})
    if not ch:
        await call.answer("Kanal topilmadi!", show_alert=True)
        return

    channel_name = ch["channel_name"]

    try:
        cid       = int(channel_id_str)
        member    = await bot.get_chat_member(cid, user_id)
        is_member = member.status not in ["left", "kicked", "banned"]
    except Exception as e:
        logger.warning(f"Kanal tekshirishda xato {channel_id_str}: {e}")
        await call.answer(f"⚠️ {channel_name} — tekshirib bo'lmadi", show_alert=True)
        return

    bonus_doc = await channel_bonus.find_one({
        "user_id": user_id,
        "channel_id": channel_id_str
    })

    result_text = ""

    if is_member and not bonus_doc:
        try:
            await channel_bonus.insert_one({
                "user_id": user_id,
                "channel_id": channel_id_str,
                "stars_given": sub_stars
            })
            await add_balance(user_id, sub_stars, f"Kanal obuna bonusi: {channel_name}")
            balance = await get_balance(user_id)
            result_text = f"✅ {channel_name}\n➕ +{sub_stars}⭐ qo'shildi!\n💰 Balans: {balance}⭐"
        except DuplicateKeyError:
            result_text = f"✅ {channel_name}\nBonus oldin olingan"
        except Exception as e:
            logger.error(f"Bonus qo'shishda xato {channel_name}: {e}")
            result_text = f"⚠️ {channel_name} — xato yuz berdi"

    elif is_member and bonus_doc:
        result_text = f"✅ {channel_name}\nBonus allaqachon olingan"

    elif not is_member and bonus_doc:
        given = float(bonus_doc.get("stars_given", sub_stars))
        await channel_bonus.delete_one({
            "user_id": user_id,
            "channel_id": channel_id_str
        })
        current_balance = await get_balance(user_id)
        deduct_amount   = min(given, current_balance)
        if deduct_amount > 0:
            await deduct_balance(user_id, deduct_amount, f"Kanal tark etildi: {channel_name}")
            balance = await get_balance(user_id)
            result_text = f"❌ {channel_name}\n-{round(deduct_amount, 2)}⭐ ayirildi\n💰 Balans: {balance}⭐"
        else:
            result_text = f"❌ {channel_name}\nObuna bo'lmagansiz"
    else:
        result_text = f"❌ {channel_name}\nObuna bo'lmagansiz — avval obuna bo'ling!"

    await call.answer(result_text, show_alert=True)
    await render_channels_menu(user_id, call.message)


@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    user_id = call.from_user.id
    chs     = await get_channels()
    if not chs:
        await call.answer("Hozircha kanallar yo'q!", show_alert=True)
        return

    sub_stars = float(await get_setting("subscribe_stars") or 0.10)
    results   = []
    earned    = 0.0
    lost      = 0.0

    for ch in chs:
        channel_id_str = ch["channel_id"]
        channel_name   = ch["channel_name"]

        try:
            cid       = int(channel_id_str)
            member    = await bot.get_chat_member(cid, user_id)
            is_member = member.status not in ["left", "kicked", "banned"]
        except Exception as e:
            logger.warning(f"Kanal tekshirishda xato {channel_id_str}: {e}")
            results.append(f"⚠️ {channel_name} — tekshirib bo'lmadi")
            continue

        bonus_doc = await channel_bonus.find_one({
            "user_id": user_id,
            "channel_id": channel_id_str
        })

        if is_member and not bonus_doc:
            try:
                await channel_bonus.insert_one({
                    "user_id": user_id,
                    "channel_id": channel_id_str,
                    "stars_given": sub_stars
                })
                await add_balance(user_id, sub_stars, f"Kanal obuna bonusi: {channel_name}")
                earned += sub_stars
                results.append(f"✅ {channel_name} — +{sub_stars}⭐ qo'shildi!")
            except DuplicateKeyError:
                results.append(f"✅ {channel_name} — bonus oldin olingan")
            except Exception as e:
                logger.error(f"Bonus qo'shishda xato {channel_name}: {e}")
                results.append(f"⚠️ {channel_name} — xato yuz berdi")

        elif is_member and bonus_doc:
            results.append(f"✅ {channel_name} — bonus oldin olingan")

        elif not is_member and bonus_doc:
            given = float(bonus_doc.get("stars_given", sub_stars))
            await channel_bonus.delete_one({
                "user_id": user_id,
                "channel_id": channel_id_str
            })
            current_balance = await get_balance(user_id)
            deduct_amount   = min(given, current_balance)
            if deduct_amount > 0:
                await deduct_balance(
                    user_id, deduct_amount,
                    f"Kanal tark etildi: {channel_name}"
                )
                lost += deduct_amount
                results.append(f"❌ {channel_name} — -{round(deduct_amount, 2)}⭐ ayirildi")
            else:
                results.append(f"❌ {channel_name} — obuna bo'lmagansiz")
        else:
            results.append(f"❌ {channel_name} — obuna bo'lmagansiz")

    balance = await get_balance(user_id)
    summary = ""
    if earned > 0:
        summary += f"\n\n➕ Jami qo'shildi: +{round(earned, 2)}⭐"
    if lost > 0:
        summary += f"\n➖ Jami ayirildi: -{round(lost, 2)}⭐"
    summary += f"\n💰 Balans: {balance}⭐"

    await call.answer("\n".join(results) + summary, show_alert=True)
    await render_channels_menu(user_id, call.message)


# ===================== SUPPORT =====================

@router.callback_query(F.data == "support")
async def support_menu(call: CallbackQuery):
    await call.message.edit_text(
        f"🆘 <b>Yordam / Muammo</b>\n\n"
        f"Muammo yoki savolingizni yozing, admin tez orada javob beradi.\n\n"
        f"💬 Guruhimiz: {SUPPORT_GROUP}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Xabar yozish", callback_data="support_write")],
            [InlineKeyboardButton(text="💬 Guruhga o'tish", url=SUPPORT_GROUP)],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "support_write")
async def support_write(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    allowed, minutes_left = await check_support_cooldown(user_id)
    if not allowed:
        await call.answer(f"⏳ {minutes_left} daqiqadan keyin yozishingiz mumkin!", show_alert=True)
        return
    await state.set_state(UserStates.support_message)
    await call.message.edit_text(
        "✏️ <b>Muammongizni yozing</b>\n\nBekor qilish: /start",
        reply_markup=back_kb("support"), parse_mode="HTML"
    )
    await call.answer()


@router.message(UserStates.support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_id   = message.from_user.id
    username  = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    allowed, minutes_left = await check_support_cooldown(user_id)
    if not allowed:
        await message.answer(f"⏳ {minutes_left} daqiqadan keyin yozishingiz mumkin!")
        await state.clear()
        return
    uname = f"@{username}" if username else full_name
    await update_support_cooldown(user_id)
    await state.clear()
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🆘 <b>Yordam so'rovi!</b>\n\n"
            f"👤 {uname}\n"
            f"🪪 <code>{user_id}</code>\n\n"
            f"📝 {message.text}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin ga xabar yuborishda xato: {e}")
    await message.answer(
        f"✅ Xabaringiz qabul qilindi!\n"
        f"Admin tez orada javob beradi.\n\n"
        f"👉 {SUPPORT_GROUP}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Guruhga o'tish", url=SUPPORT_GROUP)],
            [InlineKeyboardButton(text="🏠 Bosh menyu", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


# ===================== GIFT =====================

@router.callback_query(F.data == "buy_gift")
async def buy_gift_menu(call: CallbackQuery):
    if not is_working_hours():
        now = datetime.now(TIMEZONE)
        await call.answer(
            f"⏰ Gift olish faqat soat 20:00 — 00:00 da!\nHozir: {now.strftime('%H:%M')}",
            show_alert=True
        )
        return

    user_id   = call.from_user.id
    balance   = await get_balance(user_id)
    ref_count = await get_referral_count(user_id)

    if ref_count < MIN_REFERRALS_FOR_GIFT:
        remaining = MIN_REFERRALS_FOR_GIFT - ref_count
        bot_info  = await bot.get_me()
        ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
        await call.message.edit_text(
            f"🎁 <b>Gift olish</b>\n\n"
            f"❌ Gift olish uchun kamida <b>{MIN_REFERRALS_FOR_GIFT} ta do'st</b> taklif qilishingiz kerak!\n\n"
            f"👥 Siz taklif qilganlar: <b>{ref_count}/{MIN_REFERRALS_FOR_GIFT}</b>\n"
            f"⏳ Yana <b>{remaining} ta do'st</b> kerak\n\n"
            f"🔗 Referral linkingiz:\n<code>{ref_link}</code>\n\n"
            f"Do'stlaringizni taklif qiling va gift oling! 🎁",
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
        return

    await call.message.edit_text(
        f"🎁 <b>Gift olish</b>\n\n"
        f"💰 Balans: <b>{balance}⭐</b>\n"
        f"👥 Referrallar: <b>{ref_count}/{MIN_REFERRALS_FOR_GIFT}</b> ✅\n\n"
        f"✅ = Sotib olish mumkin\n"
        f"❌ = Stars yetarli emas\n\n"
        f"⏰ Buyurtma qabul: <b>20:00 — 00:00</b>\n\n"
        f"Qaysi giftni xohlaysiz? 👇",
        reply_markup=await gifts_keyboard(balance), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("buyg_"))
async def select_gift(call: CallbackQuery):
    if not is_working_hours():
        await call.answer("⏰ Faqat soat 20:00 — 00:00!", show_alert=True)
        return

    user_id   = call.from_user.id
    ref_count = await get_referral_count(user_id)

    if ref_count < MIN_REFERRALS_FOR_GIFT:
        await call.answer(
            f"❌ Gift olish uchun {MIN_REFERRALS_FOR_GIFT} ta referral kerak!\n"
            f"Sizda: {ref_count} ta",
            show_alert=True
        )
        return

    idx = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift    = GIFTS[idx]
    balance = await get_balance(user_id)
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
                InlineKeyboardButton(text="✅ Ha!", callback_data=f"confirmg_{idx}"),
                InlineKeyboardButton(text="❌ Yo'q", callback_data="buy_gift")
            ]
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("confirmg_"))
async def confirm_gift(call: CallbackQuery):
    if not is_working_hours():
        await call.answer("⏰ Faqat soat 20:00 — 00:00!", show_alert=True)
        return

    user_id   = call.from_user.id
    ref_count = await get_referral_count(user_id)

    if ref_count < MIN_REFERRALS_FOR_GIFT:
        await call.answer(
            f"❌ Gift olish uchun {MIN_REFERRALS_FOR_GIFT} ta referral kerak!\n"
            f"Sizda: {ref_count} ta",
            show_alert=True
        )
        return

    if not await check_order_cooldown(user_id):
        await call.answer("⏳ Iltimos, 15 soniya kuting!", show_alert=True)
        return
    is_subbed, not_subbed = await check_subscription(user_id)
    if not is_subbed:
        names = "\n".join([f"❌ {ch['channel_name']}" for ch in not_subbed])
        await call.answer(f"❌ Kanallardan chiqib ketgansiz!\n{names}", show_alert=True)
        return
    idx = int(call.data.split("_")[1])
    if idx >= len(GIFTS):
        await call.answer("Gift topilmadi!", show_alert=True)
        return
    gift    = GIFTS[idx]
    balance = await get_balance(user_id)
    if balance < gift["stars"]:
        await call.answer("❌ Stars yetarli emas!", show_alert=True)
        return
    user      = await get_user(user_id)
    username  = user.get("username") or ""
    full_name = user.get("full_name") or ""
    uname     = f"@{username}" if username else full_name

    ok = await deduct_balance(user_id, gift["stars"], f"Gift buyurtma: {gift['name']}")
    if not ok:
        await call.answer("❌ Stars yetarli emas!", show_alert=True)
        return

    order_id = await add_order(
        user_id, username, full_name,
        gift["name"], gift["emoji"], gift["stars"]
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Yangi gift buyurtma!</b>\n\n"
            f"🆔 Buyurtma: <code>{order_id}</code>\n"
            f"👤 {uname}\n"
            f"🪪 <code>{user_id}</code>\n"
            f"🎁 {gift['emoji']} {gift['name']} — {gift['stars']}⭐\n"
            f"👥 Referrallar: {ref_count}\n"
            f"🕐 {datetime.now(TIMEZONE).strftime('%H:%M')}\n\n"
            f"✅ {uname} ga {gift['stars']} stars gifti yuboring!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Bajarildi",
                    callback_data=f"done_order|{order_id}|{user_id}"
                )]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin xabar yuborishda xato: {e}")

    new_balance = await get_balance(user_id)
    await call.message.edit_text(
        f"✅ <b>Buyurtmangiz qabul qilindi!</b>\n\n"
        f"🎁 {gift['emoji']} <b>{gift['name']}</b> — {gift['stars']}⭐\n\n"
        f"⏳ Admin soat <b>20:00 — 00:00</b> da gift yuboradi.\n\n"
        f"💰 Qolgan balans: <b>{new_balance}⭐</b>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("done_order|"))
async def done_order(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        _, order_id, user_id_str = call.data.split("|")
        user_id = int(user_id_str)
    except Exception:
        await call.answer("❌ Noto'g'ri format!", show_alert=True)
        return

    await complete_order(order_id)
    await admin_log(ADMIN_ID, "complete_order", f"order_id={order_id}, user_id={user_id}")
    try:
        await bot.send_message(
            user_id,
            "🎉 <b>Giftingiz yuborildi!</b>\n\nAdmin sizga gift yubordi. Tekshiring! ✅",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>BAJARILDI</b>",
        parse_mode="HTML"
    )
    await call.answer("✅ Buyurtma bajarildi!")


@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id   = call.from_user.id
    balance   = await get_balance(user_id)
    ref_stars = await get_setting("referral_stars") or "0.25"
    sub_stars = await get_setting("subscribe_stars") or "0.10"
    await call.message.edit_text(
        f"⭐ <b>Stars Gift Bot</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"• Do'st taklif → <b>+{ref_stars}⭐</b>\n"
        f"• Kanal obuna → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'pla → 🎁 Gift ol!\n\n"
        f"⏰ Gift olish vaqti: <b>20:00 — 00:00</b>\n"
        f"👥 Gift uchun kamida <b>{MIN_REFERRALS_FOR_GIFT} ta referral</b> kerak!",
        reply_markup=main_menu(user_id), parse_mode="HTML"
    )
    await call.answer()


# ===================== ADMIN HANDLERS =====================

@router.callback_query(F.data == "admin_panel")
async def admin_panel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    await call.message.edit_text(
        "🔧 <b>Admin Panel</b>",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "admin_orders")
async def admin_orders_handler(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    pending = await get_pending_orders()
    if not pending:
        await call.answer("Kutayotgan buyurtmalar yo'q!", show_alert=True)
        return
    text    = "📦 <b>Kutayotgan buyurtmalar:</b>\n\n"
    buttons = []
    for o in pending:
        oid   = str(o["_id"])
        uname = f"@{o['username']}" if o.get("username") else o.get("full_name", "")
        text += f"{uname} — {o['gift_emoji']} {o['gift_name']} ({o['gift_stars']}⭐)\n"
        buttons.append([InlineKeyboardButton(
            text=f"✅ {uname} — {o['gift_emoji']} {o['gift_stars']}⭐",
            callback_data=f"done_order|{oid}|{o['user_id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "admin_add_channel")
@router.callback_query(F.data == "admin_add_channel")
async def admin_add_channel_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_channel_link)
    await call.message.edit_text(
        "➕ <b>Kanal qo'shish</b>\n\n"
        "Quyidagilardan birini yuboring:\n\n"
        "1️⃣ Kanaldan istalgan xabarni <b>forward qiling</b> (private kanal)\n"
        "2️⃣ <code>@kanalname</code> (public kanal)\n"
        "3️⃣ <code>-1001234567890</code> (kanal ID)\n\n"
        "⚠️ Bot kanalda <b>admin</b> bo'lishi va\n"
        "<b>Admin tasdiqlashini so'rash</b> o'CHIQ bo'lishi kerak!",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.add_channel_link)
@router.message(AdminStates.add_channel_link)
async def process_channel_link(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    channel_id = None
    auto_name = None
    link = None

    # ✅ Forward qilingan xabar orqali ID olish
    if message.forward_from_chat:
        chat = message.forward_from_chat
        channel_id = str(chat.id)
        auto_name = chat.title or "Kanal"
        try:
            link = await bot.export_chat_invite_link(int(channel_id))
        except Exception:
            link = f"https://t.me/c/{channel_id.lstrip('-100')}"

    # Raqamli ID
    elif message.text and message.text.strip().lstrip("-").isdigit():
        raw = message.text.strip()
        try:
            chat = await bot.get_chat(int(raw))
            channel_id = str(chat.id)
            auto_name = chat.title or raw
            try:
                link = await bot.export_chat_invite_link(int(channel_id))
            except Exception:
                link = f"https://t.me/c/{channel_id.lstrip('-100')}"
        except Exception:
            await message.answer(
                "❌ <b>Kanal topilmadi!</b>\n\nBot kanalda admin bo'lishi kerak!",
                reply_markup=back_kb("admin_panel"),
                parse_mode="HTML"
            )
            return

    # Public username / link
    elif message.text:
        raw = message.text.strip()
        if "t.me/+" in raw:
            await message.answer(
                "❌ Bu link ishlamaydi!\n\n"
                "Kanaldan istalgan <b>xabarni forward qiling</b> 👇",
                reply_markup=back_kb("admin_panel"),
                parse_mode="HTML"
            )
            return

        if "t.me/" in raw:
            part = raw.split("t.me/")[-1].split("/")[0]
            username = "@" + part
        elif raw.startswith("@"):
            username = raw
        else:
            username = "@" + raw

        try:
            chat = await bot.get_chat(username)
            channel_id = str(chat.id)
            auto_name = chat.title or username
            link = f"https://t.me/{username.lstrip('@')}"
        except Exception:
            await message.answer(
                "❌ <b>Kanal topilmadi!</b>",
                reply_markup=back_kb("admin_panel"),
                parse_mode="HTML"
            )
            return

    else:
        await message.answer(
            "❌ Tushunmadim!\n\n"
            "Kanaldan xabar <b>forward</b> qiling 👇",
            reply_markup=back_kb("admin_panel"),
            parse_mode="HTML"
        )
        return

    if not channel_id:
        await message.answer(
            "❌ <b>Kanal topilmadi!</b>\n\n"
            "Kanaldan xabar <b>forward</b> qiling!",
            reply_markup=back_kb("admin_panel"),
            parse_mode="HTML"
        )
        return

    await add_channel(channel_id, auto_name, link)
    await admin_log(ADMIN_ID, "add_channel", f"id={channel_id}, name={auto_name}")
    await state.clear()
    await message.answer(
        f"✅ <b>Kanal qo'shildi!</b>\n\n"
        f"📢 {auto_name}\n"
        f"🔗 {link}\n"
        f"🆔 <code>{channel_id}</code>",
        reply_markup=admin_keyboard(),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "admin_remove_channel")
async def admin_remove_channel_handler(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    chs = await get_channels()
    if not chs:
        await call.answer("Kanallar yo'q!", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(
            text=f"🗑 {ch['channel_name']}",
            callback_data=f"delch_{ch['channel_id']}"
        )]
        for ch in chs
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(
        "➖ <b>Qaysi kanalni o'chirish?</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("delch_"))
async def delete_channel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    channel_id = call.data[6:]
    await remove_channel(channel_id)
    await admin_log(ADMIN_ID, "remove_channel", f"id={channel_id}")
    await call.answer("✅ O'chirildi!", show_alert=True)
    chs = await get_channels()
    if not chs:
        await call.message.edit_text(
            "✅ Barcha kanallar o'chirildi.",
            reply_markup=admin_keyboard()
        )
        return
    buttons = [
        [InlineKeyboardButton(
            text=f"🗑 {ch['channel_name']}",
            callback_data=f"delch_{ch['channel_id']}"
        )]
        for ch in chs
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == "admin_set_referral")
async def admin_set_referral(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = await get_setting("referral_stars")
    await state.set_state(AdminStates.set_referral_stars)
    await call.message.edit_text(
        f"⭐ <b>Referral stars</b>\n\nHozirgi: <b>{current}⭐</b>\n\nYangi miqdor kiriting:",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_referral_stars)
async def process_referral_stars(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip())
        if val < 0:
            raise ValueError
        await set_setting("referral_stars", val)
        await admin_log(ADMIN_ID, "set_referral_stars", str(val))
        await state.clear()
        await message.answer(
            f"✅ Referral stars: <b>{val}⭐</b>",
            reply_markup=admin_keyboard(), parse_mode="HTML"
        )
    except Exception:
        await message.answer("❌ To'g'ri raqam kiriting! Masalan: 0.25")


@router.callback_query(F.data == "admin_set_subscribe")
async def admin_set_subscribe(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = await get_setting("subscribe_stars")
    await state.set_state(AdminStates.set_subscribe_stars)
    await call.message.edit_text(
        f"⭐ <b>Obuna stars</b>\n\nHozirgi: <b>{current}⭐</b>\n\nYangi miqdor kiriting:",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_subscribe_stars)
async def process_subscribe_stars(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip())
        if val < 0:
            raise ValueError
        await set_setting("subscribe_stars", val)
        await admin_log(ADMIN_ID, "set_subscribe_stars", str(val))
        await state.clear()
        await message.answer(
            f"✅ Obuna stars: <b>{val}⭐</b>",
            reply_markup=admin_keyboard(), parse_mode="HTML"
        )
    except Exception:
        await message.answer("❌ To'g'ri raqam kiriting! Masalan: 0.10")


@router.callback_query(F.data == "admin_add_balance")
async def admin_add_balance(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_balance_input)
    await call.message.edit_text(
        "💰 <b>Balans qo'shish</b>\n\n"
        "Format: <code>USER_ID MIQDOR</code>\n"
        "Masalan: <code>123456789 10.5</code>",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
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
        if amt <= 0:
            raise ValueError
        if not await get_user(uid):
            await message.answer("❌ Foydalanuvchi topilmadi!")
            return
        await add_balance(uid, amt, "Admin tomonidan qo'shildi")
        await admin_log(ADMIN_ID, "add_balance", f"uid={uid}, amt={amt}")
        await state.clear()
        new_bal = await get_balance(uid)
        await message.answer(
            f"✅ <code>{uid}</code> ga <b>+{amt}⭐</b> qo'shildi!\n"
            f"Yangi balans: <b>{new_bal}⭐</b>",
            reply_markup=admin_keyboard(), parse_mode="HTML"
        )
        try:
            await bot.send_message(
                uid,
                f"💰 Hisobingizga <b>+{amt}⭐</b> qo'shildi!",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception:
        await message.answer(
            "❌ Format: <code>USER_ID MIQDOR</code>\nMasalan: <code>123456789 10.5</code>",
            parse_mode="HTML"
        )


@router.callback_query(F.data == "admin_deduct_balance")
async def admin_deduct_balance_cb(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.deduct_balance_input)
    await call.message.edit_text(
        "💸 <b>Balans ayirish</b>\n\n"
        "Format: <code>USER_ID MIQDOR</code>\n"
        "Masalan: <code>123456789 5.0</code>",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
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
        if amt <= 0:
            raise ValueError
        if not await get_user(uid):
            await message.answer("❌ Foydalanuvchi topilmadi!")
            return
        ok = await deduct_balance(uid, amt, "Admin tomonidan ayirildi")
        await admin_log(ADMIN_ID, "deduct_balance", f"uid={uid}, amt={amt}, ok={ok}")
        await state.clear()
        if not ok:
            await message.answer("❌ Balans yetarli emas!", reply_markup=admin_keyboard())
            return
        new_bal = await get_balance(uid)
        await message.answer(
            f"✅ <code>{uid}</code> dan <b>-{amt}⭐</b> ayirildi!\n"
            f"Yangi balans: <b>{new_bal}⭐</b>",
            reply_markup=admin_keyboard(), parse_mode="HTML"
        )
    except Exception:
        await message.answer(
            "❌ Format: <code>USER_ID MIQDOR</code>\nMasalan: <code>123456789 5.0</code>",
            parse_mode="HTML"
        )


@router.callback_query(F.data == "admin_stats")
async def admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    total_users, total_balance, total_credits, total_gifts, pending_gifts = await get_stats()
    ref_stars = await get_setting("referral_stars")
    sub_stars = await get_setting("subscribe_stars")
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{total_users}</b>\n"
        f"💰 Jami balanslar: <b>{total_balance}⭐</b>\n"
        f"📈 Jami kreditlar: <b>{total_credits}</b>\n"
        f"🎁 Bajarilgan buyurtmalar: <b>{total_gifts}</b>\n"
        f"⏳ Kutayotgan buyurtmalar: <b>{pending_gifts}</b>\n\n"
        f"⚙️ Referral: <b>{ref_stars}⭐</b> | Obuna: <b>{sub_stars}⭐</b>\n"
        f"👥 Gift uchun min referral: <b>{MIN_REFERRALS_FOR_GIFT} ta</b>",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


# ===================== ADMIN USERS (TOP 150, PAGED) =====================

async def show_users_page(message, top_list: list, page: int, edit: bool = True):
    per_page = 50
    total    = len(top_list)
    start    = page * per_page
    end      = min(start + per_page, total)
    chunk    = top_list[start:end]

    text = f"👥 <b>Top {total} foydalanuvchi ({start+1}–{end}):</b>\n\n"
    for i, u in enumerate(chunk, start + 1):
        uname = f"@{u['username']}" if u.get("username") else u.get("full_name", "Noma'lum")
        bal   = round(u.get("balance", 0), 2)
        refs  = u.get("referral_count", 0)
        text += f"{i}. {uname} — <b>{bal}⭐</b> | 👥{refs}\n"

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"ausers_{page-1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"ausers_{page+1}"))

    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin_users")
async def admin_users_handler(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global _users_cache
    _users_cache = await users.find().sort("balance", -1).limit(150).to_list(150)
    await show_users_page(call.message, _users_cache, page=0, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("ausers_"))
async def admin_users_page(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global _users_cache
    page = int(call.data.split("_")[1])
    if not _users_cache:
        _users_cache = await users.find().sort("balance", -1).limit(150).to_list(150)
    await show_users_page(call.message, _users_cache, page=page, edit=True)
    await call.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📣 <b>Broadcast</b>\n\n"
        "Xabar yuboring:\n"
        "• Faqat matn\n"
        "• Rasm + matn (caption)\n"
        "• Video + matn (caption)\n\n"
        "<i>Bekor qilish: /start</i>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()

@router.message(AdminStates.broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()

    user_ids = await get_all_user_ids()
    sent, failed, blocked = 0, 0, 0
    status_msg = await message.answer(f"📣 Yuborilmoqda... 0/{len(user_ids)}")

    async def send_to_user(uid: int) -> str:
        """Xabar yuboradi, xato bo'lsa retry qiladi. 'sent'/'blocked'/'failed' qaytaradi"""
        for attempt in range(3):  # 3 marta urinish
            try:
                if message.photo:
                    await bot.send_photo(
                        uid,
                        photo=message.photo[-1].file_id,
                        caption=f"📣 {message.caption}" if message.caption else "📣",
                        parse_mode="HTML"
                    )
                elif message.video:
                    await bot.send_video(
                        uid,
                        video=message.video.file_id,
                        caption=f"📣 {message.caption}" if message.caption else "📣",
                        parse_mode="HTML"
                    )
                elif message.animation:
                    await bot.send_animation(
                        uid,
                        animation=message.animation.file_id,
                        caption=f"📣 {message.caption}" if message.caption else "📣",
                        parse_mode="HTML"
                    )
                elif message.text:
                    await bot.send_message(
                        uid,
                        f"📣 {message.text}",
                        parse_mode="HTML"
                    )
                return "sent"

            except Exception as e:
                err = str(e).lower()
                # Foydalanuvchi botni bloklagan
                if "blocked" in err or "deactivated" in err or "chat not found" in err:
                    return "blocked"
                # Flood limit — kutib qayta urinish
                if "429" in err or "too many requests" in err:
                    wait = 5 * (attempt + 1)  # 5s, 10s, 15s
                    logger.warning(f"Flood limit! {wait}s kutilmoqda...")
                    await asyncio.sleep(wait)
                    continue
                # Boshqa xato
                if attempt == 2:
                    return "failed"
                await asyncio.sleep(1)
        return "failed"

    for i, uid in enumerate(user_ids):
        result = await send_to_user(uid)
        if result == "sent":
            sent += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1

        # Har 30 da bir status yangilash
        if (i + 1) % 30 == 0:
            try:
                await status_msg.edit_text(
                    f"📣 Yuborilmoqda... {i+1}/{len(user_ids)}\n"
                    f"✅ Yuborildi: {sent}\n"
                    f"🚫 Bloklagan: {blocked}\n"
                    f"❌ Xato: {failed}"
                )
            except Exception:
                pass

        # ✅ To'g'ri delay — Telegram 30 xabar/sekund ruxsat beradi
        await asyncio.sleep(0.1)  # 10 xabar/sekund — xavfsiz

    await admin_log(ADMIN_ID, "broadcast", f"sent={sent}, blocked={blocked}, failed={failed}")
    await status_msg.edit_text(
        f"✅ <b>Broadcast tugadi!</b>\n\n"
        f"✅ Yuborildi: <b>{sent}</b>\n"
        f"🚫 Bloklagan: <b>{blocked}</b>\n"
        f"❌ Xato: <b>{failed}</b>\n"
        f"📊 Jami: <b>{len(user_ids)}</b>",
        parse_mode="HTML"
    )
    await message.answer("🏠 Admin panel:", reply_markup=admin_keyboard())
# ===================== AUTO CHECK SUBSCRIPTIONS =====================
async def auto_check_subscriptions():
    await asyncio.sleep(10)
    while True:
        logger.info("🔄 Avtomatik obuna tekshiruvi boshlandi...")
        chs = await get_channels()
        if chs:
            sub_stars    = float(await get_setting("subscribe_stars") or 0.10)
            all_user_ids = await get_all_user_ids()
            for user_id in all_user_ids:
                for ch in chs:
                    channel_id_str = ch["channel_id"]
                    channel_name   = ch["channel_name"]
                    try:
                        cid       = int(channel_id_str)
                        member    = await bot.get_chat_member(cid, user_id)
                        is_member = member.status not in ["left", "kicked", "banned"]
                    except Exception:
                        continue

                    bonus_doc = await channel_bonus.find_one({
                        "user_id": user_id,
                        "channel_id": channel_id_str
                    })

                    if not is_member and bonus_doc:
                        given = float(bonus_doc.get("stars_given", sub_stars))
                        await channel_bonus.delete_one({
                            "user_id": user_id,
                            "channel_id": channel_id_str
                        })
                        current_balance = await get_balance(user_id)
                        deduct_amount   = min(given, current_balance)
                        if deduct_amount > 0:
                            await deduct_balance(
                                user_id, deduct_amount,
                                f"Kanal tark etildi (auto): {channel_name}"
                            )
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"❌ <b>{channel_name}</b> kanalini tark etgansiz!\n"
                                    f"💸 -{deduct_amount}⭐ ayirildi.",
                                    parse_mode="HTML"
                                )
                            except Exception:
                                pass
                    await asyncio.sleep(0.1)
        logger.info("✅ Avtomatik tekshiruv tugadi.")
        await asyncio.sleep(6 * 3600)
        
async def resolve_channel(link_or_username: str):
    raw = link_or_username.strip()

    # Private invite link (+xxxx) — ishlamaydi
    if "t.me/+" in raw or raw.startswith("+"):
        return None, None

    # Raqamli ID — to'g'ridan to'g'ri
    if raw.lstrip("-").isdigit():
        try:
            chat = await bot.get_chat(int(raw))
            return str(chat.id), chat.title or raw
        except Exception as e:
            logger.warning(f"ID bo'yicha topilmadi: {raw} — {e}")
            return None, None

    # Link yoki username dan username ajratib olamiz
    if "t.me/" in raw:
        part = raw.split("t.me/")[-1].split("/")[0].strip()
        username = "@" + part if not part.startswith("@") else part
    elif raw.startswith("@"):
        username = raw
    else:
        username = "@" + raw

    # Username orqali chat olamiz → IDni avtomatik olamiz
    try:
        chat = await bot.get_chat(username)
        return str(chat.id), chat.title or username
    except Exception as e:
        logger.warning(f"Username topilmadi: {username} — {e}")
        return None, None
# ===================== MAIN =====================
async def main():
    await init_db()
    dp.include_router(router)

    async def health(request):
        return web.Response(text="OK")

    app    = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"✅ Web server port {port} da ishga tushdi!")

    asyncio.create_task(auto_check_subscriptions())

    logger.info("✅ Bot ishga tushdi!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
