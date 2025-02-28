import sqlite3
import requests
import io
import numpy as np
import time, math
from datetime import datetime, timedelta
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    BotCommand
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler
)

# Словари валют
fiat_info = {
    "USD": "US Dollar", "EUR": "Euro", "JPY": "Japanese Yen", "GBP": "British Pound",
    "AUD": "Australian Dollar", "CAD": "Canadian Dollar", "CHF": "Swiss Franc",
    "CNY": "Chinese Yuan", "SEK": "Swedish Krona", "NZD": "New Zealand Dollar",
    "MXN": "Mexican Peso", "SGD": "Singapore Dollar", "HKD": "Hong Kong Dollar",
    "NOK": "Norwegian Krone", "KRW": "South Korean Won", "TRY": "Turkish Lira",
    "INR": "Indian Rupee", "RUB": "Russian Ruble", "BRL": "Brazilian Real",
    "ZAR": "South African Rand", "DKK": "Danish Krone", "PLN": "Polish Zloty",
    "THB": "Thai Baht", "IDR": "Indonesian Rupiah", "HUF": "Hungarian Forint"
}

crypto_info = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "USDT": "Tether", "BNB": "Binance Coin",
    "XRP": "XRP", "ADA": "Cardano", "DOGE": "Dogecoin", "SOL": "Solana",
    "DOT": "Polkadot", "MATIC": "Polygon", "LTC": "Litecoin", "SHIB": "Shiba Inu",
    "TRX": "TRON", "AVAX": "Avalanche", "UNI": "Uniswap", "LINK": "Chainlink",
    "ATOM": "Cosmos", "ALGO": "Algorand", "XLM": "Stellar", "FTT": "FTX Token",
    "NEAR": "NEAR Protocol", "VET": "VeChain", "ICP": "Internet Computer",
    "MANA": "Decentraland", "EOS": "EOS", "AXS": "Axie Infinity"
}

# Для получения цены через альтернативные биржи
# Gate.io: https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT
# ByBit: https://api.bybit.com/spot/v1/ticker/24hr?symbol=BTCUSDT

# Глобальные переменные
bot_messages = {}  # {chat_id: [message_id, ...]}
session = requests.Session()

# Кэш для данных Центробанка (60 сек)
_cached_cbr_data = None
_cached_cbr_timestamp = 0


def get_cbr_data():
    global _cached_cbr_data, _cached_cbr_timestamp
    now = time.time()
    if _cached_cbr_data is None or now - _cached_cbr_timestamp > 60:
        _cached_cbr_data = session.get(CBR_API_URL, timeout=3).json()
        _cached_cbr_timestamp = now
    return _cached_cbr_data


# Функция для проверки и добавления столбца в таблицу
def ensure_column_exists(table: str, column: str, definition: str):
    with sqlite3.connect('subscriptions.db') as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table})")
        cols = [info[1] for info in cursor.fetchall()]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            conn.commit()


