#!/usr/bin/env python3
"""ربات آب‌وهوا — نسخه کامل با ترجمه کامل"""

import os, logging, asyncio, time, sqlite3, re
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
WTTR_API = "https://wttr.in"
DB_PATH = "weather_bot.db"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== ادمین ====================
_admin_sessions = {}
ADMIN_SESSION_TTL = 3600

def is_admin(uid: int) -> bool:
    ts = _admin_sessions.get(uid)
    if ts and time.time() - ts < ADMIN_SESSION_TTL: return True
    if ts: _admin_sessions.pop(uid, None)
    return False

def login_admin(uid: int): _admin_sessions[uid] = time.time()
def logout_admin(uid: int): _admin_sessions.pop(uid, None)

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
        c.execute("INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?", (key, inc, inc))
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
                 "notify_enabled": 0, "notify_time": "07:00", "pending_action": None, "compare_temp": None}
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

# ==================== HTTP ====================
_http: Optional[httpx.AsyncClient] = None

async def get_http():
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=50, max_connections=100),
            headers={"User-Agent": "WeatherBot/8.0"},
        )
    return _http

_geo_cache, _geo_t = {}, {}
_wx_cache = {}
_aqi_cache = {}
GEO_TTL = 86400
WX_TTL = 900
AQI_TTL = 900

def _cget(cache, tc, key, ttl):
    if key in cache and time.time() - tc.get(key, 0) < ttl: return cache[key]
    cache.pop(key, None); tc.pop(key, None); return None

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
                plain = re.sub(r'<[^>]+>', '', text)
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

CITY_INDEX = {}
for name_en, name_fa, lat, lon in IRAN:
    entry = {"name": name_en, "name_fa": name_fa, "latitude": lat, "longitude": lon, "country_code": "IR"}
    CITY_INDEX[name_en.lower()] = entry
    CITY_INDEX[name_fa] = entry
    CITY_INDEX[name_fa.replace("‌", "")] = entry
    CITY_INDEX[name_fa.replace("ي", "ی").replace("ك", "ک")] = entry

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

def pol_lvl(v, k):
    if v is None: return "⚪"
    th = {"pm2_5":[10,25,50,75,100],"pm10":[20,50,100,150,200],"no2":[40,90,120,230,340],
          "so2":[20,80,250,350,500],"o3":[60,100,140,180,240],"co":[4000,10000,20000,35000,50000]}.get(k,[50,100,150,200,300])
    for i, t in enumerate(th):
        if v <= t: return ["🟢","🟡","🟠","🔴","🟣"][i]
    return "⚫"

def pol_quality(v, k, lang):
    if v is None: return ""
    th = {"pm2_5":[10,25,50,75,100],"pm10":[20,50,100,150,200],"no2":[40,90,120,230,340],
          "so2":[20,80,250,350,500],"o3":[60,100,140,180,240],"co":[4000,10000,20000,35000,50000]}.get(k,[50,100,150,200,300])
    fa = ["عالی","قابل قبول","متوسط","ضعیف","خیلی ضعیف","خطرناک"]
    en = ["Excellent","Acceptable","Moderate","Poor","Very Poor","Hazardous"]
    for i, t in enumerate(th):
        if v <= t: return fa[i] if lang=="fa" else en[i]
    return fa[-1] if lang=="fa" else en[-1]

def fmt_t(c, u): return f"{round(c*9/5+32)}°F" if u=="F" else f"{round(c)}°C"

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
        if rp >= 60: t.append(f"☔ Rain {rp}%")
        if uv >= 6: t.append("🕶️ Sunscreen")
    return t[:3]

# ==================== API ====================
async def geocode(q, lang="fa"):
    key = q.strip().lower()
    c = _cget(_geo_cache, _geo_t, key, GEO_TTL)
    if c: return c
    entry = CITY_INDEX.get(q.strip())
    if entry:
        _geo_cache[key] = entry; _geo_t[key] = time.time()
        return entry
    try:
        cl = await get_http()
        r = await cl.get(GEO_API, params={"name": q.strip(), "count": 5, "language": lang, "format": "json"})
        r.raise_for_status()
        res = r.json().get("results", [])
        if not res: return None
        res.sort(key=lambda x: 0 if x.get("country_code") == "IR" else 1)
        out = res[0]
        _geo_cache[key] = out; _geo_t[key] = time.time()
        return out
    except Exception as e:
        logger.error(f"geocode: {e}"); return None


