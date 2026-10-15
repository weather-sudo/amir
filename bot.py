#!/usr/bin/env python3
"""
ربات تلگرام آب‌وهوا — متصل به Open-Meteo
"""

import os
import logging
import asyncio
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

# ============================================================
#  تنظیمات
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
#  لیست شهرهای ایرانی (۱۵۰+ شهر)
# ============================================================
IRAN_CITIES = [
    # ==================== تهران و حومه ====================
    {"name": "Tehran", "name_fa": "تهران", "lat": 35.6892, "lon": 51.3890, "admin": "Tehran"},
    {"name": "Karaj", "name_fa": "کرج", "lat": 35.8355, "lon": 50.9915, "admin": "Alborz"},
    {"name": "Eslamshahr", "name_fa": "اسلامشهر", "lat": 35.5522, "lon": 51.2350, "admin": "Tehran"},
    {"name": "Shahriar", "name_fa": "شهریار", "lat": 35.6594, "lon": 51.0589, "admin": "Tehran"},
    {"name": "Varamin", "name_fa": "ورامین", "lat": 35.3244, "lon": 51.6458, "admin": "Tehran"},
    {"name": "Pakdasht", "name_fa": "پاکدشت", "lat": 35.4722, "lon": 51.6828, "admin": "Tehran"},
    {"name": "Pardis", "name_fa": "پردیس", "lat": 35.7386, "lon": 51.8097, "admin": "Tehran"},
    {"name": "Robat Karim", "name_fa": "رباط‌کریم", "lat": 35.4844, "lon": 51.0833, "admin": "Tehran"},
    {"name": "Qods", "name_fa": "قدس", "lat": 35.7167, "lon": 51.1167, "admin": "Tehran"},
    {"name": "Malard", "name_fa": "ملارد", "lat": 35.6667, "lon": 50.9833, "admin": "Tehran"},
    {"name": "Damavand", "name_fa": "دماوند", "lat": 35.7172, "lon": 52.0653, "admin": "Tehran"},
    {"name": "Firuzkuh", "name_fa": "فیروزکوه", "lat": 35.7567, "lon": 52.7731, "admin": "Tehran"},
    {"name": "Lavasan", "name_fa": "لواسان", "lat": 35.8231, "lon": 51.6361, "admin": "Tehran"},
    {"name": "Rudehen", "name_fa": "رودهن", "lat": 35.7347, "lon": 51.9103, "admin": "Tehran"},
    {"name": "Shahr-e Qods", "name_fa": "شهرقدس", "lat": 35.7167, "lon": 51.1167, "admin": "Tehran"},

    # ==================== خراسان رضوی ====================
    {"name": "Mashhad", "name_fa": "مشهد", "lat": 36.2605, "lon": 59.6168, "admin": "Razavi Khorasan"},
    {"name": "Neyshabur", "name_fa": "نیشابور", "lat": 36.2133, "lon": 58.7958, "admin": "Razavi Khorasan"},
    {"name": "Sabzevar", "name_fa": "سبزوار", "lat": 36.2125, "lon": 57.6819, "admin": "Razavi Khorasan"},
    {"name": "Torbat-e Heydarieh", "name_fa": "تربت حیدریه", "lat": 35.2736, "lon": 59.2197, "admin": "Razavi Khorasan"},
    {"name": "Torbat-e Jam", "name_fa": "تربت جام", "lat": 35.2439, "lon": 60.6236, "admin": "Razavi Khorasan"},
    {"name": "Quchan", "name_fa": "قوچان", "lat": 37.1061, "lon": 58.5094, "admin": "Razavi Khorasan"},
    {"name": "Kashmar", "name_fa": "کاشمر", "lat": 35.2381, "lon": 58.4656, "admin": "Razavi Khorasan"},
    {"name": "Gonabad", "name_fa": "گناباد", "lat": 34.3528, "lon": 58.6836, "admin": "Razavi Khorasan"},
    {"name": "Chenaran", "name_fa": "چناران", "lat": 36.6456, "lon": 59.1211, "admin": "Razavi Khorasan"},
    {"name": "Sarakhs", "name_fa": "سرخس", "lat": 36.5447, "lon": 61.1581, "admin": "Razavi Khorasan"},
    {"name": "Fariman", "name_fa": "فریمان", "lat": 35.7039, "lon": 59.8500, "admin": "Razavi Khorasan"},
    {"name": "Khaf", "name_fa": "خواف", "lat": 34.5764, "lon": 60.1408, "admin": "Razavi Khorasan"},
    {"name": "Bardaskan", "name_fa": "بردسکن", "lat": 35.2606, "lon": 57.9711, "admin": "Razavi Khorasan"},

    # ==================== اصفهان ====================
    {"name": "Isfahan", "name_fa": "اصفهان", "lat": 32.6546, "lon": 51.6680, "admin": "Isfahan"},
    {"name": "Kashan", "name_fa": "کاشان", "lat": 33.9850, "lon": 51.4100, "admin": "Isfahan"},
    {"name": "Najafabad", "name_fa": "نجف‌آباد", "lat": 32.6333, "lon": 51.3667, "admin": "Isfahan"},
    {"name": "Khomeynishahr", "name_fa": "خمینی‌شهر", "lat": 32.7000, "lon": 51.5167, "admin": "Isfahan"},
    {"name": "Shahin Shahr", "name_fa": "شاهین‌شهر", "lat": 32.8667, "lon": 51.5500, "admin": "Isfahan"},
    {"name": "Shahreza", "name_fa": "شهرضا", "lat": 32.0000, "lon": 51.8667, "admin": "Isfahan"},
    {"name": "Fooladshahr", "name_fa": "فولادشهر", "lat": 32.4833, "lon": 51.4000, "admin": "Isfahan"},
    {"name": "Zarrinshahr", "name_fa": "زرین‌شهر", "lat": 32.3833, "lon": 51.3833, "admin": "Isfahan"},
    {"name": "Khomeyn", "name_fa": "خمین", "lat": 33.6419, "lon": 50.0800, "admin": "Markazi"},
    {"name": "Natanz", "name_fa": "نطنز", "lat": 33.5108, "lon": 51.9183, "admin": "Isfahan"},
    {"name": "Ardestan", "name_fa": "اردستان", "lat": 33.3761, "lon": 52.3700, "admin": "Isfahan"},
    {"name": "Golpayegan", "name_fa": "گلپایگان", "lat": 33.4536, "lon": 50.2889, "admin": "Isfahan"},
    {"name": "Semirom", "name_fa": "سمیرم", "lat": 31.4183, "lon": 51.5711, "admin": "Isfahan"},
    {"name": "Dehaqan", "name_fa": "دهاقان", "lat": 32.0500, "lon": 51.6500, "admin": "Isfahan"},

    # ==================== فارس ====================
    {"name": "Shiraz", "name_fa": "شیراز", "lat": 29.5918, "lon": 52.5837, "admin": "Fars"},
    {"name": "Marvdasht", "name_fa": "مرودشت", "lat": 29.8739, "lon": 52.8025, "admin": "Fars"},
    {"name": "Jahrom", "name_fa": "جهرم", "lat": 28.5000, "lon": 53.5600, "admin": "Fars"},
    {"name": "Fasa", "name_fa": "فسا", "lat": 28.9389, "lon": 53.6481, "admin": "Fars"},
    {"name": "Kazerun", "name_fa": "کازرون", "lat": 29.6158, "lon": 51.6553, "admin": "Fars"},
    {"name": "Lar", "name_fa": "لار", "lat": 27.6833, "lon": 54.3417, "admin": "Fars"},
    {"name": "Abadeh", "name_fa": "آباده", "lat": 31.1606, "lon": 52.6519, "admin": "Fars"},
    {"name": "Darab", "name_fa": "داراب", "lat": 28.7519, "lon": 54.5406, "admin": "Fars"},
    {"name": "Neyriz", "name_fa": "نی‌ریز", "lat": 29.1981, "lon": 54.3281, "admin": "Fars"},
    {"name": "Sepidan", "name_fa": "سپیدان", "lat": 30.2333, "lon": 51.9833, "admin": "Fars"},
    {"name": "Eqlid", "name_fa": "اقلید", "lat": 30.8978, "lon": 52.6867, "admin": "Fars"},
    {"name": "Estahban", "name_fa": "استهبان", "lat": 29.1278, "lon": 54.0417, "admin": "Fars"},
    {"name": "Zarqan", "name_fa": "زرقان", "lat": 29.7711, "lon": 52.7233, "admin": "Fars"},

    # ==================== آذربایجان شرقی ====================
    {"name": "Tabriz", "name_fa": "تبریز", "lat": 38.0962, "lon": 46.2738, "admin": "East Azerbaijan"},
    {"name": "Maragheh", "name_fa": "مراغه", "lat": 37.3892, "lon": 46.2382, "admin": "East Azerbaijan"},
    {"name": "Marand", "name_fa": "مرند", "lat": 38.4320, "lon": 45.7700, "admin": "East Azerbaijan"},
    {"name": "Ahar", "name_fa": "اهر", "lat": 38.4775, "lon": 47.0662, "admin": "East Azerbaijan"},
    {"name": "Mianeh", "name_fa": "میانه", "lat": 37.4211, "lon": 47.7100, "admin": "East Azerbaijan"},
    {"name": "Bonab", "name_fa": "بناب", "lat": 38.4283, "lon": 45.9087, "admin": "East Azerbaijan"},
    {"name": "Sarab", "name_fa": "سراب", "lat": 36.3175, "lon": 47.5648, "admin": "East Azerbaijan"},
    {"name": "Azarshahr", "name_fa": "آذرشهر", "lat": 37.7585, "lon": 45.9794, "admin": "East Azerbaijan"},
    {"name": "Osku", "name_fa": "اسکو", "lat": 37.9161, "lon": 46.1245, "admin": "East Azerbaijan"},
    {"name": "Shabestar", "name_fa": "شبستر", "lat": 38.1784, "lon": 45.7002, "admin": "East Azerbaijan"},
    {"name": "Jolfa", "name_fa": "جلفا", "lat": 38.9376, "lon": 45.6252, "admin": "East Azerbaijan"},
    {"name": "Sardrud", "name_fa": "سردرود", "lat": 38.0314, "lon": 46.1475, "admin": "East Azerbaijan"},
    {"name": "Hadishahr", "name_fa": "هادیشهر", "lat": 38.8475, "lon": 45.6622, "admin": "East Azerbaijan"},
    {"name": "Ajab Shir", "name_fa": "عجب‌شیر", "lat": 37.4828, "lon": 45.8936, "admin": "East Azerbaijan"},

    # ==================== آذربایجان غربی ====================
    {"name": "Urmia", "name_fa": "ارومیه", "lat": 37.5553, "lon": 45.0725, "admin": "West Azerbaijan"},
    {"name": "Khoy", "name_fa": "خوی", "lat": 38.5500, "lon": 44.9500, "admin": "West Azerbaijan"},
    {"name": "Mahabad", "name_fa": "مهاباد", "lat": 36.7639, "lon": 45.7222, "admin": "West Azerbaijan"},
    {"name": "Bukan", "name_fa": "بوکان", "lat": 36.5210, "lon": 46.2100, "admin": "West Azerbaijan"},
    {"name": "Miandoab", "name_fa": "میاندوآب", "lat": 36.9667, "lon": 46.1000, "admin": "West Azerbaijan"},
    {"name": "Salmas", "name_fa": "سلماس", "lat": 38.1972, "lon": 44.7683, "admin": "West Azerbaijan"},
    {"name": "Naqadeh", "name_fa": "نقده", "lat": 36.9553, "lon": 45.3881, "admin": "West Azerbaijan"},
    {"name": "Piranshahr", "name_fa": "پیرانشهر", "lat": 36.7000, "lon": 45.1500, "admin": "West Azerbaijan"},
    {"name": "Sardasht", "name_fa": "سردشت", "lat": 36.1580, "lon": 45.4752, "admin": "West Azerbaijan"},
    {"name": "Shahin Dezh", "name_fa": "شاهین‌دژ", "lat": 36.6769, "lon": 46.5667, "admin": "West Azerbaijan"},
    {"name": "Takab", "name_fa": "تکاب", "lat": 36.4008, "lon": 47.1125, "admin": "West Azerbaijan"},
    {"name": "Maku", "name_fa": "ماکو", "lat": 39.2956, "lon": 44.5117, "admin": "West Azerbaijan"},
    {"name": "Chaldoran", "name_fa": "چالدران", "lat": 39.0642, "lon": 44.3833, "admin": "West Azerbaijan"},

    # ==================== خوزستان ====================
    {"name": "Ahvaz", "name_fa": "اهواز", "lat": 31.3167, "lon": 48.6844, "admin": "Khuzestan"},
    {"name": "Abadan", "name_fa": "آبادان", "lat": 30.3392, "lon": 48.3043, "admin": "Khuzestan"},
    {"name": "Khorramshahr", "name_fa": "خرمشهر", "lat": 30.4408, "lon": 48.1845, "admin": "Khuzestan"},
    {"name": "Dezful", "name_fa": "دزفول", "lat": 32.3830, "lon": 48.4058, "admin": "Khuzestan"},
    {"name": "Andimeshk", "name_fa": "اندیمشک", "lat": 32.4610, "lon": 48.3534, "admin": "Khuzestan"},
    {"name": "Shushtar", "name_fa": "شوشتر", "lat": 32.0452, "lon": 48.8540, "admin": "Khuzestan"},
    {"name": "Behbahan", "name_fa": "بهبهان", "lat": 30.5959, "lon": 50.2417, "admin": "Khuzestan"},
    {"name": "Izeh", "name_fa": "ایذه", "lat": 31.8300, "lon": 49.8667, "admin": "Khuzestan"},
    {"name": "Masjed Soleyman", "name_fa": "مسجدسلیمان", "lat": 31.9353, "lon": 49.3039, "admin": "Khuzestan"},
    {"name": "Shush", "name_fa": "شوش", "lat": 32.1944, "lon": 48.2572, "admin": "Khuzestan"},
    {"name": "Bandar Mahshahr", "name_fa": "بندر ماهشهر", "lat": 30.5589, "lon": 49.1981, "admin": "Khuzestan"},
    {"name": "Omidiyeh", "name_fa": "امیدیه", "lat": 30.7622, "lon": 49.7025, "admin": "Khuzestan"},
    {"name": "Ramhormoz", "name_fa": "رامهرمز", "lat": 31.2792, "lon": 49.6022, "admin": "Khuzestan"},
    {"name": "Susangerd", "name_fa": "سوسنگرد", "lat": 31.5619, "lon": 48.1819, "admin": "Khuzestan"},
    {"name": "Hendijan", "name_fa": "هندیجان", "lat": 30.2342, "lon": 49.7139, "admin": "Khuzestan"},
    {"name": "Baghmolek", "name_fa": "باغ‌ملک", "lat": 31.5258, "lon": 49.8831, "admin": "Khuzestan"},
    {"name": "Dehdez", "name_fa": "دهدز", "lat": 31.7083, "lon": 50.2856, "admin": "Khuzestan"},

    # ==================== گیلان ====================
    {"name": "Rasht", "name_fa": "رشت", "lat": 37.2808, "lon": 49.5832, "admin": "Gilan"},
    {"name": "Bandar Anzali", "name_fa": "بندر انزلی", "lat": 37.4739, "lon": 49.4578, "admin": "Gilan"},
    {"name": "Lahijan", "name_fa": "لاهیجان", "lat": 37.2019, "lon": 50.0047, "admin": "Gilan"},
    {"name": "Langarud", "name_fa": "لنگرود", "lat": 37.1956, "lon": 50.1525, "admin": "Gilan"},
    {"name": "Astaneh-ye Ashrafiyeh", "name_fa": "آستانه اشرفیه", "lat": 37.2611, "lon": 49.9431, "admin": "Gilan"},
    {"name": "Talesh", "name_fa": "تالش", "lat": 37.8008, "lon": 48.9058, "admin": "Gilan"},
    {"name": "Rudsar", "name_fa": "رودسر", "lat": 37.1364, "lon": 50.2931, "admin": "Gilan"},
    {"name": "Fuman", "name_fa": "فومن", "lat": 37.2239, "lon": 49.3119, "admin": "Gilan"},
    {"name": "Sowme'eh Sara", "name_fa": "صومعه‌سرا", "lat": 37.3114, "lon": 49.3206, "admin": "Gilan"},
    {"name": "Rudbar", "name_fa": "رودبار", "lat": 36.8236, "lon": 49.4278, "admin": "Gilan"},
    {"name": "Astara", "name_fa": "آستارا", "lat": 38.4222, "lon": 48.8722, "admin": "Gilan"},
    {"name": "Masal", "name_fa": "ماسال", "lat": 37.3631, "lon": 49.1325, "admin": "Gilan"},
    {"name": "Rezvanshahr", "name_fa": "رضوانشهر", "lat": 37.5514, "lon": 49.1372, "admin": "Gilan"},

    # ==================== مازندران ====================
    {"name": "Sari", "name_fa": "ساری", "lat": 36.5633, "lon": 53.0601, "admin": "Mazandaran"},
    {"name": "Babol", "name_fa": "بابل", "lat": 36.5513, "lon": 52.6789, "admin": "Mazandaran"},
    {"name": "Amol", "name_fa": "آمل", "lat": 36.4696, "lon": 52.3506, "admin": "Mazandaran"},
    {"name": "Qaem Shahr", "name_fa": "قائم‌شهر", "lat": 36.4633, "lon": 52.8600, "admin": "Mazandaran"},
    {"name": "Behshahr", "name_fa": "بهشهر", "lat": 36.6925, "lon": 53.5522, "admin": "Mazandaran"},
    {"name": "Chalus", "name_fa": "چالوس", "lat": 36.6550, "lon": 51.4200, "admin": "Mazandaran"},
    {"name": "Noshahr", "name_fa": "نوشهر", "lat": 36.6489, "lon": 51.4964, "admin": "Mazandaran"},
    {"name": "Ramsar", "name_fa": "رامسر", "lat": 36.9228, "lon": 50.6539, "admin": "Mazandaran"},
    {"name": "Tonekabon", "name_fa": "تنکابن", "lat": 36.8167, "lon": 50.8761, "admin": "Mazandaran"},
    {"name": "Babolsar", "name_fa": "بابلسر", "lat": 36.7025, "lon": 52.6500, "admin": "Mazandaran"},
    {"name": "Fereydunkenar", "name_fa": "فریدونکنار", "lat": 36.6856, "lon": 52.5164, "admin": "Mazandaran"},
    {"name": "Juybar", "name_fa": "جویبار", "lat": 36.6419, "lon": 52.9122, "admin": "Mazandaran"},
    {"name": "Savadkuh", "name_fa": "سوادکوه", "lat": 36.1900, "lon": 52.8600, "admin": "Mazandaran"},
    {"name": "Neka", "name_fa": "نکا", "lat": 36.6528, "lon": 53.2961, "admin": "Mazandaran"},
    {"name": "Galugah", "name_fa": "گلوگاه", "lat": 36.7219, "lon": 53.8083, "admin": "Mazandaran"},

    # ==================== کرمان ====================
    {"name": "Kerman", "name_fa": "کرمان", "lat": 30.2839, "lon": 57.0834, "admin": "Kerman"},
    {"name": "Bam", "name_fa": "بم", "lat": 29.1060, "lon": 58.3570, "admin": "Kerman"},
    {"name": "Rafsanjan", "name_fa": "رفسنجان", "lat": 30.4067, "lon": 55.9939, "admin": "Kerman"},
    {"name": "Sirjan", "name_fa": "سیرجان", "lat": 29.4522, "lon": 55.6794, "admin": "Kerman"},
    {"name": "Jiroft", "name_fa": "جیرفت", "lat": 28.6744, "lon": 57.7436, "admin": "Kerman"},
    {"name": "Zarand", "name_fa": "زرند", "lat": 30.8128, "lon": 56.5619, "admin": "Kerman"},
    {"name": "Shahr-e Babak", "name_fa": "شهربابک", "lat": 30.1147, "lon": 55.1172, "admin": "Kerman"},
    {"name": "Kahnuj", "name_fa": "کهنوج", "lat": 27.9522, "lon": 57.7006, "admin": "Kerman"},
    {"name": "Bardsir", "name_fa": "بردسیر", "lat": 29.9256, "lon": 56.5739, "admin": "Kerman"},
    {"name": "Ravar", "name_fa": "راور", "lat": 31.2647, "lon": 56.8050, "admin": "Kerman"},

    # ==================== خراسان شمالی و جنوبی ====================
    {"name": "Bojnord", "name_fa": "بجنورد", "lat": 37.4747, "lon": 57.3290, "admin": "North Khorasan"},
    {"name": "Shirvan", "name_fa": "شیروان", "lat": 37.3975, "lon": 57.9300, "admin": "North Khorasan"},
    {"name": "Esfarayen", "name_fa": "اسفراین", "lat": 37.0764, "lon": 57.5100, "admin": "North Khorasan"},
    {"name": "Birjand", "name_fa": "بیرجند", "lat": 32.8663, "lon": 59.2211, "admin": "South Khorasan"},
    {"name": "Qaen", "name_fa": "قائن", "lat": 33.7258, "lon": 59.1858, "admin": "South Khorasan"},
    {"name": "Ferdows", "name_fa": "فردوس", "lat": 34.0175, "lon": 58.1700, "admin": "South Khorasan"},
    {"name": "Tabas", "name_fa": "طبس", "lat": 33.5956, "lon": 56.9244, "admin": "South Khorasan"},

    # ==================== کرمانشاه و کردستان ====================
    {"name": "Kermanshah", "name_fa": "کرمانشاه", "lat": 34.3142, "lon": 47.0650, "admin": "Kermanshah"},
    {"name": "Eslamabad-e Gharb", "name_fa": "اسلام‌آباد غرب", "lat": 34.1089, "lon": 46.5308, "admin": "Kermanshah"},
    {"name": "Harsin", "name_fa": "هرسین", "lat": 34.2736, "lon": 47.5861, "admin": "Kermanshah"},
    {"name": "Sonqor", "name_fa": "سنقر", "lat": 34.7839, "lon": 47.5986, "admin": "Kermanshah"},
    {"name": "Paveh", "name_fa": "پاوه", "lat": 35.0456, "lon": 46.3575, "admin": "Kermanshah"},
    {"name": "Sanandaj", "name_fa": "سنندج", "lat": 35.3149, "lon": 46.9921, "admin": "Kurdistan"},
    {"name": "Saqqez", "name_fa": "سقز", "lat": 36.2498, "lon": 46.2735, "admin": "Kurdistan"},
    {"name": "Marivan", "name_fa": "مریوان", "lat": 35.5239, "lon": 46.1761, "admin": "Kurdistan"},
    {"name": "Baneh", "name_fa": "بانه", "lat": 35.9989, "lon": 45.8867, "admin": "Kurdistan"},
    {"name": "Qorveh", "name_fa": "قروه", "lat": 35.1664, "lon": 47.8047, "admin": "Kurdistan"},
    {"name": "Bijar", "name_fa": "بیجار", "lat": 35.8728, "lon": 47.6025, "admin": "Kurdistan"},

    # ==================== سایر استان‌ها ====================
    {"name": "Qom", "name_fa": "قم", "lat": 34.6416, "lon": 50.8746, "admin": "Qom"},
    {"name": "Ardabil", "name_fa": "اردبیل", "lat": 38.2494, "lon": 48.3014, "admin": "Ardabil"},
    {"name": "Parsabad", "name_fa": "پارس‌آباد", "lat": 39.6489, "lon": 47.9164, "admin": "Ardabil"},
    {"name": "Khalkhal", "name_fa": "خلخال", "lat": 37.6192, "lon": 48.5253, "admin": "Ardabil"},
    {"name": "Meshgin Shahr", "name_fa": "مشگین‌شهر", "lat": 38.3986, "lon": 47.6806, "admin": "Ardabil"},
    {"name": "Arak", "name_fa": "اراک", "lat": 34.1380, "lon": 49.8470, "admin": "Markazi"},
    {"name": "Saveh", "name_fa": "ساوه", "lat": 35.0214, "lon": 50.3567, "admin": "Markazi"},
    {"name": "Mahallat", "name_fa": "محلات", "lat": 33.9111, "lon": 50.4531, "admin": "Markazi"},
    {"name": "Zanjan", "name_fa": "زنجان", "lat": 36.6769, "lon": 48.4963, "admin": "Zanjan"},
    {"name": "Abhar", "name_fa": "ابهر", "lat": 36.1458, "lon": 49.2181, "admin": "Zanjan"},
    {"name": "Khorramdarreh", "name_fa": "خرمدره", "lat": 36.2069, "lon": 49.1914, "admin": "Zanjan"},
    {"name": "Qazvin", "name_fa": "قزوین", "lat": 36.2688, "lon": 50.0041, "admin": "Qazvin"},
    {"name": "Takestan", "name_fa": "تاکستان", "lat": 36.0697, "lon": 49.6981, "admin": "Qazvin"},
    {"name": "Gorgan", "name_fa": "گرگان", "lat": 36.8378, "lon": 54.4414, "admin": "Golestan"},
    {"name": "Gonbad-e Kavus", "name_fa": "گنبد کاووس", "lat": 37.2506, "lon": 55.1675, "admin": "Golestan"},
    {"name": "Aliabad-e Katul", "name_fa": "علی‌آباد کتول", "lat": 36.9086, "lon": 54.8653, "admin": "Golestan"},
    {"name": "Bandar Torkaman", "name_fa": "بندر ترکمن", "lat": 36.9019, "lon": 54.0719, "admin": "Golestan"},
    {"name": "Yazd", "name_fa": "یزد", "lat": 31.8974, "lon": 54.3569, "admin": "Yazd"},
    {"name": "Meybod", "name_fa": "میبد", "lat": 32.2500, "lon": 54.0167, "admin": "Yazd"},
    {"name": "Ardakan", "name_fa": "اردکان", "lat": 32.3100, "lon": 54.0175, "admin": "Yazd"},
    {"name": "Bafq", "name_fa": "بافق", "lat": 31.6119, "lon": 55.4106, "admin": "Yazd"},
    {"name": "Taft", "name_fa": "تفت", "lat": 31.7481, "lon": 54.2058, "admin": "Yazd"},
    {"name": "Zahedan", "name_fa": "زاهدان", "lat": 29.4963, "lon": 60.8629, "admin": "Sistan"},
    {"name": "Zabol", "name_fa": "زابل", "lat": 31.0289, "lon": 61.4947, "admin": "Sistan"},
    {"name": "Chabahar", "name_fa": "چابهار", "lat": 25.2919, "lon": 60.6431, "admin": "Sistan"},
    {"name": "Iranshahr", "name_fa": "ایرانشهر", "lat": 27.2025, "lon": 60.6847, "admin": "Sistan"},
    {"name": "Saravan", "name_fa": "سراوان", "lat": 27.3706, "lon": 62.3342, "admin": "Sistan"},
    {"name": "Khash", "name_fa": "خاش", "lat": 28.2211, "lon": 61.2158, "admin": "Sistan"},
    {"name": "Hamadan", "name_fa": "همدان", "lat": 34.7983, "lon": 48.5148, "admin": "Hamadan"},
    {"name": "Malayer", "name_fa": "ملایر", "lat": 34.2969, "lon": 48.8236, "admin": "Hamadan"},
    {"name": "Nahavand", "name_fa": "نهاوند", "lat": 34.1889, "lon": 48.3769, "admin": "Hamadan"},
    {"name": "Tuyserkan", "name_fa": "تویسرکان", "lat": 34.5522, "lon": 48.4456, "admin": "Hamadan"},
    {"name": "Asadabad", "name_fa": "اسدآباد", "lat": 34.7819, "lon": 48.1197, "admin": "Hamadan"},
    {"name": "Bandar Abbas", "name_fa": "بندرعباس", "lat": 27.1832, "lon": 56.2666, "admin": "Hormozgan"},
    {"name": "Qeshm", "name_fa": "قشم", "lat": 26.9581, "lon": 56.2719, "admin": "Hormozgan"},
    {"name": "Kish", "name_fa": "کیش", "lat": 26.5578, "lon": 53.9803, "admin": "Hormozgan"},
    {"name": "Minab", "name_fa": "میناب", "lat": 27.1461, "lon": 57.0803, "admin": "Hormozgan"},
    {"name": "Bandar Lengeh", "name_fa": "بندر لنگه", "lat": 26.5581, "lon": 54.8806, "admin": "Hormozgan"},
    {"name": "Bushehr", "name_fa": "بوشهر", "lat": 28.9234, "lon": 50.8200, "admin": "Bushehr"},
    {"name": "Borazjan", "name_fa": "برازجان", "lat": 29.2664, "lon": 51.2136, "admin": "Bushehr"},
    {"name": "Bandar Ganaveh", "name_fa": "بندر گناوه", "lat": 29.5792, "lon": 50.5172, "admin": "Bushehr"},
    {"name": "Kangan", "name_fa": "کنگان", "lat": 27.8372, "lon": 52.0631, "admin": "Bushehr"},
    {"name": "Ilam", "name_fa": "ایلام", "lat": 33.6374, "lon": 46.4227, "admin": "Ilam"},
    {"name": "Dehloran", "name_fa": "دهلران", "lat": 32.6942, "lon": 47.2639, "admin": "Ilam"},
    {"name": "Abdanan", "name_fa": "آبدانان", "lat": 32.9919, "lon": 47.4200, "admin": "Ilam"},
    {"name": "Eyvan", "name_fa": "ایوان", "lat": 33.8275, "lon": 46.3081, "admin": "Ilam"},
    {"name": "Semnan", "name_fa": "سمنان", "lat": 35.5769, "lon": 53.3953, "admin": "Semnan"},
    {"name": "Shahrud", "name_fa": "شاهرود", "lat": 36.4181, "lon": 54.9764, "admin": "Semnan"},
    {"name": "Damghan", "name_fa": "دامغان", "lat": 36.1681, "lon": 54.3481, "admin": "Semnan"},
    {"name": "Garmsar", "name_fa": "گرمسار", "lat": 35.2186, "lon": 52.3406, "admin": "Semnan"},
    {"name": "Khorramabad", "name_fa": "خرم‌آباد", "lat": 33.4878, "lon": 48.3558, "admin": "Lorestan"},
    {"name": "Borujerd", "name_fa": "بروجرد", "lat": 33.8973, "lon": 48.7516, "admin": "Lorestan"},
    {"name": "Dorud", "name_fa": "دورود", "lat": 33.4922, "lon": 49.0580, "admin": "Lorestan"},
    {"name": "Aligudarz", "name_fa": "الیگودرز", "lat": 33.4006, "lon": 49.6961, "admin": "Lorestan"},
    {"name": "Kohdasht", "name_fa": "کوهدشت", "lat": 33.5303, "lon": 47.6072, "admin": "Lorestan"},
    {"name": "Yasuj", "name_fa": "یاسوج", "lat": 30.6681, "lon": 51.5881, "admin": "Kohgiluyeh"},
    {"name": "Dogonbadan", "name_fa": "دوگنبدان", "lat": 30.3600, "lon": 50.7953, "admin": "Kohgiluyeh"},
    {"name": "Shahr-e Kord", "name_fa": "شهرکرد", "lat": 32.3260, "lon": 50.8644, "admin": "Chaharmahal"},
    {"name": "Borujen", "name_fa": "بروجن", "lat": 31.9667, "lon": 51.2833, "admin": "Chaharmahal"},
    {"name": "Farokh Shahr", "name_fa": "فرخ‌شهر", "lat": 32.2797, "lon": 50.9711, "admin": "Chaharmahal"},
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
#  تنظیمات کاربران
# ============================================================
user_settings = {}


def get_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = {"lang": "fa", "unit": "C", "cities": []}
    return user_settings[user_id]


# ============================================================
#  کیبورد پایین (Reply Keyboard)
# ============================================================
def get_main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """ساخت کیبورد پایین بر اساس زبان کاربر"""
    if lang == "fa":
        keyboard = [
            [KeyboardButton("🌤️ آب‌وهوا"), KeyboardButton("🌫️ کیفیت هوا")],
            [KeyboardButton("📅 پیش‌بینی"), KeyboardButton("⭐ علاقه‌مندی‌ها")],
            [KeyboardButton("📍 موقعیت من"), KeyboardButton("⚙️ تنظیمات")],
            [KeyboardButton("❓ راهنما")],
        ]
        placeholder = "اسم شهر رو بنویس..."
    else:
        keyboard = [
            [KeyboardButton("🌤️ Weather"), KeyboardButton("🌫️ Air Quality")],
            [KeyboardButton("📅 Forecast"), KeyboardButton("⭐ Favorites")],
            [KeyboardButton("📍 My Location"), KeyboardButton("⚙️ Settings")],
            [KeyboardButton("❓ Help")],
        ]
        placeholder = "Type a city name..."
    
    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        input_field_placeholder=placeholder
    )


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
            "🔹 <b>چطور کار کنم؟</b>\n"
            "• از دکمه‌های پایین استفاده کن\n"
            "• یا اسم شهر رو بنویس (فارسی یا انگلیسی)\n"
            "• یا از دستورها استفاده کن: /weather تهران\n\n"
            "💡 مثلاً: <code>تهران</code> یا <code>Paris</code>"
        )
    else:
        text = (
            "🌤️ <b>Welcome to Weather Bot!</b>\n\n"
            "I can tell you the weather and air quality of any city.\n\n"
            "🔹 <b>How to use?</b>\n"
            "• Use the buttons below\n"
            "• Or type a city name (Persian or English)\n"
            "• Or use commands: /weather Tehran\n\n"
            "💡 Example: <code>Tehran</code> or <code>Paris</code>"
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(lang)
    )


