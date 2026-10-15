#!/usr/bin/env python3
"""
ربات تلگرام آب‌وهوا — متصل به Open-Meteo
هماهنگ با سایت Weather & Air Quality
"""

import os
import logging
import asyncio
from datetime import datetime
from typing import Optional

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ============================================================
#  تنظیمات — توکن از Environment Variable خونده می‌شه
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"
AQI_API = "https://air-quality-api.open-meteo.com/v1/air-quality"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
#  شهرهای ایرانی
# ============================================================
IRAN_CITIES = [
    {"name": "Tehran", "name_fa": "تهران", "lat": 35.6892, "lon": 51.3890, "admin": "Tehran"},
    {"name": "Mashhad", "name_fa": "مشهد", "lat": 36.2605, "lon": 59.6168, "admin": "Razavi Khorasan"},
    {"name": "Isfahan", "name_fa": "اصفهان", "lat": 32.6546, "lon": 51.6680, "admin": "Isfahan"},
    {"name": "Shiraz", "name_fa": "شیراز", "lat": 29.5918, "lon": 52.5837, "admin": "Fars"},
    {"name": "Tabriz", "name_fa": "تبریز", "lat": 38.0962, "lon": 46.2738, "admin": "East Azerbaijan"},
    {"name": "Ahvaz", "name_fa": "اهواز", "lat": 31.3167, "lon": 48.6844, "admin": "Khuzestan"},
    {"name": "Qom", "name_fa": "قم", "lat": 34.6416, "lon": 50.8746, "admin": "Qom"},
    {"name": "Karaj", "name_fa": "کرج", "lat": 35.8355, "lon": 50.9915, "admin": "Alborz"},
    {"name": "Ardabil", "name_fa": "اردبیل", "lat": 38.2494, "lon": 48.3014, "admin": "Ardabil"},
    {"name": "Sari", "name_fa": "ساری", "lat": 36.5633, "lon": 53.0601, "admin": "Mazandaran"},
    {"name": "Arak", "name_fa": "اراک", "lat": 34.1380, "lon": 49.8470, "admin": "Markazi"},
    {"name": "Urmia", "name_fa": "ارومیه", "lat": 37.5553, "lon": 45.0725, "admin": "West Azerbaijan"},
    {"name": "Gorgan", "name_fa": "گرگان", "lat": 36.8378, "lon": 54.4414, "admin": "Golestan"},
    {"name": "Rasht", "name_fa": "رشت", "lat": 37.2808, "lon": 49.5832, "admin": "Gilan"},
    {"name": "Kerman", "name_fa": "کرمان", "lat": 30.2839, "lon": 57.0834, "admin": "Kerman"},
    {"name": "Yazd", "name_fa": "یزد", "lat": 31.8974, "lon": 54.3569, "admin": "Yazd"},
    {"name": "Kermanshah", "name_fa": "کرمانشاه", "lat": 34.3142, "lon": 47.0650, "admin": "Kermanshah"},
    {"name": "Zahedan", "name_fa": "زاهدان", "lat": 29.4963, "lon": 60.8629, "admin": "Sistan"},
    {"name": "Hamadan", "name_fa": "همدان", "lat": 34.7983, "lon": 48.5148, "admin": "Hamadan"},
    {"name": "Bandar Abbas", "name_fa": "بندرعباس", "lat": 27.1832, "lon": 56.2666, "admin": "Hormozgan"},
    {"name": "Bushehr", "name_fa": "بوشهر", "lat": 28.9234, "lon": 50.8200, "admin": "Bushehr"},
    {"name": "Qazvin", "name_fa": "قزوین", "lat": 36.2688, "lon": 50.0041, "admin": "Qazvin"},
    {"name": "Sanandaj", "name_fa": "سنندج", "lat": 35.3149, "lon": 46.9921, "admin": "Kurdistan"},
    {"name": "Khorramabad", "name_fa": "خرم‌آباد", "lat": 33.4878, "lon": 48.3558, "admin": "Lorestan"},
    {"name": "Birjand", "name_fa": "بیرجند", "lat": 32.8663, "lon": 59.2211, "admin": "South Khorasan"},
    {"name": "Bojnord", "name_fa": "بجنورد", "lat": 37.4747, "lon": 57.3290, "admin": "North Khorasan"},
    {"name": "Ilam", "name_fa": "ایلام", "lat": 33.6374, "lon": 46.4227, "admin": "Ilam"},
    {"name": "Semnan", "name_fa": "سمنان", "lat": 35.5769, "lon": 53.3953, "admin": "Semnan"},
    {"name": "Zanjan", "name_fa": "زنجان", "lat": 36.6769, "lon": 48.4963, "admin": "Zanjan"},
    {"name": "Kashan", "name_fa": "کاشان", "lat": 33.9850, "lon": 51.4100, "admin": "Isfahan"},
]

