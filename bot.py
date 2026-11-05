import os
import io
import json
import time
import html
import re
import asyncio
import logging
import urllib.parse
from datetime import datetime

from aiohttp import web

from telegram import (
    Update, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    InlineKeyboardButton as _TgInlineButton, KeyboardButton as _TgKeyButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ExtBot, filters
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.utils import get_peer_id
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, FloodWaitError,
    UserAlreadyParticipantError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    InputPeerSelf, UpdateGroupCallParticipants, InputPeerUser,
    Channel, Chat as TelegramChat
)

from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
OWNER_ID       = int(os.environ.get("OWNER_ID") or "7849054022")
API_ID         = int(os.environ.get("API_ID") or 0)
API_HASH       = os.environ.get("API_HASH", "")
DATA_DIR       = os.environ.get("DATA_DIR", ".")   # Render Disk দিলে /data দিন
os.makedirs(DATA_DIR, exist_ok=True)
ACCOUNTS_FILE  = os.path.join(DATA_DIR, "accounts.json")
ADMIN_FILE     = os.path.join(DATA_DIR, "admins.json")
SILENCE_FILE   = os.path.join(DATA_DIR, "silence.wav")
START_TIME     = time.time()
PORT           = int(os.environ.get("PORT") or 10000)
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL") or 4)
PER_PAGE       = 15

# ✅ Owner username auto-detect হবে startup-এ — env var দরকার নেই
_owner_username_cache: str | None = None

# ═══════════════════════════════════════════════════════════════════════════════
# PREMIUM EMOJI  (Bot API 9.4+ — bot যে account দিয়ে বানানো, তাতে Telegram Premium থাকতে হবে)
# নিচে প্রতিটা সাধারণ emoji এর পাশে "GET_PREMIUM_ID" এর জায়গায় premium emoji এর ID বসান।
# "GET_PREMIUM_ID" (বা সংখ্যা ছাড়া কিছু) থাকলে ওই emoji সাধারণই থাকবে।
# ═══════════════════════════════════════════════════════════════════════════════
PREMIUM_EMOJI_IDS = {
    "✅": "6226699395098745883",
    "🟢": "6321328167558585449",
    "🔴": "6323418085759919255",
    "🚨": "5395695537687123235",
    "👨‍👩‍👦": "4927046070900360103",
    "➕": "5397916757333654639",
    "🗄": "5262529363710060188",
    "📥": "5443127283898405358",
    "📤": "5445355530111437729",
    "🤖": "5372981976804366741",
    "🛜": "5402436682131718162",
    "⏺": "5195287308479242646",
    "🚫": "6264989883241076562",
    "📮": "5287533898803211359",
    "📌": "5397782960512444700",
    "🥰": "5416015487525988007",
    "❌": "6294194896349699910",
    "📊": "6226505614764285291",
    "📱": "5407025283456835913",
    "👇": "6114077020361070827",
    "⚠️": "5420323339723881652",
    "🔄": "5253464392850221514",
    "⏳": "6138579366403119002",
    "🔐": "5291873529464122510",
    "🎉": "5461151367559141950",
    "👤": "5249053508681883137",
    "💫": "5348181392528253881",
    "📋": "5197269100878907942",
    "🔒": "5296369303661067030",
    "🎁": "6116072659735287323",
    "🗑️": "5445267414562389170",
    "🎥": "6334554201519031929",
    "🎯": "5463274047771000031",
    "✔️": "5206607081334906820",
    "💣": "5280569974404966639",
    "🐍": "6136567092620564683",
    "🪖": "5357188789351490453",
    "♻️": "5377584064326804458",
    "👑": "6266995104687330978",
    "💡": "6100202351849642503",
    "⛔": "5260293700088511294",
    "🗨️": "5316904813613556587",
    "📦": "5472335930549347896",
    "🎊": "5956192270213452101",
    "📭": "5352896944496728039",
    "👋": "5440431182602842059",
    "✨": "6285088169817805553",
    "◀️": "6138930810692049889",
    "▶️": "6138500424904219564",
}

EMOJI_MAP: dict = {k: str(v).strip() for k, v in PREMIUM_EMOJI_IDS.items()
                   if str(v).strip().isdigit()}
_EMOJI_KEYS = sorted(EMOJI_MAP, key=len, reverse=True)
_EMOJI_RE = re.compile("|".join(re.escape(k) for k in _EMOJI_KEYS)) if _EMOJI_KEYS else None

def _emoji_tag(m) -> str:
    e = m.group(0)
    return f'<tg-emoji emoji-id="{EMOJI_MAP[e]}">{e}</tg-emoji>'

def _split_icon(text, api_kwargs):
    """Button text এর শুরুর emoji premium হলে সেটা button icon হয়ে যায়"""
    api_kwargs = dict(api_kwargs or {})
    if _EMOJI_RE and isinstance(text, str):
        t = text.lstrip()
        for e in _EMOJI_KEYS:
            if t.startswith(e):
                api_kwargs["icon_custom_emoji_id"] = EMOJI_MAP[e]
                rest = t[len(e):].strip()
                if rest:
                    text = rest
                break
    return text, api_kwargs

def InlineKeyboardButton(text, *args, api_kwargs=None, **kwargs):
    text, api_kwargs = _split_icon(text, api_kwargs)
    return _TgInlineButton(text, *args, api_kwargs=api_kwargs or None, **kwargs)

def KeyboardButton(text, *args, api_kwargs=None, **kwargs):
    text, api_kwargs = _split_icon(text, api_kwargs)
    return _TgKeyButton(text, *args, api_kwargs=api_kwargs or None, **kwargs)

def _premiumize(text, kwargs: dict, key: str = "parse_mode"):
    """text এ mapped emoji থাকলে <tg-emoji> বসায় (HTML বা plain text হলে)। Markdown হলে অপরিবর্তিত।"""
    if not (_EMOJI_RE and isinstance(text, str) and _EMOJI_RE.search(text)):
        return text
    pm = kwargs.get(key)
    if isinstance(pm, str):
        if pm.upper() != "HTML":
            return text
        return _EMOJI_RE.sub(_emoji_tag, text)
    kwargs[key] = "HTML"                      # parse_mode নেই -> plain text, escape করে HTML বানাই
    return _EMOJI_RE.sub(_emoji_tag, html.escape(text, quote=False))

class PremiumBot(ExtBot):
    async def send_message(self, chat_id, text, *args, **kwargs):
        if not args:
            text = _premiumize(text, kwargs)
        return await super().send_message(chat_id, text, *args, **kwargs)

    async def edit_message_text(self, text, *args, **kwargs):
        if not args:
            text = _premiumize(text, kwargs)
        return await super().edit_message_text(text, *args, **kwargs)

    async def send_document(self, chat_id, document, *args, **kwargs):
        if not args and isinstance(kwargs.get("caption"), str):
            kwargs["caption"] = _premiumize(kwargs["caption"], kwargs)
        return await super().send_document(chat_id, document, *args, **kwargs)

# ═══════════════════════════════════════════════════════════════════════════════
# STORAGE (multi-tenant per-user accounts)
# ═══════════════════════════════════════════════════════════════════════════════
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Migrate পুরনো single-tenant format থেকে multi-tenant format
                migrated = {}
                for k, v in data.items():
                    if isinstance(v, dict) and "session" in v:
                        migrated.setdefault(str(OWNER_ID), {})[k] = v
                    elif isinstance(v, dict):
                        migrated[k] = v
                return migrated
        except Exception:
            pass
    return {}

def _atomic_write_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def save_accounts(data):
    _atomic_write_json(ACCOUNTS_FILE, data)

def user_accounts(uid: int) -> dict:
    return load_accounts().get(str(uid), {})

def set_user_accounts(uid: int, accs: dict):
    all_data = load_accounts()
    all_data[str(uid)] = accs
    save_accounts(all_data)

def load_admins() -> dict:
    """admins.json -> {id: {settings, ie, type, added_by, added_at}}
    (পুরনো list format হলে auto-migrate হয়: settings=OFF, import/export=ON, NORMAL)"""
    if not os.path.exists(ADMIN_FILE):
        return {}
    try:
        with open(ADMIN_FILE) as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    if isinstance(raw, list):
        for x in raw:
            try:
                out[int(x)] = {"settings": False, "ie": True, "type": "normal",
                               "added_by": OWNER_ID, "added_at": 0}
            except (TypeError, ValueError):
                pass
    elif isinstance(raw, dict):
        for k, v in raw.items():
            try:
                aid = int(k)
            except (TypeError, ValueError):
                continue
            v = v if isinstance(v, dict) else {}
            out[aid] = {
                "settings": bool(v.get("settings", False)),
                "ie": bool(v.get("ie", True)),
                "type": "premium" if v.get("type") == "premium" else "normal",
                "added_by": int(v.get("added_by") or OWNER_ID),
                "added_at": int(v.get("added_at") or 0),
            }
    return out

