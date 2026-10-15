#!/usr/bin/env python3
"""ربات هوشمند آب‌وهوا — نسخه نهایی درست"""

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
        c = db()
        r = c.execute("SELECT COUNT(*) as n FROM users WHERE last_seen > datetime('now', '-1 day')").fetchone()
        c.close(); return r["n"]
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
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=30, max_connections=60),
            headers={"User-Agent": "WeatherBot/4.0"},
        )
    return _http

_geo_cache, _geo_t = {}, {}
_w_cache = {}
TTL = 300

def _cget(cache, tc, key, ttl):
    if key in cache and time.time() - tc.get(key, 0) < ttl: return cache[key]
    cache.pop(key, None); tc.pop(key, None); return None

# ==================== شهرها ====================
IRAN = [
    ("Tehran","تهران",35.6892,51.3890),("Karaj","کرج",35.8355,50.9915),
    ("Eslamshahr","اسلامشهر",35.5522,51.2350),("Shahriar","شهریار",35.6594,51.0589),
    ("Varamin","ورامین",35.3244,51.6458),("Pakdasht","پاکدشت",35.4722,51.6828),
    ("Pardis","پردیس",35.7386,51.8097),("Robat Karim","رباط‌کریم",35.4844,51.0833),
    ("Qods","قدس",35.7167,51.1167),("Malard","ملارد",35.6667,50.9833),
    ("Damavand","دماوند",35.7172,52.0653),("Firuzkuh","فیروزکوه",35.7567,52.7731),
    ("Lavasan","لواسان",35.8231,51.6361),("Rudehen","رودهن",35.7347,51.9103),
    ("Nasimshahr","نسیم‌شهر",35.5667,51.1667),("Golestan","گلستان",35.5167,51.1833),
    ("Qarchak","قرچک",35.4394,51.5689),("Pishva","پیشوا",35.3092,51.7275),
    ("Absard","آبسرد",35.6172,52.1411),("Abali","آبعلی",35.7667,51.9667),
    ("Mashhad","مشهد",36.2605,59.6168),("Neyshabur","نیشابور",36.2133,58.7958),
    ("Sabzevar","سبزوار",36.2125,57.6819),("Torbat-e Heydarieh","تربت حیدریه",35.2736,59.2197),
    ("Torbat-e Jam","تربت جام",35.2439,60.6236),("Quchan","قوچان",37.1061,58.5094),
    ("Kashmar","کاشمر",35.2381,58.4656),("Gonabad","گناباد",34.3528,58.6836),
    ("Chenaran","چناران",36.6456,59.1211),("Sarakhs","سرخس",36.5447,61.1581),
    ("Fariman","فریمان",35.7039,59.8500),("Khaf","خواف",34.5764,60.1408),
    ("Bardaskan","بردسکن",35.2606,57.9711),("Dargaz","درگز",37.4444,59.1067),
    ("Kalat","کلات",36.9961,59.7619),("Bajestan","بجستان",34.5167,58.1833),
    ("Khalilabad","خلیل‌آباد",35.2561,58.2856),("Bakharz","باخرز",34.9833,60.3167),
    ("Isfahan","اصفهان",32.6546,51.6680),("Kashan","کاشان",33.9850,51.4100),
    ("Najafabad","نجف‌آباد",32.6333,51.3667),("Khomeynishahr","خمینی‌شهر",32.7000,51.5167),
    ("Shahin Shahr","شاهین‌شهر",32.8667,51.5500),("Shahreza","شهرضا",32.0000,51.8667),
    ("Fooladshahr","فولادشهر",32.4833,51.4000),("Zarrinshahr","زرین‌شهر",32.3833,51.3833),
    ("Khomeyn","خمین",33.6419,50.0800),("Natanz","نطنز",33.5108,51.9183),
    ("Ardestan","اردستان",33.3761,52.3700),("Golpayegan","گلپایگان",33.4536,50.2889),
    ("Semirom","سمیرم",31.4183,51.5711),("Dehaqan","دهاقان",32.0500,51.6500),
    ("Nain","نائین",32.8603,53.0869),("Mobarakeh","مبارکه",32.3453,51.5014),
    ("Kuhpayeh","کوهپایه",32.7167,52.4333),("Mehriz","مهریز",31.5833,54.4333),
    ("Chadegan","چادگان",32.7683,50.6278),("Shiraz","شیراز",29.5918,52.5837),
    ("Marvdasht","مرودشت",29.8739,52.8025),("Jahrom","جهرم",28.5000,53.5600),
    ("Fasa","فسا",28.9389,53.6481),("Kazerun","کازرون",29.6158,51.6553),
    ("Lar","لار",27.6833,54.3417),("Abadeh","آباده",31.1606,52.6519),
    ("Darab","داراب",28.7519,54.5406),("Neyriz","نی‌ریز",29.1981,54.3281),
    ("Sepidan","سپیدان",30.2333,51.9833),("Eqlid","اقلید",30.8978,52.6867),
    ("Estahban","استهبان",29.1278,54.0417),("Zarqan","زرقان",29.7711,52.7233),
    ("Kharameh","خرامه",29.5000,53.3167),("Pasargad","پاسارگاد",30.2000,53.1833),
    ("Bavanat","بوانات",30.4667,53.6500),("Arsanjan","ارسنجان",29.9167,53.3167),
    ("Mamasani","ممسنی",30.1000,51.5333),("Mohr","مهر",27.5500,52.8833),
    ("Lamerd","لامرد",27.3333,53.1833),("Qir","قیر",28.3333,53.0333),
    ("Farashband","فراشبند",28.8667,52.0833),("Sadra","صدرا",29.7833,52.4667),
    ("Safashahr","صفاشهر",30.6167,53.6500),("Tabriz","تبریز",38.0962,46.2738),
    ("Maragheh","مراغه",37.3892,46.2382),("Marand","مرند",38.4320,45.7700),
    ("Ahar","اهر",38.4775,47.0662),("Mianeh","میانه",37.4211,47.7100),
    ("Bonab","بناب",38.4283,45.9087),("Sarab","سراب",36.3175,47.5648),
    ("Azarshahr","آذرشهر",37.7585,45.9794),("Osku","اسکو",37.9161,46.1245),
    ("Shabestar","شبستر",38.1784,45.7002),("Jolfa","جلفا",38.9376,45.6252),
    ("Sardrud","سردرود",38.0314,46.1475),("Ajab Shir","عجب‌شیر",37.4828,45.8936),
    ("Bostanabad","بستان‌آباد",37.8500,46.8333),("Hashtrud","هشترود",37.4833,47.0500),
    ("Heris","هریس",38.2500,47.1167),("Malekan","ملکان",37.1333,46.1000),
    ("Varzaqan","ورزقان",38.5167,46.6500),("Kaleybar","کلیبر",38.8667,47.0333),
    ("Urmia","ارومیه",37.5553,45.0725),("Khoy","خوی",38.5500,44.9500),
    ("Mahabad","مهاباد",36.7639,45.7222),("Bukan","بوکان",36.5210,46.2100),
    ("Miandoab","میاندوآب",36.9667,46.1000),("Salmas","سلماس",38.1972,44.7683),
    ("Naqadeh","نقده",36.9553,45.3881),("Piranshahr","پیرانشهر",36.7000,45.1500),
    ("Sardasht","سردشت",36.1580,45.4752),("Shahin Dezh","شاهین‌دژ",36.6769,46.5667),
    ("Takab","تکاب",36.4008,47.1125),("Maku","ماکو",39.2956,44.5117),
    ("Oshnavieh","اشنویه",37.0397,45.0983),("Poldasht","پلدشت",39.3483,45.0711),
    ("Showt","شوط",39.2167,44.7667),("Ahvaz","اهواز",31.3167,48.6844),
    ("Abadan","آبادان",30.3392,48.3043),("Khorramshahr","خرمشهر",30.4408,48.1845),
    ("Dezful","دزفول",32.3830,48.4058),("Andimeshk","اندیمشک",32.4610,48.3534),
    ("Shushtar","شوشتر",32.0452,48.8540),("Behbahan","بهبهان",30.5959,50.2417),
    ("Izeh","ایذه",31.8300,49.8667),("Masjed Soleyman","مسجدسلیمان",31.9353,49.3039),
    ("Shush","شوش",32.1944,48.2572),("Bandar Mahshahr","بندر ماهشهر",30.5589,49.1981),
    ("Omidiyeh","امیدیه",30.7622,49.7025),("Ramhormoz","رامهرمز",31.2792,49.6022),
    ("Susangerd","سوسنگرد",31.5619,48.1819),("Hendijan","هندیجان",30.2342,49.7139),
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
    ("Khomam","خمام",37.4000,49.6333),("Chaboksar","چابکسر",36.9667,50.5667),
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
    ("Pol-e Sefid","پل سفید",36.1167,52.9167),("Zirab","زیرآب",36.1833,52.9667),
    ("Shirgah","شیرگاه",36.2833,52.8833),("Kiasar","کیاسر",36.2333,53.5333),
    ("Mahmudabad","محمودآباد",36.6333,52.2667),("Nur","نور",36.5667,52.0167),
    ("Kelardasht","کلاردشت",36.4833,51.1333),("Marzanabad","مرزن‌آباد",36.4500,51.3000),
    ("Nashtarud","نشتارود",36.7500,51.0000),("Rineh","رینه",35.8833,52.0500),
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
    ("Bojnord","بجنورد",37.4747,57.3290),("Shirvan","شیروان",37.3975,57.9300),
    ("Esfarayen","اسفراین",37.0764,57.5100),("Birjand","بیرجند",32.8663,59.2211),
    ("Qaen","قائن",33.7258,59.1858),("Ferdows","فردوس",34.0175,58.1700),
    ("Tabas","طبس",33.5956,56.9244),("Nehbandan","نهبندان",31.5333,60.0333),
    ("Sarbisheh","سربیشه",32.5833,59.8000),("Khusf","خوسف",32.7833,58.8833),
    ("Boshruyeh","بشرویه",33.8667,57.4167),("Sarayan","سرایان",33.8667,58.5167),
    ("Kermanshah","کرمانشاه",34.3142,47.0650),("Eslamabad-e Gharb","اسلام‌آباد غرب",34.1089,46.5308),
    ("Harsin","هرسین",34.2736,47.5861),("Sonqor","سنقر",34.7839,47.5986),
    ("Paveh","پاوه",35.0456,46.3575),("Sanandaj","سنندج",35.3149,46.9921),
    ("Saqqez","سقز",36.2498,46.2735),("Marivan","مریوان",35.5239,46.1761),
    ("Baneh","بانه",35.9989,45.8867),("Qorveh","قروه",35.1664,47.8047),
    ("Bijar","بیجار",35.8728,47.6025),("Kangavar","کنگاور",34.5000,47.9667),
    ("Sahneh","صحنه",34.4833,47.6833),("Javanrud","جوانرود",34.8000,46.5000),
    ("Kamyaran","کامیاران",34.8000,46.9333),("Dehgolan","دهگلان",35.2833,47.4167),
    ("Qom","قم",34.6416,50.8746),("Ardabil","اردبیل",38.2494,48.3014),
    ("Parsabad","پارس‌آباد",39.6489,47.9164),("Khalkhal","خلخال",37.6192,48.5253),
    ("Meshgin Shahr","مشگین‌شهر",38.3986,47.6806),("Germi","گرمی",39.0333,48.0833),
    ("Bileh Savar","بیله سوار",39.3833,48.3500),("Nir","نیر",38.0333,47.9833),
    ("Sareyn","سرعین",38.1500,48.0667),("Arak","اراک",34.1380,49.8470),
    ("Saveh","ساوه",35.0214,50.3567),("Mahallat","محلات",33.9111,50.4531),
    ("Zanjan","زنجان",36.6769,48.4963),("Abhar","ابهر",36.1458,49.2181),
    ("Khorramdarreh","خرمدره",36.2069,49.1914),("Qazvin","قزوین",36.2688,50.0041),
    ("Takestan","تاکستان",36.0697,49.6981),("Gorgan","گرگان",36.8378,54.4414),
    ("Gonbad-e Kavus","گنبد کاووس",37.2506,55.1675),("Aliabad-e Katul","علی‌آباد کتول",36.9086,54.8653),
    ("Bandar Torkaman","بندر ترکمن",36.9019,54.0719),("Yazd","یزد",31.8974,54.3569),
    ("Meybod","میبد",32.2500,54.0167),("Ardakan","اردکان",32.3100,54.0175),
    ("Bafq","بافق",31.6119,55.4106),("Taft","تفت",31.7481,54.2058),
    ("Zahedan","زاهدان",29.4963,60.8629),("Zabol","زابل",31.0289,61.4947),
    ("Chabahar","چابهار",25.2919,60.6431),("Iranshahr","ایرانشهر",27.2025,60.6847),
    ("Saravan","سراوان",27.3706,62.3342),("Khash","خاش",28.2211,61.2158),
    ("Hamadan","همدان",34.7983,48.5148),("Malayer","ملایر",34.2969,48.8236),
    ("Nahavand","نهاوند",34.1889,48.3769),("Tuyserkan","تویسرکان",34.5522,48.4456),
    ("Asadabad","اسدآباد",34.7819,48.1197),("Bandar Abbas","بندرعباس",27.1832,56.2666),
    ("Qeshm","قشم",26.9581,56.2719),("Kish","کیش",26.5578,53.9803),
    ("Minab","میناب",27.1461,57.0803),("Bandar Lengeh","بندر لنگه",26.5581,54.8806),
    ("Bushehr","بوشهر",28.9234,50.8200),("Borazjan","برازجان",29.2664,51.2136),
    ("Bandar Ganaveh","بندر گناوه",29.5792,50.5172),("Kangan","کنگان",27.8372,52.0631),
    ("Ilam","ایلام",33.6374,46.4227),("Dehloran","دهلران",32.6942,47.2639),
    ("Abdanan","آبدانان",32.9919,47.4200),("Eyvan","ایوان",33.8275,46.3081),
    ("Semnan","سمنان",35.5769,53.3953),("Shahrud","شاهرود",36.4181,54.9764),
    ("Damghan","دامغان",36.1681,54.3481),("Garmsar","گرمسار",35.2186,52.3406),
    ("Khorramabad","خرم‌آباد",33.4878,48.3558),("Borujerd","بروجرد",33.8973,48.7516),
    ("Dorud","دورود",33.4922,49.0580),("Aligudarz","الیگودرز",33.4006,49.6961),
    ("Kohdasht","کوهدشت",33.5303,47.6072),("Yasuj","یاسوج",30.6681,51.5881),
    ("Dogonbadan","دوگنبدان",30.3600,50.7953),("Shahr-e Kord","شهرکرد",32.3260,50.8644),
    ("Borujen","بروجن",31.9667,51.2833),("Lordegan","لردگان",31.5167,50.8167),
    ("Farsan","فارسان",32.2500,50.5667),("Farrokhshahr","فرخ‌شهر",32.2833,50.9833),
    ("Hafshejan","هفشجان",32.1833,50.8333),("Nafch","نافچ",32.4167,50.8000),
]

