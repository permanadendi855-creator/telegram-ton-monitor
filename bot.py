import os
import asyncio
import logging
import base64

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from telegram.constants import ParseMode

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# WALLET
# ============================================================

WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"

USDT_JETTON_WALLET = (
    "EQAmwNPCaojho0YTS8ZfwnK5zHjduMZeZbeie5dLHeFTAWD7"
)

USDT_JETTON_MASTER = (
    "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
)


# ============================================================
# CONFIG
# ============================================================

TONCENTER_BASE = "https://toncenter.com/api/v3"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_KEY = os.getenv("TONCENTER_API_KEY", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta"
)

TZ = ZoneInfo(TIMEZONE_NAME)


CHAT_IDS = {
    x.strip()
    for x in os.getenv("CHAT_IDS", "").split(",")
    if x.strip()
}


# ============================================================
# GLOBAL
# ============================================================

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
# TELEGRAM MENU
# ============================================================

def menu():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("💰 Info Saldo"),
                KeyboardButton("🟣 10 Transaksi TON"),
            ],
            [
                KeyboardButton("🪙 10 Transaksi USDT"),
                KeyboardButton("🪙 Token Dimiliki"),
            ],
            [
                KeyboardButton("👁 Memantau Wallet"),
                KeyboardButton("🔄 Refresh"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
# HTML ESCAPE
# ============================================================

def esc(text):

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# TON ADDRESS
# RAW 0:HASH -> UQ... FRIENDLY ADDRESS
# ============================================================

def crc16_xmodem(data):

    crc = 0

    for byte in data:

        crc ^= byte << 8

        for _ in range(8):

            if crc & 0x8000:

                crc = ((crc << 1) ^ 0x1021) & 0xFFFF

            else:

                crc = (crc << 1) & 0xFFFF

    return crc


def raw_to_friendly(address):

    if not address:
        return "-"

    address = str(address).strip()

    # Sudah friendly
    if address.startswith("UQ") or address.startswith("EQ"):

        return address

    # Raw TON address
    if ":" not in address:

        return address

    try:

        workchain, account = address.split(":", 1)

        if len(account) != 64:
            return address

        workchain = int(workchain)

        account_bytes = bytes.fromhex(account)

        # Non-bounceable mainnet address.
        # UQ... adalah format yang diinginkan.
        tag = 0x51

        data = bytes([
            tag,
            workchain & 0xFF
        ]) + account_bytes

        checksum = crc16_xmodem(data)

        result = data + checksum.to_bytes(
            2,
            byteorder="big"
        )

        return base64.urlsafe_b64encode(
            result
        ).decode().rstrip("=")

    except Exception:

        return address


def friendly_address(address):

    return raw_to_friendly(address)


# ============================================================
# SHORT ADDRESS
# ============================================================

def short(addr):

    if not addr:
        return "-"

    addr = friendly_address(addr)

    if len(addr) < 18:
        return addr

    return addr[:9] + "..." + addr[-8:]


# ============================================================
# TIME
# ============================================================

def fmt_time(ts):

    if not ts:
        return "-"

    dt = datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).astimezone(TZ)

    return dt.strftime(
        "%d/%m/%Y %H:%M:%S"
    )


# ============================================================
# AMOUNT
# ============================================================

def amount(raw, decimals):

    try:

        value = (
            Decimal(str(raw))
            / (Decimal(10) ** decimals)
        )

        text = (
            format(value, "f")
            .rstrip("0")
            .rstrip(".")
        )

        return text if text else "0"

    except (InvalidOperation, ValueError):

        return "0"


# ============================================================
# TONCENTER API
# ============================================================

async def api(path, params=None):

    headers = {}

    if API_KEY:

        headers["X-API-Key"] = API_KEY

    response = await client.get(
        TONCENTER_BASE + path,
        params=params,
        headers=headers,
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# WALLET STATE
# ============================================================

async def wallet_state():

    data = await api(
        "/walletStates",
        {
            "address": WALLET_ADDRESS,
        },
    )

    wallets = data.get(
        "wallets",
        []
    )

    if wallets:

        return wallets[0]

    return {}


# ============================================================
# JETTON WALLET
# ============================================================

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
# TON TRANSACTIONS
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


# ============================================================
# JETTON IN
# ============================================================

async def jetton_in():

    return await api(
        "/jetton/transfers",
        {
            "owner_address": WALLET_ADDRESS,
            "direction": "in",
            "limit": 100,
            "sort": "desc",
        },
    )


# ============================================================
# JETTON OUT
# ============================================================

async def jetton_out():

    return await api(
        "/jetton/transfers",
        {
            "owner_address": WALLET_ADDRESS,
            "direction": "out",
            "limit": 100,
            "sort": "desc",
        },
    )


# ============================================================
# METADATA
# ============================================================

def metadata(response, master):

    data = response.get(
        "metadata",
        {}
    )

    if master in data:

        info = data[master].get(
            "token_info",
            []
        )

        if info:

            return info[0]

    for value in data.values():

        info = value.get(
            "token_info",
            []
        )

        if info:

            return info[0]

    return {}


# ============================================================
# DECIMALS
# ============================================================

def decimals(info, master):

    if master == USDT_JETTON_MASTER:

        return 6

    extra = info.get(
        "extra",
        {}
    )

    for key in [
        "decimals",
        "decimal"
    ]:

        if key in extra:

            try:

                return int(extra[key])

            except Exception:

                pass

    try:

        return int(
            info.get(
                "decimals",
                9
            )
        )

    except Exception:

        return 9


# ============================================================
# NORMALIZE JETTON
# ============================================================

def normalize_jetton(
    item,
    response,
    direction
):

    master = item.get(
        "jetton_master",
        ""
    )

    info = metadata(
        response,
        master
    )

    symbol = (
        info.get("symbol")
        or "JETTON"
    )

    if master == USDT_JETTON_MASTER:

        symbol = "USDT"

    name = (
        info.get("name")
        or symbol
    )

    dec = decimals(
        info,
        master
    )

    source = friendly_address(
        item.get("source", "")
    )

    destination = friendly_address(
        item.get("destination", "")
    )

    timestamp = int(
        item.get(
            "transaction_now",
            0
        )
    )

    txhash = item.get(
        "transaction_hash",
        ""
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
            item.get(
                "amount",
                "0"
            ),
            dec
        ),

        "source": source,

        "destination": destination,

        "timestamp": timestamp,

        "hash": txhash,

        "master": master,

        "aborted": item.get(
            "transaction_aborted",
            False
        ),
    }


# ============================================================
# NORMALIZE TON
# ============================================================

def normalize_ton(tx):

    events = []

    timestamp = int(
        tx.get(
            "now",
            0
        )
    )

    txhash = tx.get(
        "hash",
        ""
    )

    incoming = (
        tx.get("in_msg")
        or {}
    )

    source = incoming.get(
        "source",
        ""
    )

    value = int(
        incoming.get(
            "value",
            0
        )
        or 0
    )

    if source and value > 0:

        events.append(
            {
                "id":
                    "TI:" + txhash,

                "kind":
                    "TON",

                "direction":
                    "in",

                "symbol":
                    "TON",

                "amount":
                    amount(
                        value,
                        9
                    ),

                "source":
                    friendly_address(
                        source
                    ),

                "destination":
                    friendly_address(
                        WALLET_ADDRESS
                    ),

                "timestamp":
                    timestamp,

                "hash":
                    txhash,
            }
        )

    for i, msg in enumerate(
        tx.get("out_msgs") or []
    ):

        destination = msg.get(
            "destination",
            ""
        )

        value = int(
            msg.get(
                "value",
                0
            )
            or 0
        )

        if destination and value > 0:

            events.append(
                {
                    "id":
                        "TO:"
                        + txhash
                        + ":"
                        + str(i),

                    "kind":
                        "TON",

                    "direction":
                        "out",

                    "symbol":
                        "TON",

                    "amount":
                        amount(
                            value,
                            9
                        ),

                    "source":
                        friendly_address(
                            WALLET_ADDRESS
                        ),

                    "destination":
                        friendly_address(
                            destination
                        ),

                    "timestamp":
                        timestamp,

                    "hash":
                        txhash,
                }
            )

    return events


# ============================================================
# ALL RECENT EVENTS
# ============================================================

async def recent_events():

    ton_data, jetton_in_data, jetton_out_data = (
        await asyncio.gather(
            ton_transactions(),
            jetton_in(),
            jetton_out(),
        )
    )

    events = []

    # TON
    for tx in ton_data.get(
        "transactions",
        []
    ):

        events.extend(
            normalize_ton(tx)
        )

    # Jetton IN
    for item in jetton_in_data.get(
        "jetton_transfers",
        []
    ):

        if not item.get(
            "transaction_aborted",
            False
        ):

            events.append(
                normalize_jetton(
                    item,
                    jetton_in_data,
                    "in"
                )
            )

    # Jetton OUT
    for item in jetton_out_data.get(
        "jetton_transfers",
        []
    ):

        if not item.get(
            "transaction_aborted",
            False
        ):

            events.append(
                normalize_jetton(
                    item,
                    jetton_out_data,
                    "out"
                )
            )

    # Terbaru
    events.sort(
        key=lambda x: x["timestamp"],
        reverse=True
    )

    # Hilangkan duplicate
    unique = []

    ids = set()

    for event in events:

        if event["id"] in ids:

            continue

        ids.add(
            event["id"]
        )

        unique.append(event)

    return unique


# ============================================================
# TON ONLY HISTORY
# ============================================================

async def ton_history():

    data = await ton_transactions()

    events = []

    for tx in data.get(
        "transactions",
        []
    ):

        events.extend(
            normalize_ton(tx)
        )

    events.sort(
        key=lambda x: x["timestamp"],
        reverse=True
    )

    unique = []

    ids = set()

    for event in events:

        if event["id"] in ids:

            continue

        ids.add(
            event["id"]
        )

        unique.append(event)

    return unique[:10]


# ============================================================
# USDT ONLY HISTORY
# ============================================================

async def usdt_history():

    incoming, outgoing = await asyncio.gather(
        jetton_in(),
        jetton_out(),
    )

    events = []

    # USDT masuk
    for item in incoming.get(
        "jetton_transfers",
        []
    ):

        if item.get(
            "transaction_aborted",
            False
        ):

            continue

        master = item.get(
            "jetton_master",
            ""
        )

        if master != USDT_JETTON_MASTER:

            continue

        events.append(
            normalize_jetton(
                item,
                incoming,
                "in"
            )
        )

    # USDT keluar
    for item in outgoing.get(
        "jetton_transfers",
        []
    ):

        if item.get(
            "transaction_aborted",
            False
        ):

            continue

        master = item.get(
            "jetton_master",
            ""
        )

        if master != USDT_JETTON_MASTER:

            continue

        events.append(
            normalize_jetton(
                item,
                outgoing,
                "out"
            )
        )

    events.sort(
        key=lambda x: x["timestamp"],
        reverse=True
    )

    unique = []

    ids = set()

    for event in events:

        if event["id"] in ids:

            continue

        ids.add(
            event["id"]
        )

        unique.append(event)

    return unique[:10]


# ============================================================
# NOTIFICATION
# ============================================================

async def send_notification(
    app,
    event
):

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

{icon} <b>{title} {esc(event["symbol"])}</b>

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

Jetton Master:
<code>{esc(event["master"])}</code>
"""

    if event.get("hash"):

        text += f"""

🔗 <a href="https://tonviewer.com/transaction/{event["hash"]}">Lihat transaksi</a>
"""

    for chat_id in recipients:

        try:

            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        except Exception as e:

            logger.warning(
                "Gagal kirim notifikasi ke %s: %s",
                chat_id,
                e
            )


# ============================================================
# MONITOR
# ============================================================

async def monitor(app):

    global baseline_ready

    while True:

        try:

            events = await recent_events()

            ids = {
                event["id"]
                for event in events
            }

            # Saat pertama bot hidup,
            # jangan mengirim semua history lama.
            if not baseline_ready:

                seen_events.update(
                    ids
                )

                baseline_ready = True

            else:

                fresh = []

                for event in events:

                    if event["id"] not in seen_events:

                        fresh.append(
                            event
                        )

                for event in fresh:

                    seen_events.add(
                        event["id"]
                    )

                # Kirim dari transaksi paling lama
                # ke paling baru.
                for event in reversed(
                    fresh
                ):

                    await send_notification(
                        app,
                        event
                    )

        except Exception as e:

            logger.exception(
                "Monitor error: %s",
                e
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = f"""
<b>UPDATE WALLET PORTAL</b>

Wallet:

<code>{esc(WALLET_ADDRESS)}</code>

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

async def chatid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        f"<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# INFO SALDO
# ============================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        state, jets = await asyncio.gather(
            wallet_state(),
            jetton_wallets(),
        )

        ton = amount(
            state.get(
                "balance",
                "0"
            ),
            9
        )

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"🟣 TON: <b>{esc(ton)} TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        for wallet in jets.get(
            "jetton_wallets",
            []
        ):

            master = wallet.get(
                "jetton",
                ""
            )

            info = metadata(
                jets,
                master
            )

            symbol = (
                info.get("symbol")
                or "JETTON"
            )

            if master == USDT_JETTON_MASTER:

                symbol = "USDT"

            name = (
                info.get("name")
                or symbol
            )

            bal = amount(
                wallet.get(
                    "balance",
                    "0"
                ),
                decimals(
                    info,
                    master
                )
            )

            lines.append(
                f"• <b>{esc(symbol)}</b>: "
                f"{esc(bal)} "
                f"({esc(name)})"
            )

        lines.append("")

        lines.append(
            "Wallet:"
        )

        lines.append(
            f"<code>{esc(WALLET_ADDRESS)}</code>"
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

        await update.message.reply_text(
            "❌ Error membaca saldo\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# 10 TRANSAKSI TON
# ============================================================

async def transactions_ton(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        events = await ton_history()

        lines = [
            "🟣 <b>10 TRANSAKSI TON TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Tidak ada transaksi TON."
            )

        for i, event in enumerate(
            events,
            1
        ):

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

            address = (
                event["source"]
                if event["direction"] == "in"
                else event["destination"]
            )

            label = (
                "Dari"
                if event["direction"] == "in"
                else "Ke"
            )

            lines.append(
                f"<b>{i}. {icon} {title} TON</b>\n"
                f"Jumlah: "
                f"<b>{esc(event['amount'])} TON</b>\n"
                f"{label}:\n"
                f"<code>{esc(address)}</code>\n"
                f"🕐 {fmt_time(event['timestamp'])}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Error membaca transaksi TON\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# 10 TRANSAKSI USDT
# ============================================================

async def transactions_usdt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        events = await usdt_history()

        lines = [
            "🪙 <b>10 TRANSAKSI USDT TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Tidak ada transaksi USDT."
            )

        for i, event in enumerate(
            events,
            1
        ):

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

            address = (
                event["source"]
                if event["direction"] == "in"
                else event["destination"]
            )

            label = (
                "Dari"
                if event["direction"] == "in"
                else "Ke"
            )

            lines.append(
                f"<b>{i}. {icon} {title} USDT</b>\n"
                f"Jumlah: "
                f"<b>{esc(event['amount'])} USDT</b>\n"
                f"{label}:\n"
                f"<code>{esc(address)}</code>\n"
                f"🕐 {fmt_time(event['timestamp'])}\n"
                f"🔗 <a href=\"https://tonviewer.com/transaction/{event['hash']}\">Lihat transaksi</a>\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=menu(),
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Error membaca transaksi USDT\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# TOKEN DIMILIKI
# ============================================================

async def tokens(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        jets = await jetton_wallets()

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        wallets = jets.get(
            "jetton_wallets",
            []
        )

        if not wallets:

            lines.append(
                "Tidak ada token."
            )

        for i, wallet in enumerate(
            wallets,
            1
        ):

            master = wallet.get(
                "jetton",
                ""
            )

            info = metadata(
                jets,
                master
            )

            symbol = (
                info.get("symbol")
                or "JETTON"
            )

            if master == USDT_JETTON_MASTER:

                symbol = "USDT"

            name = (
                info.get("name")
                or symbol
            )

            bal = amount(
                wallet.get(
                    "balance",
                    "0"
                ),
                decimals(
                    info,
                    master
                )
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

        await update.message.reply_text(
            "❌ Error token\n\n"
            + esc(str(e)),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# MONITOR WALLET
# ============================================================

async def monitor_wallet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = str(
        update.effective_chat.id
    )

    if chat_id in monitor_users:

        monitor_users.remove(
            chat_id
        )

        status = "OFF 🔴"

    else:

        monitor_users.add(
            chat_id
        )

        status = "ON 🟢"

    await update.message.reply_text(
        f"""
👁 <b>MEMANTAU WALLET</b>

Status: <b>{status}</b>

Wallet:
<code>{esc(WALLET_ADDRESS)}</code>

Bot akan memeriksa aktivitas wallet
setiap {POLL_SECONDS} detik selama bot aktif.
""",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


# ============================================================
# REFRESH
# ============================================================

async def refresh(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🔄 Menu diperbarui.",
        reply_markup=menu(),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text

    if text == "💰 Info Saldo":

        await balance(
            update,
            context
        )

    elif text == "🟣 10 Transaksi TON":

        await transactions_ton(
            update,
            context
        )

    elif text == "🪙 10 Transaksi USDT":

        await transactions_usdt(
            update,
            context
        )

    elif text == "🪙 Token Dimiliki":

        await tokens(
            update,
            context
        )

    elif text == "👁 Memantau Wallet":

        await monitor_wallet(
            update,
            context
        )

    elif text == "🔄 Refresh":

        await refresh(
            update,
            context
        )


# ============================================================
# POST INIT
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
# POST SHUTDOWN
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
            "TELEGRAM_BOT_TOKEN belum diatur."
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
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
