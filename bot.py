import os
import asyncio
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"

USDT_JETTON_WALLET = "EQAmwNPCaojho0YTS8ZfwnK5zHjduMZeZbeie5dLHeFTAWD7"
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

TONCENTER_BASE = "https://toncenter.com/api/v3"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_KEY = os.getenv("TONCENTER_API_KEY", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Jakarta")

TZ = ZoneInfo(TIMEZONE_NAME)


# ============================================================
# CHAT ID
# Mendukung CHAT_ID maupun CHAT_IDS
# ============================================================

CHAT_IDS = set()

chat_id_single = os.getenv("CHAT_ID", "").strip()

if chat_id_single:
    CHAT_IDS.add(chat_id_single)

chat_ids_multiple = os.getenv("CHAT_IDS", "")

for x in chat_ids_multiple.split(","):
    x = x.strip()

    if x:
        CHAT_IDS.add(x)


monitor_users = set()
seen_events = set()

baseline_ready = False
client = None


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# MENU
# ============================================================

def menu():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("💰 Info Saldo"),
                KeyboardButton("📜 20 Transaksi"),
            ],
            [
                KeyboardButton("🪙 Token Dimiliki"),
                KeyboardButton("👁 Memantau Wallet"),
            ],
            [
                KeyboardButton("🔄 Refresh"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
# UTIL
# ============================================================

def esc(text):

    if text is None:
        return "-"

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def safe_int(value, default=0):

    if value is None:
        return default

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value, default=""):

    if value is None:
        return default

    return str(value)


def short(addr):

    if not addr:
        return "-"

    if len(addr) < 18:
        return addr

    return addr[:9] + "..." + addr[-8:]


def fmt_time(ts):

    ts = safe_int(ts)

    if ts <= 0:
        return "-"

    try:

        dt = datetime.fromtimestamp(
            ts,
            tz=timezone.utc
        ).astimezone(TZ)

        return dt.strftime("%d/%m/%Y %H:%M:%S")

    except Exception:

        return "-"


def amount(raw, decimals):

    try:

        if raw is None:
            return "0"

        value = Decimal(str(raw)) / (
            Decimal(10) ** safe_int(decimals, 9)
        )

        text = format(value, "f").rstrip("0").rstrip(".")

        return text if text else "0"

    except (InvalidOperation, ValueError, TypeError):

        return "0"


# ============================================================
# TONCENTER API
# ============================================================

async def api(path, params=None):

    headers = {}

    if API_KEY:
        headers["X-API-Key"] = API_KEY

    r = await client.get(
        TONCENTER_BASE + path,
        params=params,
        headers=headers,
    )

    r.raise_for_status()

    return r.json()


# ============================================================
# WALLET
# ============================================================

async def wallet_state():

    data = await api(
        "/walletStates",
        {
            "address": WALLET_ADDRESS,
        },
    )

    wallets = data.get("wallets", [])

    if wallets:
        return wallets[0]

    return {}


async def jetton_wallets():

    return await api(
        "/jetton/wallets",
        {
            "owner_address": WALLET_ADDRESS,
            "exclude_zero_balance": "true",
            "limit": 100,
        },
    )


# ============================================================
# TRANSACTIONS
# ============================================================

async def ton_transactions():

    return await api(
        "/transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": 100,
            "sort": "desc",
        },
    )


async def jetton_in():

    return await api(
        "/jetton/transfers",
        {
            "owner_address": [WALLET_ADDRESS],
            "direction": "in",
            "limit": 1000,
            "sort": "desc",
        },
    )


async def jetton_out():

    return await api(
        "/jetton/transfers",
        {
            "owner_address": [WALLET_ADDRESS],
            "direction": "out",
            "limit": 1000,
            "sort": "desc",
        },
    )


# ============================================================
# METADATA
# ============================================================

def metadata(response, master):

    data = response.get("metadata", {}) or {}

    master = safe_str(master)

    if master in data:

        value = data.get(master) or {}

        info = value.get("token_info", []) or []

        if info:
            return info[0] or {}

    for value in data.values():

        if not isinstance(value, dict):
            continue

        info = value.get("token_info", []) or []

        if info:
            return info[0] or {}

    return {}