def save_admins():
    _atomic_write_json(ADMIN_FILE, {str(k): v for k, v in admins.items()})

admins: dict = load_admins()

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_admin_user(uid: int) -> bool:
    return uid == OWNER_ID or uid in admins

def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and is_admin_user(u.id))

# ── Admin permissions (Owner = সব access) ──────────────────────────────────
def can_manage_admins(uid: int) -> bool:
    """ADMIN SETTING ACCESS ON থাকলে Owner এর মতো admin add/remove করতে পারবে"""
    return is_owner(uid) or bool(admins.get(uid, {}).get("settings"))

def can_import_export(uid: int) -> bool:
    """BOT IMPORT & EXPORT ON থাকলে import/export button + access থাকবে"""
    return is_owner(uid) or bool(admins.get(uid, {}).get("ie"))

def managed_admins(uid: int) -> list:
    """Owner সব admin দেখবে; settings-access admin শুধু নিজের add করা admin দেখবে"""
    if is_owner(uid):
        return sorted(admins)
    return sorted(a for a, info in admins.items() if info.get("added_by") == uid)

# ── Account lending (Premium admin) ────────────────────────────────────────
# Lent account এর data lender এর list এই থাকে, শুধু info["lent_to"] = admin_id বসে।
#   lender  : সব দেখতে ও export করতে পারে, কিন্তু use/remove পারে না (admin remove না করা পর্যন্ত)
#   receiver: শুধু use করতে পারে (number masked, remove/export নেই)
def mask_phone(p: str) -> str:
    if len(p) <= 7:
        return p[:2] + "•" * max(0, len(p) - 2)
    return p[:5] + "•" * (len(p) - 7) + p[-2:]

def lent_in_accounts(uid: int) -> dict:
    """অন্য কেউ uid কে যে account গুলো দিয়েছে"""
    out = {}
    for owner_id, accs in load_accounts().items():
        if owner_id == str(uid):
            continue
        for ph, info in accs.items():
            if info.get("lent_to") == uid:
                out[ph] = info
    return out

def lent_counts() -> dict:
    """{admin_id: কতগুলো account পেয়েছে}"""
    out: dict = {}
    for accs in load_accounts().values():
        for info in accs.values():
            t = info.get("lent_to")
            if t:
                out[t] = out.get(t, 0) + 1
    return out

def visible_accounts(uid: int) -> list:
    """ALL ACCOUNT list: [(phone, info, kind)]   kind = own | lent_out | lent_in"""
    own = user_accounts(uid)
    rows = [(ph, info, "lent_out" if info.get("lent_to") else "own")
            for ph, info in own.items()]
    for ph, info in lent_in_accounts(uid).items():
        if ph not in own:
            rows.append((ph, info, "lent_in"))
    return rows

def usable_accounts(uid: int) -> dict:
    """Join/VC এ যে account গুলো use করা যাবে (own + পাওয়া account; দিয়ে দেওয়া গুলো বাদ)"""
    return {ph: info for ph, info, kind in visible_accounts(uid) if kind != "lent_out"}

def lendable_phones(uid: int) -> list:
    """যে account গুলো এখনো কাউকে দেওয়া হয়নি (Your Have Accounts)"""
    return [ph for ph, info in user_accounts(uid).items() if not info.get("lent_to")]

# ═══════════════════════════════════════════════════════════════════════════════
# STATE CACHES
# ═══════════════════════════════════════════════════════════════════════════════
active_clients: dict = {}
pending_clients: dict = {}
call_engines: dict = {}
live_handlers: dict = {}
live_state: dict = {}
watchdog_tasks: dict = {}
live_sessions: dict = {}
user_flow: dict = {}
admin_drafts: dict = {}   # (uid, message_id) -> add-admin draft

S_WAIT_PHONE     = "wait_phone"
S_WAIT_CODE      = "wait_code"
S_WAIT_2FA       = "wait_2fa"
S_WAIT_LIVE_LINK = "wait_live_link"
S_WAIT_LIVE_CNT  = "wait_live_count"
S_WAIT_GRP_LINK  = "wait_grp_link"
S_WAIT_RM_ACC    = "wait_rm_acc"
S_WAIT_ADD_ADMIN = "wait_add_admin"
S_WAIT_RM_ADMIN  = "wait_rm_admin"
S_WAIT_IMPORT    = "wait_import"
S_WAIT_ADMIN_ACCS = "wait_admin_accs"

async def get_client(uid: int, phone: str, session_str: str) -> TelegramClient:
    key = (uid, phone)
    if key in active_clients:
        c = active_clients[key]
        if not c.is_connected():
            try:
                await c.connect()
            except Exception:
                pass
        return c
    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await c.connect()
    active_clients[key] = c
    return c

# ═══════════════════════════════════════════════════════════════════════════════
# UI / KEYBOARDS
# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Bot API 9.0+ button color — works on Telegram ≥ Feb 9 2026
_R = {"style": "danger"}  # "danger" = red  |  "success" = green  |  "primary" = blue
def _G(on: bool = True) -> dict:
    return {"style": "success"} if on else _R

def _yn(on: bool) -> str:
    return "🟢" if on else "🔴"

def access_denied_kb(user_id: int) -> InlineKeyboardMarkup:
    msg_text = (
        f"𝗜 𝗡𝗲𝗲𝗱 𝗩𝗰 𝗝𝗼𝗶𝗻 𝗕𝗼𝘁 𝗔𝗰𝗰𝗲𝘀𝘀.\n\n"
        f"𝗠𝘆 𝗨𝘀𝗲𝗿 𝗜𝗱 : {user_id}"
    )
    if _owner_username_cache:
        url = f"https://t.me/{_owner_username_cache}?text={urllib.parse.quote(msg_text)}"
    else:
        url = f"tg://user?id={OWNER_ID}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚨 𝗔𝗖𝗖𝗘𝗦𝗦 𝗡𝗘𝗘𝗗", url=url, api_kwargs=_R)
    ]])

