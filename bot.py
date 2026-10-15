#!/usr/bin/env python3
"""ربات تلگرام آب‌وهوا — نسخه پیشرفته"""

import os, logging, asyncio, time, sqlite3, json
from datetime import datetime, timedelta, time as dtime
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
WEB_APP_URL = os.environ.get("WEB_APP_URL", "https://t.me")  # آدرس سایتت رو بذار
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AQI_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
DB_PATH = "weather_bot.db"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== دیتابیس ====================
def db_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    c = db_conn()
    cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, lang TEXT DEFAULT 'fa', unit TEXT DEFAULT 'C',
        default_city TEXT, notify_enabled INTEGER DEFAULT 0, notify_time TEXT DEFAULT '07:00',
        theme TEXT DEFAULT 'auto', username TEXT, first_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS favorites(
        user_id INTEGER, city_name TEXT, lat REAL, lon REAL, added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(user_id, lat, lon))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER DEFAULT 0)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS errors(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, error TEXT, context TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.commit()
    c.close()
    log_stat("bot_starts")


def log_stat(key: str, inc: int = 1):
    try:
        c = db_conn()
        c.execute("INSERT INTO stats(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+?",
                  (key, inc, inc))
        c.commit()
        c.close()
    except Exception:
        pass


def get_stat(key: str) -> int:
    try:
        c = db_conn()
        r = c.execute("SELECT value FROM stats WHERE key=?", (key,)).fetchone()
        c.close()
        return r["value"] if r else 0
    except Exception:
        return 0


def get_all_stats() -> dict:
    try:
        c = db_conn()
        rows = c.execute("SELECT key,value FROM stats").fetchall()
        c.close()
        return {r["key"]: r["value"] for r in rows}
    except Exception:
        return {}


def log_error(user_id: Optional[int], error: str, context: str = ""):
    try:
        c = db_conn()
        c.execute("INSERT INTO errors(user_id,error,context) VALUES(?,?,?)",
                  (user_id, str(error)[:500], str(context)[:200]))
        # Keep only last 500 errors
        c.execute("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY id DESC LIMIT 500)")
        c.commit()
        c.close()
    except Exception:
        pass


def get_recent_errors(limit: int = 10) -> list:
    try:
        c = db_conn()
        rows = c.execute("SELECT user_id,error,context,created_at FROM errors ORDER BY id DESC LIMIT ?",
                         (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ==================== تنظیمات کاربران ====================
_user_cache = {}


def get_user(user_id: int, update: Update = None) -> dict:
    """گرفتن یا ساخت تنظیمات کاربر (با کش)"""
    if user_id in _user_cache:
        u = _user_cache[user_id].copy()
    else:
        c = db_conn()
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if r:
            u = dict(r)
        else:
            c.execute("INSERT INTO users(user_id) VALUES(?)", (user_id,))
            c.commit()
            u = {"user_id": user_id, "lang": "fa", "unit": "C", "default_city": None,
                 "notify_enabled": 0, "notify_time": "07:00", "theme": "auto"}
            log_stat("new_users")
        c.close()

    if update:
        try:
            c = db_conn()
            c.execute("UPDATE users SET last_seen=CURRENT_TIMESTAMP, username=?, first_name=? WHERE user_id=?",
                      (update.effective_user.username or "", update.effective_user.first_name or "", user_id))
            c.commit()
            c.close()
        except Exception:
            pass

    _user_cache[user_id] = u
    return u.copy()


def save_user(user_id: int, **kwargs):
    try:
        c = db_conn()
        fields = ", ".join(f"{k}=?" for k in kwargs.keys())
        c.execute(f"UPDATE users SET {fields} WHERE user_id=?", list(kwargs.values()) + [user_id])
        c.commit()
        c.close()
        if user_id in _user_cache:
            _user_cache[user_id].update(kwargs)
    except Exception as e:
        logger.error(f"save_user error: {e}")


def get_favorites(user_id: int) -> list:
    try:
        c = db_conn()
        rows = c.execute("SELECT city_name, lat, lon FROM favorites WHERE user_id=? ORDER BY added_at",
                         (user_id,)).fetchall()
        c.close()
        return [{"name": r["city_name"], "lat": r["lat"], "lon": r["lon"]} for r in rows]
    except Exception:
        return []


def add_favorite(user_id: int, name: str, lat: float, lon: float) -> bool:
    try:
        c = db_conn()
        c.execute("INSERT OR IGNORE INTO favorites(user_id,city_name,lat,lon) VALUES(?,?,?,?)",
                  (user_id, name, lat, lon))
        c.commit()
        c.close()
        log_stat("favorites_added")
        return True
    except Exception:
        return False


def remove_favorite(user_id: int, lat: float, lon: float) -> bool:
    try:
        c = db_conn()
        c.execute("DELETE FROM favorites WHERE user_id=? AND ABS(lat-?)<0.01 AND ABS(lon-?)<0.01",
                  (user_id, lat, lon))
        c.commit()
        c.close()
        return True
    except Exception:
        return False


def is_favorite(user_id: int, lat: float, lon: float) -> bool:
    try:
        c = db_conn()
        r = c.execute("SELECT 1 FROM favorites WHERE user_id=? AND ABS(lat-?)<0.01 AND ABS(lon-?)<0.01",
                      (user_id, lat, lon)).fetchone()
        c.close()
        return r is not None
    except Exception:
        return False


def get_users_with_notify() -> list:
    try:
        c = db_conn()
        rows = c.execute("SELECT user_id,lang,unit,default_city,notify_time FROM users WHERE notify_enabled=1").fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_total_users() -> int:
    try:
        c = db_conn()
        r = c.execute("SELECT COUNT(*) as n FROM users").fetchone()
        c.close()
        return r["n"]
    except Exception:
        return 0


# ==================== HTTP Client با Connection Pool ====================
_http_client: Optional[httpx.AsyncClient] = None


async def get_http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            headers={"User-Agent": "WeatherBot/2.0"},
        )
    return _http_client


# ==================== کش ====================
_geo_cache, _geo_time = {}, {}
_weather_cache = {}
CACHE_TTL = 300


def _cache_get(cache, time_cache, key, ttl):
    if key in cache and time.time() - time_cache.get(key, 0) < ttl:
        return cache[key]
    cache.pop(key, None)
    time_cache.pop(key, None)
    return None


# ==================== شهرها ====================
IRAN_CITIES = [
    {"name": "Tehran", "name_fa": "تهران", "lat": 35.6892, "lon": 51.3890},
    {"name": "Mashhad", "name_fa": "مشهد", "lat": 36.2605, "lon": 59.6168},
    {"name": "Isfahan", "name_fa": "اصفهان", "lat": 32.6546, "lon": 51.6680},
    {"name": "Shiraz", "name_fa": "شیراز", "lat": 29.5918, "lon": 52.5837},
    {"name": "Tabriz", "name_fa": "تبریز", "lat": 38.0962, "lon": 46.2738},
    {"name": "Ahvaz", "name_fa": "اهواز", "lat": 31.3167, "lon": 48.6844},
    {"name": "Qom", "name_fa": "قم", "lat": 34.6416, "lon": 50.8746},
    {"name": "Karaj", "name_fa": "کرج", "lat": 35.8355, "lon": 50.9915},
    {"name": "Ardabil", "name_fa": "اردبیل", "lat": 38.2494, "lon": 48.3014},
    {"name": "Sari", "name_fa": "ساری", "lat": 36.5633, "lon": 53.0601},
    {"name": "Arak", "name_fa": "اراک", "lat": 34.1380, "lon": 49.8470},
    {"name": "Urmia", "name_fa": "ارومیه", "lat": 37.5553, "lon": 45.0725},
    {"name": "Gorgan", "name_fa": "گرگان", "lat": 36.8378, "lon": 54.4414},
    {"name": "Rasht", "name_fa": "رشت", "lat": 37.2808, "lon": 49.5832},
    {"name": "Kerman", "name_fa": "کرمان", "lat": 30.2839, "lon": 57.0834},
    {"name": "Yazd", "name_fa": "یزد", "lat": 31.8974, "lon": 54.3569},
    {"name": "Kermanshah", "name_fa": "کرمانشاه", "lat": 34.3142, "lon": 47.0650},
    {"name": "Zahedan", "name_fa": "زاهدان", "lat": 29.4963, "lon": 60.8629},
    {"name": "Hamadan", "name_fa": "همدان", "lat": 34.7983, "lon": 48.5148},
    {"name": "Bandar Abbas", "name_fa": "بندرعباس", "lat": 27.1832, "lon": 56.2666},
    {"name": "Bushehr", "name_fa": "بوشهر", "lat": 28.9234, "lon": 50.8200},
    {"name": "Qazvin", "name_fa": "قزوین", "lat": 36.2688, "lon": 50.0041},
    {"name": "Sanandaj", "name_fa": "سنندج", "lat": 35.3149, "lon": 46.9921},
    {"name": "Khorramabad", "name_fa": "خرم‌آباد", "lat": 33.4878, "lon": 48.3558},
    {"name": "Birjand", "name_fa": "بیرجند", "lat": 32.8663, "lon": 59.2211},
    {"name": "Bojnord", "name_fa": "بجنورد", "lat": 37.4747, "lon": 57.3290},
    {"name": "Ilam", "name_fa": "ایلام", "lat": 33.6374, "lon": 46.4227},
    {"name": "Semnan", "name_fa": "سمنان", "lat": 35.5769, "lon": 53.3953},
    {"name": "Zanjan", "name_fa": "زنجان", "lat": 36.6769, "lon": 48.4963},
    {"name": "Kashan", "name_fa": "کاشان", "lat": 33.9850, "lon": 51.4100},
    {"name": "Maragheh", "name_fa": "مراغه", "lat": 37.3892, "lon": 46.2382},
    {"name": "Bandar Anzali", "name_fa": "بندر انزلی", "lat": 37.4739, "lon": 49.4578},
    {"name": "Chabahar", "name_fa": "چابهار", "lat": 25.2919, "lon": 60.6431},
    {"name": "Kish", "name_fa": "کیش", "lat": 26.5578, "lon": 53.9803},
    {"name": "Qeshm", "name_fa": "قشم", "lat": 26.9581, "lon": 56.2719},
    {"name": "Yasuj", "name_fa": "یاسوج", "lat": 30.6681, "lon": 51.5881},
    {"name": "Shahr-e Kord", "name_fa": "شهرکرد", "lat": 32.3260, "lon": 50.8644},
]
PERSIAN_MAP = {c["name_fa"]: c["name"] for c in IRAN_CITIES}

# ==================== ترجمه آب‌وهوا ====================
WMO_DESC = {
    0: {"en": "Clear sky", "fa": "آسمان صاف", "icon": "☀️", "theme": "sunny"},
    1: {"en": "Mainly clear", "fa": "تقریباً صاف", "icon": "🌤️", "theme": "sunny"},
    2: {"en": "Partly cloudy", "fa": "نیمه ابری", "icon": "⛅", "theme": "cloudy"},
    3: {"en": "Overcast", "fa": "ابری", "icon": "☁️", "theme": "cloudy"},
    45: {"en": "Fog", "fa": "مه", "icon": "🌁", "theme": "cloudy"},
    48: {"en": "Rime fog", "fa": "مه یخ‌زده", "icon": "🌁", "theme": "cloudy"},
    51: {"en": "Light drizzle", "fa": "نم‌نم باران", "icon": "🌦️", "theme": "rainy"},
    53: {"en": "Drizzle", "fa": "نم‌نم باران", "icon": "🌦️", "theme": "rainy"},
    55: {"en": "Dense drizzle", "fa": "نم‌نم شدید", "icon": "🌧️", "theme": "rainy"},
    61: {"en": "Slight rain", "fa": "باران سبک", "icon": "🌧️", "theme": "rainy"},
    63: {"en": "Moderate rain", "fa": "باران متوسط", "icon": "🌧️", "theme": "rainy"},
    65: {"en": "Heavy rain", "fa": "باران شدید", "icon": "🌧️", "theme": "rainy"},
    71: {"en": "Slight snow", "fa": "برف سبک", "icon": "🌨️", "theme": "snowy"},
    73: {"en": "Snow", "fa": "برف متوسط", "icon": "🌨️", "theme": "snowy"},
    75: {"en": "Heavy snow", "fa": "برف شدید", "icon": "❄️", "theme": "snowy"},
    80: {"en": "Rain showers", "fa": "رگبار", "icon": "🌦️", "theme": "rainy"},
    81: {"en": "Moderate showers", "fa": "رگبار متوسط", "icon": "🌧️", "theme": "rainy"},
    82: {"en": "Violent showers", "fa": "رگبار شدید", "icon": "⛈️", "theme": "stormy"},
    95: {"en": "Thunderstorm", "fa": "رعد و برق", "icon": "⛈️", "theme": "stormy"},
    96: {"en": "Thunderstorm", "fa": "رعد و برق و تگرگ", "icon": "⛈️", "theme": "stormy"},
    99: {"en": "Severe storm", "fa": "رعد و برق شدید", "icon": "⛈️", "theme": "stormy"},
}


# ==================== توابع کمکی ====================
def progress_bar(value: float, max_value: float, length: int = 10) -> str:
    if max_value <= 0: return "▱" * length
    ratio = min(max(value / max_value, 0), 1)
    filled = int(ratio * length)
    return "▰" * filled + "▱" * (length - filled)


def wind_dir_fa(deg: int) -> str:
    return ["شمال", "شمال‌شرق", "شرق", "جنوب‌شرق", "جنوب", "جنوب‌غرب", "غرب", "شمال‌غرب"][int((deg + 22.5) // 45) % 8]


def wind_dir_en(deg: int) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


def uv_level(uv: float, lang: str):
    if uv < 3: return ("پایین", "🟢") if lang == "fa" else ("Low", "🟢")
    if uv < 6: return ("متوسط", "🟡") if lang == "fa" else ("Moderate", "🟡")
    if uv < 8: return ("بالا", "🟠") if lang == "fa" else ("High", "🟠")
    if uv < 11: return ("خیلی بالا", "🔴") if lang == "fa" else ("Very High", "🔴")
    return ("خطرناک", "🟣") if lang == "fa" else ("Extreme", "🟣")


def aqi_info(val: float, lang: str) -> dict:
    levels = [
        (20, {"label": "خوب" if lang == "fa" else "Good", "icon": "🟢",
              "advice": "هوا پاکه! برو بیرون لذت ببر 😎" if lang == "fa" else "Air is clean! Enjoy 🎉"}),
        (40, {"label": "قابل قبول" if lang == "fa" else "Fair", "icon": "🟡",
              "advice": "قابل قبوله ولی گروه‌های حساس احتیاط کنن" if lang == "fa" else "Acceptable, sensitive groups be careful"}),
        (60, {"label": "متوسط" if lang == "fa" else "Moderate", "icon": "🟠",
              "advice": "کودکان و سالمندان فعالیت بیرون رو محدود کنن 😷" if lang == "fa" else "Children and elderly limit outdoor"}),
        (80, {"label": "ضعیف" if lang == "fa" else "Poor", "icon": "🔴",
              "advice": "ماسک بزن و فعالیت بیرون رو کم کن" if lang == "fa" else "Wear mask, reduce outdoor"}),
        (100, {"label": "خیلی ضعیف" if lang == "fa" else "Very Poor", "icon": "🟣",
               "advice": "از خونه بیرون نرو! ماسک بزن" if lang == "fa" else "Don't go out! Wear mask"}),
        (9999, {"label": "خطرناک" if lang == "fa" else "Hazardous", "icon": "⚫",
                "advice": "🚨 در خونه بمون! پنجره‌ها رو ببند" if lang == "fa" else "🚨 Stay in! Close windows"}),
    ]
    for t, info in levels:
        if val <= t: return info
    return levels[-1][1]


def pollutant_level(v, kind: str) -> str:
    if v is None: return "⚪"
    t = {"pm2_5": [10, 25, 50, 75, 100], "pm10": [20, 50, 100, 150, 200],
         "no2": [40, 90, 120, 230, 340], "so2": [20, 80, 250, 350, 500],
         "o3": [60, 100, 140, 180, 240], "co": [4000, 10000, 20000, 35000, 50000]}.get(kind, [50, 100, 150, 200, 300])
    for i, th in enumerate(t):
        if v <= th: return ["🟢", "🟡", "🟠", "🔴", "🟣"][i]
    return "⚫"


def fmt_temp(c: float, unit: str) -> str:
    return f"{round(c * 9/5 + 32)}°F" if unit == "F" else f"{round(c)}°C"


def get_theme_emoji(code: int) -> str:
    theme = WMO_DESC.get(code, {}).get("theme", "cloudy")
    return {"sunny": "🌞", "cloudy": "☁️", "rainy": "🌧️", "snowy": "❄️", "stormy": "⛈️"}.get(theme, "🌡️")


def smart_advice(weather: dict, aqi: Optional[dict], lang: str) -> list:
    """توصیه هوشمند بر اساس آب‌وهوا"""
    advice = []
    c = weather["current"]
    d = weather["daily"]
    temp = c["temperature_2m"]
    feels = c["apparent_temperature"]
    uv = c.get("uv_index", 0) or 0
    rain_prob = d["precipitation_probability_max"][0] if d.get("precipitation_probability_max") else 0

    if lang == "fa":
        if feels >= 40: advice.append("🥵 گرمای شدید! آب زیاد بخور و از خونه بیرون نرو")
        elif feels >= 32: advice.append("☀️ هوا گرمه، حتماً ضدآفتاب بزن")
        elif feels <= 0: advice.append("🥶 سرده! لباس گرم بپوش")
        elif feels <= 10: advice.append("🧥 هوا خنکه، یه کاپشن بردار")
        if rain_prob >= 60: advice.append(f"☔ احتمال باران {rain_prob}% — چتر یادت نره!")
        elif rain_prob >= 30: advice.append(f"🌦️ احتمال باران {rain_prob}% — شاید چتر لازم بشه")
        if uv >= 8: advice.append("☀️ UV خیلی بالا! ضدآفتاب و عینک آفتابی ضروریه")
        elif uv >= 6: advice.append("🕶️ ضدآفتاب بزن")
        if aqi and aqi.get("european_aqi"):
            aqi_v = aqi["european_aqi"]
            if aqi_v >= 60: advice.append(f"😷 کیفیت هوا {aqi_v} — ماسک بزن")
        wind = c["wind_speed_10m"]
        if wind >= 40: advice.append("💨 باد شدیده، مراقب باش")
    else:
        if feels >= 40: advice.append("🥵 Extreme heat! Drink water, stay inside")
        elif feels >= 32: advice.append("☀️ Hot! Wear sunscreen")
        elif feels <= 0: advice.append("🥶 Freezing! Wear warm clothes")
        elif feels <= 10: advice.append("🧥 Chilly, take a jacket")
        if rain_prob >= 60: advice.append(f"☔ Rain {rain_prob}% — take umbrella!")
        elif rain_prob >= 30: advice.append(f"🌦️ Rain chance {rain_prob}%")
        if uv >= 8: advice.append("☀️ High UV! Sunscreen + sunglasses")
        elif uv >= 6: advice.append("🕶️ Wear sunscreen")
        if aqi and aqi.get("european_aqi"):
            aqi_v = aqi["european_aqi"]
            if aqi_v >= 60: advice.append(f"😷 AQI {aqi_v} — wear mask")
        wind = c["wind_speed_10m"]
        if wind >= 40: advice.append("💨 Strong wind, be careful")

    return advice


# ==================== API ====================
async def geocode(query: str, lang: str = "fa") -> Optional[dict]:
    q = query.strip()
    key = q.lower()
    cached = _cache_get(_geo_cache, _geo_time, key, 86400)
    if cached: return cached
    if q in PERSIAN_MAP: q = PERSIAN_MAP[q]
    try:
        client = await get_http()
        r = await client.get(GEO_API, params={"name": q, "count": 5, "language": lang, "format": "json"})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results: return None
        results.sort(key=lambda x: 0 if x.get("country_code") == "IR" else 1)
        res = results[0]
        _geo_cache[key] = res; _geo_time[key] = time.time()
        return res
    except Exception as e:
        logger.error(f"geocode: {e}"); return None


async def fetch_weather(lat, lon, past_days=1):
    params = {"latitude": lat, "longitude": lon,
              "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,pressure_msl,visibility,dew_point_2m,uv_index",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max",
              "timezone": "auto", "forecast_days": 7, "past_days": past_days}
    client = await get_http()
    r = await client.get(WEATHER_API, params=params)
    r.raise_for_status()
    return r.json()


async def fetch_aqi(lat, lon):
    params = {"latitude": lat, "longitude": lon,
              "current": "european_aqi,us_aqi,pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,sulphur_dioxide,ozone,uv_index",
              "timezone": "auto"}
    try:
        client = await get_http()
        r = await client.get(AQI_API, params=params)
        r.raise_for_status()
        return r.json().get("current")
    except Exception:
        return None


async def fetch_all(lat, lon):
    key = f"{lat:.3f},{lon:.3f}"
    cached = _weather_cache.get(key)
    if cached and time.time() - cached["ts"] < CACHE_TTL:
        return cached["w"], cached["a"]
    w, a = await asyncio.gather(fetch_weather(lat, lon), fetch_aqi(lat, lon), return_exceptions=True)
    if isinstance(w, Exception): w = None
    if isinstance(a, Exception): a = None
    if w: _weather_cache[key] = {"w": w, "a": a, "ts": time.time()}
    return w, a


# ==================== ساخت پیام ====================
def build_weather(city: dict, weather: dict, aqi: Optional[dict], lang: str, unit: str, show_yesterday: bool = True) -> str:
    c = weather["current"]; d = weather["daily"]
    code = c["weather_code"]
    wmo = WMO_DESC.get(code, {"en": "Unknown", "fa": "نامشخص", "icon": "❓"})
    theme_emoji = get_theme_emoji(code)
    city_name = (city.get("name_fa") or city.get("name")) if lang == "fa" else city.get("name")
    desc = wmo["fa"] if lang == "fa" else wmo["en"]
    icon = wmo["icon"]

    temp = fmt_temp(c["temperature_2m"], unit)
    feels = fmt_temp(c["apparent_temperature"], unit)
    wind = round(c["wind_speed_10m"])
    wind_fa = wind_dir_fa(c["wind_direction_10m"]); wind_en = wind_dir_en(c["wind_direction_10m"])
    hum = c["relative_humidity_2m"]; press = round(c["pressure_msl"])
    vis = c.get("visibility"); uv = c.get("uv_index"); dew = c.get("dew_point_2m")

    sunrise = sunset = "—"
    try:
        if d.get("sunrise"): sunrise = datetime.fromisoformat(d["sunrise"][0]).strftime("%H:%M")
        if d.get("sunset"): sunset = datetime.fromisoformat(d["sunset"][0]).strftime("%H:%M")
    except Exception: pass

    L = []
    if lang == "fa":
        L.append(f"{theme_emoji} {icon} <b>{city_name}</b>  {theme_emoji}")
        L.append(f"<i>{desc}</i>")
        L.append("")
        L.append(f"🌡️ <b>{temp}</b>  •  حس واقعی: <b>{feels}</b>")
        L.append("")

        # مقایسه با دیروز
        if show_yesterday and d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
            try:
                yesterday_max = d["temperature_2m_max"][0]
                today_max = d["temperature_2m_max"][1]
                diff = today_max - yesterday_max
                if abs(diff) >= 1:
                    arrow = "📈" if diff > 0 else "📉"
                    if diff > 0:
                        L.append(f"{arrow} <b>امروز {round(diff)}° گرم‌تر</b> از دیروزه")
                    else:
                        L.append(f"{arrow} <b>امروز {round(abs(diff))}° سردتر</b> از دیروزه")
                    L.append("")
            except Exception: pass

        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 رطوبت: <b>{hum}%</b>")
        L.append(f"<code>{progress_bar(hum, 100)}</code>")
        L.append(f"💨 باد: <b>{wind} km/h</b> ({wind_fa})")
        L.append(f"<code>{progress_bar(wind, 100)}</code>")
        L.append(f"🔽 فشار: <b>{press}</b> hPa")
        if vis: L.append(f"👁️ دید: <b>{round(vis/1000, 1)}</b> km")
        if dew is not None: L.append(f"💦 نقطه شبنم: <b>{fmt_temp(dew, unit)}</b>")
        if uv is not None:
            lbl, ic = uv_level(uv, "fa")
            L.append(f"☀️ UV: {ic} <b>{lbl}</b> ({uv:.1f})")
        L.append(f"🌅 طلوع: <b>{sunrise}</b>  •  🌇 غروب: <b>{sunset}</b>")

        if aqi and aqi.get("european_aqi") is not None:
            av = aqi["european_aqi"]; info = aqi_info(av, "fa")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ <b>کیفیت هوا</b>: {info['icon']} <b>{info['label']}</b>  •  AQI: <b>{av}</b>")
            L.append(f"<code>{progress_bar(av, 100)}</code>")
            pm25 = aqi.get("pm2_5"); pm10 = aqi.get("pm10")
            if pm25 is not None: L.append(f"{pollutant_level(pm25,'pm2_5')} PM2.5: <b>{round(pm25)}</b> µg/m³")
            if pm10 is not None: L.append(f"{pollutant_level(pm10,'pm10')} PM10: <b>{round(pm10)}</b> µg/m³")

        # توصیه هوشمند
        advice = smart_advice(weather, aqi, "fa")
        if advice:
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append("💡 <b>توصیه هوشمند:</b>")
            for a in advice[:4]: L.append(f"  {a}")

        # پیش‌بینی ۳ روز (از ایندکس 1 چون 0 مربوط به دیروزه)
        L.append("")
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append("📅 <b>پیش‌بینی ۳ روز آینده:</b>")
        L.append("")
        days_fa = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]
        start_idx = 1  # skip yesterday
        for i in range(start_idx, min(start_idx + 3, len(d["time"]))):
            dc = d["weather_code"][i]; dw = WMO_DESC.get(dc, {"fa": "؟", "icon": "❓"})
            dt = datetime.fromisoformat(d["time"][i])
            dn = days_fa[dt.weekday()]; date = dt.strftime("%m/%d")
            tmax = fmt_temp(d["temperature_2m_max"][i], unit)
            tmin = fmt_temp(d["temperature_2m_min"][i], unit)
            rn = d["precipitation_probability_max"][i]
            L.append(f"{dw['icon']} <b>{dn}</b> ({date})")
            L.append(f"   🌡️ {tmin} ~ {tmax}  •  💧 {rn}%")
        L.append("")
        L.append("<i>📡 داده‌ها از Open-Meteo</i>")
    else:
        # English version (compact similar)
        L.append(f"{theme_emoji} {icon} <b>{city_name}</b>  {theme_emoji}")
        L.append(f"<i>{desc}</i>")
        L.append("")
        L.append(f"🌡️ <b>{temp}</b>  •  Feels: <b>{feels}</b>")
        L.append("")
        if show_yesterday and d.get("temperature_2m_max") and len(d["temperature_2m_max"]) >= 2:
            try:
                diff = d["temperature_2m_max"][1] - d["temperature_2m_max"][0]
                if abs(diff) >= 1:
                    arrow = "📈" if diff > 0 else "📉"
                    L.append(f"{arrow} <b>{round(abs(diff))}° {'warmer' if diff>0 else 'colder'}</b> than yesterday")
                    L.append("")
            except Exception: pass
        L.append("━━━━━━━━━━━━━━━━━━━━")
        L.append(f"💧 Humidity: <b>{hum}%</b>")
        L.append(f"<code>{progress_bar(hum, 100)}</code>")
        L.append(f"💨 Wind: <b>{wind} km/h</b> ({wind_en})")
        L.append(f"<code>{progress_bar(wind, 100)}</code>")
        L.append(f"🔽 Pressure: <b>{press}</b> hPa")
        if vis: L.append(f"👁️ Visibility: <b>{round(vis/1000,1)}</b> km")
        if uv is not None:
            lbl, ic = uv_level(uv, "en")
            L.append(f"☀️ UV: {ic} <b>{lbl}</b> ({uv:.1f})")
        L.append(f"🌅 {sunrise}  •  🌇 {sunset}")
        if aqi and aqi.get("european_aqi") is not None:
            av = aqi["european_aqi"]; info = aqi_info(av, "en")
            L.append("")
            L.append("━━━━━━━━━━━━━━━━━━━━")
            L.append(f"🌫️ <b>{info['label']}</b> • AQI: <b>{av}</b>")
        advice = smart_advice(weather, aqi, "en")
        if advice:
            L.append("")
            L.append("💡 <b>Smart advice:</b>")
            for a in advice[:4]: L.append(f"  {a}")
        L.append("")
        L.append("📅 <b>3-day forecast:</b>")
        start_idx = 1
        for i in range(start_idx, min(start_idx + 3, len(d["time"]))):
            dc = d["weather_code"][i]; dw = WMO_DESC.get(dc, {"en": "?", "icon": "❓"})
            dt = datetime.fromisoformat(d["time"][i])
            L.append(f"{dw['icon']} <b>{dt.strftime('%A')}</b> ({dt.strftime('%m/%d')}) 🌡️ {fmt_temp(d['temperature_2m_min'][i], unit)}~{fmt_temp(d['temperature_2m_max'][i], unit)}")
        L.append("")
        L.append("<i>📡 Data from Open-Meteo</i>")

    return "\n".join(L)


# ==================== انیمیشن پیشرفت ====================
async def animate(msg, stages: list, delay: float = 0.35):
    """متن متحرک: پیام‌ها یکی‌یکی نشون داده می‌شن"""
    for s in stages:
        try:
            await msg.edit_text(s)
        except Exception:
            pass
        await asyncio.sleep(delay)


# ==================== ارسال‌ها ====================
async def send_weather_msg(chat, user_id, city_query, u, reply_to=None):
    """ارسال پیام آب‌وهوا با دکمه ذخیره"""
    if reply_to:
        msg = await reply_to.reply_text("⏳ در حال دریافت...")
    else:
        msg = await chat.send_message("⏳ در حال دریافت...")

    try:
        city = await geocode(city_query, u["lang"])
        if not city:
            err = "❌ شهر پیدا نشد" if u["lang"] == "fa" else "❌ City not found"
            await msg.edit_text(err)
            return

        lat, lon = city["latitude"], city["longitude"]

        # انیمیشن
        await animate(msg, ["⏳ در حال دریافت...", "🌡️ در حال پردازش...", "✨ آماده شد!"], 0.25)

        w, a = await fetch_all(lat, lon)
        if not w:
            await msg.edit_text("⚠️ خطا در دریافت داده" if u["lang"] == "fa" else "⚠️ Failed to fetch")
            return

        text = build_weather(city, w, a, u["lang"], u["unit"])
        saved = is_favorite(user_id, lat, lon)
        lat_s, lon_s = f"{lat:.4f}", f"{lon:.4f}"
        cname = city["name"]

        if saved:
            btn = "🗑️ حذف" if u["lang"] == "fa" else "🗑️ Remove"
            cb = f"fav_rm:{lat_s}:{lon_s}:{cname}"
        else:
            btn = "⭐ ذخیره" if u["lang"] == "fa" else "⭐ Save"
            cb = f"fav_add:{lat_s}:{lon_s}:{cname}"

        kb = [[InlineKeyboardButton(btn, callback_data=cb)]]
        if WEB_APP_URL and WEB_APP_URL != "https://t.me":
            kb.append([InlineKeyboardButton(
                "🌐 وب‌سایت کامل" if u["lang"] == "fa" else "🌐 Full Website",
                url=WEB_APP_URL
            )])

        await msg.edit_text(text, parse_mode=ParseMode.HTML,
                            reply_markup=InlineKeyboardMarkup(kb))
        log_stat("weather_requests")
    except Exception as e:
        logger.exception("send_weather")
        log_error(user_id, e, "send_weather_msg")
        try: await msg.edit_text(f"❌ خطا: {str(e)[:100]}")
        except Exception: pass


async def send_aqi_msg(chat, user_id, city_query, u):
    msg = await chat.send_message("⏳ در حال دریافت...")
    try:
        city = await geocode(city_query, u["lang"])
        if not city:
            await msg.edit_text("❌ شهر پیدا نشد" if u["lang"] == "fa" else "❌ Not found"); return
        _, a = await fetch_all(city["latitude"], city["longitude"])
        if not a or a.get("european_aqi") is None:
            await msg.edit_text("⚠️ داده موجود نیست" if u["lang"] == "fa" else "⚠️ No data"); return
        v = a["european_aqi"]; info = aqi_info(v, u["lang"])
        cname = (city.get("name_fa") or city["name"]) if u["lang"] == "fa" else city["name"]
        lang = u["lang"]

        if lang == "fa":
            L = [f"🌫️ <b>کیفیت هوای {cname}</b>", "", f"{info['icon']} <b>{info['label']}</b>",
                 f"📊 AQI: <b>{v}</b>", f"<code>{progress_bar(v,100)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>آلاینده‌ها:</b>", ""]
        else:
            L = [f"🌫️ <b>Air Quality — {cname}</b>", "", f"{info['icon']} <b>{info['label']}</b>",
                 f"📊 AQI: <b>{v}</b>", f"<code>{progress_bar(v,100)}</code>", "",
                 "━━━━━━━━━━━━━━━━━━━━", "🔬 <b>Pollutants:</b>", ""]

        pm25 = a.get("pm2_5"); pm10 = a.get("pm10"); no2 = a.get("nitrogen_dioxide")
        so2 = a.get("sulphur_dioxide"); o3 = a.get("ozone"); co = a.get("carbon_monoxide")
        uv = a.get("uv_index")
        labels = {"pm2_5": "PM2.5", "pm10": "PM10", "no2": "NO₂", "so2": "SO₂", "o3": "O₃", "co": "CO"}
        for k, name in labels.items():
            vv = {"pm2_5": pm25, "pm10": pm10, "no2": no2, "so2": so2, "o3": o3, "co": co}[k]
            if vv is not None:
                L.append(f"{pollutant_level(vv, k)} <b>{name}</b>: {round(vv)} µg/m³")
        if uv is not None:
            lbl, ic = uv_level(uv, lang)
            L.append("")
            L.append(f"☀️ UV: {ic} <b>{lbl}</b> ({uv:.1f})")
        L += ["", "━━━━━━━━━━━━━━━━━━━━", f"💡 <b>{'توصیه' if lang=='fa' else 'Advice'}:</b>",
              f"  {info['advice']}", "", "<i>📡 Open-Meteo AQI</i>"]
        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        log_stat("aqi_requests")
    except Exception as e:
        logger.exception("send_aqi")
        log_error(user_id, e, "send_aqi_msg")
        try: await msg.edit_text(f"❌ {str(e)[:100]}")
        except Exception: pass


async def send_forecast_msg(chat, user_id, city_query, u):
    msg = await chat.send_message("⏳ ...")
    try:
        city = await geocode(city_query, u["lang"])
        if not city:
            await msg.edit_text("❌ پیدا نشد" if u["lang"] == "fa" else "❌ Not found"); return
        w, _ = await fetch_all(city["latitude"], city["longitude"])
        if not w:
            await msg.edit_text("⚠️ خطا"); return
        d = w["daily"]
        cname = (city.get("name_fa") or city["name"]) if u["lang"] == "fa" else city["name"]
        L = [f"📅 <b>{'پیش‌بینی ۷ روزه' if u['lang']=='fa' else '7-day forecast'} — {cname}</b>", ""]
        days_fa = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یکشنبه"]
        start = 1  # skip yesterday
        for i in range(start, min(start + 7, len(d["time"]))):
            dc = d["weather_code"][i]; dw = WMO_DESC.get(dc, {"fa": "؟", "en": "?", "icon": "❓"})
            dt = datetime.fromisoformat(d["time"][i])
            dn = days_fa[dt.weekday()] if u["lang"] == "fa" else dt.strftime("%A")
            desc = dw["fa"] if u["lang"] == "fa" else dw["en"]
            L.append(f"{dw['icon']} <b>{dn}</b> ({dt.strftime('%m/%d')})")
            L.append(f"   🌡️ {fmt_temp(d['temperature_2m_min'][i], u['unit'])} ~ {fmt_temp(d['temperature_2m_max'][i], u['unit'])}  •  💧 {d['precipitation_probability_max'][i]}%")
            L.append(f"   <i>{desc}</i>")
            L.append("")
        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        log_stat("forecast_requests")
    except Exception as e:
        log_error(user_id, e, "send_forecast")
        try: await msg.edit_text(f"❌ {str(e)[:100]}")
        except Exception: pass


async def send_compare_msg(chat, user_id, cities: list, u):
    if len(cities) < 2:
        await chat.send_message("مثال: <code>/compare تهران مشهد</code>" if u["lang"] == "fa"
                                else "Example: <code>/compare Tehran Mashhad</code>",
                                parse_mode=ParseMode.HTML)
        return
    msg = await chat.send_message("⏳ در حال مقایسه...")
    try:
        c1 = await geocode(cities[0], u["lang"]); c2 = await geocode(cities[1], u["lang"])
        if not c1 or not c2:
            await msg.edit_text("❌ یکی از شهرها پیدا نشد" if u["lang"] == "fa" else "❌ City not found"); return
        (w1, a1), (w2, a2) = await asyncio.gather(
            fetch_all(c1["latitude"], c1["longitude"]),
            fetch_all(c2["latitude"], c2["longitude"])
        )
        if not w1 or not w2:
            await msg.edit_text("⚠️ خطا"); return

        def name(c):
            return (c.get("name_fa") or c["name"]) if u["lang"] == "fa" else c["name"]

        t1, t2 = w1["current"]["temperature_2m"], w2["current"]["temperature_2m"]
        diff = t1 - t2
        lang = u["lang"]
        L = [f"⚖️ <b>{'مقایسه شهرها' if lang=='fa' else 'Compare Cities'}</b>", "",
             f"📍 <b>{name(c1)}</b>", f"  🌡️ {fmt_temp(t1, u['unit'])}",
             f"  💧 {w1['current']['relative_humidity_2m']}%",
             f"  💨 {round(w1['current']['wind_speed_10m'])} km/h",
             f"  📝 {WMO_DESC.get(w1['current']['weather_code'], {}).get(lang if lang=='fa' else 'en', '?')}"]
        if a1 and a1.get("european_aqi"):
            info1 = aqi_info(a1["european_aqi"], lang)
            L.append(f"  🌫️ AQI: {info1['icon']} {a1['european_aqi']} ({info1['label']})")
        L += ["", f"📍 <b>{name(c2)}</b>", f"  🌡️ {fmt_temp(t2, u['unit'])}",
              f"  💧 {w2['current']['relative_humidity_2m']}%",
              f"  💨 {round(w2['current']['wind_speed_10m'])} km/h",
              f"  📝 {WMO_DESC.get(w2['current']['weather_code'], {}).get(lang if lang=='fa' else 'en', '?')}"]
        if a2 and a2.get("european_aqi"):
            info2 = aqi_info(a2["european_aqi"], lang)
            L.append(f"  🌫️ AQI: {info2['icon']} {a2['european_aqi']} ({info2['label']})")
        L += ["", "━━━━━━━━━━━━━━━━━━━━"]
        if abs(diff) < 0.5:
            L.append(f"🤝 {'دمای هر دو شهر تقریباً یکسانه' if lang=='fa' else 'Similar temperature'}")
        elif diff > 0:
            L.append(f"📈 <b>{name(c1)}</b> {round(abs(diff))}° {'گرم‌تره' if lang=='fa' else 'warmer'}")
        else:
            L.append(f"📈 <b>{name(c2)}</b> {round(abs(diff))}° {'گرم‌تره' if lang=='fa' else 'warmer'}")

        await msg.edit_text("\n".join(L), parse_mode=ParseMode.HTML)
        log_stat("compare_requests")
    except Exception as e:
        log_error(user_id, e, "send_compare")
        try: await msg.edit_text(f"❌ {str(e)[:100]}")
        except Exception: pass


# ==================== کیبورد پایین ====================
def main_kb(lang: str) -> ReplyKeyboardMarkup:
    if lang == "fa":
        kb = [[KeyboardButton("🌤️ آب‌وهوا"), KeyboardButton("🌫️ کیفیت هوا")],
              [KeyboardButton("📅 پیش‌بینی"), KeyboardButton("⭐ علاقه‌مندی‌ها")],
              [KeyboardButton("📍 موقعیت من"), KeyboardButton("⚙️ تنظیمات")],
              [KeyboardButton("❓ راهنما")]]
        ph = "اسم شهر رو بنویس..."
    else:
        kb = [[KeyboardButton("🌤️ Weather"), KeyboardButton("🌫️ Air Quality")],
              [KeyboardButton("📅 Forecast"), KeyboardButton("⭐ Favorites")],
              [KeyboardButton("📍 My Location"), KeyboardButton("⚙️ Settings")],
              [KeyboardButton("❓ Help")]]
        ph = "Type a city name..."
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, input_field_placeholder=ph)


# ==================== دستورات ====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    lang = u["lang"]
    if lang == "fa":
        text = ("🌤️ <b>به ربات هوشمند آب‌وهوا خوش آمدید!</b>\n\n"
                "🔹 <b>دستورات جدید:</b>\n"
                "/weather — آب‌وهوای شهر پیش‌فرض\n"
                "/compare [شهر۱] [شهر۲] — مقایسه دو شهر\n"
                "/default [شهر] — تعیین شهر پیش‌فرض\n"
                "/notify on/off — اعلان روزانه\n"
                "/settime 07:00 — ساعت اعلان\n"
                "/theme — تم رنگی\n"
                "/stats — آمار ربات\n\n"
                "💡 فقط اسم شهر رو بنویس یا از دکمه‌ها استفاده کن")
    else:
        text = ("🌤️ <b>Welcome to Smart Weather Bot!</b>\n\n"
                "🔹 <b>Commands:</b>\n"
                "/weather — Weather of default city\n"
                "/compare [city1] [city2]\n"
                "/default [city]\n/notify on/off\n/settime 07:00\n/theme\n/stats\n\n"
                "💡 Type a city name or use buttons")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_kb(lang))


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)