def decimals(info, master):

    if master == USDT_JETTON_MASTER:
        return 6

    info = info or {}

    extra = info.get("extra", {}) or {}

    for key in ["decimals", "decimal"]:

        if key in extra:

            try:
                return int(extra[key])
            except (TypeError, ValueError):
                pass

    try:
        return int(info.get("decimals", 9))
    except (TypeError, ValueError):
        return 9


# ============================================================
# NORMALIZE JETTON
# ============================================================

def normalize_jetton(item, response, direction):

    item = item or {}

    master = safe_str(
        item.get("jetton_master")
    )

    info = metadata(
        response,
        master
    )

    symbol = safe_str(
        info.get("symbol"),
        "JETTON"
    )

    if master == USDT_JETTON_MASTER:
        symbol = "USDT"

    name = safe_str(
        info.get("name"),
        symbol
    )

    dec = decimals(
        info,
        master
    )

    source = safe_str(
        item.get("source")
    )

    destination = safe_str(
        item.get("destination")
    )

    txhash = safe_str(
        item.get("transaction_hash")
    )

    timestamp = safe_int(
        item.get("transaction_now")
    )

    return {
        "id": (
            "J:"
            + txhash
            + ":"
            + direction
            + ":"
            + master
        ),

        "kind": "JETTON",

        "direction": direction,

        "symbol": symbol,

        "name": name,

        "amount": amount(
            item.get("amount", "0"),
            dec
        ),

        "source": source,

        "destination": destination,

        "timestamp": timestamp,

        "hash": txhash,

        "master": master,

        "aborted": bool(
            item.get(
                "transaction_aborted",
                False
            )
        ),
    }


# ============================================================
# NORMALIZE TON
# ============================================================

def normalize_ton(tx):

    tx = tx or {}

    events = []

    ts = safe_int(
        tx.get("now")
    )

    txhash = safe_str(
        tx.get("hash")
    )

    # --------------------------------------------------------
    # INCOMING TON
    # --------------------------------------------------------

    incoming = tx.get("in_msg") or {}

    source = safe_str(
        incoming.get("source")
    )

    value = safe_int(
        incoming.get("value")
    )

    if source and value > 0:

        events.append(
            {
                "id": "TI:" + txhash,

                "kind": "TON",

                "direction": "in",

                "symbol": "TON",

                "amount": amount(
                    value,
                    9
                ),

                "source": source,

                "destination": WALLET_ADDRESS,

                "timestamp": ts,

                "hash": txhash,
            }
        )

    # --------------------------------------------------------
    # OUTGOING TON
    # --------------------------------------------------------

    for i, msg in enumerate(
        tx.get("out_msgs") or []
    ):

        msg = msg or {}

        dst = safe_str(
            msg.get("destination")
        )

        val = safe_int(
            msg.get("value")
        )

        if dst and val > 0:

            events.append(
                {
                    "id": (
                        "TO:"
                        + txhash
                        + ":"
                        + str(i)
                    ),

                    "kind": "TON",

                    "direction": "out",

                    "symbol": "TON",

                    "amount": amount(
                        val,
                        9
                    ),

                    "source": WALLET_ADDRESS,

                    "destination": dst,

                    "timestamp": ts,

                    "hash": txhash,
                }
            )

    return events


# ============================================================
# RECENT EVENTS
# ============================================================

async def recent_events():

    ton, jin, jout = await asyncio.gather(
        ton_transactions(),
        jetton_in(),
        jetton_out(),
    )

    events = []

    # --------------------------------------------------------
    # TON
    # --------------------------------------------------------

    for tx in (
        ton.get("transactions", [])
        or []
    ):

        events.extend(
            normalize_ton(tx)
        )

    # --------------------------------------------------------
    # JETTON MASUK
    # --------------------------------------------------------

    for item in (
        jin.get("jetton_transfers", [])
        or []
    ):

        if not item.get(
            "transaction_aborted",
            False
        ):

            events.append(
                normalize_jetton(
                    item,
                    jin,
                    "in"
                )
            )

    # --------------------------------------------------------
    # JETTON KELUAR
    # --------------------------------------------------------

    for item in (
        jout.get("jetton_transfers", [])
        or []
    ):

        if not item.get(
            "transaction_aborted",
            False
        ):

            events.append(
                normalize_jetton(
                    item,
                    jout,
                    "out"
                )
            )

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    events.sort(
        key=lambda x: safe_int(
            x.get("timestamp")
        ),
        reverse=True,
    )

    # --------------------------------------------------------
    # REMOVE DUPLICATE
    # --------------------------------------------------------

    unique = []

    ids = set()

    for e in events:

        eid = e.get("id")

        if not eid:
            continue

        if eid in ids:
            continue

        ids.add(eid)

        unique.append(e)

    return unique


