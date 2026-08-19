import os
import asyncio
import time
import logging
import base64
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
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
# CONFIG
# ============================================================

WALLET_ADDRESS = (
    "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
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

TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta",
)

LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)


# Optional.
# Bisa diisi:
# AUTO_MONITOR_CHAT_IDS=123456789,987654321
#
# Tetapi TIDAK WAJIB.
# User bisa menyalakan monitor lewat tombol
# "Memantau Wallet".
AUTO_MONITOR_CHAT_IDS = {
    x.strip()
    for x in os.getenv(
        "AUTO_MONITOR_CHAT_IDS",
        "",
    ).split(",")
    if x.strip()
}


# ============================================================
# GLOBAL STATE
# ============================================================

logging.basicConfig(
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
    level=logging.INFO,
)

logger = logging.getLogger(
    "telegram-ton-monitor"
)

monitor_chats: set[str] = set(
    AUTO_MONITOR_CHAT_IDS
)

seen_event_ids: set[str] = set()

baseline_ready = False

http_client: httpx.AsyncClient | None = None


# ============================================================
# DECIMAL / FORMAT
# ============================================================

def format_decimal(
    value: Decimal,
    max_places: int = 8,
) -> str:
    """
    Format Decimal tanpa quantize yang bisa overflow
    pada angka besar.
    """

    text = format(value, "f")

    if "." not in text:
        return text

    whole, fraction = text.split(
        ".",
        1,
    )

    fraction = fraction[:max_places]
    fraction = fraction.rstrip("0")

    if not fraction:
        return whole

    return f"{whole}.{fraction}"


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
        TypeError,
    ):
        return str(raw or "0")


def format_ton(
    raw: str | int | None,
) -> str:
    return format_amount(
        raw,
        9,
    )


# ============================================================
# TIME
# ============================================================

def fmt_time(
    timestamp: int | float | None,
) -> str:

    if not timestamp:
        return "-"

    try:
        dt = datetime.fromtimestamp(
            int(timestamp),
            tz=timezone.utc,
        ).astimezone(LOCAL_TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    except (
        ValueError,
        OverflowError,
        OSError,
    ):
        return "-"


# ============================================================
# HTML
# ============================================================

def html_escape(
    text: Any,
) -> str:

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ============================================================
# TON ADDRESS CONVERSION
# ============================================================

def crc16_xmodem(
    data: bytes,
) -> int:

    crc = 0

    for byte in data:
        crc ^= byte << 8

        for _ in range(8):
            if crc & 0x8000:
                crc = (
                    (crc << 1)
                    ^ 0x1021
                ) & 0xFFFF
            else:
                crc = (
                    crc << 1
                ) & 0xFFFF

    return crc


def raw_address_to_friendly(
    address: str,
    bounceable: bool = False,
) -> str:

    """
    Convert:
        0:ABCDEF...

    menjadi:
        UQ...

    Untuk mainnet:
        EQ = bounceable
        UQ = non-bounceable

    Kita sengaja memakai UQ untuk alamat yang berasal
    dari raw address supaya tampil seperti alamat wallet
    pada screenshot Tonviewer kamu.
    """

    if not address:
        return address

    address = str(address).strip()

    # Sudah friendly.
    if address.startswith(
        ("EQ", "UQ", "kQ", "0Q")
    ):
        return address

    if ":" not in address:
        return address

    try:
        wc_text, hash_text = address.split(
            ":",
            1,
        )

        wc = int(wc_text)

        hash_text = hash_text.strip()

        if len(hash_text) != 64:
            return address

        account_hash = bytes.fromhex(
            hash_text
        )

        # Mainnet:
        # 0x11 = bounceable -> EQ
        # 0x51 = non-bounceable -> UQ
        tag = (
            0x11
            if bounceable
            else 0x51
        )

        payload = bytes(
            [
                tag,
                wc & 0xFF,
            ]
        ) + account_hash

        checksum = crc16_xmodem(
            payload
        )

        raw = (
            payload
            + checksum.to_bytes(
                2,
                "big",
            )
        )

        return (
            base64.urlsafe_b64encode(raw)
            .decode("ascii")
            .rstrip("=")
        )

    except (
        ValueError,
        TypeError,
        binascii.Error,
    ):
        return address


def friendly_address(
    address: str | None,
) -> str:

    if not address:
        return "-"

    address = str(address).strip()

    # Pertahankan EQ/UQ kalau API sudah memberikannya.
    if address.startswith(
        ("EQ", "UQ", "kQ", "0Q")
    ):
        return address

    # Semua raw address -> UQ.
    return raw_address_to_friendly(
        address,
        bounceable=False,
    )


# ============================================================
# EXPLORER
# ============================================================

def explorer_url(
    value: str,
) -> str:

    return (
        "https://tonviewer.com/"
        + value
    )


# ============================================================
# TELEGRAM KEYBOARDS
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
                    "🟣 10 Transaksi TON",
                    callback_data="tx_ton",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🪙 10 Transaksi USDT",
                    callback_data="tx_usdt",
                ),
                InlineKeyboardButton(
                    "🪙 Token Dimiliki",
                    callback_data="tokens",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👁 Memantau Wallet",
                    callback_data="monitor",
                ),
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


