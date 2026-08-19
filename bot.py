import os
import asyncio
import time
import logging
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
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
).strip().rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

TONCENTER_API_KEY = os.getenv(
    "TONCENTER_API_KEY",
    "",
).strip()


def safe_int_env(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """
    Ambil integer dari environment variable tanpa membuat
    aplikasi crash kalau value-nya salah.
    """
    raw = os.getenv(name, "").strip()

    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            logging.warning(
                "%s=%r tidak valid. Menggunakan default=%s",
                name,
                raw,
                default,
            )
            value = default

    if minimum is not None:
        value = max(minimum, value)

    if maximum is not None:
        value = min(maximum, value)

    return value


POLL_SECONDS = safe_int_env(
    "POLL_SECONDS",
    20,
    minimum=10,
    maximum=3600,
)


TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta",
).strip() or "Asia/Jakarta"


try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    logging.warning(
        "TIMEZONE %r tidak ditemukan. "
        "Menggunakan Asia/Jakarta.",
        TIMEZONE_NAME,
    )
    TIMEZONE_NAME = "Asia/Jakarta"
    LOCAL_TZ = ZoneInfo("Asia/Jakarta")


AUTO_MONITOR_CHAT_IDS = {
    x.strip()
    for x in os.getenv(
        "AUTO_MONITOR_CHAT_IDS",
        "",
    ).split(",")
    if x.strip()
}


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

monitor_task: asyncio.Task | None = None


# ============================================================
# HELPERS
# ============================================================

def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        if value is None or value == "":
            return default

        return int(value)

    except (TypeError, ValueError, OverflowError):
        return default


def format_decimal(
    value: Decimal,
    max_places: int = 8,
) -> str:
    if max_places < 0:
        max_places = 0

    quantizer = Decimal(
        "1." + ("0" * max_places)
    )

    try:
        q = value.quantize(
            quantizer,
            rounding=ROUND_DOWN,
        )
    except InvalidOperation:
        return "0"

    text = format(q, "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return text if text else "0"


def format_amount(
    raw: str | int | float | Decimal | None,
    decimals: int,
) -> str:
    try:
        decimals = max(
            0,
            min(int(decimals), 30),
        )

        value = Decimal(
            str(raw if raw is not None else "0")
        ) / (
            Decimal(10) ** decimals
        )

        return format_decimal(
            value,
            min(decimals, 8),
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
        OverflowError,
    ):
        return str(
            raw if raw is not None else "0"
        )


def format_ton(
    raw: str | int | float | Decimal | None,
) -> str:
    return format_amount(
        raw,
        9,
    )


def fmt_time(
    timestamp: int | float | None,
) -> str:
    timestamp_int = safe_int(
        timestamp,
        0,
    )

    if timestamp_int <= 0:
        return "-"

    try:
        dt = datetime.fromtimestamp(
            timestamp_int,
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


def html_escape(
    text: Any,
) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def explorer_url(
    value: str,
) -> str:
    return (
        "https://tonviewer.com/"
        + str(value).strip()
    )


def is_dict(
    value: Any,
) -> bool:
    return isinstance(
        value,
        dict,
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

    url = (
        f"{TONCENTER_BASE}/"
        f"{path.lstrip('/')}"
    )

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": (
            "telegram-ton-monitor/1.0"
        ),
    }

    if TONCENTER_API_KEY:
        headers["X-API-Key"] = (
            TONCENTER_API_KEY
        )

    last_error: Exception | None = None

    for attempt in range(
        max(1, retries)
    ):
        try:
            response = await http_client.get(
                url,
                params=params or {},
                headers=headers,
            )

            response.raise_for_status()

            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    "TON Center mengembalikan "
                    "response JSON tidak valid."
                ) from exc

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Format response TON Center "
                    "tidak sesuai."
                )

            if data.get("error"):
                raise RuntimeError(
                    str(data.get("error"))
                )

            if data.get("errors"):
                raise RuntimeError(
                    str(data.get("errors"))
                )

            return data

        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ) as exc:
            last_error = exc

            status_code = (
                exc.response.status_code
                if isinstance(
                    exc,
                    httpx.HTTPStatusError,
                )
                and exc.response is not None
                else None
            )

            logger.warning(
                "API request gagal "
                "(attempt %s/%s, status=%s): %s",
                attempt + 1,
                retries,
                status_code,
                exc,
            )

            if attempt >= retries - 1:
                break

            # Backoff: 1s, 2s, 4s
            await asyncio.sleep(
                min(
                    2 ** attempt,
                    8,
                )
            )

        except Exception as exc:
            last_error = exc

            logger.exception(
                "Error API tidak terduga."
            )

            if attempt >= retries - 1:
                break

            await asyncio.sleep(1)

    raise RuntimeError(
        f"Gagal mengakses TON Center: "
        f"{last_error}"
    )


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

    transactions = data.get(
        "transactions",
        [],
    )

    if not isinstance(
        transactions,
        list,
    ):
        return []

    return [
        item
        for item in transactions
        if isinstance(item, dict)
    ]


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

    if not isinstance(
        states,
        list,
    ):
        return {}

    for state in states:
        if isinstance(
            state,
            dict,
        ):
            return state

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
        params["direction"] = direction

    data = await api_get(
        "jetton/transfers",
        params,
    )

    transfers = data.get(
        "jetton_transfers",
        [],
    )

    if not isinstance(
        transfers,
        list,
    ):
        return []

    return [
        item
        for item in transfers
        if isinstance(item, dict)
    ]