# ============================================================
# NOTIFICATION
# ============================================================

async def send_notification(app, event):

    recipients = (
        CHAT_IDS
        | monitor_users
    )

    if not recipients:
        return

    icon = (
        "🟢"
        if event["direction"] == "in"
        else "🔴"
    )

    title = (
        "MASUK"
        if event["direction"] == "in"
        else "KELUAR"
    )

    sign = (
        "+"
        if event["direction"] == "in"
        else "-"
    )

    text = f"""
🚨 <b>TRANSAKSI BARU</b>

{icon} <b>{title}</b>

💰 Jumlah:
<b>{sign}{esc(event["amount"])} {esc(event["symbol"])}</b>

📤 Pengirim:
<code>{esc(event["source"])}</code>

📥 Penerima:
<code>{esc(event["destination"])}</code>

📅 {fmt_time(event["timestamp"])} {TIMEZONE_NAME}
"""

    if event["kind"] == "JETTON":

        text += f"""

🪙 Token:
<b>{esc(event["symbol"])}</b>

🪙 Jetton Master:
<code>{esc(event["master"])}</code>
"""

    txhash = safe_str(
        event.get("hash")
    )

    if txhash:

        text += f"""

🔗 <a href="https://tonviewer.com/transaction/{txhash}">Lihat transaksi</a>
"""

    for cid in recipients:

        try:

            await app.bot.send_message(
                chat_id=cid,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        except Exception as e:

            logger.warning(
                "Gagal mengirim notifikasi ke %s: %s",
                cid,
                e,
            )


# ============================================================
# MONITOR
# ============================================================

async def monitor(app):

    global baseline_ready

    logger.info(
        "Wallet monitor aktif. Interval: %s detik",
        POLL_SECONDS
    )

    while True:

        try:

            events = await recent_events()

            ids = {
                x["id"]
                for x in events
                if x.get("id")
            }

            # ------------------------------------------------
            # PERTAMA KALI
            # Jangan kirim history lama sebagai transaksi baru
            # ------------------------------------------------

            if not baseline_ready:

                seen_events.update(ids)

                baseline_ready = True

                logger.info(
                    "Baseline transaksi dibuat: %s event",
                    len(ids)
                )

            else:

                fresh = []

                for e in events:

                    eid = e.get("id")

                    if not eid:
                        continue

                    if eid not in seen_events:

                        fresh.append(e)

                # Simpan semua event baru
                for e in fresh:

                    seen_events.add(
                        e["id"]
                    )

                # Kirim dari yang lama ke yang terbaru
                for e in reversed(fresh):

                    await send_notification(
                        app,
                        e
                    )

                    logger.info(
                        "Transaksi baru: %s %s %s",
                        e["direction"],
                        e["amount"],
                        e["symbol"],
                    )

        except Exception as e:

            logger.exception(
                "Error monitor: %s",
                e
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

async def start(update, context):

    text = f"""
<b>UPDATE WALLET PORTAL</b>

Wallet:

<code>{WALLET_ADDRESS}</code>

Pilih menu di bawah.
"""

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


# ============================================================
# CHAT ID
# ============================================================

async def chatid(update, context):

    await update.message.reply_text(
        f"<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):

    try:

        state, jets = await asyncio.gather(
            wallet_state(),
            jetton_wallets(),
        )

        ton = amount(
            state.get("balance", "0"),
            9
        )

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"🟣 TON: <b>{esc(ton)} TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        for w in (
            jets.get(
                "jetton_wallets",
                []
            )
            or []
        ):

            master = safe_str(
                w.get("jetton")
            )

            info = metadata(
                jets,
                master
            )

            symbol = safe_str(
                info.get("symbol"),
                "JETTON"
            )

            if master == USDT_JETTON_MASTER:
                symbol = "USDT"

            name = safe_str(
                info.get("name"),
                symbol
            )

            bal = amount(
                w.get("balance", "0"),
                decimals(
                    info,
                    master
                ),
            )

            lines.append(
                f"• <b>{esc(symbol)}</b>: "
                f"{esc(bal)} "
                f"({esc(name)})"
            )

        lines.append("")
        lines.append("Wallet:")
        lines.append(
            f"<code>{WALLET_ADDRESS}</code>"
        )

        lines.append("")

        lines.append(
            f"🕐 "
            f"{fmt_time(datetime.now().timestamp())} "
            f"{TIMEZONE_NAME}"
        )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        logger.exception(e)

        await update.message.reply_text(
            "❌ Error membaca saldo\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# TOKENS
# ============================================================

async def tokens(update, context):

    try:

        jets = await jetton_wallets()

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        for i, w in enumerate(
            jets.get(
                "jetton_wallets",
                []
            )
            or [],
            1
        ):

            master = safe_str(
                w.get("jetton")
            )

            info = metadata(
                jets,
                master
            )

            symbol = safe_str(
                info.get("symbol"),
                "JETTON"
            )

            if master == USDT_JETTON_MASTER:
                symbol = "USDT"

            name = safe_str(
                info.get("name"),
                symbol
            )

            bal = amount(
                w.get("balance", "0"),
                decimals(
                    info,
                    master
                ),
            )

            lines.append(
                f"{i}. <b>{esc(symbol)}</b>\n"
                f"Saldo: {esc(bal)}\n"
                f"{esc(name)}\n"
                f"<code>{esc(master)}</code>\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        logger.exception(e)

        await update.message.reply_text(
            "❌ Error token\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# TRANSACTIONS
# ============================================================

async def transactions(update, context):

    try:

        events = await recent_events()

        lines = [
            "📜 <b>20 TRANSAKSI TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Tidak ada transaksi ditemukan."
            )

        for i, e in enumerate(
            events[:20],
            1
        ):

            icon = (
                "🟢"
                if e["direction"] == "in"
                else "🔴"
            )

            title = (
                "MASUK"
                if e["direction"] == "in"
                else "KELUAR"
            )

            addr = (
                e["source"]
                if e["direction"] == "in"
                else e["destination"]
            )

            lines.append(
                f"<b>{i}. "
                f"{icon} "
                f"{title} "
                f"{esc(e['symbol'])}</b>\n"
                f"Jumlah: "
                f"{esc(e['amount'])} "
                f"{esc(e['symbol'])}\n"
                f"{'Dari' if e['direction'] == 'in' else 'Ke'}:\n"
                f"<code>{esc(addr)}</code>\n"
                f"🕐 "
                f"{fmt_time(e['timestamp'])}\n"
            )

            if e["kind"] == "JETTON":

                lines.append(
                    f"🪙 Token: "
                    f"<b>{esc(e['symbol'])}</b>\n"
                )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        logger.exception(e)

        await update.message.reply_text(
            "❌ Error transaksi\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# MONITOR WALLET BUTTON
# ============================================================

async def monitor_wallet(update, context):

    cid = str(
        update.effective_chat.id
    )

    if cid in monitor_users:

        monitor_users.remove(cid)

        status = "OFF 🔴"

    else:

        monitor_users.add(cid)

        status = "ON 🟢"

    await update.message.reply_text(
        f"👁 Memantau Wallet\n\n"
        f"Status: <b>{status}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


# ============================================================
# REFRESH
# ============================================================

async def refresh(update, context):

    await update.message.reply_text(
        "🔄 Menu diperbarui",
        reply_markup=menu(),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    if not update.message:
        return

    t = update.message.text

    if t == "💰 Info Saldo":

        await balance(
            update,
            context
        )

    elif t == "📜 20 Transaksi":

        await transactions(
            update,
            context
        )

    elif t == "🪙 Token Dimiliki":

        await tokens(
            update,
            context
        )

    elif t == "👁 Memantau Wallet":

        await monitor_wallet(
            update,
            context
        )

    elif t == "🔄 Refresh":

        await refresh(
            update,
            context
        )


# ============================================================
# STARTUP
# ============================================================

async def post_init(app):

    global client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            20.0
        )
    )

    app.create_task(
        monitor(app)
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(app):

    global client

    if client:

        await client.aclose()


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset."
        )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "chatid",
            chatid
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()
