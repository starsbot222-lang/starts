import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
import pytz

from aiohttp import web
from typing import Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatJoinRequest, TelegramObject, BotCommand,
)
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramNotFound, TelegramBadRequest
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

MIN_REFERRALS_FOR_GIFT = 3  # default, DB dan o'qiladi
_our_channel_url: str = SUPPORT_GROUP  # admin paneldan o'zgartiriladi
# ======================================================


def ensure_utc(dt: datetime) -> datetime:
    """MongoDB dan kelgan naive datetime larni UTC ga o'tkazadi."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

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
referral_hourly   = mdb["referral_hourly"]
referral_minute   = mdb["referral_minute"]
support_cooldown  = mdb["support_cooldown"]
channel_bonus     = mdb["user_channel_bonus"]
join_requests_col = mdb["join_requests"]
exchange_orders   = mdb["exchange_orders"]
transfers_col     = mdb["transfers"]
contests          = mdb["contests"]
contest_refs_col  = mdb["contest_refs"]


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
    await settings_col.update_one(
        {"key": "min_referrals"},
        {"$setOnInsert": {"key": "min_referrals", "value": "3"}},
        upsert=True
    )
    _pubg_default = json.dumps([[15,5],[30,10],[60,25],[120,50],[240,100]])
    await settings_col.update_one(
        {"key": "pubg_variants"},
        {"$setOnInsert": {"key": "pubg_variants", "value": _pubg_default}},
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
    await referral_minute.create_index(
        "created_at",
        expireAfterSeconds=60
    )
    await users.create_index("is_banned")
    await support_cooldown.create_index(
        "last_sent_at",
        expireAfterSeconds=7200
    )
    await join_requests_col.create_index(
        [("user_id", 1), ("channel_id", 1)], unique=True
    )
    await exchange_orders.create_index("user_id")
    await exchange_orders.create_index("status")
    await exchange_orders.create_index("created_at")
    await transfers_col.create_index("token", unique=True)
    await transfers_col.create_index("from_user_id")
    await contests.create_index("status")
    await contests.create_index("token", sparse=True, unique=True)
    await contest_refs_col.create_index(
        [("contest_id", 1), ("referred_id", 1)], unique=True
    )
    await contest_refs_col.create_index([("contest_id", 1), ("referrer_id", 1)])

    await settings_col.update_one(
        {"key": "our_channel"},
        {"$setOnInsert": {"key": "our_channel", "value": SUPPORT_GROUP}},
        upsert=True
    )
    global _our_channel_url
    oc = await settings_col.find_one({"key": "our_channel"})
    if oc:
        _our_channel_url = oc["value"]

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
        "created_at": datetime.now(timezone.utc)
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
                "last_active": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "user_id": user_id,
                "balance": 0.0,
                "referred_by": referred_by,
                "referral_count": 0,
                "last_order_time": None,
                "joined_at": datetime.now(timezone.utc)
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
        "created_at": datetime.now(timezone.utc)
    })


async def deduct_balance(user_id: int, amount: float, desc: str = "") -> bool:
    result = await users.find_one_and_update(
        {"user_id": user_id, "balance": {"$gte": amount}},
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
        "created_at": datetime.now(timezone.utc)
    })
    return True


async def force_deduct_balance(user_id: int, amount: float, desc: str = ""):
    """Balans yetarli bo'lmasa ham ayiradi (manfiy balansga tushishi mumkin)."""
    await users.update_one({"user_id": user_id}, {"$inc": {"balance": -amount}})
    await transactions.insert_one({
        "user_id": user_id,
        "amount": amount,
        "type": "debit",
        "description": desc,
        "created_at": datetime.now(timezone.utc)
    })


async def get_balance(user_id: int) -> float:
    user = await users.find_one({"user_id": user_id})
    return round(user.get("balance", 0), 2) if user else 0.0


async def get_channels():
    return await channels.find().to_list(length=100)


async def add_channel(channel_id: str, name: str, link: str):
    await channels.update_one(
        {"channel_id": channel_id},
        {"$set": {
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
        "created_at": datetime.now(timezone.utc)
    })
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"last_order_time": datetime.now(timezone.utc)}}
    )
    order_id = str(result.inserted_id)
    return order_id


async def complete_order(order_id: str):
    await orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "done"}}
    )


async def get_pending_orders():
    return await orders.find({"status": "pending"}).sort("created_at", 1).to_list(length=100)


async def is_banned(user_id: int) -> bool:
    user = await users.find_one({"user_id": user_id})
    return bool(user and user.get("is_banned"))


async def ban_user(user_id: int, reason: str = ""):
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": True, "ban_reason": reason}}
    )
    await admin_logs.insert_one({
        "admin_id": 0,
        "action": "auto_ban",
        "details": f"user_id={user_id}, reason={reason}",
        "created_at": datetime.now(timezone.utc)
    })
    logger.warning(f"🚫 User {user_id} banned: {reason}")


async def unban_user(user_id: int):
    await users.update_one(
        {"user_id": user_id},
        {"$set": {"is_banned": False, "ban_reason": ""}}
    )


async def check_referral_abuse(referred_by: int) -> bool:
    # Allaqachon banlangan bo'lsa — hech narsa qilma
    user_doc = await users.find_one({"user_id": referred_by}, {"is_banned": 1})
    if user_doc and user_doc.get("is_banned"):
        return False

    # 1 daqiqada 15 dan ortiq referral → ban
    minute_key = datetime.now(TIMEZONE).strftime("%Y%m%d%H%M")
    min_doc = await referral_minute.find_one_and_update(
        {"referrer_id": referred_by, "minute_key": minute_key},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)}
        },
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    if min_doc["count"] > 15:
        # Faqat birinchi oshib ketganda ban va 1 ta xabar
        if min_doc["count"] == 16:
            await ban_user(referred_by, "1 daqiqada 15+ referral spam")
            u = await users.find_one({"user_id": referred_by}, {"username": 1, "full_name": 1})
            uname = f"@{u['username']}" if u and u.get("username") else str(referred_by)
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚫 <b>Spam aniqlandi!</b>\n\n"
                    f"👤 {uname} (<code>{referred_by}</code>)\n"
                    f"⚡ 1 daqiqada <b>15+ ta</b> odam uning nomidan kirdi\n"
                    f"🔒 Avtomatik ban qilindi!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Bandan chiqarish", callback_data=f"unban_u_{referred_by}")
                    ]]),
                    parse_mode="HTML"
                )
            except Exception:
                pass
        return False

    # Soatiga 5 dan ortiq referral → rad etish (ban emas)
    hour_key = datetime.now(TIMEZONE).strftime("%Y%m%d%H")
    doc = await referral_hourly.find_one_and_update(
        {"referrer_id": referred_by, "hour_key": hour_key},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)}
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
        diff = (datetime.now(timezone.utc) - ensure_utc(last)).total_seconds()
        return diff >= 15
    except Exception:
        return True


async def check_support_cooldown(user_id: int):
    doc = await support_cooldown.find_one({"user_id": user_id})
    if not doc or not doc.get("last_sent_at"):
        return True, 0
    try:
        last = doc["last_sent_at"]
        diff = (datetime.now(timezone.utc) - ensure_utc(last)).total_seconds()
        if diff >= 3600:
            return True, 0
        minutes_left = int((3600 - diff) / 60) + 1
        return False, minutes_left
    except Exception:
        return True, 0


async def update_support_cooldown(user_id: int):
    await support_cooldown.update_one(
        {"user_id": user_id},
        {"$set": {"last_sent_at": datetime.now(timezone.utc)}},
        upsert=True
    )


async def is_member(channel_id_str: str, user_id: int) -> bool:
    try:
        cid = int(channel_id_str)
        member = await bot.get_chat_member(cid, user_id)
        if member.status in ["member", "administrator", "creator", "restricted"]:
            return True
    except Exception as e:
        logger.warning(f"get_chat_member xato {channel_id_str}: {e}")
    # Zaявka (join request) yuborgan bo'lsa ham a'zo sifatida qabul qilamiz
    req = await join_requests_col.find_one({"user_id": user_id, "channel_id": channel_id_str})
    return req is not None

async def check_subscription(user_id: int):
    chs = await get_channels()
    if not chs:
        return True, []
    not_subbed = []
    for ch in chs:
        ok = await is_member(ch["channel_id"], user_id)
        if not ok:
            not_subbed.append(ch)
    return len(not_subbed) == 0, not_subbed


async def get_min_referrals() -> int:
    val = await get_setting("min_referrals")
    try:
        return int(val)
    except Exception:
        return MIN_REFERRALS_FOR_GIFT


async def get_referral_count(user_id: int) -> int:
    user = await users.find_one({"user_id": user_id})
    return user.get("referral_count", 0) if user else 0


async def get_variants(vtype: str) -> list:
    doc = await settings_col.find_one({"key": f"{vtype}_variants"})
    if not doc:
        return []
    try:
        return json.loads(doc["value"])
    except Exception:
        return []


async def set_variant_slot(vtype: str, slot: int, stars: int, amount: float):
    variants = await get_variants(vtype)
    while len(variants) <= slot:
        variants.append([0, 0])
    variants[slot] = [stars, round(amount, 2)]
    await settings_col.update_one(
        {"key": f"{vtype}_variants"},
        {"$set": {"value": json.dumps(variants)}},
        upsert=True
    )


async def add_exchange_order(
    user_id: int, username: str, full_name: str,
    order_type: str, stars: int, amount: float,
    detail1: str, detail2: str
) -> str:
    result = await exchange_orders.insert_one({
        "type": order_type,
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "stars": stars,
        "amount": amount,
        "detail1": detail1,
        "detail2": detail2,
        "status": "pending",
        "created_at": datetime.now(timezone.utc)
    })
    order_id = str(result.inserted_id)
    return order_id


async def complete_exchange_order(order_id: str):
    await exchange_orders.update_one(
        {"_id": ObjectId(order_id)},
        {"$set": {"status": "done"}}
    )


async def create_transfer(from_user_id: int, amount: float) -> str:
    token = secrets.token_hex(6)  # 12 belgili
    await transfers_col.insert_one({
        "token": token,
        "from_user_id": from_user_id,
        "amount": amount,
        "net_amount": round(amount * 0.95, 2),
        "commission": round(amount * 0.05, 2),
        "status": "pending",
        "to_user_id": None,
        "created_at": datetime.now(timezone.utc)
    })
    return token


async def get_transfer(token: str):
    return await transfers_col.find_one({"token": token})