async def settings_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = get_settings(user_id)
    lang = s["lang"]

    if lang == "fa":
        text = (
            "⚙️ <b>تنظیمات</b>\n\n"
            f"🌐 زبان: {'فارسی' if s['lang'] == 'fa' else 'English'}\n"
            f"🌡️ واحد دما: {s['unit']}\n\n"
            "👇 برای تغییر، یکی از دکمه‌های زیر رو بزن:"
        )
    else:
        text = (
            "⚙️ <b>Settings</b>\n\n"
            f"🌐 Language: {'Persian' if s['lang'] == 'fa' else 'English'}\n"
            f"🌡️ Temp unit: {s['unit']}\n\n"
            "👇 Tap one of the buttons below:"
        )

    inline_keyboard = [
        [InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
         InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
        [InlineKeyboardButton("°C", callback_data="unit:C"),
         InlineKeyboardButton("°F", callback_data="unit:F")],
    ]

    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard(lang)
    )

    await update.message.reply_text(
        "👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard)
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
    if any(abs(c["lat"] - entry["lat"]) < 0.01 and abs(c["lon"] - entry["lon"]) < 0.01 for c in s["cities"]):
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


async def handle_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """پردازش موقعیت مکانی کاربر"""
    user_id = update.effective_user.id
    s = get_settings(user_id)
    loc = update.message.location

    if not loc:
        return

    msg = await update.message.reply_text("⏳ ...")

    try:
        lat, lon = loc.latitude, loc.longitude
        weather, aqi = await asyncio.gather(
            fetch_weather(lat, lon),
            fetch_aqi(lat, lon),
        )

        city_name = "موقعیت شما" if s["lang"] == "fa" else "Your Location"
        city = {"name": city_name, "latitude": lat, "longitude": lon}
        text = build_weather_message(city, weather, aqi, s["lang"], s["unit"])
        await msg.edit_text(text, parse_mode=ParseMode.HTML)

    except Exception as e:
        logger.exception("Location error")
        await msg.edit_text(f"❌ {e}")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """تشخیص دکمه‌های کیبورد از اسم شهر"""
    user_id = update.effective_user.id
    s = get_settings(user_id)
    text = update.message.text.strip()

    # ---- تشخیص دکمه‌های کیبورد ----
    weather_btns = ["🌤️ آب‌وهوا", "🌤️ Weather"]
    aqi_btns = ["🌫️ کیفیت هوا", "🌫️ Air Quality"]
    forecast_btns = ["📅 پیش‌بینی", "📅 Forecast"]
    fav_btns = ["⭐ علاقه‌مندی‌ها", "⭐ Favorites"]
    loc_btns = ["📍 موقعیت من", "📍 My Location"]
    settings_btns = ["⚙️ تنظیمات", "⚙️ Settings"]
    help_btns = ["❓ راهنما", "❓ Help"]

    # ---- دکمه آب‌وهوا ----
    if text in weather_btns:
        msg = "🏙️ اسم شهر رو بنویس (مثلاً: تهران)" if s["lang"] == "fa" \
            else "🏙️ Type the city name (e.g., Tehran)"
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(s["lang"]))
        return

    # ---- دکمه کیفیت هوا ----
    if text in aqi_btns:
        msg = "🌫️ اسم شهر رو بنویس تا کیفیت هواش رو بگم" if s["lang"] == "fa" \
            else "🌫️ Type the city name for air quality"
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(s["lang"]))
        return

    # ---- دکمه پیش‌بینی ----
    if text in forecast_btns:
        msg = "📅 اسم شهر رو بنویس تا پیش‌بینی ۷ روزه بدم" if s["lang"] == "fa" \
            else "📅 Type the city name for 7-day forecast"
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(s["lang"]))
        return

    # ---- دکمه علاقه‌مندی‌ها ----
    if text in fav_btns:
        if not s["cities"]:
            msg = "📭 لیستت خالیه. با دستور /add شهر اضافه کن." if s["lang"] == "fa" \
                else "📭 Your list is empty. Use /add to add cities."
            await update.message.reply_text(msg, reply_markup=get_main_keyboard(s["lang"]))
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
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_keyboard(s["lang"])
        )
        return

    # ---- دکمه موقعیت من ----
    if text in loc_btns:
        msg = (
            "📍 برای استفاده از موقعیتت:\n\n"
            "1. آیکن 📎 (گیره) رو بزن\n"
            "2. گزینه «Location» رو انتخاب کن\n"
            "3. موقعیتت رو بفرست\n\n"
            "من آب‌وهوای همون‌جا رو بهت می‌گم!"
            if s["lang"] == "fa" else
            "📍 To use your location:\n\n"
            "1. Tap 📎 (attachment)\n"
            "2. Choose «Location»\n"
            "3. Send your location\n\n"
            "I'll tell you the weather there!"
        )
        await update.message.reply_text(msg, reply_markup=get_main_keyboard(s["lang"]))
        return

    # ---- دکمه تنظیمات ----
    if text in settings_btns:
        await settings_cmd(update, ctx)
        return

    # ---- دکمه راهنما ----
    if text in help_btns:
        await start(update, ctx)
        return

    # ---- اگه دکمه نبود → اسم شهر ----
    await send_weather(update, text, s)


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
        await query.message.reply_text(
            "🔽",
            reply_markup=get_main_keyboard(s["lang"])
        )
        return

    if data.startswith("unit:"):
        s["unit"] = data.split(":")[1]
        msg = f"✅ واحد: {s['unit']}" if s["lang"] == "fa" else f"✅ Unit: {s['unit']}"
        await query.edit_message_text(msg)
        return


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

    # ✅ موقعیت مکانی
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # ✅ متن (شامل دکمه‌های کیبورد)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🤖 ربات در حال اجراست... Ctrl+C برای توقف")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
