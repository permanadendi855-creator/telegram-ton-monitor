import os
import asyncio
import time
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIG
# ============================================================

WALLET_ADDRESS = (
    "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
)

USDT_JETTON_WALLET = (
    "EQAmwNPCaojho0YTS8ZfwnK5zHjduMZeZbeie5dLHeFTAWD7"
)

USDT_JETTON_MASTER = (
    "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
)

TONCENTER_BASE = os.getenv(
    "TONCENTER_BASE",
    "https://toncenter.com/api/v3",
).rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TONCENTER_API_KEY = os.getenv(
    "TONCENTER_API_KEY",
    "",
).strip()

POLL_SECONDS = max(
    10,
    int(os.getenv("POLL_SECONDS", "20")),
)

MAX_RECENT = 20

TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta",
)

LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

CHAT_IDS = {
    x.strip()
    for x in os.getenv("CHAT_IDS", "").split(",")
    if x.strip()
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(
    "telegram-ton-monitor"
)

# ============================================================
# GLOBAL STATE
# ============================================================

monitor_chats: set[str] = set()
seen_event_ids: set[str] = set()
baseline_ready = False
http_client: httpx.AsyncClient | None = None


# ============================================================
# HELPERS
# ============================================================

def short_address(
    address: str | None,
    left: int = 10,
    right: int = 8,
) -> str:

    if not address:
        return "-"

    address = str(address)

    if len(address) <= left + right + 3:
        return address

    return f"{address[:left]}...{address[-right:]}"


def format_decimal(
    value: Decimal,
    max_places: int = 8,
) -> str:

    q = value.quantize(
        Decimal(
            "1." + "0" * max_places
        )
    )

    text = (
        format(q, "f")
        .rstrip("0")
        .rstrip(".")
    )

    return text if text else "0"


def format_amount(
    raw: str | int | None,
    decimals: int,
) -> str:

    try:

        value = (
            Decimal(str(raw or "0"))
            / (Decimal(10) ** decimals)
        )

        return format_decimal(
            value,
            min(decimals, 8),
        )

    except (
        InvalidOperation,
        ValueError,
    ):

        return str(raw or "0")


def format_ton_nano(
    raw: str | int | None,
) -> str:

    return format_amount(
        raw,
        9,
    )


def fmt_time(
    timestamp: int | float | None,
) -> str:

    if not timestamp:
        return "-"

    dt = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc,
    ).astimezone(
        LOCAL_TZ,
    )

    return dt.strftime(
        "%d/%m/%Y %H:%M:%S",
    )


def html_escape(
    text: str,
) -> str:

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def explorer_url(
    value: str,
) -> str:

    return (
        "https://tonviewer.com/"
        + value
    )


# ============================================================
# MENU
# ============================================================

def menu_markup() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💰 Info Saldo",
                    callback_data="balance",
                ),
                InlineKeyboardButton(
                    "📜 20 Transaksi",
                    callback_data="tx20",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🪙 Token Dimiliki",
                    callback_data="tokens",
                ),
                InlineKeyboardButton(
                    "👁 Memantau Wallet",
                    callback_data="monitor",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="home",
                ),
            ],
        ]
    )


def back_markup() -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Kembali",
                    callback_data="home",
                )
            ]
        ]
    )


# ============================================================
# TON CENTER
# ============================================================

async def api_get(
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:

    global http_client

    if http_client is None:
        raise RuntimeError(
            "HTTP client belum siap"
        )

    headers = {}

    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY

    url = (
        f"{TONCENTER_BASE}/"
        f"{path.lstrip('/')}"
    )

    response = await http_client.get(
        url,
        params=params or {},
        headers=headers,
    )

    response.raise_for_status()

    data = response.json()

    if (
        isinstance(data, dict)
        and data.get("error")
    ):
        raise RuntimeError(
            str(data["error"])
        )

    return data


# ============================================================
# TRANSACTIONS
# ============================================================

async def get_ton_transactions(
    limit: int = 50,
) -> list[dict[str, Any]]:

    data = await api_get(
        "transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": min(limit, 1000),
            "sort": "desc",
        },
    )

    return data.get(
        "transactions",
        [],
    )