async def use_transfer(token: str, to_user_id: int) -> bool:
    result = await transfers_col.find_one_and_update(
        {"token": token, "status": "pending"},
        {"$set": {"status": "used", "to_user_id": to_user_id, "used_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER
    )
    return result is not None


async def cancel_transfer(token: str, from_user_id: int) -> bool:
    result = await transfers_col.find_one_and_update(
        {"token": token, "from_user_id": from_user_id, "status": "pending"},
        {"$set": {"status": "cancelled"}},
        return_document=ReturnDocument.AFTER
    )
    return result is not None


async def get_pending_exchange_orders():
    return await exchange_orders.find(
        {"status": "pending"}
    ).sort("created_at", 1).to_list(length=100)


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
    add_channel_id       = State()
    set_referral_stars   = State()
    set_subscribe_stars  = State()
    set_min_referrals    = State()
    set_pubg_variant     = State()
    broadcast            = State()
    add_balance_input    = State()
    deduct_balance_input = State()
    search_user          = State()
    send_user_msg        = State()
    slot_contest_setup   = State()
    ref_contest_setup    = State()
    set_our_channel      = State()


class UserStates(StatesGroup):
    support_message   = State()
    pubg_id           = State()
    pubg_nick         = State()
    transfer_amount   = State()


# ===================== BOT & ROUTER =====================
bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(storage=MemoryStorage())
router = Router()

_users_cache: list = []


# ===================== BAN MIDDLEWARE =====================

class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user and user.id != ADMIN_ID:
            banned = await is_banned(user.id)
            if banned:
                if hasattr(event, "answer"):
                    try:
                        await event.answer("🚫 Siz botdan bloklangansiz.")
                    except Exception:
                        pass
                return
        return await handler(event, data)


# ===================== KLAVIATURALAR =====================
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="⭐ Balansim",      callback_data="balance"),
            InlineKeyboardButton(text="👥 Referral",      callback_data="referral"),
        ],
        [InlineKeyboardButton(text="🎁 Gift olish",       callback_data="buy_gift")],
        [InlineKeyboardButton(text="🎰 Kunlik sovg'a",    callback_data="daily_menu")],
        [InlineKeyboardButton(text="📢 Kanallarga obuna", callback_data="channels")],
        [InlineKeyboardButton(text="🎮 PUBG UC",             callback_data="exchange_pubg")],
        [InlineKeyboardButton(text="📋 Transaksiyalar",   callback_data="transactions")],
        [
            InlineKeyboardButton(text="🏆 Reyting",        callback_data="leaderboard"),
            InlineKeyboardButton(text="💸 Stars uzatish",  callback_data="transfer_menu"),
        ],
        [InlineKeyboardButton(text="⏰ Ish vaqti",        callback_data="work_hours")],
        [InlineKeyboardButton(text="🆘 Yordam / Muammo", callback_data="support")],
        [InlineKeyboardButton(text="ℹ️ Bot haqida",      callback_data="about")],
        [InlineKeyboardButton(text="📢 Bizning kanal",   url=_our_channel_url)],
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
        [InlineKeyboardButton(text="👥 Min referral sozla",   callback_data="admin_set_min_refs")],
        [InlineKeyboardButton(text="💰 Balans qo'shish",      callback_data="admin_add_balance")],
        [InlineKeyboardButton(text="💸 Balans ayirish",       callback_data="admin_deduct_balance")],
        [InlineKeyboardButton(text="📦 Gift buyurtmalar",       callback_data="admin_orders")],
        [InlineKeyboardButton(text="💱 Almashtirish buyurtmalari", callback_data="admin_exc_orders")],
        [InlineKeyboardButton(text="🎮 PUBG variantlari",      callback_data="admin_pubg_config")],
        [InlineKeyboardButton(text="🔍 Foydalanuvchi qidirish", callback_data="admin_search_user")],
        [
            InlineKeyboardButton(text="🎰 Slot konkurs",    callback_data="admin_slot_contest"),
            InlineKeyboardButton(text="🏆 Ref konkurs",     callback_data="admin_ref_contest"),
        ],
        [InlineKeyboardButton(text="📊 Statistika",           callback_data="admin_stats")],
        [InlineKeyboardButton(text="📣 Xabar yuborish",       callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar",     callback_data="admin_users")],
        [InlineKeyboardButton(text="🚫 Banlangan userlar",    callback_data="admin_banned")],
        [InlineKeyboardButton(text="📢 Bizning kanal sozla",  callback_data="admin_our_channel")],
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


# ===================== CHANNELS RENDER =====================

async def render_channels_menu(user_id: int, message) -> None:
    """
    Kanallar menyusini render qiladi.
    Oddiy channel_link ishlatiladi (join request yo'q).
    Foydalanuvchi avval bonus olgan bo'lsa ✅, bo'lmasa ❌ ko'rinadi.
    """
    chs       = await get_channels()
    sub_stars = await get_setting("subscribe_stars") or "0.10"

    if not chs:
        await message.edit_text(
            "📢 Hozircha kanallar yo'q.",
            reply_markup=back_kb()
        )
        return

    buttons = []
    for ch in chs:
        channel_id_str = ch["channel_id"]

        # Bonus olganmi?
        bonus_doc = await channel_bonus.find_one({
            "user_id": user_id,
            "channel_id": channel_id_str
        })
        icon = "✅" if bonus_doc else "❌"

        # Oddiy kanal linki
        link = ch.get("channel_link", "")

        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {ch['channel_name']}",
                url=link
            ),
            InlineKeyboardButton(
                text="🔄 Tekshirish",
                callback_data=f"checksub_{channel_id_str}"
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
        f"✅ = Bonus olindi\n"
        f"❌ = Hali bonus olinmagan\n\n"
        f"Kanal nomiga bosib obuna bo'ling,\n"
        f"keyin <b>🔄 Tekshirish</b> tugmasini bosing 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


# ===================== JOIN REQUEST HANDLER =====================

@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest):
    user_id        = event.from_user.id
    channel_id_str = str(event.chat.id)
    try:
        await join_requests_col.update_one(
            {"user_id": user_id, "channel_id": channel_id_str},
            {"$set": {"created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
        logger.info(f"Join request saqlandi: user={user_id}, channel={channel_id_str}")
    except Exception as e:
        logger.error(f"Join request saqlashda xato: {e}")


# ===================== START HANDLER =====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id   = message.from_user.id
    username  = message.from_user.username or ""
    full_name = message.from_user.full_name or ""

    if await is_banned(user_id):
        await message.answer("🚫 Siz botdan bloklangansiz. Muammo bo'lsa admin bilan bog'laning.")
        return

    args = message.text.split()
    referred_by    = None
    transfer_token = None
    slot_token     = None
    if len(args) > 1:
        arg = args[1]
        if arg.startswith("tr_"):
            transfer_token = arg[3:]
        elif arg.startswith("slot_"):
            slot_token = arg[5:]
        else:
            try:
                ref_id = int(arg)
                if ref_id != user_id:
                    referred_by = ref_id
            except ValueError:
                pass

    existing = await get_user(user_id)
    await add_user(user_id, username, full_name, referred_by)

    if not existing and referred_by:
        abuse_ok = await check_referral_abuse(referred_by)
        if abuse_ok:
            ref_stars = float(await get_setting("referral_stars") or 0.25)
            await users.update_one(
                {"user_id": referred_by},
                {"$inc": {"referral_count": 1}}
            )
            await add_balance(referred_by, ref_stars, f"Referral bonus: {user_id}")
            try:
                await bot.send_message(
                    referred_by,
                    f"🎉 Yangi do'stingiz botga qo'shildi!\n"
                    f"➕ <b>+{ref_stars}⭐</b> hisobingizga qo'shildi!",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            # Faol referral konkursga qayd qilish
            ref_contest = await contests.find_one({"type": "referral", "status": "active"})
            if ref_contest:
                try:
                    await contest_refs_col.insert_one({
                        "contest_id": ref_contest["_id"],
                        "referrer_id": referred_by,
                        "referred_id": user_id,
                        "created_at": datetime.now(timezone.utc)
                    })
                except DuplicateKeyError:
                    pass

    if slot_token:
        await _handle_slot_join(message, user_id, username, full_name, slot_token)
        return

    if transfer_token:
        tr = await get_transfer(transfer_token)
        if tr is None or tr["status"] != "pending":
            await message.answer(
                "❌ Bu transfer havolasi yaroqsiz yoki allaqachon ishlatilgan.",
                parse_mode="HTML"
            )
        elif tr["from_user_id"] == user_id:
            await message.answer(
                "❌ O'z transferingizni qabul qila olmaysiz.",
                parse_mode="HTML"
            )
        else:
            net = tr["net_amount"]
            await message.answer(
                f"💸 <b>Stars Transfer</b>\n\n"
                f"Sizga <b>{tr['amount']}⭐</b> jo'natilgan.\n"
                f"5% komissiyadan so'ng: <b>{net}⭐</b>\n\n"
                f"Qabul qilasizmi?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Qabul qilish", callback_data=f"tr_accept_{transfer_token}"),
                    InlineKeyboardButton(text="❌ Rad etish",   callback_data="back_main"),
                ]]),
                parse_mode="HTML"
            )
        return

    balance   = await get_balance(user_id)
    ref_stars = await get_setting("referral_stars") or "0.25"
    sub_stars = await get_setting("subscribe_stars") or "0.10"
    min_refs  = await get_min_referrals()

    await message.answer(
        f"⭐ <b>Stars Gift Bot</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"• Do'st taklif → <b>+{ref_stars}⭐</b>\n"
        f"• Kanal obuna → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'pla → 🎁 Gift ol!\n\n"
        f"⏰ Gift olish vaqti: <b>20:00 — 00:00</b>\n"
        f"👥 Gift uchun kamida <b>{min_refs} ta referral</b> kerak!",
        reply_markup=main_menu(user_id),
        parse_mode="HTML"
    )


# ===================== WORK HOURS =====================

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer("❌ Bekor qilindi.", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")
    else:
        await message.answer("🏠 Bosh sahifa:", reply_markup=main_menu(message.from_user.id), parse_mode="HTML")


@router.message(Command("balansim"))
async def cmd_balansim(message: Message):
    user_id = message.from_user.id
    balance = await get_balance(user_id)
    can_daily, can_spin, d_left, s_left = await _daily_status(user_id)
    daily_txt = "✅ Tayyor" if can_daily else f"⏳ {_fmt_seconds(d_left)}"
    spin_txt  = "✅ Tayyor" if can_spin  else f"⏳ {_fmt_seconds(s_left)}"
    await message.answer(
        f"⭐ <b>Balansingiz</b>\n\n"
        f"💰 Balans: <b>{balance}⭐</b>\n\n"
        f"🎁 Kunlik bonus: {daily_txt}\n"
        f"🎰 Omad g'ildiragi: {spin_txt}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Kunlik sovg'alar", callback_data="daily_menu")],
            [InlineKeyboardButton(text="🔙 Bosh sahifa",      callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "about")
async def about_handler(call: CallbackQuery):
    ref_stars = await get_setting("referral_stars") or "0.25"
    sub_stars = await get_setting("subscribe_stars") or "0.10"
    min_refs  = await get_min_referrals()
    await call.message.edit_text(
        f"ℹ️ <b>Stars Gift Bot haqida</b>\n\n"
        f"Bu bot orqali bepul ⭐ Stars yig'ib, Telegram Gift va boshqa mukofotlarga almashtirishingiz mumkin!\n\n"
        f"<b>Qanday ishlaydi?</b>\n"
        f"👥 Do'st taklif qiling → <b>+{ref_stars}⭐</b>\n"
        f"📢 Kanallarga obuna bo'ling → <b>+{sub_stars}⭐</b>\n"
        f"⭐ Stars yig'ing → kerakli miqdorga yeting\n\n"
        f"<b>Nimalarga almashtirasiz?</b>\n"
        f"🎁 Telegram Gift (15⭐ dan)\n"
        f"🎮 PUBG UC — ID orqali\n\n"
        f"<b>Shartlar:</b>\n"
        f"👥 Gift uchun kamida <b>{min_refs} ta referral</b>\n"
        f"⏰ Gift vaqti: <b>20:00 — 00:00</b>\n"
        f"🔒 Spam qilganda avtomatik ban\n\n"
        f"<b>Qo'llab-quvvatlash:</b>\n"
        f"💬 {SUPPORT_GROUP}",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "leaderboard")
async def leaderboard_handler(call: CallbackQuery):
    top = await users.find(
        {"is_banned": {"$ne": True}}
    ).sort("referral_count", -1).limit(10).to_list(10)

    if not top:
        await call.answer("Hali ma'lumot yo'q.", show_alert=True)
        return

    lines = ["🏆 <b>TOP-10 Referral Reytingi</b>\n"]
    medals = ["🥇", "🥈", "🥉"] + ["4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, u in enumerate(top):
        name  = u.get("full_name") or u.get("username") or f"User {u['user_id']}"
        count = u.get("referral_count", 0)
        lines.append(f"{medals[i]} <b>{name}</b> — {count} ta referral")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


# ===================== P2P TRANSFER =====================

@router.callback_query(F.data == "transfer_menu")
async def transfer_menu(call: CallbackQuery):
    balance = await get_balance(call.from_user.id)
    await call.message.edit_text(
        f"💸 <b>Stars Uzatish (P2P)</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"Do'stingizga Stars yuboring.\n"
        f"• 5% komissiya olinadi\n"
        f"• Minimal miqdor: <b>10⭐</b>\n"
        f"• Havola 24 soat amal qiladi\n\n"
        f"Necha yulduz jo'natmoqchisiz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="10⭐",  callback_data="tr_quick_10"),
                InlineKeyboardButton(text="25⭐",  callback_data="tr_quick_25"),
                InlineKeyboardButton(text="50⭐",  callback_data="tr_quick_50"),
            ],
            [
                InlineKeyboardButton(text="100⭐", callback_data="tr_quick_100"),
                InlineKeyboardButton(text="200⭐", callback_data="tr_quick_200"),
                InlineKeyboardButton(text="500⭐", callback_data="tr_quick_500"),
            ],
            [InlineKeyboardButton(text="✏️ Boshqa miqdor", callback_data="tr_custom")],
            [InlineKeyboardButton(text="🔙 Ortga",         callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "tr_custom")
async def transfer_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.transfer_amount)
    await call.message.edit_text(
        "✏️ Miqdorni kiriting (kamida 10⭐):\n\n<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("tr_quick_"))
async def transfer_quick(call: CallbackQuery):
    try:
        amount = float(call.data.split("_")[2])
    except (IndexError, ValueError):
        await call.answer("Xato!", show_alert=True)
        return
    await _process_transfer_create(call.message, call.from_user.id, amount, edit=True)
    await call.answer()


@router.message(UserStates.transfer_amount)
async def transfer_amount_input(message: Message, state: FSMContext):
    try:
        amount = float(message.text.strip())
    except ValueError:
        await message.answer("❌ Raqam kiriting. Masalan: 50")
        return
    if amount < 10:
        await message.answer("❌ Minimal miqdor 10⭐.")
        return
    await state.clear()
    await _process_transfer_create(message, message.from_user.id, amount, edit=False)


async def _process_transfer_create(msg, user_id: int, amount: float, edit: bool):
    balance = await get_balance(user_id)
    if balance < amount:
        text = f"❌ Balansingiz yetarli emas.\n💰 Balans: <b>{balance}⭐</b>, kerak: <b>{amount}⭐</b>"
        if edit:
            await msg.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
        return

    net = round(amount * 0.95, 2)
    commission = round(amount * 0.05, 2)

    ok = await deduct_balance(user_id, amount, f"Transfer yaratildi: -{amount}⭐")
    if not ok:
        text = "❌ Balansni ayirishda xato. Qayta urinib ko'ring."
        if edit:
            await msg.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
        else:
            await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
        return

    token = await create_transfer(user_id, amount)
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=tr_{token}"

    text = (
        f"✅ <b>Transfer havolasi yaratildi!</b>\n\n"
        f"💸 Jo'natildi: <b>{amount}⭐</b>\n"
        f"🤝 Do'stingiz oladi: <b>{net}⭐</b>\n"
        f"📊 Komissiya: <b>{commission}⭐</b>\n\n"
        f"🔗 Havola:\n<code>{link}</code>\n\n"
        f"<i>Havola 24 soat amal qiladi. Agar ishlatilmasa, /cancel_tr_{token} buyrug'i bilan bekor qilishingiz mumkin.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"tr_cancel_{token}")],
        [InlineKeyboardButton(text="🏠 Bosh sahifa",  callback_data="back_main")],
    ])
    if edit:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("tr_accept_"))
async def transfer_accept(call: CallbackQuery):
    token = call.data[len("tr_accept_"):]
    to_user_id = call.from_user.id

    tr = await get_transfer(token)
    if tr is None or tr["status"] != "pending":
        await call.answer("❌ Transfer yaroqsiz yoki allaqachon ishlatilgan.", show_alert=True)
        return
    if tr["from_user_id"] == to_user_id:
        await call.answer("❌ O'z transferingizni qabul qila olmaysiz.", show_alert=True)
        return

    ok = await use_transfer(token, to_user_id)
    if not ok:
        await call.answer("❌ Transfer ishlatib bo'lindi.", show_alert=True)
        return

    net = tr["net_amount"]
    await add_balance(to_user_id, net, f"Transfer qabul qilindi: +{net}⭐")

    try:
        await bot.send_message(
            tr["from_user_id"],
            f"✅ Transferingiz qabul qilindi!\n"
            f"💸 <b>{tr['amount']}⭐</b> jo'natildi.",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await call.message.edit_text(
        f"✅ <b>Qabul qilindi!</b>\n\n"
        f"💰 Hisobingizga <b>+{net}⭐</b> qo'shildi.",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("tr_cancel_"))
async def transfer_cancel(call: CallbackQuery):
    token = call.data[len("tr_cancel_"):]
    user_id = call.from_user.id

    tr = await get_transfer(token)
    if tr is None:
        await call.answer("❌ Transfer topilmadi.", show_alert=True)
        return
    if tr["from_user_id"] != user_id:
        await call.answer("❌ Bu sizning transferingiz emas.", show_alert=True)
        return
    if tr["status"] != "pending":
        await call.answer("❌ Bu transfer allaqachon tugagan.", show_alert=True)
        return

    ok = await cancel_transfer(token, user_id)
    if not ok:
        await call.answer("❌ Bekor qilishda xato.", show_alert=True)
        return

    await add_balance(user_id, tr["amount"], f"Transfer bekor qilindi: +{tr['amount']}⭐")
    await call.message.edit_text(
        f"✅ Transfer bekor qilindi.\n"
        f"💰 <b>+{tr['amount']}⭐</b> qaytarildi.",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "work_hours")
async def work_hours_info(call: CallbackQuery):
    now    = datetime.now(TIMEZONE)
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


# ===================== BALANCE =====================

@router.callback_query(F.data == "balance")
async def show_balance(call: CallbackQuery):
    user_id   = call.from_user.id
    balance   = await get_balance(user_id)
    user      = await get_user(user_id)
    ref_count = user.get("referral_count", 0) if user else 0
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
    min_refs  = await get_min_referrals()

    ref_status = (
        f"✅ Gift olish mumkin (👥 {ref_count}/{min_refs})"
        if ref_count >= min_refs
        else f"❌ Gift uchun yana {min_refs - ref_count} ta do'st kerak"
    )

    await call.message.edit_text(
        f"💰 <b>Balansingiz: {balance}⭐</b>\n\n"
        f"👥 Taklif qilganlar: <b>{ref_count}/{min_refs} kishi</b>\n"
        f"{ref_status}\n\n"
        f"🔗 Referral linkingiz:\n<code>{ref_link}</code>",
        reply_markup=back_kb(), parse_mode="HTML"
    )
    await call.answer()


# ===================== REFERRAL =====================

@router.callback_query(F.data == "referral")
async def show_referral(call: CallbackQuery):
    user_id   = call.from_user.id
    user      = await get_user(user_id)
    ref_count = user.get("referral_count", 0) if user else 0
    ref_stars = await get_setting("referral_stars") or "0.25"
    balance   = await get_balance(user_id)
    bot_info  = await bot.get_me()
    ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
    min_refs  = await get_min_referrals()

    remaining     = max(0, min_refs - ref_count)
    progress_text = (
        f"✅ <b>Gift olish huquqingiz bor!</b>"
        if remaining == 0
        else f"⏳ Gift olish uchun yana <b>{remaining} ta do'st</b> taklif qiling"
    )

    await call.message.edit_text(
        f"👥 <b>Referral tizimi</b>\n\n"
        f"🔗 Sizning linkingiz:\n<code>{ref_link}</code>\n\n"
        f"🎁 Har bir do'st uchun: <b>+{ref_stars}⭐</b>\n"
        f"👤 Taklif qilganlar: <b>{ref_count}/{min_refs} kishi</b>\n"
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


# ===================== TRANSACTIONS =====================

@router.callback_query(F.data == "transactions")
async def show_transactions(call: CallbackQuery):
    user_id = call.from_user.id
    txs     = await transactions.find(
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
    await call.answer()
    await render_channels_menu(call.from_user.id, call.message)


_check_sub_cooldown: dict[int, float] = {}  # user_id → last_check timestamp

@router.callback_query(F.data == "check_sub")
async def check_all_subs(call: CallbackQuery):
    """Barcha kanallarni tekshirish."""
    import time
    user_id = call.from_user.id
    now_ts  = time.monotonic()
    last    = _check_sub_cooldown.get(user_id, 0)
    if now_ts - last < 30:
        left = int(30 - (now_ts - last))
        await call.answer(f"⏳ {left} soniyadan so'ng tekshiring.", show_alert=True)
        return
    _check_sub_cooldown[user_id] = now_ts

    chs       = await get_channels()
    sub_stars = float(await get_setting("subscribe_stars") or 0.10)

    if not chs:
        await call.answer("Kanallar yo'q!", show_alert=True)
        return

    given_count = 0
    for ch in chs:
        channel_id_str = ch["channel_id"]
        channel_name   = ch["channel_name"]

        bonus_doc = await channel_bonus.find_one({
            "user_id": user_id,
            "channel_id": channel_id_str
        })
        if bonus_doc:
            given_count += 1
            continue

        ok = await is_member(channel_id_str, user_id)
        if ok:
            try:
                await channel_bonus.insert_one({
                    "user_id": user_id,
                    "channel_id": channel_id_str,
                    "stars_given": sub_stars
                })
                await add_balance(user_id, sub_stars, f"Kanal obuna bonusi: {channel_name}")
                given_count += 1
            except DuplicateKeyError:
                given_count += 1
            except Exception as e:
                logger.error(f"check_all_subs bonus xato: {e}")

    total = len(chs)
    await call.answer(
        f"✅ {given_count}/{total} ta kanaldan bonus olindingiz!",
        show_alert=True
    )
    await render_channels_menu(user_id, call.message)


@router.callback_query(F.data.startswith("checksub_"))
async def check_single_sub(call: CallbackQuery):
    """Bitta kanal tekshirish tugmasi."""
    user_id        = call.from_user.id
    channel_id_str = call.data[len("checksub_"):]
    sub_stars      = float(await get_setting("subscribe_stars") or 0.10)

    ch = await channels.find_one({"channel_id": channel_id_str})
    if not ch:
        await call.answer("Kanal topilmadi!", show_alert=True)
        return

    channel_name = ch["channel_name"]

    bonus_doc = await channel_bonus.find_one({
        "user_id": user_id,
        "channel_id": channel_id_str
    })
    if bonus_doc:
        await call.answer(f"✅ {channel_name} — bonus allaqachon olingan", show_alert=True)
        await render_channels_menu(user_id, call.message)
        return

    ok = await is_member(channel_id_str, user_id)
    if not ok:
        await call.answer(
            f"❌ {channel_name}\n"
            f"Avval kanalga obuna bo'ling! 👆",
            show_alert=True
        )
        return

    try:
        await channel_bonus.insert_one({
            "user_id": user_id,
            "channel_id": channel_id_str,
            "stars_given": sub_stars
        })
        await add_balance(user_id, sub_stars, f"Kanal obuna bonusi: {channel_name}")
        balance = await get_balance(user_id)
        await call.answer(
            f"✅ {channel_name}\n"
            f"➕ +{sub_stars}⭐ qo'shildi!\n"
            f"💰 Balans: {balance}⭐",
            show_alert=True
        )
    except DuplicateKeyError:
        await call.answer(f"✅ {channel_name} — bonus oldin olingan", show_alert=True)
    except Exception as e:
        logger.error(f"checksub_ bonus xato: {e}")
        await call.answer("❌ Xato yuz berdi, qayta urinib ko'ring!", show_alert=True)

    await render_channels_menu(user_id, call.message)


# ===================== SUPPORT =====================

@router.callback_query(F.data == "support")
async def support_menu(call: CallbackQuery):
    await call.message.edit_text(
        f"🆘 <b>Yordam / Muammo</b>\n\n"
        f"Muammo yoki savolingizni yozing, admin tez orada javob beradi.\n\n"
        f"💬 Guruhimiz: {SUPPORT_GROUP}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Xabar yozish",   callback_data="support_write")],
            [InlineKeyboardButton(text="💬 Guruhga o'tish",  url=SUPPORT_GROUP)],
            [InlineKeyboardButton(text="📢 Bizning kanal",   url="https://t.me/starsChannelpy")],
            [InlineKeyboardButton(text="🔙 Ortga",           callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "support_write")
async def support_write(call: CallbackQuery, state: FSMContext):
    user_id          = call.from_user.id
    allowed, minutes = await check_support_cooldown(user_id)
    if not allowed:
        await call.answer(f"⏳ {minutes} daqiqadan keyin yozishingiz mumkin!", show_alert=True)
        return
    await state.set_state(UserStates.support_message)
    await call.message.edit_text(
        "✏️ <b>Muammongizni yozing</b>\n\nBekor qilish: /start",
        reply_markup=back_kb("support"), parse_mode="HTML"
    )
    await call.answer()


@router.message(UserStates.support_message)
async def process_support_message(message: Message, state: FSMContext):
    user_id          = message.from_user.id
    username         = message.from_user.username or ""
    full_name        = message.from_user.full_name or ""
    allowed, minutes = await check_support_cooldown(user_id)
    if not allowed:
        await message.answer(f"⏳ {minutes} daqiqadan keyin yozishingiz mumkin!")
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
            [InlineKeyboardButton(text="🏠 Bosh menyu",     callback_data="back_main")],
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
    min_refs  = await get_min_referrals()

    if ref_count < min_refs:
        remaining = min_refs - ref_count
        bot_info  = await bot.get_me()
        ref_link  = f"https://t.me/{bot_info.username}?start={user_id}"
        await call.message.edit_text(
            f"🎁 <b>Gift olish</b>\n\n"
            f"❌ Gift olish uchun kamida <b>{min_refs} ta do'st</b> taklif qilishingiz kerak!\n\n"
            f"👥 Siz taklif qilganlar: <b>{ref_count}/{min_refs}</b>\n"
            f"⏳ Yana <b>{remaining} ta do'st</b> kerak\n\n"
            f"🔗 Referral linkingiz:\n<code>{ref_link}</code>",
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
        f"👥 Referrallar: <b>{ref_count}/{min_refs}</b> ✅\n\n"
        f"✅ = Sotib olish mumkin\n"
        f"❌ = Stars yetarli emas\n\n"
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
    min_refs  = await get_min_referrals()

    if ref_count < min_refs:
        await call.answer(
            f"❌ Gift olish uchun {min_refs} ta referral kerak!\nSizda: {ref_count} ta",
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
    min_refs  = await get_min_referrals()

    if ref_count < min_refs:
        await call.answer(
            f"❌ Gift olish uchun {min_refs} ta referral kerak!\nSizda: {ref_count} ta",
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

    order_id = await add_order(user_id, username, full_name, gift["name"], gift["emoji"], gift["stars"])

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


# ===================== PUBG UC EXCHANGE =====================

@router.callback_query(F.data == "exchange_pubg")
async def exchange_pubg_menu(call: CallbackQuery):
    user_id  = call.from_user.id
    balance  = await get_balance(user_id)
    variants = await get_variants("pubg")
    if not variants or all(v[0] == 0 for v in variants):
        await call.answer("Hozircha PUBG UC mavjud emas!", show_alert=True)
        return
    buttons = []
    for i, (stars, uc) in enumerate(variants):
        if stars <= 0:
            continue
        mark = "✅" if balance >= stars else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {stars}⭐ = {uc} UC",
            callback_data=f"pubg_buy_{i}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")])
    await call.message.edit_text(
        f"🎮 <b>PUBG UC sotib olish</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"✅ = Yetarli | ❌ = Stars kam\n\n"
        f"Variantni tanlang 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("pubg_buy_"))
async def pubg_select_variant(call: CallbackQuery, state: FSMContext):
    try:
        slot = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        await call.answer("❌ Xato!", show_alert=True)
        return
    variants = await get_variants("pubg")
    if slot >= len(variants):
        await call.answer("Variant topilmadi!", show_alert=True)
        return
    stars, uc = variants[slot]
    if stars <= 0:
        await call.answer("Bu variant faol emas!", show_alert=True)
        return
    balance = await get_balance(call.from_user.id)
    if balance < stars:
        await call.answer(f"❌ Stars yetarli emas!\nKerak: {stars}⭐\nBalans: {balance}⭐", show_alert=True)
        return
    await state.update_data(pubg_stars=stars, pubg_uc=uc)
    await state.set_state(UserStates.pubg_id)
    await call.message.edit_text(
        f"🎮 <b>{stars}⭐ → {uc} UC</b>\n\n"
        f"PUBG ID raqamingizni yuboring:\n"
        f"<i>Masalan: 5123456789</i>\n\n"
        f"Bekor qilish: /start",
        reply_markup=back_kb("exchange_pubg"), parse_mode="HTML"
    )
    await call.answer()


@router.message(UserStates.pubg_id)
async def pubg_get_id(message: Message, state: FSMContext):
    uid = message.text.strip() if message.text else ""
    if not uid.isdigit() or not (5 <= len(uid) <= 15):
        await message.answer("❌ PUBG ID faqat raqamlardan iborat bo'lishi kerak (5-15 ta)!\nQaytadan yuboring:")
        return
    await state.update_data(pubg_player_id=uid)
    await state.set_state(UserStates.pubg_nick)
    await message.answer(
        f"✅ ID: <code>{uid}</code>\n\n"
        f"PUBG nicknameingizni yuboring:\n"
        f"<i>Masalan: ProPlayer123</i>",
        parse_mode="HTML"
    )


@router.message(UserStates.pubg_nick)
async def pubg_get_nick(message: Message, state: FSMContext):
    nick = message.text.strip() if message.text else ""
    if len(nick) < 3 or len(nick) > 30:
        await message.answer("❌ Nickname 3-30 ta belgidan iborat bo'lishi kerak!\nQaytadan yuboring:")
        return
    data      = await state.get_data()
    stars     = data["pubg_stars"]
    uc        = data["pubg_uc"]
    player_id = data["pubg_player_id"]
    user_id   = message.from_user.id
    username  = message.from_user.username or ""
    full_name = message.from_user.full_name or ""
    uname     = f"@{username}" if username else full_name

    ok = await deduct_balance(user_id, stars, f"PUBG UC: {stars}⭐ → {uc} UC")
    if not ok:
        await state.clear()
        await message.answer("❌ Stars yetarli emas! Balans o'zgardi.")
        return

    order_id = await add_exchange_order(
        user_id, username, full_name, "pubg", stars, uc, player_id, nick
    )
    await state.clear()

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🎮 <b>PUBG UC so'rovi!</b>\n\n"
            f"🆔 Buyurtma: <code>{order_id}</code>\n"
            f"👤 {uname} | <code>{user_id}</code>\n"
            f"⭐ {stars}⭐ → <b>{uc} UC</b>\n"
            f"🆔 PUBG ID: <code>{player_id}</code>\n"
            f"🎮 Nickname: <b>{nick}</b>\n"
            f"🕐 {datetime.now(TIMEZONE).strftime('%H:%M')}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✅ UC yuborildi",
                    callback_data=f"done_exc|{order_id}|{user_id}"
                )
            ]]),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin ga PUBG buyurtma yuborishda xato: {e}")

    new_balance = await get_balance(user_id)
    await message.answer(
        f"✅ <b>So'rov qabul qilindi!</b>\n\n"
        f"🎮 <b>{stars}⭐ → {uc} UC</b>\n"
        f"🆔 PUBG ID: {player_id}\n"
        f"🎮 Nickname: {nick}\n\n"
        f"⏳ Admin tez orada UC yuboradi.\n"
        f"💰 Qolgan balans: <b>{new_balance}⭐</b>",
        reply_markup=back_kb(), parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("done_exc|"))
async def done_exchange(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    try:
        parts    = call.data.split("|")
        order_id = parts[1]
        user_id  = int(parts[2])
    except Exception:
        await call.answer("❌ Noto'g'ri format!", show_alert=True)
        return

    order = await exchange_orders.find_one({"_id": ObjectId(order_id)})
    if not order:
        await call.answer("Buyurtma topilmadi!", show_alert=True)
        return
    if order["status"] == "done":
        await call.answer("Bu buyurtma allaqachon bajarilgan!", show_alert=True)
        return

    await complete_exchange_order(order_id)
    await admin_log(ADMIN_ID, "complete_exchange", f"order_id={order_id}, type={order['type']}")

    user_msg = (
            f"✅ <b>PUBG UC hisobingizga yuborildi!</b>\n\n"
            f"🎮 {order['stars']}⭐ → <b>{order['amount']} UC</b>\n"
            f"🆔 ID: {order['detail1']}"
        )
    try:
        await bot.send_message(user_id, user_msg, parse_mode="HTML")
    except Exception:
        pass
    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>BAJARILDI</b>",
        parse_mode="HTML"
    )
    await call.answer("✅ Bajarildi!")


@router.callback_query(F.data == "back_main")
async def back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id   = call.from_user.id
    await users.update_one({"user_id": user_id}, {"$set": {"last_active": datetime.now(timezone.utc)}})
    balance   = await get_balance(user_id)
    ref_stars = await get_setting("referral_stars") or "0.25"
    sub_stars = await get_setting("subscribe_stars") or "0.10"
    min_refs  = await get_min_referrals()
    await call.message.edit_text(
        f"⭐ <b>Stars Gift Bot</b>\n\n"
        f"💰 Balansingiz: <b>{balance}⭐</b>\n\n"
        f"• Do'st taklif → <b>+{ref_stars}⭐</b>\n"
        f"• Kanal obuna → <b>+{sub_stars}⭐</b>\n"
        f"• Stars to'pla → 🎁 Gift ol!\n\n"
        f"⏰ Gift olish vaqti: <b>20:00 — 00:00</b>\n"
        f"👥 Gift uchun kamida <b>{min_refs} ta referral</b> kerak!",
        reply_markup=main_menu(user_id), parse_mode="HTML"
    )
    await call.answer()


# ===================== KUNLIK SOVGA + OMAD G'ILDIRAGI =====================

SPIN_PRIZES = [
    {"amount": 0.10, "label": "0.1⭐",  "weight": 40},
    {"amount": 0.25, "label": "0.25⭐", "weight": 30},
    {"amount": 0.50, "label": "0.5⭐",  "weight": 15},
    {"amount": 1.00, "label": "1⭐",    "weight": 10},
    {"amount": 2.00, "label": "2⭐",    "weight": 4},
    {"amount": 5.00, "label": "5⭐",    "weight": 1},
]

def _spin_weighted_random() -> dict:
    import random
    pool = []
    for prize in SPIN_PRIZES:
        pool.extend([prize] * prize["weight"])
    return random.choice(pool)


async def _daily_status(user_id: int) -> tuple[bool, bool, int, int]:
    """(can_daily, can_spin, daily_secs_left, spin_secs_left)"""
    now = datetime.now(timezone.utc)
    u = await users.find_one({"user_id": user_id}, {"last_daily_at": 1, "last_spin_at": 1})
    if not u:
        return True, True, 0, 0

    daily_last = ensure_utc(u.get("last_daily_at"))
    spin_last  = ensure_utc(u.get("last_spin_at"))
    day = timedelta(hours=24)

    can_daily = (now - daily_last) >= day
    can_spin  = (now - spin_last)  >= day
    daily_left = max(0, int((daily_last + day - now).total_seconds())) if not can_daily else 0
    spin_left  = max(0, int((spin_last  + day - now).total_seconds())) if not can_spin  else 0
    return can_daily, can_spin, daily_left, spin_left


def _fmt_seconds(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}s {m}d {s}s" if h else f"{m}d {s}s"


@router.callback_query(F.data == "daily_menu")
async def daily_menu_handler(call: CallbackQuery):
    user_id = call.from_user.id
    can_daily, can_spin, d_left, s_left = await _daily_status(user_id)

    daily_txt = "✅ Tayyor!" if can_daily else f"⏳ {_fmt_seconds(d_left)}"
    spin_txt  = "✅ Tayyor!" if can_spin  else f"⏳ {_fmt_seconds(s_left)}"

    buttons = []
    if can_daily:
        buttons.append([InlineKeyboardButton(text="🎁 Kunlik bonus olish (+0.5⭐)", callback_data="claim_daily")])
    else:
        buttons.append([InlineKeyboardButton(text=f"🎁 Kunlik bonus — {daily_txt}", callback_data="daily_menu")])

    if can_spin:
        buttons.append([InlineKeyboardButton(text="🎰 Omad g'ildiragini aylantir!", callback_data="claim_spin")])
    else:
        buttons.append([InlineKeyboardButton(text=f"🎰 Omad g'ildiragi — {spin_txt}", callback_data="daily_menu")])

    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="back_main")])

    await call.message.edit_text(
        "🎰 <b>Kunlik Sovg'alar</b>\n\n"
        f"🎁 <b>Kunlik bonus:</b> {daily_txt}\n"
        f"   Har 24 soatda +0.5⭐ bepul!\n\n"
        f"🎰 <b>Omad g'ildiragi:</b> {spin_txt}\n"
        f"   0.1⭐ dan 5⭐ gacha yutib olish!\n\n"
        f"<i>Har kuni qaytib keling — har kuni sovg'a!</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data == "claim_daily")
async def claim_daily_handler(call: CallbackQuery):
    user_id = call.from_user.id
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(hours=24)

    result = await users.find_one_and_update(
        {"user_id": user_id, "$or": [
            {"last_daily_at": {"$exists": False}},
            {"last_daily_at": None},
            {"last_daily_at": {"$lt": cutoff}},
        ]},
        {"$set": {"last_daily_at": now}},
        return_document=ReturnDocument.AFTER
    )
    if result is None:
        _, _, d_left, _ = await _daily_status(user_id)
        await call.answer(f"⏳ Kunlik bonus {_fmt_seconds(d_left)} dan so'ng!", show_alert=True)
        return

    await add_balance(user_id, 0.5, "Kunlik bonus")
    balance = await get_balance(user_id)
    await call.message.edit_text(
        "🎁 <b>Kunlik Bonus!</b>\n\n"
        f"🎉 <b>+0.5⭐</b> hisobingizga qo'shildi!\n"
        f"💰 Yangi balans: <b>{balance}⭐</b>\n\n"
        f"<i>Ertaga yana keling!</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎰 Omad g'ildiragini aylantir!", callback_data="claim_spin")],
            [InlineKeyboardButton(text="🔙 Ortga", callback_data="daily_menu")],
        ]),
        parse_mode="HTML"
    )
    await call.answer("🎁 +0.5⭐ qo'shildi!")