# نقشه سریع
CITY_INDEX = {}
for name_en, name_fa, lat, lon in IRAN:
    entry = {"name": name_en, "name_fa": name_fa, "latitude": lat, "longitude": lon, "country_code": "IR"}
    CITY_INDEX[name_en.lower()] = entry
    CITY_INDEX[name_fa] = entry
    CITY_INDEX[name_fa.replace("‌", "")] = entry
    CITY_INDEX[name_fa.replace("ي", "ی").replace("ك", "ک")] = entry

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
        (20,{"lbl":"خوب" if lang=="fa" else "Good","ic":"🟢","ad":"هوا پاکه 😎" if lang=="fa" else "Air is clean 😎"}),
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
    # لیست محلی
    entry = CITY_INDEX.get(q.strip())
    if entry:
        _geo_cache[key] = entry; _geo_t[key] = time.time()
        return entry
    # API
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

async def f_weather(la, lo):
    # ✅ پارامترهای ساده و مطمئن (بدون cloud_cover و uv_index_max)
    p = {"latitude": la, "longitude": lo,
         "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,visibility,dew_point_2m,uv_index",
         "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
         "timezone": "auto", "forecast_days": 7, "past_days": 1}
    cl = await get_http()
    r = await cl.get(WEATHER_API, params=p)
    r.raise_for_status()
    return r.json()

