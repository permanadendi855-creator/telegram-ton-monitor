import os
import asyncio
import time
import logging
import base64
import binascii

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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


# ============================================================
# CHAT ID
# ============================================================

# Bisa pakai:
#
# CHAT_ID=123456789
#
# atau:
#
# AUTO_MONITOR_CHAT_IDS=123456789,987654321
#
# Keduanya didukung.

CHAT_ID = os.getenv(
    "CHAT_ID",
    "",
).strip()

AUTO_MONITOR_CHAT_IDS = {
    x.strip()
    for x in os.getenv(
        "AUTO_MONITOR_CHAT_IDS",
        "",
    ).split(",")
    if x.strip()
}

if CHAT_ID:
    AUTO_MONITOR_CHAT_IDS.add(CHAT_ID)


# ============================================================
# LOGGING
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


# ============================================================
# GLOBAL STATE
# ============================================================

monitor_chats: set[str] = set(
    AUTO_MONITOR_CHAT_IDS
)

seen_event_ids: set[str] = set()

baseline_ready = False

http_client: httpx.AsyncClient | None = None


# ============================================================
# SAFE HELPERS
# ============================================================

def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value is None:
            return default

        if isinstance(value, bool):
            return int(value)

        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):
        return default


def safe_str(
    value: Any,
    default: str = "",
) -> str:
    if value is None:
        return default

    return str(value)


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

def raw_to_friendly_address(
    address: str,
    bounceable: bool = False,
) -> str:
    """
    Convert TON raw address:

        0:HEX64

    menjadi friendly TON address:

        UQ...  (non-bounceable)
        EQ...  (bounceable)

    Semua alamat wallet yang ditampilkan bot akan memakai
    friendly address, bukan 0:HEX.
    """

    value = safe_str(address).strip()

    if not value:
        return "-"

    # Sudah friendly.
    if (
        len(value) >= 48
        and value[0] in {
            "E",
            "U",
            "k",
            "0",
        }
        and ":" not in value
    ):
        return value

    if ":" not in value:
        return value

    try:
        workchain_text, hash_part = (
            value.split(":", 1)
        )

        workchain = int(
            workchain_text
        )

        hash_part = (
            hash_part
            .strip()
            .lower()
        )

        if len(hash_part) != 64:
            return value

        account_hash = bytes.fromhex(
            hash_part
        )

        # TON friendly address tags:
        #
        # 0x11 = bounceable
        # 0x51 = non-bounceable
        #
        # EQ = bounceable
        # UQ = non-bounceable

        tag = (
            0x11
            if bounceable
            else 0x51
        )

        workchain_byte = (
            workchain & 0xFF
        ).to_bytes(
            1,
            byteorder="big",
        )

        body = (
            bytes([tag])
            + workchain_byte
            + account_hash
        )

        checksum = binascii.crc_hqx(
            body,
            0,
        ).to_bytes(
            2,
            byteorder="big",
        )

        encoded = base64.urlsafe_b64encode(
            body + checksum
        ).decode("ascii")

        return encoded.rstrip("=")

    except Exception:
        logger.warning(
            "Gagal convert address: %s",
            value,
        )

        return value


def wallet_address(
    address: Any,
) -> str:
    """
    Format alamat wallet sebagai UQ...
    """

    return raw_to_friendly_address(
        safe_str(address),
        bounceable=False,
    )


def contract_address(
    address: Any,
) -> str:
    """
    Format contract/master sebagai EQ...
    """

    return raw_to_friendly_address(
        safe_str(address),
        bounceable=True,
    )


# ============================================================
# NUMBER / TIME FORMAT
# ============================================================

