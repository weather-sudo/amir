#!/usr/bin/env python3
"""ربات هوشمند آب‌وهوا — نسخه نهایی"""

import os, logging, asyncio, time, sqlite3
from datetime import datetime
from typing import Optional

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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "NASA2010")
WEB_APP_URL = os.environ.get("WEB_APP_URL", "")
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AQI_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
DB_PATH = "weather_bot.db"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== نشست ادمین ====================
_admin_sessions = {}
ADMIN_SESSION_TTL = 3600


def is_admin(uid: int) -> bool:
    ts = _admin_sessions.get(uid)
    if ts and time.time() - ts < ADMIN_SESSION_TTL:
        return True
    if ts:
        _admin_sessions.pop(uid, None)
    return False


def login_admin(uid: int):
    _admin_sessions[uid] = time.time()


def logout_admin(uid: int):
    _admin_sessions.pop(uid, None)


# ==================== دیتابیس ====================
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'fa', unit TEXT DEFAULT 'C',
        default_city TEXT, notify_enabled INTEGER DEFAULT 0, notify_time TEXT DEFAULT '07:00',
        pending_action TEXT, compare_temp TEXT, username TEXT, first_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, error TEXT, context TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.commit(); c.close()
    stat("bot_starts")


def stat(key: str, inc: int = 1):
    try:
        c = db()
        c.execute("INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?",
                  (key, inc, inc))
        c.commit(); c.close()
    except Exception: pass


def all_stats() -> dict:
    try:
        c = db(); r = c.execute("SELECT key,value FROM stats").fetchall(); c.close()
        return {x["key"]: x["value"] for x in r}
    except Exception: return {}


def log_err(uid, e, ctx=""):
    try:
        c = db()
        c.execute("INSERT INTO errors(user_id,error,context) VALUES(?,?,?)",
                  (uid, str(e)[:400], str(ctx)[:150]))
        c.execute("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY id DESC LIMIT 300)")
        c.commit(); c.close()
    except Exception: pass


