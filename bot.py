#!/usr/bin/env python3
"""ربات آب‌وهوا — نسخه کامل اصلاح‌شده"""

import os, logging, asyncio, time, sqlite3, re, math
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional
from html import escape, unescape
from urllib.parse import quote

import httpx
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ==================== تنظیمات ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE").strip()
# Only this Telegram account can view private bot diagnostics.
# Set OWNER_ID to your numeric Telegram user ID in the hosting environment.
OWNER_ID = os.environ.get("OWNER_ID", "").strip()
OWNER_INFO_CODE = os.environ.get("OWNER_INFO_CODE", "6167").strip() or "6167"
STATS_TIMEZONE = os.environ.get("STATS_TIMEZONE", "Asia/Tehran").strip() or "Asia/Tehran"
WEB_APP_URL = os.environ.get("WEB_APP_URL", "")
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AQI_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
WTTR_API = "https://wttr.in"
# Railway: mount a persistent Volume at /data and set DB_PATH=/data/weather_bot.db.
# Without a persistent volume SQLite data can disappear on redeploy/restart.
DB_PATH = os.environ.get("DB_PATH", "/data/weather_bot.db" if os.path.isdir("/data") else "weather_bot.db")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== دسترسی مالک ====================
def is_owner(uid: int) -> bool:
    """Return True only for the configured Telegram account owner."""
    return OWNER_ID.isdigit() and str(uid) == OWNER_ID

# ==================== دیتابیس ====================
def db():
    """Open a short-lived SQLite connection with a small lock wait."""
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=8.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=8000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def stats_date() -> str:
    """Current reporting day in STATS_TIMEZONE; daily records are never reset."""
    try:
        return datetime.now(ZoneInfo(STATS_TIMEZONE)).date().isoformat()
    except Exception:
        return datetime.now(ZoneInfo("UTC")).date().isoformat()


def record_activity(uid: int):
    """Record one distinct user per reporting day and refresh their daily last_seen."""
    try:
        c = db()
        c.execute(
            """INSERT INTO daily_activity(activity_date,user_id,last_seen)
               VALUES(?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(activity_date,user_id) DO UPDATE SET last_seen=CURRENT_TIMESTAMP""",
            (stats_date(), int(uid)),
        )
        c.commit()
        c.close()
    except Exception as e:
        logger.warning("record_activity failed: %s", e)


def daily_stats(day: Optional[str] = None) -> dict:
    day = day or stats_date()
    try:
        c = db()
        rows = c.execute("SELECT key,value FROM daily_stats WHERE activity_date=?", (day,)).fetchall()
        c.close()
        return {r["key"]: int(r["value"]) for r in rows}
    except Exception as e:
        logger.warning("daily_stats failed: %s", e)
        return {}


def daily_user_history(days: int = 7) -> list:
    """Return persisted distinct-user counts by date (newest first)."""
    try:
        c = db()
        rows = c.execute(
            """SELECT activity_date, COUNT(DISTINCT user_id) AS users
               FROM daily_activity
               GROUP BY activity_date ORDER BY activity_date DESC LIMIT ?""",
            (max(1, min(int(days), 90)),),
        ).fetchall()
        c.close()
        return [(r["activity_date"], int(r["users"])) for r in rows]
    except Exception as e:
        logger.warning("daily_user_history failed: %s", e)
        return []


def users_today(day: Optional[str] = None) -> int:
    day = day or stats_date()
    try:
        c = db()
        row = c.execute("SELECT COUNT(DISTINCT user_id) AS n FROM daily_activity WHERE activity_date=?", (day,)).fetchone()
        c.close()
        return int(row["n"] if row else 0)
    except Exception as e:
        logger.warning("users_today failed: %s", e)
        return 0

def init_db():
    c = db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'fa', unit TEXT DEFAULT 'C',
        default_city TEXT, notify_enabled INTEGER DEFAULT 0, notify_time TEXT DEFAULT '07:00',
        pending_action TEXT, compare_temp TEXT, username TEXT, first_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        default_timezone TEXT, last_notify_date TEXT)""")
    # Migrate existing SQLite databases without dropping user data.
    user_columns = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "default_timezone" not in user_columns:
        cur.execute("ALTER TABLE users ADD COLUMN default_timezone TEXT")
    if "last_notify_date" not in user_columns:
        cur.execute("ALTER TABLE users ADD COLUMN last_notify_date TEXT")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_stats(
        activity_date TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(activity_date, key))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS daily_activity(
        activity_date TEXT NOT NULL, user_id INTEGER NOT NULL,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(activity_date, user_id))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, error TEXT, context TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.commit(); c.close()
    stat("bot_starts")

def stat(key: str, inc: int = 1):
    """Increment lifetime and per-day metrics in one transaction."""
    try:
        c = db()
        c.execute(
            "INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?",
            (key, inc, inc),
        )
        c.execute(
            """INSERT INTO daily_stats(activity_date,key,value) VALUES(?,?,?)
               ON CONFLICT(activity_date,key) DO UPDATE SET value=value+excluded.value""",
            (stats_date(), key, inc),
        )
        c.commit()
        c.close()
    except Exception as e:
        logger.warning("stat(%s) failed: %s", key, e)

def all_stats() -> dict:
    try:
        c = db(); r = c.execute("SELECT key,value FROM stats").fetchall(); c.close()
        return {x["key"]: x["value"] for x in r}
    except Exception: return {}

def log_err(uid, e, ctx=""):
    try:
        c = db()
        c.execute("INSERT INTO errors(user_id,error,context) VALUES(?,?,?)", (uid, str(e)[:400], str(ctx)[:150]))
        c.execute("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY id DESC LIMIT 300)")
        c.commit(); c.close()
    except Exception: pass

def recent_errors(limit=5):
    try:
        c = db(); r = c.execute("SELECT error,created_at FROM errors ORDER BY id DESC LIMIT ?", (limit,)).fetchall(); c.close()
        return [dict(x) for x in r]
    except Exception: return []

def total_users():
    try:
        c = db(); r = c.execute("SELECT COUNT(*) as n FROM users").fetchone(); c.close()
        return r["n"]
    except Exception: return 0

def active_users_24h():
    try:
        c = db(); r = c.execute("SELECT COUNT(*) as n FROM users WHERE last_seen > datetime('now', '-1 day')").fetchone(); c.close()
        return r["n"]
    except Exception: return 0

def top_cities(limit=5):
    try:
        c = db()
        r = c.execute("""SELECT default_city as c, COUNT(*) as n FROM users
                         WHERE default_city IS NOT NULL GROUP BY default_city ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
        c.close(); return [(x["c"], x["n"]) for x in r]
    except Exception: return []

# ==================== کاربران ====================
_u_cache = {}
USER_CACHE_MAX = 5000

def get_user(uid, update=None):
    if uid in _u_cache:
        u = _u_cache[uid].copy()
    else:
        c = db()
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if r: u = dict(r)
        else:
            c.execute("INSERT INTO users(user_id) VALUES(?)", (uid,))
            c.commit()
            u = {"user_id": uid, "lang": "fa", "unit": "C", "default_city": None,
                 "notify_enabled": 0, "notify_time": "07:00", "pending_action": None,
                 "compare_temp": None, "default_timezone": None, "last_notify_date": None}
            stat("new_users")
        c.close()
    if update:
        record_activity(uid)
        try:
            c = db()
            c.execute("UPDATE users SET last_seen=CURRENT_TIMESTAMP, username=?, first_name=? WHERE user_id=?",
                      (update.effective_user.username or "", update.effective_user.first_name or "", uid))
            c.commit(); c.close()
        except Exception: pass
    # Keep the in-memory cache bounded and refresh recently used entries.
    _u_cache.pop(uid, None)
    _u_cache[uid] = u
    if len(_u_cache) > USER_CACHE_MAX:
        _u_cache.pop(next(iter(_u_cache)))
    return u.copy()

def save_user(uid, **kw):
    try:
        c = db()
        sets = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE users SET {sets} WHERE user_id=?", list(kw.values()) + [uid])
        c.commit(); c.close()
        if uid in _u_cache: _u_cache[uid].update(kw)
    except Exception as e: logger.error(f"save_user: {e}")

def notify_users():
    try:
        c = db()
        r = c.execute("SELECT user_id,lang,unit,default_city,default_timezone,notify_time,last_notify_date FROM users WHERE notify_enabled=1").fetchall()
        c.close(); return [dict(x) for x in r]
    except Exception: return []

# ==================== HTTP ====================
_http: Optional[httpx.AsyncClient] = None

async def get_http():
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
            headers={"User-Agent": "WeatherBot/9.0"},
        )
    return _http

# ✅ FIX #1: کش صحیح — timestamp cache جدا
_geo_cache, _geo_t = {}, {}
_wx_cache, _wx_t = {}, {}
_aqi_cache, _aqi_t = {}, {}
GEO_TTL = 86400
WX_TTL = 900
AQI_TTL = 900
CACHE_MAX = 5000  # ✅ FIX #7: سقف کش

def _cget(cache, tc, key, ttl):
    if key in cache:
        if time.time() - tc.get(key, 0) < ttl:
            return cache[key]
        cache.pop(key, None); tc.pop(key, None)
    return None

def _cset(cache, tc, key, value):
    cache[key] = value
    tc[key] = time.time()
    # ✅ FIX #7: محدودیت کش
    if len(cache) > CACHE_MAX:
        oldest = sorted(tc.items(), key=lambda x: x[1])[:len(cache) - CACHE_MAX + 100]
        for k, _ in oldest:
            cache.pop(k, None); tc.pop(k, None)

