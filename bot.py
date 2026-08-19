import os
import asyncio
import base64
import struct
import time
import logging
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

WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"

# USDT wallet/master di jaringan TON
USDT_JETTON_WALLET = "EQAmwNPCaojho0YTS8ZfwnK5zHjduMZeZbeie5dLHeFTAWD7"
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

TONCENTER_BASE = os.getenv(
    "TONCENTER_BASE",
    "https://toncenter.com/api/v3",
).rstrip("/")

TONCENTER_V2_BASE = os.getenv(
    "TONCENTER_V2_BASE",
    "https://toncenter.com/api/v2",
).rstrip("/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TONCENTER_API_KEY = os.getenv("TONCENTER_API_KEY", "").strip()

POLL_SECONDS = max(10, int(os.getenv("POLL_SECONDS", "20")))
MAX_RECENT = 20

TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Jakarta")
LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)

# ============================================================
# CHAT ID
# ============================================================
#
# Bisa memakai:
#
# CHAT_ID=123456789
#
# atau:
#
# AUTO_MONITOR_CHAT_IDS=123456789,987654321
#
# Jika CHAT_ID diisi di Railway, bot otomatis mengirim notifikasi
# setelah restart tanpa perlu /start lagi.
#

CHAT_ID = os.getenv("CHAT_ID", "").strip()

AUTO_MONITOR_CHAT_IDS = {
    x.strip()
    for x in (
        os.getenv("AUTO_MONITOR_CHAT_IDS", "") + "," + CHAT_ID
    ).split(",")
    if x.strip()
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("telegram-ton-monitor")


# ============================================================
# GLOBAL STATE
# ============================================================

monitor_chats: set[str] = set(AUTO_MONITOR_CHAT_IDS)

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
    max_places: int = 6,
) -> str:

    q = value.quantize(
        Decimal("1." + "0" * max_places)
    )

    text = format(q, "f").rstrip("0").rstrip(".")

    return text if text else "0"


def format_amount(
    raw: str | int | None,
    decimals: int,
) -> str:

    try:
        value = Decimal(
            str(raw or "0")
        ) / (Decimal(10) ** decimals)

        return format_decimal(
            value,
            min(decimals, 8),
        )

    except (InvalidOperation, ValueError):
        return str(raw or "0")


def format_ton_nano(
    raw: str | int | None,
) -> str:

    return format_amount(raw, 9)


def fmt_time(
    timestamp: int | float | None,
) -> str:

    if not timestamp:
        return "-"

    dt = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc,
    ).astimezone(LOCAL_TZ)

    return dt.strftime(
        "%d/%m/%Y %H:%M:%S"
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
    address_or_hash: str,
) -> str:

    return (
        f"https://tonviewer.com/{address_or_hash}"
    )


def _crc16_xmodem(
    data: bytes,
) -> bytes:

    crc = 0

    for byte in data:

        crc ^= byte << 8

        for _ in range(8):

            if crc & 0x8000:
                crc = (
                    (crc << 1) ^ 0x1021
                ) & 0xFFFF
            else:
                crc = (
                    crc << 1
                ) & 0xFFFF

    return struct.pack(
        ">H",
        crc,
    )


# ============================================================
# ADDRESS FORMAT
# ============================================================

def as_eq_address(
    address: str | None,
) -> str:

    """
    UQ -> EQ.

    Wallet tetap ditampilkan sebagai UQ di Telegram,
    tetapi untuk query API TON dipakai EQ agar lebih
    konsisten pada endpoint balance/account.
    """

    if not address:
        return "-"

    value = str(address).strip()

    if not (
        value.startswith("EQ")
        or value.startswith("UQ")
    ):
        return value

    try:

        raw = base64.urlsafe_b64decode(
            value
            + "=" * (-len(value) % 4)
        )

        if len(raw) != 36:
            return value

        payload = (
            bytes([0x11])
            + raw[1:34]
        )

        return (
            base64.urlsafe_b64encode(
                payload
                + _crc16_xmodem(payload)
            )
            .decode()
            .rstrip("=")
        )

    except Exception:
        return value