async def f_aqi(la, lo):
    # ✅ پارامترهای ساده و تست‌شده
    p = {"latitude": la, "longitude": lo,
         "current": "european_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,uv_index,ammonia,dust",
         "timezone": "auto"}
    try:
        cl = await get_http()
        r = await cl.get(AQI_API, params=p)
        r.raise_for_status()
        return r.json().get("current")
    except Exception as e:
        logger.warning(f"aqi failed: {e}"); return None

async def fetch_all(la, lo):
    k = f"{la:.3f},{lo:.3f}"
    c = _w_cache.get(k)
    if c and time.time() - c["t"] < TTL: return c["w"], c["a"]
    w, a = await asyncio.gather(f_weather(la, lo), f_aqi(la, lo), return_exceptions=True)
    if isinstance(w, Exception): logger.error(f"W err: {w}"); w = None
    if isinstance(a, Exception): logger.warning(f"A err: {a}"); a = None
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
        L.append(f"🌅 طلوع: <b>{sr}</b>  •  🌇 غروب: <b>{ss}</b>")
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
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 Humidity: <b>{hum}%</b>")
        L.append(f"💨 Wind: <b>{wnd} km/h</b> ({we})")
        L.append(f"🔽 Pressure: <b>{prs}</b> hPa")
        if uv is not None:
            lb, ci = uv_lvl(uv, "en")
            L.append(f"☀️ UV: {ci} <b>{lb}</b>")
        L.append(f"🌅 Sunrise: <b>{sr}</b>  •  🌇 Sunset: <b>{ss}</b>")
        if a and a.get("european_aqi") is not None:
            av = a["european_aqi"]; info = aqi_i(av, "en")
            L.append("")
            L.append(f"🌫️ {info['ic']} <b>{info['lbl']}</b>  •  AQI: <b>{av}</b>")
        tips = smart_tips(w, a, "en")
        if tips:
            L.append("")
            L.append("💡 <b>Smart Advice:</b>")
            for t in tips: L.append(f"  {t}")
        L.append("")
        L.append("📅 <b>3-day forecast:</b>")
        for i in range(1, min(4, len(d["time"]))):
            wc = WMO.get(d["weather_code"][i], {"en":"?","ic":"❓"})
            dt = datetime.fromisoformat(d["time"][i])
            L.append(f"{wc['ic']} <b>{dt.strftime('%A')}</b> 🌡️ {fmt_t(d['temperature_t2m_min'][i] if 'temperature_t2m_min' in d else d['temperature_2m_min'][i],unit)}~{fmt_t(d['temperature_2m_max'][i],unit)}")
        L.append("")
        L.append("<i>📡 Data from Open-Meteo</i>")
    return "\n".join(L)