def monitor_markup(
    chat_id: str,
) -> InlineKeyboardMarkup:

    status = (
        "🟢 ON"
        if chat_id in monitor_chats
        else "⚪ OFF"
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Status notifikasi: {status}",
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
    )


# ============================================================
# API
# ============================================================

async def api_get(
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:

    global http_client

    if http_client is None:
        raise RuntimeError(
            "HTTP client belum siap."
        )

    headers: dict[str, str] = {}

    if TONCENTER_API_KEY:
        headers["X-API-Key"] = (
            TONCENTER_API_KEY
        )

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

    if isinstance(data, dict):
        if data.get("error"):
            raise RuntimeError(
                str(data["error"])
            )

        if data.get("code") not in (
            None,
            0,
        ):
            raise RuntimeError(
                str(data)
            )

    return data


# ============================================================
# TON TRANSACTIONS
# ============================================================

async def get_ton_transactions(
    limit: int = 100,
) -> list[dict[str, Any]]:

    data = await api_get(
        "transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": min(
                limit,
                1000,
            ),
            "sort": "desc",
        },
    )

    return data.get(
        "transactions",
        [],
    )


# ============================================================
# JETTON WALLETS
# ============================================================

async def get_jetton_wallets(
    limit: int = 100,
) -> dict[str, Any]:

    return await api_get(
        "jetton/wallets",
        {
            "owner_address": WALLET_ADDRESS,
            "exclude_zero_balance": "true",
            "limit": min(
                limit,
                1000,
            ),
            "sort": "desc",
        },
    )


# ============================================================
# ACCOUNT STATE / TON BALANCE
# ============================================================

async def get_account_state() -> dict[str, Any]:

    data = await api_get(
        "accountStates",
        {
            "address": WALLET_ADDRESS,
            "include_boc": "false",
        },
    )

    # API v3 sekarang menggunakan "accounts".
    accounts = data.get(
        "accounts",
        [],
    )

    if accounts:
        return accounts[0]

    # Fallback untuk response lama.
    account_states = data.get(
        "account_states",
        [],
    )

    if account_states:
        return account_states[0]

    return {}


# ============================================================
# JETTON TRANSFERS
# ============================================================

