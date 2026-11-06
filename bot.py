import os
import random
import io
import copy
import json
import base64
import atexit
import threading
import time
import html
import re
import hmac
import hashlib
import asyncio
import logging
import urllib.parse
from datetime import datetime
from urllib.parse import parse_qsl

from aiohttp import web

from telegram import (
    Update, InlineKeyboardMarkup, ReplyKeyboardMarkup, WebAppInfo, MenuButtonWebApp,
    InlineKeyboardButton as _TgInlineButton, KeyboardButton as _TgKeyButton
)
from telegram.error import TelegramError, RetryAfter
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
    UserAlreadyParticipantError, UserNotParticipantError
)
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest, GetParticipantRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest, ImportChatInviteRequest, GetFullChatRequest
)
from telethon.tl.functions.phone import (
    LeaveGroupCallRequest, EditGroupCallParticipantRequest
)
from telethon.tl.types import (
    InputPeerSelf, UpdateGroupCallParticipants, InputPeerUser,
    Channel, Chat as TelegramChat, ChatInviteAlready, ReactionEmoji
)
from telethon.tl.functions.messages import GetMessagesViewsRequest, SendReactionRequest

from pytgcalls import PyTgCalls
from pytgcalls.types import GroupCallConfig
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
# httpx প্রতিটা request এর পুরো URL log করে — তাতে BOT_TOKEN দেখা যায়। তাই ওটা চুপ রাখা হলো।
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

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

# ── Database (restart/deploy এ data যাতে না হারায়) ──────────────────────────────
MONGODB_URI    = os.environ.get("MONGODB_URI", "").strip()     # MongoDB Atlas connection string
MONGODB_DB     = os.environ.get("MONGODB_DB", "vcjoiner").strip() or "vcjoiner"
DATA_KEY       = os.environ.get("DATA_KEY", "").strip()        # session string encrypt করার গোপন key
START_TIME     = time.time()
PORT           = int(os.environ.get("PORT") or 10000)
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL") or 4)
PER_PAGE       = 15