# ==================== ارسال‌ها ====================
async def snd_weather(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        city = await geocode(q, u["lang"])
        if not city:
            await msg.edit_text("❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ City not found"); return
        la, lo = city["latitude"], city["longitude"]
        w, a = await fetch_all(la, lo)
        if not w:
            await msg.edit_text("⚠️ خطا در دریافت داده. دوباره تلاش کن" if u["lang"]=="fa" else "⚠️ Failed. Try again"); return
        text = build_w(city, w, a, u["lang"], u["unit"])
        kb = None
        if WEB_APP_URL:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🌐 وب‌سایت کامل" if u["lang"]=="fa" else "🌐 Full Website", url=WEB_APP_URL)]])
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        stat("weather_requests")
    except Exception as e:
        logger.exception("snd_weather")
        log_err(uid, e, "snd_weather")
        try: await msg.edit_text(f"❌ {str(e)[:80]}")
        except Exception: pass

async def snd_aqi(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        city = await geocode(q, u["lang"])
        if not city:
            await msg.edit_text("❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ Not found"); return
        _, a = await fetch_all(city["latitude"], city["longitude"])
        if not a or a.get("european_aqi") is None:
            await msg.edit_text("⚠️ داده کیفیت هوا موجود نیست" if u["lang"]=="fa" else "⚠️ AQI data not available"); return

        v = a["european_aqi"]; info = aqi_i(v, u["lang"])
        cn = (city.get("name_fa") or city["name"]) if u["lang"]=="fa" else city["name"]

        pm25 = a.get("pm2_5"); pm10 = a.get("pm10")
        no2 = a.get("nitrogen_dioxide"); so2 = a.get("sulphur_dioxide")
        o3 = a.get("ozone"); co = a.get("carbon_monoxide")
        nh3 = a.get("ammonia"); dust = a.get("dust")
        uv = a.get("uv_index")

        pollutants = {
            "pm2_5": (pm25, "PM2.5", "ذرات ریز خطرناک", "Fine particles"),
            "pm10": (pm10, "PM10", "ذرات معلق", "Coarse particles"),
            "no2": (no2, "NO₂", "دی‌اکسید نیتروژن", "Nitrogen dioxide"),
            "so2": (so2, "SO₂", "دی‌اکسید گوگرد", "Sulphur dioxide"),
            "o3": (o3, "O₃", "ازن", "Ozone"),
            "co": (co, "CO", "مونوکسید کربن", "Carbon monoxide"),
        }
        dominant = None; dominant_ratio = 0
        ths = {"pm2_5": 15, "pm10": 45, "no2": 25, "so2": 40, "o3": 100, "co": 4000}
        for key, (val, *_) in pollutants.items():
            if val is not None and val > 0:
                ratio = val / ths.get(key, 100)
                if ratio > dominant_ratio:
                    dominant_ratio = ratio; dominant = key

        if u["lang"] == "fa":
            L = [f"🌫️ <b>کیفیت هوای {cn}</b>", "", f"{info['ic']} <b>{info['lbl']}</b>",
                 f"📊 شاخص AQI: <b>{v}</b>", f"<code>{bar(v,100,15)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>آلاینده‌های اصلی:</b>", ""]
            for key, (val, name, fa_desc, _) in pollutants.items():
                if val is not None:
                    marker = " ⭐" if key == dominant else ""
                    quality = pol_quality(val, key, "fa")
                    L.append(f"{pol_lvl(val,key)} <b>{name}</b>: {round(val)} µg/m³  <i>({quality})</i>{marker}")
                    L.append(f"   <i>{fa_desc}</i>")
            if dominant:
                L.append(""); L.append(f"🔴 <b>آلاینده غالب:</b> {pollutants[dominant][1]}")
            if uv is not None:
                L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
                lb, ci = uv_lvl(uv, "fa")
                L.append(f"☀️ <b>شاخص UV:</b> {ci} <b>{lb}</b> ({uv:.1f})")
            if nh3 is not None and nh3 > 0:
                L.append(f"🧪 آمونیاک: <b>{round(nh3)}</b> µg/m³")
            if dust is not None and dust > 0:
                L.append(f"🌪️ غبار: <b>{round(dust)}</b> µg/m³")
            L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append("💡 <b>توصیه سلامتی:</b>"); L.append(f"  {info['ad']}")
            if v >= 60:
                L.append("  🏠 توصیه می‌شه در خونه بمونی")
                L.append("  😷 در صورت بیرون رفتن، ماسک N95 بزن")
            if dominant == "pm2_5" and pm25 and pm25 > 35:
                L.append("  ⚠️ ذرات PM2.5 بالا — برای ریه خطرناکه")
            if dominant == "o3" and o3 and o3 > 120:
                L.append("  ⚠️ ازن بالا — ورزش در فضای باز نکن")
            L.append(""); L.append("<i>📡 داده‌ها از Open-Meteo Air Quality</i>")
        else:
            L = [f"🌫️ <b>Air Quality — {cn}</b>", "", f"{info['ic']} <b>{info['lbl']}</b>",
                 f"📊 AQI: <b>{v}</b>", f"<code>{bar(v,100,15)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>Main Pollutants:</b>", ""]
            for key, (val, name, _, en_desc) in pollutants.items():
                if val is not None:
                    marker = " ⭐" if key == dominant else ""
                    quality = pol_quality(val, key, "en")
                    L.append(f"{pol_lvl(val,key)} <b>{name}</b>: {round(val)} µg/m³  <i>({quality})</i>{marker}")
            if dominant:
                L.append(""); L.append(f"🔴 <b>Dominant:</b> {pollutants[dominant][1]}")
            if uv is not None:
                L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
                lb, ci = uv_lvl(uv, "en")
                L.append(f"☀️ <b>UV Index:</b> {ci} <b>{lb}</b> ({uv:.1f})")
            L.append(""); L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append("💡 <b>Health Advice:</b>"); L.append(f"  {info['ad']}")
            if v >= 60:
                L.append("  🏠 Stay indoors recommended")
                L.append("  😷 Wear N95 mask if going out")
            L.append(""); L.append("<i>📡 Data from Open-Meteo Air Quality</i>")

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        stat("aqi_requests")
    except Exception as e:
        log_err(uid, e, "snd_aqi")
        try: await msg.edit_text(f"❌ {str(e)[:80]}")
        except Exception: pass

async def snd_forecast(ctx, chat_id, uid, q, u):
    load = "⏳ در حال دریافت..." if u["lang"]=="fa" else "⏳ Fetching..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        city = await geocode(q, u["lang"])
        if not city:
            await msg.edit_text("❌ شهر پیدا نشد" if u["lang"]=="fa" else "❌ Not found"); return
        w, _ = await fetch_all(city["latitude"], city["longitude"])
        if not w:
            await msg.edit_text("⚠️ خطا در دریافت داده" if u["lang"]=="fa" else "⚠️ Failed"); return
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
        stat("forecast_requests")
    except Exception as e:
        log_err(uid, e, "snd_forecast")
        try: await msg.edit_text(f"❌ {str(e)[:80]}")
        except Exception: pass

async def snd_compare(ctx, chat_id, uid, cities, u):
    if len(cities) < 2:
        msg = ("مثال: <code>/compare تهران مشهد</code>" if u["lang"]=="fa"
               else "Example: <code>/compare Tehran Mashhad</code>")
        await ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))
        return
    load = "⏳ در حال مقایسه..." if u["lang"]=="fa" else "⏳ Comparing..."
    msg = await ctx.bot.send_message(chat_id=chat_id, text=load, reply_markup=kb_main(u["lang"]))
    try:
        c1 = await geocode(cities[0], u["lang"]); c2 = await geocode(cities[1], u["lang"])
        if not c1 or not c2:
            await msg.edit_text("❌ یکی از شهرها پیدا نشد" if u["lang"]=="fa" else "❌ One city not found"); return
        (w1, a1), (w2, a2) = await asyncio.gather(
            fetch_all(c1["latitude"], c1["longitude"]),
            fetch_all(c2["latitude"], c2["longitude"]))
        if not w1 or not w2:
            await msg.edit_text("⚠️ خطا" if u["lang"]=="fa" else "⚠️ Error"); return
        def nm(c): return (c.get("name_fa") or c["name"]) if u["lang"]=="fa" else c["name"]
        t1, t2 = w1["current"]["temperature_2m"], w2["current"]["temperature_2m"]
        diff = t1 - t2

        if u["lang"] == "fa":
            L = [f"⚖️ <b>مقایسه دمای دو شهر</b>", "",
                 f"📍 <b>{nm(c1)}</b>", f"  🌡️ دما: <b>{fmt_t(t1, u['unit'])}</b>",
                 f"  💧 رطوبت: {w1['current']['relative_humidity_2m']}%",
                 f"  💨 باد: {round(w1['current']['wind_speed_10m'])} km/h"]
            if a1 and a1.get("european_aqi"):
                info1 = aqi_i(a1["european_aqi"], "fa")
                L.append(f"  🌫️ AQI: {info1['ic']} {a1['european_aqi']} ({info1['lbl']})")
            L += ["", f"📍 <b>{nm(c2)}</b>", f"  🌡️ دما: <b>{fmt_t(t2, u['unit'])}</b>",
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
                 f"📍 <b>{nm(c1)}</b>", f"  🌡️ {fmt_t(t1, u['unit'])}",
                 f"  💧 {w1['current']['relative_humidity_2m']}%",
                 f"  💨 {round(w1['current']['wind_speed_10m'])} km/h",
                 "", f"📍 <b>{nm(c2)}</b>", f"  🌡️ {fmt_t(t2, u['unit'])}",
                 f"  💧 {w2['current']['relative_humidity_2m']}%",
                 f"  💨 {round(w2['current']['wind_speed_10m'])} km/h",
                 "", "━━━━━━━━━━━━━━━━━━━━"]
            if abs(diff) < 0.5: L.append("🤝 Similar temperature")
            elif diff > 0: L.append(f"📈 <b>{nm(c1)}</b> is <b>{round(abs(diff))}°</b> warmer")
            else: L.append(f"📈 <b>{nm(c2)}</b> is <b>{round(abs(diff))}°</b> warmer")

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        stat("compare_requests")
    except Exception as e:
        log_err(uid, e, "snd_compare")
        try: await msg.edit_text(f"❌ {str(e)[:80]}")
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
    s = get_user(u["user_id"])
    lang = u["lang"]
    nst = "✅ روشن" if s.get("notify_enabled") else "❌ خاموش"
    nst_en = "ON ✅" if s.get("notify_enabled") else "OFF ❌"
    has_default = s.get("default_city") or ("تنظیم نشده" if lang == "fa" else "not set")

    if lang == "fa":
        txt = (f"🎛️ <b>منوی ربات</b>\n\n🏙️ شهر پیش‌فرض: <b>{has_default}</b>\n"
               f"🔔 اعلان روزانه: <b>{nst}</b>\n⏰ ساعت اعلان: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ واحد دما: <b>{s['unit']}</b>\n🌐 زبان: <b>فارسی</b>")
        rows = [[InlineKeyboardButton("🏙️ تغییر شهر پیش‌فرض", callback_data="m:def")],
                [InlineKeyboardButton("⚖️ مقایسه دمای دو شهر", callback_data="m:cmp")],
                [InlineKeyboardButton(f"🔔 اعلان روزانه ({nst})", callback_data="m:ntf")],
                [InlineKeyboardButton("⏰ تغییر ساعت اعلان", callback_data="m:tim")],
                [InlineKeyboardButton("🌐 زبان", callback_data="m:lng"),
                 InlineKeyboardButton("🌡️ واحد", callback_data="m:unt")]]
    else:
        txt = (f"🎛️ <b>Bot Menu</b>\n\n🏙️ Default: <b>{has_default}</b>\n"
               f"🔔 Notify: <b>{nst_en}</b>\n⏰ Time: <b>{s.get('notify_time','07:00')}</b>\n"
               f"🌡️ Unit: <b>{s['unit']}</b>\n🌐 Language: <b>English</b>")
        rows = [[InlineKeyboardButton("🏙️ Change Default", callback_data="m:def")],
                [InlineKeyboardButton("⚖️ Compare Cities", callback_data="m:cmp")],
                [InlineKeyboardButton(f"🔔 Notify ({nst_en})", callback_data="m:ntf")],
                [InlineKeyboardButton("⏰ Notify Time", callback_data="m:tim")],
                [InlineKeyboardButton("🌐 Language", callback_data="m:lng"),
                 InlineKeyboardButton("🌡️ Unit", callback_data="m:unt")]]
    return txt, rows