def as_uq_address(
    address: str | None,
) -> str:

    """
    EQ -> UQ.

    Hanya mengubah friendly-address tag.
    Account hash tetap sama.

    Semua alamat penerima/pengirim yang ditampilkan
    oleh bot akan dibuat dalam format UQ/EQ TON,
    bukan alamat TON RAW.
    """

    if not address:
        return "-"

    value = str(address).strip()

    if not (
        value.startswith("EQ")
        or value.startswith("UQ")
    ):
        return value

    try:

        raw = base64.urlsafe_b64decode(
            value
            + "=" * (-len(value) % 4)
        )

        if len(raw) != 36:
            return value

        payload = (
            bytes([0x51])
            + raw[1:34]
        )

        return (
            base64.urlsafe_b64encode(
                payload
                + _crc16_xmodem(payload)
            )
            .decode()
            .rstrip("=")
        )

    except Exception:
        return value


# ============================================================
# TELEGRAM MENU
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
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="home",
                ),
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


async def api_get_v2(
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:

    global http_client

    if http_client is None:
        raise RuntimeError(
            "HTTP client belum siap"
        )

    headers = {}

    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY

    url = (
        f"{TONCENTER_V2_BASE}/"
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
        and data.get("ok") is False
    ):
        raise RuntimeError(
            str(
                data.get("error")
                or data
            )
        )

    if (
        isinstance(data, dict)
        and "result" in data
    ):
        return data["result"]

    return data


# ============================================================
# TON BALANCE
# ============================================================

async def get_ton_balance_raw() -> str:

    """
    Membaca saldo TON live.

    Penting:
    wallet disimpan sebagai UQ,
    tetapi query balance memakai EQ.

    Mencoba beberapa endpoint supaya tidak lagi
    muncul TON = 0 ketika Tonviewer sebenarnya
    menunjukkan saldo.
    """

    api_address = as_eq_address(
        WALLET_ADDRESS
    )

    candidates: list[str] = []

    # --------------------------------------------------------
    # 1. V2 getAddressBalance
    # --------------------------------------------------------

    try:

        result = await api_get_v2(
            "getAddressBalance",
            {
                "address": api_address,
            },
        )

        if result is not None:
            candidates.append(
                str(result)
            )

    except Exception:

        logger.exception(
            "V2 getAddressBalance gagal"
        )

    # --------------------------------------------------------
    # 2. V2 getAddressInformation
    # --------------------------------------------------------

    try:

        info = await api_get_v2(
            "getAddressInformation",
            {
                "address": api_address,
            },
        )

        if (
            isinstance(info, dict)
            and info.get("balance")
            is not None
        ):

            candidates.append(
                str(info["balance"])
            )

    except Exception:

        logger.exception(
            "V2 getAddressInformation gagal"
        )

    # --------------------------------------------------------
    # 3. V3 accountStates
    # --------------------------------------------------------

    try:

        state = await get_account_state()

        for key in (
            "balance",
            "account_balance",
        ):

            if state.get(key) is not None:

                candidates.append(
                    str(state[key])
                )

    except Exception:

        logger.exception(
            "V3 accountStates gagal"
        )

    # --------------------------------------------------------
    # Pilih nilai valid terbesar.
    #
    # Tujuannya menghindari kasus salah satu endpoint
    # sementara mengembalikan 0/stale sementara endpoint
    # lain sudah membaca saldo sebenarnya.
    # --------------------------------------------------------

    valid: list[int] = []

    for value in candidates:

        try:

            valid.append(
                int(value)
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

    if valid:
        return str(
            max(valid)
        )

    return "0"


# ============================================================
# TON TRANSACTIONS
# ============================================================

async def get_ton_transactions(
    limit: int = 50,
) -> list[dict[str, Any]]:

    data = await api_get(
        "transactions",
        {
            "account": as_eq_address(
                WALLET_ADDRESS
            ),
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
# JETTON
# ============================================================

async def get_jetton_transfers(
    limit: int = 100,
    jetton_master: str | None = None,
) -> list[dict[str, Any]]:

    params: dict[str, Any] = {
        "owner_address": WALLET_ADDRESS,
        "limit": min(
            limit,
            1000,
        ),
        "sort": "desc",
    }

    if jetton_master:
        params["jetton_master"] = (
            jetton_master
        )

    data = await api_get(
        "jetton/transfers",
        params,
    )

    return data.get(
        "jetton_transfers",
        [],
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
                limit,
                1000,
            ),
            "sort": "desc",
        },
    )


async def get_account_state() -> dict[str, Any]:

    data = await api_get(
        "accountStates",
        {
            "address": as_eq_address(
                WALLET_ADDRESS
            ),
        },
    )

    states = data.get(
        "account_states",
        [],
    )

    return (
        states[0]
        if states
        else {}
    )


# ============================================================
# TOKEN METADATA
# ============================================================

async def get_usdt_master_metadata() -> dict[str, Any]:

    try:

        data = await api_get(
            "jetton/masters",
            {
                "address":
                    USDT_JETTON_MASTER,
                "limit": 1,
            },
        )

        masters = data.get(
            "jetton_masters",
            [],
        )

        metadata = data.get(
            "metadata",
            {},
        )

        if masters:

            key_candidates = [
                masters[0].get(
                    "address"
                ),
                USDT_JETTON_MASTER,
            ]

            for key in key_candidates:

                if (
                    key
                    and key in metadata
                ):

                    info = metadata[
                        key
                    ].get(
                        "token_info",
                        [],
                    )

                    if info:
                        return info[0]

        for item in metadata.values():

            info = item.get(
                "token_info",
                [],
            )

            if info:
                return info[0]

    except Exception:

        logger.exception(
            "Gagal mengambil metadata USDT"
        )

    return {
        "name": "Tether USD",
        "symbol": "USDT",
        "decimals": "6",
    }


def token_info_from_response(
    response: dict[str, Any],
    jetton_address: str,
) -> dict[str, Any]:

    metadata = response.get(
        "metadata",
        {},
    )

    candidates = [
        jetton_address,
        jetton_address.replace(
            "-",
            "_",
        ),
    ]

    for key in candidates:

        if key in metadata:

            info = metadata[
                key
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

    if raw is None:

        extra = (
            info.get("extra")
            or {}
        )

        raw = extra.get(
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
# NORMALIZE JETTON TRANSFER
# ============================================================

def normalize_jetton_transfer(
    item: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:

    master = (
        item.get(
            "jetton_master"
        )
        or ""
    )

    info = {}

    if metadata:
        info = token_info_from_response(
            metadata,
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
        if master
        == USDT_JETTON_MASTER
        else token_decimals(
            info,
            9,
        )
    )

    source_raw = (
        item.get("source")
        or ""
    )

    destination_raw = (
        item.get("destination")
        or ""
    )

    source = as_uq_address(
        source_raw
    )

    destination = as_uq_address(
        destination_raw
    )

    wallet_uq = as_uq_address(
        WALLET_ADDRESS
    )

    if (
        as_uq_address(source_raw)
        == wallet_uq
    ):

        direction = "out"
        counterparty = destination

    elif (
        as_uq_address(
            destination_raw
        )
        == wallet_uq
    ):

        direction = "in"
        counterparty = source

    else:

        direction = "?"
        counterparty = (
            destination
            or source
        )

    return {
        "kind": "jetton",
        "direction": direction,
        "symbol": symbol,
        "name": name,
        "master": master,
        "amount_raw": str(
            item.get(
                "amount",
                "0",
            )
        ),
        "amount": format_amount(
            item.get(
                "amount",
                "0",
            ),
            decimals,
        ),
        "decimals": decimals,
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

    wallet_uq = as_uq_address(
        WALLET_ADDRESS
    )

    if (
        source
        and as_uq_address(source)
        != wallet_uq
        and value > 0
    ):

        events.append(
            {
                "kind": "ton",
                "direction": "in",
                "symbol": "TON",
                "amount":
                    format_ton_nano(
                        value
                    ),
                "source":
                    as_uq_address(
                        source
                    ),
                "destination":
                    wallet_uq,
                "counterparty":
                    as_uq_address(
                        source
                    ),
                "timestamp": now,
                "lt": lt,
                "hash": tx_hash,
                "event_id":
                    f"ton-in:{tx_hash}",
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
            and as_uq_address(
                destination
            ) != wallet_uq
            and msg_value > 0
        ):

            events.append(
                {
                    "kind": "ton",
                    "direction": "out",
                    "symbol": "TON",
                    "amount":
                        format_ton_nano(
                            msg_value
                        ),
                    "source":
                        wallet_uq,
                    "destination":
                        as_uq_address(
                            destination
                        ),
                    "counterparty":
                        as_uq_address(
                            destination
                        ),
                    "timestamp": now,
                    "lt": lt,
                    "hash": tx_hash,
                    "event_id":
                        f"ton-out:{tx_hash}:{index}",
                }
            )

    return events


def normalize_jetton_event(
    item: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:

    event = normalize_jetton_transfer(
        item,
        response,
    )

    event["event_id"] = (
        f"jetton:"
        f"{event['hash']}:"
        f"{event['lt']}:"
        f"{event['master']}:"
        f"{event['direction']}"
    )

    return event


# ============================================================
# RECENT EVENTS
# ============================================================

async def build_recent_events(
    limit: int = 20,
) -> list[dict[str, Any]]:

    ton_txs, jetton_data = (
        await asyncio.gather(
            get_ton_transactions(50),
            api_get(
                "jetton/transfers",
                {
                    "owner_address":
                        WALLET_ADDRESS,
                    "limit": 100,
                    "sort": "desc",
                },
            ),
        )
    )

    ton_events = []

    for tx in ton_txs:
        ton_events.extend(
            normalize_ton_events(tx)
        )

    jetton_items = (
        jetton_data.get(
            "jetton_transfers",
            [],
        )
    )

    jetton_events = [
        normalize_jetton_event(
            item,
            jetton_data,
        )
        for item in jetton_items
        if (
            item.get(
                "jetton_master"
            )
            or ""
        )
        == USDT_JETTON_MASTER
    ]

    events = (
        ton_events
        + jetton_events
    )

    events = [
        e
        for e in events
        if not e.get("aborted")
    ]

    events.sort(
        key=lambda x: (
            int(
                x.get(
                    "timestamp"
                )
                or 0
            ),
            str(
                x.get("lt")
                or ""
            ),
        ),
        reverse=True,
    )

    unique = []
    keys = set()

    for event in events:

        key = (
            event.get(
                "event_id"
            )
            or (
                event.get("kind"),
                event.get("hash"),
                event.get(
                    "counterparty"
                ),
                event.get("amount"),
            )
        )

        if key in keys:
            continue

        keys.add(key)

        unique.append(event)

        if len(unique) >= limit:
            break

    return unique


# ============================================================
# FORMAT TRANSACTION
# ============================================================

def format_recent_event(
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

    timestamp = fmt_time(
        event.get(
            "timestamp"
        )
    )

    return (
        f"{number}. {icon} "
        f"<b>{label} "
        f"{html_escape(symbol)}</b>\n"
        f"   Jumlah: <b>{sign}"
        f"{html_escape(event.get('amount', '0'))} "
        f"{html_escape(symbol)}</b>\n"
        f"   "
        f"{'Dari' if direction == 'in' else 'Ke'}: "
        f"<code>"
        f"{html_escape(counterparty)}"
        f"</code>\n"
        f"   🕐 {timestamp} "
        f"{html_escape(TIMEZONE_NAME)}"
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

    explorer = (
        explorer_url(tx_hash)
        if tx_hash
        else explorer_url(
            WALLET_ADDRESS
        )
    )

    text = (
        f"🚨 <b>TRANSAKSI BARU</b>\n\n"
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
        f"📅 {timestamp} "
        f"{html_escape(TIMEZONE_NAME)}\n"
    )

    if tx_hash:

        text += (
            f'\n🔗 <a href="{explorer}">'
            f"Lihat transaksi di Tonviewer"
            f"</a>"
        )

    failed = []

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
        "Monitoring aktif: "
        "wallet=%s, "
        "USDT master=%s, "
        "interval=%ss",
        WALLET_ADDRESS,
        USDT_JETTON_MASTER,
        POLL_SECONDS,
    )

    while True:

        try:

            ton_txs, jetton_data = (
                await asyncio.gather(
                    get_ton_transactions(50),
                    api_get(
                        "jetton/transfers",
                        {
                            "owner_address":
                                WALLET_ADDRESS,
                            "limit": 100,
                            "sort": "desc",
                        },
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

            for item in (
                jetton_data.get(
                    "jetton_transfers",
                    [],
                )
            ):

                if (
                    item.get(
                        "jetton_master"
                    )
                    or ""
                ) != USDT_JETTON_MASTER:
                    continue

                event = (
                    normalize_jetton_event(
                        item,
                        jetton_data,
                    )
                )

                if not event.get(
                    "aborted"
                ):

                    events.append(
                        event
                    )

            events.sort(
                key=lambda x: (
                    int(
                        x.get(
                            "timestamp"
                        )
                        or 0
                    ),
                    str(
                        x.get(
                            "lt"
                        )
                        or ""
                    ),
                ),
                reverse=True,
            )

            current_ids = {
                e["event_id"]
                for e in events
                if e.get("event_id")
            }

            if not baseline_ready:

                seen_event_ids.update(
                    current_ids
                )

                baseline_ready = True

                logger.info(
                    "Baseline dibuat: "
                    "%d event terakhir "
                    "ditandai sudah diketahui.",
                    len(current_ids),
                )

            else:

                new_events = []

                for event in events:

                    event_id = event.get(
                        "event_id"
                    )

                    if (
                        not event_id
                        or event_id
                        in seen_event_ids
                    ):
                        continue

                    if event.get(
                        "aborted"
                    ):
                        continue

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
                "Error pada monitor loop"
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    chat_id = str(
        update.effective_chat.id
    )

    # Chat yang melakukan /start langsung masuk
    # ke daftar monitor.
    monitor_chats.add(
        chat_id
    )

    text = (
        "👋 <b>TON WALLET MONITOR</b>\n\n"
        "Wallet yang dipantau:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"
        "🟣 TON + 🪙 USDT "
        "dipantau otomatis 24 jam.\n\n"
        "Tidak ada tombol ON/OFF.\n"
        "Monitoring berjalan otomatis "
        "selama Railway menjalankan bot.\n\n"
        "Gunakan tombol/menu Telegram "
        "di kanan kolom pesan untuk membuka "
        "perintah bot."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def chat_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    await update.effective_message.reply_text(
        f"Chat ID Anda:\n"
        f"<code>"
        f"{update.effective_chat.id}"
        f"</code>",
        parse_mode=ParseMode.HTML,
    )


async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    try:

        ton_balance_raw, wallets_response = (
            await asyncio.gather(
                get_ton_balance_raw(),
                get_jetton_wallets(100),
            )
        )

        ton_balance = format_ton_nano(
            ton_balance_raw
        )

        wallets = wallets_response.get(
            "jetton_wallets",
            [],
        )

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
                "dengan saldo > 0."
            )

        else:

            for wallet in wallets[:30]:

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = (
                    token_info_from_response(
                        wallets_response,
                        master,
                    )
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
                f"{html_escape(as_uq_address(WALLET_ADDRESS))}"
                f"</code>",
                "",
                f"🕐 Update: "
                f"{fmt_time(time.time())} "
                f"{html_escape(TIMEZONE_NAME)}",
            ]
        )

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil saldo"
        )

        await update.effective_message.reply_text(
            f"❌ Gagal mengambil saldo.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
        )


async def transactions_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
                "Belum ditemukan transfer "
                "TON/USDT."
            )

        else:

            for i, event in enumerate(
                events,
                1,
            ):

                lines.append(
                    format_recent_event(
                        event,
                        i,
                    )
                )

                lines.append("")

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi"
        )

        await update.effective_message.reply_text(
            f"❌ Gagal mengambil transaksi.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
        )


async def tokens_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

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
                "Tidak ada Jetton "
                "dengan saldo > 0."
            )

        else:

            for i, wallet in enumerate(
                wallets[:50],
                1,
            ):

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = (
                    token_info_from_response(
                        response,
                        master,
                    )
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
                    f"{html_escape(symbol)}"
                    f"</b> — "
                    f"{html_escape(balance)}\n"
                    f"   "
                    f"{html_escape(name)}\n"
                    f"   Master: "
                    f"<code>"
                    f"{html_escape(master)}"
                    f"</code>"
                )

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil daftar token"
        )

        await update.effective_message.reply_text(
            f"❌ Gagal mengambil token.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
        )


# ============================================================
# INLINE MENU
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

        ton_balance_raw, wallets_response = (
            await asyncio.gather(
                get_ton_balance_raw(),
                get_jetton_wallets(100),
            )
        )

        ton_balance = format_ton_nano(
            ton_balance_raw
        )

        wallets = wallets_response.get(
            "jetton_wallets",
            [],
        )

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
                "dengan saldo > 0."
            )

        else:

            for wallet in wallets[:30]:

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = (
                    token_info_from_response(
                        wallets_response,
                        master,
                    )
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
                f"{html_escape(as_uq_address(WALLET_ADDRESS))}"
                f"</code>",
                "",
                f"🕐 Update: "
                f"{fmt_time(time.time())} "
                f"{html_escape(TIMEZONE_NAME)}",
            ]
        )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil saldo"
        )

        await query.edit_message_text(
            f"❌ Gagal mengambil saldo.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )


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
                "Tidak ada Jetton "
                "dengan saldo > 0."
            )

        else:

            for i, wallet in enumerate(
                wallets[:50],
                1,
            ):

                master = (
                    wallet.get("jetton")
                    or ""
                )

                info = (
                    token_info_from_response(
                        response,
                        master,
                    )
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
                    f"{html_escape(symbol)}"
                    f"</b> — "
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
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil daftar token"
        )

        await query.edit_message_text(
            f"❌ Gagal mengambil token.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )


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
                "Belum ditemukan transfer "
                "TON/Jetton."
            )

        else:

            for i, event in enumerate(
                events,
                1,
            ):

                lines.append(
                    format_recent_event(
                        event,
                        i,
                    )
                )

                lines.append("")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
            disable_web_page_preview=True,
        )

    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi"
        )

        await query.edit_message_text(
            f"❌ Gagal mengambil transaksi.\n"
            f"<code>"
            f"{html_escape(str(exc))}"
            f"</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Kembali",
                            callback_data="home",
                        )
                    ]
                ]
            ),
        )