def format_decimal(
    value: Decimal,
    max_places: int = 8,
) -> str:

    q = value.quantize(
        Decimal(
            "1."
            + "0" * max_places
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
            Decimal(
                str(raw or "0")
            )
            / (
                Decimal(10)
                ** decimals
            )
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


def fmt_time(
    timestamp: int | float | None,
) -> str:

    ts = safe_int(timestamp)

    if not ts:
        return "-"

    try:
        dt = datetime.fromtimestamp(
            ts,
            tz=timezone.utc,
        ).astimezone(
            LOCAL_TZ
        )

        return dt.strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    except (
        ValueError,
        OverflowError,
        OSError,
    ):
        return "-"


def explorer_url(
    value: str,
) -> str:
    return (
        "https://tonviewer.com/"
        + value
    )


# ============================================================
# TELEGRAM MENUS
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
                    callback_data="monitor_status",
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "🔴 Matikan Notifikasi"
                        if chat_id in monitor_chats
                        else "🟢 Nyalakan Notifikasi"
                    ),
                    callback_data="monitor_toggle",
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
# TONCENTER API
# ============================================================

async def api_get(
    path: str,
    params: dict[str, Any] | None = None,
    retries: int = 3,
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

    last_error: Exception | None = None

    for attempt in range(
        retries
    ):

        try:
            response = await http_client.get(
                url,
                params=params or {},
                headers=headers,
            )

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                last_error = RuntimeError(
                    f"TON Center HTTP "
                    f"{response.status_code}"
                )

                if attempt < retries - 1:
                    await asyncio.sleep(
                        2 ** attempt
                    )

                    continue

            response.raise_for_status()

            data = response.json()

            if not isinstance(
                data,
                dict,
            ):
                raise RuntimeError(
                    "Response API tidak valid."
                )

            if data.get("error"):
                raise RuntimeError(
                    str(data["error"])
                )

            return data

        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.HTTPStatusError,
            ValueError,
            RuntimeError,
        ) as exc:

            last_error = exc

            if attempt < retries - 1:

                await asyncio.sleep(
                    2 ** attempt
                )

                continue

            break

    raise RuntimeError(
        f"TON Center gagal: "
        f"{last_error}"
    )


# ============================================================
# TON API FUNCTIONS
# ============================================================

async def get_ton_transactions(
    limit: int = 100,
) -> list[dict[str, Any]]:

    data = await api_get(
        "transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": min(
                max(limit, 1),
                1000,
            ),
            "sort": "desc",
        },
    )

    result = data.get(
        "transactions",
        [],
    )

    return (
        result
        if isinstance(result, list)
        else []
    )