async def fetch_weather_only(la, lo, city_name=""):
    k = f"{la:.3f},{lo:.3f}"
    c = _cget(_wx_cache, {}, k, WX_TTL)
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
        w = r.json()
    except Exception as e:
        logger.warning(f"OM W failed: {e}"); w = None
    if not w and city_name:
        try:
            cl = await get_http()
            r = await cl.get(f"{WTTR_API}/{city_name}?format=j1")
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
                    "rain_prob": int(days[0].get("hourly", [{}])[4].get("chanceofrain", 0)) if days and len(days[0].get("hourly", [])) > 4 else 0
                }
        except Exception as e:
            logger.warning(f"wttr failed: {e}"); w = None
    if w: _wx_cache[k] = {"w": w, "t": time.time()}
    return w


async def fetch_aqi_only(la, lo):
    k = f"{la:.3f},{lo:.3f}"
    c = _cget(_aqi_cache, {}, k, AQI_TTL)
    if c: return c
    try:
        cl = await get_http()
        p = {"latitude": la, "longitude": lo,
             "current": "european_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,uv_index",
             "timezone": "auto"}
        r = await cl.get(AQI_API, params=p)
        r.raise_for_status()
        a = r.json().get("current")
        if a: _aqi_cache[k] = {"a": a, "t": time.time()}
        return a
    except Exception: return None


async def fetch_all_parallel(la, lo, city_name=""):
    wx_task = asyncio.create_task(fetch_weather_only(la, lo, city_name))
    aqi_task = asyncio.create_task(fetch_aqi_only(la, lo))
    w = await wx_task
    a = None
    try:
        a = await asyncio.wait_for(aqi_task, timeout=2.0)
    except Exception:
        a = None
    return w, a


# ==================== ساخت پیام ====================
def build_w(city, w, a, lang, unit):
    c = w["current"]; d = w.get("daily", {})
    code = c.get("weather_code", 0)
    wm = WMO.get(code, {"fa":"نامشخص","en":"Unknown","ic":"❓"})
    cn = (city.get("name_fa") or city["name"]) if lang == "fa" else city["name"]
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
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  حس واقعی: <b>{fls}</b>")
        try:
            if d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
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
        L.append("")
        L.append(f"🌡️ <b>{tmp}</b>  •  Feels like: <b>{fls}</b>")
        try:
            if d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
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