def get_kb(uid: int) -> ReplyKeyboardMarkup:
    """Owner = সব button. Admin = permission অনুযায়ী (OFF হলে button-ই দেখাবে না)"""
    rows = [
        [KeyboardButton("👨‍👩‍👦 𝗝𝗢𝗜𝗡 𝗟𝗜𝗩𝗘", api_kwargs=_R), KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗖𝗛/𝗚𝗥𝗣", api_kwargs=_R)],
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", api_kwargs=_R),  KeyboardButton("🗄 𝗔𝗟𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", api_kwargs=_R)],
    ]
    if can_manage_admins(uid):
        rows.append([KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗗𝗠𝗜𝗡", api_kwargs=_R), KeyboardButton("🗄 𝗔𝗟𝗟 𝗔𝗗𝗠𝗜𝗡", api_kwargs=_R)])
    if can_import_export(uid):
        rows.append([KeyboardButton("📥 𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧", api_kwargs=_R), KeyboardButton("📤 𝗕𝗢𝗧 𝗘𝗫𝗣𝗢𝗥𝗧", api_kwargs=_R)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

KEYBOARD_PATTERNS = [
    "𝗝𝗢𝗜𝗡 𝗟𝗜𝗩𝗘", "𝗝𝗢𝗜𝗡 𝗖𝗛/𝗚𝗥𝗣",
    "𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", "𝗔𝗟𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧",
    "𝗔𝗗𝗗 𝗔𝗗𝗠𝗜𝗡", "𝗔𝗟𝗟 𝗔𝗗𝗠𝗜𝗡",
    "𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧", "𝗕𝗢𝗧 𝗘𝗫𝗣𝗢𝗥𝗧",
]

def is_keyboard_button(text: str) -> bool:
    return any(p in text for p in KEYBOARD_PATTERNS)

def _format_join_status(sd: dict) -> str:
    return (
        f"🤖 𝗧𝗼𝘁𝗮𝗹 𝗕𝗼𝘁 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 : {sd.get('total', 0)}\n\n"
        f"🛜 𝗣𝗮𝗻𝗱𝗶𝗻𝗴 𝗝𝗼𝗶𝗻 : {max(0, sd.get('pending', 0))}\n\n"
        f"⏺ 𝗔𝗹𝗿𝗲𝗮𝗱𝘆 𝗝𝗼𝗶𝗻 : {sd.get('already', 0)}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# /start + ACCESS DENIED
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        uid = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(
            f"🚫 <b>𝗬𝗼𝘂 𝗡𝗼𝘁 𝗛𝗮𝘃𝗲 𝗔𝗻𝘆 𝗔𝗰𝗰𝗲𝘀𝘀.</b>\n\n"
            f"📮 <b>𝗬𝗼𝘂𝗿 𝗜𝗱 :</b> <code>{uid}</code>\n\n"
            f"📌 <b>𝗬𝗼𝘂 𝗡𝗲𝗲𝗱 𝗔𝗰𝗰𝗲𝘀𝘀 𝗧𝗵𝗲𝗻 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿.</b>",
            parse_mode="HTML",
            reply_markup=access_denied_kb(uid)
        )
        return
    uid = update.effective_user.id
    user_flow.pop(uid, None)
    if is_owner(uid):
        msg = ("𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝗧𝗼 𝗩𝗰 𝗝𝗼𝗶𝗻 𝗕𝗼𝘁 𝗢𝘄𝗻𝗲𝗿 𝗕𝗮𝗯𝘂 🥰\n\n"
               "𝗬𝗼𝘂 𝗖𝗮𝗻 𝗨𝘀𝗲 𝗔𝗻𝘆𝘁𝗵𝗶𝗻𝗴𝘀...")
    else:
        msg = ("𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝗧𝗼 𝗩𝗰 𝗝𝗼𝗶𝗻 𝗕𝗼𝘁 🥰\n\n"
               "𝗡𝗼𝘄 𝗬𝗼𝘂 𝗖𝗮𝗻 𝗨𝘀𝗲 𝗧𝗵𝗶𝘀 𝗕𝗼𝘁....")
    await update.message.reply_text(msg, reply_markup=get_kb(uid))

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    user_flow.pop(uid, None)
    await update.message.reply_text("❌ Cancelled.", reply_markup=get_kb(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# ADD ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════
async def start_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    n = len(user_accounts(uid))
    await update.message.reply_text(
        f"📊 <b>𝗬𝗼𝘂𝗿 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 : {n}</b>\n\n"
        f"📱 নতুন একাউন্টের নাম্বার দিন 👇\n\n"
        f"<i>Example : +8801XXXXXXXXX</i>",
        parse_mode="HTML"
    )

async def handle_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = update.message.text.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    accs = user_accounts(uid)
    if phone in accs:
        user_flow.pop(uid, None)
        await update.message.reply_text("⚠️ এই নাম্বার ইতিমধ্যে আপনার লিস্টে আছে!",
                                        reply_markup=get_kb(uid))
        return
    user_flow[uid]["data"]["phone"] = phone

    status_msg = await update.message.reply_text(
        "🔄 <i>Connecting to Telegram servers...</i>", parse_mode="HTML")
    try:
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        pending_clients[(uid, phone)] = c
        await status_msg.edit_text("⏳ <i>Sending verification code...</i>", parse_mode="HTML")
        r = await c.send_code_request(phone)
        user_flow[uid]["data"]["code_hash"] = r.phone_code_hash
        user_flow[uid]["state"] = S_WAIT_CODE
        await status_msg.edit_text(
            f"✅ <b>Code Sent Successfully!</b>\n\n"
            f"📱 <code>{html.escape(phone)}</code>\n\n"
            f"🔐 <b>ভেরিফিকেশন কোড দিন</b> 👇",
            parse_mode="HTML"
        )
    except FloodWaitError as e:
        user_flow.pop(uid, None)
        await status_msg.edit_text(f"⏳ Flood wait: {e.seconds}s")
    except Exception as e:
        user_flow.pop(uid, None)
        await status_msg.edit_text(f"❌ Error: {html.escape(str(e))}")

async def handle_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = user_flow[uid]["data"]["phone"]
    client = pending_clients.get((uid, phone))
    if not client:
        user_flow.pop(uid, None)
        return await update.message.reply_text("❌ Session expired.",
                                               reply_markup=get_kb(uid))
    code = update.message.text.strip().replace(" ", "")
    try:
        await client.sign_in(phone, code,
                             phone_code_hash=user_flow[uid]["data"]["code_hash"])
        await _save_account(update, uid, client, phone)
    except SessionPasswordNeededError:
        user_flow[uid]["state"] = S_WAIT_2FA
        await update.message.reply_text("🔐 <b>2FA Password দিন</b> 👇", parse_mode="HTML")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text("❌ ভুল বা মেয়াদ শেষ কোড।",
                                        reply_markup=get_kb(uid))
    except Exception as e:
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text(f"❌ Error: {html.escape(str(e))}",
                                        reply_markup=get_kb(uid))

async def handle_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = user_flow[uid]["data"]["phone"]
    client = pending_clients.get((uid, phone))
    if not client:
        user_flow.pop(uid, None)
        return await update.message.reply_text("❌ Session expired.",
                                               reply_markup=get_kb(uid))
    pwd = update.message.text.strip()
    try:
        await client.sign_in(password=pwd)
        await _save_account(update, uid, client, phone)
    except Exception as e:
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text(f"❌ Wrong password: {html.escape(str(e))}",
                                        reply_markup=get_kb(uid))

async def _save_account(update: Update, uid: int, client: TelegramClient, phone: str):
    me = await client.get_me()
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
    accs = user_accounts(uid)
    accs[phone] = {
        "name": name,
        "session": client.session.save(),
        "added_at": int(time.time()),
        "status": "active",
    }
    set_user_accounts(uid, accs)
    active_clients[(uid, phone)] = client
    pending_clients.pop((uid, phone), None)
    user_flow.pop(uid, None)
    await update.message.reply_text(
        f"🎉 <b>LOGIN SUCCESSFUL</b> 🎉\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name:</b> {html.escape(name)}\n"
        f"📱 <b>Number:</b> <code>{html.escape(phone)}</code>\n"
        f"✅ <b>Status:</b> Active\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💫 <i>Account added to your list</i>",
        parse_mode="HTML",
        reply_markup=get_kb(uid)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ALL ACCOUNT (pagination + status check)
# ═══════════════════════════════════════════════════════════════════════════════
async def _check_status_quick(session_str: str) -> str:
    c = None
    try:
        c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await c.connect()
        me = await asyncio.wait_for(c.get_me(), timeout=2.5)
        return "🟢" if me else "🔴"
    except Exception:
        return "🔴"
    finally:
        if c:
            try: await c.disconnect()
            except Exception: pass

async def show_accounts(src, ctx: ContextTypes.DEFAULT_TYPE, uid: int, page: int = 0):
    accs = visible_accounts(uid)
    is_cb = isinstance(src, Update) and src.callback_query is not None

    if not accs:
        text = "📋 <b>আপনার কোনো অ্যাকাউন্ট নেই।</b>"
        if is_cb:
            await src.callback_query.edit_message_text(text, parse_mode="HTML")
        else:
            await src.message.reply_text(text, parse_mode="HTML", reply_markup=get_kb(uid))
        return

    items = accs
    total = len(items)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PER_PAGE
    page_items = items[start:start + PER_PAGE]

    statuses = await asyncio.gather(*[
        _check_status_quick(info.get("session", "")) for _, info, _k in page_items
    ])

    lines = [f"⏺ <b>Account List Page Number : {page + 1}</b>\n"]
    for i, ((ph, info, kind), st) in enumerate(zip(page_items, statuses)):
        n = start + i + 1
        ts = info.get("added_at", 0)
        try:
            added = datetime.fromtimestamp(int(ts)).strftime("%d %b %Y")
        except Exception:
            added = "—"
        shown = mask_phone(ph) if kind == "lent_in" else ph     # পাওয়া account এর number পুরো দেখাবে না
        mark = {"lent_out": " 🔒", "lent_in": " 🎁"}.get(kind, "")
        lines.append(f"{n}. <code>{html.escape(shown)}</code> - {added} - {st}{mark}")
    if any(k == "lent_out" for _, _, k in page_items):
        lines.append("\n🔒 <i>Admin কে দেওয়া (use/remove বন্ধ)</i>")
    if any(k == "lent_in" for _, _, k in page_items):
        lines.append("\n🎁 <i>আপনাকে দেওয়া (শুধু use করা যাবে)</i>")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️ PRV", callback_data=f"acc:{page - 1}", api_kwargs={"style": "success"}),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="acc:noop", api_kwargs=_R),
            InlineKeyboardButton("NXT ▶️", callback_data=f"acc:{page + 1}", api_kwargs={"style": "success"}),
        ],
        [InlineKeyboardButton("🗑️ 𝗥𝗘𝗠𝗢𝗩𝗘 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", callback_data=f"acc_rm:{page}", api_kwargs=_R)],
    ])

    if is_cb:
        await src.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await src.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def handle_rm_acc(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        n = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("❌ একটি সংখ্যা দিন (list number)")
    rows = visible_accounts(uid)
    if not (1 <= n <= len(rows)):
        return await update.message.reply_text(f"❌ 1 থেকে {len(rows)} এর মধ্যে দিন")
    phone, _info, kind = rows[n - 1]
    if kind == "lent_out":
        return await update.message.reply_text(
            "🔒 এই account টি admin কে দেওয়া আছে।\n"
            "সেই admin remove না করা পর্যন্ত remove করা যাবে না।")
    if kind == "lent_in":
        return await update.message.reply_text(
            "🔒 এই account টি আপনাকে দেওয়া হয়েছে — আপনি remove করতে পারবেন না।")
    accs = user_accounts(uid)
    info = accs.pop(phone)
    set_user_accounts(uid, accs)

    key = (uid, phone)
    if key in active_clients:
        try: await active_clients[key].log_out()
        except Exception:
            try: await active_clients[key].disconnect()
            except Exception: pass
        active_clients.pop(key, None)
    else:
        try:
            c = TelegramClient(StringSession(info.get("session", "")), API_ID, API_HASH)
            await c.connect()
            try: await c.log_out()
            except Exception: pass
            await c.disconnect()
        except Exception: pass

    user_flow.pop(uid, None)
    await update.message.reply_text(
        f"✅ <b>Account Removed</b>\n\n📱 <code>{html.escape(phone)}</code>",
        parse_mode="HTML", reply_markup=get_kb(uid)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# JOIN LIVE
# ═══════════════════════════════════════════════════════════════════════════════
async def ask_live_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎥 <b>Live link বা @username দিন</b> 👇\n\n"
        "<i>Public : @channelname / t.me/channelname</i>\n"
        "<i>Private : t.me/+xxxxxxxx</i>",
        parse_mode="HTML"
    )

async def ask_grp_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🟢 <b>Channel / Group link বা @username দিন</b> 👇\n\n"
        "<i>Public : @channelname / t.me/channelname</i>\n"
        "<i>Private : t.me/+xxxxxxxx</i>",
        parse_mode="HTML"
    )

async def handle_live_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    link = update.message.text.strip()
    accs = usable_accounts(uid)
    if not accs:
        user_flow.pop(uid, None)
        return await update.message.reply_text("❌ আগে অ্যাকাউন্ট যোগ করুন।",
                                               reply_markup=get_kb(uid))
    user_flow[uid]["data"]["link"] = link

    total = len(accs)
    sd = {"total": total, "pending": total, "already": 0}
    status_msg = await update.message.reply_text(_format_join_status(sd), parse_mode="HTML")

    joined_phones = []
    for phone, info in accs.items():
        try:
            client = await get_client(uid, phone, info.get("session", ""))
            entity, _new = await _ensure_joined(client, link)
            if entity:
                joined_phones.append(phone)
                sd["already"] += 1
            sd["pending"] -= 1
        except Exception as e:
            logging.warning(f"[JOIN-GRP] {phone}: {e}")
            sd["pending"] -= 1
        try:
            await status_msg.edit_text(_format_join_status(sd), parse_mode="HTML")
        except Exception:
            pass

    if not joined_phones:
        user_flow.pop(uid, None)
        return await status_msg.edit_text(
            "❌ কোনো account group এ join করতে পারিনি।\n"
            "Link চেক করুন অথবা group এ access আছে কিনা দেখুন।"
        )

    user_flow[uid]["data"]["joined_phones"] = joined_phones

    if len(joined_phones) == 1:
        user_flow[uid]["data"]["count"] = 1
        vc_msg = await update.message.reply_text(
            "🔄 <i>Starting VC join...</i>", parse_mode="HTML")
        return await _do_vc_join(update, ctx, uid, vc_msg)

    user_flow[uid]["state"] = S_WAIT_LIVE_CNT
    await update.message.reply_text(
        f"✅ <b>{len(joined_phones)}</b> account group এ join হয়েছে।\n\n"
        f"🎯 কতটি দিয়ে VC-তে Join করাবেন? <b>(1-{len(joined_phones)})</b>",
        parse_mode="HTML"
    )

async def handle_live_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    joined_phones = user_flow[uid]["data"].get("joined_phones", [])
    max_n = len(joined_phones)
    try:
        n = int(update.message.text.strip())
        if not 1 <= n <= max_n:
            raise ValueError
    except (ValueError, TypeError):
        return await update.message.reply_text(f"⚠️ 1 থেকে {max_n} এর মধ্যে দিন")
    user_flow[uid]["data"]["count"] = n
    user_flow[uid]["state"] = None
    vc_msg = await update.message.reply_text("🔄 <i>Starting VC join...</i>",
                                              parse_mode="HTML")
    await _do_vc_join(update, ctx, uid, vc_msg)

async def _do_vc_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                      uid: int, status_msg):
    data = user_flow[uid]["data"]
    link = data["link"]
    count = data["count"]
    joined_phones = data.get("joined_phones", [])
    accs = usable_accounts(uid)
    phones = joined_phones[:count]

    sd = {"total": len(phones), "pending": len(phones), "already": 0}
    session_id = os.urandom(8).hex()
    session_phones = []
    session_calls = {}
    session_engines = {}
    vchat = None

    for phone in phones:
        info = accs.get(phone)
        if not info:
            sd["pending"] -= 1
            continue
        try:
            client = await get_client(uid, phone, info["session"])
            entity, _new = await _ensure_joined(client, link)
            if not entity:
                sd["pending"] -= 1
                continue

            call = None
            try:
                if isinstance(entity, Channel):
                    full = await client(GetFullChannelRequest(entity))
                else:
                    full = await client(GetFullChatRequest(entity.id))
                call = getattr(full.full_chat, "call", None)
            except Exception:
                try:
                    full = await client(GetFullChatRequest(entity.id))
                    call = getattr(full.full_chat, "call", None)
                except Exception:
                    call = None

            if call is None:
                sd["pending"] -= 1
                continue

            me = await client.get_me()
            engine = await _get_engine(uid, phone, client)
            vchat_id = get_peer_id(entity)
            await _engine_join(uid, phone, engine, vchat_id, call, me, client)

            handler = _make_remute_handler(client, call, me.id, phone)
            client.add_event_handler(handler, events.Raw(UpdateGroupCallParticipants))
            live_handlers.setdefault((uid, phone), []).append(handler)

            st = {"engine": engine, "chat_id": vchat_id,
                  "call": call, "client": client, "me": me}
            live_state[(uid, phone)] = st
            _start_watchdog(uid, phone, st)

            session_phones.append(phone)
            session_calls[phone] = call
            session_engines[phone] = engine
            vchat = vchat_id
            sd["already"] += 1
            sd["pending"] -= 1
        except NoActiveGroupCall:
            sd["pending"] -= 1
            logging.warning(f"[VC] {phone}: NoActiveGroupCall")
        except Exception as e:
            logging.exception(f"[VC] {phone} failed")
            sd["pending"] -= 1
        try:
            await status_msg.edit_text(_format_join_status(sd), parse_mode="HTML")
        except Exception:
            pass

    user_flow.pop(uid, None)

    if not session_phones:
        return await status_msg.edit_text(
            "❌ কোনো account VC-তে join করতে পারিনি।\n"
            "Live চালু আছে কিনা চেক করুন।"
        )

    live_sessions[session_id] = {
        "uid": uid,
        "phones": session_phones,
        "calls": session_calls,
        "engines": session_engines,
        "chat_id": vchat,
    }

    await status_msg.edit_text(
        f"✅ <b>𝗬𝗼𝘂 𝗟𝗶𝘃𝗲 𝗝𝗼𝗶𝗻 𝗥𝗲𝗾 𝗦𝘂𝗰𝗰𝗲𝘀𝘀</b> ✅\n\n"
        f"🎯 <b>𝗬𝗼𝘂𝗿 𝗧𝗮𝗿𝗴𝗲𝘁 𝗠𝗲𝗺𝗯𝗲𝗿𝘀 𝗟𝗶𝘃𝗲 𝗝𝗼𝗼𝗶𝗻....</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✔️ Joined : <b>{len(session_phones)}</b> / {count}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💣 𝗟𝗘𝗔𝗩𝗘 𝗧𝗢 𝗩𝗖",
                                 callback_data=f"leave:{session_id}", api_kwargs=_R)
        ]])
    )

# ═══════════════════════════════════════════════════════════════════════════════
# JOIN CHANNEL / GROUP
# ═══════════════════════════════════════════════════════════════════════════════
async def handle_grp_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    link = update.message.text.strip()
    accs = usable_accounts(uid)
    if not accs:
        user_flow.pop(uid, None)
        return await update.message.reply_text("❌ আগে অ্যাকাউন্ট যোগ করুন।",
                                               reply_markup=get_kb(uid))

    total = len(accs)
    sd = {"total": total, "pending": total, "already": 0}
    status_msg = await update.message.reply_text(_format_join_status(sd), parse_mode="HTML")

    for phone, info in accs.items():
        try:
            client = await get_client(uid, phone, info["session"])
            entity, _new = await _ensure_joined(client, link)
            if entity:
                sd["already"] += 1
            sd["pending"] -= 1
        except Exception as e:
            logging.warning(f"[GRP] {phone}: {e}")
            sd["pending"] -= 1
        try:
            await status_msg.edit_text(_format_join_status(sd), parse_mode="HTML")
        except Exception:
            pass

    user_flow.pop(uid, None)
    await status_msg.edit_text(
        _format_join_status(sd) + "\n\n✅ <b>Join Completed!</b>",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ADD ADMIN / ALL ADMIN
# ═══════════════════════════════════════════════════════════════════════════════
ADMIN_SETTING_TITLE = "𝗔𝗗𝗠𝗜𝗡 𝗦𝗘𝗧𝗧𝗜𝗡𝗚 𝗢𝗡/𝗢𝗙𝗙 🐍"

def draft_settings_kb(d: dict) -> InlineKeyboardMarkup:
    # সব button red; ON করলে green. PREMIUM/NORMAL এর একটাই green থাকবে. NEXT সবসময় green.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪖 𝗔𝗗𝗠𝗜𝗡 𝗦𝗘𝗧𝗧𝗜𝗡𝗚 𝗔𝗖𝗖𝗘𝗦𝗦", callback_data="nadm:set",
                              api_kwargs=_G(d["settings"]))],
        [InlineKeyboardButton("♻️ 𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧 & 𝗘𝗫𝗣𝗢𝗥𝗧", callback_data="nadm:ie",
                              api_kwargs=_G(d["ie"]))],
        [InlineKeyboardButton("𝗣𝗥𝗘𝗠𝗜𝗨𝗠", callback_data="nadm:prem",
                              api_kwargs=_G(d["type"] == "premium")),
         InlineKeyboardButton("𝗡𝗢𝗥𝗠𝗔𝗟", callback_data="nadm:norm",
                              api_kwargs=_G(d["type"] == "normal"))],
        [InlineKeyboardButton("𝗡𝗘𝗫𝗧", callback_data="nadm:next", api_kwargs=_G(True))],
    ])