# ============================================================
# JETTON HELPERS
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

    possible_keys = (
        jetton_address,
        jetton_address.replace(
            "-",
            "_",
        ),
    )

    for key in possible_keys:
        value = metadata.get(key)

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
            and isinstance(
                info[0],
                dict,
            )
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
            and isinstance(
                info[0],
                dict,
            )
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

        if 0 <= decimals <= 30:
            return decimals

    except (
        TypeError,
        ValueError,
    ):
        pass

    return default


# ============================================================
# NORMALIZE TON EVENTS
# ============================================================

def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    now = safe_int(
        tx.get("now"),
        0,
    )

    tx_hash = str(
        tx.get("hash")
        or ""
    )

    lt = str(
        tx.get("lt")
        or ""
    )

    in_msg = tx.get(
        "in_msg"
    )

    if not isinstance(
        in_msg,
        dict,
    ):
        in_msg = {}

    source = str(
        in_msg.get("source")
        or ""
    )

    value = safe_int(
        in_msg.get("value"),
        0,
    )

    if (
        source
        and source != WALLET_ADDRESS
        and value > 0
    ):
        events.append(
            {
                "kind": "ton",
                "direction": "in",
                "symbol": "TON",
                "amount": format_ton(value),
                "source": source,
                "destination": WALLET_ADDRESS,
                "counterparty": source,
                "timestamp": now,
                "lt": lt,
                "hash": tx_hash,
                "aborted": False,
                "event_id": (
                    f"ton-in:{tx_hash}"
                ),
            }
        )

    out_msgs = tx.get(
        "out_msgs"
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

        destination = str(
            msg.get("destination")
            or ""
        )

        msg_value = safe_int(
            msg.get("value"),
            0,
        )

        if (
            destination
            and destination != WALLET_ADDRESS
            and msg_value > 0
        ):
            events.append(
                {
                    "kind": "ton",
                    "direction": "out",
                    "symbol": "TON",
                    "amount": format_ton(
                        msg_value
                    ),
                    "source": WALLET_ADDRESS,
                    "destination": destination,
                    "counterparty": destination,
                    "timestamp": now,
                    "lt": lt,
                    "hash": tx_hash,
                    "aborted": False,
                    "event_id": (
                        f"ton-out:"
                        f"{tx_hash}:"
                        f"{index}"
                    ),
                }
            )

    return events


# ============================================================
# NORMALIZE USDT EVENTS
# ============================================================

def normalize_usdt_event(
    item: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    source = str(
        item.get("source")
        or ""
    )

    destination = str(
        item.get("destination")
        or ""
    )

    if direction == "out":
        counterparty = destination
    else:
        counterparty = source

    transaction_hash = str(
        item.get(
            "transaction_hash"
        )
        or ""
    )

    transaction_lt = str(
        item.get(
            "transaction_lt"
        )
        or ""
    )

    transaction_now = safe_int(
        item.get(
            "transaction_now"
        ),
        0,
    )

    aborted_raw = item.get(
        "transaction_aborted"
    )

    aborted = (
        aborted_raw is True
        or str(
            aborted_raw
        ).lower()
        == "true"
        or safe_int(
            aborted_raw,
            0,
        )
        == 1
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
        "timestamp": transaction_now,
        "lt": transaction_lt,
        "hash": transaction_hash,
        "trace_id": str(
            item.get(
                "trace_id"
            )
            or ""
        ),
        "aborted": aborted,
        "event_id": (
            f"usdt:"
            f"{transaction_hash}:"
            f"{transaction_lt}:"
            f"{direction}"
        ),
    }


# ============================================================
# SORT / UNIQUE EVENTS
# ============================================================

def event_sort_key(
    event: dict[str, Any],
) -> tuple[int, int]:
    timestamp = safe_int(
        event.get(
            "timestamp"
        ),
        0,
    )

    lt_raw = str(
        event.get("lt")
        or ""
    )

    lt = (
        safe_int(
            lt_raw,
            0,
        )
        if lt_raw.isdigit()
        else 0
    )

    return (
        timestamp,
        lt,
    )


def unique_events(
    events: list[dict[str, Any]],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    events.sort(
        key=event_sort_key,
        reverse=True,
    )

    result: list[dict[str, Any]] = []

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
            seen.add(event_id)

        result.append(event)

        if (
            limit is not None
            and len(result) >= limit
        ):
            break

    return result


# ============================================================
# RECENT TON
# ============================================================

async def get_recent_ton_events(
    limit: int = 10,
) -> list[dict[str, Any]]:
    transactions = await get_ton_transactions(
        100
    )

    events: list[dict[str, Any]] = []

    for tx in transactions:
        try:
            events.extend(
                normalize_ton_events(
                    tx
                )
            )
        except Exception:
            logger.exception(
                "Gagal normalize TON transaction."
            )

    events = [
        event
        for event in events
        if not event.get(
            "aborted",
            False,
        )
    ]

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
    incoming, outgoing = await asyncio.gather(
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

    events: list[dict[str, Any]] = []

    for item in incoming:
        try:
            event = normalize_usdt_event(
                item,
                "in",
            )

            if not event.get(
                "aborted",
                False,
            ):
                events.append(event)

        except Exception:
            logger.exception(
                "Gagal normalize USDT incoming."
            )

    for item in outgoing:
        try:
            event = normalize_usdt_event(
                item,
                "out",
            )

            if not event.get(
                "aborted",
                False,
            ):
                events.append(event)

        except Exception:
            logger.exception(
                "Gagal normalize USDT outgoing."
            )

    return unique_events(
        events,
        limit,
    )


# ============================================================
# FORMAT HISTORY
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

    symbol = str(
        event.get(
            "symbol",
            "TON",
        )
    )

    amount = str(
        event.get(
            "amount",
            "0",
        )
    )

    counterparty = str(
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
# TELEGRAM - TON HISTORY
# ============================================================

async def show_ton_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer(
            "Mengambil 10 transaksi TON..."
        )
    except TelegramError:
        pass

    try:
        events = await get_recent_ton_events(
            10
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
            "Gagal mengambil transaksi TON."
        )

        try:
            await query.edit_message_text(
                "❌ Gagal mengambil transaksi TON.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_markup(),
            )
        except TelegramError:
            pass


# ============================================================
# TELEGRAM - USDT HISTORY
# ============================================================

async def show_usdt_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer(
            "Mengambil 10 transaksi USDT..."
        )
    except TelegramError:
        pass

    try:
        events = await get_recent_usdt_events(
            10
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
            "Gagal mengambil transaksi USDT."
        )

        try:
            await query.edit_message_text(
                "❌ Gagal mengambil transaksi USDT.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_markup(),
            )
        except TelegramError:
            pass


# ============================================================
# MONITOR EVENTS
# ============================================================

async def get_monitor_events() -> list[dict[str, Any]]:
    results = await asyncio.gather(
        get_ton_transactions(50),
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
        return_exceptions=True,
    )

    ton_result = results[0]
    usdt_in_result = results[1]
    usdt_out_result = results[2]

    events: list[dict[str, Any]] = []

    # ------------------------------
    # TON
    # ------------------------------

    if isinstance(
        ton_result,
        Exception,
    ):
        logger.warning(
            "TON monitor request gagal: %s",
            ton_result,
        )
    else:
        for tx in ton_result:
            try:
                events.extend(
                    normalize_ton_events(
                        tx
                    )
                )
            except Exception:
                logger.exception(
                    "Gagal normalize TON event."
                )

    # ------------------------------
    # USDT IN
    # ------------------------------

    if isinstance(
        usdt_in_result,
        Exception,
    ):
        logger.warning(
            "USDT incoming request gagal: %s",
            usdt_in_result,
        )
    else:
        for item in usdt_in_result:
            try:
                event = normalize_usdt_event(
                    item,
                    "in",
                )

                if not event.get(
                    "aborted",
                    False,
                ):
                    events.append(event)

            except Exception:
                logger.exception(
                    "Gagal normalize USDT incoming."
                )

    # ------------------------------
    # USDT OUT
    # ------------------------------

    if isinstance(
        usdt_out_result,
        Exception,
    ):
        logger.warning(
            "USDT outgoing request gagal: %s",
            usdt_out_result,
        )
    else:
        for item in usdt_out_result:
            try:
                event = normalize_usdt_event(
                    item,
                    "out",
                )

                if not event.get(
                    "aborted",
                    False,
                ):
                    events.append(event)

            except Exception:
                logger.exception(
                    "Gagal normalize USDT outgoing."
                )

    return unique_events(
        events
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

    symbol = str(
        event.get(
            "symbol",
            "TON",
        )
    )

    source = str(
        event.get(
            "source"
        )
        or "-"
    )

    destination = str(
        event.get(
            "destination"
        )
        or "-"
    )

    amount = str(
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

    tx_hash = str(
        event.get(
            "hash"
        )
        or ""
    )

    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"
        f"{icon} <b>{label}</b>\n\n"
        f"💰 Jumlah: <b>{sign}"
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
        f"📅 "
        f"{html_escape(timestamp)} "
        f"{html_escape(TIMEZONE_NAME)}\n"
    )

    if tx_hash:
        safe_hash = html_escape(
            tx_hash
        )

        text += (
            f'\n🔗 <a href="'
            f'{explorer_url(safe_hash)}'
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

        except Forbidden as exc:
            logger.warning(
                "Bot tidak punya akses "
                "ke chat %s: %s",
                chat_id,
                exc,
            )

            failed.append(
                chat_id
            )

        except BadRequest as exc:
            logger.warning(
                "Telegram BadRequest "
                "untuk chat %s: %s",
                chat_id,
                exc,
            )

            # Jangan langsung menghapus chat
            # kecuali jelas chat tidak tersedia.
            error_text = str(
                exc
            ).lower()

            if (
                "chat not found"
                in error_text
                or "user is deactivated"
                in error_text
            ):
                failed.append(
                    chat_id
                )

        except TelegramError as exc:
            logger.warning(
                "Telegram error "
                "untuk chat %s: %s",
                chat_id,
                exc,
            )

        except Exception as exc:
            logger.exception(
                "Error kirim notifikasi "
                "ke chat %s: %s",
                chat_id,
                exc,
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
        "Interval: %s detik",
        POLL_SECONDS,
    )

    # ------------------------------
    # Initial delay
    # ------------------------------
    await asyncio.sleep(2)

    while True:
        try:
            events = await get_monitor_events()

            current_ids = {
                str(
                    event.get(
                        "event_id"
                    )
                )
                for event in events
                if event.get(
                    "event_id"
                )
            }

            # --------------------------
            # BASELINE
            # --------------------------

            if not baseline_ready:
                seen_event_ids.update(
                    current_ids
                )

                baseline_ready = True

                logger.info(
                    "Baseline dibuat: %d event.",
                    len(current_ids),
                )

            # --------------------------
            # DETECT NEW EVENTS
            # --------------------------

            else:
                new_events = [
                    event
                    for event in events
                    if (
                        event.get(
                            "event_id"
                        )
                        and event.get(
                            "event_id"
                        )
                        not in seen_event_ids
                        and not event.get(
                            "aborted",
                            False,
                        )
                    )
                ]

                if new_events:
                    logger.info(
                        "Ditemukan %d transaksi baru.",
                        len(new_events),
                    )

                for event in new_events:
                    event_id = event.get(
                        "event_id"
                    )

                    if event_id:
                        seen_event_ids.add(
                            str(event_id)
                        )

                # oldest -> newest
                for event in reversed(
                    new_events
                ):
                    try:
                        await send_event_notification(
                            application,
                            event,
                        )
                    except Exception:
                        logger.exception(
                            "Gagal mengirim "
                            "notifikasi event."
                        )

            # --------------------------
            # LIMIT MEMORY
            # --------------------------

            if len(
                seen_event_ids
            ) > 5000:
                logger.info(
                    "Reset event cache. "
                    "Total sebelumnya=%d",
                    len(seen_event_ids),
                )

                seen_event_ids.clear()

                seen_event_ids.update(
                    current_ids
                )

        except asyncio.CancelledError:
            logger.info(
                "Monitor loop dihentikan."
            )
            raise

        except Exception:
            logger.exception(
                "Error pada monitor loop. "
                "Loop tetap berjalan."
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
    message = update.effective_message

    if message is None:
        return

    text = (
        "👋 <b>TON Wallet Monitor</b>\n\n"
        "Wallet yang dipantau:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"
        "🟣 TON + 🪙 USDT dipantau.\n\n"
        "Pilih menu di bawah:"
    )

    try:
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=menu_markup(),
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception(
            "Gagal mengirim /start."
        )


# ============================================================
# /CHATID
# ============================================================

async def chat_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if (
        message is None
        or chat is None
    ):
        return

    try:
        await message.reply_text(
            "Chat ID Anda:\n"
            f"<code>"
            f"{html_escape(chat.id)}"
            f"</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.exception(
            "Gagal mengirim /chatid."
        )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer(
            "Mengambil saldo..."
        )
    except TelegramError:
        pass

    try:
        state_result, wallets_result = (
            await asyncio.gather(
                get_account_state(),
                get_jetton_wallets(100),
                return_exceptions=True,
            )
        )

        # --------------------------
        # ACCOUNT STATE
        # --------------------------

        if isinstance(
            state_result,
            Exception,
        ):
            raise RuntimeError(
                f"Gagal mengambil saldo TON: "
                f"{state_result}"
            )

        # --------------------------
        # JETTON WALLET
        # --------------------------

        if isinstance(
            wallets_result,
            Exception,
        ):
            raise RuntimeError(
                f"Gagal mengambil saldo Jetton: "
                f"{wallets_result}"
            )

        state = state_result

        wallets_response = (
            wallets_result
        )

        ton_balance = format_ton(
            state.get(
                "balance",
                "0",
            )
        )

        wallets = wallets_response.get(
            "jetton_wallets",
            [],
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
            f"{html_escape(ton_balance)} TON"
            f"</b>",
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

                master = str(
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
                "🕐 Update: "
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
            "Gagal mengambil saldo."
        )

        try:
            await query.edit_message_text(
                "❌ Gagal mengambil saldo.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_markup(),
            )
        except TelegramError:
            pass


# ============================================================
# TOKENS
# ============================================================

async def show_tokens(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer(
            "Mengambil token..."
        )
    except TelegramError:
        pass

    try:
        response = await get_jetton_wallets(
            100
        )

        wallets = response.get(
            "jetton_wallets",
            [],
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

                master = str(
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

                balance = format_amount(
                    wallet.get(
                        "balance",
                        "0",
                    ),
                    decimals,
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
            "Gagal mengambil daftar token."
        )

        try:
            await query.edit_message_text(
                "❌ Gagal mengambil token.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_markup(),
            )
        except TelegramError:
            pass


# ============================================================
# MONITOR BUTTON
# ============================================================

async def show_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer()
    except TelegramError:
        pass

    message = query.message

    if message is None:
        return

    chat_id = str(
        message.chat.id
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

    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=monitor_markup(
                chat_id
            ),
        )
    except TelegramError:
        logger.exception(
            "Gagal menampilkan status monitor."
        )


# ============================================================
# HOME
# ============================================================

async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if query is None:
        return

    try:
        await query.answer()
    except TelegramError:
        pass

    try:
        await query.edit_message_text(
            "🏠 <b>TON WALLET MONITOR</b>\n\n"
            "Pilih menu:",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_markup(),
        )
    except TelegramError:
        logger.exception(
            "Gagal menampilkan home."
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

    data = (
        query.data
        or ""
    )

    try:
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
            try:
                await query.answer(
                    "Menu tidak dikenal.",
                    show_alert=True,
                )
            except TelegramError:
                pass

    except Exception:
        logger.exception(
            "Unhandled error pada "
            "button_handler."
        )

        try:
            await query.answer(
                "❌ Terjadi error. "
                "Coba lagi.",
                show_alert=True,
            )
        except TelegramError:
            pass


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application: Application,
) -> None:
    global http_client
    global monitor_task

    logger.info(
        "Menjalankan post_init..."
    )

    # ------------------------------
    # Validate Telegram token
    # ------------------------------

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset. "
            "Tambahkan di Railway Variables."
        )

    # ------------------------------
    # Validate TON Center URL
    # ------------------------------

    if not TONCENTER_BASE.startswith(
        ("http://", "https://")
    ):
        raise RuntimeError(
            "TONCENTER_BASE tidak valid."
        )

    # ------------------------------
    # HTTP CLIENT
    # ------------------------------

    timeout = httpx.Timeout(
        timeout=30.0,
        connect=10.0,
        read=30.0,
        write=30.0,
        pool=10.0,
    )

    limits = httpx.Limits(
        max_connections=20,
        max_keepalive_connections=10,
    )

    http_client = httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
    )

    # ------------------------------
    # API KEY WARNING
    # ------------------------------

    if not TONCENTER_API_KEY:
        logger.warning(
            "TONCENTER_API_KEY belum diset. "
            "API publik dapat terkena rate limit."
        )
    else:
        logger.info(
            "TONCENTER_API_KEY terdeteksi."
        )

    # ------------------------------
    # START MONITOR TASK
    # ------------------------------

    monitor_task = asyncio.create_task(
        monitor_loop(application),
        name="ton-wallet-monitor",
    )

    application.bot_data[
        "monitor_task"
    ] = monitor_task

    logger.info(
        "Monitor task berhasil dibuat."
    )


# ============================================================
# POST SHUTDOWN
# ============================================================

async def post_shutdown(
    application: Application,
) -> None:
    global http_client
    global monitor_task

    logger.info(
        "Menjalankan post_shutdown..."
    )

    # ------------------------------
    # STOP MONITOR
    # ------------------------------

    task = monitor_task

    if task is None:
        task = application.bot_data.get(
            "monitor_task"
        )

    if task is not None:
        if not task.done():
            task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Error saat shutdown "
                "monitor task."
            )

    monitor_task = None

    # ------------------------------
    # CLOSE HTTP
    # ------------------------------

    if http_client is not None:
        try:
            await http_client.aclose()
        except Exception:
            logger.exception(
                "Error menutup HTTP client."
            )

        http_client = None

    logger.info(
        "Shutdown selesai."
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    logger.info(
        "======================================"
    )

    logger.info(
        "TON Wallet Monitor starting..."
    )

    logger.info(
        "Python process started."
    )

    logger.info(
        "Wallet: %s",
        WALLET_ADDRESS,
    )

    logger.info(
        "Timezone: %s",
        TIMEZONE_NAME,
    )

    logger.info(
        "Poll interval: %s seconds",
        POLL_SECONDS,
    )

    logger.info(
        "Auto monitor chats: %d",
        len(
            AUTO_MONITOR_CHAT_IDS
        ),
    )

    logger.info(
        "======================================"
    )

    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "ERROR: TELEGRAM_BOT_TOKEN "
            "belum diset sebagai "
            "environment variable."
        )

    try:
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

        # --------------------------
        # COMMANDS
        # --------------------------

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

        # --------------------------
        # BUTTONS
        # --------------------------

        application.add_handler(
            CallbackQueryHandler(
                button_handler
            )
        )

        logger.info(
            "Telegram handlers siap."
        )

        logger.info(
            "Bot mulai polling..."
        )

        application.run_polling(
            allowed_updates=(
                Update.ALL_TYPES
            ),
            drop_pending_updates=True,
            close_loop=True,
        )

    except KeyboardInterrupt:
        logger.info(
            "Bot dihentikan manual."
        )

    except SystemExit:
        raise

    except Exception:
        logger.exception(
            "FATAL ERROR pada main()."
        )
        raise


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