PERSIAN_MAP = {c["name_fa"]: c["name"] for c in IRAN_CITIES}

# ============================================================
#  ترجمه کد آب‌وهوا
# ============================================================
WMO_DESC = {
    0: {"en": "Clear sky", "fa": "آسمان صاف", "icon": "☀️"},
    1: {"en": "Mainly clear", "fa": "تقریباً صاف", "icon": "🌤️"},
    2: {"en": "Partly cloudy", "fa": "نیمه ابری", "icon": "⛅"},
    3: {"en": "Overcast", "fa": "ابری", "icon": "☁️"},
    45: {"en": "Fog", "fa": "مه", "icon": "🌁"},
    48: {"en": "Rime fog", "fa": "مه یخ‌زده", "icon": "🌁"},
    51: {"en": "Light drizzle", "fa": "نم‌نم باران", "icon": "🌦️"},
    53: {"en": "Moderate drizzle", "fa": "نم‌نم باران", "icon": "🌦️"},
    55: {"en": "Dense drizzle", "fa": "نم‌نم شدید", "icon": "🌧️"},
    61: {"en": "Slight rain", "fa": "باران سبک", "icon": "🌧️"},
    63: {"en": "Moderate rain", "fa": "باران متوسط", "icon": "🌧️"},
    65: {"en": "Heavy rain", "fa": "باران شدید", "icon": "🌧️"},
    71: {"en": "Slight snow", "fa": "برف سبک", "icon": "🌨️"},
    73: {"en": "Moderate snow", "fa": "برف متوسط", "icon": "🌨️"},
    75: {"en": "Heavy snow", "fa": "برف شدید", "icon": "❄️"},
    80: {"en": "Rain showers", "fa": "رگبار", "icon": "🌦️"},
    81: {"en": "Moderate showers", "fa": "رگبار متوسط", "icon": "🌧️"},
    82: {"en": "Violent showers", "fa": "رگبار شدید", "icon": "⛈️"},
    95: {"en": "Thunderstorm", "fa": "رعد و برق", "icon": "⛈️"},
    96: {"en": "Thunderstorm & hail", "fa": "رعد و برق و تگرگ", "icon": "⛈️"},
    99: {"en": "Severe thunderstorm", "fa": "رعد و برق شدید", "icon": "⛈️"},
}

AQI_LEVELS = [
    (20, {"en": "Good", "fa": "خوب", "icon": "🟢"}),
    (40, {"en": "Fair", "fa": "قابل قبول", "icon": "🟡"}),
    (60, {"en": "Moderate", "fa": "متوسط", "icon": "🟠"}),
    (80, {"en": "Poor", "fa": "ضعیف", "icon": "🔴"}),
    (100, {"en": "Very Poor", "fa": "خیلی ضعیف", "icon": "🟣"}),
    (9999, {"en": "Hazardous", "fa": "خطرناک", "icon": "⚫"}),
]

# ============================================================
#  تنظیمات کاربران (در حافظه)
# ============================================================
user_settings = {}


def get_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = {"lang": "fa", "unit": "C", "cities": []}
    return user_settings[user_id]