def draft_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("𝗬𝗘𝗦", callback_data="nadm:yes", api_kwargs=_G(True)),
        InlineKeyboardButton("𝗡𝗢𝗢", callback_data="nadm:no", api_kwargs=_R),
    ]])

def _draft_summary(d: dict) -> str:
    return (
        f"👑 <b>𝗨𝘀𝗲𝗿 𝗜𝗱 :</b> <code>{d['target']}</code>\n\n"
        f"🪖 <b>𝗔𝗗𝗠𝗜𝗡 𝗦𝗘𝗧𝗧𝗜𝗡𝗚 𝗔𝗖𝗖𝗘𝗦𝗦 :</b> {_yn(d['settings'])}\n\n"
        f"♻️ <b>𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧 &amp; 𝗘𝗫𝗣𝗢𝗥𝗧</b> {_yn(d['ie'])}\n\n"
        f"💡 <b>𝗔𝗗𝗠𝗜𝗡 𝗧𝗬𝗣𝗘 :</b> {'𝗣𝗥𝗘𝗠𝗜𝗨𝗠' if d['type'] == 'premium' else '𝗡𝗢𝗥𝗠𝗔𝗟'}"
    )

async def ask_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆔 <b>Admin user ID দিন</b> 👇", parse_mode="HTML")

async def _begin_add_admin(update: Update, uid: int, target: int):
    """User ID পাওয়ার পর ADMIN SETTING ON/OFF message দেখায়"""
    if target == OWNER_ID:
        return await update.message.reply_text("⚠️ এটা owner ID।", reply_markup=get_kb(uid))
    if target == uid or target in admins:
        return await update.message.reply_text("⚠️ Already admin।", reply_markup=get_kb(uid))
    d = {"target": target, "settings": False, "ie": False,
         "type": "normal", "stage": "settings"}
    sent = await update.message.reply_text(ADMIN_SETTING_TITLE,
                                           reply_markup=draft_settings_kb(d))
    admin_drafts[(uid, sent.message_id)] = d

