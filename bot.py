import os
import asyncio
import time
import logging
import base64
import threading
import binascii
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# KEEP ALIVE WEB SERVER BUAT UPTIMEROBOT/RENDER
# ============================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive"

def run_web():
    app.run(host='0.0.0', port=int(os.getenv("PORT", 8080)))

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# ============================================================
# CONFIG - AMBIL DARI ENV BIAR AMAN DI GITHUB
# ============================================================

WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5")
USDT_JETTON_MASTER = os.getenv("USDT_JETTON_MASTER", "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs")
TONCENTER_BASE = os.getenv("TONCENTER_BASE", "https://toncenter.com/api/v3").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()
POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "20")))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

AUTO_MONITOR_CHAT_IDS = {
    x.strip()
    for x in os.getenv("AUTO_MONITOR_CHAT_IDS", "").split(",")
    if x.strip()
}

# ============================================================
# GLOBAL STATE
# ============================================================

logging.basicConfig(
    format=("%(asctime)s | %(levelname)s | %(name)s | %(message)s"),
    level=logging.INFO,
)
logger = logging.getLogger("telegram-ton-monitor")
monitor_chats: set[str] = set(AUTO_MONITOR_CHAT_IDS)
seen_event_ids: set[str] = set()
baseline_ready = False
http_client: httpx.AsyncClient | None = None

# ============================================================
# DECIMAL / FORMAT
# ============================================================

def format_decimal(value: Decimal, max_places: int = 8) -> str:
    text = format(value, "f")
    if "." not in text:
        return text
    whole, fraction = text.split(".", 1)
    fraction = fraction[:max_places]
    fraction = fraction.rstrip("0")
    if not fraction:
        return whole
    return f"{whole}.{fraction}"

def format_amount(raw: str | int | None, decimals: int) -> str:
    try:
        value = (Decimal(str(raw or "0")) / (Decimal(10) ** decimals))
        return format_decimal(value, min(decimals, 8))
    except (InvalidOperation, ValueError, TypeError):
        return str(raw or "0")

def format_ton(raw: str | int | None) -> str:
    return format_amount(raw, 9)

# ============================================================
# TIME
# ============================================================

def fmt_time(timestamp: int | float | None) -> str:
    if not timestamp:
        return "-"
    try:
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).astimezone(LOCAL_TZ)
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "-"

# ============================================================
# HTML
# ============================================================

def html_escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

# ============================================================
# TON ADDRESS CONVERSION
# ============================================================

def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc

def raw_address_to_friendly(address: str, bounceable: bool = False) -> str:
    if not address:
        return address
    address = str(address).strip()
    if address.startswith(("EQ", "UQ", "kQ", "0Q")):
        return address
    if ":" not in address:
        return address
    try:
        wc_text, hash_text = address.split(":", 1)
        wc = int(wc_text)
        hash_text = hash_text.strip()
        if len(hash_text)!= 64:
            return address
        account_hash = bytes.fromhex(hash_text)
        tag = (0x11 if bounceable else 0x51)
        payload = bytes([tag, wc & 0xFF]) + account_hash
        checksum = crc16_xmodem(payload)
        raw = payload + checksum.to_bytes(2, "big")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    except (ValueError, TypeError, binascii.Error):
        return address

def friendly_address(address: str | None) -> str:
    if not address:
        return "-"
    address = str(address).strip()
    if address.startswith(("EQ", "UQ", "kQ", "0Q")):
        return address
    return raw_address_to_friendly(address, bounceable=False)

# ============================================================
# EXPLORER
# ============================================================

def explorer_url(value: str) -> str:
    return ("https://tonviewer.com/" + value)

# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Info Saldo", callback_data="balance"), InlineKeyboardButton("🟣 10 Transaksi TON", callback_data="tx_ton")],
        [InlineKeyboardButton("🪙 10 Transaksi USDT", callback_data="tx_usdt"), InlineKeyboardButton("🪙 Token Dimiliki", callback_data="tokens")],
        [InlineKeyboardButton("👁 Memantau Wallet", callback_data="monitor"), InlineKeyboardButton("🔄 Refresh", callback_data="home")],
    ])

def back_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="home")]])

def monitor_markup(chat_id: str) -> InlineKeyboardMarkup:
    status = ("🟢 ON" if chat_id in monitor_chats else "⚪ OFF")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Status notifikasi: {status}", callback_data="monitor")],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="home")],
    ])

# ============================================================
# API
# ============================================================

async def api_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global http_client
    if http_client is None:
        raise RuntimeError("HTTP client belum siap.")
    headers: dict[str, str] = {}
    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY
    url = (f"{TONCENTER_BASE}/{path.lstrip('/')}")
    response = await http_client.get(url, params=params or {}, headers=headers)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        if data.get("code") not in (None, 0):
            raise RuntimeError(str(data))
    return data

# [SEMUA FUNGSI KAMU DARI get_ton_transactions SAMPAI show_home TETAP SAMA]
# BIAR GAK KEPANJANGAN, KODE NYA GAK AKU RUBAH SAMA SEKALI
# COPY AJA DARI KODE LAMA KAMU MULAI DARI SINI...

# ============================================================
# POST INIT - NOTIFIKASI OTOMATIS JALAN DI SINI
# ============================================================

async def post_init(application: Application) -> None:
    global http_client
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum diset.")
    timeout = httpx.Timeout(20.0, connect=10.0)
    http_client = httpx.AsyncClient(timeout=timeout)

    await application.bot.set_my_commands([
        BotCommand("start", "Buka menu utama"),
        BotCommand("menu", "Buka menu utama"),
        BotCommand("chatid", "Lihat Chat ID"),
    ])
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    if not TONCENTER_API_KEY:
        logger.warning("TONCENTER_API_KEY belum diset; request API v3 menggunakan rate limit publik.")

    # INI YANG BIKIN NOTIF 24 JAM
    application.bot_data["monitor_task"] = asyncio.create_task(monitor_loop(application))
    logger.info("post_init selesai.")

# ============================================================
# POST SHUTDOWN
# ============================================================

async def post_shutdown(application: Application) -> None:
    global http_client
    task = application.bot_data.get("monitor_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if http_client:
        await http_client.aclose()
        http_client = None

# ============================================================
# MAIN
# ============================================================

def main() -> None:
    keep_alive() # JALANKAN WEB SERVER DULU

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN belum diset sebagai environment variable.")

    application = (
        ApplicationBuilder()
       .token(TELEGRAM_BOT_TOKEN)
       .post_init(post_init)
       .post_shutdown(post_shutdown)
       .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot Telegram mulai polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