async def get_jetton_transfers(
    limit: int = 100,
    jetton_master: str | None = None,
) -> dict[str, Any]:

    params: dict[str, Any] = {
        "owner_address": WALLET_ADDRESS,
        "limit": min(limit, 1000),
        "sort": "desc",
    }

    if jetton_master:
        params["jetton_master"] = jetton_master

    return await api_get(
        "jetton/transfers",
        params,
    )


async def get_jetton_wallets(
    limit: int = 100,
) -> dict[str, Any]:

    return await api_get(
        "jetton/wallets",
        {
            "owner_address": WALLET_ADDRESS,
            "exclude_zero_balance": "true",
            "limit": min(limit, 1000),
            "sort": "desc",
        },
    )


async def get_account_state() -> dict[str, Any]:

    data = await api_get(
        "accountStates",
        {
            "address": WALLET_ADDRESS,
        },
    )

    states = data.get(
        "account_states",
        [],
    )

    return states[0] if states else {}


# ============================================================
# TOKEN METADATA
# ============================================================

def token_info_from_response(
    response: dict[str, Any],
    jetton_address: str,
) -> dict[str, Any]:

    metadata = response.get(
        "metadata",
        {},
    )

    if not isinstance(metadata, dict):
        return {}

    if jetton_address in metadata:

        info = metadata[
            jetton_address
        ].get(
            "token_info",
            [],
        )

        if info:
            return info[0]

    for key, value in metadata.items():

        if str(key) == str(
            jetton_address
        ):

            info = value.get(
                "token_info",
                [],
            )

            if info:
                return info[0]

    return {}


def token_decimals(
    info: dict[str, Any],
    default: int = 9,
) -> int:

    raw = info.get(
        "decimals"
    )

    try:
        return int(raw)
    except (
        TypeError,
        ValueError,
    ):
        return default


# ============================================================
# NORMALIZE JETTON
# ============================================================