# ==================== ارسال ====================
async def snd_weather(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    try:
        msg = await ctx.bot.send_message(chat_id=chat_id, text=load)
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
        try: await safe_edit(msg, f"❌ {str(e)[:80]}")
        except Exception: pass


async def snd_aqi(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load)
    try:
        city = await geocode(q, u["lang"])
        if not city:
            await safe_edit(msg, "❌" if u["lang"]=="fa" else "❌"); return
        a = await fetch_aqi_only(city["latitude"], city["longitude"])
        if not a or a.get("european_aqi") is None:
            err = "⚠️ داده AQI در دسترس نیست" if u["lang"]=="fa" else "⚠️ AQI data unavailable"
            await safe_edit(msg, err); return
        v = a["european_aqi"]; info = aqi_i(v, u["lang"])
        cn = (city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]
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
        try: await safe_edit(msg, f"❌ {str(e)[:80]}")
        except Exception: pass


async def snd_forecast(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load)
    try:
        city = await geocode(q, u["lang"])
        if not city: await safe_edit(msg, "❌"); return
        w = await fetch_weather_only(city["latitude"], city["longitude"], city.get("name",""))
        if not w: await safe_edit(msg, "⚠️"); return
        d = w.get("daily", {})
        cn = (city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]
        if not d.get("time"): await safe_edit(msg, "⚠️"); return
        if u["lang"] == "fa":
            L = [f"📅 <b>پیش‌بینی — {cn}</b>", ""]
            days_fa = ["دوشنبه","سه‌شنبه","چهارشنبه","پنج‌شنبه","جمعه","شنبه","یکشنبه"]
        else:
            L = [f"📅 <b>Forecast — {cn}</b>", ""]
            days_fa = None
        for i in range(1, min(8, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"fa":"؟","en":"?","ic":"❓"})
            try:
                dt = datetime.fromisoformat(d["time"][i])
                dn = days_fa[dt.weekday()] if days_fa else dt.strftime("%A")
            except Exception: continue
            L.append(f"{wc['ic']} <b>{dn}</b> — {fmt_t(d['temperature_2m_min'][i],u['unit'])} ~ {fmt_t(d['temperature_2m_max'][i],u['unit'])}")
        await safe_edit(msg, "\n".join(L), parse_mode=ParseMode.HTML)
        stat("forecast_requests")
    except Exception as e:
        log_err(uid, e, "snd_forecast")
        try: await safe_edit(msg, f"❌ {str(e)[:80]}")
        except Exception: pass


async def snd_compare(ctx, chat_id, uid, cities, u):
    if len(cities) < 2:
        msg = "مثال: /compare تهران مشهد" if u["lang"]=="fa" else "Example: /compare Tehran Mashhad"
        await ctx.bot.send_message(chat_id=chat_id, text=msg); return
    msg = await ctx.bot.send_message(chat_id=chat_id, text="⏳ ...")
    try:
        c1, c2 = await asyncio.gather(geocode(cities[0], u["lang"]), geocode(cities[1], u["lang"]))
        if not c1 or not c2:
            await safe_edit(msg, "❌"); return
        (w1, _), (w2, _) = await asyncio.gather(
            fetch_all_parallel(c1["latitude"], c1["longitude"], c1.get("name","")),
            fetch_all_parallel(c2["latitude"], c2["longitude"], c2.get("name","")))
        if not w1 or not w2: await safe_edit(msg, "⚠️"); return
        def nm(c): return (c.get("name_fa") or c["name"]) if u["lang"]=="fa" else c["name"]
        t1 = w1["current"]["temperature_2m"]; t2 = w2["current"]["temperature_2m"]
        diff = t1 - t2
        if u["lang"] == "fa":
            L = [f"⚖️ <b>مقایسه دمای دو شهر</b>", "",
                 f"📍 <b>{nm(c1)}</b> — 🌡️ <b>{fmt_t(t1, u['unit'])}</b>",
                 f"📍 <b>{nm(c2)}</b> — 🌡️ <b>{fmt_t(t2, u['unit'])}</b>", ""]
            if abs(diff) < 0.5: L.append("🤝 دمای هر دو شهر یکسانه")
            elif diff > 0: L.append(f"📈 <b>{nm(c1)}</b> {round(abs(diff))}° گرم‌تر از <b>{nm(c2)}</b>")
            else: L.append(f"📈 <b>{nm(c2)}</b> {round(abs(diff))}° گرم‌تر از <b>{nm(c1)}</b>")
        else:
            L = [f"⚖️ <b>Compare City Temperatures</b>", "",
                 f"📍 <b>{nm(c1)}</b> — 🌡️ <b>{fmt_t(t1, u['unit'])}</b>",
                 f"📍 <b>{nm(c2)}</b> — 🌡️ <b>{fmt_t(t2, u['unit'])}</b>", ""]
            if abs(diff) < 0.5: L.append("🤝 Same temperature")
            elif diff > 0: L.append(f"📈 <b>{nm(c1)}</b> is {round(abs(diff))}° warmer than <b>{nm(c2)}</b>")
            else: L.append(f"📈 <b>{nm(c2)}</b> is {round(abs(diff))}° warmer than <b>{nm(c1)}</b>")
        await safe_edit(msg, "\n".join(L), parse_mode=ParseMode.HTML)
        stat("compare_requests")
    except Exception as e:
        log_err(uid, e, "snd_compare")
        try: await safe_edit(msg, f"❌ {str(e)[:80]}")
        except Exception: pass


# ==================== کیبورد ====================
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


def build_menu(u):
    s = get_user(u["user_id"]); lang = u["lang"]
    nst = "✅ روشن" if s.get("notify_enabled") else "❌ خاموش"
    nst_en = "ON ✅" if s.get("notify_enabled") else "OFF ❌"
    hd = s.get("default_city") or ("تنظیم نشده" if lang == "fa" else "not set")
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


def build_admin_panel(u):
    lang = u["lang"]; s = all_stats()
    total = total_users(); active = active_users_24h()
    errs = recent_errors(5); tops = top_cities(5)
    if lang == "fa":
        txt = (f"🔐 <b>پنل مدیریت</b>\n\n"
               f"👥 کاربران: <b>{total}</b>\n"
               f"🟢 فعال ۲۴ ساعت: <b>{active}</b>\n"
               f"🚀 تعداد اجرا: <b>{s.get('bot_starts',0)}</b>\n\n"
               f"📈 استفاده:\n"
               f"  🌤️ آب‌وهوا: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ AQI: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 پیش‌بینی: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ مقایسه: <b>{s.get('compare_requests',0)}</b>\n\n"
               f"🔔 اعلان فعال: <b>{s.get('notify_enabled',0)}</b>\n"
               f"📨 اعلان ارسالی: <b>{s.get('notifications_sent',0)}</b>")
        if tops:
            txt += "\n\n🏆 <b>محبوب‌ترین شهرها:</b>\n"
            m = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i,(cn,cnt) in enumerate(tops): txt += f"  {m[i]} {cn} — <b>{cnt}</b>\n"
        if errs:
            txt += "\n⚠️ <b>خطاهای اخیر:</b>\n"
            for e in errs: txt += f"  <code>{e['error'][:60]}</code>\n"
        rows = [[InlineKeyboardButton("🔄 رفرش آمار", callback_data="a:refresh")],
                [InlineKeyboardButton("🚪 خروج از پنل", callback_data="a:logout")]]
    else:
        txt = (f"🔐 <b>Admin Panel</b>\n\n"
               f"👥 Users: <b>{total}</b>\n"
               f"🟢 Active 24h: <b>{active}</b>\n"
               f"🚀 Starts: <b>{s.get('bot_starts',0)}</b>\n\n"
               f"📈 Usage:\n"
               f"  🌤️ Weather: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ AQI: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 Forecast: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ Compare: <b>{s.get('compare_requests',0)}</b>")
        if tops:
            txt += "\n\n🏆 <b>Top Cities:</b>\n"
            m = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i,(cn,cnt) in enumerate(tops): txt += f"  {m[i]} {cn} — <b>{cnt}</b>\n"
        if errs:
            txt += "\n⚠️ <b>Recent errors:</b>\n"
            for e in errs: txt += f"  <code>{e['error'][:60]}</code>\n"
        rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="a:refresh")],
                [InlineKeyboardButton("🚪 Logout", callback_data="a:logout")]]
    return txt, rows