async def get_jetton_transfers(
    jetton_master: str,
    direction: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:

    params: dict[str, Any] = {
        "owner_address": WALLET_ADDRESS,
        "jetton_master": jetton_master,
        "limit": min(
            limit,
            1000,
        ),
        "sort": "desc",
    }

    if direction in {
        "in",
        "out",
    }:
        params["direction"] = direction

    data = await api_get(
        "jetton/transfers",
        params,
    )

    return data.get(
        "jetton_transfers",
        [],
    )


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

    if not isinstance(
        metadata,
        dict,
    ):
        return {}

    for key in (
        jetton_address,
        jetton_address.replace(
            "-",
            "_",
        ),
    ):

        value = metadata.get(key)

        if isinstance(
            value,
            dict,
        ):
            info = value.get(
                "token_info",
                [],
            )

            if info:
                return info[0]

    for key, value in metadata.items():

        if str(key) == str(
            jetton_address
        ):

            if isinstance(
                value,
                dict,
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

    if raw is None:
        raw = (
            info.get("extra")
            or {}
        ).get("decimals")

    try:
        return int(raw)

    except (
        TypeError,
        ValueError,
    ):
        return default


# ============================================================
# NORMALIZE TON EVENTS
# ============================================================

def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:

    events: list[
        dict[str, Any]
    ] = []

    now = int(
        tx.get("now") or 0
    )

    tx_hash = str(
        tx.get("hash") or ""
    )

    lt = str(
        tx.get("lt") or ""
    )

    aborted = bool(
        (tx.get("description") or {}).get(
            "aborted"
        )
    )

    in_msg = (
        tx.get("in_msg")
        or {}
    )

    source = friendly_address(
        in_msg.get("source")
    )

    destination = friendly_address(
        in_msg.get("destination")
    )

    value = int(
        in_msg.get("value") or 0
    )

    if (
        source
        and source != "-"
        and source != friendly_address(
            WALLET_ADDRESS
        )
        and value > 0
        and not aborted
    ):

        events.append(
            {
                "kind": "ton",
                "direction": "in",
                "symbol": "TON",
                "amount": format_ton(
                    value
                ),
                "source": source,
                "destination": (
                    friendly_address(
                        WALLET_ADDRESS
                    )
                ),
                "counterparty": source,
                "timestamp": now,
                "lt": lt,
                "hash": tx_hash,
                "event_id": (
                    f"ton-in:"
                    f"{tx_hash}"
                ),
                "aborted": aborted,
            }
        )

    for index, msg in enumerate(
        tx.get("out_msgs") or []
    ):

        destination = friendly_address(
            msg.get("destination")
        )

        msg_value = int(
            msg.get("value") or 0
        )

        if (
            destination
            and destination != "-"
            and destination
            != friendly_address(
                WALLET_ADDRESS
            )
            and msg_value > 0
            and not aborted
        ):

            events.append(
                {
                    "kind": "ton",
                    "direction": "out",
                    "symbol": "TON",
                    "amount": format_ton(
                        msg_value
                    ),
                    "source": friendly_address(
                        WALLET_ADDRESS
                    ),
                    "destination": destination,
                    "counterparty": destination,
                    "timestamp": now,
                    "lt": lt,
                    "hash": tx_hash,
                    "event_id": (
                        f"ton-out:"
                        f"{tx_hash}:"
                        f"{index}"
                    ),
                    "aborted": aborted,
                }
            )

    return events


# ============================================================
# NORMALIZE USDT EVENT
# ============================================================

def normalize_usdt_event(
    item: dict[str, Any],
    direction: str,
) -> dict[str, Any]:

    source = friendly_address(
        item.get("source")
    )

    destination = friendly_address(
        item.get("destination")
    )

    if direction == "out":
        counterparty = destination
    else:
        counterparty = source

    return {
        "kind": "usdt",
        "direction": direction,
        "symbol": "USDT",
        "name": "Tether USD",
        "master": USDT_JETTON_MASTER,

        # USDT TON = 6 decimals.
        "amount": format_amount(
            item.get("amount", "0"),
            6,
        ),

        "source": source,
        "destination": destination,
        "counterparty": counterparty,

        "timestamp": int(
            item.get(
                "transaction_now"
            )
            or 0
        ),

        "lt": str(
            item.get(
                "transaction_lt"
            )
            or ""
        ),

        "hash": str(
            item.get(
                "transaction_hash"
            )
            or ""
        ),

        "trace_id": str(
            item.get(
                "trace_id"
            )
            or ""
        ),

        "aborted": bool(
            item.get(
                "transaction_aborted"
            )
        ),

        "event_id": (
            "usdt:"
            f"{item.get('transaction_hash', '')}:"
            f"{item.get('transaction_lt', '')}:"
            f"{direction}"
        ),
    }


# ============================================================
# UNIQUE / SORT EVENTS
# ============================================================

def sort_and_unique_events(
    events: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:

    events.sort(
        key=lambda event: (
            int(
                event.get(
                    "timestamp"
                )
                or 0
            ),
            (
                int(
                    event.get(
                        "lt"
                    )
                    or 0
                )
                if str(
                    event.get(
                        "lt"
                    )
                    or ""
                ).isdigit()
                else 0
            ),
        ),
        reverse=True,
    )

    unique: list[
        dict[str, Any]
    ] = []

    seen: set[str] = set()

    for event in events:

        event_id = str(
            event.get(
                "event_id"
            )
            or ""
        )

        if (
            event_id
            and event_id in seen
        ):
            continue

        if event_id:
            seen.add(
                event_id
            )

        unique.append(
            event
        )

        if len(unique) >= limit:
            break

    return unique


# ============================================================
# RECENT TON
# ============================================================

async def get_recent_ton_events(
    limit: int = 10,
) -> list[dict[str, Any]]:

    transactions = (
        await get_ton_transactions(
            100
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    for tx in transactions:
        events.extend(
            normalize_ton_events(
                tx
            )
        )

    events = [
        event
        for event in events
        if not event.get(
            "aborted"
        )
    ]

    return sort_and_unique_events(
        events,
        limit,
    )


# ============================================================
# RECENT USDT
# ============================================================

async def get_recent_usdt_events(
    limit: int = 10,
) -> list[dict[str, Any]]:

    incoming, outgoing = (
        await asyncio.gather(
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "in",
                100,
            ),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "out",
                100,
            ),
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    for item in incoming:

        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event["aborted"]:
            events.append(
                event
            )

    for item in outgoing:

        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event["aborted"]:
            events.append(
                event
            )

    return sort_and_unique_events(
        events,
        limit,
    )


# ============================================================
# HISTORY FORMAT
# ============================================================

def format_history_event(
    event: dict[str, Any],
    number: int,
) -> str:

    direction = event.get(
        "direction"
    )

    if direction == "in":

        icon = "🟢"
        label = "MASUK"
        sign = "+"
        address_label = "Dari"

    else:

        icon = "🔴"
        label = "KELUAR"
        sign = "-"
        address_label = "Ke"

    symbol = event.get(
        "symbol",
        "TON",
    )

    amount = event.get(
        "amount",
        "0",
    )

    counterparty = event.get(
        "counterparty"
    ) or "-"

    timestamp = fmt_time(
        event.get(
            "timestamp"
        )
    )

    return (
        f"{number}. {icon} "
        f"<b>{label} "
        f"{html_escape(symbol)}</b>\n"
        f"Jumlah: {sign}"
        f"{html_escape(amount)} "
        f"{html_escape(symbol)}\n"
        f"{address_label}:\n"
        f"<code>"
        f"{html_escape(counterparty)}"
        f"</code>\n"
        f"🕐 "
        f"{html_escape(timestamp)} "
        f"{html_escape(TIMEZONE_NAME)}"
    )


# ============================================================
# SHOW TON TRANSACTIONS
# ============================================================

async def show_ton_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Mengambil 10 transaksi TON..."
    )

    try:

        events = (
            await get_recent_ton_events(
                10
            )
        )

        lines = [
            "🟣 "
            "<b>10 TRANSAKSI TON TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Tidak ada transaksi TON."
            )

        else:

            for index, event in enumerate(
                events,
                1,
            ):

                lines.append(
                    format_history_event(
                        event,
                        index,
                    )
                )

                lines.append("")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi TON"
        )

        await query.edit_message_text(
            "❌ Gagal mengambil transaksi TON.\n"
            f"<code>"
            f"{html_escape(exc)}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# SHOW USDT TRANSACTIONS
# ============================================================

async def show_usdt_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Mengambil 10 transaksi USDT..."
    )

    try:

        events = (
            await get_recent_usdt_events(
                10
            )
        )

        lines = [
            "🪙 "
            "<b>10 TRANSAKSI USDT TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Tidak ada transaksi USDT."
            )

        else:

            for index, event in enumerate(
                events,
                1,
            ):

                lines.append(
                    format_history_event(
                        event,
                        index,
                    )
                )

                lines.append("")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi USDT"
        )

        await query.edit_message_text(
            "❌ Gagal mengambil transaksi USDT.\n"
            f"<code>"
            f"{html_escape(exc)}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# MONITOR EVENTS
# ============================================================

async def get_monitor_events() -> list[
    dict[str, Any]
]:

    ton_txs, usdt_in, usdt_out = (
        await asyncio.gather(
            get_ton_transactions(100),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "in",
                100,
            ),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "out",
                100,
            ),
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    for tx in ton_txs:

        events.extend(
            normalize_ton_events(
                tx
            )
        )

    for item in usdt_in:

        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event["aborted"]:
            events.append(
                event
            )

    for item in usdt_out:

        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event["aborted"]:
            events.append(
                event
            )

    return sort_and_unique_events(
        events,
        500,
    )


# ============================================================
# SEND NOTIFICATION
# ============================================================

async def send_event_notification(
    application: Application,
    event: dict[str, Any],
) -> None:

    if not monitor_chats:
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
        event.get(
            "source"
        )
        or "-"
    )

    destination = (
        event.get(
            "destination"
        )
        or "-"
    )

    amount = (
        event.get(
            "amount"
        )
        or "0"
    )

    timestamp = fmt_time(
        event.get(
            "timestamp"
        )
    )

    tx_hash = (
        event.get(
            "hash"
        )
        or ""
    )

    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"
        f"{icon} <b>{label}</b>\n\n"
        f"💰 Jumlah: <b>"
        f"{sign}"
        f"{html_escape(amount)} "
        f"{html_escape(symbol)}"
        f"</b>\n\n"
        f"📤 Pengirim:\n"
        f"<code>"
        f"{html_escape(source)}"
        f"</code>\n\n"
        f"📥 Penerima:\n"
        f"<code>"
        f"{html_escape(destination)}"
        f"</code>\n\n"
        f"📅 "
        f"{html_escape(timestamp)} "
        f"{html_escape(TIMEZONE_NAME)}\n"
    )

    if tx_hash:

        text += (
            f'\n🔗 <a href="'
            f'{explorer_url(tx_hash)}'
            f'">'
            "Lihat transaksi di Tonviewer"
            "</a>"
        )

    failed: list[str] = []

    for chat_id in list(
        monitor_chats
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
                "Gagal mengirim notifikasi "
                "ke %s: %s",
                chat_id,
                exc,
            )

            failed.append(
                chat_id
            )

    for chat_id in failed:

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
        "Monitoring aktif: wallet=%s, "
        "USDT master=%s, interval=%ss",
        WALLET_ADDRESS,
        USDT_JETTON_MASTER,
        POLL_SECONDS,
    )

    while True:

        try:

            events = (
                await get_monitor_events()
            )

            current_ids = {
                event["event_id"]
                for event in events
                if event.get(
                    "event_id"
                )
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

                new_events = [
                    event
                    for event in events
                    if (
                        event.get(
                            "event_id"
                        )
                        and event[
                            "event_id"
                        ]
                        not in seen_event_ids
                        and not event.get(
                            "aborted"
                        )
                    )
                ]

                for event in new_events:

                    seen_event_ids.add(
                        event[
                            "event_id"
                        ]
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
                "Error pada monitor loop"
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
        "👋 <b>TON Wallet Monitor</b>\n\n"
        "Wallet yang dipantau:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"
        "🟣 TON + 🪙 USDT dipantau.\n\n"
        "Pilih menu di bawah:"
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
        "Chat ID Anda:\n"
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

        state, wallets_response = (
            await asyncio.gather(
                get_account_state(),
                get_jetton_wallets(100),
            )
        )

        # FIX PENTING:
        # API v3 memakai "accounts"
        # dan balance ada di account object.
        ton_raw = (
            state.get("balance")
            or "0"
        )

        ton_balance = format_ton(
            ton_raw
        )

        wallets = (
            wallets_response.get(
                "jetton_wallets",
                [],
            )
        )

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"TON: <b>"
            f"{html_escape(ton_balance)} TON"
            f"</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        if not wallets:

            lines.append(
                "Tidak ada Jetton dengan "
                "saldo &gt; 0."
            )

        else:

            for wallet in wallets[:30]:

                master = (
                    wallet.get(
                        "jetton"
                    )
                    or ""
                )

                info = (
                    token_info_from_response(
                        wallets_response,
                        master,
                    )
                )

                symbol = (
                    info.get(
                        "symbol"
                    )
                    or (
                        "USDT"
                        if master
                        == USDT_JETTON_MASTER
                        else "JETTON"
                    )
                )

                name = (
                    info.get(
                        "name"
                    )
                    or symbol
                )

                decimals = (
                    6
                    if master
                    == USDT_JETTON_MASTER
                    else token_decimals(
                        info,
                        9,
                    )
                )

                balance = (
                    format_amount(
                        wallet.get(
                            "balance",
                            "0",
                        ),
                        decimals,
                    )
                )

                lines.append(
                    f"• <b>"
                    f"{html_escape(symbol)}"
                    f"</b> — "
                    f"{html_escape(balance)}\n"
                    f"  "
                    f"{html_escape(name)}"
                )

        lines.extend(
            [
                "",
                "Wallet:\n"
                f"<code>"
                f"{html_escape(WALLET_ADDRESS)}"
                f"</code>",
                f"🕐 Update: "
                f"{html_escape(fmt_time(time.time()))} "
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
            "Gagal mengambil saldo"
        )

        await query.edit_message_text(
            "❌ Gagal mengambil saldo.\n"
            f"<code>"
            f"{html_escape(exc)}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


# ============================================================
# TOKENS
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

        response = (
            await get_jetton_wallets(
                100
            )
        )

        wallets = (
            response.get(
                "jetton_wallets",
                [],
            )
        )

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        if not wallets:

            lines.append(
                "Tidak ada Jetton dengan "
                "saldo &gt; 0."
            )

        else:

            for i, wallet in enumerate(
                wallets[:50],
                1,
            ):

                master = (
                    wallet.get(
                        "jetton"
                    )
                    or ""
                )

                info = (
                    token_info_from_response(
                        response,
                        master,
                    )
                )

                symbol = (
                    info.get(
                        "symbol"
                    )
                    or (
                        "USDT"
                        if master
                        == USDT_JETTON_MASTER
                        else "JETTON"
                    )
                )

                name = (
                    info.get(
                        "name"
                    )
                    or symbol
                )

                decimals = (
                    6
                    if master
                    == USDT_JETTON_MASTER
                    else token_decimals(
                        info,
                        9,
                    )
                )

                balance = (
                    format_amount(
                        wallet.get(
                            "balance",
                            "0",
                        ),
                        decimals,
                    )
                )

                lines.append(
                    f"<b>{i}. "
                    f"{html_escape(symbol)}</b> — "
                    f"{html_escape(balance)}\n"
                    f"   "
                    f"{html_escape(name)}\n"
                    f"   Master: "
                    f"<code>"
                    f"{html_escape(master)}"
                    f"</code>"
                )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil daftar token"
        )

        await query.edit_message_text(
            "❌ Gagal mengambil token.\n"
            f"<code>"
            f"{html_escape(exc)}"
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

        status_text = (
            "⚪ Notifikasi otomatis "
            "<b>DIMATIKAN</b>."
        )

    else:

        monitor_chats.add(
            chat_id
        )

        status_text = (
            "🟢 Notifikasi otomatis "
            "<b>DIHIDUPKAN</b>."
        )

    text = (
        "👁️ <b>MEMANTAU WALLET</b>\n\n"
        f"{status_text}\n\n"
        "Bot memeriksa transaksi TON "
        "dan USDT secara berkala.\n\n"
        f"Interval: "
        f"<b>{POLL_SECONDS} detik</b>"
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=monitor_markup(
            chat_id
        ),
    )


# ============================================================
# HOME
# ============================================================

async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "🏠 <b>TON WALLET MONITOR</b>\n\n"
        f"Wallet:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"
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

    data = query.data

    if data == "balance":

        await show_balance(
            update,
            context,
        )

    elif data == "tx_ton":

        await show_ton_transactions(
            update,
            context,
        )

    elif data == "tx_usdt":

        await show_usdt_transactions(
            update,
            context,
        )

    elif data == "tokens":

        await show_tokens(
            update,
            context,
        )

    elif data == "monitor":

        await show_monitor(
            update,
            context,
        )

    elif data == "home":

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
# POST INIT
# ============================================================

async def post_init(
    application: Application,
) -> None:

    global http_client

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset. "
            "Tambahkan di Railway Variables."
        )

    timeout = httpx.Timeout(
        20.0,
        connect=10.0,
    )

    http_client = httpx.AsyncClient(
        timeout=timeout,
    )

    # ========================================================
    # TELEGRAM MENU BUTTON
    # ========================================================

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Buka menu utama",
            ),
            BotCommand(
                "menu",
                "Buka menu utama",
            ),
            BotCommand(
                "chatid",
                "Lihat Chat ID",
            ),
        ]
    )

    # Ini yang membuat tombol menu Telegram
    # tersedia di area input chat.
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    if not TONCENTER_API_KEY:

        logger.warning(
            "TONCENTER_API_KEY belum diset; "
            "request API v3 menggunakan "
            "rate limit publik."
        )

    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(
            application
        )
    )

    logger.info(
        "post_init selesai."
    )


# ============================================================
# POST SHUTDOWN
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
            "ERROR: TELEGRAM_BOT_TOKEN "
            "belum diset sebagai "
            "environment variable."
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

    # Commands
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

    # Inline buttons
    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    logger.info(
        "Bot Telegram mulai polling..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