@router.callback_query(F.data == "claim_spin")
async def claim_spin_handler(call: CallbackQuery):
    user_id = call.from_user.id
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(hours=24)

    result = await users.find_one_and_update(
        {"user_id": user_id, "$or": [
            {"last_spin_at": {"$exists": False}},
            {"last_spin_at": None},
            {"last_spin_at": {"$lt": cutoff}},
        ]},
        {"$set": {"last_spin_at": now}},
        return_document=ReturnDocument.AFTER
    )
    if result is None:
        _, _, _, s_left = await _daily_status(user_id)
        await call.answer(f"⏳ G'ildiraq {_fmt_seconds(s_left)} dan so'ng!", show_alert=True)
        return

    prize = _spin_weighted_random()
    await add_balance(user_id, prize["amount"], f"Omad g'ildiragi: {prize['label']}")
    balance = await get_balance(user_id)

    reel = "🍒 🍋 ⭐ 🎰 💫 🎁 🌟 ⚡"

    await call.message.edit_text(
        f"🎰 <b>Omad G'ildiragi!</b>\n\n"
        f"<b>{reel}</b>\n\n"
        f"🎉 Tabriklaymiz!\n"
        f"✨ <b>+{prize['label']}</b> yutdingiz!\n"
        f"💰 Yangi balans: <b>{balance}⭐</b>\n\n"
        f"<i>Ertaga yana o'ynang!</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Kunlik bonusga qaytish", callback_data="daily_menu")],
            [InlineKeyboardButton(text="🔙 Bosh sahifa", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )
    await call.answer(f"🎉 {prize['label']} yutdingiz!")


# ===================== AUTO REMINDER =====================

async def send_reminders():
    """Har 2 soatda 24 soat kirmagan foydalanuvchilarga eslatma yuboradi."""
    await asyncio.sleep(3600)  # Botni ishga tushirishdan 1 soat o'tgach boshlash
    while True:
        try:
            now    = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=24)
            reminder_cutoff = now - timedelta(hours=24)

            cursor = users.find(
                {
                    "is_banned": {"$ne": True},
                    "last_active": {"$lt": cutoff},
                    "$or": [
                        {"last_reminder_at": {"$exists": False}},
                        {"last_reminder_at": None},
                        {"last_reminder_at": {"$lt": reminder_cutoff}},
                    ]
                },
                {"user_id": 1}
            )
            user_ids = [doc["user_id"] async for doc in cursor]
            logger.info(f"📢 Eslatma yuborish: {len(user_ids)} ta foydalanuvchi")

            for uid in user_ids:
                try:
                    await bot.send_message(
                        uid,
                        "⭐ <b>Balansingiz siz kutmoqda!</b>\n\n"
                        "Starslarni yeg'ishda davom eting — "
                        "kunlik bonus va omad g'ildiragingiz kutmoqda! 🎰\n\n"
                        "👇 Botga kiring:",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🎰 Kunlik sovg'alar", callback_data="daily_menu")],
                        ]),
                        parse_mode="HTML"
                    )
                    await users.update_one(
                        {"user_id": uid},
                        {"$set": {"last_reminder_at": now}}
                    )
                except TelegramForbiddenError:
                    pass
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception:
                    pass
                await asyncio.sleep(0.15)
        except Exception as e:
            logger.error(f"send_reminders xato: {e}")
        await asyncio.sleep(2 * 3600)  # har 2 soatda


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
async def admin_add_channel_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.add_channel_link)
    await call.message.edit_text(
        "➕ <b>Kanal qo'shish</b>\n\n"
        "<b>Ochiq kanal:</b>\n"
        "<code>https://t.me/kanalnom</code>\n"
        "→ Bot ID ni o'zi topadi ✅\n\n"
        "<b>Maxfiy kanal:</b>\n"
        "<code>https://t.me/+xxxxxxxxxx</code>\n"
        "→ Keyingi qadamda kanaldan xabar forward qilasiz\n\n"
        "⚠️ Bot kanalda <b>admin</b> bo'lishi kerak!",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.add_channel_link)