# Инициализация БД
def init_db():
    with sqlite3.connect('subscriptions.db') as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER,
                pair TEXT,
                threshold REAL DEFAULT NULL,
                PRIMARY KEY (user_id, pair)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                notifications BOOLEAN DEFAULT 1,
                default_currency TEXT DEFAULT 'USD'
            )
        ''')
    ensure_column_exists("user_settings", "data_source", "TEXT DEFAULT 'BINANCE'")


init_db()


# Работа с БД
def db_execute(query, params=()):
    with sqlite3.connect('subscriptions.db') as conn:
        conn.execute(query, params)
        conn.commit()


def db_fetchall(query, params=()):
    with sqlite3.connect('subscriptions.db') as conn:
        return conn.execute(query, params).fetchall()


# Функции настроек
def load_user_settings(user_id):
    settings = db_fetchall('SELECT notifications, default_currency, data_source FROM user_settings WHERE user_id = ?',
                           (user_id,))
    return {
        "notifications": bool(settings[0][0]),
        "default_currency": settings[0][1],
        "data_source": settings[0][2]
    } if settings else {"notifications": True, "default_currency": "USD", "data_source": "BINANCE"}


def save_user_settings(user_id, settings):
    db_execute('''
        INSERT OR REPLACE INTO user_settings (user_id, notifications, default_currency, data_source)
        VALUES (?, ?, ?, ?)
    ''', (user_id, settings['notifications'], settings['default_currency'], settings['data_source']))


# Конфигурация
BOT_TOKEN = 'YOUR_BOT_TOCKEN'
BINANCE_API_URL = 'https://api.binance.com/api/v3/ticker/price'
CBR_API_URL = 'https://www.cbr-xml-daily.ru/daily_json.js'

KEYBOARD = [
    ['🔄 Конвертер'],
    ['💰 Популярные валюты', '💵 Популярные криптовалюты'],
    ['📊 Подписки', '⚙️ Настройки'],
    ['❓ Помощь', '🗑 Очистить чат']
]


# Хелперы для ответа
def get_reply_target(update: Update):
    return update.message or (update.callback_query.message if update.callback_query else None)


async def tracked_reply(update: Update, text: str, **kwargs):
    target = get_reply_target(update)
    if target:
        msg = await target.reply_text(text, **kwargs)
        bot_messages.setdefault(target.chat.id, []).append(msg.message_id)
        return msg
    print("No target for reply")


# Функция конвертации фиатных валют с учётом Nominal (с кэшированием ЦБ)
def convert_fiat_value(value: float, from_cur: str, to_cur: str) -> float:
    data = get_cbr_data()

    def get_rate(cur):
        return 1.0 if cur == "RUB" else data['Valute'][cur]['Value'] / data['Valute'][cur]['Nominal']

    rub_value = value * get_rate(from_cur)
    return rub_value if to_cur == "RUB" else rub_value / get_rate(to_cur)


# Функция получения цены криптовалюты с выбранного источника
def get_crypto_price_api(crypto: str, source: str) -> float:
    if source == "BINANCE":
        resp = session.get(f"{BINANCE_API_URL}?symbol={crypto}USDT", timeout=3)
        data = resp.json()
        if 'price' not in data:
            raise Exception("Пара не найдена")
        return float(data['price'])
    elif source == "GATEIO":
        url = f"https://api.gateio.ws/api/v4/spot/tickers?currency_pair={crypto}_USDT"
        resp = session.get(url, timeout=3)
        data = resp.json()
        if not data:
            raise Exception("Нет данных с Gate.io")
        return float(data[0]['last'])
    elif source == "BYBIT":
        if crypto == "SHIB":
            raise Exception("ByBit не поддерживает SHIB")
        url = f"https://api.bybit.com/spot/v1/ticker/24hr?symbol={crypto}USDT"
        resp = session.get(url, timeout=3)
        data = resp.json()
        if data.get("ret_code", -1) != 0:
            raise Exception("Ошибка от ByBit")
        return float(data["result"]["lastPrice"])
    else:
        raise Exception("Неизвестный источник данных")


# Конвертация криптовалют
async def convert_crypto_command(update: Update, context: ContextTypes.DEFAULT_TYPE, crypto: str, target_currency: str):
    settings = load_user_settings(update.effective_user.id)
    source = settings.get("data_source", "BINANCE")
    try:
        usd_price = get_crypto_price_api(crypto, source)
        price = usd_price if target_currency == "USD" else convert_fiat_value(usd_price, "USD", target_currency)
        price_str = f"{price:,.8f}" if price < 0.01 else f"{price:,.2f}"
        await tracked_reply(update,
                            f"🪙 {crypto}\nЦена: {price_str} {target_currency}\nИсточник: {source}",
                            parse_mode="HTML")
    except Exception as e:
        await tracked_reply(update, f"❌ Ошибка: {str(e)}", parse_mode="HTML")


# Конвертация фиатных валют
async def convert_fiat_command(update: Update, context: ContextTypes.DEFAULT_TYPE, from_cur: str, to_cur: str):
    try:
        rate = convert_fiat_value(1, from_cur, to_cur)
        await tracked_reply(update,
                            f"💵 {from_cur} → {to_cur}\nКурс: 1 {from_cur} = {rate:,.2f} {to_cur}\nИсточник: ЦБ РФ",
                            parse_mode="HTML")
    except Exception as e:
        await tracked_reply(update, f"❌ Ошибка: {str(e)}", parse_mode="HTML")


# Команда /convert
async def convert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 1:
        return await tracked_reply(update, "Использование: /convert <Код> [Целевая]")
    from_code = args[0].upper()
    to_code = args[1].upper() if len(args) > 1 else load_user_settings(update.effective_user.id).get("default_currency",
                                                                                                     "USD")
    if from_code in crypto_info:
        await convert_crypto_command(update, context, from_code, to_code)
    elif from_code in fiat_info:
        await convert_fiat_command(update, context, from_code, to_code)
    else:
        await tracked_reply(update, f"❌ Неизвестная валюта: {from_code}")


# Inline-меню для популярных валют
async def crypto_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("BTC", callback_data="crypto_BTC"),
         InlineKeyboardButton("ETH", callback_data="crypto_ETH"),
         InlineKeyboardButton("DOGE", callback_data="crypto_DOGE")]
    ])
    await tracked_reply(update, "Выберите криптовалюту для конвертации:", reply_markup=inline_kb)


async def fiat_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("USD", callback_data="fiat_USD"),
         InlineKeyboardButton("EUR", callback_data="fiat_EUR"),
         InlineKeyboardButton("RUB", callback_data="fiat_RUB"),
         InlineKeyboardButton("GBP", callback_data="fiat_GBP")]
    ])
    await tracked_reply(update, "Выберите валюту для конвертации:", reply_markup=inline_kb)


# Функция, которая выводит все доступные на бирже криптовалюты с названиями (используя Binance exchangeInfo)
async def list_available_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = session.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5)
        data = resp.json()
        symbols = data.get("symbols", [])
        crypto_set = set()
        for sym in symbols:
            # Отбираем только пары с USDT
            if sym.get("quoteAsset") == "USDT":
                crypto_set.add(sym.get("baseAsset"))
        crypto_list = sorted(list(crypto_set))
        text = "💹 <b>Доступные криптовалюты на бирже (пары с USDT):</b>\n\n"
        for c in crypto_list:
            name = crypto_info.get(c, "Название неизвестно")
            text += f"• {c} — {name}\n"
        await tracked_reply(update, text, parse_mode="HTML")
    except Exception as e:
        await tracked_reply(update, f"❌ Ошибка: {str(e)}", parse_mode="HTML")


# Функция, которая выводит все доступные фиатные валюты (из нашего словаря)
async def list_available_fiat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💹 <b>Доступные фиатные валюты:</b>\n\n"
    for code, name in sorted(fiat_info.items()):
        text += f"• {code} — {name}\n"
    await tracked_reply(update, text, parse_mode="HTML")


# Функция, которая выводит цены на криптовалюту на разных платформах и разницу между ценами
async def compare_crypto_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        crypto = context.args[0].upper()
    except:
        return await tracked_reply(update, "Использование: /compare <Код> (например, /compare BTC)", parse_mode="HTML")
    results = {}
    sources = ["BINANCE", "GATEIO", "BYBIT"]
    for source in sources:
        try:
            price = get_crypto_price_api(crypto, source)
            results[source] = price
        except Exception as e:
            results[source] = None
    text = f"📊 <b>Сравнение цен для {crypto}:</b>\n\n"
    valid_prices = {}
    for source, price in results.items():
        if price is not None:
            valid_prices[source] = price
            text += f"• {source}: {price:,.2f} USD\n"
        else:
            text += f"• {source}: Нет данных\n"
    if valid_prices:
        max_price = max(valid_prices.values())
        min_price = min(valid_prices.values())
        diff = max_price - min_price
        pct = (diff / min_price * 100) if min_price != 0 else 0
        text += f"\nРазница: {diff:,.2f} USD ({pct:+.2f}%)"
    else:
        text += "\nНет данных для сравнения."
    await tracked_reply(update, text, parse_mode="HTML")


# Команда /check
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code = context.args[0].upper()
        if code in fiat_info:
            info = f"💵 {code}: {fiat_info[code]}"
        elif code in crypto_info:
            info = f"🪙 {code}: {crypto_info[code]}"
        else:
            info = f"❓ Нет данных для кода {code}"
    except:
        info = "Использование: /check <Код> (например, /check USD)"
    await tracked_reply(update, info, parse_mode="HTML")


# Команда /trend – поддержка периода с единицами (m, h, d, mo, y)
async def trend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code = context.args[0].upper()
    except:
        return await tracked_reply(update,
                                   "Использование: /trend <Код> [Период] (например, /trend BTC 24h или /trend EUR 1y)",
                                   parse_mode="HTML")
    if len(context.args) > 1:
        period_arg = context.args[1]
        num_part = ''.join(filter(str.isdigit, period_arg))
        unit_part = ''.join(filter(str.isalpha, period_arg)).lower()
        period_value = int(num_part) if num_part else 0
        if unit_part in {"m", "min"}:
            time_unit = "minutes"
        elif unit_part == "h":
            time_unit = "hours"
        elif unit_part == "d":
            time_unit = "days"
        elif unit_part in {"mo", "month", "months"}:
            time_unit = "months"
        elif unit_part in {"y", "year", "years"}:
            time_unit = "years"
        else:
            time_unit = "days"
    else:
        if code in crypto_info:
            period_value, time_unit = 24, "hours"
        elif code in fiat_info:
            period_value, time_unit = 30, "days"
        else:
            return await tracked_reply(update, f"❓ Нет данных для кода {code}", parse_mode="HTML")
    processing_msg = await tracked_reply(update,
                                         f"⏳ Обработка запроса для {code} за последние {period_value} {time_unit}...",
                                         parse_mode="HTML")
    if code in crypto_info:
        settings = load_user_settings(update.effective_user.id)
        source = settings.get("data_source", "BINANCE")
        if source == "BINANCE":
            if time_unit in {"minutes", "hours"} and ((time_unit == "minutes" and period_value < 60 * 48) or (
                    time_unit == "hours" and period_value < 48)):
                interval = "1m" if time_unit == "minutes" else "1h"
                limit = period_value
            else:
                interval = "1d"
                if time_unit == "days":
                    limit = period_value
                elif time_unit == "months":
                    limit = period_value * 30
                elif time_unit == "years":
                    limit = period_value * 365
                else:
                    limit = period_value
            url = f"https://api.binance.com/api/v3/klines?symbol={code}USDT&interval={interval}&limit={limit}"
            data = session.get(url, timeout=5).json()
            if not data or not isinstance(data, list):
                return await processing_msg.edit_text("❌ Нет данных для графика", parse_mode="HTML")
            date_format = '%H:%M' if interval in {"1m", "1h"} else '%d-%m'
            dates = [datetime.fromtimestamp(entry[0] / 1000).strftime(date_format) for entry in data]
            prices = [float(entry[4]) for entry in data]
        elif source in {"GATEIO", "BYBIT"}:
            interval = "1d"
            if time_unit in {"days", "months", "years"}:
                if time_unit == "days":
                    limit = period_value
                elif time_unit == "months":
                    limit = period_value * 30
                elif time_unit == "years":
                    limit = period_value * 365
                else:
                    limit = period_value
            else:
                limit = 24
            url = f"https://api.binance.com/api/v3/klines?symbol={code}USDT&interval={interval}&limit={limit}"
            data = session.get(url, timeout=5).json()
            dates = [datetime.fromtimestamp(entry[0] / 1000).strftime('%d-%m') for entry in data]
            prices = [float(entry[4]) for entry in data]
        pct_change = ((prices[-1] - prices[0]) / prices[0]) * 100
        caption = f"📈 {code} за последние {period_value} {time_unit}\nИзменение: {pct_change:+.2f}%\nИсточник: {source}"
    elif code in fiat_info:
        if time_unit in {"minutes", "hours"}:
            days = 1
        elif time_unit == "days":
            days = period_value
        elif time_unit == "months":
            days = period_value * 30
        elif time_unit == "years":
            days = period_value * 365
        else:
            days = period_value
        end_date = datetime.today()
        start_date = end_date - timedelta(days=days - 1)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        default = load_user_settings(update.effective_user.id)['default_currency']
        url = f"https://api.exchangerate.host/timeseries?start_date={start_str}&end_date={end_str}&base={code}&symbols={default}"
        data_json = session.get(url, timeout=5).json()
        rates = data_json.get("rates", {})
        if not rates:
            return await processing_msg.edit_text("❌ Нет данных для графика", parse_mode="HTML")
        sorted_dates = sorted(rates.keys())
        dates = [datetime.strptime(d, '%Y-%m-%d').strftime('%d-%m') for d in sorted_dates]
        prices = [rates[d][default] for d in sorted_dates]
        pct_change = ((prices[-1] - prices[0]) / prices[0]) * 100
        caption = f"📈 {code} → {default} за {days} дн.\nИзменение: {pct_change:+.2f}%\nИсточник: exchangerate.host"
    else:
        return await processing_msg.edit_text(f"❓ Нет данных для кода {code}", parse_mode="HTML")
    x = np.arange(len(prices))
    slope, intercept = np.polyfit(x, prices, 1)
    trend_line = slope * x + intercept
    plt.figure(figsize=(10, 6))
    plt.plot(x, prices, marker='o', linestyle='-', color='blue', label='Цена')
    plt.plot(x, trend_line, color='red', linestyle='--', linewidth=2, label='Тренд')
    plt.plot(x[-1], trend_line[-1], 'ro', markersize=5)
    plt.title(f"{code} – тренд")
    plt.xlabel("Время")
    ylabel = "Цена (USDT)" if code in crypto_info else f"Цена ({default})"
    plt.ylabel(ylabel)
    plt.legend()
    plt.grid(True)
    if len(x) > 12:
        indices = np.linspace(0, len(x) - 1, num=12, dtype=int)
        plt.xticks(indices, [dates[i] for i in indices], rotation=45)
    else:
        plt.xticks(x, dates, rotation=45)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    try:
        await processing_msg.edit_media(media=InputMediaPhoto(media=buf, caption=caption, parse_mode="HTML"))
    except Exception as e:
        target = get_reply_target(update)
        if target:
            await target.reply_photo(photo=buf, caption=caption, parse_mode="HTML")


# Команда /listcrypto – вывод всех доступных криптовалют с названиями (из Binance exchangeInfo)
async def list_available_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = session.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5)
        data = resp.json()
        symbols = data.get("symbols", [])
        crypto_set = set()
        for sym in symbols:
            if sym.get("quoteAsset") == "USDT":
                crypto_set.add(sym.get("baseAsset"))
        crypto_list = sorted(list(crypto_set))
        text = "💹 <b>Доступные криптовалюты (пары с USDT):</b>\n\n"
        for c in crypto_list:
            name = crypto_info.get(c, "Название неизвестно")
            text += f"• {c} — {name}\n"
        await tracked_reply(update, text, parse_mode="HTML")
    except Exception as e:
        await tracked_reply(update, f"❌ Ошибка: {str(e)}", parse_mode="HTML")


# Команда /listfiat – вывод всех доступных фиатных валют
async def list_available_fiat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💹 <b>Доступные фиатные валюты:</b>\n\n"
    for code, name in sorted(fiat_info.items()):
        text += f"• {code} — {name}\n"
    await tracked_reply(update, text, parse_mode="HTML")


# Команда /compare – вывод цен на криптовалюту на разных платформах и разница между ценами
async def compare_crypto_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        crypto = context.args[0].upper()
    except:
        return await tracked_reply(update, "Использование: /compare <Код> (например, /compare BTC)", parse_mode="HTML")
    results = {}
    sources = ["BINANCE", "GATEIO", "BYBIT"]
    for source in sources:
        try:
            price = get_crypto_price_api(crypto, source)
            results[source] = price
        except Exception as e:
            results[source] = None
    text = f"📊 <b>Сравнение цен для {crypto}:</b>\n\n"
    valid_prices = {}
    for source, price in results.items():
        if price is not None:
            valid_prices[source] = price
            text += f"• {source}: {price:,.2f} USD\n"
        else:
            text += f"• {source}: Нет данных\n"
    if valid_prices:
        max_price = max(valid_prices.values())
        min_price = min(valid_prices.values())
        diff = max_price - min_price
        pct = (diff / min_price * 100) if min_price != 0 else 0
        text += f"\nРазница между максимальной и минимальной ценой: {diff:,.2f} USD ({pct:+.2f}%)"
    else:
        text += "\nНет данных для сравнения."
    await tracked_reply(update, text, parse_mode="HTML")


# Команды подписки и прочие
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pair = context.args[0].upper()
        db_execute('INSERT OR IGNORE INTO subscriptions (user_id, pair) VALUES (?, ?)',
                   (update.effective_user.id, pair))
        await tracked_reply(update, f"✅ Подписка на {pair} добавлена", parse_mode="HTML")
    except:
        await tracked_reply(update, "❌ Используйте: /subscribe <Пара>")


async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = db_fetchall('SELECT pair FROM subscriptions WHERE user_id = ?', (update.effective_user.id,))
    text = "📋 Ваши подписки:\n" + "\n".join([f"• {s[0]}" for s in subs]) if subs else "Нет подписок"
    await tracked_reply(update, text)


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in bot_messages:
        for msg_id in bot_messages[chat_id]:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except:
                pass
        bot_messages[chat_id] = []
    await tracked_reply(update, "🗑 История очищена")


# Обработка inline-кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data.startswith("crypto_"):
        await convert_crypto_command(update, context, data.split("_")[1],
                                     load_user_settings(user_id)["default_currency"])
    elif data.startswith("fiat_"):
        await convert_fiat_command(update, context, data.split("_")[1])
    elif data == "toggle_notifications":
        settings = load_user_settings(user_id)
        settings["notifications"] = not settings["notifications"]
        save_user_settings(user_id, settings)
        status = "ВКЛ" if settings["notifications"] else "ВЫКЛ"
        await query.edit_message_text(f"⚙️ Уведомления: {status}")
    elif data == "change_default_currency":
        inline_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(c, callback_data=f"set_default_{c}") for c in ["USD", "EUR", "RUB", "GBP"]
        ]])
        await query.edit_message_text("Выберите валюту по умолчанию:", reply_markup=inline_kb)
    elif data.startswith("set_default_"):
        new_currency = data.split("_")[2]
        settings = load_user_settings(user_id)
        settings["default_currency"] = new_currency
        save_user_settings(user_id, settings)
        await query.edit_message_text(f"✅ Валюта по умолчанию: {new_currency}")
    elif data == "change_data_source":
        inline_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("BINANCE", callback_data="set_source_BINANCE"),
            InlineKeyboardButton("GATEIO", callback_data="set_source_GATEIO"),
            InlineKeyboardButton("BYBIT", callback_data="set_source_BYBIT")
        ]])
        await query.edit_message_text("Выберите источник данных для криптовалют:", reply_markup=inline_kb)
    elif data.startswith("set_source_"):
        new_source = data.split("_")[2]
        settings = load_user_settings(user_id)
        settings["data_source"] = new_source
        save_user_settings(user_id, settings)
        await query.edit_message_text(f"✅ Источник данных: {new_source}")
    elif data in ["show_rates_fiat", "show_rates_crypto"]:
        if "fiat" in data:
            await handle_show_rates_fiat(update, context)
        else:
            await handle_show_rates_crypto(update, context)
    else:
        await query.edit_message_text("Неизвестная команда.", parse_mode="HTML")


# Основной обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text in fiat_info or text in crypto_info:
        if text in crypto_info:
            await convert_crypto_command(update, context, text,
                                         load_user_settings(update.effective_user.id)["default_currency"])
        else:
            await convert_fiat_command(update, context, text)
    elif text == '🔄 КОНВЕРТЕР':
        await tracked_reply(update, "Введите код валюты для конвертации.\nПример: BTC или USD")
    elif text == '💰 ПОПУЛЯРНЫЕ ВАЛЮТЫ':
        await popular_currencies_initial(update, context)
    elif text == '💵 ПОПУЛЯРНЫЕ КРИПТОВАЛЮТЫ':
        await popular_cryptocurrencies_initial(update, context)
    elif text == '❓ ПОМОЩЬ':
        await help_command(update, context)
    elif text == '⚙️ НАСТРОЙКИ':
        await settings_command(update, context)
    elif text == '📊 ПОДПИСКИ':
        await show_subscriptions(update, context)
    elif text == '🗑 ОЧИСТИТЬ ЧАТ':
        await clear_history(update, context)
    elif text == '/LISTCRYPTO':
        await list_available_crypto(update, context)
    elif text == '/LISTFIAT':
        await list_available_fiat(update, context)
    elif text.startswith('/COMPARE'):
        await compare_crypto_prices(update, context)
    else:
        await tracked_reply(update, "Неизвестная команда. Введите /help", parse_mode="HTML")


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tracked_reply(update,
                        "👋 Добро пожаловать!\n\nИспользуйте кнопки ниже или введите /help для списка команд",
                        reply_markup=ReplyKeyboardMarkup(KEYBOARD, resize_keyboard=True))


# Команда /help – красивый вывод
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 <b>Доступные команды:</b>
• /start – Главное меню
• /convert <code>Код</code> [<code>Целевая</code>] – Конвертация валют
• /subscribe <code>Пара</code> – Подписка на уведомления
• /subscriptions – Список подписок
• /trend <code>Код</code> [<code>Период</code>] – График тренда 
      (Период задается числом и единицей: m – минуты, h – часы, d – дни, mo – месяцы, y – годы)
• /check <code>Код</code> – Информация о валюте
• /listcrypto – Список всех криптовалют на бирже
• /listfiat – Список всех фиатных валют
• /compare <code>Код</code> – Сравнение цен криптовалюты на разных платформах
• /settings – Настройки (уведомления, валюта по умолчанию, источник данных)
• /clear – Очистка истории

<b>Примеры:</b>
• /convert BTC
• /convert BTC RUB
• /subscribe BTCUSDT
• /trend ETH 48h
• /trend EUR 1y
• /check USD
• /listcrypto
• /listfiat
• /compare BTC
    """
    await tracked_reply(update, help_text, parse_mode="HTML")


# Команда /settings – настройка уведомлений, валюты и источника данных
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = load_user_settings(update.effective_user.id)
    status = "ВКЛ" if settings["notifications"] else "ВЫКЛ"
    text = (f"⚙️ <b>Настройки</b>:\n"
            f"Уведомления: {status}\n"
            f"Валюта по умолчанию: {settings['default_currency']}\n"
            f"Источник данных: {settings.get('data_source', 'BINANCE')}")
    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Вкл/Выкл уведомлений", callback_data="toggle_notifications")],
        [InlineKeyboardButton("Изменить валюту", callback_data="change_default_currency")],
        [InlineKeyboardButton("Изменить источник данных", callback_data="change_data_source")]
    ])
    await tracked_reply(update, text, reply_markup=inline_kb, parse_mode="HTML")


# Команда /subscribe
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pair = context.args[0].upper()
        db_execute('INSERT OR IGNORE INTO subscriptions (user_id, pair) VALUES (?, ?)',
                   (update.effective_user.id, pair))
        await tracked_reply(update, f"✅ Подписка на {pair} добавлена", parse_mode="HTML")
    except:
        await tracked_reply(update, "❌ Используйте: /subscribe <Пара>")


# Команда /subscriptions
async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = db_fetchall('SELECT pair FROM subscriptions WHERE user_id = ?', (update.effective_user.id,))
    text = "📋 Ваши подписки:\n" + "\n".join([f"• {s[0]}" for s in subs]) if subs else "Нет подписок"
    await tracked_reply(update, text)


# Команда /clear
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in bot_messages:
        for msg_id in bot_messages[chat_id]:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except:
                pass
        bot_messages[chat_id] = []
    await tracked_reply(update, "🗑 История очищена")


# Команда /listcrypto – вывод всех доступных криптовалют с названиями
async def list_available_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = session.get("https://api.binance.com/api/v3/exchangeInfo", timeout=5)
        data = resp.json()
        symbols = data.get("symbols", [])
        crypto_set = set()
        for sym in symbols:
            if sym.get("quoteAsset") == "USDT":
                crypto_set.add(sym.get("baseAsset"))
        crypto_list = sorted(list(crypto_set))
        text = "💹 <b>Доступные криптовалюты (пары с USDT):</b>\n\n"
        for c in crypto_list:
            name = crypto_info.get(c, "Название неизвестно")
            text += f"• {c} — {name}\n"
        await tracked_reply(update, text, parse_mode="HTML")
    except Exception as e:
        await tracked_reply(update, f"❌ Ошибка: {str(e)}", parse_mode="HTML")


# Команда /listfiat – вывод всех доступных фиатных валют
async def list_available_fiat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💹 <b>Доступные фиатные валюты:</b>\n\n"
    for code, name in sorted(fiat_info.items()):
        text += f"• {code} — {name}\n"
    await tracked_reply(update, text, parse_mode="HTML")


# Команда /compare – вывод цен криптовалюты на разных платформах и разница между ценами
async def compare_crypto_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        crypto = context.args[0].upper()
    except:
        return await tracked_reply(update, "Использование: /compare <Код> (например, /compare BTC)", parse_mode="HTML")
    results = {}
    sources = ["BINANCE", "GATEIO", "BYBIT"]
    for source in sources:
        try:
            price = get_crypto_price_api(crypto, source)
            results[source] = price
        except Exception as e:
            results[source] = None
    text = f"📊 <b>Сравнение цен для {crypto}:</b>\n\n"
    valid_prices = {}
    for source, price in results.items():
        if price is not None:
            valid_prices[source] = price
            text += f"• {source}: {price:,.2f} USD\n"
        else:
            text += f"• {source}: Нет данных\n"
    if valid_prices:
        max_price = max(valid_prices.values())
        min_price = min(valid_prices.values())
        diff = max_price - min_price
        pct = (diff / min_price * 100) if min_price != 0 else 0
        text += f"\nРазница: {diff:,.2f} USD ({pct:+.2f}%)"
    else:
        text += "\nНет данных для сравнения."
    await tracked_reply(update, text, parse_mode="HTML")


# Команда /trend – поддержка периода с единицами: m, h, d, mo, y
async def trend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        code = context.args[0].upper()
    except:
        return await tracked_reply(update,
                                   "Использование: /trend <Код> [Период] (например, /trend BTC 24h или /trend EUR 1y)",
                                   parse_mode="HTML")
    if len(context.args) > 1:
        period_arg = context.args[1]
        num_part = ''.join(filter(str.isdigit, period_arg))
        unit_part = ''.join(filter(str.isalpha, period_arg)).lower()
        period_value = int(num_part) if num_part else 0
        if unit_part in {"m", "min"}:
            time_unit = "minutes"
        elif unit_part == "h":
            time_unit = "hours"
        elif unit_part == "d":
            time_unit = "days"
        elif unit_part in {"mo", "month", "months"}:
            time_unit = "months"
        elif unit_part in {"y", "year", "years"}:
            time_unit = "years"
        else:
            time_unit = "days"
    else:
        if code in crypto_info:
            period_value, time_unit = 24, "hours"
        elif code in fiat_info:
            period_value, time_unit = 30, "days"
        else:
            return await tracked_reply(update, f"❓ Нет данных для кода {code}", parse_mode="HTML")
    processing_msg = await tracked_reply(update,
                                         f"⏳ Обработка запроса для {code} за последние {period_value} {time_unit}...",
                                         parse_mode="HTML")
    if code in crypto_info:
        settings = load_user_settings(update.effective_user.id)
        source = settings.get("data_source", "BINANCE")
        if source == "BINANCE":
            if time_unit in {"minutes", "hours"} and ((time_unit == "minutes" and period_value < 60 * 48) or (
                    time_unit == "hours" and period_value < 48)):
                interval = "1m" if time_unit == "minutes" else "1h"
                limit = period_value
            else:
                interval = "1d"
                if time_unit == "days":
                    limit = period_value
                elif time_unit == "months":
                    limit = period_value * 30
                elif time_unit == "years":
                    limit = period_value * 365
                else:
                    limit = period_value
            url = f"https://api.binance.com/api/v3/klines?symbol={code}USDT&interval={interval}&limit={limit}"
            data = session.get(url, timeout=5).json()
            if not data or not isinstance(data, list):
                return await processing_msg.edit_text("❌ Нет данных для графика", parse_mode="HTML")
            date_format = '%H:%M' if interval in {"1m", "1h"} else '%d-%m'
            dates = [datetime.fromtimestamp(entry[0] / 1000).strftime(date_format) for entry in data]
            prices = [float(entry[4]) for entry in data]
        elif source in {"GATEIO", "BYBIT"}:
            interval = "1d"
            if time_unit in {"days", "months", "years"}:
                if time_unit == "days":
                    limit = period_value
                elif time_unit == "months":
                    limit = period_value * 30
                elif time_unit == "years":
                    limit = period_value * 365
                else:
                    limit = period_value
            else:
                limit = 24
            url = f"https://api.binance.com/api/v3/klines?symbol={code}USDT&interval={interval}&limit={limit}"
            data = session.get(url, timeout=5).json()
            dates = [datetime.fromtimestamp(entry[0] / 1000).strftime('%d-%m') for entry in data]
            prices = [float(entry[4]) for entry in data]
        pct_change = ((prices[-1] - prices[0]) / prices[0]) * 100
        caption = f"📈 {code} за последние {period_value} {time_unit}\nИзменение: {pct_change:+.2f}%\nИсточник: {source}"
    elif code in fiat_info:
        if time_unit in {"minutes", "hours"}:
            days = 1
        elif time_unit == "days":
            days = period_value
        elif time_unit == "months":
            days = period_value * 30
        elif time_unit == "years":
            days = period_value * 365
        else:
            days = period_value
        end_date = datetime.today()
        start_date = end_date - timedelta(days=days - 1)
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
        default = load_user_settings(update.effective_user.id)['default_currency']
        url = f"https://api.exchangerate.host/timeseries?start_date={start_str}&end_date={end_str}&base={code}&symbols={default}"
        data_json = session.get(url, timeout=5).json()
        rates = data_json.get("rates", {})
        if not rates:
            return await processing_msg.edit_text("❌ Нет данных для графика", parse_mode="HTML")
        sorted_dates = sorted(rates.keys())
        dates = [datetime.strptime(d, '%Y-%m-%d').strftime('%d-%m') for d in sorted_dates]
        prices = [rates[d][default] for d in sorted_dates]
        pct_change = ((prices[-1] - prices[0]) / prices[0]) * 100
        caption = f"📈 {code} → {default} за {days} дн.\nИзменение: {pct_change:+.2f}%\nИсточник: exchangerate.host"
    else:
        return await processing_msg.edit_text(f"❓ Нет данных для кода {code}", parse_mode="HTML")
    x = np.arange(len(prices))
    slope, intercept = np.polyfit(x, prices, 1)
    trend_line = slope * x + intercept
    plt.figure(figsize=(10, 6))
    plt.plot(x, prices, marker='o', linestyle='-', color='blue', label='Цена')
    plt.plot(x, trend_line, color='red', linestyle='--', linewidth=2, label='Тренд')
    plt.plot(x[-1], trend_line[-1], 'ro', markersize=5)
    plt.title(f"{code} – тренд")
    plt.xlabel("Время")
    ylabel = "Цена (USDT)" if code in crypto_info else f"Цена ({default})"
    plt.ylabel(ylabel)
    plt.legend()
    plt.grid(True)
    if len(x) > 12:
        indices = np.linspace(0, len(x) - 1, num=12, dtype=int)
        plt.xticks(indices, [dates[i] for i in indices], rotation=45)
    else:
        plt.xticks(x, dates, rotation=45)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    try:
        await processing_msg.edit_media(media=InputMediaPhoto(media=buf, caption=caption, parse_mode="HTML"))
    except Exception as e:
        target = get_reply_target(update)
        if target:
            await target.reply_photo(photo=buf, caption=caption, parse_mode="HTML")


# Команды подписки и отображения подписок
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pair = context.args[0].upper()
        db_execute('INSERT OR IGNORE INTO subscriptions (user_id, pair) VALUES (?, ?)',
                   (update.effective_user.id, pair))
        await tracked_reply(update, f"✅ Подписка на {pair} добавлена", parse_mode="HTML")
    except:
        await tracked_reply(update, "❌ Используйте: /subscribe <Пара>")


async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = db_fetchall('SELECT pair FROM subscriptions WHERE user_id = ?', (update.effective_user.id,))
    text = "📋 Ваши подписки:\n" + "\n".join([f"• {s[0]}" for s in subs]) if subs else "Нет подписок"
    await tracked_reply(update, text)


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in bot_messages:
        for msg_id in bot_messages[chat_id]:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except:
                pass
        bot_messages[chat_id] = []
    await tracked_reply(update, "🗑 История очищена")


# Обработка inline-кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if data.startswith("crypto_"):
        await convert_crypto_command(update, context, data.split("_")[1],
                                     load_user_settings(user_id)["default_currency"])
    elif data.startswith("fiat_"):
        await convert_fiat_command(update, context, data.split("_")[1])
    elif data == "toggle_notifications":
        settings = load_user_settings(user_id)
        settings["notifications"] = not settings["notifications"]
        save_user_settings(user_id, settings)
        status = "ВКЛ" if settings["notifications"] else "ВЫКЛ"
        await query.edit_message_text(f"⚙️ Уведомления: {status}")
    elif data == "change_default_currency":
        inline_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(c, callback_data=f"set_default_{c}") for c in ["USD", "EUR", "RUB", "GBP"]
        ]])
        await query.edit_message_text("Выберите валюту по умолчанию:", reply_markup=inline_kb)
    elif data.startswith("set_default_"):
        new_currency = data.split("_")[2]
        settings = load_user_settings(user_id)
        settings["default_currency"] = new_currency
        save_user_settings(user_id, settings)
        await query.edit_message_text(f"✅ Валюта по умолчанию: {new_currency}")
    elif data == "change_data_source":
        inline_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("BINANCE", callback_data="set_source_BINANCE"),
            InlineKeyboardButton("GATEIO", callback_data="set_source_GATEIO"),
            InlineKeyboardButton("BYBIT", callback_data="set_source_BYBIT")
        ]])
        await query.edit_message_text("Выберите источник данных для криптовалют:", reply_markup=inline_kb)
    elif data.startswith("set_source_"):
        new_source = data.split("_")[2]
        settings = load_user_settings(user_id)
        settings["data_source"] = new_source
        save_user_settings(user_id, settings)
        await query.edit_message_text(f"✅ Источник данных: {new_source}")
    elif data in ["show_rates_fiat", "show_rates_crypto"]:
        if "fiat" in data:
            await handle_show_rates_fiat(update, context)
        else:
            await handle_show_rates_crypto(update, context)
    else:
        await query.edit_message_text("Неизвестная команда.", parse_mode="HTML")


# Основной обработчик текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    if text in fiat_info or text in crypto_info:
        if text in crypto_info:
            await convert_crypto_command(update, context, text,
                                         load_user_settings(update.effective_user.id)["default_currency"])
        else:
            await convert_fiat_command(update, context, text)
    elif text == '🔄 КОНВЕРТЕР':
        await tracked_reply(update, "Введите код валюты для конвертации.\nПример: BTC или USD")
    elif text == '💰 ПОПУЛЯРНЫЕ ВАЛЮТЫ':
        await popular_currencies_initial(update, context)
    elif text == '💵 ПОПУЛЯРНЫЕ КРИПТОВАЛЮТЫ':
        await popular_cryptocurrencies_initial(update, context)
    elif text == '❓ ПОМОЩЬ':
        await help_command(update, context)
    elif text == '⚙️ НАСТРОЙКИ':
        await settings_command(update, context)
    elif text == '📊 ПОДПИСКИ':
        await show_subscriptions(update, context)
    elif text == '🗑 ОЧИСТИТЬ ЧАТ':
        await clear_history(update, context)
    elif text == '/LISTCRYPTO':
        await list_available_crypto(update, context)
    elif text == '/LISTFIAT':
        await list_available_fiat(update, context)
    elif text.startswith('/COMPARE'):
        await compare_crypto_prices(update, context)
    else:
        await tracked_reply(update, "Неизвестная команда. Введите /help", parse_mode="HTML")


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tracked_reply(update,
                        "👋 Добро пожаловать!\n\nИспользуйте кнопки ниже или введите /help для списка команд",
                        reply_markup=ReplyKeyboardMarkup(KEYBOARD, resize_keyboard=True))


# Команда /help – красивый вывод
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 <b>Доступные команды:</b>
• /start – Главное меню
• /convert <code>Код</code> [<code>Целевая</code>] – Конвертация валют
• /subscribe <code>Пара</code> – Подписка на уведомления
• /subscriptions – Список подписок
• /trend <code>Код</code> [<code>Период</code>] – График тренда 
      (Период задается числом и единицей: m – минуты, h – часы, d – дни, mo – месяцы, y – годы)
      Для криптовалют: если период меньше 48 часов – почасовой, иначе – дневной.
      Для фиатных валют используется дневной тренд.
• /check <code>Код</code> – Информация о валюте
• /listcrypto – Список всех доступных криптовалют на бирже
• /listfiat – Список всех доступных фиатных валют
• /compare <code>Код</code> – Сравнение цен криптовалюты на разных платформах
• /settings – Настройки (уведомления, валюта по умолчанию, источник данных)
• /clear – Очистка истории

<b>Примеры:</b>
• /convert BTC
• /convert BTC RUB
• /subscribe BTCUSDT
• /trend ETH 48h
• /trend EUR 1y
• /check USD
• /listcrypto
• /listfiat
• /compare BTC
    """
    await tracked_reply(update, help_text, parse_mode="HTML")


# Команда /settings – настройка уведомлений, валюты и источника данных
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = load_user_settings(update.effective_user.id)
    status = "ВКЛ" if settings["notifications"] else "ВЫКЛ"
    text = (f"⚙️ <b>Настройки</b>:\n"
            f"Уведомления: {status}\n"
            f"Валюта по умолчанию: {settings['default_currency']}\n"
            f"Источник данных: {settings.get('data_source', 'BINANCE')}")
    inline_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Вкл/Выкл уведомлений", callback_data="toggle_notifications")],
        [InlineKeyboardButton("Изменить валюту", callback_data="change_default_currency")],
        [InlineKeyboardButton("Изменить источник данных", callback_data="change_data_source")]
    ])
    await tracked_reply(update, text, reply_markup=inline_kb, parse_mode="HTML")


# Команда /subscribe
async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pair = context.args[0].upper()
        db_execute('INSERT OR IGNORE INTO subscriptions (user_id, pair) VALUES (?, ?)',
                   (update.effective_user.id, pair))
        await tracked_reply(update, f"✅ Подписка на {pair} добавлена", parse_mode="HTML")
    except:
        await tracked_reply(update, "❌ Используйте: /subscribe <Пара>")


# Команда /subscriptions
async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = db_fetchall('SELECT pair FROM subscriptions WHERE user_id = ?', (update.effective_user.id,))
    text = "📋 Ваши подписки:\n" + "\n".join([f"• {s[0]}" for s in subs]) if subs else "Нет подписок"
    await tracked_reply(update, text)


# Команда /clear
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in bot_messages:
        for msg_id in bot_messages[chat_id]:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except:
                pass
        bot_messages[chat_id] = []
    await tracked_reply(update, "🗑 История очищена")


# Пост и установка команд для бота
async def post_init(application: Application):
    commands = [
        BotCommand("start", "Главное меню"),
        BotCommand("convert", "Конвертация валют"),
        BotCommand("subscribe", "Подписка на пару"),
        BotCommand("trend", "График тренда"),
        BotCommand("check", "Информация о валюте"),
        BotCommand("listcrypto", "Список криптовалют"),
        BotCommand("listfiat", "Список фиатов"),
        BotCommand("compare", "Сравнение цен криптовалют"),
        BotCommand("settings", "Настройки"),
        BotCommand("clear", "Очистка истории")
    ]
    await application.bot.set_my_commands(commands)


# Основной запуск бота
def main():
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
        .build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('convert', convert_command))
    application.add_handler(CommandHandler('check', check_command))
    application.add_handler(CommandHandler('trend', trend_command))
    application.add_handler(CommandHandler('subscribe', subscribe))
    application.add_handler(CommandHandler('subscriptions', show_subscriptions))
    application.add_handler(CommandHandler('settings', settings_command))
    application.add_handler(CommandHandler('clear', clear_history))
    application.add_handler(CommandHandler('listcrypto', list_available_crypto))
    application.add_handler(CommandHandler('listfiat', list_available_fiat))
    application.add_handler(CommandHandler('compare', compare_crypto_prices))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.run_polling()


# Добавляем новые функции после существующих обработчиков

async def popular_currencies_initial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню с популярными фиатными валютами"""
    buttons = [
        [InlineKeyboardButton("USD", callback_data="fiat_USD"),
         InlineKeyboardButton("EUR", callback_data="fiat_EUR")],
        [InlineKeyboardButton("GBP", callback_data="fiat_GBP"),
         InlineKeyboardButton("JPY", callback_data="fiat_JPY")],
        [InlineKeyboardButton("Показать все курсы", callback_data="show_rates_fiat")]
    ]
    await tracked_reply(update, "💰 Популярные фиатные валюты:",
                        reply_markup=InlineKeyboardMarkup(buttons))


async def popular_cryptocurrencies_initial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню с популярными криптовалютами"""
    buttons = [
        [InlineKeyboardButton("BTC", callback_data="crypto_BTC"),
         InlineKeyboardButton("ETH", callback_data="crypto_ETH")],
        [InlineKeyboardButton("DOGE", callback_data="crypto_DOGE"),
         InlineKeyboardButton("BNB", callback_data="crypto_BNB")],
        [InlineKeyboardButton("Показать все курсы", callback_data="show_rates_crypto")]
    ]
    await tracked_reply(update, "💵 Популярные криптовалюты:",
                        reply_markup=InlineKeyboardMarkup(buttons))


async def handle_show_rates_fiat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает курсы популярных фиатных валют"""
    try:
        text = "📊 Текущие курсы фиатных валют:\n\n"
        for code in ["USD", "EUR", "GBP", "JPY", "CNY"]:
            rate = convert_fiat_value(1, code, "RUB")
            text += f"• 1 {code} = {rate:.2f} RUB\n"
        await update.callback_query.edit_message_text(text)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ Ошибка: {str(e)}")


async def handle_show_rates_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает курсы популярных криптовалют"""
    try:
        user_id = update.effective_user.id
        settings = load_user_settings(user_id)
        text = "📊 Текущие курсы криптовалют:\n\n"

        for code in ["BTC", "ETH", "BNB", "DOGE", "XRP"]:
            try:
                price = get_crypto_price_api(code, settings["data_source"])
                text += f"• 1 {code} = {price:,.2f} USD\n"
            except:
                text += f"• {code}: Нет данных\n"

        await update.callback_query.edit_message_text(text)
    except Exception as e:
        await update.callback_query.edit_message_text(f"❌ Ошибка: {str(e)}")

if __name__ == '__main__':
    main()