def recent_errors(limit=5):
    try:
        c = db(); r = c.execute("SELECT error,created_at FROM errors ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall(); c.close()
        return [dict(x) for x in r]
    except Exception: return []


def total_users():
    try:
        c = db(); r = c.execute("SELECT COUNT(*) as n FROM users").fetchone(); c.close()
        return r["n"]
    except Exception: return 0


def active_users_24h():
    try:
        c = db()
        r = c.execute("SELECT COUNT(*) as n FROM users WHERE last_seen > datetime('now', '-1 day')").fetchone()
        c.close()
        return r["n"]
    except Exception: return 0


def top_cities(limit=5):
    try:
        c = db()
        r = c.execute("""SELECT default_city as c, COUNT(*) as n FROM users
                         WHERE default_city IS NOT NULL GROUP BY default_city
                         ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
        c.close()
        return [(x["c"], x["n"]) for x in r]
    except Exception: return []


# ==================== کاربران ====================
_u_cache = {}


def get_user(uid, update=None):
    if uid in _u_cache:
        u = _u_cache[uid].copy()
    else:
        c = db()
        r = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if r:
            u = dict(r)
        else:
            c.execute("INSERT INTO users(user_id) VALUES(?)", (uid,))
            c.commit()
            u = {"user_id": uid, "lang": "fa", "unit": "C", "default_city": None,
                 "notify_enabled": 0, "notify_time": "07:00",
                 "pending_action": None, "compare_temp": None}
            stat("new_users")
        c.close()
    if update:
        try:
            c = db()
            c.execute("UPDATE users SET last_seen=CURRENT_TIMESTAMP, username=?, first_name=? WHERE user_id=?",
                      (update.effective_user.username or "", update.effective_user.first_name or "", uid))
            c.commit(); c.close()
        except Exception: pass
    _u_cache[uid] = u
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
        r = c.execute("SELECT user_id,lang,unit,default_city,notify_time FROM users WHERE notify_enabled=1").fetchall()
        c.close(); return [dict(x) for x in r]
    except Exception: return []


# ==================== HTTP Client ====================
_http: Optional[httpx.AsyncClient] = None


async def get_http():
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=30, max_connections=60),
            headers={"User-Agent": "WeatherBot/3.0"},
        )
    return _http


# ==================== کش ====================
_geo_cache, _geo_t = {}, {}
_w_cache = {}
TTL = 300


def _cget(cache, tc, key, ttl):
    if key in cache and time.time() - tc.get(key, 0) < ttl: return cache[key]
    cache.pop(key, None); tc.pop(key, None); return None


# ==================== لیست شهرها ====================
IRAN = [
    ("Tehran","تهران",35.6892,51.3890),("Karaj","کرج",35.8355,50.9915),
    ("Eslamshahr","اسلامشهر",35.5522,51.2350),("Shahriar","شهریار",35.6594,51.0589),
    ("Varamin","ورامین",35.3244,51.6458),("Pakdasht","پاکدشت",35.4722,51.6828),
    ("Pardis","پردیس",35.7386,51.8097),("Robat Karim","رباط‌کریم",35.4844,51.0833),
    ("Qods","قدس",35.7167,51.1167),("Malard","ملارد",35.6667,50.9833),
    ("Damavand","دماوند",35.7172,52.0653),("Firuzkuh","فیروزکوه",35.7567,52.7731),
    ("Lavasan","لواسان",35.8231,51.6361),("Rudehen","رودهن",35.7347,51.9103),
    ("Nasimshahr","نسیم‌شهر",35.5667,51.1667),("Golestan","گلستان",35.5167,51.1833),
    ("Salehieh","صالحیه",35.6333,51.0667),("Baghestan","باغستان",35.6333,51.0833),
    ("Sabashahr","صباشهر",35.6167,51.1000),("Vahidieh","وحیدیه",35.6167,51.0500),
    ("Parand","پرند",35.4833,50.9500),("Chahardangeh","چهاردانگه",35.6000,51.2833),
    ("Qarchak","قرچک",35.4394,51.5689),("Pishva","پیشوا",35.3092,51.7275),
    ("Absard","آبسرد",35.6172,52.1411),("Kilan","کیلان",35.5433,52.2222),
    ("Abali","آبعلی",35.7667,51.9667),("Shemshak","شمشک",36.0167,51.5000),

    ("Mashhad","مشهد",36.2605,59.6168),("Neyshabur","نیشابور",36.2133,58.7958),
    ("Sabzevar","سبزوار",36.2125,57.6819),("Torbat-e Heydarieh","تربت حیدریه",35.2736,59.2197),
    ("Torbat-e Jam","تربت جام",35.2439,60.6236),("Quchan","قوچان",37.1061,58.5094),
    ("Kashmar","کاشمر",35.2381,58.4656),("Gonabad","گناباد",34.3528,58.6836),
    ("Chenaran","چناران",36.6456,59.1211),("Sarakhs","سرخس",36.5447,61.1581),
    ("Fariman","فریمان",35.7039,59.8500),("Khaf","خواف",34.5764,60.1408),
    ("Bardaskan","بردسکن",35.2606,57.9711),("Dargaz","درگز",37.4444,59.1067),
    ("Kalat","کلات",36.9961,59.7619),("Bajestan","بجستان",34.5167,58.1833),
    ("Roshtkhar","رشتخوار",34.9769,59.6222),("Khalilabad","خلیل‌آباد",35.2561,58.2856),
    ("Bakharz","باخرز",34.9833,60.3167),("Salehabad","صالح‌آباد",35.6833,61.1000),

    ("Isfahan","اصفهان",32.6546,51.6680),("Kashan","کاشان",33.9850,51.4100),
    ("Najafabad","نجف‌آباد",32.6333,51.3667),("Khomeynishahr","خمینی‌شهر",32.7000,51.5167),
    ("Shahin Shahr","شاهین‌شهر",32.8667,51.5500),("Shahreza","شهرضا",32.0000,51.8667),
    ("Fooladshahr","فولادشهر",32.4833,51.4000),("Zarrinshahr","زرین‌شهر",32.3833,51.3833),
    ("Khomeyn","خمین",33.6419,50.0800),("Natanz","نطنز",33.5108,51.9183),
    ("Ardestan","اردستان",33.3761,52.3700),("Golpayegan","گلپایگان",33.4536,50.2889),
    ("Semirom","سمیرم",31.4183,51.5711),("Dehaqan","دهاقان",32.0500,51.6500),
    ("Borkhar","برخوار",32.8667,51.6167),("Tiran","تیران",32.7000,51.1500),
    ("Karkevand","کرکوند",32.3667,51.4333),("Varzaneh","ورزنه",32.4167,52.6500),
    ("Nain","نائین",32.8603,53.0869),("Khur","خوروبیابانک",33.7722,54.8722),
    ("Mobarakeh","مبارکه",32.3453,51.5014),("Kuhpayeh","کوهپایه",32.7167,52.4333),
    ("Mehriz","مهریز",31.5833,54.4333),("Chadegan","چادگان",32.7683,50.6278),

    ("Shiraz","شیراز",29.5918,52.5837),("Marvdasht","مرودشت",29.8739,52.8025),
    ("Jahrom","جهرم",28.5000,53.5600),("Fasa","فسا",28.9389,53.6481),
    ("Kazerun","کازرون",29.6158,51.6553),("Lar","لار",27.6833,54.3417),
    ("Abadeh","آباده",31.1606,52.6519),("Darab","داراب",28.7519,54.5406),
    ("Neyriz","نی‌ریز",29.1981,54.3281),("Sepidan","سپیدان",30.2333,51.9833),
    ("Eqlid","اقلید",30.8978,52.6867),("Estahban","استهبان",29.1278,54.0417),
    ("Zarqan","زرقان",29.7711,52.7233),("Kharameh","خرامه",29.5000,53.3167),
    ("Pasargad","پاسارگاد",30.2000,53.1833),("Bavanat","بوانات",30.4667,53.6500),
    ("Arsanjan","ارسنجان",29.9167,53.3167),("Mamasani","ممسنی",30.1000,51.5333),
    ("Mohr","مهر",27.5500,52.8833),("Lamerd","لامرد",27.3333,53.1833),
    ("Qir","قیر",28.3333,53.0333),("Farashband","فراشبند",28.8667,52.0833),
    ("Sadra","صدرا",29.7833,52.4667),("Safashahr","صفاشهر",30.6167,53.6500),

    ("Tabriz","تبریز",38.0962,46.2738),("Maragheh","مراغه",37.3892,46.2382),
    ("Marand","مرند",38.4320,45.7700),("Ahar","اهر",38.4775,47.0662),
    ("Mianeh","میانه",37.4211,47.7100),("Bonab","بناب",38.4283,45.9087),
    ("Sarab","سراب",36.3175,47.5648),("Azarshahr","آذرشهر",37.7585,45.9794),
    ("Osku","اسکو",37.9161,46.1245),("Shabestar","شبستر",38.1784,45.7002),
    ("Jolfa","جلفا",38.9376,45.6252),("Sardrud","سردرود",38.0314,46.1475),
    ("Ajab Shir","عجب‌شیر",37.4828,45.8936),("Bostanabad","بستان‌آباد",37.8500,46.8333),
    ("Hashtrud","هشترود",37.4833,47.0500),("Heris","هریس",38.2500,47.1167),
    ("Malekan","ملکان",37.1333,46.1000),("Varzaqan","ورزقان",38.5167,46.6500),
    ("Kaleybar","کلیبر",38.8667,47.0333),("Basmenj","باسمنج",37.9833,46.4667),
    ("Ilkhchi","ایلخچی",37.9333,45.9833),("Sahand","سهند",37.9500,46.1167),
    ("Torkamanchay","ترکمانچای",37.5833,47.3833),

    ("Urmia","ارومیه",37.5553,45.0725),("Khoy","خوی",38.5500,44.9500),
    ("Mahabad","مهاباد",36.7639,45.7222),("Bukan","بوکان",36.5210,46.2100),
    ("Miandoab","میاندوآب",36.9667,46.1000),("Salmas","سلماس",38.1972,44.7683),
    ("Naqadeh","نقده",36.9553,45.3881),("Piranshahr","پیرانشهر",36.7000,45.1500),
    ("Sardasht","سردشت",36.1580,45.4752),("Shahin Dezh","شاهین‌دژ",36.6769,46.5667),
    ("Takab","تکاب",36.4008,47.1125),("Maku","ماکو",39.2956,44.5117),
    ("Chaldoran","چالدران",39.0642,44.3833),("Oshnavieh","اشنویه",37.0397,45.0983),
    ("Chaypareh","چایپاره",38.8333,45.0333),("Poldasht","پلدشت",39.3483,45.0711),
    ("Showt","شوط",39.2167,44.7667),("Keshavarz","کشاورز",37.1167,46.1667),
    ("Mohammadyar","محمدیار",37.0833,45.5000),("Firuraq","فیرورق",38.6000,44.8500),

    ("Ahvaz","اهواز",31.3167,48.6844),("Abadan","آبادان",30.3392,48.3043),
    ("Khorramshahr","خرمشهر",30.4408,48.1845),("Dezful","دزفول",32.3830,48.4058),
    ("Andimeshk","اندیمشک",32.4610,48.3534),("Shushtar","شوشتر",32.0452,48.8540),
    ("Behbahan","بهبهان",30.5959,50.2417),("Izeh","ایذه",31.8300,49.8667),
    ("Masjed Soleyman","مسجدسلیمان",31.9353,49.3039),("Shush","شوش",32.1944,48.2572),
    ("Bandar Mahshahr","بندر ماهشهر",30.5589,49.1981),("Omidiyeh","امیدیه",30.7622,49.7025),
    ("Ramhormoz","رامهرمز",31.2792,49.6022),("Susangerd","سوسنگرد",31.5619,48.1819),
    ("Hendijan","هندیجان",30.2342,49.7139),("DehdeZ","دهدز",31.7083,50.2856),
    ("Shadegan","شادگان",30.6500,48.6667),("Hoveyzeh","هویزه",31.4667,48.0833),
    ("Haftgel","هفتکل",31.4667,49.5500),("Aghajari","آغاجاری",30.7000,49.8333),
    ("Gotvand","گتوند",32.2500,48.8167),("Lali","لالی",32.3333,49.1000),

    ("Rasht","رشت",37.2808,49.5832),("Bandar Anzali","بندر انزلی",37.4739,49.4578),
    ("Lahijan","لاهیجان",37.2019,50.0047),("Langarud","لنگرود",37.1956,50.1525),
    ("Astaneh-ye Ashrafiyeh","آستانه اشرفیه",37.2611,49.9431),("Talesh","تالش",37.8008,48.9058),
    ("Rudsar","رودسر",37.1364,50.2931),("Fuman","فومن",37.2239,49.3119),
    ("Sowme'eh Sara","صومعه‌سرا",37.3114,49.3206),("Rudbar","رودبار",36.8236,49.4278),
    ("Astara","آستارا",38.4222,48.8722),("Masal","ماسال",37.3631,49.1325),
    ("Rezvanshahr","رضوانشهر",37.5514,49.1372),("Amlash","املش",37.1000,50.1833),
    ("Khomam","خمام",37.4000,49.6333),("Lasht-e Nesha","لشت نشا",37.3667,49.8500),
    ("Kuchesfahan","کوچصفهان",37.2833,49.7667),("Chaboksar","چابکسر",36.9667,50.5667),
    ("Manjil","منجیل",36.7442,49.4019),("Lowshan","لوشان",36.6333,49.5167),
    ("Rostamabad","رستم‌آباد",36.9000,49.4833),("Pareh Sar","پره‌سر",37.6000,49.0667),
    ("Asalem","اسالم",37.7333,48.9333),("Hashtpar","هشتپر",37.8008,48.9058),

    ("Sari","ساری",36.5633,53.0601),("Babol","بابل",36.5513,52.6789),
    ("Amol","آمل",36.4696,52.3506),("Qaem Shahr","قائم‌شهر",36.4633,52.8600),
    ("Behshahr","بهشهر",36.6925,53.5522),("Chalus","چالوس",36.6550,51.4200),
    ("Noshahr","نوشهر",36.6489,51.4964),("Ramsar","رامسر",36.9228,50.6539),
    ("Tonekabon","تنکابن",36.8167,50.8761),("Babolsar","بابلسر",36.7025,52.6500),
    ("Fereydunkenar","فریدونکنار",36.6856,52.5164),("Juybar","جویبار",36.6419,52.9122),
    ("Neka","نکا",36.6528,53.2961),("Galugah","گلوگاه",36.7219,53.8083),
    ("Savadkuh","سوادکوه",36.1900,52.8600),("Pol-e Sefid","پل سفید",36.1167,52.9167),
    ("Zirab","زیرآب",36.1833,52.9667),("Shirgah","شیرگاه",36.2833,52.8833),
    ("Kiasar","کیاسر",36.2333,53.5333),("Mahmudabad","محمودآباد",36.6333,52.2667),
    ("Nur","نور",36.5667,52.0167),("Kelardasht","کلاردشت",36.4833,51.1333),
    ("Marzanabad","مرزن‌آباد",36.4500,51.3000),("Salman Shahr","سلمان‌شهر",36.7000,51.1833),
    ("Nashtarud","نشتارود",36.7500,51.0000),("Abbasabad","عباس‌آباد",36.7167,51.1000),
    ("Rineh","رینه",35.8833,52.0500),("Polur","پلور",35.8333,52.0500),

    ("Kerman","کرمان",30.2839,57.0834),("Bam","بم",29.1060,58.3570),
    ("Rafsanjan","رفسنجان",30.4067,55.9939),("Sirjan","سیرجان",29.4522,55.6794),
    ("Jiroft","جیرفت",28.6744,57.7436),("Zarand","زرند",30.8128,56.5619),
    ("Shahr-e Babak","شهربابک",30.1147,55.1172),("Kahnuj","کهنوج",27.9522,57.7006),
    ("Bardsir","بردسیر",29.9256,56.5739),("Ravar","راور",31.2647,56.8050),
    ("Anar","انار",30.8667,55.2667),("Manujan","منوجان",27.4000,57.5000),
    ("Qaleh Ganj","قلعه گنج",27.5333,57.8833),("Fahraj","فهرج",28.9500,58.8833),
    ("Rigan","ریگان",28.7500,59.0333),("Narmashir","نرماشیر",28.9500,58.6833),
    ("Faryab","فاریاب",28.1167,57.2333),("Arzuiyeh","ارزوئیه",28.4500,56.3667),
    ("Kuhbanan","کوهبنان",31.4167,56.2833),("Rabor","رابر",29.2833,56.9167),
    ("Rayen","راین",29.6000,57.4333),("Mahan","ماهان",30.0500,57.2833),
    ("Shahdad","شهداد",30.4167,57.7000),("Golbaf","گلباف",29.8833,57.7333),
    ("Chatrud","چترود",30.6000,56.9167),("Baghin","باغین",30.1833,56.8000),

    ("Bojnord","بجنورد",37.4747,57.3290),("Shirvan","شیروان",37.3975,57.9300),
    ("Esfarayen","اسفراین",37.0764,57.5100),("Birjand","بیرجند",32.8663,59.2211),
    ("Qaen","قائن",33.7258,59.1858),("Ferdows","فردوس",34.0175,58.1700),
    ("Tabas","طبس",33.5956,56.9244),("Jajrom","جاجرم",37.0333,56.3833),
    ("Garmeh","گرمه",37.0167,56.2833),("Nehbandan","نهبندان",31.5333,60.0333),
    ("Sarbisheh","سربیشه",32.5833,59.8000),("Darmian","درمیان",32.9500,60.0333),
    ("Khusf","خوسف",32.7833,58.8833),("Boshruyeh","بشرویه",33.8667,57.4167),
    ("Sarayan","سرایان",33.8667,58.5167),("Zirkuh","زیرکوه",33.5833,60.0333),

    ("Kermanshah","کرمانشاه",34.3142,47.0650),("Eslamabad-e Gharb","اسلام‌آباد غرب",34.1089,46.5308),
    ("Harsin","هرسین",34.2736,47.5861),("Sonqor","سنقر",34.7839,47.5986),
    ("Paveh","پاوه",35.0456,46.3575),("Sanandaj","سنندج",35.3149,46.9921),
    ("Saqqez","سقز",36.2498,46.2735),("Marivan","مریوان",35.5239,46.1761),
    ("Baneh","بانه",35.9989,45.8867),("Qorveh","قروه",35.1664,47.8047),
    ("Bijar","بیجار",35.8728,47.6025),("Kangavar","کنگاور",34.5000,47.9667),
    ("Sahneh","صحنه",34.4833,47.6833),("Gilan-e Gharb","گیلان غرب",34.1333,45.9167),
    ("Ravansar","روانسر",34.7167,46.6500),("Javanrud","جوانرود",34.8000,46.5000),
    ("Divandarreh","دیواندره",35.9167,47.0333),("Kamyaran","کامیاران",34.8000,46.9333),
    ("Dehgolan","دهگلان",35.2833,47.4167),("Sarvabad","سروآباد",35.3167,46.3667),

    ("Qom","قم",34.6416,50.8746),("Ardabil","اردبیل",38.2494,48.3014),
    ("Parsabad","پارس‌آباد",39.6489,47.9164),("Khalkhal","خلخال",37.6192,48.5253),
    ("Meshgin Shahr","مشگین‌شهر",38.3986,47.6806),("Germi","گرمی",39.0333,48.0833),
    ("Bileh Savar","بیله سوار",39.3833,48.3500),("Nir","نیر",38.0333,47.9833),
    ("Kowsar","کوثر",37.7167,48.2667),("Sareyn","سرعین",38.1500,48.0667),
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
    ("Ardal","اردل",32.0000,50.6500),("Farrokhshahr","فرخ‌شهر",32.2833,50.9833),
    ("Hafshejan","هفشجان",32.1833,50.8333),("Gandoman","گندمان",31.8667,51.1333),
    ("Boldaji","بلداجی",31.9333,51.0500),("Nafch","نافچ",32.4167,50.8000),
    ("Shalamzar","شلمزار",32.0500,50.8167),("Junqan","جونقان",32.1500,50.6833),
]

PERSIAN_MAP = {c[1]: c[0] for c in IRAN}

# ==================== ترجمه آب‌وهوا ====================
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
    95: {"fa":"رعد و برق","en":"Thunder","ic":"⛈️"},96: {"fa":"رعد و تگرگ","en":"Thunder+hail","ic":"⛈️"},
    99: {"fa":"طوفان شدید","en":"Severe","ic":"⛈️"},
}


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
        (20,{"lbl":"خوب" if lang=="fa" else "Good","ic":"🟢",
             "ad":"هوا پاکه، برو بیرون لذت ببر 😎" if lang=="fa" else "Air is clean! Enjoy outdoors 😎"}),
        (40,{"lbl":"قابل قبول" if lang=="fa" else "Fair","ic":"🟡",
             "ad":"کیفیت هوا قابل قبوله" if lang=="fa" else "Air quality is acceptable"}),
        (60,{"lbl":"متوسط" if lang=="fa" else "Moderate","ic":"🟠",
             "ad":"گروه‌های حساس احتیاط کنن" if lang=="fa" else "Sensitive groups be careful"}),
        (80,{"lbl":"ضعیف" if lang=="fa" else "Poor","ic":"🔴",
             "ad":"ماسک بزن و فعالیت بیرون رو کم کن" if lang=="fa" else "Wear mask, reduce outdoor activity"}),
        (100,{"lbl":"خیلی ضعیف" if lang=="fa" else "Very Poor","ic":"🟣",
              "ad":"از خونه بیرون نرو! ماسک بزن" if lang=="fa" else "Don't go out! Wear mask"}),
        (9999,{"lbl":"خطرناک" if lang=="fa" else "Hazardous","ic":"⚫",
               "ad":"🚨 در خونه بمون! پنجره‌ها رو ببند" if lang=="fa" else "🚨 Stay indoors! Close windows"}),
    ]
    for t, i in levels:
        if v <= t: return i
    return levels[-1][1]


def pol_lvl(v, k):
    if v is None: return "⚪"
    th = {"pm2_5":[10,25,50,75,100],"pm10":[20,50,100,150,200],"no2":[40,90,120,230,340],
          "so2":[20,80,250,350,500],"o3":[60,100,140,180,240],"co":[4000,10000,20000,35000,50000]}.get(k,[50,100,150,200,300])
    for i, t in enumerate(th):
        if v <= t: return ["🟢","🟡","🟠","🔴","🟣"][i]
    return "⚫"


def fmt_t(c, u): return f"{round(c*9/5+32)}°F" if u=="F" else f"{round(c)}°C"


def smart_tips(w, a, lang):
    t = []
    c = w["current"]; d = w["daily"]
    feels = c["apparent_temperature"]
    uv = c.get("uv_index", 0) or 0
    rp = d["precipitation_probability_max"][1] if len(d.get("precipitation_probability_max", [])) > 1 else 0
    if lang == "fa":
        if feels >= 42: t.append("🥵 گرمای شدید! آب زیاد بخور")
        elif feels >= 32: t.append("☀️ هوا گرمه، ضدآفتاب بزن")
        elif feels <= 0: t.append("🥶 سرده! لباس گرم بپوش")
        elif feels <= 10: t.append("🧥 کاپشن بردار")
        if rp >= 60: t.append(f"☔ احتمال باران {rp}% — چتر یادت نره")
        elif rp >= 30: t.append(f"🌦️ احتمال باران {rp}%")
        if uv >= 8: t.append("🕶️ UV بالا، عینک آفتابی بزن")
        elif uv >= 6: t.append("🕶️ ضدآفتاب بزن")
        if a and a.get("european_aqi", 0) >= 60: t.append(f"😷 کیفیت هوا {a['european_aqi']} — ماسک بزن")
        if c["wind_speed_10m"] >= 40: t.append("💨 باد شدید، مراقب باش")
    else:
        if feels >= 42: t.append("🥵 Extreme heat! Drink water")
        elif feels >= 32: t.append("☀️ Hot! Wear sunscreen")
        elif feels <= 0: t.append("🥶 Freezing! Wear warm clothes")
        elif feels <= 10: t.append("🧥 Chilly, take a jacket")
        if rp >= 60: t.append(f"☔ Rain {rp}% — take umbrella!")
        elif rp >= 30: t.append(f"🌦️ Rain chance {rp}%")
        if uv >= 8: t.append("🕶️ High UV! Sunscreen + sunglasses")
        elif uv >= 6: t.append("🕶️ Wear sunscreen")
        if a and a.get("european_aqi", 0) >= 60: t.append(f"😷 AQI {a['european_aqi']} — wear mask")
        if c["wind_speed_10m"] >= 40: t.append("💨 Strong wind, be careful")
    return t[:3]


# ==================== API ====================
async def geocode(q, lang="fa"):
    key = q.strip().lower()
    c = _cget(_geo_cache, _geo_t, key, 86400)
    if c: return c
    qn = PERSIAN_MAP.get(q.strip(), q.strip())
    try:
        cl = await get_http()
        r = await cl.get(GEO_API, params={"name": qn, "count": 5, "language": lang, "format": "json"})
        r.raise_for_status()
        res = r.json().get("results", [])
        if not res: return None
        res.sort(key=lambda x: 0 if x.get("country_code") == "IR" else 1)
        out = res[0]
        _geo_cache[key] = out; _geo_t[key] = time.time()
        return out
    except Exception as e:
        logger.error(f"geocode: {e}"); return None


async def f_weather(la, lo):
    p = {"latitude": la, "longitude": lo,
         "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,visibility,dew_point_2m,uv_index",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
         "timezone": "auto", "forecast_days": 7, "past_days": 1}
    cl = await get_http()
    r = await cl.get(WEATHER_API, params=p)
    r.raise_for_status()
    return r.json()


async def f_aqi(la, lo):
    p = {"latitude": la, "longitude": lo,
         "current": "european_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,uv_index",
         "timezone": "auto"}
    try:
        cl = await get_http()
        r = await cl.get(AQI_API, params=p)
        r.raise_for_status()
        return r.json().get("current")
    except Exception: return None


async def fetch_all(la, lo):
    k = f"{la:.3f},{lo:.3f}"
    c = _w_cache.get(k)
    if c and time.time() - c["t"] < TTL:
        return c["w"], c["a"]
    w, a = await asyncio.gather(f_weather(la, lo), f_aqi(la, lo), return_exceptions=True)
    if isinstance(w, Exception): w = None
    if isinstance(a, Exception): a = None
    if w: _w_cache[k] = {"w": w, "a": a, "t": time.time()}
    return w, a


# ==================== ساخت پیام ====================
def build_w(city, w, a, lang, unit):
    c = w["current"]; d = w["daily"]
    code = c["weather_code"]; wm = WMO.get(code, {"fa":"نامشخص","en":"Unknown","ic":"❓"})
    cn = (city.get("name_fa") or city["name"]) if lang == "fa" else city["name"]
    desc = wm["fa"] if lang == "fa" else wm["en"]
    ic = wm["ic"]
    tmp = fmt_t(c["temperature_2m"], unit); fls = fmt_t(c["apparent_temperature"], unit)
    wnd = round(c["wind_speed_10m"]); wf = wdir_fa(c["wind_direction_10m"]); we = wdir_en(c["wind_direction_10m"])
    hum = c["relative_humidity_2m"]; prs = round(c["pressure_msl"])
    vis = c.get("visibility"); uv = c.get("uv_index"); dw = c.get("dew_point_2m")
    sr = ss = "—"
    try:
        if d.get("sunrise"): sr = datetime.fromisoformat(d["sunrise"][1]).strftime("%H:%M")
        if d.get("sunset"): ss = datetime.fromisoformat(d["sunset"][1]).strftime("%H:%M")
    except Exception: pass

    L = []
    if lang == "fa":
        L.append(f"{ic} <b>{cn}</b>")
        L.append(f"<i>{desc}</i>")
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  حس واقعی: <b>{fls}</b>")
        try:
            if len(d.get("temperature_2m_max", [])) >= 2:
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
        L.append(f"<code>{bar(wnd,100)}</code>")
        L.append(f"🔽 فشار: <b>{prs}</b> hPa")
        if vis: L.append(f"👁️ دید: <b>{round(vis/1000,1)}</b> km")
        if dw is not None: L.append(f"💦 شبنم: <b>{fmt_t(dw,unit)}</b>")
        if uv is not None:
            lb, ci = uv_lvl(uv, "fa")
            L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
        L.append(f"🌅 طلوع: <b>{sr}</b>")
        L.append(f"🌇 غروب: <b>{ss}</b>")
        if a and a.get("european_aqi") is not None:
            av = a["european_aqi"]; info = aqi_i(av, "fa")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ کیفیت هوا: {info['ic']} <b>{info['lbl']}</b>  •  AQI: <b>{av}</b>")
            L.append(f"<code>{bar(av,100)}</code>")
        tips = smart_tips(w, a, "fa")
        if tips:
            L.append("")
            L.append("💡 <b>توصیه هوشمند:</b>")
            for t in tips: L.append(f"  {t}")
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append("📅 <b>پیش‌بینی ۳ روز آینده:</b>")
        L.append("")
        days = ["دوشنبه","سه‌شنبه","چهارشنبه","پنج‌شنبه","جمعه","شنبه","یکشنبه"]
        for i in range(1, min(4, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"fa":"؟","ic":"❓"})
            dt = datetime.fromisoformat(d["time"][i])
            L.append(f"{wc['ic']} <b>{days[dt.weekday()]}</b> ({dt.strftime('%m/%d')})")
            L.append(f"   🌡️ {fmt_t(d['temperature_2m_min'][i],unit)} ~ {fmt_t(d['temperature_2m_max'][i],unit)}  💧 {d['precipitation_probability_max'][i]}%")
        L.append("")
        L.append("<i>📡 داده‌ها از Open-Meteo</i>")
    else:
        L.append(f"{ic} <b>{cn}</b>")
        L.append(f"<i>{desc}</i>")
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  Feels like: <b>{fls}</b>")
        try:
            if len(d.get("temperature_2m_max", [])) >= 2:
                diff = d["temperature_2m_max"][1] - d["temperature_2m_max"][0]
                if abs(diff) >= 1:
                    ar = "📈" if diff > 0 else "📉"
                    L.append(f"{ar} Today is <b>{round(abs(diff))}°</b> {'warmer' if diff>0 else 'colder'} than yesterday")
        except Exception: pass
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 Humidity: <b>{hum}%</b>")
        L.append(f"<code>{bar(hum,100)}</code>")
        L.append(f"💨 Wind: <b>{wnd} km/h</b> ({we})")
        L.append(f"<code>{bar(wnd,100)}</code>")
        L.append(f"🔽 Pressure: <b>{prs}</b> hPa")
        if vis: L.append(f"👁️ Visibility: <b>{round(vis/1000,1)}</b> km")
        if dw is not None: L.append(f"💦 Dew point: <b>{fmt_t(dw,unit)}</b>")
        if uv is not None:
            lb, ci = uv_lvl(uv, "en")
            L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")
        L.append(f"🌅 Sunrise: <b>{sr}</b>")
        L.append(f"🌇 Sunset: <b>{ss}</b>")
        if a and a.get("european_aqi") is not None:
            av = a["european_aqi"]; info = aqi_i(av, "en")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ Air Quality: {info['ic']} <b>{info['lbl']}</b>  •  AQI: <b>{av}</b>")
            L.append(f"<code>{bar(av,100)}</code>")
        tips = smart_tips(w, a, "en")
        if tips:
            L.append("")
            L.append("💡 <b>Smart Advice:</b>")
            for t in tips: L.append(f"  {t}")
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append("📅 <b>3-day forecast:</b>")
        L.append("")
        for i in range(1, min(4, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"en":"?","ic":"❓"})
            dt = datetime.fromisoformat(d["time"][i])
            L.append(f"{wc['ic']} <b>{dt.strftime('%A')}</b> ({dt.strftime('%m/%d')})")
            L.append(f"   🌡️ {fmt_t(d['temperature_2m_min'][i],unit)} ~ {fmt_t(d['temperature_2m_max'][i],unit)}  💧 {d['precipitation_probability_max'][i]}%")
        L.append("")
        L.append("<i>📡 Data from Open-Meteo</i>")
    return "\n".join(L)


# ==================== ارسال‌ها ====================
async def snd_weather(chat, uid, q, u, reply_to=None):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await (reply_to.reply_text(load) if reply_to else chat.send_message(load))
    try:
        city = await geocode(q, u["lang"])
        if not city:
            err = "❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ City not found"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        la, lo = city["latitude"], city["longitude"]
        w, a = await fetch_all(la, lo)
        if not w:
            err = "⚠️ خطا در دریافت داده" if u["lang"]=="fa" else "⚠️ Failed to fetch"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        text = build_w(city, w, a, u["lang"], u["unit"])
        kb = []
        if WEB_APP_URL:
            kb.append([InlineKeyboardButton(
                "🌐 وب‌سایت کامل" if u["lang"]=="fa" else "🌐 Full Website", url=WEB_APP_URL)])
        if kb:
            await msg.edit_text(text, parse_mode=ParseMode.HTML,
                               reply_markup=InlineKeyboardMarkup(kb))
        else:
            await msg.edit_text(text, parse_mode=ParseMode.HTML)
        # ارسال کیبورد پایین برای آپدیت خودکار
        await chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
        stat("weather_requests")
    except Exception as e:
        logger.exception("snd_weather")
        log_err(uid, e, "snd_weather")
        try: await msg.edit_text(f"❌ {str(e)[:80]}", reply_markup=kb_main(u["lang"]))
        except Exception: pass


async def snd_aqi(chat, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await chat.send_message(load)
    try:
        city = await geocode(q, u["lang"])
        if not city:
            err = "❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ Not found"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        _, a = await fetch_all(city["latitude"], city["longitude"])
        if not a or a.get("european_aqi") is None:
            err = "⚠️ داده کیفیت هوا موجود نیست" if u["lang"]=="fa" else "⚠️ AQI data not available"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        v = a["european_aqi"]; info = aqi_i(v, u["lang"])
        cn = (city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]

        if u["lang"] == "fa":
            L = [f"🌫️ <b>کیفیت هوای {cn}</b>", "",
                 f"{info['ic']} وضعیت: <b>{info['lbl']}</b>",
                 f"📊 شاخص AQI: <b>{v}</b>",
                 f"<code>{bar(v,100)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━",
                 "🔬 <b>آلاینده‌ها:</b>", ""]
        else:
            L = [f"🌫️ <b>Air Quality — {cn}</b>", "",
                 f"{info['ic']} Status: <b>{info['lbl']}</b>",
                 f"📊 AQI: <b>{v}</b>",
                 f"<code>{bar(v,100)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━",
                 "🔬 <b>Pollutants:</b>", ""]

        labels_fa = [("pm2_5","PM2.5 (ذرات ریز)","pm2_5"),("pm10","PM10 (ذرات معلق)","pm10"),
                     ("no2","NO₂ (دی‌اکسید نیتروژن)","no2"),("so2","SO₂ (دی‌اکسید گوگرد)","so2"),
                     ("o3","O₃ (ازن)","o3"),("co","CO (مونوکسید کربن)","co")]
        labels_en = [("pm2_5","PM2.5","pm2_5"),("pm10","PM10","pm10"),
                     ("no2","NO₂","no2"),("so2","SO₂","so2"),
                     ("o3","O₃","o3"),("co","CO","co")]
        labels = labels_fa if u["lang"]=="fa" else labels_en
        for k, n, kk in labels:
            vv = a.get(kk)
            if vv is not None:
                L.append(f"{pol_lvl(vv,k)} <b>{n}</b>: {round(vv)} µg/m³")

        uv = a.get("uv_index")
        if uv is not None:
            lb, ci = uv_lvl(uv, u["lang"])
            L.append(""); L.append(f"☀️ UV: {ci} <b>{lb}</b> ({uv:.1f})")

        L += ["", "━━━━━━━━━━━━━━━━━━━━",
              f"💡 <b>{'توصیه سلامتی' if u['lang']=='fa' else 'Health Advice'}:</b>",
              f"  {info['ad']}", "",
              f"<i>📡 {'داده‌ها از Open-Meteo' if u['lang']=='fa' else 'Data from Open-Meteo'}</i>"]

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        # ارسال کیبورد پایین برای آپدیت خودکار
        await chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
        stat("aqi_requests")
    except Exception as e:
        log_err(uid, e, "snd_aqi")
        try: await msg.edit_text(f"❌ {str(e)[:80]}", reply_markup=kb_main(u["lang"]))
        except Exception: pass


async def snd_forecast(chat, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await chat.send_message(load)
    try:
        city = await geocode(q, u["lang"])
        if not city:
            err = "❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ Not found"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        w, _ = await fetch_all(city["latitude"], city["longitude"])
        if not w:
            err = "⚠️ خطا در دریافت داده" if u["lang"]=="fa" else "⚠️ Failed"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        d = w["daily"]
        cn = (city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]

        if u["lang"] == "fa":
            L = [f"📅 <b>پیش‌بینی ۷ روزه {cn}</b>", ""]
            days = ["دوشنبه","سه‌شنبه","چهارشنبه","پنج‌شنبه","جمعه","شنبه","یکشنبه"]
        else:
            L = [f"📅 <b>7-day forecast — {cn}</b>", ""]
            days = None

        for i in range(1, min(8, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"fa":"؟","en":"?","ic":"❓"})
            dt = datetime.fromisoformat(d["time"][i])
            dn = days[dt.weekday()] if days else dt.strftime("%A")
            desc = wc["fa"] if u["lang"]=="fa" else wc["en"]
            L.append(f"{wc['ic']} <b>{dn}</b> ({dt.strftime('%m/%d')})")
            L.append(f"   🌡️ {fmt_t(d['temperature_2m_min'][i],u['unit'])} ~ {fmt_t(d['temperature_2m_max'][i],u['unit'])}")
            L.append(f"   💧 {d['precipitation_probability_max'][i]}%  •  {desc}")
            L.append("")

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        # ارسال کیبورد پایین برای آپدیت خودکار
        await chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
        stat("forecast_requests")
    except Exception as e:
        log_err(uid, e, "snd_forecast")
        try: await msg.edit_text(f"❌ {str(e)[:80]}", reply_markup=kb_main(u["lang"]))
        except Exception: pass


async def snd_compare(chat, uid, cities, u):
    if len(cities) < 2:
        msg = ("مثال: <code>/compare تهران مشهد</code>" if u["lang"]=="fa"
               else "Example: <code>/compare Tehran Mashhad</code>")
        await chat.send_message(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    load = "⏳ در حال مقایسه..." if u["lang"]=="fa" else "⏳ Comparing..."
    msg = await chat.send_message(load)
    try:
        c1 = await geocode(cities[0], u["lang"]); c2 = await geocode(cities[1], u["lang"])
        if not c1 or not c2:
            err = "❌ یکی از شهرها پیدا نشد" if u["lang"]=="fa" else "❌ One city not found"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        (w1, a1), (w2, a2) = await asyncio.gather(
            fetch_all(c1["latitude"], c1["longitude"]),
            fetch_all(c2["latitude"], c2["longitude"]))
        if not w1 or not w2:
            err = "⚠️ خطا" if u["lang"]=="fa" else "⚠️ Error"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        def nm(c): return (c.get("name_fa") or c["name"]) if u["lang"]=="fa" else c["name"]
        t1, t2 = w1["current"]["temperature_2m"], w2["current"]["temperature_2m"]
        diff = t1 - t2

        if u["lang"] == "fa":
            L = [f"⚖️ <b>مقایسه دمای دو شهر</b>", "",
                 f"📍 <b>{nm(c1)}</b>",
                 f"  🌡️ دما: <b>{fmt_t(t1, u['unit'])}</b>",
                 f"  💧 رطوبت: {w1['current']['relative_humidity_2m']}%",
                 f"  💨 باد: {round(w1['current']['wind_speed_10m'])} km/h"]
            if a1 and a1.get("european_aqi"):
                info1 = aqi_i(a1["european_aqi"], "fa")
                L.append(f"  🌫️ AQI: {info1['ic']} {a1['european_aqi']} ({info1['lbl']})")
            L += ["", f"📍 <b>{nm(c2)}</b>",
                  f"  🌡️ دما: <b>{fmt_t(t2, u['unit'])}</b>",
                  f"  💧 رطوبت: {w2['current']['relative_humidity_2m']}%",
                  f"  💨 باد: {round(w2['current']['wind_speed_10m'])} km/h"]
            if a2 and a2.get("european_aqi"):
                info2 = aqi_i(a2["european_aqi"], "fa")
                L.append(f"  🌫️ AQI: {info2['ic']} {a2['european_aqi']} ({info2['lbl']})")
            L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
            if abs(diff) < 0.5:
                L.append("🤝 دمای هر دو شهر تقریباً یکسانه")
            elif diff > 0:
                L.append(f"📈 <b>{nm(c1)}</b> حدود <b>{round(abs(diff))}°</b> گرم‌تر از <b>{nm(c2)}</b>")
            else:
                L.append(f"📈 <b>{nm(c2)}</b> حدود <b>{round(abs(diff))}°</b> گرم‌تر از <b>{nm(c1)}</b>")
        else:
            L = [f"⚖️ <b>Compare City Temperatures</b>", "",
                 f"📍 <b>{nm(c1)}</b>",
                 f"  🌡️ Temp: <b>{fmt_t(t1, u['unit'])}</b>",
                 f"  💧 Humidity: {w1['current']['relative_humidity_2m']}%",
                 f"  💨 Wind: {round(w1['current']['wind_speed_10m'])} km/h"]
            if a1 and a1.get("european_aqi"):
                info1 = aqi_i(a1["european_aqi"], "en")
                L.append(f"  🌫️ AQI: {info1['ic']} {a1['european_aqi']} ({info1['lbl']})")
            L += ["", f"📍 <b>{nm(c2)}</b>",
                  f"  🌡️ Temp: <b>{fmt_t(t2, u['unit'])}</b>",
                  f"  💧 Humidity: {w2['current']['relative_humidity_2m']}%",
                  f"  💨 Wind: {round(w2['current']['wind_speed_10m'])} km/h"]
            if a2 and a2.get("european_aqi"):
                info2 = aqi_i(a2["european_aqi"], "en")
                L.append(f"  🌫️ AQI: {info2['ic']} {a2['european_aqi']} ({info2['lbl']})")
            L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
            if abs(diff) < 0.5:
                L.append("🤝 Similar temperature")
            elif diff > 0:
                L.append(f"📈 <b>{nm(c1)}</b> is <b>{round(abs(diff))}°</b> warmer than <b>{nm(c2)}</b>")
            else:
                L.append(f"📈 <b>{nm(c2)}</b> is <b>{round(abs(diff))}°</b> warmer than <b>{nm(c1)}</b>")

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        # ارسال کیبورد پایین برای آپدیت خودکار
        await chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
        stat("compare_requests")
    except Exception as e:
        log_err(uid, e, "snd_compare")
        try: await msg.edit_text(f"❌ {str(e)[:80]}", reply_markup=kb_main(u["lang"]))
        except Exception: pass


# ==================== کیبورد پایین ====================
def kb_main(lang):
    if lang == "fa":
        rows = [[KeyboardButton("🌤️ آب‌وهوا"), KeyboardButton("🌫️ کیفیت هوا")],
                [KeyboardButton("📅 پیش‌بینی"), KeyboardButton("🎛️ منو")],
                [KeyboardButton("🔐 پنل مدیریت")]]
        ph = "اسم شهر رو بنویس..."
    else:
        rows = [[KeyboardButton("🌤️ Weather"), KeyboardButton("🌫️ Air Quality")],
                [KeyboardButton("📅 Forecast"), KeyboardButton("🎛️ Menu")],
                [KeyboardButton("🔐 Admin Panel")]]
        ph = "Type a city name..."
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, input_field_placeholder=ph)


# ==================== منو ====================
def build_menu(u):
    s = get_user(u["user_id"])
    lang = u["lang"]
    nst = "✅ روشن" if s.get("notify_enabled") else "❌ خاموش"
    nst_en = "ON ✅" if s.get("notify_enabled") else "OFF ❌"
    has_default = s.get("default_city") or ("تنظیم نشده" if lang == "fa" else "not set")

    if lang == "fa":
        txt = (f"🎛️ <b>منوی ربات</b>\n\n"
               f"🏙️ شهر پیش‌فرض: <b>{has_default}</b>\n"
               f"🔔 اعلان روزانه: <b>{nst}</b>\n"
               f"⏰ ساعت اعلان: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ واحد دما: <b>{s['unit']}</b>\n"
               f"🌐 زبان: <b>فارسی</b>")
        rows = [
            [InlineKeyboardButton("🏙️ تغییر شهر پیش‌فرض", callback_data="m:def")],
            [InlineKeyboardButton("⚖️ مقایسه دمای دو شهر", callback_data="m:cmp")],
            [InlineKeyboardButton(f"🔔 اعلان روزانه ({nst})", callback_data="m:ntf")],
            [InlineKeyboardButton("⏰ تغییر ساعت اعلان", callback_data="m:tim")],
            [InlineKeyboardButton("🌐 زبان", callback_data="m:lng"),
             InlineKeyboardButton("🌡️ واحد", callback_data="m:unt")],
        ]
    else:
        txt = (f"🎛️ <b>Bot Menu</b>\n\n"
               f"🏙️ Default city: <b>{has_default}</b>\n"
               f"🔔 Daily notify: <b>{nst_en}</b>\n"
               f"⏰ Notify time: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ Temp unit: <b>{s['unit']}</b>\n"
               f"🌐 Language: <b>English</b>")
        rows = [
            [InlineKeyboardButton("🏙️ Change Default City", callback_data="m:def")],
            [InlineKeyboardButton("⚖️ Compare City Temperatures", callback_data="m:cmp")],
            [InlineKeyboardButton(f"🔔 Daily Notify ({nst_en})", callback_data="m:ntf")],
            [InlineKeyboardButton("⏰ Change Notify Time", callback_data="m:tim")],
            [InlineKeyboardButton("🌐 Language", callback_data="m:lng"),
             InlineKeyboardButton("🌡️ Unit", callback_data="m:unt")],
        ]
    return txt, rows


# ==================== پنل ادمین ====================
def build_admin_panel(u):
    lang = u["lang"]
    s = all_stats()
    total = total_users()
    active = active_users_24h()
    errs = recent_errors(3)
    tops = top_cities(5)

    if lang == "fa":
        txt = (f"🔐 <b>پنل مدیریت</b>\n\n"
               f"📊 <b>آمار کلی:</b>\n"
               f"  👥 کل کاربران: <b>{total}</b>\n"
               f"  🟢 فعال ۲۴ ساعت: <b>{active}</b>\n"
               f"  🚀 تعداد اجرا: <b>{s.get('bot_starts',0)}</b>\n\n"
               f"📈 <b>استفاده:</b>\n"
               f"  🌤️ آب‌وهوا: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ کیفیت هوا: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 پیش‌بینی: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ مقایسه: <b>{s.get('compare_requests',0)}</b>\n\n"
               f"🔔 <b>اعلان:</b>\n"
               f"  🔛 فعال: <b>{s.get('notify_enabled',0)}</b>\n"
               f"  📨 ارسال‌شده: <b>{s.get('notifications_sent',0)}</b>")

        if tops:
            txt += "\n\n🏆 <b>محبوب‌ترین شهرها:</b>\n"
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i, (cn, cnt) in enumerate(tops):
                txt += f"  {medals[i]} {cn} — <b>{cnt}</b>\n"

        if errs:
            txt += "\n⚠️ <b>خطاهای اخیر:</b>\n"
            for e in errs:
                txt += f"  <code>{e['error'][:60]}</code>\n"

        txt += "\n🕐 <i>نشست ۱ ساعته — بعدش باید دوباره وارد شوی</i>"

        rows = [
            [InlineKeyboardButton("🔄 رفرش آمار", callback_data="a:refresh")],
            [InlineKeyboardButton("🚪 خروج از پنل", callback_data="a:logout")],
        ]
    else:
        txt = (f"🔐 <b>Admin Panel</b>\n\n"
               f"📊 <b>Overview:</b>\n"
               f"  👥 Total users: <b>{total}</b>\n"
               f"  🟢 Active 24h: <b>{active}</b>\n"
               f"  🚀 Bot starts: <b>{s.get('bot_starts',0)}</b>\n\n"
               f"📈 <b>Usage:</b>\n"
               f"  🌤️ Weather: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ AQI: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 Forecast: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ Compare: <b>{s.get('compare_requests',0)}</b>\n\n"
               f"🔔 <b>Notifications:</b>\n"
               f"  🔛 Enabled: <b>{s.get('notify_enabled',0)}</b>\n"
               f"  📨 Sent: <b>{s.get('notifications_sent',0)}</b>")

        if tops:
            txt += "\n\n🏆 <b>Top Cities:</b>\n"
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i, (cn, cnt) in enumerate(tops):
                txt += f"  {medals[i]} {cn} — <b>{cnt}</b>\n"

        if errs:
            txt += "\n⚠️ <b>Recent errors:</b>\n"
            for e in errs:
                txt += f"  <code>{e['error'][:60]}</code>\n"

        txt += "\n🕐 <i>Session 1h — re-login after expiry</i>"

        rows = [
            [InlineKeyboardButton("🔄 Refresh", callback_data="a:refresh")],
            [InlineKeyboardButton("🚪 Logout", callback_data="a:logout")],
        ]
    return txt, rows


# ==================== دستورات ====================
async def cmd_start(update, ctx):
    u = get_user(update.effective_user.id, update)
    save_user(u["user_id"], pending_action=None)
    lang = u["lang"]
    if lang == "fa":
        txt = ("🌤️ <b>به ربات هوشمند آب‌وهوا خوش آمدید!</b>\n\n"
               "🔹 <b>راحت‌ترین راه:</b>\n"
               "فقط اسم شهر رو بنویس و بفرست! 🚀\n\n"
               "🎛️ برای همه امکانات، دکمه <b>منو</b> رو بزن.\n\n"
               "💡 مثلاً: <code>تهران</code> یا <code>Paris</code>")
    else:
        txt = ("🌤️ <b>Welcome to Smart Weather Bot!</b>\n\n"
               "🔹 <b>Easiest way:</b>\n"
               "Just type a city name and send! 🚀\n\n"
               "🎛️ Tap <b>Menu</b> for all features.\n\n"
               "💡 Example: <code>Tehran</code> or <code>Paris</code>")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main(lang))


async def cmd_weather(update, ctx):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await snd_weather(update.effective_chat, u["user_id"], u["default_city"], u); return
        msg = "اسم شهر رو بنویس" if u["lang"]=="fa" else "Type city name"
        await update.message.reply_text(msg, reply_markup=kb_main(u["lang"])); return
    await snd_weather(update.effective_chat, u["user_id"], " ".join(args), u)


async def cmd_aqi(update, ctx):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await snd_aqi(update.effective_chat, u["user_id"], u["default_city"], u); return
        msg = "مثال: <code>/aqi تهران</code>" if u["lang"]=="fa" else "Example: <code>/aqi Tehran</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    await snd_aqi(update.effective_chat, u["user_id"], " ".join(args), u)


async def cmd_forecast(update, ctx):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await snd_forecast(update.effective_chat, u["user_id"], u["default_city"], u); return
        msg = "مثال: <code>/forecast تهران</code>" if u["lang"]=="fa" else "Example: <code>/forecast Tehran</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    await snd_forecast(update.effective_chat, u["user_id"], " ".join(args), u)


async def cmd_compare(update, ctx):
    u = get_user(update.effective_user.id, update)
    await snd_compare(update.effective_chat, u["user_id"], ctx.args, u)


async def cmd_default(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u["lang"] == "fa":
            msg = (f"🏙️ شهر پیش‌فرض: <b>{u.get('default_city') or 'تنظیم نشده'}</b>\n\n"
                   f"مثال: <code>/default تهران</code>")
        else:
            msg = (f"🏙️ Default city: <b>{u.get('default_city') or 'not set'}</b>\n\n"
                   f"Example: <code>/default Tehran</code>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    cn = " ".join(ctx.args)
    city = await geocode(cn, u["lang"])
    if not city:
        err = "❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ Not found"
        await update.message.reply_text(err, reply_markup=kb_main(u["lang"])); return
    save_user(u["user_id"], default_city=city["name"])
    if u["lang"] == "fa":
        msg = f"✅ <b>{city['name']}</b> به عنوان شهر پیش‌فرض تنظیم شد"
    else:
        msg = f"✅ <b>{city['name']}</b> set as default city"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))


async def cmd_notify(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u["lang"] == "fa":
            s = "روشن ✅" if u.get("notify_enabled") else "خاموش ❌"
            msg = (f"🔔 اعلان روزانه: <b>{s}</b>\n"
                   f"⏰ ساعت: <b>{u.get('notify_time','07:00')}</b>\n\n"
                   f"<code>/notify on</code> — روشن\n<code>/notify off</code> — خاموش")
        else:
            s = "ON ✅" if u.get("notify_enabled") else "OFF ❌"
            msg = (f"🔔 Daily notify: <b>{s}</b>\n"
                   f"⏰ Time: <b>{u.get('notify_time','07:00')}</b>\n\n"
                   f"<code>/notify on</code> / <code>/notify off</code>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    a = ctx.args[0].lower()
    if a in ("on","روشن"):
        if not u.get("default_city"):
            msg = "⚠️ اول شهر پیش‌فرض رو تنظیم کن: /default تهران" if u["lang"]=="fa" \
                else "⚠️ Set default city first: /default Tehran"
            await update.message.reply_text(msg, reply_markup=kb_main(u["lang"])); return
        save_user(u["user_id"], notify_enabled=1)
        if u["lang"] == "fa":
            msg = f"✅ اعلان روزانه روشن شد\n⏰ هر روز ساعت <b>{u.get('notify_time','07:00')}</b>"
        else:
            msg = f"✅ Daily notify enabled\n⏰ Every day at <b>{u.get('notify_time','07:00')}</b>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))
        stat("notify_enabled")
    elif a in ("off","خاموش"):
        save_user(u["user_id"], notify_enabled=0)
        msg = "❌ اعلان خاموش شد" if u["lang"]=="fa" else "❌ Notifications disabled"
        await update.message.reply_text(msg, reply_markup=kb_main(u["lang"]))


async def cmd_settime(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        msg = "مثال: <code>/settime 07:30</code>" if u["lang"]=="fa" else "Example: <code>/settime 07:30</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    try:
        h, m = map(int, ctx.args[0].split(":"))
        if not (0<=h<24 and 0<=m<60): raise ValueError
        t = f"{h:02d}:{m:02d}"
        save_user(u["user_id"], notify_time=t)
        msg = f"✅ ساعت اعلان: <b>{t}</b>" if u["lang"]=="fa" else f"✅ Notify time: <b>{t}</b>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))
    except Exception:
        msg = "❌ فرمت اشتباه. مثال: <code>07:30</code>" if u["lang"]=="fa" else "❌ Wrong format. Example: <code>07:30</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))


async def on_location(update, ctx):
    u = get_user(update.effective_user.id, update)
    loc = update.message.location
    if not loc: return
    load_msg = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await update.message.reply_text(load_msg)
    try:
        w, a = await fetch_all(loc.latitude, loc.longitude)
        if not w:
            err = "⚠️ خطا در دریافت داده" if u["lang"]=="fa" else "⚠️ Failed"
            await msg.edit_text(err, reply_markup=kb_main(u["lang"])); return
        city_name = "موقعیت شما" if u["lang"]=="fa" else "Your Location"
        city = {"name": city_name, "latitude": loc.latitude, "longitude": loc.longitude}
        text = build_w(city, w, a, u["lang"], u["unit"])
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
        await update.effective_chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
    except Exception as e:
        log_err(u["user_id"], e, "location")


# ==================== هندلر متن ====================
async def on_text(update, ctx):
    u = get_user(update.effective_user.id, update)
    t = update.message.text.strip()
    lang = u["lang"]

    # ==================== پنل مدیریت ====================
    if t in ["🔐 پنل مدیریت", "🔐 Admin Panel"]:
        # حذف پیام کاربر
        try: await update.message.delete()
        except Exception: pass
        if is_admin(u["user_id"]):
            txt, rows = build_admin_panel(u)
            await update.effective_chat.send_message(txt, parse_mode=ParseMode.HTML,
                                                     reply_markup=InlineKeyboardMarkup(rows))
            return
        save_user(u["user_id"], pending_action="admin_pass")
        if lang == "fa":
            msg = ("🔐 <b>ورود به پنل مدیریت</b>\n\n"
                   "لطفاً رمز عبور را وارد کنید:\n"
                   "<i>(برای انصراف /start رو بزن)</i>")
        else:
            msg = ("🔐 <b>Admin Panel Login</b>\n\n"
                   "Please enter password:\n"
                   "<i>(cancel with /start)</i>")
        await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML)
        return

    # ==================== منو ====================
    if t in ["🎛️ منو","🎛️ Menu"]:
        try: await update.message.delete()
        except Exception: pass
        txt, rows = build_menu(u)
        await update.effective_chat.send_message(txt, parse_mode=ParseMode.HTML,
                                                 reply_markup=InlineKeyboardMarkup(rows))
        return

    # ==================== دکمه‌های اصلی ====================
    if t in ["🌤️ آب‌وهوا","🌤️ Weather"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_weather(update.effective_chat, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="w")
        msg = "🏙️ اسم شهر:" if lang=="fa" else "🏙️ City name:"
        await update.effective_chat.send_message(msg, reply_markup=kb_main(lang)); return

    if t in ["🌫️ کیفیت هوا","🌫️ Air Quality"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_aqi(update.effective_chat, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="a")
        msg = "🌫️ اسم شهر:" if lang=="fa" else "🌫️ City:"
        await update.effective_chat.send_message(msg, reply_markup=kb_main(lang)); return

    if t in ["📅 پیش‌بینی","📅 Forecast"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_forecast(update.effective_chat, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="f")
        msg = "📅 اسم شهر:" if lang=="fa" else "📅 City:"
        await update.effective_chat.send_message(msg, reply_markup=kb_main(lang)); return

    # ==================== pending_action ====================
    u2 = get_user(u["user_id"])
    act = u2.get("pending_action")

    # --- رمز ادمین ---
    if act == "admin_pass":
        save_user(u["user_id"], pending_action=None)
        try: await update.message.delete()
        except Exception: pass
        if t == ADMIN_PASSWORD:
            login_admin(u["user_id"])
            ok_msg = "✅ رمز درست! ورود موفق" if lang=="fa" else "✅ Correct! Login successful"
            await update.effective_chat.send_message(ok_msg)
            u3 = get_user(u["user_id"])
            txt, rows = build_admin_panel(u3)
            await update.effective_chat.send_message(txt, parse_mode=ParseMode.HTML,
                                                     reply_markup=InlineKeyboardMarkup(rows))
        else:
            err = "❌ رمز اشتباه است" if lang=="fa" else "❌ Wrong password"
            await update.effective_chat.send_message(err)
        return

    if act:
        try: await update.message.delete()
        except Exception: pass
        save_user(u["user_id"], pending_action=None)
        if act == "def":
            c = await geocode(t, lang)
            if not c:
                err = "❌ شهر پیدا نشد" if lang=="fa" else "❌ Not found"
                await update.effective_chat.send_message(err, reply_markup=kb_main(lang)); return
            save_user(u["user_id"], default_city=c["name"])
            u3 = get_user(u["user_id"])
            txt, rows = build_menu(u3)
            if lang == "fa":
                head = f"✅ <b>{c['name']}</b> به عنوان شهر پیش‌فرض تنظیم شد\n\n"
            else:
                head = f"✅ <b>{c['name']}</b> set as default city\n\n"
            await update.effective_chat.send_message(head + txt, parse_mode=ParseMode.HTML,
                                                     reply_markup=InlineKeyboardMarkup(rows))
            return
        if act == "c1":
            save_user(u["user_id"], compare_temp=t, pending_action="c2")
            if lang == "fa":
                msg = f"✅ شهر اول: <b>{t}</b>\n\n⚖️ حالا اسم <b>شهر دوم</b> رو بنویس:"
            else:
                msg = f"✅ City 1: <b>{t}</b>\n\n⚖️ Now type <b>city 2</b>:"
            await update.effective_chat.send_message(msg, parse_mode=ParseMode.HTML); return
        if act == "c2":
            c1 = u2.get("compare_temp")
            save_user(u["user_id"], compare_temp=None)
            await snd_compare(update.effective_chat, u["user_id"], [c1, t], u); return
        if act == "w": await snd_weather(update.effective_chat, u["user_id"], t, u); return
        if act == "a": await snd_aqi(update.effective_chat, u["user_id"], t, u); return
        if act == "f": await snd_forecast(update.effective_chat, u["user_id"], t, u); return

    # --- اسم شهر عادی ---
    try: await update.message.delete()
    except Exception: pass
    await snd_weather(update.effective_chat, u["user_id"], t, u)


# ==================== Callbacks ====================
async def on_cb(update, ctx):
    q = update.callback_query
    u = get_user(q.from_user.id, update)
    d = q.data
    lang = u["lang"]

    # ==================== پنل ادمین ====================
    if d == "a:refresh":
        if not is_admin(u["user_id"]):
            await q.answer("⛔", show_alert=True); return
        await q.answer("🔄")
        txt, rows = build_admin_panel(u)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "a:logout":
        logout_admin(u["user_id"])
        msg = "🚪 از پنل خارج شدی. برای ورود دوباره، دکمه 🔐 پنل مدیریت رو بزن." if lang=="fa" \
            else "🚪 Logged out. Tap 🔐 Admin Panel to login again."
        await q.answer("🚪")
        try: await q.edit_message_text(msg)
        except Exception: pass
        return

    # ==================== تغییر زبان ====================
    if d.startswith("lang:"):
        nl = d.split(":")[1]
        save_user(u["user_id"], lang=nl)
        await q.answer("✅")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        # آپدیت کیبورد پایین
        await q.message.chat.send_message("🔽", reply_markup=kb_main(nl))
        return

    # ==================== تغییر واحد ====================
    if d.startswith("unit:"):
        un = d.split(":")[1]
        save_user(u["user_id"], unit=un)
        await q.answer(f"✅ {un}")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    # ==================== منو: شهر پیش‌فرض ====================
    if d == "m:def":
        save_user(u["user_id"], pending_action="def")
        await q.answer()
        msg = "🏙️ اسم شهر جدید رو بنویس:" if lang=="fa" else "🏙️ Type new city name:"
        await q.message.chat.send_message(msg)
        return

    # ==================== منو: مقایسه ====================
    if d == "m:cmp":
        save_user(u["user_id"], pending_action="c1", compare_temp=None)
        await q.answer()
        msg = "⚖️ اسم <b>شهر اول</b> رو بنویس:" if lang=="fa" else "⚖️ Type <b>city 1</b>:"
        await q.message.chat.send_message(msg, parse_mode=ParseMode.HTML)
        return

    # ==================== منو: toggle اعلان ====================
    if d == "m:ntf":
        s = get_user(u["user_id"])
        if not s.get("default_city"):
            alert = "⚠️ اول شهر پیش‌فرض رو تنظیم کن" if lang=="fa" else "⚠️ Set default first"
            await q.answer(alert, show_alert=True); return
        new = 0 if s.get("notify_enabled") else 1
        save_user(u["user_id"], notify_enabled=new)
        if new: stat("notify_enabled")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        alert = ("✅ روشن شد" if lang=="fa" else "✅ Enabled") if new else \
                ("🔕 خاموش شد" if lang=="fa" else "🔕 Disabled")
        await q.answer(alert, show_alert=False)
        return

    # ==================== منو: ساعت اعلان ====================
    if d == "m:tim":
        await q.answer()
        times = ["06:00","07:00","08:00","09:00","12:00","15:00","18:00","21:00"]
        rows = [[InlineKeyboardButton(tt, callback_data=f"st:{tt}")
                 for tt in times[i:i+4]] for i in range(0,len(times),4)]
        rows.append([InlineKeyboardButton(
            "🔙 بازگشت به منو" if lang=="fa" else "🔙 Back to Menu",
            callback_data="m:home")])
        msg = "⏰ ساعت اعلان رو انتخاب کن:" if lang=="fa" else "⏰ Choose notify time:"
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows))
        return

    # ==================== ساعت انتخاب شد ====================
    if d.startswith("st:"):
        tt = d.split(":")[1] + ":00"
        save_user(u["user_id"], notify_time=tt)
        await q.answer(f"✅ {tt}")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    # ==================== برگشت به منو ====================
    if d == "m:home":
        await q.answer()
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try:
            await q.edit_message_text(txt, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    # ==================== منو: زبان ====================
    if d == "m:lng":
        await q.answer()
        rows = [[InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
                 InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
                [InlineKeyboardButton("🔙 بازگشت" if lang=="fa" else "🔙 Back",
                                      callback_data="m:home")]]
        msg = "🌐 انتخاب زبان:" if lang=="fa" else "🌐 Choose language:"
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows))
        return

    # ==================== منو: واحد ====================
    if d == "m:unt":
        await q.answer()
        rows = [[InlineKeyboardButton("🌡️ سلسیوس °C", callback_data="unit:C"),
                 InlineKeyboardButton("🌡️ فارنهایت °F", callback_data="unit:F")],
                [InlineKeyboardButton("🔙 بازگشت" if lang=="fa" else "🔙 Back",
                                      callback_data="m:home")]]
        msg = "🌡️ واحد دما:" if lang=="fa" else "🌡️ Temperature unit:"
        await q.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(rows))
        return


# ==================== اعلان روزانه ====================
async def notify_job(ctx):
    now = datetime.now().strftime("%H:%M")
    for ud in notify_users():
        if ud.get("notify_time") != now: continue
        try:
            cn = ud["default_city"]
            if not cn: continue
            lg, un = ud["lang"], ud["unit"]
            city = await geocode(cn, lg)
            if not city: continue
            w, a = await fetch_all(city["latitude"], city["longitude"])
            if not w: continue
            c = w["current"]; ic = WMO.get(c["weather_code"], {"ic":"❓"})["ic"]
            cname = (city.get("name_fa") or city["name"]) if lg=="fa" else city["name"]

            if lg == "fa":
                L = ["☀️ <b>صبح بخیر!</b>", "",
                     f"{ic} <b>{cname}</b>",
                     f"🌡️ دما: <b>{fmt_t(c['temperature_2m'],un)}</b>",
                     f"📝 وضعیت: {WMO.get(c['weather_code'],{}).get('fa','?')}", ""]
                tips = smart_tips(w, a, "fa")
                if tips:
                    L.append("💡 <b>توصیه هوشمند:</b>")
                    for tp in tips: L.append(f"  {tp}")
                L.append("")
                L.append("<i>برای خاموش کردن: /notify off</i>")
            else:
                L = ["☀️ <b>Good morning!</b>", "",
                     f"{ic} <b>{cname}</b>",
                     f"🌡️ Temp: <b>{fmt_t(c['temperature_2m'],un)}</b>",
                     f"📝 Condition: {WMO.get(c['weather_code'],{}).get('en','?')}", ""]
                tips = smart_tips(w, a, "en")
                if tips:
                    L.append("💡 <b>Smart advice:</b>")
                    for tp in tips: L.append(f"  {tp}")
                L.append("")
                L.append("<i>To disable: /notify off</i>")

            await ctx.bot.send_message(ud["user_id"], "\n".join(L), parse_mode=ParseMode.HTML,
                                       reply_markup=kb_main(lg))
            stat("notifications_sent")
        except Exception as e:
            logger.error(f"notify {ud['user_id']}: {e}")
            log_err(ud["user_id"], e, "notify")


# ==================== main ====================
async def post_init(app):
    init_db()
    await get_http()
    if app.job_queue:
        app.job_queue.run_repeating(notify_job, interval=30, first=10)
    logger.info("✅ Bot started")


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ BOT_TOKEN set کن!"); return
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
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