async def show_monitor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer(
        "Monitoring otomatis 24 jam aktif."
    )

    await show_home(
        update,
        context,
    )


async def show_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        "🏠 <b>TON WALLET MONITOR</b>\n\n"
        "Gunakan menu Telegram di kanan "
        "kolom pesan untuk membuka perintah.\n\n"
        "🟣 TON + 🪙 USDT "
        "tetap dipantau otomatis 24 jam.",
        parse_mode=ParseMode.HTML,
    )


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

    elif data == "tx20":

        await show_transactions(
            update,
            context,
        )

    elif data == "tokens":

        await show_tokens(
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
# APP LIFECYCLE
# ============================================================

async def post_init(
    application: Application,
) -> None:

    global http_client

    timeout = httpx.Timeout(
        20.0,
        connect=10.0,
    )

    http_client = (
        httpx.AsyncClient(
            timeout=timeout
        )
    )

    if not TELEGRAM_BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset. "
            "Tambahkan di Railway Variables."
        )

    if not TONCENTER_API_KEY:

        logger.warning(
            "TONCENTER_API_KEY belum diset. "
            "API v3 dapat membatasi request. "
            "Gunakan API key untuk monitoring 24/7."
        )

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
            BotCommand(
                "saldo",
                "Lihat saldo TON + USDT",
            ),
            BotCommand(
                "transaksi",
                "Lihat 20 transaksi terakhir",
            ),
            BotCommand(
                "token",
                "Lihat token yang dimiliki",
            ),
        ]
    )

    # Ini yang membuat ikon menu Telegram
    # muncul di kanan kolom pesan.
    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    # Monitoring langsung jalan ketika Railway
    # menjalankan bot.
    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(application)
    )

    logger.info(
        "AUTO MONITOR AKTIF. Chat IDs: %s",
        sorted(monitor_chats),
    )


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
        CommandHandler(
            "saldo",
            balance_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "transaksi",
            transactions_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "token",
            tokens_command,
        )
    )

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


if __name__ == "__main__":
    main()