async def get_jetton_wallets(
    limit: int = 100,
) -> dict[str, Any]:

    return await api_get(
        "jetton/wallets",
        {
            "owner_address": WALLET_ADDRESS,
            "exclude_zero_balance": "true",
            "limit": min(
                max(limit, 1),
                1000,
            ),
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

    if (
        isinstance(states, list)
        and states
        and isinstance(states[0], dict)
    ):
        return states[0]

    return {}


async def get_jetton_transfers(
    jetton_master: str,
    direction: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:

    params: dict[str, Any] = {
        "owner_address": WALLET_ADDRESS,
        "jetton_master": jetton_master,
        "limit": min(
            max(limit, 1),
            1000,
        ),
        "sort": "desc",
    }

    if direction in {
        "in",
        "out",
    }:
        params["direction"] = (
            direction
        )

    data = await api_get(
        "jetton/transfers",
        params,
    )

    result = data.get(
        "jetton_transfers",
        [],
    )

    return (
        result
        if isinstance(result, list)
        else []
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

    possible_keys = [
        jetton_address,
        jetton_address.replace(
            "-",
            "_",
        ),
    ]

    for key in possible_keys:

        value = metadata.get(
            key
        )

        if not isinstance(
            value,
            dict,
        ):
            continue

        info = value.get(
            "token_info",
            [],
        )

        if (
            isinstance(info, list)
            and info
            and isinstance(info[0], dict)
        ):
            return info[0]

    for key, value in metadata.items():

        if str(key) != str(
            jetton_address
        ):
            continue

        if not isinstance(
            value,
            dict,
        ):
            continue

        info = value.get(
            "token_info",
            [],
        )

        if (
            isinstance(info, list)
            and info
            and isinstance(info[0], dict)
        ):
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

        extra = info.get(
            "extra"
        )

        if isinstance(
            extra,
            dict,
        ):
            raw = extra.get(
                "decimals"
            )

    try:
        decimals = int(raw)

        if decimals < 0:
            return default

        if decimals > 30:
            return default

        return decimals

    except (
        TypeError,
        ValueError,
    ):
        return default


# ============================================================
# NORMALIZE TON TRANSACTIONS
# ============================================================

def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:

    events: list[
        dict[str, Any]
    ] = []

    now = safe_int(
        tx.get("now")
    )

    tx_hash = safe_str(
        tx.get("hash")
    )

    lt = safe_str(
        tx.get("lt")
    )

    in_msg = tx.get(
        "in_msg"
    ) or {}

    if not isinstance(
        in_msg,
        dict,
    ):
        in_msg = {}

    source_raw = (
        in_msg.get("source")
        or ""
    )

    destination_raw = (
        in_msg.get("destination")
        or WALLET_ADDRESS
    )

    value = safe_int(
        in_msg.get("value")
    )

    source = wallet_address(
        source_raw
    )

    destination = wallet_address(
        destination_raw
    )

    # TON MASUK
    if (
        source_raw
        and value > 0
        and source_raw != WALLET_ADDRESS
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
                "destination": destination,
                "counterparty": source,
                "timestamp": now,
                "lt": lt,
                "hash": tx_hash,
                "event_id": (
                    f"ton-in:"
                    f"{tx_hash}"
                ),
            }
        )

    # TON KELUAR
    out_msgs = (
        tx.get("out_msgs")
        or []
    )

    if not isinstance(
        out_msgs,
        list,
    ):
        out_msgs = []

    for index, msg in enumerate(
        out_msgs
    ):

        if not isinstance(
            msg,
            dict,
        ):
            continue

        destination_raw = (
            msg.get("destination")
            or ""
        )

        msg_value = safe_int(
            msg.get("value")
        )

        if (
            destination_raw
            and destination_raw
            != WALLET_ADDRESS
            and msg_value > 0
        ):

            destination = wallet_address(
                destination_raw
            )

            events.append(
                {
                    "kind": "ton",
                    "direction": "out",
                    "symbol": "TON",
                    "amount": format_ton(
                        msg_value
                    ),
                    "source": wallet_address(
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
                }
            )

    return events


# ============================================================
# NORMALIZE USDT
# ============================================================

def normalize_usdt_event(
    item: dict[str, Any],
    direction: str,
) -> dict[str, Any]:

    source_raw = (
        item.get("source")
        or ""
    )

    destination_raw = (
        item.get("destination")
        or ""
    )

    source = wallet_address(
        source_raw
    )

    destination = wallet_address(
        destination_raw
    )

    if direction == "out":
        counterparty = destination
    else:
        counterparty = source

    tx_hash = safe_str(
        item.get(
            "transaction_hash"
        )
    )

    transaction_lt = safe_str(
        item.get(
            "transaction_lt"
        )
    )

    trace_id = safe_str(
        item.get(
            "trace_id"
        )
    )

    event_id = (
        f"usdt:"
        f"{tx_hash}:"
        f"{transaction_lt}:"
        f"{direction}:"
        f"{trace_id}"
    )

    return {
        "kind": "usdt",
        "direction": direction,
        "symbol": "USDT",
        "name": "Tether USD",
        "master": USDT_JETTON_MASTER,
        "amount": format_amount(
            item.get(
                "amount",
                "0",
            ),
            6,
        ),
        "source": source,
        "destination": destination,
        "counterparty": counterparty,
        "timestamp": safe_int(
            item.get(
                "transaction_now"
            )
        ),
        "lt": transaction_lt,
        "hash": tx_hash,
        "trace_id": trace_id,
        "aborted": bool(
            item.get(
                "transaction_aborted"
            )
        ),
        "event_id": event_id,
    }


# ============================================================
# SORT / UNIQUE EVENTS
# ============================================================

def event_sort_key(
    event: dict[str, Any],
) -> tuple[int, int]:

    timestamp = safe_int(
        event.get("timestamp")
    )

    lt_raw = safe_str(
        event.get("lt")
    )

    lt = (
        safe_int(lt_raw)
        if lt_raw.isdigit()
        else 0
    )

    return (
        timestamp,
        lt,
    )


def unique_events(
    events: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:

    events.sort(
        key=event_sort_key,
        reverse=True,
    )

    unique: list[
        dict[str, Any]
    ] = []

    seen: set[str] = set()

    for event in events:

        event_id = safe_str(
            event.get(
                "event_id"
            )
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
            200
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    for tx in transactions:

        if not isinstance(
            tx,
            dict,
        ):
            continue

        events.extend(
            normalize_ton_events(
                tx
            )
        )

    return unique_events(
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
                500,
            ),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "out",
                500,
            ),
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    for item in incoming:

        if not isinstance(
            item,
            dict,
        ):
            continue

        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event.get(
            "aborted"
        ):
            events.append(
                event
            )

    for item in outgoing:

        if not isinstance(
            item,
            dict,
        ):
            continue

        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event.get(
            "aborted"
        ):
            events.append(
                event
            )

    return unique_events(
        events,
        limit,
    )


# ============================================================
# HISTORY DISPLAY
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

    symbol = safe_str(
        event.get(
            "symbol",
            "TON",
        )
    )

    amount = safe_str(
        event.get(
            "amount",
            "0",
        )
    )

    counterparty = (
        event.get(
            "counterparty"
        )
        or "-"
    )

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
# TELEGRAM: TON HISTORY
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
            "🟣 <b>10 TRANSAKSI TON TERAKHIR</b>",
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
# TELEGRAM: USDT HISTORY
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
            "🪙 <b>10 TRANSAKSI USDT TERAKHIR</b>",
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

async def get_monitor_events(
) -> list[dict[str, Any]]:

    ton_txs, usdt_in, usdt_out = (
        await asyncio.gather(
            get_ton_transactions(
                100
            ),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "in",
                200,
            ),
            get_jetton_transfers(
                USDT_JETTON_MASTER,
                "out",
                200,
            ),
        )
    )

    events: list[
        dict[str, Any]
    ] = []

    # TON
    for tx in ton_txs:

        if not isinstance(
            tx,
            dict,
        ):
            continue

        events.extend(
            normalize_ton_events(
                tx
            )
        )

    # USDT IN
    for item in usdt_in:

        if not isinstance(
            item,
            dict,
        ):
            continue

        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event.get(
            "aborted"
        ):
            events.append(
                event
            )

    # USDT OUT
    for item in usdt_out:

        if not isinstance(
            item,
            dict,
        ):
            continue

        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event.get(
            "aborted"
        ):
            events.append(
                event
            )

    return unique_events(
        events,
        1000,
    )


# ============================================================
# NOTIFICATION
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

    symbol = safe_str(
        event.get(
            "symbol",
            "TON",
        )
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

    tx_hash = safe_str(
        event.get("hash")
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
            "\n🔗 "
            f'<a href="{explorer_url(tx_hash)}">'
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
                "Gagal kirim notifikasi "
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
        "Monitoring aktif."
    )

    logger.info(
        "Wallet: %s",
        WALLET_ADDRESS,
    )

    logger.info(
        "USDT master: %s",
        USDT_JETTON_MASTER,
    )

    logger.info(
        "Interval: %ss",
        POLL_SECONDS,
    )

    logger.info(
        "Chat monitor awal: %s",
        sorted(monitor_chats),
    )

    while True:

        try:

            events = (
                await get_monitor_events()
            )

            current_ids = {
                event.get(
                    "event_id"
                )
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
                    "Baseline dibuat: "
                    "%d event.",
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

                # Tandai dulu supaya tidak duplicate.
                for event in new_events:

                    seen_event_ids.add(
                        event[
                            "event_id"
                        ]
                    )

                # Kirim dari yang lama ke yang baru.
                for event in reversed(
                    new_events
                ):

                    await send_event_notification(
                        application,
                        event,
                    )

                # Batasi memory.
                if len(
                    seen_event_ids
                ) > 10000:

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

        try:

            await asyncio.sleep(
                POLL_SECONDS
            )

        except asyncio.CancelledError:

            raise


# ============================================================
# /START
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
# /CHATID
# ============================================================

async def chat_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    chat = update.effective_chat

    if chat is None:
        return

    await update.effective_message.reply_text(
        "Chat ID Anda:\n"
        f"<code>{chat.id}</code>",
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
                get_jetton_wallets(
                    100
                ),
            )
        )

        ton_balance = format_ton(
            state.get(
                "balance",
                "0",
            )
        )

        wallets = (
            wallets_response.get(
                "jetton_wallets",
                [],
            )
        )

        if not isinstance(
            wallets,
            list,
        ):
            wallets = []

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"TON: <b>"
            f"{html_escape(ton_balance)} "
            f"TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        if not wallets:

            lines.append(
                "Tidak ada Jetton "
                "dengan saldo &gt; 0."
            )

        else:

            for wallet in wallets[:30]:

                if not isinstance(
                    wallet,
                    dict,
                ):
                    continue

                master = safe_str(
                    wallet.get(
                        "jetton"
                    )
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

        if not isinstance(
            wallets,
            list,
        ):
            wallets = []

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        if not wallets:

            lines.append(
                "Tidak ada Jetton "
                "dengan saldo &gt; 0."
            )

        else:

            for i, wallet in enumerate(
                wallets[:50],
                1,
            ):

                if not isinstance(
                    wallet,
                    dict,
                ):
                    continue

                master = safe_str(
                    wallet.get(
                        "jetton"
                    )
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

                balance = format_amount(
                    wallet.get(
                        "balance",
                        "0",
                    ),
                    decimals,
                )

                # Master ditampilkan EQ,
                # bukan 0:HEX.
                master_display = (
                    contract_address(
                        master
                    )
                )

                lines.append(
                    f"<b>{i}. "
                    f"{html_escape(symbol)}"
                    f"</b> — "
                    f"{html_escape(balance)}\n"
                    f"   "
                    f"{html_escape(name)}\n"
                    f"   Master:\n"
                    f"   <code>"
                    f"{html_escape(master_display)}"
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
# MONITOR PAGE
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

    status = (
        "🟢 AKTIF"
        if chat_id in monitor_chats
        else "⚪ MATI"
    )

    text = (
        "👁️ <b>MEMANTAU WALLET</b>\n\n"
        f"Status notifikasi: "
        f"<b>{status}</b>\n\n"
        "Bot memeriksa transaksi "
        "TON dan USDT secara berkala.\n\n"
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
# TOGGLE MONITOR
# ============================================================

async def toggle_monitor(
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
        "Bot memeriksa transaksi "
        "TON dan USDT secara berkala.\n\n"
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
# MONITOR STATUS
# ============================================================

async def monitor_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Status monitor tidak diubah."
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
        disable_web_page_preview=True,
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if query is None:
        return

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

    elif data == "monitor_toggle":

        await toggle_monitor(
            update,
            context,
        )

    elif data == "monitor_status":

        await monitor_status(
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
            "TELEGRAM_BOT_TOKEN belum diset."
        )

    timeout = httpx.Timeout(
        20.0,
        connect=10.0,
    )

    http_client = (
        httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
    )

    if not TONCENTER_API_KEY:

        logger.warning(
            "TONCENTER_API_KEY belum diset. "
            "API v3 memiliki rate limit publik."
        )

    # Jalankan monitor background.
    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(
            application
        )
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

    # Buttons
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