# ============================================================
#  API
# ============================================================
async def geocode(query: str, lang: str = "fa") -> Optional[dict]:
    query_norm = query.strip()
    if query_norm in PERSIAN_MAP:
        query_norm = PERSIAN_MAP[query_norm]

    params = {
        "name": query_norm,
        "count": 5,
        "language": lang,
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(GEO_API, params=params)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        if not results:
            return None
        results.sort(key=lambda x: 0 if x.get("country_code") == "IR" else 1)
        return results[0]


async def fetch_weather(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                   "wind_speed_10m,wind_direction_10m,pressure_msl,visibility,dew_point_2m,uv_index",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
                 "precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 7,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(WEATHER_API, params=params)
        r.raise_for_status()
        return r.json()


async def fetch_aqi(lat: float, lon: float) -> Optional[dict]:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "european_aqi,pm2_5,pm10",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(AQI_API, params=params)
            r.raise_for_status()
            return r.json().get("current")
    except Exception as e:
        logger.warning(f"AQI fetch failed: {e}")
        return None


def aqi_info(value: float, lang: str) -> dict:
    for threshold, info in AQI_LEVELS:
        if value <= threshold:
            return info
    return AQI_LEVELS[-1][1]


def fmt_temp(celsius: float, unit: str) -> str:
    if unit == "F":
        return f"{round(celsius * 9/5 + 32)}°F"
    return f"{round(celsius)}°C"


# ============================================================
#  ساخت پیام آب‌وهوا
# ============================================================
def build_weather_message(city: dict, weather: dict, aqi: Optional[dict], lang: str, unit: str) -> str:
    c = weather["current"]
    d = weather["daily"]
    code = c["weather_code"]
    wmo = WMO_DESC.get(code, {"en": "Unknown", "fa": "نامشخص", "icon": "❓"})

    city_name = (city.get("name_fa") or city.get("name")) if lang == "fa" else city.get("name")
    desc = wmo["fa"] if lang == "fa" else wmo["en"]
    icon = wmo["icon"]

    temp = fmt_temp(c["temperature_2m"], unit)
    feels = fmt_temp(c["apparent_temperature"], unit)
    wind = round(c["wind_speed_10m"])

    lines = []

    if lang == "fa":
        lines.append(f"{icon} <b>{city_name}</b>")
        lines.append("")
        lines.append(f"🌡️ دما: <b>{temp}</b>  (حس: {feels})")
        lines.append(f"📝 وضعیت: {desc}")
        lines.append(f"💧 رطوبت: {c['relative_humidity_2m']}%")
        lines.append(f"💨 باد: {wind} km/h ({c['wind_direction_10m']}°)")
        lines.append(f"🔽 فشار: {round(c['pressure_msl'])} hPa")
        if c.get("visibility"):
            lines.append(f"👁️ دید: {round(c['visibility'] / 1000, 1)} km")
        if c.get("uv_index") is not None:
            lines.append(f"☀️ UV: {c['uv_index']:.1f}")

        if aqi and aqi.get("european_aqi") is not None:
            aqi_val = aqi["european_aqi"]
            info = aqi_info(aqi_val, lang)
            lines.append("")
            lines.append(f"🌫️ کیفیت هوا: {info['icon']} <b>{info['fa']}</b> (AQI {aqi_val})")
            if aqi.get("pm2_5") is not None:
                lines.append(f"   PM2.5: {round(aqi['pm2_5'])} µg/m³")
            if aqi.get("pm10") is not None:
                lines.append(f"   PM10: {round(aqi['pm10'])} µg/m³")

        lines.append("")
        lines.append("📅 <b>پیش‌بینی ۳ روز آینده:</b>")
        for i in range(1, min(4, len(d["time"]))):
            day_code = d["weather_code"][i]
            day_wmo = WMO_DESC.get(day_code, {"fa": "؟", "icon": "❓"})
            date = datetime.fromisoformat(d["time"][i]).strftime("%m/%d")
            tmax = fmt_temp(d["temperature_2m_max"][i], unit)
            tmin = fmt_temp(d["temperature_2m_min"][i], unit)
            rain = d["precipitation_probability_max"][i]
            lines.append(f"{day_wmo['icon']} {date}: {tmin} ~ {tmax}  💧{rain}%")
    else:
        lines.append(f"{icon} <b>{city_name}</b>")
        lines.append("")
        lines.append(f"🌡️ Temp: <b>{temp}</b>  (Feels: {feels})")
        lines.append(f"📝 Condition: {desc}")
        lines.append(f"💧 Humidity: {c['relative_humidity_2m']}%")
        lines.append(f"💨 Wind: {wind} km/h ({c['wind_direction_10m']}°)")
        lines.append(f"🔽 Pressure: {round(c['pressure_msl'])} hPa")
        if c.get("visibility"):
            lines.append(f"👁️ Visibility: {round(c['visibility'] / 1000, 1)} km")
        if c.get("uv_index") is not None:
            lines.append(f"☀️ UV: {c['uv_index']:.1f}")

        if aqi and aqi.get("european_aqi") is not None:
            aqi_val = aqi["european_aqi"]
            info = aqi_info(aqi_val, lang)
            lines.append("")
            lines.append(f"🌫️ Air Quality: {info['icon']} <b>{info['en']}</b> (AQI {aqi_val})")

        lines.append("")
        lines.append("📅 <b>3-day forecast:</b>")
        for i in range(1, min(4, len(d["time"]))):
            day_code = d["weather_code"][i]
            day_wmo = WMO_DESC.get(day_code, {"en": "?", "icon": "❓"})
            date = datetime.fromisoformat(d["time"][i]).strftime("%m/%d")
            tmax = fmt_temp(d["temperature_2m_max"][i], unit)
            tmin = fmt_temp(d["temperature_2m_min"][i], unit)
            rain = d["precipitation_probability_max"][i]
            lines.append(f"{day_wmo['icon']} {date}: {tmin} ~ {tmax}  💧{rain}%")

    lines.append("")
    lines.append(f"<i>{'داده‌ها از Open-Meteo' if lang == 'fa' else 'Data from Open-Meteo'}</i>")

    return "\n".join(lines)


# ============================================================
#  دستورات ربات
# ============================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    lang = s["lang"]

    if lang == "fa":
        text = (
            "🌤️ <b>به ربات آب‌وهوا خوش آمدید!</b>\n\n"
            "من می‌تونم آب‌وهوا و کیفیت هوای هر شهر دنیا رو بهت بگم.\n\n"
            "🔹 <b>دستورات:</b>\n"
            "/weather [شهر] — آب‌وهوای فعلی\n"
            "/aqi [شهر] — کیفیت هوا\n"
            "/forecast [شهر] — پیش‌بینی ۷ روزه\n"
            "/add [شهر] — افزودن به لیست علاقه‌مندی\n"
            "/mycities — شهرهای ذخیره‌شده\n"
            "/remove [شهر] — حذف از لیست\n"
            "/settings — تنظیمات\n\n"
            "💡 فقط کافیه اسم شهر رو بفرستی (فارسی یا انگلیسی)!"
        )
    else:
        text = (
            "🌤️ <b>Welcome to Weather Bot!</b>\n\n"
            "I can tell you the weather and air quality of any city.\n\n"
            "🔹 <b>Commands:</b>\n"
            "/weather [city] — Current weather\n"
            "/aqi [city] — Air quality\n"
            "/forecast [city] — 7-day forecast\n"
            "/add [city] — Add to favorites\n"
            "/mycities — Saved cities\n"
            "/remove [city] — Remove from list\n"
            "/settings — Settings\n\n"
            "💡 Just send a city name (Persian or English)!"
        )

    keyboard = [
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
    ]
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    lang = s["lang"]

    if lang == "fa":
        text = (
            "⚙️ <b>تنظیمات</b>\n\n"
            f"🌐 زبان: {'فارسی' if s['lang'] == 'fa' else 'English'}\n"
            f"🌡️ واحد دما: {s['unit']}"
        )
    else:
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"🌐 Language: {'Persian' if s['lang'] == 'fa' else 'English'}\n"
            f"🌡️ Temp unit: {s['unit']}"
        )

    keyboard = [
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
        [InlineKeyboardButton("°C", callback_data="unit:C"),
         InlineKeyboardButton("°F", callback_data="unit:F")],
    ]
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def weather_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    args = ctx.args

    if not args:
        text = "لطفاً نام شهر رو وارد کن. مثال: <code>/weather تهران</code>" if s["lang"] == "fa" \
            else "Please provide a city name. Example: <code>/weather Tehran</code>"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    city_query = " ".join(args)
    await send_weather(update, city_query, s)


async def send_weather(update: Update, city_query: str, s: dict):
    msg = await update.message.reply_text("⏳ ...")
    try:
        city = await geocode(city_query, s["lang"])
        if not city:
            err = "❌ شهر پیدا نشد. نام دیگری امتحان کن." if s["lang"] == "fa" else "❌ City not found."
            await msg.edit_text(err)
            return

        lat, lon = city["latitude"], city["longitude"]
        weather, aqi = await asyncio.gather(
            fetch_weather(lat, lon),
            fetch_aqi(lat, lon),
        )

        text = build_weather_message(city, weather, aqi, s["lang"], s["unit"])
        await msg.edit_text(text, parse_mode=ParseMode.HTML)

    except httpx.HTTPError as e:
        logger.error(f"HTTP error: {e}")
        err = "⚠️ خطا در دریافت داده. دوباره تلاش کن." if s["lang"] == "fa" \
            else "⚠️ Failed to fetch data. Try again."
        await msg.edit_text(err)
    except Exception as e:
        logger.exception("Unexpected error")
        err = f"❌ خطای غیرمنتظره: {e}" if s["lang"] == "fa" else f"❌ Unexpected error: {e}"
        await msg.edit_text(err)


async def aqi_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    args = ctx.args

    if not args:
        text = "مثال: <code>/aqi تهران</code>" if s["lang"] == "fa" else "Example: <code>/aqi Tehran</code>"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    city_query = " ".join(args)
    msg = await update.message.reply_text("⏳ ...")
    try:
        city = await geocode(city_query, s["lang"])
        if not city:
            err = "❌ شهر پیدا نشد." if s["lang"] == "fa" else "❌ City not found."
            await msg.edit_text(err)
            return

        aqi = await fetch_aqi(city["latitude"], city["longitude"])
        if not aqi or aqi.get("european_aqi") is None:
            err = "⚠️ داده کیفیت هوا موجود نیست." if s["lang"] == "fa" \
                else "⚠️ Air quality data not available."
            await msg.edit_text(err)
            return

        val = aqi["european_aqi"]
        info = aqi_info(val, s["lang"])
        city_name = (city.get("name_fa") or city["name"]) if s["lang"] == "fa" else city["name"]

        if s["lang"] == "fa":
            text = (
                f"🌫️ <b>کیفیت هوای {city_name}</b>\n\n"
                f"{info['icon']} وضعیت: <b>{info['fa']}</b>\n"
                f"📊 شاخص AQI: <b>{val}</b>\n"
            )
            if aqi.get("pm2_5") is not None:
                text += f"• PM2.5: {round(aqi['pm2_5'])} µg/m³\n"
            if aqi.get("pm10") is not None:
                text += f"• PM10: {round(aqi['pm10'])} µg/m³\n"
        else:
            text = (
                f"🌫️ <b>Air Quality — {city_name}</b>\n\n"
                f"{info['icon']} Status: <b>{info['en']}</b>\n"
                f"📊 AQI: <b>{val}</b>\n"
            )
            if aqi.get("pm2_5") is not None:
                text += f"• PM2.5: {round(aqi['pm2_5'])} µg/m³\n"
            if aqi.get("pm10") is not None:
                text += f"• PM10: {round(aqi['pm10'])} µg/m³\n"

        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("AQI error")
        await msg.edit_text(f"❌ {e}")


async def forecast_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    args = ctx.args

    if not args:
        text = "مثال: <code>/forecast تهران</code>" if s["lang"] == "fa" else "Example: <code>/forecast Tehran</code>"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    city_query = " ".join(args)
    msg = await update.message.reply_text("⏳ ...")
    try:
        city = await geocode(city_query, s["lang"])
        if not city:
            await msg.edit_text("❌ شهر پیدا نشد." if s["lang"] == "fa" else "❌ City not found.")
            return

        weather = await fetch_weather(city["latitude"], city["longitude"])
        d = weather["daily"]
        city_name = (city.get("name_fa") or city["name"]) if s["lang"] == "fa" else city["name"]

        header = f"📅 <b>پیش‌بینی ۷ روزه {city_name}</b>\n\n" if s["lang"] == "fa" \
            else f"📅 <b>7-day forecast — {city_name}</b>\n\n"

        lines = [header]
        for i in range(len(d["time"])):
            code = d["weather_code"][i]
            wmo = WMO_DESC.get(code, {"fa": "؟", "en": "?", "icon": "❓"})
            date = datetime.fromisoformat(d["time"][i]).strftime("%a %m/%d")
            tmax = fmt_temp(d["temperature_2m_max"][i], s["unit"])
            tmin = fmt_temp(d["temperature_2m_min"][i], s["unit"])
            rain = d["precipitation_probability_max"][i]
            desc = wmo["fa"] if s["lang"] == "fa" else wmo["en"]
            lines.append(f"{wmo['icon']} <b>{date}</b>  {tmin} ~ {tmax}  💧{rain}%  {desc}")

        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Forecast error")
        await msg.edit_text(f"❌ {e}")


async def add_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    args = ctx.args
    if not args:
        await update.message.reply_text("مثال: /add تهران" if s["lang"] == "fa" else "Example: /add Tehran")
        return

    query = " ".join(args)
    city = await geocode(query, s["lang"])
    if not city:
        await update.message.reply_text("❌ شهر پیدا نشد." if s["lang"] == "fa" else "❌ City not found.")
        return

    entry = {"name": city["name"], "lat": city["latitude"], "lon": city["longitude"]}
    if any(c["lat"] == entry["lat"] and c["lon"] == entry["lon"] for c in s["cities"]):
        await update.message.reply_text("ℹ️ قبلاً اضافه شده." if s["lang"] == "fa" else "ℹ️ Already added.")
        return

    s["cities"].append(entry)
    msg = f"✅ «{city['name']}» اضافه شد." if s["lang"] == "fa" else f"✅ Added «{city['name']}»."
    await update.message.reply_text(msg)


async def remove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    args = ctx.args
    if not args:
        await update.message.reply_text("مثال: /remove تهران" if s["lang"] == "fa" else "Example: /remove Tehran")
        return

    query = " ".join(args).lower()
    before = len(s["cities"])
    s["cities"] = [c for c in s["cities"] if query not in c["name"].lower()]
    if len(s["cities"]) == before:
        await update.message.reply_text("❌ پیدا نشد." if s["lang"] == "fa" else "❌ Not found.")
    else:
        await update.message.reply_text("✅ حذف شد." if s["lang"] == "fa" else "✅ Removed.")


async def mycities_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)

    if not s["cities"]:
        await update.message.reply_text(
            "📭 لیست خالیه. با /add اضافه کن." if s["lang"] == "fa"
            else "📭 List is empty. Use /add."
        )
        return

    header = "📍 <b>شهرهای ذخیره‌شده:</b>\n\n" if s["lang"] == "fa" else "📍 <b>Saved cities:</b>\n\n"
    lines = [header]
    for c in s["cities"]:
        try:
            weather = await fetch_weather(c["lat"], c["lon"])
            temp = fmt_temp(weather["current"]["temperature_2m"], s["unit"])
            code = weather["current"]["weather_code"]
            icon = WMO_DESC.get(code, {"icon": "❓"})["icon"]
            lines.append(f"{icon} <b>{c['name']}</b> — {temp}")
        except Exception:
            lines.append(f"❓ <b>{c['name']}</b> — ?")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    await send_weather(update, update.message.text.strip(), s)


async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    s = get_settings(user_id)
    data = query.data

    if data.startswith("lang:"):
        s["lang"] = data.split(":")[1]
        msg = "✅ زبان تغییر کرد." if s["lang"] == "fa" else "✅ Language changed."
        await query.edit_message_text(msg)

    elif data.startswith("unit:"):
        s["unit"] = data.split(":")[1]
        msg = f"✅ واحد: {s['unit']}" if s["lang"] == "fa" else f"✅ Unit: {s['unit']}"
        await query.edit_message_text(msg)


# ============================================================
#  main
# ============================================================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        print("⚠️  توکن ربات رو در متغیر محیطی BOT_TOKEN تنظیم کن!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("weather", weather_cmd))
    app.add_handler(CommandHandler("aqi", aqi_cmd))
    app.add_handler(CommandHandler("forecast", forecast_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("mycities", mycities_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 ربات در حال اجراست... Ctrl+C برای توقف")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()