# ==================== دستورات ====================
async def cmd_start(update, ctx):
    u = get_user(update.effective_user.id, update)
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
    await snd_compare(ctx, update.effective_chat.id, u["user_id"], ctx.args, u)


async def cmd_default(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        await update.message.reply_text(f"🏙️ {u.get('default_city') or '—'}",
                                        reply_markup=kb_main(u["lang"])); return
    city = await geocode(" ".join(ctx.args), u["lang"])
    if not city: await update.message.reply_text("❌"); return
    save_user(u["user_id"], default_city=city["name"])
    msg = f"✅ <b>{city['name']}</b>" if u["lang"]=="fa" else f"✅ <b>{city['name']}</b>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))


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
        save_user(u["user_id"], notify_enabled=1); stat("notify_enabled")
        await update.message.reply_text("✅", reply_markup=kb_main(u["lang"]))
    elif a in ("off","خاموش"):
        save_user(u["user_id"], notify_enabled=0)
        await update.message.reply_text("❌", reply_markup=kb_main(u["lang"]))


async def cmd_settime(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        msg = "/settime 07:30"
        await update.message.reply_text(msg); return
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
    except Exception as e: log_err(u["user_id"], e, "loc")


# ==================== متن ====================
async def on_text(update, ctx):
    u = get_user(update.effective_user.id, update)
    t = update.message.text.strip(); lang = u["lang"]
    chat_id = update.effective_chat.id

    if t in ["🔐 پنل مدیریت", "🔐 Admin Panel"]:
        try: await update.message.delete()
        except Exception: pass
        if is_admin(u["user_id"]):
            txt, rows = build_admin_panel(u)
            await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(rows))
            return
        save_user(u["user_id"], pending_action="admin_pass")
        msg = "🔐 رمز عبور رو وارد کن:" if lang=="fa" else "🔐 Enter password:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg)
        return

    if t in ["🎛️ منو","🎛️ Menu"]:
        try: await update.message.delete()
        except Exception: pass
        txt, rows = build_menu(u)
        await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup(rows))
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

    if act == "admin_pass":
        save_user(u["user_id"], pending_action=None)
        try: await update.message.delete()
        except Exception: pass
        if t == ADMIN_PASSWORD:
            login_admin(u["user_id"])
            u3 = get_user(u["user_id"])
            txt, rows = build_admin_panel(u3)
            await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(rows))
        else:
            msg = "❌ رمز اشتباه" if lang=="fa" else "❌ Wrong password"
            await ctx.bot.send_message(chat_id=chat_id, text=msg)
        return

    if act:
        try: await update.message.delete()
        except Exception: pass
        save_user(u["user_id"], pending_action=None)
        if act == "def":
            c = await geocode(t, lang)
            if not c: await ctx.bot.send_message(chat_id=chat_id, text="❌"); return
            save_user(u["user_id"], default_city=c["name"])
            u3 = get_user(u["user_id"])
            txt, rows = build_menu(u3)
            await ctx.bot.send_message(chat_id=chat_id, text=f"✅ <b>{c['name']}</b>\n\n{txt}",
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            return
        if act == "c1":
            save_user(u["user_id"], compare_temp=t, pending_action="c2")
            msg = f"✅ {t}\n\n⚖️ شهر دوم:" if lang=="fa" else f"✅ {t}\n\n⚖️ Second city:"
            await ctx.bot.send_message(chat_id=chat_id, text=msg); return
        if act == "c2":
            c1 = u2.get("compare_temp")
            save_user(u["user_id"], compare_temp=None)
            await snd_compare(ctx, chat_id, u["user_id"], [c1, t], u); return
        if act == "w": await snd_weather(ctx, chat_id, u["user_id"], t, u); return
        if act == "a": await snd_aqi(ctx, chat_id, u["user_id"], t, u); return
        if act == "f": await snd_forecast(ctx, chat_id, u["user_id"], t, u); return

    try: await update.message.delete()
    except Exception: pass
    await snd_weather(ctx, chat_id, u["user_id"], t, u)


# ==================== Callbacks ====================
async def on_cb(update, ctx):
    q = update.callback_query
    u = get_user(q.from_user.id, update)
    d = q.data; lang = u["lang"]; chat_id = q.message.chat.id

    if d == "a:refresh":
        if not is_admin(u["user_id"]): await q.answer("⛔", show_alert=True); return
        await q.answer("🔄")
        txt, rows = build_admin_panel(u)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "a:logout":
        logout_admin(u["user_id"]); await q.answer("🚪")
        msg = "🚪 از پنل خارج شدی" if lang=="fa" else "🚪 Logged out"
        try: await q.edit_message_text(msg)
        except Exception: pass
        return

    if d.startswith("lang:"):
        nl = d.split(":")[1]
        save_user(u["user_id"], lang=nl); await q.answer("✅")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        await ctx.bot.send_message(chat_id=chat_id, text="🔽", reply_markup=kb_main(nl))
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
        await ctx.bot.send_message(chat_id=chat_id, text=msg)
        return

    if d == "m:cmp":
        save_user(u["user_id"], pending_action="c1", compare_temp=None); await q.answer()
        msg = "⚖️ اسم شهر اول:" if lang=="fa" else "⚖️ First city:"
        await ctx.bot.send_message(chat_id=chat_id, text=msg)
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
    now = datetime.now().strftime("%H:%M")
    for ud in notify_users():
        if ud.get("notify_time") != now: continue
        try:
            cn = ud["default_city"]
            if not cn: continue
            lg, un = ud["lang"], ud["unit"]
            city = await geocode(cn, lg)
            if not city: continue
            w, a = await fetch_all_parallel(city["latitude"], city["longitude"], city.get("name",""))
            if not w: continue
            c = w["current"]; ic = WMO.get(c.get("weather_code",0), {"ic":"❓"})["ic"]
            cname = (city.get("name_fa") or city["name"]) if lg=="fa" else city["name"]
            if lg == "fa":
                L = [f"☀️ <b>صبح بخیر!</b>", "",
                     f"{ic} <b>{cname}</b>", f"🌡️ <b>{fmt_t(c.get('temperature_2m',0),un)}</b>"]
            else:
                L = [f"☀️ <b>Good morning!</b>", "",
                     f"{ic} <b>{cname}</b>", f"🌡️ <b>{fmt_t(c.get('temperature_2m',0),un)}</b>"]
            await ctx.bot.send_message(chat_id=ud["user_id"], text="\n".join(L), parse_mode=ParseMode.HTML)
            stat("notifications_sent")
        except Exception as e:
            logger.error(f"notify: {e}")


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