# Mini App (ALL ACCOUNT / ALL ADMIN panel).  Render এ RENDER_EXTERNAL_URL auto থাকে; না থাকলে WEBAPP_URL দিন।
WEBAPP_URL     = (os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBAPP_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
WEBAPP_AUTH_MAX_AGE = 24 * 3600     # initData এর বয়স এর বেশি হলে panel বন্ধ (আবার bot থেকে খুলতে হবে)
BOT_REF = None                      # main() এ set হয় (admin এর নাম আনার জন্য)

# Reply-keyboard Mini App এ Telegram initData দেয় না — তাই bot প্রতিটা user এর keyboard এ তার নামে signed token বসায়।
# token = uid + মেয়াদ + HMAC(bot token থেকে বানানো গোপন key). বদলানো/বানানো যায় না, মেয়াদ শেষ হলে বন্ধ,
# আর প্রতি request এ admin কিনা আবার দেখা হয় (admin remove হলে সাথে সাথে access বন্ধ)।
WEBAPP_TOKEN_TTL = 3 * 24 * 3600
_TOK_KEY = hashlib.sha256(b"tok:" + BOT_TOKEN.encode()).digest()

def make_token(uid: int) -> str:
    exp = int(time.time()) + WEBAPP_TOKEN_TTL
    sig = hmac.new(_TOK_KEY, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    return f"{uid}.{exp}.{sig}"

def verify_token(tok: str):
    """-> (uid, None) valid হলে; নাহলে (None, 'expired' | 'unauthorized')"""
    try:
        uid_s, exp_s, sig = str(tok).split(".")
        uid, exp = int(uid_s), int(exp_s)
    except (ValueError, TypeError):
        return None, "unauthorized"
    calc = hmac.new(_TOK_KEY, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(calc, sig):
        return None, "unauthorized"
    if exp < time.time():
        return None, "expired"
    return uid, None

def webapp_url(tab: str, uid: int = None):
    """uid দিলে (keyboard button) URL এ signed token থাকে; না দিলে (inline button) initData দিয়ে login হয়"""
    if not WEBAPP_URL.startswith("https://"):
        return None
    u = f"{WEBAPP_URL}/app?tab={tab}&v=2"
    return u + f"&t={make_token(uid)}" if uid else u

# ✅ Owner username auto-detect হবে startup-এ — env var দরকার নেই
_owner_username_cache: str | None = None

# ═══════════════════════════════════════════════════════════════════════════════
# PREMIUM EMOJI  (Bot API 9.4+ — bot যে account দিয়ে বানানো, তাতে Telegram Premium থাকতে হবে)
# নিচের প্রতিটা line = বটে ব্যবহার হওয়া একটা emoji (বেশি emoji বাদ দিয়ে শুধু দরকারি গুলো রাখা হয়েছে)।
# "GET_PREMIUM_ID" এর জায়গায় সেই emoji এর premium ID (শুধু সংখ্যা) বসান।
# "GET_PREMIUM_ID" (বা সংখ্যা ছাড়া কিছু) থাকলে ওই emoji সাধারণই থাকবে, error হবে না।
# নতুন emoji ব্যবহার করলে এখানে আরেকটা line যোগ করুন:  "😀": "123456789",
# ═══════════════════════════════════════════════════════════════════════════════
PREMIUM_EMOJI_IDS = {
    "✅": "6267008582294705964",  # success / done title (login, code sent, join, import, admin added, left VC)
    "❌": "6294194896349699910",  # error / cancel / failed title
    "⚠️": "6267039884016358504",  # warning (already exists, invalid input, flood wait, expired)
    "⛔": "5188187560035234546",  # no access / blocked account
    "🚫": "6339344004882112151",  # Cancelled + You Not Have Any Access
    "🟢": "5215685881989442149",  # LIVE status dot + admin ON flag + keyboard JOIN CH/GRP button
    "🔴": "5398038979217989221",  # OFF status dot + admin OFF flag
    "🥰": "6269181779911908635",  # /start welcome header
    "👑": "6266995104687330978",  # Add admin summary (User Id) + cancel message
    "🤖": "5172522439917175584",  # Join status title (Joining Accounts / Live VC / Group)
    "📱": "5407025283456835913",  # ADD ACCOUNT prompt title
    "🔐": "5291873529464122510",  # 2FA Password prompt title
    "🆔": "5014902839575577394",  # Admin User ID prompt title
    "🎥": "5389039613983602470",  # Join Live link prompt title
    "📢": "6068631133883471225",  # Channel / Group link prompt title
    "📦": "5472335930549347896",  # Premium admin — Your Have Accounts title
    "🗄": "6321207771035338339",  # ALL ACCOUNT / ALL ADMIN keyboard button + ALL ACCOUNT HISTORY title
    "🪖": "5361684864196157805",  # ALL ADMIN HISTORY title + ADMIN SETTING ACCESS button
    "♻️": "5377584064326804458",  # BOT IMPORT & EXPORT button (admin setting)
    "📥": "6053338928595870015",  # BOT IMPORT button + import messages
    "📤": "5445355530111437729",  # BOT EXPORT button + export caption
    "➕": "5397916757333654639",  # ADD ACCOUNT / ADD ADMIN keyboard button
    "👨‍👩‍👦": "5836747361400918804",  # JOIN LIVE keyboard button
    "🔄": "5253464392850221514",  # REFRESH button
    "🗑️": "5445267414562389170",  # REMOVE button + Account/Admin Removed title
    "💣": "5375445874988036618",  # LEAVE TO VC button + Leaving VC title
    "🚨": "6267144651153609853",  # ACCESS NEED button (no access screen)
    "📭": "5352896944496728039",  # Empty list (no account / no admin / no session)
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
        try:
            return await super().send_message(chat_id, text, *args, **kwargs)
        except TelegramError:
            ak = kwargs.get("api_kwargs") or {}
            if "message_effect_id" not in ak:
                raise
            # effect support না থাকলে (group/পুরনো client) effect ছাড়াই পাঠাই
            kwargs["api_kwargs"] = {k: v for k, v in ak.items() if k != "message_effect_id"} or None
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
#   • MONGODB_URI থাকলে data MongoDB তে থাকে (restart এ হারায় না), session সহ সব DATA_KEY দিয়ে encrypt হয়ে।
#   • না থাকলে আগের মতো accounts.json / admins.json (Render restart এ মুছে যেতে পারে)।
#   • data memory তে cache থাকে; প্রতিটা save এ local file + background thread এ MongoDB update হয়।
#   • DB থেকে data পড়তে না পারলে (বা key ভুল হলে) DB তে কিছুই লেখা হয় না — আগের data নিরাপদ থাকে।
# ═══════════════════════════════════════════════════════════════════════════════
try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:                                            # pragma: no cover
    Fernet = InvalidToken = None

_fernet = None
if DATA_KEY and Fernet:
    _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(("vcj:" + DATA_KEY).encode()).digest()))

_st_lock = threading.RLock()
_st_ready = False
_acc_cache: dict = {}
_admins_raw = None                       # DB/file থেকে পড়া কাঁচা admin data (load_admins parse করে)
_db_col = None                           # MongoDB collection (None = DB বন্ধ/degraded)
_db_state = {"mode": "file", "error": "", "pushed": {}, "last_push": 0, "writes": 0}
_dirty = threading.Event()

def _db_pack(obj) -> dict:
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    if _fernet:
        return {"enc": True, "blob": _fernet.encrypt(raw.encode()).decode()}
    return {"enc": False, "json": raw}

def _db_unpack(doc: dict):
    if doc.get("enc"):
        if not _fernet:
            raise RuntimeError("DB data encrypted কিন্তু DATA_KEY নেই")
        try:
            return json.loads(_fernet.decrypt(doc["blob"].encode()).decode())
        except InvalidToken:
            raise RuntimeError("DATA_KEY ভুল — DB data decrypt হচ্ছে না")
    return json.loads(doc.get("json") or "null")

def _read_local(path):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None

def _normalize_accounts(data) -> dict:
    if not isinstance(data, dict):
        return {}
    migrated = {}
    for k, v in data.items():
        if isinstance(v, dict) and "session" in v:          # পুরনো single-tenant format
            migrated.setdefault(str(OWNER_ID), {})[k] = v
        elif isinstance(v, dict):
            migrated[str(k)] = v
    return migrated

def _db_snapshot():
    with _st_lock:
        acc = copy.deepcopy(_acc_cache)
        if "admins" in globals():
            adm = {str(k): copy.deepcopy(v) for k, v in list(admins.items())}   # list() = main thread এর সাথে সংঘর্ষ নেই
        else:
            adm = _admins_raw if isinstance(_admins_raw, dict) else {}
    return acc, adm

def _db_push() -> int:
    """বদলানো document গুলো MongoDB তে লেখে (বদলায়নি এমনগুলো skip). return = লেখা document সংখ্যা"""
    if _db_col is None:
        return 0
    acc, adm = _db_snapshot()
    pushed, n = _db_state["pushed"], 0
    wanted = {f"acc:{u}": a for u, a in acc.items()}
    wanted["admins"] = adm
    for key, obj in wanted.items():
        h = hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if pushed.get(key) != h:
            _db_col.replace_one({"_id": key}, {"_id": key, **_db_pack(obj), "ts": int(time.time())}, upsert=True)
            pushed[key] = h
            n += 1
    for key in [k for k in pushed if k not in wanted]:        # মুছে ফেলা user
        _db_col.delete_one({"_id": key})
        pushed.pop(key, None)
        n += 1
    if n:
        _db_state["writes"] += n
        _db_state["last_push"] = int(time.time())
    return n

def _db_writer():
    while True:
        _dirty.wait()
        time.sleep(0.3)                                       # পরপর save একসাথে করে
        _dirty.clear()
        try:
            _db_push()
            _db_state["error"] = ""
        except Exception as e:
            _db_state["error"] = f"push: {e}"
            logging.error(f"[DB] লিখতে পারিনি, ৫ সেকেন্ড পরে আবার: {e}")
            time.sleep(5)
            _dirty.set()

def _db_flush():
    """বন্ধ হওয়ার আগে বাকি data MongoDB তে লিখে ফেলে"""
    if _db_col is not None:
        try:
            _db_push()
        except Exception as e:
            logging.error(f"[DB] flush error: {e}")

def _db_mark_dirty():
    if _db_col is not None:
        _dirty.set()

def _storage_init():
    global _st_ready, _acc_cache, _admins_raw, _db_col
    with _st_lock:
        if _st_ready:
            return
        local_acc = _normalize_accounts(_read_local(ACCOUNTS_FILE))
        local_adm = _read_local(ADMIN_FILE)
        _acc_cache, _admins_raw = local_acc, local_adm
        if not MONGODB_URI:
            _db_state["mode"] = "file"
            logging.warning("⚠️ MONGODB_URI নেই — data শুধু ফাইলে (Render restart এ হারাতে পারে)")
            _st_ready = True
            return
        last_err = ""
        for attempt in range(2):
            try:
                import pymongo
                client = pymongo.MongoClient(MONGODB_URI, serverSelectionTimeoutMS=6000, connectTimeoutMS=6000)
                col = client[MONGODB_DB]["store"]
                docs = list(col.find({}))
                acc, adm = {}, None
                for d in docs:
                    _id = str(d.get("_id", ""))
                    if _id.startswith("acc:"):
                        acc[_id[4:]] = _db_unpack(d) or {}
                    elif _id == "admins":
                        adm = _db_unpack(d)
                if docs:                                       # DB ই আসল (source of truth)
                    _acc_cache, _admins_raw = _normalize_accounts(acc), adm
                    logging.info(f"✅ MongoDB থেকে data লোড: {len(acc)} user")
                else:                                          # প্রথমবার: local data DB তে তুলে দেওয়া
                    logging.info(f"🔄 MongoDB খালি — local data ({len(local_acc)} user) migrate হচ্ছে")
                _db_col = col
                _db_state["pushed"] = {}                       # DB তে যা আছে তার hash (আবার লেখার দরকার নেই)
                if docs:
                    for k, o in [(f"acc:{u}", a) for u, a in _acc_cache.items()] + [("admins", None)]:
                        if o is not None:
                            _db_state["pushed"][k] = hashlib.sha1(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                _db_state["mode"] = "mongodb"
                _db_state["error"] = ""
                threading.Thread(target=_db_writer, daemon=True, name="db-writer").start()
                atexit.register(_db_flush)
                if not docs:
                    _dirty.set()                               # migrate: local data DB তে লেখা শুরু
                break
            except Exception as e:
                last_err = str(e)
                logging.error(f"[DB] সংযোগ/পড়া ব্যর্থ ({attempt + 1}/2): {str(e)[:300]}")
                _db_col = None
                time.sleep(1)
        if _db_col is None:
            _db_state["mode"] = "degraded"
            _db_state["error"] = last_err[:300]
            if "SSL handshake" in last_err or "TLSV1_ALERT" in last_err or "timed out" in last_err.lower():
                _db_state["error"] = ("Atlas এ ঢুকতে পারছে না — Atlas > Network Access এ 0.0.0.0/0 (Allow from anywhere) "
                                      "দিন, cluster Active কিনা দেখুন, তারপর bot restart করুন")
                logging.critical("🚨 Atlas Network Access এ 0.0.0.0/0 নেই বলে মনে হচ্ছে (SSL handshake error)")
            logging.critical("🚨 MongoDB থেকে data পড়া যায়নি — DB তে কিছু লেখা হবে না (আগের data নিরাপদ). "
                             "MONGODB_URI / DATA_KEY / Atlas Network Access দেখে bot restart করুন")
        _st_ready = True

def load_accounts():
    _storage_init()
    with _st_lock:
        return copy.deepcopy(_acc_cache)

def _atomic_write_json(path: str, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

def save_accounts(data):
    global _acc_cache
    _storage_init()
    with _st_lock:
        _acc_cache = copy.deepcopy(data)
    try:
        _atomic_write_json(ACCOUNTS_FILE, data)
    except Exception as e:
        logging.warning(f"[FILE] accounts.json লেখা যায়নি: {e}")
    _db_mark_dirty()

def user_accounts(uid: int) -> dict:
    return load_accounts().get(str(uid), {})

def set_user_accounts(uid: int, accs: dict):
    all_data = load_accounts()
    all_data[str(uid)] = accs
    save_accounts(all_data)

def load_admins() -> dict:
    """admins.json -> {id: {settings, ie, type, added_by, added_at}}
    (পুরনো list format হলে auto-migrate হয়: settings=OFF, import/export=ON, NORMAL)"""
    _storage_init()
    raw = _admins_raw
    if raw is None:
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
    _storage_init()
    try:
        _atomic_write_json(ADMIN_FILE, {str(k): v for k, v in admins.items()})
    except Exception as e:
        logging.warning(f"[FILE] admins.json লেখা যায়নি: {e}")
    _db_mark_dirty()

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
LINE = "━━━━━━━━━━━━━━━━━━━━"
SEP = "┃"

# Telegram message effects (শুধু private chat এ; কাজ না করলে auto effect ছাড়া যায়)
FX_PARTY = {"message_effect_id": "5046509860389126442"}   # 🎉
FX_FIRE  = {"message_effect_id": "5104841245755180586"}   # 🔥
FX_LIKE  = {"message_effect_id": "5107584321108051014"}   # 👍
FX_HEART = {"message_effect_id": "5044134455711629726"}   # ❤️

def mb(s: str) -> str:
    """ASCII -> 𝗠𝗮𝘁𝗵 𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱 (বটের label style)"""
    out = []
    for ch in s:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D5EE + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)

def pbar(done: int, total: int, width: int = 10, tick=None) -> str:
    """▰▰▰▱▱▱ 30%  —  tick দিলে progress এর মাথাটা blink করে (animation)"""
    total = max(int(total), 1)
    done = max(0, min(int(done), total))
    f = int(round(width * done / total))
    cells = ["▰"] * f + ["▱"] * (width - f)
    if tick is not None and f < width:
        cells[f] = "▰" if tick % 2 else "▱"
    return "".join(cells) + f" {int(100 * done / total)}%"

# ── Animation (emoji নয়, তাই premium ID লাগে না) ───────────────────────────
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"            # braille spinner
DOTS = ["", ".", "..", "..."]

def spin(tick) -> str:
    return SPIN[int(tick) % len(SPIN)]

def sweep(tick: int, width: int = 12, size: int = 4) -> str:
    """▱▱▰▰▰▰▱▱▱▱▱▱  —  ডান-বাম bounce করা loading bar"""
    span = max(width - size, 1)
    period = span * 2
    p = int(tick) % period
    pos = p if p <= span else period - p
    return "▱" * pos + "▰" * size + "▱" * (width - size - pos)

def loading_frame(title: str, tick: int, label: str = "Loading") -> str:
    return (f"{title}\n{LINE}\n"
            f"{spin(tick)} <b>{mb(label)}{DOTS[tick % len(DOTS)]}</b>\n"
            f"{sweep(tick)}")

async def animate(edit, render, work, interval: float = 0.9, max_frames: int = 90, final: bool = False):
    """work (coroutine) চলার সময় render(tick) দিয়ে message বারবার edit করে।
    edit(text) = async callable. Flood-safe: একই text হলে edit হয় না, RetryAfter এ থামে।
    final=True হলে work শেষে render(None) দিয়ে শেষ frame (100%) দেখায় — তারপর একটু থেমে return করে,
    যাতে পরের message 100% দেখানোর আগে চলে না আসে।"""
    task = asyncio.ensure_future(work)
    tick, last = 0, None
    try:
        while not task.done() and tick < max_frames:
            text = render(tick)
            if text != last:
                try:
                    await edit(text)
                    last = text
                except RetryAfter as e:
                    await asyncio.sleep(min(float(e.retry_after), 5))
                except Exception:
                    pass
            tick += 1
            try:
                await asyncio.wait_for(asyncio.shield(task), interval)
            except asyncio.TimeoutError:
                pass
        result = await task
        if final:
            text = render(None)
            for _ in range(2):
                if text == last:
                    break
                try:
                    await edit(text)
                    last = text
                    break
                except RetryAfter as e:
                    await asyncio.sleep(min(float(e.retry_after), 5))
                except Exception:
                    break
            await asyncio.sleep(0.7)
        return result
    except asyncio.CancelledError:
        task.cancel()
        raise

async def run_with_loading(edit, title: str, coro, label: str = "Loading"):
    """coro চলার সময় message এ bouncing loading animation দেখায় (edit(text) async callable)"""
    return await animate(edit, lambda t: loading_frame(title, t, label), coro, interval=0.8)

async def _final_msg(update: Update, status_msg, text: str, kb=None, fx=FX_PARTY):
    """status message মুছে নতুন message (message effect সহ) পাঠায়"""
    try:
        await status_msg.delete()
    except Exception:
        pass
    return await update.effective_chat.send_message(
        text, parse_mode="HTML", reply_markup=kb, api_kwargs=dict(fx))

def err_card(title: str, hint: str = "") -> str:
    t = f"❌ <b>{title}</b>"
    if hint:
        t += f"\n{LINE}\n<i>{mb('Tip')} : {hint}</i>"
    return t

CANCEL_HINT = f"\n\n<i>{mb('Cancel')} : /cancel</i>"

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
    def panel(label, tab):
        # keyboard button চাপলেই সরাসরি panel খোলে। Telegram এতে initData দেয় না, তাই URL এ এই user এর signed token আছে।
        u = webapp_url(tab, uid)
        if u:
            return KeyboardButton(label, web_app=WebAppInfo(url=u), api_kwargs=_R)
        return KeyboardButton(label, api_kwargs=_R)

    rows = [
        [KeyboardButton("👨‍👩‍👦 𝗝𝗢𝗜𝗡 𝗟𝗜𝗩𝗘", api_kwargs=_R), KeyboardButton("🟢 𝗝𝗢𝗜𝗡 𝗖𝗛/𝗚𝗥𝗣", api_kwargs=_R)],
        [KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", api_kwargs=_R),  panel("🗄 𝗔𝗟𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧", "accounts")],
    ]
    if can_manage_admins(uid):
        rows.append([KeyboardButton("➕ 𝗔𝗗𝗗 𝗔𝗗𝗠𝗜𝗡", api_kwargs=_R), panel("🗄 𝗔𝗟𝗟 𝗔𝗗𝗠𝗜𝗡", "admins")])
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

def _format_join_status(sd: dict, tick=None, title: str = "Joining Accounts") -> str:
    total = sd.get("total", 0)
    pending = max(0, sd.get("pending", 0))
    busy = tick is not None and pending > 0
    head = f"{spin(tick)} " if busy else "✓ "
    return (
        f"🤖 <b>{mb(title)}</b>\n"
        f"{LINE}\n"
        f"{mb('Total Bot Account')} : {total}\n"
        f"{mb('Pending Join')} : {pending}\n"
        f"{mb('Already Join')} : {sd.get('already', 0)}\n"
        + (f"{mb('Already Member')} : {sd['skipped']}\n" if sd.get("skipped") else "") +
        f"{LINE}\n"
        f"{head}{pbar(total - pending, total, tick=tick if busy else None)}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# /start + ACCESS DENIED
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        uid = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text(
            f"🚫 <b>𝗬𝗼𝘂 𝗡𝗼𝘁 𝗛𝗮𝘃𝗲 𝗔𝗻𝘆 𝗔𝗰𝗰𝗲𝘀𝘀.</b>\n"
            f"{LINE}\n"
            f"<b>𝗬𝗼𝘂𝗿 𝗜𝗱 :</b> <code>{uid}</code>\n\n"
            f"<i>𝗬𝗼𝘂 𝗡𝗲𝗲𝗱 𝗔𝗰𝗰𝗲𝘀𝘀 𝗧𝗵𝗲𝗻 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿.</i>",
            parse_mode="HTML",
            reply_markup=access_denied_kb(uid)
        )
        return
    uid = update.effective_user.id
    user_flow.pop(uid, None)
    info = admins.get(uid, {})
    if is_owner(uid):
        head = "𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝗧𝗼 𝗩𝗰 𝗝𝗼𝗶𝗻 𝗕𝗼𝘁 𝗢𝘄𝗻𝗲𝗿 𝗕𝗮𝗯𝘂 🥰"
        role = mb('OWNER')
        foot = "𝗬𝗼𝘂 𝗖𝗮𝗻 𝗨𝘀𝗲 𝗔𝗻𝘆𝘁𝗵𝗶𝗻𝗴𝘀..."
    else:
        head = "𝗪𝗲𝗹𝗰𝗼𝗺𝗲 𝗧𝗼 𝗩𝗰 𝗝𝗼𝗶𝗻 𝗕𝗼𝘁 🥰"
        role = mb('PREMIUM ADMIN') if info.get("type") == "premium" else mb('NORMAL ADMIN')
        foot = "𝗡𝗼𝘄 𝗬𝗼𝘂 𝗖𝗮𝗻 𝗨𝘀𝗲 𝗧𝗵𝗶𝘀 𝗕𝗼𝘁...."
    own = user_accounts(uid)
    lent_out = sum(1 for i in own.values() if i.get("lent_to"))
    got = len(lent_in_accounts(uid))
    rows = [
        f"<b>{mb('ID')}</b> : <code>{uid}</code>",
        f"<b>{mb('Role')}</b> : {role}",
        f"<b>{mb('Accounts')}</b> : {len(own) - lent_out + got}",
    ]
    if lent_out:
        rows.append(f"<b>{mb('Lent')}</b> : {lent_out}")
    if got:
        rows.append(f"<b>{mb('Received')}</b> : {got}")
    if can_manage_admins(uid):
        rows.append(f"<b>{mb('Admins')}</b> : {len(managed_admins(uid))}")
    msg = f"<b>{head}</b>\n{LINE}\n" + "\n".join(rows) + f"\n{LINE}\n<b>{foot}</b>"
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_kb(uid),
                                    api_kwargs=dict(FX_HEART))
    if webapp_url("accounts"):               # input এর পাশের Menu button = এক tap এ panel
        try:
            await ctx.bot.set_chat_menu_button(
                chat_id=update.effective_chat.id,
                menu_button=MenuButtonWebApp(text="Panel", web_app=WebAppInfo(url=webapp_url(
                    "accounts"))))
        except Exception as e:
            logging.warning(f"[MENU] {e}")

async def cmd_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/db — শুধু owner: database এখন কেমন চলছে"""
    uid = update.effective_user.id if update.effective_user else 0
    if not is_owner(uid):
        return
    _storage_init()
    mode = {"mongodb": "MongoDB (connected)", "file": "File only (MONGODB_URI নেই)",
            "degraded": "DEGRADED (DB পড়া যায়নি, DB তে লেখা বন্ধ)"}[_db_state["mode"]]
    acc = load_accounts()
    n_acc = sum(len(v) for v in acc.values())
    last = datetime.fromtimestamp(_db_state["last_push"]).strftime("%d %b %H:%M:%S") if _db_state["last_push"] else "—"
    txt = (
        f"<b>{mb('Database')}</b>\n{LINE}\n"
        f"<b>{mb('Mode')}</b> : {html.escape(mode)}\n"
        f"<b>{mb('Encryption')}</b> : {'ON' if _fernet else 'OFF (DATA_KEY নেই)'}\n"
        f"<b>{mb('Users')}</b> : {len(acc)}\n"
        f"<b>{mb('Accounts')}</b> : {n_acc}\n"
        f"<b>{mb('Admins')}</b> : {len(admins)}\n"
        f"<b>{mb('Last Write')}</b> : {last}\n"
        f"<b>{mb('Writes')}</b> : {_db_state['writes']}"
    )
    if _db_state["error"]:
        txt += f"\n{LINE}\n<i>{html.escape(_db_state['error'])[:300]}</i>"
    await update.message.reply_text(txt, parse_mode="HTML")

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    user_flow.pop(uid, None)
    await update.message.reply_text(f"🚫 <b>{mb('Cancelled')}</b>", parse_mode="HTML", reply_markup=get_kb(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# ADD ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════
async def start_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    n = len(user_accounts(uid))
    await update.message.reply_text(
        f"📱 <b>{mb('Add Account')}</b>\n"
        f"{LINE}\n"
        f"<b>{mb('Your Account')}</b> : {n}\n\n"
        f"নতুন একাউন্টের নাম্বার দিন\n"
        f"<i>{mb('Example')} : +8801XXXXXXXXX</i>" + CANCEL_HINT,
        parse_mode="HTML"
    )

async def handle_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = update.message.text.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    accs = user_accounts(uid)
    if phone in accs:
        user_flow.pop(uid, None)
        await update.message.reply_text("⚠️ <b>এই নাম্বার ইতিমধ্যে আপনার লিস্টে আছে!</b>", parse_mode="HTML",
                                        reply_markup=get_kb(uid))
        return
    user_flow[uid]["data"]["phone"] = phone

    stage = {"i": 0}
    steps = ["Connecting To Telegram", "Sending Verification Code"]

    def render(t):
        return (f"<b>{mb(steps[stage['i']])}{DOTS[t % len(DOTS)]}</b>\n{LINE}\n"
                f"{spin(t)} {pbar(stage['i'] + 1, len(steps) + 1, width=8, tick=t)}")

    status_msg = await update.message.reply_text(render(0), parse_mode="HTML")

    async def edit(t):
        await status_msg.edit_text(t, parse_mode="HTML")

    async def work():
        c = TelegramClient(StringSession(), API_ID, API_HASH)
        await c.connect()
        pending_clients[(uid, phone)] = c
        stage["i"] = 1
        return await c.send_code_request(phone)

    try:
        r = await animate(edit, render, work(), interval=0.8)
        user_flow[uid]["data"]["code_hash"] = r.phone_code_hash
        user_flow[uid]["state"] = S_WAIT_CODE
        await status_msg.edit_text(
            f"✅ <b>{mb('Code Sent Successfully')}</b>\n"
            f"{LINE}\n"
            f"<b>{mb('Number')}</b> : <code>{html.escape(phone)}</code>\n\n"
            f"ভেরিফিকেশন কোড দিন\n"
            f"{pbar(1, 1, width=8)}" + CANCEL_HINT,
            parse_mode="HTML"
        )
    except FloodWaitError as e:
        user_flow.pop(uid, None)
        await status_msg.edit_text(f"⚠️ <b>{mb('Flood Wait')} :</b> {e.seconds}s", parse_mode="HTML")
    except Exception as e:
        user_flow.pop(uid, None)
        await status_msg.edit_text(f"❌ <b>{mb('Error')} :</b> {html.escape(str(e))}", parse_mode="HTML")

async def handle_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = user_flow[uid]["data"]["phone"]
    client = pending_clients.get((uid, phone))
    if not client:
        user_flow.pop(uid, None)
        return await update.message.reply_text(f"❌ <b>{mb('Session Expired')}</b>",
                                               parse_mode="HTML", reply_markup=get_kb(uid))
    code = update.message.text.strip().replace(" ", "")
    try:
        await client.sign_in(phone, code,
                             phone_code_hash=user_flow[uid]["data"]["code_hash"])
        await _save_account(update, uid, client, phone)
    except SessionPasswordNeededError:
        user_flow[uid]["state"] = S_WAIT_2FA
        await update.message.reply_text(
            f"🔐 <b>{mb('2FA Password')}</b>\n{LINE}\nআপনার 2FA Password দিন" + CANCEL_HINT, parse_mode="HTML")
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text(err_card("ভুল বা মেয়াদ শেষ কোড", f"আবার {mb('ADD ACCOUNT')} দিয়ে চেষ্টা করুন"),
                                        parse_mode="HTML", reply_markup=get_kb(uid))
    except Exception as e:
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text(f"❌ <b>{mb('Error')} :</b> {html.escape(str(e))}",
                                        parse_mode="HTML", reply_markup=get_kb(uid))

async def handle_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    phone = user_flow[uid]["data"]["phone"]
    client = pending_clients.get((uid, phone))
    if not client:
        user_flow.pop(uid, None)
        return await update.message.reply_text(f"❌ <b>{mb('Session Expired')}</b>",
                                               parse_mode="HTML", reply_markup=get_kb(uid))
    pwd = update.message.text.strip()
    try:
        await client.sign_in(password=pwd)
        await _save_account(update, uid, client, phone)
    except Exception as e:
        user_flow.pop(uid, None)
        pending_clients.pop((uid, phone), None)
        try: await client.disconnect()
        except Exception: pass
        await update.message.reply_text(err_card(mb("Wrong Password"), html.escape(str(e))),
                                        parse_mode="HTML", reply_markup=get_kb(uid))

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
        f"✅ <b>{mb('LOGIN SUCCESSFUL')}</b>\n"
        f"{LINE}\n"
        f"<b>{mb('Name')}</b> : {html.escape(name)}\n"
        f"<b>{mb('Number')}</b> : <code>{html.escape(phone)}</code>\n"
        f"<b>{mb('Status')}</b> : 🟢 {mb('Active')}\n"
        f"<b>{mb('Total')}</b> : {len(accs)}\n"
        f"{LINE}\n"
        f"<i>{mb('Account Added To Your List')}</i>",
        parse_mode="HTML",
        reply_markup=get_kb(uid),
        api_kwargs=dict(FX_PARTY),
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

def _nav_kb(prefix: str, rm_prefix: str, rm_label: str, page: int, pages: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("‹ 𝗣𝗥𝗩", callback_data=f"{prefix}:{page - 1}", api_kwargs=_G(True)),
            InlineKeyboardButton(f"{mb('Page')} {page + 1}/{pages}", callback_data=f"{prefix}:noop",
                                 api_kwargs={"style": "primary"}),
            InlineKeyboardButton("𝗡𝗫𝗧 ›", callback_data=f"{prefix}:{page + 1}", api_kwargs=_G(True)),
        ],
        [
            InlineKeyboardButton("🔄 𝗥𝗘𝗙𝗥𝗘𝗦𝗛", callback_data=f"{prefix}:{page}", api_kwargs={"style": "primary"}),
            InlineKeyboardButton(rm_label, callback_data=f"{rm_prefix}:{page}", api_kwargs=_R),
        ],
    ])

async def show_accounts(src, ctx: ContextTypes.DEFAULT_TYPE, uid: int, page: int = 0):
    rows = visible_accounts(uid)
    is_cb = isinstance(src, Update) and src.callback_query is not None
    title = f"🗄 <b>{mb('ALL ACCOUNT HISTORY')}</b>"

    if not rows:
        text = (f"{title}\n{LINE}\n📭 <b>কোনো অ্যাকাউন্ট নেই</b>\n\n"
                f"<i>{mb('ADD ACCOUNT')} চেপে নতুন account যোগ করুন</i>")
        if is_cb:
            await src.callback_query.edit_message_text(text, parse_mode="HTML")
        else:
            await src.message.reply_text(text, parse_mode="HTML", reply_markup=get_kb(uid))
        return

    total = len(rows)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * PER_PAGE
    page_items = rows[start:start + PER_PAGE]

    if is_cb:
        async def edit(t, kb=None):
            await src.callback_query.edit_message_text(t, parse_mode="HTML", reply_markup=kb)
    else:
        holder = await src.message.reply_text(loading_frame(title, 0), parse_mode="HTML")
        async def edit(t, kb=None):
            await holder.edit_text(t, parse_mode="HTML", reply_markup=kb)

    statuses = await run_with_loading(edit, title, asyncio.gather(*[
        _check_status_quick(info.get("session", "")) for _, info, _k in page_items
    ]))
    live = sum(1 for s in statuses if s == "🟢")

    lines = [
        title,
        LINE,
        f"<b>{mb('Total')}</b> {total} {SEP} 🟢 {live} {SEP} 🔴 {len(statuses) - live} {SEP} <b>{mb('Page')}</b> {page + 1}/{pages}",
        LINE,
        f"<b>{mb('NO')}</b> {SEP} <b>{mb('NUMBER')}</b> {SEP} <b>{mb('DATE')}</b> {SEP} <b>{mb('LIVE')}</b>",
        LINE,
    ]
    for i, ((ph, info, kind), st) in enumerate(zip(page_items, statuses)):
        n = start + i + 1
        try:
            added = datetime.fromtimestamp(int(info.get("added_at", 0))).strftime("%d %b")
        except Exception:
            added = "——"
        shown = mask_phone(ph) if kind == "lent_in" else ph     # পাওয়া account এর number পুরো দেখাবে না
        mark = {"lent_out": f" {mb('Lent')}", "lent_in": f" {mb('Got')}"}.get(kind, "")
        lines.append(f"<code>{n:02d}</code> {SEP} <code>{html.escape(shown)}</code> {SEP} "
                     f"<code>{added}</code> {SEP} {st}{mark}")
    lines.append(LINE)
    if any(k == "lent_out" for _, _, k in page_items):
        lines.append(f"<i>{mb('Lent')} : Admin কে দেওয়া — use/remove বন্ধ</i>")
    if any(k == "lent_in" for _, _, k in page_items):
        lines.append(f"<i>{mb('Got')} : আপনাকে দেওয়া — শুধু use করা যাবে</i>")

    kb = _nav_kb("acc", "acc_rm", "🗑️ 𝗥𝗘𝗠𝗢𝗩𝗘", page, pages)
    try:
        await edit("\n".join(lines), kb)
    except Exception as e:
        if "not modified" not in str(e).lower():
            raise

async def remove_account(uid: int, phone: str) -> bool:
    """account list থেকে মুছে session log out করে (bot + Mini App দুই জায়গা থেকেই ব্যবহার হয়)"""
    accs = user_accounts(uid)
    info = accs.pop(phone, None)
    if info is None:
        return False
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
    return True

async def handle_rm_acc(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    try:
        n = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("⚠️ একটি সংখ্যা দিন (list number)")
    rows = visible_accounts(uid)
    if not (1 <= n <= len(rows)):
        return await update.message.reply_text(f"❌ 1 থেকে {len(rows)} এর মধ্যে দিন")
    phone, _info, kind = rows[n - 1]
    if kind == "lent_out":
        return await update.message.reply_text(
            "⛔ এই account টি admin কে দেওয়া আছে।\n"
            "সেই admin remove না করা পর্যন্ত remove করা যাবে না।")
    if kind == "lent_in":
        return await update.message.reply_text(
            "⛔ এই account টি আপনাকে দেওয়া হয়েছে — আপনি remove করতে পারবেন না।")
    await remove_account(uid, phone)

    user_flow.pop(uid, None)
    await update.message.reply_text(
        f"🗑️ <b>{mb('Account Removed')}</b>\n{LINE}\n"
        f"<b>{mb('Number')}</b> : <code>{html.escape(phone)}</code>\n"
        f"<b>{mb('Remaining')}</b> : {len(user_accounts(uid))}",
        parse_mode="HTML", reply_markup=get_kb(uid)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# JOIN LIVE
# ═══════════════════════════════════════════════════════════════════════════════
async def ask_live_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🎥 <b>{mb('Send Live Link')}</b>\n"
        f"{LINE}\n"
        "Live link বা @username দিন\n\n"
        f"<i>{mb('Public')} : @channelname / t.me/channelname</i>\n"
        f"<i>{mb('Private')} : t.me/+xxxxxxxx</i>" + CANCEL_HINT,
        parse_mode="HTML"
    )

async def ask_grp_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📢 <b>{mb('Send Channel / Group Link')}</b>\n"
        f"{LINE}\n"
        "Channel / Group link বা @username দিন\n\n"
        f"<i>{mb('Public')} : @channelname / t.me/channelname</i>\n"
        f"<i>{mb('Private')} : t.me/+xxxxxxxx</i>" + CANCEL_HINT,
        parse_mode="HTML"
    )

async def handle_live_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    link = update.message.text.strip()
    accs = usable_accounts(uid)
    if not accs:
        user_flow.pop(uid, None)
        return await update.message.reply_text(
            err_card("কোনো ব্যবহারযোগ্য account নেই", f"আগে {mb('ADD ACCOUNT')} দিয়ে account যোগ করুন"),
            parse_mode="HTML", reply_markup=get_kb(uid))
    user_flow[uid]["data"]["link"] = link

    ct = f"🤖 <b>{mb('Checking Accounts')}</b>"
    msg = await update.message.reply_text(loading_frame(ct, 0, "Checking"), parse_mode="HTML")

    async def edit(t):
        await msg.edit_text(t, parse_mode="HTML")

    members, others = await run_with_loading(edit, ct, _split_members(uid, accs, link), label="Checking")
    newly = []
    if others:                                   # animation শুধু যাদের join নেই তাদের জন্য
        sd = {"total": len(others), "pending": len(others), "already": 0, "skipped": len(members)}
        jt = "Joining Group For VC"

        async def _work():
            for phone in others:
                try:
                    client = await get_client(uid, phone, accs[phone].get("session", ""))
                    entity, _new = await _ensure_joined(client, link)
                    if entity:
                        newly.append(phone)
                        sd["already"] += 1
                except Exception as e:
                    logging.warning(f"[JOIN-GRP] {phone}: {e}")
                sd["pending"] -= 1

        await edit(_format_join_status(sd, 0, jt))
        await animate(edit, lambda t: _format_join_status(sd, t, jt), _work(), interval=0.9, final=True)
        status_msg = msg
    else:                                        # সবাই আগে থেকেই member — কোনো join animation নেই
        status_msg = None
        try: await msg.delete()
        except Exception: pass
    joined_phones = [p for p in accs if p in members or p in newly]

    if not joined_phones:
        user_flow.pop(uid, None)
        return await status_msg.edit_text(
            err_card("কোনো account group এ join করতে পারিনি",
                     "Link চেক করুন অথবা group এ access আছে কিনা দেখুন"),
            parse_mode="HTML")

    user_flow[uid]["data"]["joined_phones"] = joined_phones

    if len(joined_phones) == 1:
        user_flow[uid]["data"]["count"] = 1
        vc_msg = await update.message.reply_text(
            f"{spin(0)} <i>{mb('Starting VC Join')}...</i>\n{sweep(0)}", parse_mode="HTML")
        return await _do_vc_join(update, ctx, uid, vc_msg)

    user_flow[uid]["state"] = S_WAIT_LIVE_CNT
    await update.message.reply_text(
        f"✅ <b>{len(joined_phones)}</b> account group এ join হয়েছে।\n"
        f"{LINE}\n"
        f"কতটি দিয়ে VC-তে Join করাবেন? <b>(1-{len(joined_phones)})</b>" + CANCEL_HINT,
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
        return await update.message.reply_text(f"⚠️ <b>1 থেকে {max_n} এর মধ্যে দিন</b>", parse_mode="HTML")
    user_flow[uid]["data"]["count"] = n
    user_flow[uid]["state"] = None
    vc_msg = await update.message.reply_text(
        f"{spin(0)} <i>{mb('Starting VC Join')}...</i>\n{sweep(0)}", parse_mode="HTML")
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

    vt = "Joining Live VC"

    async def _work():
        nonlocal vchat
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

    await animate(lambda t: status_msg.edit_text(t, parse_mode="HTML"),
                  lambda t: _format_join_status(sd, t, vt), _work(), interval=0.9, final=True)

    user_flow.pop(uid, None)

    if not session_phones:
        return await status_msg.edit_text(
            err_card("কোনো account VC-তে join করতে পারিনি", "Live চালু আছে কিনা চেক করুন"),
            parse_mode="HTML")

    live_sessions[session_id] = {
        "uid": uid,
        "phones": session_phones,
        "calls": session_calls,
        "engines": session_engines,
        "chat_id": vchat,
        "vr": False,
        "vr_handlers": {},
    }

    await _final_msg(
        update, status_msg,
        f"✅ <b>𝗬𝗼𝘂 𝗟𝗶𝘃𝗲 𝗝𝗼𝗶𝗻 𝗥𝗲𝗾 𝗦𝘂𝗰𝗰𝗲𝘀𝘀</b>\n"
        f"{LINE}\n"
        f"<b>𝗬𝗼𝘂𝗿 𝗧𝗮𝗿𝗴𝗲𝘁 𝗠𝗲𝗺𝗯𝗲𝗿𝘀 𝗟𝗶𝘃𝗲 𝗝𝗼𝗼𝗶𝗻....</b>\n\n"
        f"<b>{mb('Joined')}</b> : {len(session_phones)} / {count}\n"
        f"{pbar(len(session_phones), count)}",
        kb=_live_kb(session_id),
    )

def _live_kb(sid: str) -> InlineKeyboardMarkup:
    on = bool(live_sessions.get(sid, {}).get("vr"))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💣 𝗟𝗘𝗔𝗩𝗘 𝗧𝗢 𝗩𝗖", callback_data=f"leave:{sid}", api_kwargs=_R)],
        [InlineKeyboardButton("✋ 𝗛𝗔𝗡𝗗 𝗥𝗔𝗜𝗦𝗘", callback_data=f"hand:{sid}", api_kwargs={"style": "primary"})],
        [InlineKeyboardButton("𝗩𝗜𝗘𝗪 & 𝗥𝗘𝗔𝗖𝗧𝗜𝗢𝗡", callback_data=f"vr:{sid}", api_kwargs=_G(on))],
    ])

# ═══════════════════════════════════════════════════════════════════════════════
# HAND RAISE + VIEW & REACTION
# ═══════════════════════════════════════════════════════════════════════════════
POSITIVE_REACTIONS = ["👍", "❤", "🔥", "🎉", "😍", "👏", "🤩", "💯", "🥰", "🤗", "😎", "🙏", "👌",
                      "⚡", "🏆", "💘", "😁", "🤝", "❤\u200d🔥", "🫡"]
_allowed_cache: dict = {}

async def _allowed_reactions(client: TelegramClient, peer):
    """চ্যানেল/গ্রুপে অনুমোদিত emoji তালিকা; None = সীমা জানা যায়নি/সব চলবে"""
    key = str(getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None) or peer)
    if key in _allowed_cache:
        return _allowed_cache[key]
    res = None
    try:
        if hasattr(peer, "channel_id"):
            full = await client(GetFullChannelRequest(peer))
        else:
            full = await client(GetFullChatRequest(peer.chat_id))
        ar = getattr(full.full_chat, "available_reactions", None)
        some = getattr(ar, "reactions", None)
        if some is not None:
            res = [r.emoticon for r in some if isinstance(r, ReactionEmoji)]
    except Exception as e:
        logging.debug(f"[VR] allowed: {e}")
    _allowed_cache[key] = res
    return res

async def _view_react(client: TelegramClient, peer, msg_id: int):
    try:
        await client(GetMessagesViewsRequest(peer=peer, id=[msg_id], increment=True))
    except Exception as e:
        logging.debug(f"[VR] view: {e}")
    allowed = await _allowed_reactions(client, peer)
    if allowed:
        pos = [e for e in allowed if e in POSITIVE_REACTIONS]
        rest = [e for e in allowed if e not in pos]
        emojis = random.sample(pos, len(pos)) + random.sample(rest, len(rest))
    else:
        emojis = random.sample(POSITIVE_REACTIONS, len(POSITIVE_REACTIONS))
    for em in emojis:
        try:
            await client(SendReactionRequest(peer=peer, msg_id=msg_id,
                                             reaction=[ReactionEmoji(emoticon=em)]))
            return True
        except FloodWaitError as e:
            await asyncio.sleep(min(e.seconds, 30))
        except Exception as e:
            logging.debug(f"[VR] react {em}: {e}")
    return False

async def _vr_set(session: dict, uid: int, on: bool):
    session["vr"] = on
    hs = session.setdefault("vr_handlers", {})
    for phone, (client, h) in list(hs.items()):
        try: client.remove_event_handler(h)
        except Exception: pass
    hs.clear()
    if not on:
        return
    seen = set()
    for phone in session["phones"]:
        st = live_state.get((uid, phone))
        if not st:
            continue
        client, chat_id = st["client"], st["chat_id"]

        async def _on_post(ev, client=client, phone=phone, chat_id=chat_id):
            if not session.get("vr"):
                return
            mid = ev.message.id
            if (phone, mid) in seen:
                return
            seen.add((phone, mid))
            await asyncio.sleep(random.uniform(1, 6))
            await _view_react(client, await client.get_input_entity(chat_id), mid)

        client.add_event_handler(_on_post, events.NewMessage(chats=chat_id, incoming=True))
        hs[phone] = (client, _on_post)

        async def _latest(client=client, phone=phone, chat_id=chat_id):
            try:
                peer = await client.get_input_entity(chat_id)
                msgs = await client.get_messages(peer, limit=1)
                if msgs and (phone, msgs[0].id) not in seen:
                    seen.add((phone, msgs[0].id))
                    await asyncio.sleep(random.uniform(0.5, 4))
                    await _view_react(client, peer, msgs[0].id)
            except Exception as e:
                logging.warning(f"[VR] latest {phone}: {e}")
        asyncio.create_task(_latest())

async def vr_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        return await q.answer("Unauthorized", show_alert=True)
    sid = q.data.split(":", 1)[1]
    session = live_sessions.get(sid)
    if not session or session["uid"] != q.from_user.id:
        return await q.answer("Session not found", show_alert=True)
    on = not session.get("vr")
    await _vr_set(session, session["uid"], on)
    await q.answer("View & Reaction ON" if on else "View & Reaction OFF")
    try:
        await q.edit_message_reply_markup(reply_markup=_live_kb(sid))
    except Exception:
        pass

async def hand_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin_user(q.from_user.id):
        return await q.answer("Unauthorized", show_alert=True)
    sid = q.data.split(":", 1)[1]
    session = live_sessions.get(sid)
    if not session or session["uid"] != q.from_user.id:
        return await q.answer("Session not found", show_alert=True)
    uid = session["uid"]
    ok = 0
    for phone in session["phones"]:
        st = live_state.get((uid, phone))
        if not st:
            continue
        try:
            await st["client"](EditGroupCallParticipantRequest(
                call=st["call"], participant=InputPeerSelf(), raise_hand=True))
            ok += 1
        except Exception as e:
            logging.warning(f"[HAND] {phone}: {e}")
    await q.answer(f"Hand Raised : {ok} / {len(session['phones'])}", show_alert=True)

# ═══════════════════════════════════════════════════════════════════════════════
# JOIN CHANNEL / GROUP
# ═══════════════════════════════════════════════════════════════════════════════
async def handle_grp_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    link = update.message.text.strip()
    accs = usable_accounts(uid)
    if not accs:
        user_flow.pop(uid, None)
        return await update.message.reply_text(
            err_card("কোনো ব্যবহারযোগ্য account নেই", f"আগে {mb('ADD ACCOUNT')} দিয়ে account যোগ করুন"),
            parse_mode="HTML", reply_markup=get_kb(uid))

    ct = f"🤖 <b>{mb('Checking Accounts')}</b>"
    msg = await update.message.reply_text(loading_frame(ct, 0, "Checking"), parse_mode="HTML")

    async def edit(t):
        await msg.edit_text(t, parse_mode="HTML")

    members, others = await run_with_loading(edit, ct, _split_members(uid, accs, link), label="Checking")
    user_flow.pop(uid, None)

    if not others:                               # সবাই আগে থেকেই member — animation লাগবে না
        n = len(members)
        return await _final_msg(
            update, msg,
            f"✅ <b>{mb('All Accounts Already Joined')}</b>\n{LINE}\n"
            f"<b>{mb('Total Bot Account')}</b> : {n}\n"
            f"<b>{mb('Already Member')}</b> : {n}\n"
            f"{pbar(n, n)}",
            kb=get_kb(uid))

    sd = {"total": len(others), "pending": len(others), "already": 0, "skipped": len(members)}
    gt = "Joining Channel / Group"
    failed = []

    async def _work():
        for phone in others:
            try:
                client = await get_client(uid, phone, accs[phone]["session"])
                entity, _new = await _ensure_joined(client, link)
                if entity:
                    sd["already"] += 1
                else:
                    failed.append(phone)
            except Exception as e:
                failed.append(phone)
                logging.warning(f"[GRP] {phone}: {e}")
            sd["pending"] -= 1

    await edit(_format_join_status(sd, 0, gt))
    await animate(edit, lambda t: _format_join_status(sd, t, gt), _work(), interval=0.9, final=True)

    extra = f"\n<b>{mb('Failed')}</b> : {len(failed)}" if failed else ""
    await _final_msg(
        update, msg,
        _format_join_status(sd, None, gt) + extra + f"\n\n✅ <b>{mb('Join Completed!')}</b>",
        kb=get_kb(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# ADD ADMIN / ALL ADMIN
# ═══════════════════════════════════════════════════════════════════════════════
ADMIN_SETTING_TITLE = "𝗔𝗗𝗠𝗜𝗡 𝗦𝗘𝗧𝗧𝗜𝗡𝗚 𝗢𝗡/𝗢𝗙𝗙"

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
        f"<b>𝗔𝗗𝗠𝗜𝗡 𝗦𝗘𝗧𝗧𝗜𝗡𝗚 𝗔𝗖𝗖𝗘𝗦𝗦 :</b> {_yn(d['settings'])}\n\n"
        f"<b>𝗕𝗢𝗧 𝗜𝗠𝗣𝗢𝗥𝗧 &amp; 𝗘𝗫𝗣𝗢𝗥𝗧 :</b> {_yn(d['ie'])}\n\n"
        f"<b>𝗔𝗗𝗠𝗜𝗡 𝗧𝗬𝗣𝗘 :</b> {'𝗣𝗥𝗘𝗠𝗜𝗨𝗠' if d['type'] == 'premium' else '𝗡𝗢𝗥𝗠𝗔𝗟'}"
    )

async def ask_add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 <b>{mb('Admin User ID')}</b>\n{LINE}\nAdmin এর numeric user ID দিন" + CANCEL_HINT,
        parse_mode="HTML")

async def _begin_add_admin(update: Update, uid: int, target: int):
    """User ID পাওয়ার পর ADMIN SETTING ON/OFF message দেখায়"""
    if target == OWNER_ID:
        return await update.message.reply_text("⚠️ <b>এটা owner এর ID।</b>", parse_mode="HTML", reply_markup=get_kb(uid))
    if target == uid or target in admins:
        return await update.message.reply_text("⚠️ <b>Already admin।</b>", parse_mode="HTML", reply_markup=get_kb(uid))
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
        return await update.message.reply_text("⚠️ <b>Valid user ID দিন (সংখ্যা)।</b>", parse_mode="HTML")
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
            f"❌ <b>𝗖𝗮𝗻𝗰𝗲𝗹𝗹𝗲𝗱 𝗔𝗱𝗱 𝗔𝗱𝗺𝗶𝗻 𝗥𝗲𝗾...</b>",
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
            try: await q.message.delete()
            except Exception: pass
            return await ctx.bot.send_message(
                q.message.chat_id,
                _draft_summary(d) + f"\n{LINE}\n✅ <b>𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆</b>",
                parse_mode="HTML", api_kwargs=dict(FX_PARTY))

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
            f"📦 <b>𝗬𝗼𝘂𝗿 𝗛𝗮𝘃𝗲 𝗔𝗰𝗰𝗼𝘂𝗻𝘁𝘀 :</b> {avail}\n{LINE}\n"
            f"এখন আপনি আপনার এডমিন কে কয়টি একাউন্ট দিতে চাচ্ছেন?\n"
            f"<i>(1-{avail})</i>" + CANCEL_HINT,
            parse_mode="HTML")
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
        return await update.message.reply_text(f"⚠️ <b>1 থেকে {len(pool)} এর মধ্যে দিন</b>", parse_mode="HTML")

    chosen = pool[:n]                       # এখনো কাউকে দেওয়া হয়নি এমন account থেকে
    await _release_runtime(uid, chosen)     # দাতার চলমান VC/client থাকলে বন্ধ
    accs = user_accounts(uid)
    for ph in chosen:
        accs[ph]["lent_to"] = target
    set_user_accounts(uid, accs)
    _create_admin(target, d, uid)
    user_flow.pop(uid, None)
    await update.message.reply_text(
        _draft_summary(d) + f"\n{LINE}\n"
        f"<b>𝗔𝗰𝗰𝗼𝘂𝗻𝘁𝘀 𝗚𝗶𝘃𝗲𝗻 :</b> {n}\n"
        f"<b>𝗬𝗼𝘂𝗿 𝗥𝗲𝗺𝗮𝗶𝗻𝗶𝗻𝗴 :</b> {len(pool) - n}\n"
        f"{LINE}\n"
        f"✅ <b>𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱 𝗦𝘂𝗰𝗰𝗲𝘀𝘀𝗳𝘂𝗹𝗹𝘆</b>",
        parse_mode="HTML", reply_markup=get_kb(uid), api_kwargs=dict(FX_PARTY))

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
    title = f"🪖 <b>{mb('ALL ADMIN HISTORY')}</b>"

    is_cb = isinstance(src, Update) and src.callback_query is not None
    if not items:
        text = (f"{title}\n{LINE}\n📭 <b>কোনো admin নেই</b>\n\n"
                f"<i>{mb('ADD ADMIN')} চেপে নতুন admin যোগ করুন</i>")
        if is_cb:
            return await src.callback_query.edit_message_text(text, parse_mode="HTML")
        return await src.message.reply_text(text, parse_mode="HTML", reply_markup=get_kb(uid))

    prem_n = sum(1 for a in items if admins.get(a, {}).get("type") == "premium")
    lines = [
        title,
        LINE,
        f"<b>{mb('Total')}</b> {total} {SEP} <b>{mb('Prem')}</b> {prem_n} {SEP} <b>{mb('Norm')}</b> {total - prem_n} {SEP} <b>{mb('Page')}</b> {page + 1}/{pages}",
        LINE,
        f"<b>{mb('NO')}</b> {SEP} <b>{mb('ID')}</b> {SEP} <b>{mb('TYPE')}</b> {SEP} <b>{mb('SET')}</b> {SEP} <b>{mb('I/E')}</b> {SEP} <b>{mb('ACC')}</b>",
        LINE,
    ]
    for i, aid in enumerate(page_items):
        n = start + i + 1
        info = admins.get(aid, {})
        prem = info.get("type") == "premium"
        row = (f"<code>{n:02d}</code> {SEP} <code>{aid}</code> {SEP} {mb('PREM') if prem else mb('NORM')} {SEP} "
               f"{_yn(info.get('settings'))} {SEP} {_yn(info.get('ie'))} {SEP} "
               f"{('<code>' + str(given.get(aid, 0)) + '</code>') if prem else '—'}")
        if is_owner(uid) and info.get("added_by") not in (None, OWNER_ID):
            row += f" ↳<code>{info['added_by']}</code>"
        lines.append(row)
    lines.append(LINE)
    lines.append(f"<i>{mb('SET')} = Admin Setting {SEP} {mb('I/E')} = Import/Export {SEP} {mb('ACC')} = Accounts Given</i>")

    kb = _nav_kb("adm", "adm_rm", "🗑️ 𝗥𝗘𝗠𝗢𝗩𝗘", page, pages)
    text = "\n".join(lines)
    if is_cb:
        try:
            await src.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise
    else:
        await src.message.reply_text(text, parse_mode="HTML", reply_markup=kb)

async def handle_rm_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    if not can_manage_admins(uid):
        return
    try:
        n = int(update.message.text.strip())
    except ValueError:
        return await update.message.reply_text("⚠️ একটি সংখ্যা দিন (list number)")
    items = managed_admins(uid)
    if not (1 <= n <= len(items)):
        return await update.message.reply_text(f"❌ 1 থেকে {len(items)} এর মধ্যে দিন")
    target = items[n - 1]
    returned = await remove_admin(target, uid)
    user_flow.pop(uid, None)
    extra = f"\n<b>{mb('Accounts Returned')}</b> : {returned}" if returned else ""
    await update.message.reply_text(
        f"🗑️ <b>{mb('Admin Removed')}</b>\n{LINE}\n<b>{mb('ID')}</b> : <code>{target}</code>{extra}",
        parse_mode="HTML", reply_markup=get_kb(uid)
    )

# ═══════════════════════════════════════════════════════════════════════════════
# BOT IMPORT / EXPORT
# ═══════════════════════════════════════════════════════════════════════════════
async def ask_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📥 <b>{mb('Send Session File')}</b>\n"
        f"{LINE}\n"
        "Session strings এর ফাইল পাঠান\n"
        "<i>JSON file (list of strings) অথবা plain text (per line)</i>" + CANCEL_HINT,
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
        return await update.message.reply_text("⚠️ <b>File পাঠান</b>", parse_mode="HTML")

    try:
        f = await ctx.bot.get_file(doc.file_id)
        data = await f.download_as_bytearray()
        text = data.decode("utf-8", errors="ignore")
    except Exception as e:
        return await update.message.reply_text(err_card("File read error", html.escape(str(e))), parse_mode="HTML")

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
        return await update.message.reply_text(err_card("কোনো valid session string পাওয়া যায়নি"), parse_mode="HTML")

    user_flow.pop(uid, None)
    total = len(sessions)
    st = {"pending": total, "success": 0}

    def _imp_text(t=None):
        pend = max(0, st["pending"])
        busy = t is not None and pend > 0
        head = f"{spin(t)} " if busy else "✓ "
        return (
            f"📥 <b>{mb('Importing Accounts')}</b>\n{LINE}\n"
            f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗦𝗧𝗥𝗜𝗡𝗚 : {total}\n"
            f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗣𝗔𝗡𝗗𝗜𝗡𝗚 : {pend}\n"
            f"𝗧𝗢𝗧𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗟𝗚 𝗦𝗨𝗖𝗖𝗘𝗦𝗦 : {st['success']}\n"
            f"{LINE}\n{head}{pbar(total - pend, total, tick=t if busy else None)}"
        )

    status_msg = await update.message.reply_text(_imp_text(0), parse_mode="HTML")

    async def _work():
        for i, sess in enumerate(sessions):
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
                st["success"] += 1
                if st["success"] % 10 == 0:           # প্রতি ১০ টা login এ milestone message
                    try:
                        await update.message.reply_text(
                            f"<b>{mb('Milestone')}</b> : {st['success']} {mb('accounts logged in')}\n"
                            f"{pbar(total - max(0, st['pending'] - 1), total)}",
                            parse_mode="HTML")
                    except Exception:
                        pass
            except Exception as e:
                logging.warning(f"[IMPORT {i+1}] {e}")
            st["pending"] -= 1

    await animate(lambda t: status_msg.edit_text(t, parse_mode="HTML"),
                  _imp_text, _work(), interval=1.0, final=True)
    success = st["success"]

    await _final_msg(
        update, status_msg,
        f"✅ <b>𝗔𝗹𝗹 𝗔𝗰𝗰𝗼𝘂𝗻𝘁 𝗟𝗼𝗴𝗶𝗻 𝗦𝘂𝗰𝗰𝗲𝘀𝘀</b>\n"
        f"{LINE}\n"
        f"<b>{mb('Success')}</b> : {success} / {total}\n"
        f"<b>{mb('Failed')}</b> : {total - success}\n"
        f"{pbar(success, total)}\n"
        f"{LINE}\n"
        f"<b>𝗡𝗼𝘄 𝗬𝗼𝘂 𝗨𝘀𝗲 𝗩𝗰 𝗕𝗼𝘁....</b>",
        kb=get_kb(uid))

async def do_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE, uid: int):
    accs = user_accounts(uid)
    if not accs:
        return await update.message.reply_text("📭 <b>কোনো account নেই।</b>", parse_mode="HTML")
    sessions = [info["session"] for info in accs.values() if info.get("session")]
    if not sessions:
        return await update.message.reply_text("📭 <b>কোনো session string নেই।</b>", parse_mode="HTML")

    buf = io.BytesIO(json.dumps(sessions, indent=2).encode())
    buf.name = f"accounts_{uid}.json"
    await update.message.reply_document(
        document=buf,
        caption=(f"📤 <b>{mb('BOT EXPORT')}</b>\n{LINE}\n"
                 f"<b>{mb('Sessions')}</b> : {len(sessions)}\n"
                 f"<b>{mb('Date')}</b> : {datetime.now().strftime('%d %b %Y')}\n"
                 f"<i>{mb('Warning')} : এই file কাউকে শেয়ার করবেন না</i>"),
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ENSURE JOINED / CALL ENGINE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _invite_hash(link: str):
    link = link.strip()
    if "t.me/+" in link or "joinchat/" in link:
        return (link.split("/+")[-1] if "/+" in link else link.split("joinchat/")[-1]).strip()
    return None

async def _is_member(client: TelegramClient, link: str) -> bool:
    """account আগে থেকেই ওই channel/group এ আছে কিনা — join না করেই চেক"""
    try:
        h = _invite_hash(link)
        if h:
            return isinstance(await client(CheckChatInviteRequest(h)), ChatInviteAlready)
        username = (link.strip().replace("https://", "").replace("http://", "")
                    .replace("t.me/", "").replace("@", "").split("/")[0])
        entity = await client.get_entity(username)
        if isinstance(entity, Channel):
            try:
                await client(GetParticipantRequest(entity, InputPeerSelf()))
                return True
            except UserNotParticipantError:
                return False
        me = await client.get_me()                       # সাধারণ group
        full = await client(GetFullChatRequest(entity.id))
        return any(getattr(p, "user_id", None) == me.id
                   for p in getattr(full.full_chat.participants, "participants", []))
    except Exception:
        return False        # নিশ্চিত না হলে "member না" ধরে join চেষ্টা হবে (join idempotent)

async def _split_members(uid: int, accs: dict, link: str):
    """-> (আগে থেকেই member এর phones, join লাগবে এমন phones) — accs এর order বজায় থাকে"""
    sem = asyncio.Semaphore(4)

    async def one(phone, info):
        async with sem:
            try:
                client = await get_client(uid, phone, info.get("session", ""))
                return phone, await _is_member(client, link)
            except Exception:
                return phone, False

    res = await asyncio.gather(*[one(p, i) for p, i in accs.items()])
    return [p for p, m in res if m], [p for p, m in res if not m]

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
            try: await c.mute(update.chat_id)
            except Exception: pass
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
    _last_mute_at.pop(me.id, None)
    await _force_mute(engine, chat_id, client, call)
    # join এর পর সার্ভারে participant বসতে সময় লাগে, তাই কয়েকবার mute নিশ্চিত করা হয়
    asyncio.create_task(_remute_later(engine, chat_id, client, call))

_last_mute_at: dict = {}

async def _force_mute(engine, chat_id, client, call):
    try: await engine.mute(chat_id)
    except Exception: pass
    try:
        await client(EditGroupCallParticipantRequest(
            call=call, participant=InputPeerSelf(), muted=True))
    except Exception: pass

async def _remute_later(engine, chat_id, client, call):
    for d in (1.5, 3, 6, 12):
        await asyncio.sleep(d)
        await _force_mute(engine, chat_id, client, call)

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
                if now - _last_mute_at.get(me_id, 0) < 3:
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

    await q.answer()
    uid = session["uid"]
    accs = usable_accounts(uid)
    phones = list(session["phones"])
    st_l = {"left": 0}
    try: await _vr_set(session, uid, False)
    except Exception: pass

    def _render(t=None):
        done = st_l["left"]
        busy = t is not None and done < len(phones)
        head = f"{spin(t)} " if busy else "✓ "
        return (f"💣 <b>{mb('Leaving VC')}</b>\n{LINE}\n"
                f"<b>{mb('Accounts Left')}</b> : {done} / {len(phones)}\n"
                f"{head}{pbar(done, len(phones), tick=t if busy else None)}")

    await q.edit_message_text(_render(0), parse_mode="HTML")

    async def _work():
        for phone in phones:
            _stop_watchdog(uid, phone)
            st = live_state.pop((uid, phone), {})
            try: _last_mute_at.pop(st["me"].id, None)
            except Exception: pass
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
                st_l["left"] += 1
            except Exception as e:
                logging.error(f"[LEAVE] {phone}: {e}")

    await animate(lambda t: q.edit_message_text(t, parse_mode="HTML"), _render, _work(), interval=1.0, final=True)

    left = st_l["left"]
    live_sessions.pop(session_id, None)
    await q.edit_message_text(
        f"✅ <b>{mb('Left VC Success')}</b>\n{LINE}\n"
        f"<b>{mb('Accounts Left')}</b> : {left}\n"
        f"{pbar(left, max(left, 1))}",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
async def _no_access(update: Update, uid: int):
    user_flow.pop(uid, None)
    await update.message.reply_text("⛔ <b>আপনার এই access নেই।</b>", parse_mode="HTML", reply_markup=get_kb(uid))

_panel_msgs: dict = {}      # uid -> শেষ panel message id (chat পরিষ্কার রাখতে আগেরটা মুছি)

async def _open_panel_hint(update: Update, uid: int, tab: str):
    """ALL ACCOUNT / ALL ADMIN চাপলে: tapped message + আগের panel message মুছে একটা inline OPEN PANEL button পাঠায়"""
    chat_id = update.effective_chat.id
    for mid in (update.message.message_id, _panel_msgs.pop(uid, None)):
        if mid:
            try: await update.get_bot().delete_message(chat_id, mid)
            except Exception: pass
    title = f"🪖 <b>{mb('ALL ADMIN HISTORY')}</b>" if tab == "admins" else f"🗄 <b>{mb('ALL ACCOUNT HISTORY')}</b>"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("𝗢𝗣𝗘𝗡 𝗣𝗔𝗡𝗘𝗟", web_app=WebAppInfo(url=webapp_url(tab)),
                             api_kwargs={"style": "primary"})
    ]])
    sent = await update.get_bot().send_message(
        chat_id, f"{title}\n{LINE}\n<i>{mb('Tap Below To Open')}</i>",
        parse_mode="HTML", reply_markup=kb)
    _panel_msgs[uid] = sent.message_id

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
            if webapp_url("accounts"):
                return await _open_panel_hint(update, uid, "accounts")
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
            if webapp_url("admins"):
                return await _open_panel_hint(update, uid, "admins")
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

    await update.message.reply_text(f"<b>{mb('Please Use The Buttons')}</b>", parse_mode="HTML", reply_markup=get_kb(uid))

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
            "🗑️ <b>কোন নাম্বারটি remove করবেন?</b>\n" + LINE + "\n<i>List এর number দিন</i>" + CANCEL_HINT,
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
            "🗑️ <b>কোন ID remove করবেন?</b>\n" + LINE + "\n<i>List এর number দিন</i>" + CANCEL_HINT,
            parse_mode="HTML"
        )

    if data.startswith("nadm:"):
        return await admin_draft_cb(update, ctx)

    if data.startswith("leave:"):
        return await leave_cb(update, ctx)
    if data.startswith("hand:"):
        return await hand_cb(update, ctx)
    if data.startswith("vr:"):
        return await vr_cb(update, ctx)

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
    extra = f"\nAccounts returned: {returned}" if returned else ""
    await update.message.reply_text(f"🗑️ Removed: `{target}`{extra}", parse_mode="Markdown")

async def cmd_list_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    if not can_manage_admins(uid):
        return
    cur = "\n".join(f"• `{a}`" for a in managed_admins(uid)) or "(none)"
    head = f"Owner: `{OWNER_ID}`\n\n" if is_owner(uid) else ""
    await update.message.reply_text(f"{head}*Admins:*\n{cur}", parse_mode="Markdown")

# ═══════════════════════════════════════════════════════════════════════════════
# MINI APP  (ALL ACCOUNT / ALL ADMIN history panel)
# শুধু Telegram এর ভেতর থেকে, bot এর signed initData দিয়ে খোলে। link browser এ খুললে কিছুই দেখাবে না।
# ═══════════════════════════════════════════════════════════════════════════════
def verify_init_data(raw: str):
    """Telegram Mini App initData এর HMAC-SHA256 signature যাচাই। valid হলে user dict, নাহলে None"""
    if not raw or len(raw) > 4096 or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(raw, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        age = time.time() - int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", ""))
    except (ValueError, TypeError):
        return None
    if age > WEBAPP_AUTH_MAX_AGE or age < -300:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user

_RID_KEY = hashlib.sha256(b"rid:" + BOT_TOKEN.encode()).digest()

def rid_for(uid: int, phone: str) -> str:
    """account এর opaque id (পুরো number browser এ না পাঠিয়েও চেনার জন্য; user ভেদে আলাদা)"""
    return hmac.new(_RID_KEY, f"{uid}:{phone}".encode(), hashlib.sha256).hexdigest()[:20]

def _find_account(uid: int, rid: str):
    for ph, info, kind in visible_accounts(uid):
        if hmac.compare_digest(rid_for(uid, ph), str(rid)):
            return ph, info, kind
    return None

_STATUS_SEM = asyncio.Semaphore(3)          # একসাথে বেশি Telethon connection খুললে RAM শেষ হয়
_status_cache: dict = {}                    # sha1(session) -> (time, bool)

def _active_client_for(phone: str):
    for (u, ph), c in active_clients.items():
        if ph == phone:
            try:
                if c.is_connected():
                    return c
            except Exception:
                pass
    return None

async def _account_live(info: dict, phone: str, force: bool = False) -> bool:
    sess = info.get("session", "")
    ck = hashlib.sha1(sess.encode()).hexdigest()
    hit = _status_cache.get(ck)
    if hit and not force and time.time() - hit[0] < 25:
        return hit[1]
    async with _STATUS_SEM:
        live, c, own = False, _active_client_for(phone), False
        try:
            if c is None:
                c, own = TelegramClient(StringSession(sess), API_ID, API_HASH), True
                await asyncio.wait_for(c.connect(), 10)
            live = bool(await asyncio.wait_for(c.get_me(), 8))
        except Exception:
            live = False
        finally:
            if own and c is not None:
                try: await c.disconnect()
                except Exception: pass
    _status_cache[ck] = (time.time(), live)
    return live

_name_cache: dict = {}                      # id -> (time, name, username)

async def _tg_name(aid: int):
    hit = _name_cache.get(aid)
    if hit and time.time() - hit[0] < 600:
        return hit[1], hit[2]
    name, uname = "", ""
    if BOT_REF is not None:
        try:
            ch = await asyncio.wait_for(BOT_REF.get_chat(aid), 3)
            name = " ".join(x for x in (ch.first_name, ch.last_name) if x).strip()
            uname = ch.username or ""
        except Exception:
            pass
    _name_cache[aid] = (time.time(), name, uname)
    return name, uname

@web.middleware
async def webapp_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        if request.method != "POST":
            return web.json_response({"error": "method"}, status=405)
        h = request.headers.get("Authorization", "")
        user, why = None, "unauthorized"
        if h.startswith("tma "):                      # inline button / menu: Telegram signed initData
            user = verify_init_data(h[4:])
        elif h.startswith("tok "):                    # reply keyboard button: bot এর signed token
            tuid, why = verify_token(h[4:])
            user = {"id": tuid} if tuid else None
        if not user:
            return web.json_response({"error": why}, status=401)
        if not is_admin_user(user["id"]):
            return web.json_response({"error": "forbidden"}, status=403)
        request["uid"], request["tg_user"] = user["id"], user
    resp = await handler(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp

async def _body(request: web.Request) -> dict:
    try:
        d = await request.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

async def web_app_page(request):
    path = os.path.join(WEBAPP_DIR, "index.html")
    if not os.path.exists(path):
        return web.Response(text="panel missing", status=404)
    with open(path, "rb") as f:
        return web.Response(body=f.read(), content_type="text/html", charset="utf-8")

async def api_me(request):
    uid, u = request["uid"], request["tg_user"]
    info = admins.get(uid, {})
    role = "owner" if is_owner(uid) else ("premium" if info.get("type") == "premium" else "normal")
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip() or u.get("username") or ""
    if not name:
        n, un = await _tg_name(uid)
        name = n or (f"@{un}" if un else str(uid))
    return web.json_response({"uid": uid, "name": name, "role": role, "can_manage": can_manage_admins(uid)})

async def api_accounts(request):
    uid = request["uid"]
    rows = []
    for ph, info, kind in visible_accounts(uid):
        got = kind == "lent_in"
        rows.append({
            "rid": rid_for(uid, ph),
            "number": mask_phone(ph) if got else ph,        # পাওয়া account এর number পুরো দেখাবে না
            "name": "" if got else str(info.get("name", "")),
            "added": int(info.get("added_at", 0) or 0),
            "kind": kind,
        })
    return web.json_response({"rows": rows, "per_page": PER_PAGE})

async def api_account_status(request):
    uid, d = request["uid"], await _body(request)
    hit = _find_account(uid, d.get("rid", ""))
    if not hit:
        return web.json_response({"error": "not_found"}, status=404)
    ph, info, _kind = hit
    return web.json_response({"live": await _account_live(info, ph, bool(d.get("force")))})

async def api_account_remove(request):
    uid, d = request["uid"], await _body(request)
    hit = _find_account(uid, d.get("rid", ""))
    if not hit:
        return web.json_response({"error": "not_found"}, status=404)
    ph, _info, kind = hit
    if kind == "lent_out":
        return web.json_response({"error": "lent_out"}, status=409)
    if kind == "lent_in":
        return web.json_response({"error": "lent_in"}, status=409)
    ok = await remove_account(uid, ph)
    return web.json_response({"ok": ok, "remaining": len(user_accounts(uid))})

async def api_admins(request):
    uid = request["uid"]
    if not can_manage_admins(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    items = managed_admins(uid)
    given = lent_counts()
    names = await asyncio.gather(*[_tg_name(a) for a in items])
    rows = []
    for aid, (name, uname) in zip(items, names):
        info = admins.get(aid, {})
        prem = info.get("type") == "premium"
        by = info.get("added_by")
        rows.append({
            "id": aid, "name": name, "username": uname,
            "type": "premium" if prem else "normal",
            "settings": bool(info.get("settings")), "ie": bool(info.get("ie")),
            "given": given.get(aid, 0) if prem else 0,
            "added_by": by if (is_owner(uid) and by not in (None, OWNER_ID)) else None,
            "added": int(info.get("added_at", 0) or 0),
        })
    return web.json_response({"rows": rows, "per_page": PER_PAGE})

async def api_admin_remove(request):
    uid, d = request["uid"], await _body(request)
    if not can_manage_admins(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        target = int(d.get("id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad_id"}, status=400)
    if target not in managed_admins(uid):
        return web.json_response({"error": "not_found"}, status=404)
    returned = await remove_admin(target, uid)
    return web.json_response({"ok": True, "returned": returned})

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
    app = web.Application(middlewares=[webapp_mw])
    app.router.add_get("/app", web_app_page)
    app.router.add_get("/app/", web_app_page)
    app.router.add_post("/api/me", api_me)
    app.router.add_post("/api/accounts", api_accounts)
    app.router.add_post("/api/accounts/status", api_account_status)
    app.router.add_post("/api/accounts/remove", api_account_remove)
    app.router.add_post("/api/admins", api_admins)
    app.router.add_post("/api/admins/remove", api_admin_remove)
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
    global _owner_username_cache, BOT_REF

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
    app.add_handler(CommandHandler("db", cmd_db))
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

        BOT_REF = app.bot
        if webapp_url("accounts"):
            logging.info(f"✅ Mini App panel: {WEBAPP_URL}/app")
        else:
            logging.warning("⚠️ Mini App বন্ধ (https WEBAPP_URL / RENDER_EXTERNAL_URL নেই) — পুরনো bot table চলবে")
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