def build_admin_panel(u):
    lang = u["lang"]
    s = all_stats()
    total = total_users(); active = active_users_24h()
    errs = recent_errors(3); tops = top_cities(5)

    if lang == "fa":
        txt = (f"🔐 <b>پنل مدیریت</b>\n\n📊 <b>آمار کلی:</b>\n"
               f"  👥 کل کاربران: <b>{total}</b>\n  🟢 فعال ۲۴ ساعت: <b>{active}</b>\n"
               f"  🚀 تعداد اجرا: <b>{s.get('bot_starts',0)}</b>\n\n📈 <b>استفاده:</b>\n"
               f"  🌤️ آب‌وهوا: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ کیفیت هوا: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 پیش‌بینی: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ مقایسه: <b>{s.get('compare_requests',0)}</b>\n\n🔔 <b>اعلان:</b>\n"
               f"  🔛 فعال: <b>{s.get('notify_enabled',0)}</b>\n"
               f"  📨 ارسال‌شده: <b>{s.get('notifications_sent',0)}</b>")
        if tops:
            txt += "\n\n🏆 <b>محبوب‌ترین شهرها:</b>\n"
            medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
            for i, (cn, cnt) in enumerate(tops): txt += f"  {medals[i]} {cn} — <b>{cnt}</b>\n"
        if errs:
            txt += "\n⚠️ <b>خطاهای اخیر:</b>\n"
            for e in errs: txt += f"  <code>{e['error'][:60]}</code>\n"
        txt += "\n🕐 <i>نشست ۱ ساعته</i>"
        rows = [[InlineKeyboardButton("🔄 رفرش آمار", callback_data="a:refresh")],
                [InlineKeyboardButton("🚪 خروج", callback_data="a:logout")]]
    else:
        txt = (f"🔐 <b>Admin Panel</b>\n\n📊 <b>Overview:</b>\n"
               f"  👥 Users: <b>{total}</b>\n  🟢 Active 24h: <b>{active}</b>\n"
               f"  🚀 Starts: <b>{s.get('bot_starts',0)}</b>\n\n📈 <b>Usage:</b>\n"
               f"  🌤️ Weather: <b>{s.get('weather_requests',0)}</b>\n"
               f"  🌫️ AQI: <b>{s.get('aqi_requests',0)}</b>\n"
               f"  📅 Forecast: <b>{s.get('forecast_requests',0)}</b>\n"
               f"  ⚖️ Compare: <b>{s.get('compare_requests',0)}</b>\n\n🔔 <b>Notify:</b>\n"
               f"  🔛 Enabled: <b>{s.get('notify_enabled',0)}</b>\n"
               f"  📨 Sent: <b>{s.get('notifications_sent',0)}</b>")
        rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="a:refresh")],
                [InlineKeyboardButton("🚪 Logout", callback_data="a:logout")]]
    return txt, rows