async def process_add_channel_link(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    link = message.text.strip() if message.text else ""
    if not link.startswith("https://t.me/"):
        await message.answer(
            "❌ Noto'g'ri link!\n\n"
            "Masalan: <code>https://t.me/kanalnom</code> yoki <code>https://t.me/+xxxxxxxxxx</code>",
            parse_mode="HTML"
        )
        return

    # Ochiq kanal — username orqali ID ni o'zi topadi
    path = link.replace("https://t.me/", "").strip("/")
    if not path.startswith("+"):
        username = f"@{path}"
        try:
            chat = await bot.get_chat(username)
            channel_id = str(chat.id)
            name       = chat.title or path
            await add_channel(channel_id, name, link)
            await admin_log(ADMIN_ID, "add_channel", f"id={channel_id}, name={name}")
            await state.clear()
            await message.answer(
                f"✅ <b>Kanal qo'shildi!</b>\n\n"
                f"📢 <b>{name}</b>\n"
                f"🆔 <code>{channel_id}</code>",
                reply_markup=admin_keyboard(), parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"get_chat xato {username}: {e}")
            await message.answer(
                "❌ Bot kanalda <b>admin</b> emas yoki kanal topilmadi!\n\n"
                "Bot kanalga admin qiling, keyin qaytadan urinib ko'ring.",
                reply_markup=back_kb("admin_panel"), parse_mode="HTML"
            )
            await state.clear()
        return

    # Maxfiy kanal — kanaldan xabar forward kerak
    await state.update_data(channel_link=link)
    await state.set_state(AdminStates.add_channel_id)
    await message.answer(
        "✅ Link saqlandi!\n\n"
        "Endi <b>kanaldan istalgan xabarni</b> shu yerga <b>forward</b> qiling:\n\n"
        "📌 Kanalga kiring → xabarni bosib ushlab turing → Forward → bu chatga yuboring",
        parse_mode="HTML"
    )


@router.message(AdminStates.add_channel_id)
async def process_add_channel_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    # Forward qilingan xabardan kanal ID ni olish
    channel_id = None
    name       = None

    if message.forward_from_chat:
        channel_id = str(message.forward_from_chat.id)
        name       = message.forward_from_chat.title or channel_id
    elif message.forward_origin and hasattr(message.forward_origin, "chat"):
        channel_id = str(message.forward_origin.chat.id)
        name       = message.forward_origin.chat.title or channel_id

    if not channel_id:
        await message.answer(
            "❌ Kanal xabari topilmadi!\n\n"
            "Kanaldan xabarni <b>forward</b> qiling.\n"
            "Agar kanal <b>content protection</b> yoqilgan bo'lsa — o'chiring, forward qiling, qaytadan yoqing.",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    link = data.get("channel_link", "")
    await add_channel(channel_id, name, link)
    await admin_log(ADMIN_ID, "add_channel", f"id={channel_id}, name={name}")
    await state.clear()
    await message.answer(
        f"✅ <b>Kanal qo'shildi!</b>\n\n"
        f"📢 <b>{name}</b>\n"
        f"🆔 <code>{channel_id}</code>",
        reply_markup=admin_keyboard(), parse_mode="HTML"
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


@router.callback_query(F.data.startswith("rm_ch_"))
async def rm_channel_from_alert(call: CallbackQuery):
    """Health alert xabaridagi tezkor o'chirish tugmasi."""
    if call.from_user.id != ADMIN_ID:
        return
    channel_id = call.data[len("rm_ch_"):]
    ch = await channels.find_one({"channel_id": channel_id})
    ch_name = ch["channel_name"] if ch else channel_id
    await remove_channel(channel_id)
    await admin_log(ADMIN_ID, "remove_channel", f"id={channel_id} (health alert)")
    await call.message.edit_text(
        f"✅ <b>{ch_name}</b> kanali ro'yxatdan o'chirildi.",
        parse_mode="HTML"
    )
    await call.answer("✅ O'chirildi!")


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
        await call.message.edit_text("✅ Barcha kanallar o'chirildi.", reply_markup=admin_keyboard())
        return
    buttons = [
        [InlineKeyboardButton(text=f"🗑 {ch['channel_name']}", callback_data=f"delch_{ch['channel_id']}")]
        for ch in chs
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


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


@router.callback_query(F.data == "admin_set_min_refs")
async def admin_set_min_refs(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    current = await get_min_referrals()
    await state.set_state(AdminStates.set_min_referrals)
    await call.message.edit_text(
        f"👥 <b>Minimum referral soni</b>\n\n"
        f"Hozirgi: <b>{current} ta</b>\n\n"
        f"Gift olish uchun kerakli minimum referral sonini kiriting:\n"
        f"(0 kiritsangiz — referral sharti bo'lmaydi)",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_min_referrals)
async def process_min_referrals(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError
        await set_setting("min_referrals", val)
        await admin_log(ADMIN_ID, "set_min_referrals", str(val))
        await state.clear()
        text = f"✅ Min referral: <b>{val} ta</b>" if val > 0 else "✅ Referral sharti <b>o'chirildi</b>"
        await message.answer(text, reply_markup=admin_keyboard(), parse_mode="HTML")
    except Exception:
        await message.answer("❌ To'g'ri son kiriting! Masalan: 3")


@router.callback_query(F.data == "admin_exc_orders")
async def admin_exc_orders_handler(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    pending = await get_pending_exchange_orders()
    if not pending:
        await call.answer("Kutayotgan almashtirish yo'q!", show_alert=True)
        return
    text    = "💱 <b>Kutayotgan almashtirish buyurtmalari:</b>\n\n"
    buttons = []
    for o in pending:
        oid   = str(o["_id"])
        uname = f"@{o['username']}" if o.get("username") else o.get("full_name", "")
        label = f"🎮 {uname} — {o['stars']}⭐ → {o['amount']} UC"
        text += f"🎮 {uname} | {o['stars']}⭐ → {o['amount']} UC | ID: {o['detail1']} ({o['detail2']})\n"
        buttons.append([InlineKeyboardButton(
            text=f"✅ {label}",
            callback_data=f"done_exc|{oid}|{o['user_id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML"
    )
    await call.answer()


def _variants_keyboard(vtype: str, variants: list, back_cb: str) -> InlineKeyboardMarkup:
    unit = "UC"
    buttons = []
    for i, (stars, amount) in enumerate(variants):
        label = f"Slot {i+1}: {stars}⭐ = {amount:,} {unit}" if stars > 0 else f"Slot {i+1}: (bo'sh)"
        buttons.append([InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"{vtype}_slot_{i}")])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_pubg_config")
async def admin_pubg_config(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    variants = await get_variants("pubg")
    await call.message.edit_text(
        "🎮 <b>PUBG UC variantlarini sozlash</b>\n\n"
        "O'zgartirish uchun slotga bosing:",
        reply_markup=_variants_keyboard("pubg", variants, "admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("pubg_slot_"))
async def pubg_slot_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        slot = int(call.data.split("_")[2])
    except (ValueError, IndexError):
        await call.answer("❌ Xato!", show_alert=True)
        return
    variants = await get_variants("pubg")
    cur = variants[slot] if slot < len(variants) else [0, 0]
    await state.update_data(edit_vtype="pubg", edit_slot=slot)
    await state.set_state(AdminStates.set_pubg_variant)
    await call.message.edit_text(
        f"🎮 <b>PUBG Slot {slot+1}</b>\n\n"
        f"Hozirgi: <b>{cur[0]}⭐ = {cur[1]} UC</b>\n\n"
        f"Yangi qiymatni kiriting:\n"
        f"<code>stars uc_miqdor</code>\n"
        f"Masalan: <code>15 5</code>\n\n"
        f"O'chirish uchun: <code>0 0</code>",
        reply_markup=back_kb("admin_pubg_config"), parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_pubg_variant)
async def process_pubg_variant(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts = message.text.strip().split()
        stars = int(parts[0])
        uc    = float(parts[1])
        if stars < 0 or uc < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ Format: <code>stars uc_miqdor</code>\nMasalan: <code>15 5</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    slot = data["edit_slot"]
    await set_variant_slot("pubg", slot, stars, uc)
    await admin_log(ADMIN_ID, "set_pubg_variant", f"slot={slot}, stars={stars}, uc={uc}")
    await state.clear()
    label = f"{stars}⭐ = {uc} UC" if stars > 0 else "o'chirildi"
    await message.answer(
        f"✅ PUBG Slot {slot+1}: <b>{label}</b>",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )


# ===================== ADMIN USER SEARCH =====================

@router.callback_query(F.data == "admin_search_user")
async def admin_search_user_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.search_user)
    await call.message.edit_text(
        "🔍 <b>Foydalanuvchi qidirish</b>\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• <code>123456789</code> — Telegram ID\n"
        "• <code>@username</code> — Username\n"
        "• <code>Ism</code> — Ism bo'yicha\n\n"
        "<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.search_user)
async def admin_search_user_process(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    query = message.text.strip()

    # ID bo'yicha qidirish
    user = None
    if query.lstrip("-").isdigit():
        user = await users.find_one({"user_id": int(query)})
    # Username bo'yicha
    elif query.startswith("@"):
        uname = query[1:].lower()
        user = await users.find_one({"username": {"$regex": f"^{uname}$", "$options": "i"}})
    else:
        # Ism bo'yicha (birinchi mos kelgan)
        user = await users.find_one({"full_name": {"$regex": query, "$options": "i"}})

    if not user:
        await message.answer(
            f"❌ <b>Topilmadi:</b> <code>{query}</code>\n\n"
            f"Qayta qidirish uchun tugmani bosing.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Qayta qidirish", callback_data="admin_search_user")],
                [InlineKeyboardButton(text="🔙 Admin panel",    callback_data="admin_panel")],
            ]),
            parse_mode="HTML"
        )
        return

    await _show_user_card(message, user, send=True)


async def _show_user_card(msg, user: dict, send: bool = True, edit: bool = False):
    uid       = user["user_id"]
    name      = user.get("full_name") or "—"
    uname     = f"@{user['username']}" if user.get("username") else "—"
    balance   = user.get("balance", 0)
    refs      = user.get("referral_count", 0)
    banned    = user.get("is_banned", False)
    joined    = user.get("created_at")
    joined_str = ensure_utc(joined).strftime("%d.%m.%Y %H:%M") if joined else "—"

    status = "🚫 BAN" if banned else "✅ Faol"
    ban_btn = ("✅ Bandan chiqarish", f"unban_u_{uid}") if banned else ("🚫 Banlash", f"ban_u_{uid}")

    text = (
        f"👤 <b>Foydalanuvchi kartasi</b>\n\n"
        f"🪪 ID: <code>{uid}</code>\n"
        f"📛 Ism: <b>{name}</b>\n"
        f"🔗 Username: {uname}\n"
        f"💰 Balans: <b>{balance}⭐</b>\n"
        f"👥 Referrallar: <b>{refs} ta</b>\n"
        f"📅 Qo'shilgan: {joined_str}\n"
        f"🔒 Holat: {status}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Balans",  callback_data=f"su_add_{uid}"),
            InlineKeyboardButton(text="➖ Balans",  callback_data=f"su_ded_{uid}"),
        ],
        [InlineKeyboardButton(text=ban_btn[0],         callback_data=ban_btn[1])],
        [InlineKeyboardButton(text="✉️ Xabar yuborish", callback_data=f"su_msg_{uid}")],
        [InlineKeyboardButton(text="🔍 Qayta qidirish", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="🔙 Admin panel",    callback_data="admin_panel")],
    ])
    if send:
        await msg.answer(text, reply_markup=kb, parse_mode="HTML")
    elif edit:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("ban_u_"))
async def search_ban_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(call.data[len("ban_u_"):])
    except ValueError:
        await call.answer("Xato!", show_alert=True)
        return
    await ban_user(uid, "Admin tomonidan ban qilindi")
    await admin_log(ADMIN_ID, "ban", f"uid={uid}")
    user = await users.find_one({"user_id": uid})
    if user:
        await _show_user_card(call.message, user, send=False, edit=True)
    await call.answer("🚫 Banlandi!")
    try:
        await bot.send_message(uid, "🚫 Siz admin tomonidan botdan bloklangansiz.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("unban_u_"))
async def search_unban_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(call.data[len("unban_u_"):])
    except ValueError:
        await call.answer("Xato!", show_alert=True)
        return
    await unban_user(uid)
    await admin_log(ADMIN_ID, "unban", f"uid={uid}")
    user = await users.find_one({"user_id": uid})
    if user:
        await _show_user_card(call.message, user, send=False, edit=True)
    await call.answer("✅ Ban olib tashlandi!")


@router.callback_query(F.data.startswith("su_add_"))
async def search_user_add_bal(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data[len("su_add_"):])
    await state.set_state(AdminStates.add_balance_input)
    await state.update_data(target_uid=uid)
    await call.message.edit_text(
        f"💰 <b>Balans qo'shish</b>\n"
        f"🆔 User: <code>{uid}</code>\n\n"
        f"Miqdorni kiriting (masalan: <code>10</code>):\n\n"
        f"<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("su_ded_"))
async def search_user_ded_bal(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data[len("su_ded_"):])
    await state.set_state(AdminStates.deduct_balance_input)
    await state.update_data(target_uid=uid)
    await call.message.edit_text(
        f"💸 <b>Balans ayirish</b>\n"
        f"🆔 User: <code>{uid}</code>\n\n"
        f"Miqdorni kiriting (masalan: <code>10</code>):\n\n"
        f"<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("su_msg_"))
async def search_user_msg_start(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data[len("su_msg_"):])
    await state.set_state(AdminStates.send_user_msg)
    await state.update_data(target_uid=uid)
    await call.message.edit_text(
        f"✉️ <b>Xabar yuborish</b>\n"
        f"🆔 User: <code>{uid}</code>\n\n"
        f"Xabar matnini yuboring:\n\n"
        f"<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb("admin_panel"),
        parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.send_user_msg)
async def search_user_msg_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid  = data.get("target_uid")
    await state.clear()
    if not uid:
        await message.answer("❌ Xato: user topilmadi.")
        return
    try:
        await bot.send_message(
            uid,
            f"📩 <b>Admin xabari:</b>\n\n{message.text}",
            parse_mode="HTML"
        )
        await message.answer(
            f"✅ Xabar yuborildi → <code>{uid}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Qayta qidirish", callback_data="admin_search_user")],
                [InlineKeyboardButton(text="🔙 Admin panel",    callback_data="admin_panel")],
            ]),
            parse_mode="HTML"
        )
    except (TelegramForbiddenError, TelegramNotFound):
        await message.answer(
            f"❌ Yuborib bo'lmadi — foydalanuvchi botni bloklagan yoki topilmadi.",
            reply_markup=back_kb("admin_panel"),
            parse_mode="HTML"
        )
    await admin_log(ADMIN_ID, "send_message", f"uid={uid}")


@router.callback_query(F.data.startswith("su_view_"))
async def search_user_view(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(call.data[len("su_view_"):])
    except ValueError:
        await call.answer("Xato!", show_alert=True)
        return
    user = await users.find_one({"user_id": uid})
    if not user:
        await call.answer("Foydalanuvchi topilmadi!", show_alert=True)
        return
    await _show_user_card(call.message, user, send=False, edit=True)
    await call.answer()


@router.callback_query(F.data == "admin_banned")
async def admin_banned_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    banned_users = await users.find({"is_banned": True}).limit(50).to_list(50)
    if not banned_users:
        await call.answer("Banlangan foydalanuvchi yo'q!", show_alert=True)
        return
    buttons = []
    for u in banned_users:
        uname  = f"@{u['username']}" if u.get("username") else u.get("full_name", "Noma'lum")
        reason = u.get("ban_reason", "—")
        buttons.append([InlineKeyboardButton(
            text=f"🔓 {uname} ({u['user_id']}) | {reason}",
            callback_data=f"unban_{u['user_id']}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Ortga", callback_data="admin_panel")])
    await call.message.edit_text(
        f"🚫 <b>Banlangan foydalanuvchilar: {len(banned_users)} ta</b>\n\n"
        f"Unban qilish uchun tugmani bosing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await call.answer()


@router.callback_query(F.data.startswith("unban_"))
async def admin_unban_user(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(call.data.split("_")[1])
    except Exception:
        await call.answer("❌ Noto'g'ri format!", show_alert=True)
        return
    await unban_user(uid)
    await admin_log(ADMIN_ID, "unban", f"uid={uid}")
    try:
        await bot.send_message(uid, "✅ Siz botdan ban olib tashlandi! /start bosing.")
    except Exception:
        pass
    await call.answer(f"✅ {uid} unban qilindi!", show_alert=True)
    await admin_banned_list(call)


@router.callback_query(F.data == "admin_our_channel")
async def admin_our_channel_handler(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.set_our_channel)
    await call.message.edit_text(
        f"📢 <b>Bizning kanal havolasini o'zgartirish</b>\n\n"
        f"Hozirgi havola:\n<code>{_our_channel_url}</code>\n\n"
        f"Yangi havola yuboring (masalan: <code>https://t.me/yourchannel</code>)\n\n"
        f"<i>Bekor qilish: /cancel</i>",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


@router.message(AdminStates.set_our_channel)
async def process_our_channel(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    global _our_channel_url
    new_url = (message.text or "").strip()
    if not new_url.startswith("http"):
        await message.answer("❌ Havola http yoki https bilan boshlanishi kerak.", parse_mode="HTML")
        return
    _our_channel_url = new_url
    await set_setting("our_channel", new_url)
    await state.clear()
    await message.answer(
        f"✅ <b>Bizning kanal havolasi yangilandi!</b>\n\n"
        f"<code>{new_url}</code>",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )
    await admin_log(ADMIN_ID, "our_channel_set", new_url)


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
    data = await state.get_data()
    target_uid = data.get("target_uid")
    try:
        if target_uid:
            amt = float(message.text.strip())
            uid = target_uid
        else:
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
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Kartani ko'rish", callback_data=f"su_view_{uid}")],
            [InlineKeyboardButton(text="🔙 Admin panel",     callback_data="admin_panel")],
        ]) if target_uid else admin_keyboard()
        await message.answer(
            f"✅ <code>{uid}</code> ga <b>+{amt}⭐</b> qo'shildi!\n"
            f"Yangi balans: <b>{new_bal}⭐</b>",
            reply_markup=kb, parse_mode="HTML"
        )
        try:
            await bot.send_message(uid, f"💰 Hisobingizga <b>+{amt}⭐</b> qo'shildi!", parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        hint = "Miqdorni kiriting (masalan: <code>10.5</code>)" if target_uid else \
               "Format: <code>USER_ID MIQDOR</code>\nMasalan: <code>123456789 10.5</code>"
        await message.answer(f"❌ {hint}", parse_mode="HTML")


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
    data = await state.get_data()
    target_uid = data.get("target_uid")
    try:
        if target_uid:
            amt = float(message.text.strip())
            uid = target_uid
        else:
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
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Kartani ko'rish", callback_data=f"su_view_{uid}")],
            [InlineKeyboardButton(text="🔙 Admin panel",     callback_data="admin_panel")],
        ]) if target_uid else admin_keyboard()
        await message.answer(
            f"✅ <code>{uid}</code> dan <b>-{amt}⭐</b> ayirildi!\n"
            f"Yangi balans: <b>{new_bal}⭐</b>",
            reply_markup=kb, parse_mode="HTML"
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
    min_refs  = await get_min_referrals()
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{total_users}</b>\n"
        f"💰 Jami balanslar: <b>{total_balance}⭐</b>\n"
        f"📈 Jami kreditlar: <b>{total_credits}</b>\n"
        f"🎁 Bajarilgan buyurtmalar: <b>{total_gifts}</b>\n"
        f"⏳ Kutayotgan buyurtmalar: <b>{pending_gifts}</b>\n\n"
        f"⚙️ Referral: <b>{ref_stars}⭐</b> | Obuna: <b>{sub_stars}⭐</b>\n"
        f"👥 Gift uchun min referral: <b>{min_refs} ta</b>",
        reply_markup=back_kb("admin_panel"), parse_mode="HTML"
    )
    await call.answer()


async def show_users_page(message, top_list: list, page: int, edit: bool = True):
    per_page = 25
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
    _users_cache = await users.find().sort("balance", -1).limit(250).to_list(250)
    await show_users_page(call.message, _users_cache, page=0, edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("ausers_"))
async def admin_users_page(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    global _users_cache
    try:
        page = int(call.data.split("_")[1])
    except (IndexError, ValueError):
        page = 0
    if not _users_cache:
        _users_cache = await users.find().sort("balance", -1).limit(250).to_list(250)
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

    user_ids   = await get_all_user_ids()
    total      = len(user_ids)
    sent = failed = blocked = 0
    status_msg = await message.answer(f"📣 Yuborilmoqda... 0/{total}")

    semaphore = asyncio.Semaphore(10)

    async def _send(uid: int) -> str:
        async with semaphore:
            for attempt in range(5):
                try:
                    if message.photo:
                        await bot.send_photo(
                            uid, photo=message.photo[-1].file_id,
                            caption=f"📣 {message.caption or ''}", parse_mode="HTML"
                        )
                    elif message.video:
                        await bot.send_video(
                            uid, video=message.video.file_id,
                            caption=f"📣 {message.caption or ''}", parse_mode="HTML"
                        )
                    elif message.animation:
                        await bot.send_animation(
                            uid, animation=message.animation.file_id,
                            caption=f"📣 {message.caption or ''}", parse_mode="HTML"
                        )
                    elif message.text:
                        await bot.send_message(uid, f"📣 {message.text}", parse_mode="HTML")
                    await asyncio.sleep(0.05)  # ~20 msg/sec limit
                    return "sent"
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except (TelegramForbiddenError, TelegramNotFound):
                    return "blocked"
                except Exception as e:
                    err = str(e).lower()
                    if "blocked" in err or "deactivated" in err or "not found" in err:
                        return "blocked"
                    await asyncio.sleep(2 ** attempt)
            return "failed"

    tasks     = [_send(uid) for uid in user_ids]
    done_cnt  = 0

    for coro in asyncio.as_completed(tasks):
        result = await coro
        done_cnt += 1
        if result == "sent":
            sent += 1
        elif result == "blocked":
            blocked += 1
        else:
            failed += 1
        if done_cnt % 50 == 0 or done_cnt == total:
            try:
                await status_msg.edit_text(
                    f"📣 Yuborilmoqda... {done_cnt}/{total}\n"
                    f"✅ {sent} | 🚫 {blocked} | ❌ {failed}"
                )
            except Exception:
                pass

    await admin_log(ADMIN_ID, "broadcast", f"sent={sent}, blocked={blocked}, failed={failed}")
    await status_msg.edit_text(
        f"✅ <b>Broadcast tugadi!</b>\n\n"
        f"📊 Jami: <b>{total}</b>\n"
        f"✅ Yuborildi: <b>{sent}</b>\n"
        f"🚫 Bloklagan: <b>{blocked}</b>\n"
        f"❌ Xato: <b>{failed}</b>",
        parse_mode="HTML"
    )
    await message.answer("🏠 Admin panel:", reply_markup=admin_keyboard())


# ===================== KONKURS (CONTESTS) =====================

async def _finalize_slot_contest(contest: dict):
    """Slot to'lganda random g'oliblarni tanlaydi va adminga xabar beradi."""
    import random
    cid        = contest["_id"]
    parts      = contest["participants"]
    n_winners  = contest.get("winners_count", 3)
    winners    = random.sample(parts, min(n_winners, len(parts)))

    await contests.update_one(
        {"_id": cid},
        {"$set": {"status": "finished", "winners": winners}}
    )
    lines = [f"🎰 <b>Slot Konkurs Yakunlandi!</b>\n"]
    lines.append(f"👥 Qatnashchilar: <b>{len(parts)} ta</b>")
    lines.append(f"🏆 G'oliblar ({len(winners)} ta):\n")
    for i, w in enumerate(winners, 1):
        uname = f"@{w['username']}" if w.get("username") else w.get("full_name", "—")
        lines.append(f"{i}. {uname} — <code>{w['user_id']}</code>")
    text = "\n".join(lines)
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        pass


async def _handle_slot_join(message, user_id: int, username: str, full_name: str, token: str):
    """Foydalanuvchi slot konkurs havolasiga kirganida chaqiriladi."""
    contest = await contests.find_one({"token": token, "type": "slot", "status": "active"})
    if not contest:
        await message.answer(
            "❌ Bu konkurs mavjud emas yoki tugagan.",
            reply_markup=main_menu(user_id), parse_mode="HTML"
        )
        return

    already = any(p["user_id"] == user_id for p in contest.get("participants", []))
    if already:
        filled = len(contest["participants"])
        total  = contest["total_slots"]
        await message.answer(
            f"ℹ️ Siz allaqachon bu konkursda qatnashyapsiz.\n"
            f"📊 To'lgan: {filled}/{total}",
            reply_markup=main_menu(user_id), parse_mode="HTML"
        )
        return

    participant = {
        "user_id":   user_id,
        "username":  username,
        "full_name": full_name,
        "joined_at": datetime.now(timezone.utc)
    }
    updated = await contests.find_one_and_update(
        {"_id": contest["_id"], "status": "active"},
        {"$push": {"participants": participant}},
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        await message.answer("❌ Konkurs tugadi.", reply_markup=main_menu(user_id), parse_mode="HTML")
        return

    filled = len(updated["participants"])
    total  = updated["total_slots"]
    await message.answer(
        f"✅ <b>Konkursga qo'shildingiz!</b>\n\n"
        f"🎰 Slot: <b>{filled}/{total}</b>\n"
        f"G'oliblar random tanlanadi. Omad! 🍀",
        reply_markup=main_menu(user_id), parse_mode="HTML"
    )

    if filled >= total:
        await _finalize_slot_contest(updated)


# --- Admin: Slot konkurs yaratish ---

@router.callback_query(F.data == "admin_slot_contest")
async def admin_slot_contest(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    active = await contests.find_one({"type": "slot", "status": "active"})
    if active:
        filled = len(active.get("participants", []))
        total  = active["total_slots"]
        token  = active["token"]
        bot_info = await bot.get_me()
        link   = f"https://t.me/{bot_info.username}?start=slot_{token}"
        await call.message.edit_text(
            f"🎰 <b>Faol Slot Konkurs</b>\n\n"
            f"📊 To'lgan: <b>{filled}/{total}</b>\n"
            f"🔗 Havola:\n<code>{link}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Konkursni bekor qilish", callback_data=f"stop_slot_{str(active['_id'])}")],
                [InlineKeyboardButton(text="🔙 Admin panel",            callback_data="admin_panel")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.set_state(AdminStates.slot_contest_setup)
        await call.message.edit_text(
            "🎰 <b>Yangi Slot Konkurs</b>\n\n"
            "Format: <code>SLOTLAR G'OLIBLAR</code>\n"
            "Masalan: <code>100 3</code>\n"
            "(100 slot, 3 ta g'olib)\n\n"
            "<i>Bekor qilish: /cancel</i>",
            reply_markup=back_kb("admin_panel"), parse_mode="HTML"
        )
    await call.answer()


@router.message(AdminStates.slot_contest_setup)
async def process_slot_contest(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        parts     = message.text.strip().split()
        total     = int(parts[0])
        n_winners = int(parts[1]) if len(parts) > 1 else 3
        if total < 2 or n_winners < 1 or n_winners >= total:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("❌ Format: <code>SLOTLAR G'OLIBLAR</code>\nMasalan: <code>100 3</code>", parse_mode="HTML")
        return

    await state.clear()
    token = secrets.token_hex(4)
    await contests.insert_one({
        "type":          "slot",
        "token":         token,
        "total_slots":   total,
        "winners_count": n_winners,
        "participants":  [],
        "winners":       [],
        "status":        "active",
        "created_at":    datetime.now(timezone.utc)
    })
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=slot_{token}"
    await message.answer(
        f"✅ <b>Slot Konkurs yaratildi!</b>\n\n"
        f"🎰 Slotlar: <b>{total} ta</b>\n"
        f"🏆 G'oliblar: <b>{n_winners} ta</b>\n\n"
        f"🔗 Havola:\n<code>{link}</code>\n\n"
        f"Havolani foydalanuvchilarga yuboring.",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )
    await admin_log(ADMIN_ID, "slot_contest_created", f"slots={total}, winners={n_winners}")


@router.callback_query(F.data.startswith("stop_slot_"))
async def stop_slot_contest(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    contest_id = call.data[len("stop_slot_"):]
    await contests.update_one({"_id": ObjectId(contest_id)}, {"$set": {"status": "cancelled"}})
    await call.message.edit_text("❌ Konkurs bekor qilindi.", reply_markup=admin_keyboard())
    await call.answer("Bekor qilindi!")


# --- Admin: Referral konkurs yaratish ---

@router.callback_query(F.data == "admin_ref_contest")
async def admin_ref_contest(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    active = await contests.find_one({"type": "referral", "status": "active"})
    if active:
        end_at   = ensure_utc(active["end_at"]).astimezone(TIMEZONE)
        end_str  = end_at.strftime("%d.%m.%Y %H:%M")
        pipeline = [
            {"$match": {"contest_id": active["_id"]}},
            {"$group": {"_id": "$referrer_id", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5}
        ]
        top = await contest_refs_col.aggregate(pipeline).to_list(5)
        lines = [f"🏆 <b>Faol Referral Konkurs</b>\n", f"⏰ Tugash: <b>{end_str}</b>\n"]
        if top:
            lines.append("<b>Hozirgi reyting:</b>")
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i, row in enumerate(top):
                u = await users.find_one({"user_id": row["_id"]}, {"full_name":1,"username":1})
                name = (f"@{u['username']}" if u and u.get("username") else (u or {}).get("full_name","?")) if u else str(row["_id"])
                lines.append(f"{medals[i]} {name} — {row['count']} ta")
        else:
            lines.append("Hali hech kim taklif qilmagan.")
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏁 Konkursni yakunlash", callback_data=f"finish_ref_{str(active['_id'])}")],
                [InlineKeyboardButton(text="❌ Bekor qilish",        callback_data=f"stop_ref_{str(active['_id'])}")],
                [InlineKeyboardButton(text="🔙 Admin panel",         callback_data="admin_panel")],
            ]),
            parse_mode="HTML"
        )
    else:
        await state.set_state(AdminStates.ref_contest_setup)
        now_str = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
        await call.message.edit_text(
            f"🏆 <b>Yangi Referral Konkurs</b>\n\n"
            f"Konkurs qachon tugashini kiriting:\n"
            f"• Sana/vaqt: <code>DD.MM.YYYY HH:MM</code>\n"
            f"• Yoki soatlar soni: <code>24</code>\n\n"
            f"Hozirgi vaqt: <b>{now_str}</b>\n\n"
            f"<i>Bekor qilish: /cancel</i>",
            reply_markup=back_kb("admin_panel"), parse_mode="HTML"
        )
    await call.answer()


@router.message(AdminStates.ref_contest_setup)
async def process_ref_contest(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip() if message.text else ""
    end_at = None
    try:
        if text.isdigit():
            hours  = int(text)
            end_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        else:
            naive  = datetime.strptime(text, "%d.%m.%Y %H:%M")
            end_at = TIMEZONE.localize(naive).astimezone(timezone.utc)
    except Exception:
        await message.answer(
            "❌ Format noto'g'ri.\n"
            "Misol: <code>01.06.2025 20:00</code> yoki <code>24</code> (soat)",
            parse_mode="HTML"
        )
        return

    await state.clear()
    await contests.insert_one({
        "type":       "referral",
        "status":     "active",
        "end_at":     end_at,
        "created_at": datetime.now(timezone.utc)
    })
    end_local = end_at.astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
    await message.answer(
        f"✅ <b>Referral Konkurs boshlandi!</b>\n\n"
        f"⏰ Tugash vaqti: <b>{end_local}</b>\n\n"
        f"Kim ko'p odam qo'shsa — g'olib!\n"
        f"Vaqt kelganda avtomatik e'lon qilinadi.",
        reply_markup=admin_keyboard(), parse_mode="HTML"
    )
    await admin_log(ADMIN_ID, "ref_contest_created", f"end_at={end_local}")


@router.callback_query(F.data.startswith("finish_ref_"))
async def finish_ref_contest_cb(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    contest_id = call.data[len("finish_ref_"):]
    contest    = await contests.find_one({"_id": ObjectId(contest_id)})
    if contest:
        await _finalize_ref_contest(contest)
        await call.message.edit_text("✅ Konkurs yakunlandi!", reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("stop_ref_"))
async def stop_ref_contest(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    contest_id = call.data[len("stop_ref_"):]
    await contests.update_one({"_id": ObjectId(contest_id)}, {"$set": {"status": "cancelled"}})
    await call.message.edit_text("❌ Referral konkurs bekor qilindi.", reply_markup=admin_keyboard())
    await call.answer("Bekor qilindi!")


async def _finalize_ref_contest(contest: dict):
    """Referral konkursni yakunlaydi va g'olibni e'lon qiladi."""
    await contests.update_one({"_id": contest["_id"]}, {"$set": {"status": "finished"}})
    pipeline = [
        {"$match": {"contest_id": contest["_id"]}},
        {"$group": {"_id": "$referrer_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    top = await contest_refs_col.aggregate(pipeline).to_list(10)
    if not top:
        try:
            await bot.send_message(
                ADMIN_ID,
                "🏆 <b>Referral Konkurs Yakunlandi</b>\n\nHech kim qatnashmadi.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    lines  = ["🏆 <b>Referral Konkurs Yakunlandi!</b>\n"]
    for i, row in enumerate(top):
        u     = await users.find_one({"user_id": row["_id"]}, {"full_name":1,"username":1})
        name  = (f"@{u['username']}" if u and u.get("username") else (u or {}).get("full_name","?")) if u else str(row["_id"])
        uid   = row["_id"]
        count = row["count"]
        lines.append(f"{medals[i]} {name} (<code>{uid}</code>) — <b>{count} ta</b> referral")

    lines.append(f"\n🎉 G'olib: <b>{lines[1][2:].split('(')[0].strip()}</b>")
    try:
        await bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="HTML")
    except Exception:
        pass


async def check_ref_contest_deadline():
    """Har daqiqada referral konkurs deadlineni tekshiradi."""
    await asyncio.sleep(60)
    while True:
        try:
            now     = datetime.now(timezone.utc)
            contest = await contests.find_one({
                "type":   "referral",
                "status": "active",
                "end_at": {"$lte": now}
            })
            if contest:
                await _finalize_ref_contest(contest)
        except Exception as e:
            logger.error(f"check_ref_contest_deadline xato: {e}")
        await asyncio.sleep(60)


# ===================== KANAL SOGLIGI TEKSHIRUVI =====================

async def check_channel_health():
    """Har 6 soatda kanallarni tekshiradi, muammo bo'lsa adminga xabar beradi."""
    await asyncio.sleep(30)
    bot_info = await bot.get_me()
    while True:
        try:
            chs = await get_channels()
            for ch in chs:
                cid_str      = ch["channel_id"]
                channel_name = ch["channel_name"]
                channel_link = ch.get("channel_link", "")
                last_alert   = ch.get("health_alert_at")

                # Bir xil xabarni 30 daqiqada bir marta yubor
                if last_alert:
                    diff = (datetime.now(timezone.utc) - ensure_utc(last_alert)).total_seconds()
                    if diff < 1800:
                        continue

                problem = None
                try:
                    cid = int(cid_str)
                    member = await bot.get_chat_member(cid, bot_info.id)
                    if member.status not in ("administrator", "creator"):
                        problem = (
                            f"⚠️ <b>Bot admin emas!</b>\n\n"
                            f"📢 Kanal: <b>{channel_name}</b>\n"
                            f"Bot ushbu kanalda faqat oddiy a'zo. "
                            f"Iltimos botni <b>admin</b> qiling!"
                        )
                except TelegramForbiddenError:
                    problem = (
                        f"🚫 <b>Bot kanaldan chiqarildi!</b>\n\n"
                        f"📢 Kanal: <b>{channel_name}</b>\n"
                        f"Bot bu kanalga kira olmaydi. "
                        f"Botni qayta qo'shing yoki kanalni o'chiring."
                    )
                except TelegramNotFound:
                    problem = (
                        f"❌ <b>Kanal topilmadi!</b>\n\n"
                        f"📢 Kanal: <b>{channel_name}</b>\n"
                        f"Kanal o'chirilgan yoki ID noto'g'ri. "
                        f"Kanalni ro'yxatdan olib tashlang."
                    )
                except Exception as e:
                    logger.warning(f"Kanal health check xato ({cid_str}): {e}")

                # Maxfiy invite link eskirganligini tekshirish (t.me/+HASH)
                if not problem and channel_link and "/+" in channel_link:
                    try:
                        hash_part = channel_link.split("/+")[-1].strip("/")
                        await bot.get_chat(f"+{hash_part}")
                    except TelegramBadRequest as e:
                        err = str(e).lower()
                        if "expired" in err or "invalid" in err or "revoked" in err:
                            problem = (
                                f"🔗 <b>Kanal havolasi eskirgan!</b>\n\n"
                                f"📢 Kanal: <b>{channel_name}</b>\n"
                                f"<code>{channel_link}</code>\n\n"
                                f"Yangi invite link yarating va botga yangilang."
                            )
                    except Exception:
                        pass

                if problem:
                    await channels.update_one(
                        {"channel_id": cid_str},
                        {"$set": {"health_alert_at": datetime.now(timezone.utc)}}
                    )
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            problem,
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔗 Kanal havolasi",
                                    url=channel_link
                                )] if channel_link else [],
                                [InlineKeyboardButton(
                                    text="➖ Kanalni o'chirish",
                                    callback_data=f"rm_ch_{cid_str}"
                                )],
                                [InlineKeyboardButton(
                                    text="🔧 Admin panel",
                                    callback_data="admin_panel"
                                )],
                            ]),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"check_channel_health xato: {e}")

        await asyncio.sleep(1800)  # 30 daqiqa


# ===================== AUTO CHECK =====================

async def auto_check_subscriptions():
    await asyncio.sleep(10)
    while True:
        logger.info("🔄 Avtomatik obuna tekshiruvi...")
        try:
            chs = await get_channels()
            if chs:
                sub_stars    = float(await get_setting("subscribe_stars") or 0.10)
                all_user_ids = await get_all_user_ids()
                for user_id in all_user_ids:
                    for ch in chs:
                        channel_id_str = ch["channel_id"]
                        channel_name   = ch["channel_name"]
                        try:
                            ok = await is_member(channel_id_str, user_id)
                        except Exception:
                            ok = True  # xato bo'lsa tekshirishni o'tkazib yubor
                        bonus_doc = await channel_bonus.find_one({
                            "user_id": user_id, "channel_id": channel_id_str
                        })
                        if not ok and bonus_doc:
                            given = float(bonus_doc.get("stars_given", sub_stars))
                            await channel_bonus.delete_one({"user_id": user_id, "channel_id": channel_id_str})
                            await force_deduct_balance(user_id, given, f"Kanal tark etildi: {channel_name}")
                            new_bal = await get_balance(user_id)
                            debt_txt = f"\n⚠️ Qarz: <b>{abs(new_bal):.2f}⭐</b> — balansni to'ldiring!" if new_bal < 0 else ""
                            try:
                                await bot.send_message(
                                    user_id,
                                    f"❌ <b>{channel_name}</b> kanalini tark etgansiz!\n"
                                    f"💸 <b>-{given}⭐</b> ayirildi.{debt_txt}",
                                    parse_mode="HTML"
                                )
                            except TelegramRetryAfter as e:
                                await asyncio.sleep(e.retry_after + 1)
                            except Exception:
                                pass
                            await asyncio.sleep(0.1)  # xabar yuborilgandan keyin kut
                        await asyncio.sleep(0.1)  # har API call orasida
        except Exception as e:
            logger.error(f"auto_check xato: {e}")
        logger.info("✅ Tekshiruv tugadi.")
        await asyncio.sleep(2 * 3600)


# ===================== MAIN =====================

async def main():
    await init_db()
    dp.message.middleware(BanMiddleware())
    dp.callback_query.middleware(BanMiddleware())
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
    asyncio.create_task(check_channel_health())
    asyncio.create_task(check_ref_contest_deadline())
    asyncio.create_task(send_reminders())

    await bot.set_my_commands([
        BotCommand(command="start",    description="Botni boshlash / Bosh sahifa"),
        BotCommand(command="balansim", description="Balansingizni ko'rish"),
        BotCommand(command="cancel",   description="Amalni bekor qilish"),
    ])

    logger.info("✅ Bot ishga tushdi!")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