def normalize_jetton_transfer(
    item: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:

    master = (
        item.get("jetton_master")
        or ""
    )

    info = token_info_from_response(
        response,
        master,
    )

    symbol = (
        info.get("symbol")
        or "JETTON"
    )

    name = (
        info.get("name")
        or symbol
    )

    decimals = (
        6
        if master == USDT_JETTON_MASTER
        else token_decimals(
            info,
            9,
        )
    )

    source = (
        item.get("source")
        or ""
    )

    destination = (
        item.get("destination")
        or ""
    )

    if source == WALLET_ADDRESS:

        direction = "out"

        counterparty = destination

    elif destination == WALLET_ADDRESS:

        direction = "in"

        counterparty = source

    else:

        direction = "?"

        counterparty = (
            destination
            or source
        )

    timestamp = int(
        item.get("transaction_now")
        or item.get("now")
        or 0
    )

    tx_hash = str(
        item.get("transaction_hash")
        or ""
    )

    lt = str(
        item.get("transaction_lt")
        or ""
    )

    event_id = (
        f"jetton:"
        f"{tx_hash}:"
        f"{lt}:"
        f"{master}:"
        f"{direction}"
    )

    return {
        "event_id": event_id,
        "kind": "jetton",
        "direction": direction,
        "symbol": symbol,
        "name": name,
        "master": master,
        "amount": format_amount(
            item.get("amount", "0"),
            decimals,
        ),
        "decimals": decimals,
        "source": source,
        "destination": destination,
        "counterparty": counterparty,
        "timestamp": timestamp,
        "lt": lt,
        "hash": tx_hash,
        "aborted": bool(
            item.get("transaction_aborted")
        ),
    }


# ============================================================
# NORMALIZE TON
# ============================================================

def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:

    events = []

    now = int(
        tx.get("now")
        or 0
    )

    tx_hash = str(
        tx.get("hash")
        or ""
    )

    lt = str(
        tx.get("lt")
        or ""
    )

    in_msg = (
        tx.get("in_msg")
        or {}
    )

    source = (
        in_msg.get("source")
        or ""
    )

    value = int(
        in_msg.get("value")
        or 0
    )

    if (
        source
        and source != WALLET_ADDRESS
        and value > 0
    ):

        events.append(
            {
                "event_id":
                    f"ton-in:{tx_hash}",

                "kind": "ton",

                "direction": "in",

                "symbol": "TON",

                "amount":
                    format_ton_nano(value),

                "source": source,

                "destination":
                    WALLET_ADDRESS,

                "counterparty":
                    source,

                "timestamp": now,

                "lt": lt,

                "hash": tx_hash,

                "aborted": False,
            }
        )

    for index, msg in enumerate(
        tx.get("out_msgs")
        or []
    ):

        destination = (
            msg.get("destination")
            or ""
        )

        msg_value = int(
            msg.get("value")
            or 0
        )

        if (
            destination
            and destination != WALLET_ADDRESS
            and msg_value > 0
        ):

            events.append(
                {
                    "event_id":
                        f"ton-out:{tx_hash}:{index}",

                    "kind": "ton",

                    "direction": "out",

                    "symbol": "TON",

                    "amount":
                        format_ton_nano(
                            msg_value
                        ),

                    "source":
                        WALLET_ADDRESS,

                    "destination":
                        destination,

                    "counterparty":
                        destination,

                    "timestamp": now,

                    "lt": lt,

                    "hash": tx_hash,

                    "aborted": False,
                }
            )

    return events


# ============================================================
# RECENT EVENTS
# ============================================================

async def build_recent_events(
    limit: int = 20,
) -> list[dict[str, Any]]:

    ton_txs, jetton_data = await asyncio.gather(
        get_ton_transactions(50),
        get_jetton_transfers(100),
    )

    events = []

    for tx in ton_txs:

        events.extend(
            normalize_ton_events(tx)
        )

    for item in jetton_data.get(
        "jetton_transfers",
        [],
    ):

        event = normalize_jetton_transfer(
            item,
            jetton_data,
        )

        if not event["aborted"]:
            events.append(event)

    events.sort(
        key=lambda x: (
            int(x.get("timestamp") or 0),
            str(x.get("lt") or ""),
        ),
        reverse=True,
    )

    result = []
    used = set()

    for event in events:

        event_id = event.get(
            "event_id"
        )

        if not event_id:
            continue

        if event_id in used:
            continue

        used.add(event_id)

        result.append(event)

        if len(result) >= limit:
            break

    return result


# ============================================================
# NOTIFICATION
# ============================================================

async def send_event_notification(
    application: Application,
    event: dict[str, Any],
) -> None:

    recipients = (
        set(CHAT_IDS)
        | set(monitor_chats)
    )

    if not recipients:
        return

    direction = event.get(
        "direction"
    )

    if direction == "in":

        icon = "🟢"
        label = "SALDO MASUK"
        sign = "+"

    else:

        icon = "🔴"
        label = "SALDO KELUAR"
        sign = "-"

    symbol = event.get(
        "symbol",
        "TON",
    )

    source = (
        event.get("source")
        or "-"
    )

    destination = (
        event.get("destination")
        or "-"
    )

    amount = (
        event.get("amount")
        or "0"
    )

    timestamp = fmt_time(
        event.get("timestamp")
    )

    tx_hash = (
        event.get("hash")
        or ""
    )

    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"

        f"{icon} "
        f"<b>{label}</b>\n\n"

        f"💰 Jumlah: "
        f"<b>{sign}"
        f"{html_escape(amount)} "
        f"{html_escape(symbol)}</b>\n\n"

        f"📤 Pengirim:\n"
        f"<code>"
        f"{html_escape(source)}"
        f"</code>\n\n"

        f"📥 Penerima:\n"
        f"<code>"
        f"{html_escape(destination)}"
        f"</code>\n\n"

        f"📅 {timestamp} "
        f"{html_escape(TIMEZONE_NAME)}\n"
    )

    if event.get("master"):

        text += (
            "\n🪙 Jetton Master:\n"
            f"<code>"
            f"{html_escape(event['master'])}"
            f"</code>\n"
        )

    if tx_hash:

        url = explorer_url(
            tx_hash
        )

        text += (
            f'\n🔗 <a href="{url}">'
            "Lihat transaksi di Tonviewer"
            "</a>"
        )

    for chat_id in sorted(
        recipients
    ):

        try:

            await application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        except Exception as exc:

            logger.warning(
                "Gagal mengirim notifikasi ke %s: %s",
                chat_id,
                exc,
            )

            if chat_id not in CHAT_IDS:

                monitor_chats.discard(
                    chat_id
                )


# ============================================================
# MONITOR LOOP
# ============================================================