# ==================== دستورات ====================
async def cmd_start(update, ctx):
    u = get_user(update.effective_user.id, update)
    save_user(u["user_id"], pending_action=None)
    lang = u["lang"]
    if lang == "fa":
        txt = ("🌤️ <b>به ربات هوشمند آب‌وهوا خوش آمدید!</b>\n\n"
               "🔹 <b>راحت‌ترین راه:</b>\nفقط اسم شهر رو بنویس و بفرست! 🚀\n\n"
               "🎛️ برای همه امکانات، دکمه <b>منو</b> رو بزن.\n\n"
               "💡 مثلاً: <code>تهران</code> یا <code>Paris</code>")
    else:
        txt = ("🌤️ <b>Welcome!</b>\n\n🔹 Just type a city name and send! 🚀\n\n"
               "🎛️ Tap <b>Menu</b> for all features.\n\n💡 Example: <code>Tehran</code>")
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb_main(lang))

async def cmd_weather(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_weather(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("اسم شهر رو بنویس" if u["lang"]=="fa" else "Type city name",
                                        reply_markup=kb_main(u["lang"])); return
    await snd_weather(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)

async def cmd_aqi(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_aqi(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("مثال: /aqi تهران", reply_markup=kb_main(u["lang"])); return
    await snd_aqi(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)

async def cmd_forecast(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        if u.get("default_city"):
            await snd_forecast(ctx, update.effective_chat.id, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("مثال: /forecast تهران", reply_markup=kb_main(u["lang"])); return
    await snd_forecast(ctx, update.effective_chat.id, u["user_id"], " ".join(ctx.args), u)

async def cmd_compare(update, ctx):
    u = get_user(update.effective_user.id, update)
    await snd_compare(ctx, update.effective_chat.id, u["user_id"], ctx.args, u)

async def cmd_default(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        msg = (f"🏙️ پیش‌فرض: <b>{u.get('default_city') or 'تنظیم نشده'}</b>" if u["lang"]=="fa"
               else f"🏙️ Default: <b>{u.get('default_city') or 'not set'}</b>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    city = await geocode(" ".join(ctx.args), u["lang"])
    if not city:
        await update.message.reply_text("❌ پیدا نشد", reply_markup=kb_main(u["lang"])); return
    save_user(u["user_id"], default_city=city["name"])
    await update.message.reply_text(f"✅ <b>{city['name']}</b>", parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"]))

async def cmd_notify(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        s = "روشن ✅" if u.get("notify_enabled") else "خاموش ❌"
        await update.message.reply_text(f"🔔 اعلان: <b>{s}</b>\n⏰ <b>{u.get('notify_time','07:00')}</b>",
                                        parse_mode=ParseMode.HTML, reply_markup=kb_main(u["lang"])); return
    a = ctx.args[0].lower()
    if a in ("on","روشن"):
        if not u.get("default_city"):
            await update.message.reply_text("⚠️ اول /default تهران", reply_markup=kb_main(u["lang"])); return
        save_user(u["user_id"], notify_enabled=1)
        await update.message.reply_text("✅ روشن شد", reply_markup=kb_main(u["lang"])); stat("notify_enabled")
    elif a in ("off","خاموش"):
        save_user(u["user_id"], notify_enabled=0)
        await update.message.reply_text("❌ خاموش شد", reply_markup=kb_main(u["lang"]))

async def cmd_settime(update, ctx):
    u = get_user(update.effective_user.id, update)
    if not ctx.args:
        await update.message.reply_text("مثال: /settime 07:30", reply_markup=kb_main(u["lang"])); return
    try:
        h, m = map(int, ctx.args[0].split(":"))
        if not (0<=h<24 and 0<=m<60): raise ValueError
        t = f"{h:02d}:{m:02d}"
        save_user(u["user_id"], notify_time=t)
        await update.message.reply_text(f"✅ {t}", reply_markup=kb_main(u["lang"]))
    except Exception:
        await update.message.reply_text("❌ فرمت اشتباه", reply_markup=kb_main(u["lang"]))

async def on_location(update, ctx):
    u = get_user(update.effective_user.id, update)
    loc = update.message.location
    if not loc: return
    msg = await update.message.reply_text("⏳ در حال دریافت...")
    try:
        w, a = await fetch_all(loc.latitude, loc.longitude)
        if not w:
            await msg.edit_text("⚠️ خطا"); return
        city = {"name": "موقعیت شما" if u["lang"]=="fa" else "Your Location",
                "latitude": loc.latitude, "longitude": loc.longitude}
        await msg.edit_text(build_w(city, w, a, u["lang"], u["unit"]), parse_mode=ParseMode.HTML)
        await update.effective_chat.send_message("🔽", reply_markup=kb_main(u["lang"]))
    except Exception as e:
        log_err(u["user_id"], e, "location")

# ==================== هندلر متن ====================
async def on_text(update, ctx):
    u = get_user(update.effective_user.id, update)
    t = update.message.text.strip()
    lang = u["lang"]
    chat_id = update.effective_chat.id

    # پنل ادمین
    if t in ["🔐 پنل مدیریت", "🔐 Admin Panel"]:
        try: await update.message.delete()
        except Exception: pass
        if is_admin(u["user_id"]):
            txt, rows = build_admin_panel(u)
            await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(rows))
            return
        save_user(u["user_id"], pending_action="admin_pass")
        msg = ("🔐 <b>ورود به پنل مدیریت</b>\n\nرمز عبور رو وارد کن:\n<i>(/start برای انصراف)</i>"
               if lang=="fa" else "🔐 <b>Admin Login</b>\n\nEnter password:")
        await ctx.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        return

    # منو
    if t in ["🎛️ منو","🎛️ Menu"]:
        try: await update.message.delete()
        except Exception: pass
        txt, rows = build_menu(u)
        await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup(rows))
        return

    # دکمه‌ها
    if t in ["🌤️ آب‌وهوا","🌤️ Weather"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_weather(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="w")
        await ctx.bot.send_message(chat_id=chat_id, text="🏙️ اسم شهر:" if lang=="fa" else "🏙️ City:",
                                   reply_markup=kb_main(lang)); return

    if t in ["🌫️ کیفیت هوا","🌫️ Air Quality"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_aqi(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="a")
        await ctx.bot.send_message(chat_id=chat_id, text="🌫️ اسم شهر:", reply_markup=kb_main(lang)); return

    if t in ["📅 پیش‌بینی","📅 Forecast"]:
        try: await update.message.delete()
        except Exception: pass
        if u.get("default_city"):
            await snd_forecast(ctx, chat_id, u["user_id"], u["default_city"], u); return
        save_user(u["user_id"], pending_action="f")
        await ctx.bot.send_message(chat_id=chat_id, text="📅 اسم شهر:", reply_markup=kb_main(lang)); return

    # pending_action
    u2 = get_user(u["user_id"])
    act = u2.get("pending_action")

    if act == "admin_pass":
        save_user(u["user_id"], pending_action=None)
        try: await update.message.delete()
        except Exception: pass
        if t == ADMIN_PASSWORD:
            login_admin(u["user_id"])
            await ctx.bot.send_message(chat_id=chat_id, text="✅ ورود موفق" if lang=="fa" else "✅ Login OK")
            u3 = get_user(u["user_id"])
            txt, rows = build_admin_panel(u3)
            await ctx.bot.send_message(chat_id=chat_id, text=txt, parse_mode=ParseMode.HTML,
                                       reply_markup=InlineKeyboardMarkup(rows))
        else:
            await ctx.bot.send_message(chat_id=chat_id, text="❌ رمز اشتباه" if lang=="fa" else "❌ Wrong")
        return

    if act:
        try: await update.message.delete()
        except Exception: pass
        save_user(u["user_id"], pending_action=None)
        if act == "def":
            c = await geocode(t, lang)
            if not c:
                await ctx.bot.send_message(chat_id=chat_id, text="❌ پیدا نشد", reply_markup=kb_main(lang)); return
            save_user(u["user_id"], default_city=c["name"])
            u3 = get_user(u["user_id"])
            txt, rows = build_menu(u3)
            await ctx.bot.send_message(chat_id=chat_id, text=f"✅ <b>{c['name']}</b>\n\n{txt}",
                                       parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
            return
        if act == "c1":
            save_user(u["user_id"], compare_temp=t, pending_action="c2")
            await ctx.bot.send_message(chat_id=chat_id, text=f"✅ {t}\n\n⚖️ شهر دوم:", parse_mode=ParseMode.HTML)
            return
        if act == "c2":
            c1 = u2.get("compare_temp")
            save_user(u["user_id"], compare_temp=None)
            await snd_compare(ctx, chat_id, u["user_id"], [c1, t], u); return
        if act == "w": await snd_weather(ctx, chat_id, u["user_id"], t, u); return
        if act == "a": await snd_aqi(ctx, chat_id, u["user_id"], t, u); return
        if act == "f": await snd_forecast(ctx, chat_id, u["user_id"], t, u); return

    # اسم شهر عادی
    try: await update.message.delete()
    except Exception: pass
    await snd_weather(ctx, chat_id, u["user_id"], t, u)

# ==================== Callbacks ====================
async def on_cb(update, ctx):
    q = update.callback_query
    u = get_user(q.from_user.id, update)
    d = q.data; lang = u["lang"]
    chat_id = q.message.chat.id

    if d == "a:refresh":
        if not is_admin(u["user_id"]):
            await q.answer("⛔", show_alert=True); return
        await q.answer("🔄")
        txt, rows = build_admin_panel(u)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "a:logout":
        logout_admin(u["user_id"])
        await q.answer("🚪")
        try: await q.edit_message_text("🚪 خارج شدی" if lang=="fa" else "🚪 Logged out")
        except Exception: pass
        return

    if d.startswith("lang:"):
        nl = d.split(":")[1]
        save_user(u["user_id"], lang=nl)
        await q.answer("✅")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        await ctx.bot.send_message(chat_id=chat_id, text="🔽", reply_markup=kb_main(nl))
        return

    if d.startswith("unit:"):
        un = d.split(":")[1]
        save_user(u["user_id"], unit=un)
        await q.answer(f"✅ {un}")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        return

    if d == "m:def":
        save_user(u["user_id"], pending_action="def")
        await q.answer()
        await ctx.bot.send_message(chat_id=chat_id, text="🏙️ اسم شهر جدید:" if lang=="fa" else "🏙️ New city:")
        return

    if d == "m:cmp":
        save_user(u["user_id"], pending_action="c1", compare_temp=None)
        await q.answer()
        await ctx.bot.send_message(chat_id=chat_id, text="⚖️ شهر اول:" if lang=="fa" else "⚖️ City 1:")
        return

    if d == "m:ntf":
        s = get_user(u["user_id"])
        if not s.get("default_city"):
            await q.answer("⚠️ اول پیش‌فرض" if lang=="fa" else "⚠️ Set default", show_alert=True); return
        new = 0 if s.get("notify_enabled") else 1
        save_user(u["user_id"], notify_enabled=new)
        if new: stat("notify_enabled")
        u2 = get_user(u["user_id"])
        txt, rows = build_menu(u2)
        try: await q.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        except Exception: pass
        await q.answer("✅ روشن" if new else "🔕 خاموش", show_alert=False)
        return

    if d == "m:tim":
        await q.answer()
        times = ["06:00","07:00","08:00","09:00","12:00","15:00","18:00","21:00"]
        rows = [[InlineKeyboardButton(tt, callback_data=f"st:{tt}") for tt in times[i:i+4]] for i in range(0,len(times),4)]
        rows.append([InlineKeyboardButton("🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="m:home")])
        await q.edit_message_text("⏰ ساعت:" if lang=="fa" else "⏰ Time:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if d.startswith("st:"):
        tt = d.split(":")[1] + ":00"
        save_user(u["user_id"], notify_time=tt)
        await q.answer(f"✅ {tt}")
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
        rows = [[InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
                 InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
                [InlineKeyboardButton("🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="m:home")]]
        await q.edit_message_text("🌐 زبان:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if d == "m:unt":
        await q.answer()
        rows = [[InlineKeyboardButton("🌡️ °C", callback_data="unit:C"),
                 InlineKeyboardButton("🌡️ °F", callback_data="unit:F")],
                [InlineKeyboardButton("🔙 بازگشت" if lang=="fa" else "🔙 Back", callback_data="m:home")]]
        await q.edit_message_text("🌡️ واحد:", reply_markup=InlineKeyboardMarkup(rows))
        return

# ==================== اعلان ====================
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
                L = ["☀️ <b>صبح بخیر!</b>", "", f"{ic} <b>{cname}</b>",
                     f"🌡️ <b>{fmt_t(c['temperature_2m'],un)}</b>",
                     f"📝 {WMO.get(c['weather_code'],{}).get('fa','?')}", ""]
                tips = smart_tips(w, a, "fa")
                if tips:
                    L.append("💡 <b>توصیه:</b>")
                    for tp in tips: L.append(f"  {tp}")
            else:
                L = ["☀️ <b>Good morning!</b>", "", f"{ic} <b>{cname}</b>",
                     f"🌡️ <b>{fmt_t(c['temperature_2m'],un)}</b>", ""]
                tips = smart_tips(w, a, "en")
                if tips:
                    L.append("💡 <b>Advice:</b>")
                    for tp in tips: L.append(f"  {tp}")
            await ctx.bot.send_message(chat_id=ud["user_id"], text="\n".join(L), parse_mode=ParseMode.HTML)
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