async def handle_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    if not can_manage_admins(uid):
        user_flow.pop(uid, None)
        return
    try:
        target = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("❌ Valid user ID দিন (সংখ্যা)।")
    user_flow.pop(uid, None)
    await _begin_add_admin(update, uid, target)

def _create_admin(target: int, d: dict, added_by: int):
    admins[target] = {
        "settings": bool(d["settings"]),
        "ie": bool(d["ie"]),
        "type": d["type"],
        "added_by": added_by,
        "added_at": int(time.time()),
    }
    save_admins()

async def _release_runtime(uid: int, phones):
    """account হাতবদল হওয়ার আগে uid এর ওই account গুলোর live VC / client বন্ধ করে"""
    for phone in phones:
        key = (uid, phone)
        _stop_watchdog(uid, phone)
        st = live_state.pop(key, {})
        client = active_clients.get(key)
        for h in live_handlers.pop(key, []):
            try:
                if client:
                    client.remove_event_handler(h)
            except Exception:
                pass
        engine, vchat = st.get("engine"), st.get("chat_id")
        if engine is not None and vchat is not None:
            try: await engine.leave_call(vchat)
            except Exception: pass
        call_engines.pop(key, None)
        if client:
            try: await client.disconnect()
            except Exception: pass
        active_clients.pop(key, None)
        for sid, s in list(live_sessions.items()):
            if s["uid"] == uid and phone in s["phones"]:
                s["phones"].remove(phone)
                s["calls"].pop(phone, None)
                s["engines"].pop(phone, None)
                if not s["phones"]:
                    live_sessions.pop(sid, None)

async def admin_draft_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not can_manage_admins(uid):
        return await q.answer("⛔ আপনার এই access নেই।", show_alert=True)

    key = (uid, q.message.message_id)
    d = admin_drafts.get(key)
    if not d:
        await q.answer("⚠️ Request expired, আবার চেষ্টা করুন।", show_alert=True)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return
    action = q.data.split(":", 1)[1]

    # ── Settings stage: toggle buttons ──
    if action in ("set", "ie", "prem", "norm"):
        if d.get("stage") != "settings":
            return await q.answer()
        if action == "set":
            d["settings"] = not d["settings"]
        elif action == "ie":
            if not can_import_export(uid):      # নিজের যে access নেই সেটা অন্যকে দেওয়া যাবে না
                return await q.answer("⛔ এই access আপনার নিজের নেই।", show_alert=True)
            d["ie"] = not d["ie"]
        elif action == "prem":
            d["type"] = "premium"
        else:
            d["type"] = "normal"
        await q.answer()
        try: await q.edit_message_reply_markup(reply_markup=draft_settings_kb(d))
        except Exception: pass      # same state হলে Telegram "not modified" দেয়
        return

    # ── NEXT → confirm message (আগের message মুছে নতুন message) ──
    if action == "next":
        if d.get("stage") != "settings":
            return await q.answer()
        await q.answer()
        admin_drafts.pop(key, None)
        try: await q.message.delete()
        except Exception: pass
        d["stage"] = "confirm"
        sent = await ctx.bot.send_message(
            q.message.chat_id,
            _draft_summary(d) + "\n\n𝗬𝗼𝘂 𝗖𝗼𝗻𝗳𝗿𝗶𝗺 𝗔𝗱𝗱 𝗧𝗵𝗶𝘀 𝗔𝗱𝗺𝗶𝗻?",
            parse_mode="HTML", reply_markup=draft_confirm_kb())
        admin_drafts[(uid, sent.message_id)] = d
        return

    if d.get("stage") != "confirm":
        return await q.answer()

    # ── NOO → cancel ──
    if action == "no":
        admin_drafts.pop(key, None)
        await q.answer()
        return await q.edit_message_text(
            f"👑 <b>𝗨𝘀𝗲𝗿 𝗜𝗱 :</b> <code>{d['target']}</code>\n\n"
            f"🗨️ <b>𝗖𝗮𝗻𝗻𝗰𝗲𝗹 𝗔𝗱𝗱 𝗔𝗱𝗺𝗶𝗻 𝗥𝗲𝗾...</b>",
            parse_mode="HTML")

    # ── YES ──
    if action == "yes":
        admin_drafts.pop(key, None)
        await q.answer()
        target = d["target"]
        if target == OWNER_ID or target == uid or target in admins:
            return await q.edit_message_text("⚠️ Already admin।")

        if d["type"] == "normal":                     # NORMAL: আর কিছু জিজ্ঞেস করবে না
            _create_admin(target, d, uid)
            return await q.edit_message_text(
                _draft_summary(d) + "\n\n✅ <b>𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆</b>",
                parse_mode="HTML")

        avail = len(lendable_phones(uid))             # PREMIUM: কয়টা account দেবে?
        if avail == 0:
            return await q.edit_message_text(
                _draft_summary(d) + "\n\n❌ <b>আপনার কোনো available account নেই,</b>\n"
                "তাই Premium admin add করা গেল না।",
                parse_mode="HTML")
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        user_flow[uid] = {"state": S_WAIT_ADMIN_ACCS, "data": {"draft": d}}
        await ctx.bot.send_message(
            q.message.chat_id,
            f"𝗬𝗼𝘂𝗿 𝗛𝗮𝘃𝗲 𝗔𝗰𝗰𝗼𝘂𝗻𝘁𝘀 : {avail}\n\n"
            f"এখন আপনি আপনার এডমিন কে কয়টি একাউন্ট দিতে চাচ্ছেন?\n"
            f"(1-{avail})")
        return

    await q.answer()