# ==================== safe_edit ====================
async def safe_edit(msg, text, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
        return msg
    except Exception as e:
        err = str(e).lower()
        if "message is not modified" in err: return msg
        try:
            return await msg.get_bot().send_message(chat_id=msg.chat_id, text=text, **kwargs)
        except Exception:
            try:
                # ✅ FIX #9: HTML unescape + tag removal
                plain = re.sub(r'<[^>]+>', '', text)
                plain = unescape(plain)
                return await msg.get_bot().send_message(chat_id=msg.chat_id, text=plain[:4000])
            except Exception: return msg

# ==================== شهرها ====================
IRAN = [
    ("Tehran","تهران",35.6892,51.3890),("Karaj","کرج",35.8355,50.9915),
    ("Eslamshahr","اسلامشهر",35.5522,51.2350),("Shahriar","شهریار",35.6594,51.0589),
    ("Varamin","ورامین",35.3244,51.6458),("Pakdasht","پاکدشت",35.4722,51.6828),
    ("Pardis","پردیس",35.7386,51.8097),("Robat Karim","رباط‌کریم",35.4844,51.0833),
    ("Qods","قدس",35.7167,51.1167),("Malard","ملارد",35.6667,50.9833),
    ("Damavand","دماوند",35.7172,52.0653),("Firuzkuh","فیروزکوه",35.7567,52.7731),
    ("Lavasan","لواسان",35.8231,51.6361),("Rudehen","رودهن",35.7347,51.9103),
    ("Nasimshahr","نسیم‌شهر",35.5667,51.1667),("Qarchak","قرچک",35.4394,51.5689),
    ("Pishva","پیشوا",35.3092,51.7275),("Mashhad","مشهد",36.2605,59.6168),
    ("Neyshabur","نیشابور",36.2133,58.7958),("Sabzevar","سبزوار",36.2125,57.6819),
    ("Torbat-e Heydarieh","تربت حیدریه",35.2736,59.2197),("Torbat-e Jam","تربت جام",35.2439,60.6236),
    ("Quchan","قوچان",37.1061,58.5094),("Kashmar","کاشمر",35.2381,58.4656),
    ("Gonabad","گناباد",34.3528,58.6836),("Chenaran","چناران",36.6456,59.1211),
    ("Sarakhs","سرخس",36.5447,61.1581),("Fariman","فریمان",35.7039,59.8500),
    ("Khaf","خواف",34.5764,60.1408),("Bardaskan","بردسکن",35.2606,57.9711),
    ("Isfahan","اصفهان",32.6546,51.6680),("Kashan","کاشان",33.9850,51.4100),
    ("Najafabad","نجف‌آباد",32.6333,51.3667),("Khomeynishahr","خمینی‌شهر",32.7000,51.5167),
    ("Shahin Shahr","شاهین‌شهر",32.8667,51.5500),("Shahreza","شهرضا",32.0000,51.8667),
    ("Fooladshahr","فولادشهر",32.4833,51.4000),("Zarrinshahr","زرین‌شهر",32.3833,51.3833),
    ("Khomeyn","خمین",33.6419,50.0800),("Natanz","نطنز",33.5108,51.9183),
    ("Ardestan","اردستان",33.3761,52.3700),("Golpayegan","گلپایگان",33.4536,50.2889),
    ("Semirom","سمیرم",31.4183,51.5711),("Nain","نائین",32.8603,53.0869),
    ("Shiraz","شیراز",29.5918,52.5837),("Marvdasht","مرودشت",29.8739,52.8025),
    ("Jahrom","جهرم",28.5000,53.5600),("Fasa","فسا",28.9389,53.6481),
    ("Kazerun","کازرون",29.6158,51.6553),("Lar","لار",27.6833,54.3417),
    ("Abadeh","آباده",31.1606,52.6519),("Darab","داراب",28.7519,54.5406),
    ("Neyriz","نی‌ریز",29.1981,54.3281),("Sepidan","سپیدان",30.2333,51.9833),
    ("Eqlid","اقلید",30.8978,52.6867),("Estahban","استهبان",29.1278,54.0417),
    ("Tabriz","تبریز",38.0962,46.2738),("Maragheh","مراغه",37.3892,46.2382),
    ("Marand","مرند",38.4320,45.7700),("Ahar","اهر",38.4775,47.0662),
    ("Mianeh","میانه",37.4211,47.7100),("Bonab","بناب",38.4283,45.9087),
    ("Sarab","سراب",36.3175,47.5648),("Azarshahr","آذرشهر",37.7585,45.9794),
    ("Osku","اسکو",37.9161,46.1245),("Shabestar","شبستر",38.1784,45.7002),
    ("Jolfa","جلفا",38.9376,45.6252),("Sardrud","سردرود",38.0314,46.1475),
    ("Urmia","ارومیه",37.5553,45.0725),("Khoy","خوی",38.5500,44.9500),
    ("Mahabad","مهاباد",36.7639,45.7222),("Bukan","بوکان",36.5210,46.2100),
    ("Miandoab","میاندوآب",36.9667,46.1000),("Salmas","سلماس",38.1972,44.7683),
    ("Naqadeh","نقده",36.9553,45.3881),("Piranshahr","پیرانشهر",36.7000,45.1500),
    ("Sardasht","سردشت",36.1580,45.4752),("Shahin Dezh","شاهین‌دژ",36.6769,46.5667),
    ("Takab","تکاب",36.4008,47.1125),("Maku","ماکو",39.2956,44.5117),
    ("Ahvaz","اهواز",31.3167,48.6844),("Abadan","آبادان",30.3392,48.3043),
    ("Khorramshahr","خرمشهر",30.4408,48.1845),("Dezful","دزفول",32.3830,48.4058),
    ("Andimeshk","اندیمشک",32.4610,48.3534),("Shushtar","شوشتر",32.0452,48.8540),
    ("Behbahan","بهبهان",30.5959,50.2417),("Izeh","ایذه",31.8300,49.8667),
    ("Masjed Soleyman","مسجدسلیمان",31.9353,49.3039),("Shush","شوش",32.1944,48.2572),
    ("Bandar Mahshahr","بندر ماهشهر",30.5589,49.1981),("Omidiyeh","امیدیه",30.7622,49.7025),
    ("Ramhormoz","رامهرمز",31.2792,49.6022),("Susangerd","سوسنگرد",31.5619,48.1819),
    ("Shadegan","شادگان",30.6500,48.6667),("Hoveyzeh","هویزه",31.4667,48.0833),
    ("Rasht","رشت",37.2808,49.5832),("Bandar Anzali","بندر انزلی",37.4739,49.4578),
    ("Lahijan","لاهیجان",37.2019,50.0047),("Langarud","لنگرود",37.1956,50.1525),
    ("Astaneh-ye Ashrafiyeh","آستانه اشرفیه",37.2611,49.9431),("Talesh","تالش",37.8008,48.9058),
    ("Rudsar","رودسر",37.1364,50.2931),("Fuman","فومن",37.2239,49.3119),
    ("Sowme'eh Sara","صومعه‌سرا",37.3114,49.3206),("Rudbar","رودبار",36.8236,49.4278),
    ("Astara","آستارا",38.4222,48.8722),("Masal","ماسال",37.3631,49.1325),
    ("Rezvanshahr","رضوانشهر",37.5514,49.1372),("Amlash","املش",37.1000,50.1833),
    ("Sari","ساری",36.5633,53.0601),("Babol","بابل",36.5513,52.6789),
    ("Amol","آمل",36.4696,52.3506),("Qaem Shahr","قائم‌شهر",36.4633,52.8600),
    ("Behshahr","بهشهر",36.6925,53.5522),("Chalus","چالوس",36.6550,51.4200),
    ("Noshahr","نوشهر",36.6489,51.4964),("Ramsar","رامسر",36.9228,50.6539),
    ("Tonekabon","تنکابن",36.8167,50.8761),("Babolsar","بابلسر",36.7025,52.6500),
    ("Fereydunkenar","فریدونکنار",36.6856,52.5164),("Juybar","جویبار",36.6419,52.9122),
    ("Neka","نکا",36.6528,53.2961),("Galugah","گلوگاه",36.7219,53.8083),
    ("Pol-e Sefid","پل سفید",36.1167,52.9167),("Zirab","زیرآب",36.1833,52.9667),
    ("Shirgah","شیرگاه",36.2833,52.8833),("Kiasar","کیاسر",36.2333,53.5333),
    ("Kerman","کرمان",30.2839,57.0834),("Bam","بم",29.1060,58.3570),
    ("Rafsanjan","رفسنجان",30.4067,55.9939),("Sirjan","سیرجان",29.4522,55.6794),
    ("Jiroft","جیرفت",28.6744,57.7436),("Zarand","زرند",30.8128,56.5619),
    ("Shahr-e Babak","شهربابک",30.1147,55.1172),("Kahnuj","کهنوج",27.9522,57.7006),
    ("Bardsir","بردسیر",29.9256,56.5739),("Ravar","راور",31.2647,56.8050),
    ("Bojnord","بجنورد",37.4747,57.3290),("Shirvan","شیروان",37.3975,57.9300),
    ("Esfarayen","اسفراین",37.0764,57.5100),("Birjand","بیرجند",32.8663,59.2211),
    ("Qaen","قائن",33.7258,59.1858),("Ferdows","فردوس",34.0175,58.1700),
    ("Tabas","طبس",33.5956,56.9244),("Nehbandan","نهبندان",31.5333,60.0333),
    ("Sarbisheh","سربیشه",32.5833,59.8000),("Khusf","خوسف",32.7833,58.8833),
    ("Kermanshah","کرمانشاه",34.3142,47.0650),("Eslamabad-e Gharb","اسلام‌آباد غرب",34.1089,46.5308),
    ("Harsin","هرسین",34.2736,47.5861),("Sonqor","سنقر",34.7839,47.5986),
    ("Paveh","پاوه",35.0456,46.3575),("Sanandaj","سنندج",35.3149,46.9921),
    ("Saqqez","سقز",36.2498,46.2735),("Marivan","مریوان",35.5239,46.1761),
    ("Baneh","بانه",35.9989,45.8867),("Qorveh","قروه",35.1664,47.8047),
    ("Bijar","بیجار",35.8728,47.6025),("Kangavar","کنگاور",34.5000,47.9667),
    ("Qom","قم",34.6416,50.8746),("Ardabil","اردبیل",38.2494,48.3014),
    ("Parsabad","پارس‌آباد",39.6489,47.9164),("Khalkhal","خلخال",37.6192,48.5253),
    ("Meshgin Shahr","مشگین‌شهر",38.3986,47.6806),("Germi","گرمی",39.0333,48.0833),
    ("Bileh Savar","بیله سوار",39.3833,48.3500),("Sareyn","سرعین",38.1500,48.0667),
    ("Arak","اراک",34.1380,49.8470),("Saveh","ساوه",35.0214,50.3567),
    ("Mahallat","محلات",33.9111,50.4531),("Zanjan","زنجان",36.6769,48.4963),
    ("Abhar","ابهر",36.1458,49.2181),("Khorramdarreh","خرمدره",36.2069,49.1914),
    ("Qazvin","قزوین",36.2688,50.0041),("Takestan","تاکستان",36.0697,49.6981),
    ("Gorgan","گرگان",36.8378,54.4414),("Gonbad-e Kavus","گنبد کاووس",37.2506,55.1675),
    ("Aliabad-e Katul","علی‌آباد کتول",36.9086,54.8653),("Bandar Torkaman","بندر ترکمن",36.9019,54.0719),
    ("Yazd","یزد",31.8974,54.3569),("Meybod","میبد",32.2500,54.0167),
    ("Ardakan","اردکان",32.3100,54.0175),("Bafq","بافق",31.6119,55.4106),
    ("Taft","تفت",31.7481,54.2058),("Zahedan","زاهدان",29.4963,60.8629),
    ("Zabol","زابل",31.0289,61.4947),("Chabahar","چابهار",25.2919,60.6431),
    ("Iranshahr","ایرانشهر",27.2025,60.6847),("Saravan","سراوان",27.3706,62.3342),
    ("Khash","خاش",28.2211,61.2158),("Hamadan","همدان",34.7983,48.5148),
    ("Malayer","ملایر",34.2969,48.8236),("Nahavand","نهاوند",34.1889,48.3769),
    ("Tuyserkan","تویسرکان",34.5522,48.4456),("Asadabad","اسدآباد",34.7819,48.1197),
    ("Bandar Abbas","بندرعباس",27.1832,56.2666),("Qeshm","قشم",26.9581,56.2719),
    ("Kish","کیش",26.5578,53.9803),("Minab","میناب",27.1461,57.0803),
    ("Bandar Lengeh","بندر لنگه",26.5581,54.8806),("Bushehr","بوشهر",28.9234,50.8200),
    ("Borazjan","برازجان",29.2664,51.2136),("Bandar Ganaveh","بندر گناوه",29.5792,50.5172),
    ("Kangan","کنگان",27.8372,52.0631),("Ilam","ایلام",33.6374,46.4227),
    ("Dehloran","دهلران",32.6942,47.2639),("Abdanan","آبدانان",32.9919,47.4200),
    ("Eyvan","ایوان",33.8275,46.3081),("Semnan","سمنان",35.5769,53.3953),
    ("Shahrud","شاهرود",36.4181,54.9764),("Damghan","دامغان",36.1681,54.3481),
    ("Garmsar","گرمسار",35.2186,52.3406),("Khorramabad","خرم‌آباد",33.4878,48.3558),
    ("Borujerd","بروجرد",33.8973,48.7516),("Dorud","دورود",33.4922,49.0580),
    ("Aligudarz","الیگودرز",33.4006,49.6961),("Kohdasht","کوهدشت",33.5303,47.6072),
    ("Yasuj","یاسوج",30.6681,51.5881),("Dogonbadan","دوگنبدان",30.3600,50.7953),
    ("Shahr-e Kord","شهرکرد",32.3260,50.8644),("Borujen","بروجن",31.9667,51.2833),
    ("Lordegan","لردگان",31.5167,50.8167),("Farsan","فارسان",32.2500,50.5667),
]

def normalize_city_name(value: str) -> str:
    """Normalize Latin/Persian/Arabic variants and whitespace for city lookup."""
    value = str(value or "").strip().casefold()
    value = value.replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    return re.sub(r"\s+", " ", value).strip()


CITY_INDEX = {}

def _add_city(name_en, name_fa, lat, lon, timezone, country_code, *aliases):
    entry = {"name": name_en, "name_fa": name_fa, "latitude": float(lat), "longitude": float(lon),
             "country_code": country_code, "timezone": timezone}
    for city_name in (name_en, name_fa, *aliases):
        if city_name:
            CITY_INDEX[normalize_city_name(city_name)] = entry


for name_en, name_fa, lat, lon in IRAN:
    _add_city(name_en, name_fa, lat, lon, "Asia/Tehran", "IR")

# Common worldwide cities work quickly even when the geocoding API is unavailable.
# Other locations are still supported dynamically by Open-Meteo geocoding.
WORLD_CITIES = [
    ("London", "لندن", 51.5072, -0.1276, "Europe/London", "GB", "Greater London"),
    ("Paris", "پاریس", 48.8566, 2.3522, "Europe/Paris", "FR"),
    ("Berlin", "برلین", 52.5200, 13.4050, "Europe/Berlin", "DE"),
    ("Munich", "مونیخ", 48.1351, 11.5820, "Europe/Berlin", "DE", "München"),
    ("Hamburg", "هامبورگ", 53.5488, 9.9872, "Europe/Berlin", "DE"),
    ("Frankfurt", "فرانکفورت", 50.1109, 8.6821, "Europe/Berlin", "DE"),
    ("Vienna", "وین", 48.2082, 16.3738, "Europe/Vienna", "AT", "Wien"),
    ("Zurich", "زوریخ", 47.3769, 8.5417, "Europe/Zurich", "CH", "Zürich"),
    ("Rome", "رم", 41.9028, 12.4964, "Europe/Rome", "IT", "Roma"),
    ("Madrid", "مادرید", 40.4168, -3.7038, "Europe/Madrid", "ES"),
    ("Barcelona", "بارسلونا", 41.3874, 2.1686, "Europe/Madrid", "ES"),
    ("Amsterdam", "آمستردام", 52.3676, 4.9041, "Europe/Amsterdam", "NL"),
    ("Lisbon", "لیسبون", 38.7223, -9.1393, "Europe/Lisbon", "PT"),
    ("Stockholm", "استکهلم", 59.3293, 18.0686, "Europe/Stockholm", "SE"),
    ("Oslo", "اسلو", 59.9139, 10.7522, "Europe/Oslo", "NO"),
    ("Copenhagen", "کپنهاگ", 55.6761, 12.5683, "Europe/Copenhagen", "DK"),
    ("Istanbul", "استانبول", 41.0082, 28.9784, "Europe/Istanbul", "TR", "İstanbul"),
    ("Ankara", "آنکارا", 39.9334, 32.8597, "Europe/Istanbul", "TR"),
    ("Dubai", "دبی", 25.2048, 55.2708, "Asia/Dubai", "AE"),
    ("Abu Dhabi", "ابوظبی", 24.4539, 54.3773, "Asia/Dubai", "AE"),
    ("Doha", "دوحه", 25.2854, 51.5310, "Asia/Qatar", "QA"),
    ("Riyadh", "ریاض", 24.7136, 46.6753, "Asia/Riyadh", "SA"),
    ("Baku", "باکو", 40.4093, 49.8671, "Asia/Baku", "AZ"),
    ("Tbilisi", "تفلیس", 41.7151, 44.8271, "Asia/Tbilisi", "GE"),
    ("Yerevan", "ایروان", 40.1792, 44.4991, "Asia/Yerevan", "AM"),
    ("Moscow", "مسکو", 55.7558, 37.6173, "Europe/Moscow", "RU"),
    ("Kyiv", "کی‌یف", 50.4501, 30.5234, "Europe/Kyiv", "UA", "Kiev"),
    ("New York", "نیویورک", 40.7128, -74.0060, "America/New_York", "US", "NYC", "New York City"),
    ("Los Angeles", "لس‌آنجلس", 34.0522, -118.2437, "America/Los_Angeles", "US", "LA"),
    ("Chicago", "شیکاگو", 41.8781, -87.6298, "America/Chicago", "US"),
    ("San Francisco", "سان‌فرانسیسکو", 37.7749, -122.4194, "America/Los_Angeles", "US"),
    ("Miami", "میامی", 25.7617, -80.1918, "America/New_York", "US"),
    ("Toronto", "تورنتو", 43.6532, -79.3832, "America/Toronto", "CA"),
    ("Vancouver", "ونکوور", 49.2827, -123.1207, "America/Vancouver", "CA"),
    ("Mexico City", "مکزیکوسیتی", 19.4326, -99.1332, "America/Mexico_City", "MX"),
    ("Buenos Aires", "بوئنوس آیرس", -34.6037, -58.3816, "America/Argentina/Buenos_Aires", "AR"),
    ("Sao Paulo", "سائوپائولو", -23.5505, -46.6333, "America/Sao_Paulo", "BR", "São Paulo"),
    ("Cairo", "قاهره", 30.0444, 31.2357, "Africa/Cairo", "EG"),
    ("Cape Town", "کیپ‌تاون", -33.9249, 18.4241, "Africa/Johannesburg", "ZA"),
    ("Delhi", "دهلی", 28.6139, 77.2090, "Asia/Kolkata", "IN", "New Delhi"),
    ("Mumbai", "بمبئی", 19.0760, 72.8777, "Asia/Kolkata", "IN", "Bombay"),
    ("Karachi", "کراچی", 24.8607, 67.0011, "Asia/Karachi", "PK"),
    ("Bangkok", "بانکوک", 13.7563, 100.5018, "Asia/Bangkok", "TH"),
    ("Singapore", "سنگاپور", 1.3521, 103.8198, "Asia/Singapore", "SG"),
    ("Tokyo", "توکیو", 35.6762, 139.6503, "Asia/Tokyo", "JP"),
    ("Seoul", "سئول", 37.5665, 126.9780, "Asia/Seoul", "KR"),
    ("Beijing", "پکن", 39.9042, 116.4074, "Asia/Shanghai", "CN"),
    ("Hong Kong", "هنگ‌کنگ", 22.3193, 114.1694, "Asia/Hong_Kong", "HK"),
    ("Sydney", "سیدنی", -33.8688, 151.2093, "Australia/Sydney", "AU"),
    ("Melbourne", "ملبورن", -37.8136, 144.9631, "Australia/Melbourne", "AU"),
    ("Auckland", "اوکلند", -36.8509, 174.7645, "Pacific/Auckland", "NZ"),
    ("Brussels", "بروکسل", 50.8503, 4.3517, "Europe/Brussels", "BE"),
    ("Prague", "پراگ", 50.0755, 14.4378, "Europe/Prague", "CZ"),
    ("Warsaw", "ورشو", 52.2297, 21.0122, "Europe/Warsaw", "PL"),
    ("Budapest", "بوداپست", 47.4979, 19.0402, "Europe/Budapest", "HU"),
    ("Athens", "آتن", 37.9838, 23.7275, "Europe/Athens", "GR"),
    ("Helsinki", "هلسینکی", 60.1699, 24.9384, "Europe/Helsinki", "FI"),
    ("Dublin", "دوبلین", 53.3498, -6.2603, "Europe/Dublin", "IE"),
    ("Edinburgh", "ادینبرو", 55.9533, -3.1883, "Europe/London", "GB"),
    ("Tel Aviv", "تل‌آویو", 32.0853, 34.7818, "Asia/Jerusalem", "IL"),
    ("Amman", "امان", 31.9539, 35.9106, "Asia/Amman", "JO"),
    ("Kuwait City", "کویت", 29.3759, 47.9774, "Asia/Kuwait", "KW"),
    ("Muscat", "مسقط", 23.5880, 58.3829, "Asia/Muscat", "OM"),
    ("Tehran", "تهران", 35.6892, 51.3890, "Asia/Tehran", "IR"),
    ("Baghdad", "بغداد", 33.3152, 44.3661, "Asia/Baghdad", "IQ"),
    ("Islamabad", "اسلام‌آباد", 33.6844, 73.0479, "Asia/Karachi", "PK"),
    ("Lahore", "لاهور", 31.5204, 74.3587, "Asia/Karachi", "PK"),
    ("Dhaka", "داکا", 23.8103, 90.4125, "Asia/Dhaka", "BD"),
    ("Jakarta", "جاکارتا", -6.2088, 106.8456, "Asia/Jakarta", "ID"),
    ("Kuala Lumpur", "کوالالامپور", 3.1390, 101.6869, "Asia/Kuala_Lumpur", "MY"),
    ("Manila", "مانیل", 14.5995, 120.9842, "Asia/Manila", "PH"),
    ("Hanoi", "هانوی", 21.0278, 105.8342, "Asia/Ho_Chi_Minh", "VN"),
    ("Taipei", "تایپه", 25.0330, 121.5654, "Asia/Taipei", "TW"),
    ("Perth", "پرت", -31.9505, 115.8605, "Australia/Perth", "AU"),
    ("Brisbane", "بریزبن", -27.4698, 153.0251, "Australia/Brisbane", "AU"),
    ("Honolulu", "هونولولو", 21.3099, -157.8581, "Pacific/Honolulu", "US"),
    ("Seattle", "سیاتل", 47.6062, -122.3321, "America/Los_Angeles", "US"),
    ("Boston", "بوستون", 42.3601, -71.0589, "America/New_York", "US"),
    ("Washington", "واشینگتن", 38.9072, -77.0369, "America/New_York", "US", "Washington DC", "Washington, D.C."),
    ("Las Vegas", "لاس‌وگاس", 36.1699, -115.1398, "America/Los_Angeles", "US"),
    ("Houston", "هیوستون", 29.7604, -95.3698, "America/Chicago", "US"),
    ("Dallas", "دالاس", 32.7767, -96.7970, "America/Chicago", "US"),
    ("Atlanta", "آتلانتا", 33.7490, -84.3880, "America/New_York", "US"),
    ("Montreal", "مونترآل", 45.5019, -73.5674, "America/Toronto", "CA"),
    ("Lima", "لیما", -12.0464, -77.0428, "America/Lima", "PE"),
    ("Santiago", "سانتیاگو", -33.4489, -70.6693, "America/Santiago", "CL"),
    ("Bogota", "بوگوتا", 4.7110, -74.0721, "America/Bogota", "CO"),
    ("Nairobi", "نایروبی", -1.2921, 36.8211, "Africa/Nairobi", "KE"),
    ("Lagos", "لاگوس", 6.5244, 3.3792, "Africa/Lagos", "NG"),
    ("Casablanca", "کازابلانکا", 33.5731, -7.5898, "Africa/Casablanca", "MA"),
    ("Tunis", "تونس", 36.8065, 10.1815, "Africa/Tunis", "TN"),
    ("Addis Ababa", "آدیس‌آبابا", 8.9806, 38.7578, "Africa/Addis_Ababa", "ET"),
    ("Kolkata", "کلکته", 22.5726, 88.3639, "Asia/Kolkata", "IN"),
    ("Chennai", "چنای", 13.0827, 80.2707, "Asia/Kolkata", "IN"),
    ("Bengaluru", "بنگلور", 12.9716, 77.5946, "Asia/Kolkata", "IN", "Bangalore"),
    ("Pune", "پونه", 18.5204, 73.8567, "Asia/Kolkata", "IN"),
    ("Chengdu", "چنگدو", 30.5728, 104.0668, "Asia/Shanghai", "CN"),
    ("Shenzhen", "شنژن", 22.5431, 114.0579, "Asia/Shanghai", "CN"),
    ("Osaka", "اوساکا", 34.6937, 135.5023, "Asia/Tokyo", "JP"),
    ("Kyoto", "کیوتو", 35.0116, 135.7681, "Asia/Tokyo", "JP"),
]
for city_data in WORLD_CITIES:
    _add_city(*city_data)

WMO = {
    0: {"fa":"آسمان صاف","en":"Clear","ic":"☀️"},1: {"fa":"تقریباً صاف","en":"Mainly clear","ic":"🌤️"},
    2: {"fa":"نیمه ابری","en":"Partly cloudy","ic":"⛅"},3: {"fa":"ابری","en":"Overcast","ic":"☁️"},
    45: {"fa":"مه","en":"Fog","ic":"🌁"},48: {"fa":"مه یخ‌زده","en":"Rime fog","ic":"🌁"},
    51: {"fa":"نم‌نم","en":"Drizzle","ic":"🌦️"},53: {"fa":"نم‌نم","en":"Drizzle","ic":"🌦️"},
    55: {"fa":"نم‌نم شدید","en":"Heavy drizzle","ic":"🌧️"},61: {"fa":"باران سبک","en":"Slight rain","ic":"🌧️"},
    63: {"fa":"باران","en":"Rain","ic":"🌧️"},65: {"fa":"باران شدید","en":"Heavy rain","ic":"🌧️"},
    71: {"fa":"برف سبک","en":"Slight snow","ic":"🌨️"},73: {"fa":"برف","en":"Snow","ic":"🌨️"},
    75: {"fa":"برف شدید","en":"Heavy snow","ic":"❄️"},80: {"fa":"رگبار","en":"Showers","ic":"🌦️"},
    81: {"fa":"رگبار","en":"Showers","ic":"🌧️"},82: {"fa":"رگبار شدید","en":"Violent","ic":"⛈️"},
    56: {"fa":"نم‌نم یخ‌زده","en":"Freezing drizzle","ic":"🌧️"},57: {"fa":"نم‌نم یخ‌زده شدید","en":"Heavy freezing drizzle","ic":"🌧️"},
    66: {"fa":"باران یخ‌زده سبک","en":"Light freezing rain","ic":"🌧️"},67: {"fa":"باران یخ‌زده شدید","en":"Heavy freezing rain","ic":"🌧️"},
    77: {"fa":"دانه‌های برف","en":"Snow grains","ic":"🌨️"},
    95: {"fa":"رعد و برق","en":"Thunderstorm","ic":"⛈️"},96: {"fa":"رعد و برق همراه تگرگ سبک","en":"Thunderstorm with slight hail","ic":"⛈️"},
    85: {"fa":"رگبار برف سبک","en":"Slight snow showers","ic":"🌨️"},86: {"fa":"رگبار برف شدید","en":"Heavy snow showers","ic":"🌨️"},
    99: {"fa":"طوفان شدید همراه تگرگ","en":"Severe thunderstorm with hail","ic":"⛈️"},
}

WWW_TO_WMO = {113:0,116:2,119:3,122:3,143:45,248:45,260:48,176:80,179:71,182:56,185:56,
              200:95,227:71,230:75,263:51,266:53,281:56,284:57,293:61,296:61,299:63,
              302:63,305:65,308:65,311:66,314:67,317:71,320:73,323:71,326:71,329:73,
              332:73,335:75,338:75,350:77,353:80,356:81,359:82,362:85,365:86,368:85,
              371:86,374:85,377:86,386:95,389:96,392:95,395:99}

# ==================== توابع کمکی ====================
def bar(v, mx, n=10):
    if mx <= 0: return "▱" * n
    f = int(min(max(v / mx, 0), 1) * n)
    return "▰" * f + "▱" * (n - f)

def wdir_fa(d): return ["شمال","شمال‌شرق","شرق","جنوب‌شرق","جنوب","جنوب‌غرب","غرب","شمال‌غرب"][int((d+22.5)//45)%8]
def wdir_en(d): return ["N","NE","E","SE","S","SW","W","NW"][int((d+22.5)//45)%8]

def uv_lvl(uv, lang):
    if uv < 3: return ("پایین","🟢") if lang=="fa" else ("Low","🟢")
    if uv < 6: return ("متوسط","🟡") if lang=="fa" else ("Moderate","🟡")
    if uv < 8: return ("بالا","🟠") if lang=="fa" else ("High","🟠")
    if uv < 11: return ("خیلی بالا","🔴") if lang=="fa" else ("Very High","🔴")
    return ("خطرناک","🟣") if lang=="fa" else ("Extreme","🟣")

def aqi_i(v, lang):
    levels = [
        (20,{"lbl":"خوب" if lang=="fa" else "Good","ic":"🟢","ad":"هوا پاکه 😎" if lang=="fa" else "Air clean 😎"}),
        (40,{"lbl":"قابل قبول" if lang=="fa" else "Fair","ic":"🟡","ad":"کیفیت قابل قبوله" if lang=="fa" else "Acceptable"}),
        (60,{"lbl":"متوسط" if lang=="fa" else "Moderate","ic":"🟠","ad":"گروه‌های حساس احتیاط کنن" if lang=="fa" else "Sensitive groups be careful"}),
        (80,{"lbl":"ضعیف" if lang=="fa" else "Poor","ic":"🔴","ad":"ماسک بزن" if lang=="fa" else "Wear mask"}),
        (100,{"lbl":"خیلی ضعیف" if lang=="fa" else "Very Poor","ic":"🟣","ad":"از خونه بیرون نرو!" if lang=="fa" else "Stay indoors!"}),
        (9999,{"lbl":"خطرناک" if lang=="fa" else "Hazardous","ic":"⚫","ad":"🚨 در خونه بمون!" if lang=="fa" else "🚨 Emergency!"}),
    ]
    for t, i in levels:
        if v <= t: return i
    return levels[-1][1]

# Concentration-band upper limits for the European AQI (EEA bands revised in 2024).
# CO is available as a measured concentration, but is not one of the five pollutants
# used to calculate the European AQI; it is intentionally left unclassified here.
_EU_AQI_POLLUTANT_LIMITS = {
    "pm2_5": [5, 15, 50, 90, 140],
    "pm10": [15, 45, 120, 195, 270],
    "no2": [10, 25, 60, 100, 150],
    "o3": [60, 100, 120, 160, 180],
    "so2": [20, 40, 125, 190, 275],
}


def _pollutant_band(v, k):
    limits = _EU_AQI_POLLUTANT_LIMITS.get(k)
    if v is None or not limits:
        return None
    for i, limit in enumerate(limits):
        if v <= limit:
            return i
    return 5


def pol_lvl(v, k):
    band = _pollutant_band(v, k)
    return "⚪" if band is None else ["🟢", "🟡", "🟠", "🔴", "🟣", "⚫"][band]


def pol_quality(v, k, lang):
    if v is None:
        return ""
    if k == "co":
        return "در دسته‌بندی EU-AQI نیست" if lang == "fa" else "Not classified by EU AQI"
    band = _pollutant_band(v, k)
    if band is None:
        return "نامشخص" if lang == "fa" else "Unclassified"
    fa = ["خوب", "قابل قبول", "متوسط", "ضعیف", "خیلی ضعیف", "بسیار ضعیف"]
    en = ["Good", "Fair", "Moderate", "Poor", "Very poor", "Extremely poor"]
    return fa[band] if lang == "fa" else en[band]


def fmt_t(c, u):
    try:
        c = float(c)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(c):
        return "—"
    return f"{round(c * 9 / 5 + 32)}°F" if u == "F" else f"{round(c)}°C"

def smart_tips(w, a, lang):
    t = []
    c = w["current"]
    feels = c.get("apparent_temperature", c.get("temperature_2m", 0))
    uv = c.get("uv_index", 0) or 0
    rp = w.get("rain_prob", 0)
    if lang == "fa":
        if feels >= 42: t.append("🥵 گرمای شدید! آب زیاد بخور")
        elif feels >= 32: t.append("☀️ ضدآفتاب بزن")
        elif feels <= 0: t.append("🥶 سرده! لباس گرم بپوش")
        elif feels <= 10: t.append("🧥 کاپشن بردار")
        if rp >= 60: t.append(f"☔ باران {rp}% — چتر یادت نره")
        if uv >= 6: t.append("🕶️ ضدآفتاب بزن")
        if a and a.get("european_aqi", 0) >= 60: t.append(f"😷 AQI {a['european_aqi']}")
    else:
        if feels >= 42: t.append("🥵 Extreme heat!")
        elif feels >= 32: t.append("☀️ Hot! Sunscreen")
        elif feels <= 0: t.append("🥶 Freezing!")
        elif feels <= 10: t.append("🧥 Chilly, take jacket")
        if rp >= 60: t.append(f"☔ Rain {rp}%")
        if uv >= 6: t.append("🕶️ Sunscreen")
        if a and a.get("european_aqi", 0) >= 60: t.append(f"😷 AQI {a['european_aqi']}")
    return t[:3]

# ==================== API ====================
async def geocode(q, lang="fa"):
    query = str(q or "").strip()
    if not query:
        return None
    key = normalize_city_name(query)
    c = _cget(_geo_cache, _geo_t, key, GEO_TTL)
    if c: return c
    entry = CITY_INDEX.get(key)
    if entry:
        _cset(_geo_cache, _geo_t, key, entry)
        return entry
    try:
        cl = await get_http()
        r = await cl.get(GEO_API, params={"name": q.strip(), "count": 5, "language": lang, "format": "json"})
        r.raise_for_status()
        res = r.json().get("results", [])
        if not res: return None
        target_name = query.split(",", 1)[0].strip().casefold()
        if "," in query:
            # Respect explicit country/region qualifiers rather than forcing an Iranian match.
            res.sort(key=lambda x: (
                0 if str(x.get("name", "")).casefold() == target_name else 1,
                -(x.get("population") or 0),
            ))
        elif lang == "fa":
            # For unqualified Persian searches, prefer Iranian results, then exact/common matches.
            res.sort(key=lambda x: (
                0 if x.get("country_code") == "IR" else 1,
                0 if str(x.get("name", "")).casefold() == target_name else 1,
                -(x.get("population") or 0),
            ))
        else:
            res.sort(key=lambda x: (
                0 if str(x.get("name", "")).casefold() == target_name else 1,
                -(x.get("population") or 0),
            ))
        out = res[0]
        _cset(_geo_cache, _geo_t, key, out)
        return out
    except Exception as e:
        logger.error(f"geocode: {e}"); return None


async def fetch_weather_only(la, lo, city_name=""):
    k = f"{la:.3f},{lo:.3f}"
    # ✅ FIX #1: کش صحیح
    c = _cget(_wx_cache, _wx_t, k, WX_TTL)
    if c: return c
    w = None
    try:
        p = {"latitude": la, "longitude": lo,
             "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,visibility,dew_point_2m,uv_index",
             "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
             "timezone": "auto", "forecast_days": 7, "past_days": 1}
        cl = await get_http()
        r = await cl.get(WEATHER_API, params=p)
        r.raise_for_status()
        candidate = r.json()
        if isinstance(candidate, dict) and isinstance(candidate.get("current"), dict) and isinstance(candidate.get("daily"), dict):
            w = candidate
            # past_days=1 means daily[0] is yesterday and daily[1] is today.
            # Use today's precipitation probability for advice.
            daily = w.get("daily") or {}
            probs = daily.get("precipitation_probability_max") or []
            w["rain_prob"] = probs[1] if len(probs) > 1 and probs[1] is not None else (
                probs[0] if probs and probs[0] is not None else 0
            )
        else:
            logger.warning("OM W returned an unexpected payload")
            w = None
    except Exception as e:
        logger.warning(f"OM W failed: {e}"); w = None
    if not w and city_name:
        try:
            cl = await get_http()
            r = await cl.get(f"{WTTR_API}/{quote(str(city_name), safe='')}?format=j1")
            r.raise_for_status()
            data = r.json()
            cur = data.get("current_condition", [{}])[0]
            days = data.get("weather", [])
            if cur:
                w = {
                    "current": {
                        "temperature_2m": float(cur.get("temp_C", 0)),
                        "apparent_temperature": float(cur.get("FeelsLikeC", cur.get("temp_C", 0))),
                        "relative_humidity_2m": int(cur.get("humidity", 0)),
                        "weather_code": WWW_TO_WMO.get(int(cur.get("weatherCode", 113)), 0),
                        "wind_speed_10m": float(cur.get("windspeedKmph", 0)),
                        "wind_direction_10m": int(cur.get("winddirDegree", 0)),
                        "pressure_msl": float(cur.get("pressure", 1013)),
                        "visibility": float(cur.get("visibility", 10)) * 1000,
                        "dew_point_2m": float(cur.get("DewPointC", 0)),
                        "uv_index": float(cur.get("uvIndex", 0)),
                    },
                    "daily": {
                        "time": [d.get("date") for d in days],
                        "weather_code": [WWW_TO_WMO.get(int(d.get("hourly", [{}])[4].get("weatherCode", 113)) if len(d.get("hourly", [])) > 4 else 113, 0) for d in days],
                        "temperature_2m_max": [float(d.get("maxtempC", 0)) for d in days],
                        "temperature_2m_min": [float(d.get("mintempC", 0)) for d in days],
                        "precipitation_probability_max": [int(d.get("hourly", [{}])[4].get("chanceofrain", 0)) if len(d.get("hourly", [])) > 4 else 0 for d in days],
                        "sunrise": [None] * len(days), "sunset": [None] * len(days),
                    },
                    "rain_prob": int(days[0].get("hourly", [{}])[4].get("chanceofrain", 0)) if days and len(days[0].get("hourly", [])) > 4 else 0,
                    "_fallback_source": "wttr"
                }
        except Exception as e:
            logger.warning(f"wttr failed: {e}"); w = None
    # ✅ FIX #1: کش صحیح
    if w: _cset(_wx_cache, _wx_t, k, w)
    return w


async def fetch_aqi_only(la, lo):
    k = f"{la:.3f},{lo:.3f}"
    # ✅ FIX #1: کش صحیح
    c = _cget(_aqi_cache, _aqi_t, k, AQI_TTL)
    if c: return c
    try:
        cl = await get_http()
        p = {"latitude": la, "longitude": lo,
             "current": "european_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,uv_index",
             "timezone": "auto"}
        r = await cl.get(AQI_API, params=p)
        r.raise_for_status()
        a = r.json().get("current")
        if a: _cset(_aqi_cache, _aqi_t, k, a)
        return a
    except Exception: return None


async def fetch_all_parallel(la, lo, city_name=""):
    """Fetch weather and AQI concurrently; AQI is optional and must not stall weather."""
    wx_task = asyncio.create_task(asyncio.wait_for(fetch_weather_only(la, lo, city_name), timeout=10.0))
    aqi_task = asyncio.create_task(asyncio.wait_for(fetch_aqi_only(la, lo), timeout=3.0))
    try:
        w = await wx_task
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("weather fetch timed out/failed: %s", e)
        w = None
    a = None
    if aqi_task.done():
        try:
            a = aqi_task.result()
        except Exception:
            a = None
    else:
        try:
            a = await asyncio.wait_for(aqi_task, timeout=0.35)
        except asyncio.CancelledError:
            raise
        except Exception:
            aqi_task.cancel()
    return w, a


# ==================== ساخت پیام ====================
def build_w(city, w, a, lang, unit):
    c = w["current"]; d = w.get("daily", {})
    code = c.get("weather_code", 0)
    wm = WMO.get(code, {"fa":"نامشخص","en":"Unknown","ic":"❓"})
    cn = escape(str((city.get("name_fa") or city["name"]) if lang == "fa" else city["name"]))
    desc = wm["fa"] if lang == "fa" else wm["en"]
    ic = wm["ic"]
    tmp = fmt_t(c.get("temperature_2m", 0), unit)
    fls = fmt_t(c.get("apparent_temperature", c.get("temperature_2m", 0)), unit)
    wnd = round(c.get("wind_speed_10m", 0))
    wf = wdir_fa(c.get("wind_direction_10m", 0)); we = wdir_en(c.get("wind_direction_10m", 0))
    hum = c.get("relative_humidity_2m", 0); prs = round(c.get("pressure_msl", 0))
    vis = c.get("visibility"); uv = c.get("uv_index"); dw = c.get("dew_point_2m")

    sr = ss = "—"
    try:
        if d.get("sunrise") and d["sunrise"][1]: sr = datetime.fromisoformat(d["sunrise"][1]).strftime("%H:%M")
        if d.get("sunset") and d["sunset"][1]: ss = datetime.fromisoformat(d["sunset"][1]).strftime("%H:%M")
    except Exception: pass

    L = []
    if lang == "fa":
        L.append(f"{ic} <b>{cn}</b>")
        L.append(f"<i>{desc}</i>")
        L.append(f"✨ {ic} ━━━━━━━━━━━ {ic} ✨")
        observed_at = c.get("time")
        if observed_at:
            L.append(f"🕒 آخرین زمان داده: <b>{escape(str(observed_at).replace('T', ' '))}</b>")
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  حس واقعی: <b>{fls}</b>")
        try:
            if not w.get("_fallback_source") and d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
                diff = d["temperature_2m_max"][1] - d["temperature_2m_max"][0]
                if abs(diff) >= 1:
                    ar = "📈" if diff > 0 else "📉"
                    L.append(f"{ar} امروز <b>{round(abs(diff))}°</b> {'گرم‌تر' if diff>0 else 'سردتر'} از دیروز")
        except Exception: pass
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 رطوبت: <b>{hum}%</b>")
        L.append(f"<code>{bar(hum,100)}</code>")
        L.append(f"💨 باد: <b>{wnd} km/h</b> ({wf})")
        L.append(f"🔽 فشار: <b>{prs}</b> hPa")
        if vis: L.append(f"👁️ دید: <b>{round(vis/1000,1)}</b> km")
        if dw is not None: L.append(f"💦 شبنم: <b>{fmt_t(dw,unit)}</b>")
        if uv is not None:
            lb, ci = uv_lvl(uv, "fa")
            L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
        if sr != "—" or ss != "—":
            L.append(f"🌅 طلوع: <b>{sr}</b>  •  🌇 غروب: <b>{ss}</b>")
        if a and a.get("european_aqi") is not None:
            av = a["european_aqi"]; info = aqi_i(av, "fa")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ کیفیت هوا: {info['ic']} <b>{info['lbl']}</b>  •  AQI: <b>{av}</b>")
        tips = smart_tips(w, a, "fa")
        if tips:
            L.append("")
            L.append("💡 <b>توصیه:</b>")
            for t in tips: L.append(f"  {t}")
        if d.get("time") and len(d["time"]) > 1:
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append("📅 <b>پیش‌بینی ۳ روز:</b>")
            L.append("")
            days_fa = ["دوشنبه","سه‌شنبه","چهارشنبه","پنج‌شنبه","جمعه","شنبه","یکشنبه"]
            for i in range(1, min(4, len(d["time"]))):
                try:
                    wc = WMO.get(d["weather_code"][i], {"fa":"؟","en":"?","ic":"❓"})
                    dt = datetime.fromisoformat(d["time"][i])
                    dn = days_fa[dt.weekday()]
                    L.append(f"{wc['ic']} <b>{dn}</b> — {fmt_t(d['temperature_2m_min'][i],unit)} ~ {fmt_t(d['temperature_2m_max'][i],unit)}")
                except Exception: pass
    else:
        L.append(f"{ic} <b>{cn}</b>")
        L.append(f"<i>{desc}</i>")
        L.append(f"✨ {ic} ━━━━━━━━━━━ {ic} ✨")
        observed_at = c.get("time")
        if observed_at:
            L.append(f"🕒 Data time: <b>{escape(str(observed_at).replace('T', ' '))}</b>")
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  Feels like: <b>{fls}</b>")
        try:
            if not w.get("_fallback_source") and d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
                diff = d["temperature_2m_max"][1] - d["temperature_2m_max"][0]
                if abs(diff) >= 1:
                    ar = "📈" if diff > 0 else "📉"
                    L.append(f"{ar} Today <b>{round(abs(diff))}°</b> {'warmer' if diff>0 else 'colder'} than yesterday")
        except Exception: pass
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 Humidity: <b>{hum}%</b>")
        L.append(f"<code>{bar(hum,100)}</code>")
        L.append(f"💨 Wind: <b>{wnd} km/h</b> ({we})")
        L.append(f"🔽 Pressure: <b>{prs}</b> hPa")
        if vis: L.append(f"👁️ Visibility: <b>{round(vis/1000,1)}</b> km")
        if dw is not None: L.append(f"💦 Dew point: <b>{fmt_t(dw,unit)}</b>")
        if uv is not None:
            lb, ci = uv_lvl(uv, "en")
            L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
        if sr != "—" or ss != "—":
            L.append(f"🌅 Sunrise: <b>{sr}</b>  •  🌇 Sunset: <b>{ss}</b>")
        if a and a.get("european_aqi") is not None:
            av = a["european_aqi"]; info = aqi_i(av, "en")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ Air Quality: {info['ic']} <b>{info['lbl']}</b>  •  AQI: <b>{av}</b>")
        tips = smart_tips(w, a, "en")
        if tips:
            L.append("")
            L.append("💡 <b>Advice:</b>")
            for t in tips: L.append(f"  {t}")
        if d.get("time") and len(d["time"]) > 1:
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append("📅 <b>3-day forecast:</b>")
            L.append("")
            for i in range(1, min(4, len(d["time"]))):
                try:
                    wc = WMO.get(d["weather_code"][i], {"fa":"؟","en":"?","ic":"❓"})
                    dt = datetime.fromisoformat(d["time"][i])
                    dn = dt.strftime("%a")
                    L.append(f"{wc['ic']} <b>{dn}</b> — {fmt_t(d['temperature_2m_min'][i],unit)} ~ {fmt_t(d['temperature_2m_max'][i],unit)}")
                except Exception: pass
    return "\n".join(L)


# ==================== ارسال — با کیبورد ====================
# ✅ FIX #2: kb_main همیشه فرستاده می‌شه
async def snd_weather(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    try:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    except Exception: return
    try:
        city = await geocode(q, u["lang"])
        if not city:
            err = "❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ City not found"
            await safe_edit(msg, err); return
        la, lo = city["latitude"], city["longitude"]
        w, a = await fetch_all_parallel(la, lo, city.get("name", q))
        if not w:
            err = "⚠️ سرویس در دسترس نیست. دوباره تلاش کن." if u["lang"]=="fa" else "⚠️ Service unavailable. Try again."
            await safe_edit(msg, err); return
        text = build_w(city, w, a, u["lang"], u["unit"])
        kb = None
        if WEB_APP_URL:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🌐 وب‌سایت" if u["lang"]=="fa" else "🌐 Website", url=WEB_APP_URL)]])
        await safe_edit(msg, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        stat("weather_requests")
    except Exception as e:
        log_err(uid, e, "snd_weather")
        err_text = "❌ خطا در دریافت اطلاعات؛ دوباره تلاش کن." if u["lang"] == "fa" else "❌ Could not load data. Please try again."
        try: await safe_edit(msg, err_text)
        except Exception: pass


async def snd_aqi(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        city = await geocode(q, u["lang"])
        if not city:
            await safe_edit(msg, "❌" if u["lang"]=="fa" else "❌"); return
        a = await fetch_aqi_only(city["latitude"], city["longitude"])
        if not a or a.get("european_aqi") is None:
            err = "⚠️ داده AQI در دسترس نیست" if u["lang"]=="fa" else "⚠️ AQI data unavailable"
            await safe_edit(msg, err); return
        v = a["european_aqi"]; info = aqi_i(v, u["lang"])
        cn = escape(str((city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]))
        pollutants = {
            "pm2_5": (a.get("pm2_5"), "PM2.5", "ذرات ریز", "Fine particles"),
            "pm10": (a.get("pm10"), "PM10", "ذرات معلق", "Coarse particles"),
            "no2": (a.get("nitrogen_dioxide"), "NO₂", "نیتروژن", "Nitrogen dioxide"),
            "so2": (a.get("sulphur_dioxide"), "SO₂", "گوگرد", "Sulphur dioxide"),
            "o3": (a.get("ozone"), "O₃", "ازن", "Ozone"),
            "co": (a.get("carbon_monoxide"), "CO", "مونوکسید کربن", "Carbon monoxide"),
        }
        if u["lang"] == "fa":
            L = [f"🌫️ <b>کیفیت هوای {cn}</b>", "", f"{info['ic']} <b>{info['lbl']}</b>",
                 f"📊 AQI: <b>{v}</b>", f"<code>{bar(v,100,15)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>آلاینده‌ها:</b>", ""]
            for k,(val,name,desc,_) in pollutants.items():
                if val is not None:
                    q2 = pol_quality(val, k, "fa")
                    L.append(f"{pol_lvl(val,k)} <b>{name}</b>: {round(val)} µg/m³ <i>({q2})</i>")
            uv = a.get("uv_index")
            if uv is not None:
                lb, ci = uv_lvl(uv, "fa")
                L.append(""); L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
            L.append(""); L.append("💡 " + info["ad"])
        else:
            L = [f"🌫️ <b>Air Quality — {cn}</b>", "", f"{info['ic']} <b>{info['lbl']}</b>",
                 f"📊 AQI: <b>{v}</b>", f"<code>{bar(v,100,15)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>Pollutants:</b>", ""]
            for k,(val,name,_,_) in pollutants.items():
                if val is not None:
                    q2 = pol_quality(val, k, "en")
                    L.append(f"{pol_lvl(val,k)} <b>{name}</b>: {round(val)} µg/m³ <i>({q2})</i>")
            uv = a.get("uv_index")
            if uv is not None:
                lb, ci = uv_lvl(uv, "en")
                L.append(""); L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
            L.append(""); L.append("💡 " + info["ad"])
        await safe_edit(msg, "\n".join(L), parse_mode=ParseMode.HTML)
        stat("aqi_requests")
    except Exception as e:
        log_err(uid, e, "snd_aqi")
        err_text = "❌ خطا در دریافت اطلاعات؛ دوباره تلاش کن." if u["lang"] == "fa" else "❌ Could not load data. Please try again."
        try: await safe_edit(msg, err_text)
        except Exception: pass


async def snd_forecast(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        city = await geocode(q, u["lang"])
        if not city: await safe_edit(msg, "❌"); return
        w = await fetch_weather_only(city["latitude"], city["longitude"], city.get("name",""))
        if not w: await safe_edit(msg, "⚠️"); return
        d = w.get("daily", {})
        cn = escape(str((city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]))
        if not d.get("time"): await safe_edit(msg, "⚠️"); return
        if u["lang"] == "fa":
            L = [f"📅 <b>پیش‌بینی — {cn}</b>", ""]
            days_fa = ["دوشنبه","سه‌شنبه","چهارشنبه","پنج‌شنبه","جمعه","شنبه","یکشنبه"]
        else:
            L = [f"📅 <b>Forecast — {cn}</b>", ""]
            days_fa = None
        days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        for i in range(1, min(8, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"fa":"نامشخص","en":"Unknown","ic":"❓"})
            try:
                dt = datetime.fromisoformat(d["time"][i])
                dn = days_fa[dt.weekday()] if days_fa else days_en[dt.weekday()]
            except Exception: continue
            L.append(f"{wc['ic']} <b>{dn}</b> — {fmt_t(d['temperature_2m_min'][i],u['unit'])} ~ {fmt_t(d['temperature_2m_max'][i],u['unit'])}")
        await safe_edit(msg, "\n".join(L), parse_mode=ParseMode.HTML)
        stat("forecast_requests")
    except Exception as e:
        log_err(uid, e, "snd_forecast")
        err_text = "❌ خطا در دریافت اطلاعات؛ دوباره تلاش کن." if u["lang"] == "fa" else "❌ Could not load data. Please try again."
        try: await safe_edit(msg, err_text)
        except Exception: pass


async def snd_compare(ctx, chat_id, uid, cities, u):
    if len(cities) < 2:
        msg = "مثال: /compare تهران مشهد" if u["lang"]=="fa" else "Example: /compare Tehran Mashhad"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(u["lang"])); return
    msg = await ctx.bot.send_message(chat_id=chat_id, text="⏳ ...", reply_markup=kb_main(u["lang"]))
    try:
        c1, c2 = await asyncio.gather(geocode(cities[0], u["lang"]), geocode(cities[1], u["lang"]))
        if not c1 or not c2:
            await safe_edit(msg, "❌"); return
        (w1, _), (w2, _) = await asyncio.gather(
            fetch_all_parallel(c1["latitude"], c1["longitude"], c1.get("name","")),
            fetch_all_parallel(c2["latitude"], c2["longitude"], c2.get("name","")))
        if not w1 or not w2: await safe_edit(msg, "⚠️"); return
        def nm(c): return escape(str((c.get("name_fa") or c["name"]) if u["lang"]=="fa" else c["name"]))
        t1 = w1["current"]["temperature_2m"]; t2 = w2["current"]["temperature_2m"]
        diff = t1 - t2
        diff_display = diff * 9 / 5 if u["unit"] == "F" else diff
        diff_unit = "°F" if u["unit"] == "F" else "°C"
        if u["lang"] == "fa":
            L = [f"⚖️ <b>مقایسه دمای دو شهر</b>", "",
                 f"📍 <b>{nm(c1)}</b> — 🌡️ <b>{fmt_t(t1, u['unit'])}</b>",
                 f"📍 <b>{nm(c2)}</b> — 🌡️ <b>{fmt_t(t2, u['unit'])}</b>", ""]
            if abs(diff) < 0.5: L.append("🤝 دمای هر دو شهر یکسانه")
            elif diff > 0: L.append(f"📈 <b>{nm(c1)}</b> {round(abs(diff_display))}{diff_unit} گرم‌تر از <b>{nm(c2)}</b>")
            else: L.append(f"📈 <b>{nm(c2)}</b> {round(abs(diff_display))}{diff_unit} گرم‌تر از <b>{nm(c1)}</b>")
        else:
            L = [f"⚖️ <b>Compare City Temperatures</b>", "",
                 f"📍 <b>{nm(c1)}</b> — 🌡️ <b>{fmt_t(t1, u['unit'])}</b>",
                 f"📍 <b>{nm(c2)}</b> — 🌡️ <b>{fmt_t(t2, u['unit'])}</b>", ""]
            if abs(diff) < 0.5: L.append("🤝 Same temperature")
            elif diff > 0: L.append(f"📈 <b>{nm(c1)}</b> is {round(abs(diff_display))}{diff_unit} warmer than <b>{nm(c2)}</b>")
            else: L.append(f"📈 <b>{nm(c2)}</b> is {round(abs(diff_display))}{diff_unit} warmer than <b>{nm(c1)}</b>")
        await safe_edit(msg, "\n".join(L), parse_mode=ParseMode.HTML)
        stat("compare_requests")
    except Exception as e:
        log_err(uid, e, "snd_compare")
        err_text = "❌ خطا در دریافت اطلاعات؛ دوباره تلاش کن." if u["lang"] == "fa" else "❌ Could not load data. Please try again."
        try: await safe_edit(msg, err_text)
        except Exception: pass


# ==================== کیبورد ====================
def kb_main(lang):
    if lang == "fa":
        rows = [[KeyboardButton("🌤️ آب‌وهوا"), KeyboardButton("🌫️ کیفیت هوا")],
                [KeyboardButton("📅 پیش‌بینی"), KeyboardButton("🎛️ منو")]]
        ph = "اسم شهر رو بنویس..."
    else:
        rows = [[KeyboardButton("🌤️ Weather"), KeyboardButton("🌫️ Air Quality")],
                [KeyboardButton("📅 Forecast"), KeyboardButton("🎛️ Menu")]]
        ph = "Type a city name..."
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder=ph)


def build_menu(u):
    s = get_user(u["user_id"]); lang = u["lang"]
    nst = "✅ روشن" if s.get("notify_enabled") else "❌ خاموش"
    nst_en = "ON ✅" if s.get("notify_enabled") else "OFF ❌"
    hd = escape(str(s.get("default_city") or ("تنظیم نشده" if lang == "fa" else "not set")))
    if lang == "fa":
        txt = (f"🎛️ <b>منوی ربات</b>\n\n"
               f"🏙️ شهر پیش‌فرض: <b>{hd}</b>\n"
               f"🔔 اعلان روزانه: <b>{nst}</b>\n"
               f"⏰ ساعت اعلان: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ واحد دما: <b>{s['unit']}</b>\n\n"
               "👇 گزینه مورد نظرت رو انتخاب کن:")
        rows = [[InlineKeyboardButton("🏙️ تغییر شهر پیش‌فرض", callback_data="m:def")],
                [InlineKeyboardButton("⚖️ مقایسه دمای دو شهر", callback_data="m:cmp")],
                [InlineKeyboardButton(f"🔔 اعلان روزانه ({nst})", callback_data="m:ntf")],
                [InlineKeyboardButton("⏰ تغییر ساعت اعلان", callback_data="m:tim")],
                [InlineKeyboardButton("🌐 تغییر زبان", callback_data="m:lng"),
                 InlineKeyboardButton("🌡️ تغییر واحد", callback_data="m:unt")]]
    else:
        txt = (f"🎛️ <b>Bot Menu</b>\n\n"
               f"🏙️ Default city: <b>{hd}</b>\n"
               f"🔔 Daily notify: <b>{nst_en}</b>\n"
               f"⏰ Notify time: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ Temp unit: <b>{s['unit']}</b>\n\n"
               "👇 Choose an option:")
        rows = [[InlineKeyboardButton("🏙️ Change Default City", callback_data="m:def")],
                [InlineKeyboardButton("⚖️ Compare City Temperatures", callback_data="m:cmp")],
                [InlineKeyboardButton(f"🔔 Daily Notify ({nst_en})", callback_data="m:ntf")],
                [InlineKeyboardButton("⏰ Change Notify Time", callback_data="m:tim")],
                [InlineKeyboardButton("🌐 Change Language", callback_data="m:lng"),
                 InlineKeyboardButton("🌡️ Change Unit", callback_data="m:unt")]]
    return txt, rows


def build_owner_report(u):
    """Build private operational statistics for the configured owner."""
    lang = u.get("lang", "fa")
    stats = all_stats()
    today_stats = daily_stats()
    total = total_users()
    active = active_users_24h()
    today_active = users_today()
    history = daily_user_history(7)
    # Count only successful weather/AQI/forecast/compare requests. /start is
    # tracked separately and must not inflate this number.
    request_keys = ("weather_requests", "aqi_requests", "forecast_requests", "compare_requests")
    today_requests = sum(today_stats.get(key, 0) for key in request_keys)
    tops = top_cities(5)
    errs = recent_errors(3)
    try:
        c = db()
        row = c.execute("SELECT COUNT(*) AS n FROM users WHERE notify_enabled=1").fetchone()
        enabled_notifications = int(row["n"] if row else 0)
        c.close()
    except Exception:
        enabled_notifications = 0

    if lang == "fa":
        lines = [
            "🔐 <b>اطلاعات خصوصی ربات</b>", "",
            f"👥 کل کاربران ثبت‌شده: <b>{total}</b>",
            f"📅 کاربران یکتای امروز ({stats_date()}): <b>{today_active}</b>",
            f"📨 درخواست‌های موفق امروز: <b>{today_requests}</b>",
            f"🟢 کاربران فعال در ۲۴ ساعت: <b>{active}</b>",
            f"🆕 کاربران ثبت‌شده از زمان شروع شمارش: <b>{stats.get('new_users', 0)}</b>",
            f"🚀 دفعات راه‌اندازی پردازش ربات: <b>{stats.get('bot_starts', 0)}</b>",
            f"▶️ دفعات استفاده از /start: <b>{stats.get('start_requests', 0)} کل / {today_stats.get('start_requests', 0)} امروز</b>", "",
            "📊 <b>تعداد درخواست‌ها</b>",
            f"🌤️ آب‌وهوا: <b>{stats.get('weather_requests', 0)} کل / {today_stats.get('weather_requests', 0)} امروز</b>",
            f"🌫️ کیفیت هوا: <b>{stats.get('aqi_requests', 0)} کل / {today_stats.get('aqi_requests', 0)} امروز</b>",
            f"📅 پیش‌بینی: <b>{stats.get('forecast_requests', 0)} کل / {today_stats.get('forecast_requests', 0)} امروز</b>",
            f"⚖️ مقایسه شهرها: <b>{stats.get('compare_requests', 0)} کل / {today_stats.get('compare_requests', 0)} امروز</b>", "",
            f"🔔 کاربران با اعلان فعال: <b>{enabled_notifications}</b>",
            f"📨 اعلان‌های ارسال‌شده: <b>{stats.get('notifications_sent', 0)}</b>",
        ]
        if history:
            lines.extend(["", "📈 <b>کاربران یکتای روزهای اخیر</b>"])
            lines.extend(f"• {escape(day)}: <b>{count}</b>" for day, count in history)
        if tops:
            lines.extend(["", "🏙️ <b>شهرهای پیش‌فرض پرتکرار</b>"])
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            lines.extend(f"{medals[i]} {escape(str(city))} — <b>{count}</b>" for i, (city, count) in enumerate(tops))
        if errs:
            lines.extend(["", "⚠️ <b>آخرین خطاها</b>"])
            lines.extend(f"• <code>{escape(str(e['error'])[:100])}</code> ({escape(str(e['created_at']))})" for e in errs)
    else:
        lines = [
            "🔐 <b>Private Bot Information</b>", "",
            f"👥 Total registered users: <b>{total}</b>",
            f"📅 Unique users today ({stats_date()}): <b>{today_active}</b>",
            f"📨 Successful requests today: <b>{today_requests}</b>",
            f"🟢 Active in the last 24 hours: <b>{active}</b>",
            f"🆕 Users registered since tracking began: <b>{stats.get('new_users', 0)}</b>",
            f"🚀 Bot process starts: <b>{stats.get('bot_starts', 0)}</b>",
            f"▶️ /start uses: <b>{stats.get('start_requests', 0)} total / {today_stats.get('start_requests', 0)} today</b>", "",
            "📊 <b>Requests</b>",
            f"🌤️ Weather: <b>{stats.get('weather_requests', 0)} total / {today_stats.get('weather_requests', 0)} today</b>",
            f"🌫️ Air quality: <b>{stats.get('aqi_requests', 0)} total / {today_stats.get('aqi_requests', 0)} today</b>",
            f"📅 Forecast: <b>{stats.get('forecast_requests', 0)} total / {today_stats.get('forecast_requests', 0)} today</b>",
            f"⚖️ City comparisons: <b>{stats.get('compare_requests', 0)} total / {today_stats.get('compare_requests', 0)} today</b>", "",
            f"🔔 Users with notifications enabled: <b>{enabled_notifications}</b>",
            f"📨 Notifications sent: <b>{stats.get('notifications_sent', 0)}</b>",
        ]
        if history:
            lines.extend(["", "📈 <b>Unique users in recent days</b>"])
            lines.extend(f"• {escape(day)}: <b>{count}</b>" for day, count in history)
        if tops:
            lines.extend(["", "🏙️ <b>Most common default cities</b>"])
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
            lines.extend(f"{medals[i]} {escape(str(city))} — <b>{count}</b>" for i, (city, count) in enumerate(tops))
        if errs:
            lines.extend(["", "⚠️ <b>Recent errors</b>"])
            lines.extend(f"• <code>{escape(str(e['error'])[:100])}</code> ({escape(str(e['created_at']))})" for e in errs)
    return "\n".join(lines)


# ==================== دستورات ====================
async def cmd_start(update, ctx):
    u = get_user(update.effective_user.id, update)
    stat("start_requests")
    save_user(u["user_id"], pending_action=None)
    if u["lang"] == "fa":
        txt = ("🌤️ <b>به ربات آب‌وهوا خوش آمدید!</b>\n\n"
               "فقط اسم شهر رو بنویس و بفرست 🚀\n\n"
               "🎛️ دکمه منو برای همه امکانات\n"
               "💡 مثلاً: <code>تهران</code> یا <code>Paris</code>")
    else:
        txt = ("🌤️ <b>Welcome to Weather Bot!</b>\n\n"
               "Just type a city name and send 🚀\n\n"
               "🎛️ Tap Menu for all features\n"
               "💡 Example: <code>Tehran</code> or <code>Paris</code>")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))


async def cmd_weather(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_weather(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        msg = "اسم شهر رو بنویس" if u["lang"]=="fa" else "Type city name"
        await update.message.reply_text(msg, reply_markup=kb_main(u["lang"])); return
    await snd_weather(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)


async def cmd_aqi(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_aqi(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        msg = "/aqi تهران" if u["lang"]=="fa" else "/aqi Tehran"
        await update.message.reply_text(msg, reply_markup=kb_main(u["lang"])); return
    await snd_aqi(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)


async def cmd_forecast(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_forecast(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        msg = "/forecast تهران" if u["lang"]=="fa" else "/forecast Tehran"
        await update.message.reply_text(msg, reply_markup=kb_main(u["lang"])); return
    await snd_forecast(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)


async def cmd_compare(update, ctx):
    u = get_user(update.effective_user.id, update)
    raw = (update.message.text or "").partition(" ")[2].strip()
    if "|" in raw:
        cities = [part.strip() for part in raw.split("|", 1)]
    elif ";" in raw:
        cities = [part.strip() for part in raw.split(";", 1)]
    else:
        cities = ctx.args
    await snd_compare(ctx, update.effective_chat.id, u["user_id"], cities, u)


async def cmd_default(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        await update.message.reply_text(f"🏙️ {u.get('default_city') or '—'}",
                                        reply_markup=kb_main(u["lang"])); return
    city = await geocode(" ".join(ctx.args), u["lang"])
    if not city: await update.message.reply_text("❌"); return
    save_user(u["user_id"], default_city=city["name"],
              default_timezone=city.get("timezone"), last_notify_date=None)
    await update.message.reply_text(f"✅ <b>{escape(str(city['name']))}</b>", parse_mode=ParseMode.HTML,
                                    reply_markup=kb_main(u["lang"]))


async def cmd_notify(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        s = "✅" if u.get("notify_enabled") else "❌"
        await update.message.reply_text(f"🔔 {s} {u.get('notify_time','07:00')}",
                                        reply_markup=kb_main(u["lang"])); return
    a = ctx.args[0].lower()
    if a in ("on","روشن"):
        if not u.get("default_city"):
            msg = "اول /default تهران" if u["lang"]=="fa" else "First /default Tehran"
            await update.message.reply_text(msg); return
        changed = not bool(u.get("notify_enabled"))
        save_user(u["user_id"], notify_enabled=1)
        if changed: stat("notify_enabled")
        await update.message.reply_text("✅", reply_markup=kb_main(u["lang"]))
    elif a in ("off","خاموش"):
        save_user(u["user_id"], notify_enabled=0)
        await update.message.reply_text("❌", reply_markup=kb_main(u["lang"]))


async def cmd_settime(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        hint = "نمونه: /settime 07:30" if u["lang"] == "fa" else "Example: /settime 07:30"
        await update.message.reply_text(hint); return
    try:
        h, m = map(int, ctx.args[0].split(":"))
        if not (0<=h<24 and 0<=m<60): raise ValueError
        save_user(u["user_id"], notify_time=f"{h:02d}:{m:02d}")
        await update.message.reply_text(f"✅ {h:02d}:{m:02d}", reply_markup=kb_main(u["lang"]))
    except Exception:
        msg = "❌ فرمت اشتباه" if u["lang"]=="fa" else "❌ Wrong format"
        await update.message.reply_text(msg)


async def on_location(update, ctx):
    u = get_user(update.effective_user.id, update)
    loc = update.message.location
    if not loc: return
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await update.message.reply_text(load)
    try:
        w, a = await fetch_all_parallel(loc.latitude, loc.longitude, "")
        if not w: await safe_edit(msg, "⚠️"); return
        city = {"name": "موقعیت شما" if u["lang"]=="fa" else "Your Location",
                "latitude": loc.latitude, "longitude": loc.longitude}
        await safe_edit(msg, build_w(city, w, a, u["lang"], u["unit"]), parse_mode=ParseMode.HTML)
    except Exception as e:
        log_err(u["user_id"], e, "loc")
        err = "❌ دریافت اطلاعات موقعیت ناموفق بود؛ دوباره تلاش کن." if u["lang"] == "fa" else "❌ Could not load weather for this location. Please try again."
        try:
            await safe_edit(msg, err)
        except Exception:
            pass


# ==================== متن ====================
async def on_text(update, ctx):
    u = get_user(update.effective_user.id, update)
    t = update.message.text.strip(); lang = u["lang"]
    chat_id = update.effective_chat.id

    # The code is handled before pending city/menu actions so it always works.
    # It is not sufficient by itself: the sender must also match OWNER_ID.
    if t == OWNER_INFO_CODE:
        # The trigger is not a password by itself: it only works for OWNER_ID in private chat.
        if update.effective_chat.type != "private" or not is_owner(u["user_id"]):
            return
        try: await update.message.delete()
        except Exception: pass
        save_user(u["user_id"], pending_action=None, compare_temp=None)
        report = build_owner_report(get_user(u["user_id"]))
        await ctx.bot.send_message(chat_id=chat_id, text=report,
                                   parse_mode=ParseMode.HTML, reply_markup=kb_main(lang))
        return

    if t in ["🎛️ منو","🎛️ Menu"]:
        try: await update.message.delete()
        except Exception: pass
        # ✅ FIX #3: کیبورد پایین هم رفرش می‌شه
        txt, rows = build_menu(u)
        await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup(rows))
        # کیبورد پایین جدا
        await ctx.bot.send_message(chat_id=chat_id, text="🔽", reply_markup=kb_main(lang),
                                   disable_notification=True)
        return

    if t in ["🌤️ آب‌وهوا","🌤️ Weather"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_weather(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="w")
        msg = "🏙️ اسم شهر:" if lang=="fa" else "🏙️ City name:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(lang)); return

    if t in ["🌫️ کیفیت هوا","🌫️ Air Quality"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_aqi(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="a")
        msg = "🌫️ اسم شهر:" if lang=="fa" else "🌫️ City name:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(lang)); return

    if t in ["📅 پیش‌بینی","📅 Forecast"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_forecast(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="f")
        msg = "📅 اسم شهر:" if lang=="fa" else "📅 City name:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(lang)); return

    u2 = get_user(u["user_id"]); act = u2.get("pending_action")

    if act:
        save_user(u["user_id"], pending_action=None)
        try: await update.message.delete()
        except Exception: pass
        if act == "def":
            c = await geocode(t, lang)
            if not c: await ctx.bot.send_message(chat_id=chat_id, text="❌"); return
            save_user(u["user_id"], default_city=c["name"],
                      default_timezone=c.get("timezone"), last_notify_date=None)
            u3 = get_user(u["user_id"])
            txt, rows = build_menu(u3)
            await ctx.bot.send_message(chat_id=chat_id, text=f"✅ <b>{escape(str(c['name']))}</b>\n\n{txt}",
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            return
        if act == "c1":
            save_user(u["user_id"], compare_temp=t, pending_action="c2")
            msg = f"✅ {t}\n\n⚖️ شهر دوم:" if lang=="fa" else f"✅ {t}\n\n⚖️ Second city:"
            await ctx.bot.send_message(chat_id=chat_id, text=msg); return
        if act == "c2":
            c1 = u2.get("compare_temp")
            # ✅ FIX #5: چک کردن c1
            if not c1:
                msg = "⚠️ یه بار دیگه از منو شروع کن" if lang=="fa" else "⚠️ Restart from Menu"
                await ctx.bot.send_message(chat_id=chat_id, text=msg); return
            save_user(u["user_id"], compare_temp=None)
            await snd_compare(ctx, chat_id, u["user_id"], [c1, t], u); return
        if act == "w": await snd_weather(ctx, chat_id, u["user_id"], t, u); return
        if act == "a": await snd_aqi(ctx, chat_id, u["user_id"], t, u); return
        if act == "f": await snd_forecast(ctx, chat_id, u["user_id"], t, u); return

    # ✅ FIX #4: پیام کاربر حذف می‌شه ولی بعدش درخواست می‌ره
    try: await update.message.delete()
    except Exception: pass
    await snd_weather(ctx, chat_id, u["user_id"], t, u)


# ==================== Callbacks ====================
async def on_cb(update, ctx):
    q = update.callback_query
    u = get_user(q.from_user.id, update)
    d = q.data; lang = u["lang"]; chat_id = q.message.chat.id

    # Old inline admin-panel buttons can remain in chat history after deployment.
    if d.startswith("a:"):
        alert = "پنل قدیمی حذف شده؛ کد 6167 را در گفت‌وگوی خصوصی بفرست." if lang == "fa" else "The old admin panel was removed; send 6167 in the private chat."
        await q.answer(alert, show_alert=True)
        return

    if d.startswith("lang:"):
        nl = d.split(":")[1]
        save_user(u["user_id"], lang=nl); await q.answer("✅")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        # ✅ FIX #3: بدون نوتیفیکیشن
        await ctx.bot.send_message(chat_id=chat_id, text="🔽", reply_markup=kb_main(nl), disable_notification=True)
        return

    if d.startswith("unit:"):
        un = d.split(":")[1]
        save_user(u["user_id"], unit=un); await q.answer("✅")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "m:def":
        save_user(u["user_id"], pending_action="def"); await q.answer()
        msg = "🏙️ اسم شهر جدید:" if lang=="fa" else "🏙️ New city name:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(lang))
        return

    if d == "m:cmp":
        save_user(u["user_id"], pending_action="c1", compare_temp=None); await q.answer()
        msg = "⚖️ اسم شهر اول:" if lang=="fa" else "⚖️ First city:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb_main(lang))
        return

    if d == "m:ntf":
        s = get_user(u["user_id"])
        if not s.get("default_city"):
            alert = "⚠️ اول شهر پیش‌فرض رو تنظیم کن" if lang=="fa" else "⚠️ Set default city first"
            await q.answer(alert, show_alert=True)
            return
        new = 0 if s.get("notify_enabled") else 1
        save_user(u["user_id"], notify_enabled=new)
        if new: stat("notify_enabled")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        alert = ("✅ روشن شد" if lang=="fa" else "✅ Enabled") if new else ("🔕 خاموش شد" if lang=="fa" else "🔕 Disabled")
        await q.answer(alert, show_alert=False)
        return

    if d == "m:tim":
        await q.answer()
        times = ["06:00","07:00","08:00","09:00","12:00","15:00","18:00","21:00"]
        rows = [[InlineKeyboardButton(tt, callback_data=f"st:{tt}") for tt in times[i:i+4]] for i in range(0,len(times),4)]
        back = "🔙 بازگشت به منو" if lang=="fa" else "🔙 Back to Menu"
        rows.append([InlineKeyboardButton(back, callback_data="m:home")])
        txt = "⏰ <b>ساعت اعلان</b>\n\nساعت مورد نظرت رو انتخاب کن:" if lang=="fa" else "⏰ <b>Notify Time</b>\n\nChoose the time:"
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d.startswith("st:"):
        tt = d.split(":")[1] + ":00"
        save_user(u["user_id"], notify_time=tt); await q.answer(f"✅ {tt}")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "m:home":
        await q.answer()
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "m:lng":
        await q.answer()
        back = "🔙 بازگشت به منو" if lang=="fa" else "🔙 Back to Menu"
        rows = [[InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
                 InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
                [InlineKeyboardButton(back, callback_data="m:home")]]
        txt = "🌐 <b>انتخاب زبان</b>\n\nزبان مورد نظرت رو انتخاب کن:" if lang=="fa" else "🌐 <b>Choose Language</b>\n\nSelect your language:"
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "m:unt":
        await q.answer()
        back = "🔙 بازگشت به منو" if lang=="fa" else "🔙 Back to Menu"
        rows = [[InlineKeyboardButton("🌡️ سلسیوس °C", callback_data="unit:C"),
                 InlineKeyboardButton("🌡️ فارنهایت °F", callback_data="unit:F")],
                [InlineKeyboardButton(back, callback_data="m:home")]]
        txt = "🌡️ <b>واحد دما</b>\n\nواحد مورد نظرت رو انتخاب کن:" if lang=="fa" else "🌡️ <b>Temperature Unit</b>\n\nChoose the unit:"
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return


# ==================== notify ====================
async def notify_job(ctx):
    fallback_now = datetime.now()
    for ud in notify_users():
        city_name = ud.get("default_city")
        if not city_name:
            continue

        city = None
        tz_name = ud.get("default_timezone")
        # Backfill timezone for users who already had a saved city before this update.
        if not tz_name:
            city = await geocode(city_name, ud["lang"])
            if not city:
                continue
            tz_name = city.get("timezone")
            if tz_name:
                save_user(ud["user_id"], default_timezone=tz_name)

        try:
            local_now = datetime.now(ZoneInfo(tz_name)) if tz_name else fallback_now
        except Exception:
            local_now = fallback_now
        if ud.get("notify_time") != local_now.strftime("%H:%M"):
            continue
        today = local_now.date().isoformat()
        if ud.get("last_notify_date") == today:
            continue

        try:
            if city is None:
                city = await geocode(city_name, ud["lang"])
            if not city:
                continue
            w, a = await fetch_all_parallel(city["latitude"], city["longitude"], city.get("name", ""))
            if not w:
                continue
            c = w["current"]
            ic = WMO.get(c.get("weather_code", 0), {"ic": "❓"})["ic"]
            cname = escape(str((city.get("name_fa") or city["name"]) if ud["lang"] == "fa" else city["name"]))
            if ud["lang"] == "fa":
                lines = ["☀️ <b>صبح بخیر!</b>", "", f"{ic} <b>{cname}</b>",
                         f"🌡️ <b>{fmt_t(c.get('temperature_2m', 0), ud['unit'])}</b>"]
            else:
                lines = ["☀️ <b>Good morning!</b>", "", f"{ic} <b>{cname}</b>",
                         f"🌡️ <b>{fmt_t(c.get('temperature_2m', 0), ud['unit'])}</b>"]
            await ctx.bot.send_message(chat_id=ud["user_id"], text="\n".join(lines), parse_mode=ParseMode.HTML)
            # Persist only after successful delivery, preventing duplicate messages in the same local day.
            save_user(ud["user_id"], last_notify_date=today)
            stat("notifications_sent")
        except Exception as e:
            logger.error(f"notify: {e}")


# ==================== main ====================
async def post_init(app):
    init_db()
    await get_http()
    if app.job_queue:
        # A minute-level schedule is enough; date guard prevents duplicates across runs.
        app.job_queue.run_repeating(notify_job, interval=30, first=10)
    logger.info("✅ Bot started")


async def post_shutdown(app):
    global _http
    if _http is not None and not _http.is_closed:
        await _http.aclose()
    _http = None


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ BOT_TOKEN set کن!")
        return
    if not OWNER_ID.isdigit():
        logger.warning("OWNER_ID is not configured; the 6167 owner report is disabled until OWNER_ID is set.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("weather", cmd_weather))
    app.add_handler(CommandHandler("aqi", cmd_aqi))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("compare", cmd_compare))
    app.add_handler(CommandHandler("default", cmd_default))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("settime", cmd_settime))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("🤖 Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