async def monitor_loop(
    application: Application,
) -> None:

    global baseline_ready

    logger.info(
        "Monitoring wallet aktif."
    )

    while True:

        try:

            ton_txs, jetton_data = await asyncio.gather(
                get_ton_transactions(50),
                get_jetton_transfers(100),
            )

            events = []

            for tx in ton_txs:

                events.extend(
                    normalize_ton_events(tx)
                )

            for item in jetton_data.get(
                "jetton_transfers",
                [],
            ):

                event = normalize_jetton_transfer(
                    item,
                    jetton_data,
                )

                if not event["aborted"]:

                    events.append(event)

            events.sort(
                key=lambda x: (
                    int(x.get("timestamp") or 0),
                    str(x.get("lt") or ""),
                ),
                reverse=True,
            )

            current_ids = {
                event["event_id"]
                for event in events
                if event.get("event_id")
            }

            if not baseline_ready:

                seen_event_ids.update(
                    current_ids
                )

                baseline_ready = True

                logger.info(
                    "Baseline dibuat: %d event.",
                    len(current_ids),
                )

            else:

                new_events = []

                for event in events:

                    event_id = event.get(
                        "event_id"
                    )

                    if (
                        event_id
                        and event_id
                        not in seen_event_ids
                        and not event.get(
                            "aborted"
                        )
                    ):

                        new_events.append(
                            event
                        )

                for event in new_events:

                    seen_event_ids.add(
                        event["event_id"]
                    )

                for event in reversed(
                    new_events
                ):

                    await send_event_notification(
                        application,
                        event,
                    )

                if len(
                    seen_event_ids
                ) > 5000:

                    seen_event_ids.clear()

                    seen_event_ids.update(
                        current_ids
                    )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception(
                "Error monitoring wallet."
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    text = (
        "👋 <b>TON WALLET MONITOR</b>\n\n"

        "Wallet yang dipantau:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"

        "🟣 TON\n"
        "🪙 Semua Jetton\n"
        "💵 USDT\n\n"

        "Pilih menu:"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=menu_markup(),
        disable_web_page_preview=True,
    )


# ============================================================
# CHAT ID
# ============================================================

async def chat_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    await update.effective_message.reply_text(
        "Chat ID Anda:\n\n"
        f"<code>"
        f"{update.effective_chat.id}"
        f"</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Mengambil saldo..."
    )

    try:

        state, response = await asyncio.gather(
            get_account_state(),
            get_jetton_wallets(100),
        )

        ton_balance = format_ton_nano(
            state.get("balance", "0")
        )

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"🟣 TON: <b>{ton_balance} TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        wallets = response.get(
            "jetton_wallets",
            [],
        )

        if not wallets:

            lines.append(
                "Tidak ada Jetton dengan saldo."
            )

        else:

            for wallet in wallets[:30]:

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = token_info_from_response(
                    response,
                    master,
                )

                symbol = (
                    info.get("symbol")
                    or "JETTON"
                )

                name = (
                    info.get("name")
                    or symbol
                )

                decimals = (
                    6
                    if master == USDT_JETTON_MASTER
                    else token_decimals(
                        info,
                        9,
                    )
                )

                balance = format_amount(
                    wallet.get(
                        "balance",
                        "0",
                    ),
                    decimals,
                )

                lines.append(
                    f"• <b>"
                    f"{html_escape(symbol)}"
                    f"</b>: "
                    f"{html_escape(balance)} "
                    f"({html_escape(name)})"
                )

        lines.extend(
            [
                "",
                "Wallet:",
                f"<code>"
                f"{html_escape(WALLET_ADDRESS)}"
                f"</code>",
                "",
                f"🕐 {fmt_time(time.time())} "
                f"{html_escape(TIMEZONE_NAME)}",
            ]
        )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil saldo."
        )

        await query.edit_message_text(
            "❌ Gagal mengambil saldo.\n\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# TOKEN LIST
# ============================================================

async def show_tokens(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Mengambil token..."
    )

    try:

        response = await get_jetton_wallets(
            100
        )

        wallets = response.get(
            "jetton_wallets",
            [],
        )

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        if not wallets:

            lines.append(
                "Tidak ada Jetton."
            )

        else:

            for index, wallet in enumerate(
                wallets[:50],
                1,
            ):

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = token_info_from_response(
                    response,
                    master,
                )

                symbol = (
                    info.get("symbol")
                    or "JETTON"
                )

                name = (
                    info.get("name")
                    or symbol
                )

                decimals = (
                    6
                    if master == USDT_JETTON_MASTER
                    else token_decimals(
                        info,
                        9,
                    )
                )

                balance = format_amount(
                    wallet.get(
                        "balance",
                        "0",
                    ),
                    decimals,
                )

                lines.append(
                    f"<b>{index}. "
                    f"{html_escape(symbol)}</b> "
                    f"— {html_escape(balance)}\n"
                    f"   {html_escape(name)}"
                )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil token."
        )

        await query.edit_message_text(
            "❌ Gagal mengambil token.\n\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# 20 TRANSACTIONS
# ============================================================

async def show_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Mengambil transaksi..."
    )

    try:

        events = await build_recent_events(
            MAX_RECENT
        )

        lines = [
            "📜 <b>20 TRANSAKSI TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Belum ada transfer "
                "yang ditemukan."
            )

        else:

            for index, event in enumerate(
                events,
                1,
            ):

                if event["direction"] == "in":

                    icon = "🟢"
                    label = "MASUK"
                    sign = "+"

                else:

                    icon = "🔴"
                    label = "KELUAR"
                    sign = "-"

                symbol = event.get(
                    "symbol",
                    "TON",
                )

                counterparty = (
                    event.get(
                        "counterparty"
                    )
                    or "-"
                )

                lines.extend(
                    [
                        f"<b>{index}. "
                        f"{icon} {label} "
                        f"{html_escape(symbol)}</b>",

                        f"Jumlah: "
                        f"<b>{sign}"
                        f"{html_escape(event.get('amount', '0'))} "
                        f"{html_escape(symbol)}</b>",

                        f"{'Dari' if event['direction'] == 'in' else 'Ke'}:",
                        f"<code>"
                        f"{html_escape(counterparty)}"
                        f"</code>",

                        f"🕐 "
                        f"{fmt_time(event.get('timestamp'))} "
                        f"{html_escape(TIMEZONE_NAME)}",

                        "",
                    ]
                )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi."
        )

        await query.edit_message_text(
            "❌ Gagal mengambil transaksi.\n\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# MONITOR BUTTON
# ============================================================

async def show_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer()

    chat_id = str(
        query.message.chat.id
    )

    if chat_id in monitor_chats:

        monitor_chats.remove(
            chat_id
        )

        status = "⚪ OFF"

    else:

        monitor_chats.add(
            chat_id
        )

        status = "🟢 ON"

    text = (
        "👁️ <b>MEMANTAU WALLET</b>\n\n"

        f"Status Anda: <b>{status}</b>\n\n"

        "Bot akan memantau:\n"
        "🟣 TON masuk/keluar\n"
        "🪙 Jetton masuk/keluar\n"
        "💵 USDT masuk/keluar\n\n"

        f"Interval: "
        f"<b>{POLL_SECONDS} detik</b>\n\n"

        f"CHAT_ID permanen: "
        f"<b>{len(CHAT_IDS)}</b>"
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Ubah Status",
                        callback_data="monitor",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Kembali",
                        callback_data="home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# HOME BUTTON
# ============================================================

async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "🏠 <b>TON WALLET MONITOR</b>\n\n"
        "Pilih menu:",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_markup(),
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query.data == "balance":

        await show_balance(
            update,
            context,
        )

    elif query.data == "tx20":

        await show_transactions(
            update,
            context,
        )

    elif query.data == "tokens":

        await show_tokens(
            update,
            context,
        )

    elif query.data == "monitor":

        await show_monitor(
            update,
            context,
        )

    elif query.data == "home":

        await show_home(
            update,
            context,
        )

    else:

        await query.answer(
            "Menu tidak dikenal.",
            show_alert=True,
        )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application: Application,
) -> None:

    global http_client

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset."
        )

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            20.0,
            connect=10.0,
        )
    )

    if not TONCENTER_API_KEY:

        logger.warning(
            "TONCENTER_API_KEY belum diset."
        )

    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(
            application
        )
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(
    application: Application,
) -> None:

    global http_client

    task = application.bot_data.get(
        "monitor_task"
    )

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

    if not TELEGRAM_BOT_TOKEN:

        raise SystemExit(
            "TELEGRAM_BOT_TOKEN belum diset."
        )

    application = (
        ApplicationBuilder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .post_init(
            post_init
        )
        .post_shutdown(
            post_shutdown
        )
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "menu",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "chatid",
            chat_id_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            button_handler,
        )
    )

    logger.info(
        "TON Wallet Monitor started."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