async def handle_admin_acc_count(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    d = user_flow[uid]["data"]["draft"]
    target = d["target"]
    if not can_manage_admins(uid) or target == OWNER_ID or target == uid or target in admins:
        user_flow.pop(uid, None)
        return await update.message.reply_text("⚠️ Request বাতিল হয়েছে।", reply_markup=get_kb(uid))
    pool = lendable_phones(uid)
    if not pool:
        user_flow.pop(uid, None)
        return await update.message.reply_text("❌ আপনার কোনো available account নেই।",
                                               reply_markup=get_kb(uid))
    try:
        n = int(update.message.text.strip())
        if not 1 <= n <= len(pool):
            raise ValueError
    except (ValueError, TypeError):
        return await update.message.reply_text(f"⚠️ 1 থেকে {len(pool)} এর মধ্যে দিন")

    chosen = pool[:n]                       # এখনো কাউকে দেওয়া হয়নি এমন account থেকে
    await _release_runtime(uid, chosen)     # দাতার চলমান VC/client থাকলে বন্ধ
    accs = user_accounts(uid)
    for ph in chosen:
        accs[ph]["lent_to"] = target
    set_user_accounts(uid, accs)
    _create_admin(target, d, uid)
    user_flow.pop(uid, None)
    await update.message.reply_text(
        _draft_summary(d) + "\n\n"
        f"🎁 <b>𝗔𝗰𝗰𝗼𝘂𝗻𝘁𝘀 𝗚𝗶𝘃𝗲𝗻 :</b> {n}\n"
        f"📦 <b>𝗬𝗼𝘂𝗿 𝗥𝗲𝗺𝗮𝗶𝗻𝗶𝗻𝗴 :</b> {len(pool) - n}\n\n"
        f"✅ <b>𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆</b>",
        parse_mode="HTML", reply_markup=get_kb(uid))

async def remove_admin(target: int, by: int) -> int:
    """admin remove করে; তাকে দেওয়া account গুলো দাতার কাছে ফেরত যায়। ফেরত account সংখ্যা return করে"""
    all_data = load_accounts()
    returned = []
    for accs in all_data.values():
        for ph, info in accs.items():
            if info.get("lent_to") == target:
                info.pop("lent_to", None)
                returned.append(ph)
    if returned:
        save_accounts(all_data)
        await _release_runtime(target, returned)   # তার চলমান VC/client বন্ধ
    for info in admins.values():                   # তার add করা admin রা remover এর under এ যাবে
        if info.get("added_by") == target:
            info["added_by"] = by
    admins.pop(target, None)
    save_admins()
    user_flow.pop(target, None)
    return len(returned)

async def show_admins(src, ctx: ContextTypes.DEFAULT_TYPE, uid: int, page: int = 0):
    if not can_manage_admins(uid):
        return
    items = managed_admins(uid)
    total = len(items)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PER_PAGE
    page_items = items[start:start + PER_PAGE]
    given = lent_counts()

    lines = [f"⏺ <b>Admin List Page Number : {page + 1}</b>\n"]
    if not items:
        lines.append("(কোনো admin নেই)")
    for i, aid in enumerate(page_items):
        n = start + i + 1
        info = admins.get(aid, {})
        prem = info.get("type") == "premium"
        line = (f"{n}. <code>{aid}</code> - {'PREMIUM' if prem else 'NORMAL'}"
                f" - 🪖{_yn(info.get('settings'))} ♻️{_yn(info.get('ie'))}")
        if prem:
            line += f" - 🎁{given.get(aid, 0)}"
        if is_owner(uid) and info.get("added_by") not in (None, OWNER_ID):
            line += f" - ↳{info['added_by']}"
        lines.append(line)

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("◀️ PRV", callback_data=f"adm:{page - 1}", api_kwargs={"style": "success"}),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="adm:noop", api_kwargs=_R),
            InlineKeyboardButton("NXT ▶️", callback_data=f"adm:{page + 1}", api_kwargs={"style": "success"}),
        ],
        [InlineKeyboardButton("🗑️ 𝗥𝗘𝗠𝗢𝗩𝗘 𝗔𝗗𝗠𝗜𝗡", callback_data=f"adm_rm:{page}", api_kwargs=_R)],
    ])

    is_cb = isinstance(src, Update) and src.callback_query is not None
    if is_cb:
        await src.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await src.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def handle_rm_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    if not can_manage_admins(uid):
        return
    try:
        n = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("❌ একটি সংখ্যা দিন (list number)")
    items = managed_admins(uid)
    if not (1 <= n <= len(items)):
        return await update.message.reply_text(f"❌ 1 থেকে {len(items)} এর মধ্যে দিন")
    target = items[n - 1]
    returned = await remove_admin(target, uid)
    user_flow.pop(uid, None)
    extra = f"\n↩️ <b>Accounts Returned :</b> {returned}" if returned else ""
    await update.message.reply_text(
        f"🗑️ <b>Admin Removed</b>\n\n🆔 <code>{target}</code>{extra}",
        parse_mode="HTML", reply_markup=get_kb(uid)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# BOT IMPORT / EXPORT
# ═══════════════════════════════════════════════════════════════════════════════
async def ask_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📥 <b>Session strings এর ফাইল পাঠান</b>\n\n"
        "<i>JSON file (list of strings) অথবা plain text (per line)</i>",
        parse_mode="HTML"
    )