async def weather_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await send_weather_msg(update.effective_chat, u["user_id"], u["default_city"], u)
            return
        await update.message.reply_text(
            "اسم شهر رو بنویس یا با /default شهر پیش‌فرض رو تنظیم کن" if u["lang"] == "fa"
            else "Type a city or set default with /default", reply_markup=main_kb(u["lang"]))
        return
    await send_weather_msg(update.effective_chat, u["user_id"], " ".join(args), u)


async def aqi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await send_aqi_msg(update.effective_chat, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("مثال: /aqi تهران" if u["lang"] == "fa" else "Example: /aqi Tehran"); return
    await send_aqi_msg(update.effective_chat, u["user_id"], " ".join(args), u)


async def forecast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            await send_forecast_msg(update.effective_chat, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("مثال: /forecast تهران" if u["lang"] == "fa" else "Example: /forecast Tehran"); return
    await send_forecast_msg(update.effective_chat, u["user_id"], " ".join(args), u)


async def compare_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    await send_compare_msg(update.effective_chat, u["user_id"], args, u)


async def default_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        if u.get("default_city"):
            msg = f"🏙️ شهر پیش‌فرض: <b>{u['default_city']}</b>\n\nبرای تغییر: <code>/default [شهر]</code>" if u["lang"] == "fa" \
                else f"🏙️ Default: <b>{u['default_city']}</b>\n\nChange: <code>/default [city]</code>"
        else:
            msg = "🏙️ هنوز شهر پیش‌فرض تنظیم نکردی\n\nمثال: <code>/default تهران</code>" if u["lang"] == "fa" \
                else "🏙️ No default set\n\nExample: <code>/default Tehran</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
    city_name = " ".join(args)
    city = await geocode(city_name, u["lang"])
    if not city:
        await update.message.reply_text("❌ شهر پیدا نشد" if u["lang"] == "fa" else "❌ Not found"); return
    save_user(u["user_id"], default_city=city["name"])
    msg = f"✅ شهر پیش‌فرض: <b>{city['name']}</b>\n\nحالا /weather بدون اسم، همین شهر رو میاره" if u["lang"] == "fa" \
        else f"✅ Default: <b>{city['name']}</b>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def notify_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        status = "✅ روشن" if u.get("notify_enabled") else "❌ خاموش" if u["lang"] == "fa" else ("✅ On" if u.get("notify_enabled") else "❌ Off")
        msg = (f"🔔 اعلان روزانه: <b>{status}</b>\n"
               f"⏰ ساعت: <b>{u.get('notify_time','07:00')}</b>\n\n"
               "برای تغییر:\n"
               "<code>/notify on</code> — روشن\n"
               "<code>/notify off</code> — خاموش\n"
               "<code>/settime 08:30</code> — تغییر ساعت") if u["lang"] == "fa" else \
              (f"🔔 Daily notify: <b>{status}</b>\n⏰ Time: <b>{u.get('notify_time','07:00')}</b>\n\n"
               "<code>/notify on</code> / <code>/notify off</code>\n<code>/settime 08:30</code>")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
    arg = args[0].lower()
    if arg in ("on", "روشن"):
        if not u.get("default_city"):
            await update.message.reply_text("⚠️ اول شهر پیش‌فرض رو تنظیم کن: /default تهران" if u["lang"] == "fa"
                                            else "⚠️ Set default city first: /default Tehran")
            return
        save_user(u["user_id"], notify_enabled=1)
        msg = f"✅ اعلان روزانه روشن شد\n⏰ هر روز ساعت <b>{u.get('notify_time','07:00')}</b>" if u["lang"] == "fa" \
            else f"✅ Daily notify enabled at <b>{u.get('notify_time','07:00')}</b>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        log_stat("notify_enabled")
    elif arg in ("off", "خاموش"):
        save_user(u["user_id"], notify_enabled=0)
        msg = "❌ اعلان خاموش شد" if u["lang"] == "fa" else "❌ Notifications off"
        await update.message.reply_text(msg)


async def settime_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    args = ctx.args
    if not args:
        await update.message.reply_text("مثال: <code>/settime 07:30</code>" if u["lang"] == "fa"
                                        else "Example: <code>/settime 07:30</code>", parse_mode=ParseMode.HTML)
        return
    try:
        h, m = args[0].split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        tstr = f"{h:02d}:{m:02d}"
        save_user(u["user_id"], notify_time=tstr)
        msg = f"✅ ساعت اعلان: <b>{tstr}</b>" if u["lang"] == "fa" else f"✅ Notify time: <b>{tstr}</b>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("❌ فرمت اشتباه. مثال: <code>07:30</code>" if u["lang"] == "fa"
                                        else "❌ Wrong format. Example: <code>07:30</code>", parse_mode=ParseMode.HTML)


async def theme_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    kb = [
        [InlineKeyboardButton("🎨 خودکار", callback_data="theme:auto"),
         InlineKeyboardButton("☀️ روشن", callback_data="theme:light")],
        [InlineKeyboardButton("🌙 تاریک", callback_data="theme:dark"),
         InlineKeyboardButton("🌈 رنگی", callback_data="theme:colorful")],
    ] if u["lang"] == "fa" else [
        [InlineKeyboardButton("🎨 Auto", callback_data="theme:auto"),
         InlineKeyboardButton("☀️ Light", callback_data="theme:light")],
        [InlineKeyboardButton("🌙 Dark", callback_data="theme:dark"),
         InlineKeyboardButton("🌈 Colorful", callback_data="theme:colorful")],
    ]
    await update.message.reply_text(
        f"🎨 تم فعلی: <b>{u.get('theme','auto')}</b>" if u["lang"] == "fa"
        else f"🎨 Current theme: <b>{u.get('theme','auto')}</b>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    total = get_total_users()
    s = get_all_stats()
    if u["lang"] == "fa":
        text = (f"📊 <b>آمار ربات</b>\n\n"
                f"👥 کاربران: <b>{total}</b>\n"
                f"🌤️ درخواست آب‌وهوا: <b>{s.get('weather_requests',0)}</b>\n"
                f"🌫️ درخواست AQI: <b>{s.get('aqi_requests',0)}</b>\n"
                f"📅 پیش‌بینی: <b>{s.get('forecast_requests',0)}</b>\n"
                f"⚖️ مقایسه: <b>{s.get('compare_requests',0)}</b>\n"
                f"⭐ علاقه‌مندی: <b>{s.get('favorites_added',0)}</b>\n"
                f"🔔 اعلان فعال: <b>{s.get('notify_enabled',0)}</b>\n"
                f"🚀 تعداد اجرا: <b>{s.get('bot_starts',0)}</b>")
    else:
        text = (f"📊 <b>Bot Stats</b>\n\n"
                f"👥 Users: <b>{total}</b>\n"
                f"🌤️ Weather: <b>{s.get('weather_requests',0)}</b>\n"
                f"🌫️ AQI: <b>{s.get('aqi_requests',0)}</b>\n"
                f"📅 Forecast: <b>{s.get('forecast_requests',0)}</b>\n"
                f"⚖️ Compare: <b>{s.get('compare_requests',0)}</b>\n"
                f"⭐ Favorites: <b>{s.get('favorites_added',0)}</b>")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    lang = u["lang"]
    if lang == "fa":
        text = (f"⚙️ <b>تنظیمات</b>\n\n"
                f"🌐 زبان: {'فارسی' if lang=='fa' else 'English'}\n"
                f"🌡️ واحد: {u['unit']}\n"
                f"🏙️ شهر پیش‌فرض: {u.get('default_city') or 'تنظیم نشده'}\n"
                f"🔔 اعلان: {'روشن' if u.get('notify_enabled') else 'خاموش'}\n"
                f"⏰ ساعت اعلان: {u.get('notify_time','07:00')}\n\n"
                "👇")
    else:
        text = (f"⚙️ <b>Settings</b>\n\n"
                f"🌐 Lang: {lang}\n🌡️ Unit: {u['unit']}\n"
                f"🏙️ Default: {u.get('default_city') or 'not set'}\n"
                f"🔔 Notify: {'On' if u.get('notify_enabled') else 'Off'}\n\n👇")
    kb = [
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
        [InlineKeyboardButton("°C", callback_data="unit:C"), InlineKeyboardButton("°F", callback_data="unit:F")],
        [InlineKeyboardButton("🎨 تم" if lang == "fa" else "🎨 Theme", callback_data="theme_menu")],
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_kb(lang))
    await update.message.reply_text("👇", reply_markup=InlineKeyboardMarkup(kb))


async def mycities_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    favs = get_favorites(u["user_id"])
    if not favs:
        msg = ("📭 لیست خالیه!\n\n💡 اسم شهر رو بفرست، بعد روی ⭐ بزن"
               if u["lang"] == "fa" else "📭 Empty!\n\n💡 Send a city, then tap ⭐")
        await update.message.reply_text(msg, reply_markup=main_kb(u["lang"]))
        return
    results = await asyncio.gather(*[fetch_all(f["lat"], f["lon"]) for f in favs], return_exceptions=True)
    L = ["📍 <b>شهرهای ذخیره‌شده:</b>" if u["lang"] == "fa" else "📍 <b>Saved cities:</b>", ""]
    for f, r in zip(favs, results):
        try:
            if isinstance(r, Exception) or not r[0]:
                L.append(f"❓ <b>{f['name']}</b>"); continue
            w = r[0]
            t = fmt_temp(w["current"]["temperature_2m"], u["unit"])
            ic = WMO_DESC.get(w["current"]["weather_code"], {"icon": "❓"})["icon"]
            L.append(f"{ic} <b>{f['name']}</b> — {t}")
        except Exception: L.append(f"❓ <b>{f['name']}</b>")
    await update.message.reply_text("\n".join(L), parse_mode=ParseMode.HTML, reply_markup=main_kb(u["lang"]))


async def location_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    loc = update.message.location
    if not loc: return
    msg = await update.message.reply_text("⏳ ...")
    try:
        w, a = await fetch_all(loc.latitude, loc.longitude)
        if not w:
            await msg.edit_text("⚠️ خطا"); return
        city = {"name": "موقعیت شما" if u["lang"] == "fa" else "Your Location",
                "latitude": loc.latitude, "longitude": loc.longitude}
        text = build_weather(city, w, a, u["lang"], u["unit"])
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        log_error(u["user_id"], e, "location")


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id, update)
    text = update.message.text.strip()
    lang = u["lang"]

    if text in ["🌤️ آب‌وهوا", "🌤️ Weather"]:
        if u.get("default_city"):
            await send_weather_msg(update.effective_chat, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("🏙️ اسم شهر رو بنویس" if lang == "fa" else "🏙️ Type city name")
        return
    if text in ["🌫️ کیفیت هوا", "🌫️ Air Quality"]:
        if u.get("default_city"):
            await send_aqi_msg(update.effective_chat, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("🌫️ اسم شهر رو بنویس" if lang == "fa" else "🌫️ Type city name"); return
    if text in ["📅 پیش‌بینی", "📅 Forecast"]:
        if u.get("default_city"):
            await send_forecast_msg(update.effective_chat, u["user_id"], u["default_city"], u); return
        await update.message.reply_text("📅 اسم شهر رو بنویس" if lang == "fa" else "📅 Type city name"); return
    if text in ["⭐ علاقه‌مندی‌ها", "⭐ Favorites"]:
        await mycities_cmd(update, ctx); return
    if text in ["📍 موقعیت من", "📍 My Location"]:
        msg = ("📍 برای ارسال موقعیت:\n1. 📎 بزن\n2. Location\n3. ارسال" if lang == "fa"
               else "📍 Send location:\n1. Tap 📎\n2. Location\n3. Send")
        await update.message.reply_text(msg, reply_markup=main_kb(lang)); return
    if text in ["⚙️ تنظیمات", "⚙️ Settings"]:
        await settings_cmd(update, ctx); return
    if text in ["❓ راهنما", "❓ Help"]:
        await start(update, ctx); return

    # اسم شهر
    await send_weather_msg(update.effective_chat, u["user_id"], text, u, reply_to=update.message)


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = get_user(q.from_user.id, update)
    data = q.data

    if data.startswith("lang:"):
        lang = data.split(":")[1]
        save_user(u["user_id"], lang=lang)
        await q.edit_message_text("✅" if lang == "fa" else "✅ Language changed")
        await q.message.reply_text("🔽", reply_markup=main_kb(lang)); return

    if data.startswith("unit:"):
        unit = data.split(":")[1]
        save_user(u["user_id"], unit=unit)
        await q.edit_message_text(f"✅ {unit}"); return

    if data == "theme_menu":
        kb = [[InlineKeyboardButton("🎨 Auto", callback_data="theme:auto"),
               InlineKeyboardButton("☀️ Light", callback_data="theme:light")],
              [InlineKeyboardButton("🌙 Dark", callback_data="theme:dark"),
               InlineKeyboardButton("🌈 Colorful", callback_data="theme:colorful")]]
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb)); return

    if data.startswith("theme:"):
        theme = data.split(":")[1]
        save_user(u["user_id"], theme=theme)
        await q.edit_message_text(f"✅ Theme: {theme}"); return

    parts = data.split(":")
    action = parts[0]

    if action == "fav_add" and len(parts) >= 4:
        lat, lon, cname = float(parts[1]), float(parts[2]), parts[3]
        add_favorite(u["user_id"], cname, lat, lon)
        kb = [[InlineKeyboardButton("🗑️ حذف" if u["lang"] == "fa" else "🗑️ Remove",
                                    callback_data=f"fav_rm:{lat}:{lon}:{cname}")]]
        try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        except Exception: pass
        await q.answer(f"✅ {cname}", show_alert=False); return

    if action == "fav_rm" and len(parts) >= 4:
        lat, lon, cname = float(parts[1]), float(parts[2]), parts[3]
        remove_favorite(u["user_id"], lat, lon)
        kb = [[InlineKeyboardButton("⭐ ذخیره" if u["lang"] == "fa" else "⭐ Save",
                                    callback_data=f"fav_add:{lat}:{lon}:{cname}")]]
        try: await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        except Exception: pass
        await q.answer(f"🗑️ {cname}", show_alert=False); return


# ==================== اعلان روزانه ====================
async def daily_notify_job(ctx: ContextTypes.DEFAULT_TYPE):
    """چک هر دقیقه: هر کاربری که ساعتش رسیده، اعلان بفرست"""
    now = datetime.now().strftime("%H:%M")
    users = get_users_with_notify()
    for ud in users:
        if ud.get("notify_time") == now:
            try:
                city_name = ud["default_city"]
                if not city_name: continue
                u = {"user_id": ud["user_id"], "lang": ud["lang"], "unit": ud["unit"]}
                city = await geocode(city_name, u["lang"])
                if not city: continue
                w, a = await fetch_all(city["latitude"], city["longitude"])
                if not w: continue

                lang = u["lang"]
                c = w["current"]; d = w["daily"]
                cname = (city.get("name_fa") or city["name"]) if lang == "fa" else city["name"]
                icon = WMO_DESC.get(c["weather_code"], {"icon": "❓"})["icon"]
                temp = fmt_temp(c["temperature_2m"], u["unit"])

                if lang == "fa":
                    L = [f"☀️ <b>صبح بخیر!</b>", f"",
                         f"{icon} <b>{cname}</b>", f"🌡️ <b>{temp}</b>",
                         f"📝 {WMO_DESC.get(c['weather_code'],{}).get('fa','?')}", ""]
                else:
                    L = [f"☀️ <b>Good morning!</b>", f"",
                         f"{icon} <b>{cname}</b>", f"🌡️ <b>{temp}</b>", ""]

                # توصیه هوشمند
                advice = smart_advice(w, a, lang)
                if advice:
                    L.append(f"💡 <b>{'توصیه' if lang=='fa' else 'Advice'}:</b>")
                    for adv in advice[:3]: L.append(f"  {adv}")
                    L.append("")

                # مقایسه با دیروز
                try:
                    if len(d["temperature_2m_max"]) >= 2:
                        diff = d["temperature_2m_max"][1] - d["temperature_2m_max"][0]
                        if abs(diff) >= 1:
                            if lang == "fa":
                                L.append(f"{'📈' if diff>0 else '📉'} امروز {round(abs(diff))}° {'گرم‌تر' if diff>0 else 'سردتر'} از دیروزه")
                            else:
                                L.append(f"{'📈' if diff>0 else '📉'} {round(abs(diff))}° {'warmer' if diff>0 else 'colder'} than yesterday")
                except Exception: pass

                # AQI
                if a and a.get("european_aqi"):
                    info = aqi_info(a["european_aqi"], lang)
                    L.append(f"🌫️ {info['icon']} {info['label']} ({a['european_aqi']})")

                L.append("")
                L.append(f"<i>{'برای خاموش کردن: /notify off' if lang=='fa' else 'To disable: /notify off'}</i>")

                await ctx.bot.send_message(ud["user_id"], "\n".join(L), parse_mode=ParseMode.HTML)
                log_stat("notifications_sent")
            except Exception as e:
                logger.error(f"notify {ud['user_id']}: {e}")
                log_error(ud["user_id"], e, "daily_notify")


# ==================== main ====================
async def post_init(app: Application):
    init_db()
    await get_http()
    # Job هر 30 ثانیه چک کنه
    if app.job_queue:
        app.job_queue.run_repeating(daily_notify_job, interval=30, first=10)
    logger.info("✅ Bot started")


def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("⚠️ BOT_TOKEN set کن!"); return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("aqi", aqi_cmd))
    app.add_handler(CommandHandler("forecast", forecast_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("default", default_cmd))
    app.add_handler(CommandHandler("notify", notify_cmd))
    app.add_handler(CommandHandler("settime", settime_cmd))
    app.add_handler(CommandHandler("theme", theme_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("mycities", mycities_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.LOCATION, location_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🤖 ربات پیشرفته در حال اجراست...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