async def handle_import_doc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    uid = update.effective_user.id
    if user_flow.get(uid, {}).get("state") != S_WAIT_IMPORT or not can_import_export(uid):
        return
    doc = update.message.document
    if not doc:
        return await update.message.reply_text("❌ File পাঠান")

    try:
        f = await ctx.bot.get_file(doc.file_id)
        data = await f.download_as_bytearray()
        text = data.decode("utf-8", errors="ignore")
    except Exception as e:
        return await update.message.reply_text(f"❌ File read error: {e}")

    sessions = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            sessions = [s for s in parsed if isinstance(s, str)]
        elif isinstance(parsed, dict):
            sessions = [v for v in parsed.values() if isinstance(v, str)]
    except Exception:
        sessions = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if not sessions:
        return await update.message.reply_text("❌ কোনো valid session string পাওয়া যায়নি।")

    user_flow.pop(uid, None)
    total = len(sessions)
    pending = total
    success = 0

    status_msg = await update.message.reply_text(
        f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗦𝗧𝗥𝗜𝗡𝗚 : {total}\n\n"
        f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗣𝗔𝗡𝗗𝗜𝗡𝗚 : {pending}\n\n"
        f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗦𝗨𝗖𝗖𝗘𝗦𝗦 : {success}"
    )

    for i, sess in enumerate(sessions):
        pending -= 1
        try:
            c = TelegramClient(StringSession(sess), API_ID, API_HASH)
            await c.connect()
            me = await asyncio.wait_for(c.get_me(), timeout=10)
            if not me:
                raise Exception("Not authorized")
            phone = f"+{me.phone}" if me.phone else f"user_{me.id}"
            name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
            accs = user_accounts(uid)
            if phone not in accs:
                accs[phone] = {
                    "name": name,
                    "session": sess,
                    "added_at": int(time.time()),
                    "status": "active",
                }
                set_user_accounts(uid, accs)
            active_clients[(uid, phone)] = c
            success += 1
        except Exception as e:
            logging.warning(f"[IMPORT {i+1}] {e}")

        try:
            await status_msg.edit_text(
                f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗦𝗧𝗥𝗜𝗡𝗚 : {total}\n\n"
                f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗣𝗔𝗡𝗗𝗜𝗡𝗚 : {max(0, pending)}\n\n"
                f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗦𝗨𝗖𝗖𝗘𝗦𝗦 : {success}"
            )
        except Exception:
            pass

        if success > 0 and success % 10 == 0:
            try:
                await update.message.reply_text(
                    f"🎉 <b>10 accounts logged in!</b>\nTotal success: <b>{success}</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    try:
        await status_msg.edit_text(
            f"🎊 <b>𝗔𝗹𝗹 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗟𝗼𝗴𝗶𝗻 𝗦𝘂𝗰𝗰𝗲𝘀𝘀</b> 🎊\n\n"
            f"✅ Success: <b>{success}</b> / {total}\n\n"
            f"𝗡𝗼𝘄 𝗬𝗼𝘂 𝗨𝘀𝗲 𝗩𝗰 𝗕𝗼𝘁....",
            parse_mode="HTML",
            reply_markup=get_kb(uid)
        )
    except Exception:
        pass

async def do_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    accs = user_accounts(uid)
    if not accs:
        return await update.message.reply_text("📭 কোনো account নেই।")
    sessions = [info["session"] for info in accs.values() if info.get("session")]
    if not sessions:
        return await update.message.reply_text("📭 কোনো session string নেই।")

    buf = io.BytesIO(json.dumps(sessions, indent=2).encode())
    buf.name = f"accounts_{uid}.json"
    await update.message.reply_document(
        document=buf,
        caption=f"📤 <b>{len(sessions)}</b> session strings exported.",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ENSURE JOINED / CALL ENGINE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
async def _ensure_joined(client: TelegramClient, link: str):
    try:
        link = link.strip()
        if "t.me/+" in link or "joinchat/" in link:
            invite_hash = (link.split("/+")[-1] if "/+" in link
                           else link.split("joinchat/")[-1]).strip()
            try:
                result = await client(ImportChatInviteRequest(invite_hash))
                if result.chats:
                    return result.chats[0], True
                return None, None
            except UserAlreadyParticipantError:
                try:
                    check = await client(CheckChatInviteRequest(invite_hash))
                    if hasattr(check, "chat"):
                        return check.chat, False
                except Exception:
                    pass
                return None, None
        else:
            username = (link.replace("https://", "").replace("http://", "")
                        .replace("t.me/", "").replace("@", "")
                        .strip().split("/")[0])
            entity = await client.get_entity(username)
            try:
                await client(JoinChannelRequest(entity))
                return entity, True
            except UserAlreadyParticipantError:
                return entity, False
    except Exception as e:
        logging.error(f"[ENSURE_JOIN] {link} → {e}")
        return None, None

def _ensure_silence() -> str:
    try:
        if not os.path.exists(SILENCE_FILE):
            import wave
            with wave.open(SILENCE_FILE, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 8000 * 60)
    except Exception as e:
        logging.warning(f"[SILENCE] {e}")
    return SILENCE_FILE

def _silence_stream():
    from pytgcalls.types.raw import Stream, AudioStream, AudioParameters
    from ntgcalls import MediaSource
    return Stream(
        microphone=AudioStream(
            MediaSource.FILE, _ensure_silence(),
            AudioParameters(bitrate=48000, channels=1),
        )
    )

def _tg_filter_stream_end():
    from pytgcalls import filters as tg_filters
    return tg_filters.stream_end()

def _register_stream_loop(engine: PyTgCalls, phone: str):
    @engine.on_update(_tg_filter_stream_end())
    async def _on_stream_end(c, update):
        try:
            await asyncio.sleep(2)
            await c.play(update.chat_id, _silence_stream(),
                         config=GroupCallConfig(auto_start=False))
            logging.info(f"[{phone}] 🔄 silent stream restarted")
        except Exception as e:
            logging.warning(f"[{phone}] stream replay: {e}")

async def _get_engine(uid: int, phone: str, client: TelegramClient) -> PyTgCalls:
    key = (uid, phone)
    eng = call_engines.get(key)
    if eng is None:
        eng = PyTgCalls(client, workers=2)
        call_engines[key] = eng
        _register_stream_loop(eng, phone)
    if not getattr(eng, "_is_running", False):
        await eng.start()
    return eng

async def _engine_join(uid: int, phone: str, engine: PyTgCalls,
                       chat_id: int, call, me, client: TelegramClient):
    try:
        join_as = InputPeerUser(user_id=me.id, access_hash=me.access_hash)
    except Exception:
        join_as = me
    try:
        await engine.play(chat_id, _silence_stream(),
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    except Exception as e:
        logging.warning(f"[{phone}] silent stream failed ({e})")
        await engine.play(chat_id, None,
                          config=GroupCallConfig(join_as=join_as, auto_start=False))
    try: await engine.mute(chat_id)
    except Exception: pass
    try:
        await client(EditGroupCallParticipantRequest(
            call=call, participant=InputPeerSelf(), muted=True))
    except Exception: pass

_last_mute_at: dict = {}

def _make_remute_handler(client: TelegramClient, call, me_id: int, phone: str = None):
    async def handler(ev):
        for p in ev.participants:
            uid_p = getattr(p.peer, "user_id", None)
            if uid_p != me_id:
                continue
            if getattr(p, "left", False):
                continue
            if not p.muted:
                now = time.monotonic()
                if now - _last_mute_at.get(me_id, 0) < 60:
                    continue
                _last_mute_at[me_id] = now
                try:
                    await client(EditGroupCallParticipantRequest(
                        call=call, participant=InputPeerSelf(), muted=True,
                    ))
                except Exception:
                    pass
    return handler

async def _watchdog_loop(uid: int, phone: str, state: dict):
    checks = 0
    fails = 0
    while True:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            try:
                calls = await state["engine"].calls
                in_call = state["chat_id"] in calls
            except Exception:
                in_call = True
            if in_call:
                checks += 1
                continue
            try:
                await state["engine"].leave_call(state["chat_id"])
            except Exception:
                pass
            await asyncio.sleep(1)
            await _engine_join(uid, phone, state["engine"], state["chat_id"],
                               state["call"], state["me"], state["client"])
            fails = 0
        except asyncio.CancelledError:
            raise
        except NoActiveGroupCall:
            break
        except Exception:
            fails += 1
            try:
                await asyncio.sleep(min(KEEPALIVE_INTERVAL * (2 ** fails), 60))
            except asyncio.CancelledError:
                raise

def _start_watchdog(uid: int, phone: str, state: dict):
    _stop_watchdog(uid, phone)
    watchdog_tasks[(uid, phone)] = asyncio.create_task(_watchdog_loop(uid, phone, state))

def _stop_watchdog(uid: int, phone: str):
    t = watchdog_tasks.pop((uid, phone), None)
    if t and not t.done():
        t.cancel()

# ═══════════════════════════════════════════════════════════════════════════════
# LEAVE CALLBACK
# ═══════════════════════════════════════════════════════════════════════════════
async def leave_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        return await q.answer("Unauthorized", show_alert=True)
    session_id = q.data.split(":", 1)[1]
    session = live_sessions.get(session_id)
    if not session or session["uid"] != q.from_user.id:
        return await q.answer("Session not found", show_alert=True)

    await q.edit_message_text("⏳ <i>Leaving VC...</i>", parse_mode="HTML")
    uid = session["uid"]
    accs = usable_accounts(uid)
    left = 0

    for phone in session["phones"]:
        _stop_watchdog(uid, phone)
        st = live_state.pop((uid, phone), {})
        engine = st.get("engine")
        vchat = st.get("chat_id")
        call = session["calls"].get(phone)
        info = accs.get(phone)
        if not info:
            continue
        try:
            client = await get_client(uid, phone, info["session"])
            for h in live_handlers.pop((uid, phone), []):
                try: client.remove_event_handler(h)
                except Exception: pass
            if engine is not None and vchat is not None:
                try: await engine.leave_call(vchat)
                except NotInCallError: pass
            elif call:
                await client(LeaveGroupCallRequest(call=call, source=0))
            left += 1
        except Exception as e:
            logging.error(f"[LEAVE] {phone}: {e}")

    live_sessions.pop(session_id, None)
    await q.edit_message_text(
        f"✅ <b>{left}</b> accounts left the VC.",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
async def _no_access(update: Update, uid: int):
    user_flow.pop(uid, None)
    await update.message.reply_text("⛔ আপনার এই access নেই।", reply_markup=get_kb(uid))

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    uid = update.effective_user.id
    text = update.message.text or ""

    if is_keyboard_button(text):
        if "𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧" in text:
            user_flow[uid] = {"state": S_WAIT_PHONE, "data": {}}
            return await start_add_account(update, ctx)
        if "𝗔𝗟𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧" in text:
            user_flow.pop(uid, None)
            return await show_accounts(update, ctx, uid, 0)
        if "𝗝𝗢𝗜𝗡 𝗟𝗜𝗩𝗘" in text:
            user_flow[uid] = {"state": S_WAIT_LIVE_LINK, "data": {}}
            return await ask_live_link(update, ctx)
        if "𝗝𝗢𝗜𝗡 𝗖𝗛/𝗚𝗥𝗣" in text:
            user_flow[uid] = {"state": S_WAIT_GRP_LINK, "data": {}}
            return await ask_grp_link(update, ctx)
        if "𝗔𝗗𝗗 𝗔𝗗𝗠𝗜𝗡" in text:
            if not can_manage_admins(uid):
                return await _no_access(update, uid)
            user_flow[uid] = {"state": S_WAIT_ADD_ADMIN, "data": {}}
            return await ask_add_admin(update, ctx)
        if "𝗔𝗟𝗟 𝗔𝗗𝗠𝗜𝗡" in text:
            if not can_manage_admins(uid):
                return await _no_access(update, uid)
            user_flow.pop(uid, None)
            return await show_admins(update, ctx, uid, 0)
        if "𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧" in text:
            if not can_import_export(uid):
                return await _no_access(update, uid)
            user_flow[uid] = {"state": S_WAIT_IMPORT, "data": {}}
            return await ask_import(update, ctx)
        if "𝗕𝗢𝗧 𝗘𝗫𝗣𝗢𝗥𝗧" in text:
            if not can_import_export(uid):
                return await _no_access(update, uid)
            user_flow.pop(uid, None)
            return await do_export(update, ctx, uid)

    state = user_flow.get(uid, {}).get("state")
    if state == S_WAIT_PHONE:     return await handle_phone(update, ctx, uid)
    if state == S_WAIT_CODE:      return await handle_code(update, ctx, uid)
    if state == S_WAIT_2FA:       return await handle_2fa(update, ctx, uid)
    if state == S_WAIT_LIVE_LINK: return await handle_live_link(update, ctx, uid)
    if state == S_WAIT_LIVE_CNT:  return await handle_live_count(update, ctx, uid)
    if state == S_WAIT_GRP_LINK:  return await handle_grp_link(update, ctx, uid)
    if state == S_WAIT_RM_ACC:    return await handle_rm_acc(update, ctx, uid)
    if state == S_WAIT_ADD_ADMIN: return await handle_add_admin(update, ctx, uid)
    if state == S_WAIT_RM_ADMIN:  return await handle_rm_admin(update, ctx, uid)
    if state == S_WAIT_ADMIN_ACCS: return await handle_admin_acc_count(update, ctx, uid)

    await update.message.reply_text("👋 Please use the buttons 👇", reply_markup=get_kb(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        return await q.answer("Unauthorized", show_alert=True)
    data = q.data
    uid = q.from_user.id

    if data in ("acc:noop", "adm:noop"):
        return await q.answer()

    if data.startswith("acc:"):
        await q.answer()
        page = int(data.split(":")[1])
        return await show_accounts(update, ctx, uid, page)

    if data.startswith("acc_rm:"):
        await q.answer()
        page = int(data.split(":")[1])
        user_flow[uid] = {"state": S_WAIT_RM_ACC, "data": {"page": page}}
        return await q.message.reply_text(
            "🗑️ <b>কোন নাম্বারটি remove করবেন?</b>\n(list number দিন)",
            parse_mode="HTML"
        )

    if data.startswith("adm:"):
        await q.answer()
        if not can_manage_admins(uid): return
        page = int(data.split(":")[1])
        return await show_admins(update, ctx, uid, page)

    if data.startswith("adm_rm:"):
        await q.answer()
        if not can_manage_admins(uid): return
        page = int(data.split(":")[1])
        user_flow[uid] = {"state": S_WAIT_RM_ADMIN, "data": {"page": page}}
        return await q.message.reply_text(
            "🗑️ <b>কোন ID remove করবেন?</b>\n(list number দিন)",
            parse_mode="HTML"
        )

    if data.startswith("nadm:"):
        return await admin_draft_cb(update, ctx)

    if data.startswith("leave:"):
        return await leave_cb(update, ctx)

    await q.answer()

# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not can_manage_admins(uid):
        return await update.message.reply_text("⛔ আপনার এই access নেই।")
    if not ctx.args:
        return await update.message.reply_text("Usage: /add <user_id>")
    try:
        target = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Invalid ID")
    await _begin_add_admin(update, uid, target)

async def cmd_rm_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not can_manage_admins(uid):
        return await update.message.reply_text("⛔ আপনার এই access নেই।")
    mine = managed_admins(uid)
    if not ctx.args:
        cur = "\n".join(f"• `{a}`" for a in mine) or "(none)"
        return await update.message.reply_text(
            f"Usage: /rm <user_id>\n\nAdmins:\n{cur}", parse_mode="Markdown")
    try:
        target = int(ctx.args[0])
    except ValueError:
        return await update.message.reply_text("❌ Invalid ID")
    if target not in mine:
        return await update.message.reply_text("⚠️ Not admin")
    returned = await remove_admin(target, uid)
    extra = f"\n↩️ Accounts returned: {returned}" if returned else ""
    await update.message.reply_text(f"🗑️ Removed: `{target}`{extra}", parse_mode="Markdown")

async def cmd_list_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not can_manage_admins(uid):
        return
    cur = "\n".join(f"• `{a}`" for a in managed_admins(uid)) or "(none)"
    head = f"👑 Owner: `{OWNER_ID}`\n\n" if is_owner(uid) else ""
    await update.message.reply_text(f"{head}*Admins:*\n{cur}", parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH SERVER
# ═══════════════════════════════════════════════════════════════════════════════
async def health_handler(request):
    up = int(time.time() - START_TIME)
    return web.Response(
        text=f"✅ Bot is alive! uptime={up // 3600}h {(up % 3600) // 60}m {up % 60}s",
        status=200,
    )

async def ping_handler(request):
    return web.Response(text="pong", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ping", ping_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"✅ Health server on port {PORT}")

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error", exc_info=ctx.error)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    global _owner_username_cache

    missing = [n for n, v in (("BOT_TOKEN", BOT_TOKEN), ("API_ID", API_ID),
                              ("API_HASH", API_HASH)) if not v]
    # Render port scan পাশ করার জন্য health server আগে চালু
    await start_health_server()
    if missing:
        logging.error(f"❌ Missing env vars: {', '.join(missing)} — "
                      f"Render Dashboard → Environment এ সেট করুন।")
        await asyncio.Event().wait()   # service live থাকবে, লগ দেখে ঠিক করতে পারবেন
        return

    app = Application.builder().bot(PremiumBot(token=BOT_TOKEN)).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("add", cmd_add_admin))
    app.add_handler(CommandHandler("rm", cmd_rm_admin))
    app.add_handler(CommandHandler("admins", cmd_list_admins))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_import_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_error_handler(on_error)

    print("🤖 Bot starting...")
    async with app:
        await app.initialize()
        await app.bot.delete_webhook(drop_pending_updates=True)

        # ✅ Owner username auto-detect
        try:
            chat = await app.bot.get_chat(OWNER_ID)
            if chat.username:
                _owner_username_cache = chat.username
                logging.info(f"✅ Owner username detected: @{chat.username}")
            else:
                logging.warning(
                    "⚠️ Owner-এর কোনো username নেই — ACCESS NEED button "
                    "pre-filled message ছাড়াই profile খুলবে।"
                )
        except Exception as e:
            logging.warning(f"⚠️ Owner fetch failed: {e}")

        logging.info(f"✨ Premium emoji mapped: {len(EMOJI_MAP)}")
        await asyncio.sleep(1)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ Bot is running!")
        try:
            await asyncio.Event().wait()
        finally:
            for key in list(watchdog_tasks):
                _stop_watchdog(*key)
            live_state.clear()

if __name__ == "__main__":
    asyncio.run(main())