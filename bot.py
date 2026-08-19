import os
import asyncio
import base64
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from telegram import (
    Update,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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


TONCENTER_V3 = os.getenv(
    "TONCENTER_BASE",
    "https://toncenter.com/api/v3",
).rstrip("/")


TONCENTER_V2 = os.getenv(
    "TONCENTER_V2_BASE",
    "https://toncenter.com/api/v2",
).rstrip("/")


TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()


TONCENTER_API_KEY = os.getenv(
    "TONCENTER_API_KEY",
    "",
).strip()


# CHAT_ID UTAMA UNTUK NOTIFIKASI OTOMATIS
CHAT_ID = os.getenv(
    "CHAT_ID",
    "",
).strip()


POLL_SECONDS = max(
    15,
    int(
        os.getenv(
            "POLL_SECONDS",
            "20",
        )
    ),
)


TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta",
)


LOCAL_TZ = ZoneInfo(
    TIMEZONE_NAME
)


# Dukungan beberapa CHAT_ID kalau diperlukan.
monitor_chats: set[str] = {
    x.strip()
    for x in os.getenv(
        "AUTO_MONITOR_CHAT_IDS",
        "",
    ).split(",")
    if x.strip()
}


if CHAT_ID:
    monitor_chats.add(CHAT_ID)


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
    "ton-wallet-monitor"
)


# ============================================================
# GLOBALS
# ============================================================

http_client: httpx.AsyncClient | None = None

monitor_task: asyncio.Task | None = None

seen_event_ids: set[str] = set()

baseline_ready = False


# ============================================================
# FORMAT HELPERS
# ============================================================

def html_escape(
    value: Any,
) -> str:

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_decimal(
    value: Decimal,
    places: int = 8,
) -> str:

    q = Decimal(
        "1."
        + ("0" * places)
    )

    text = format(
        value.quantize(q),
        "f",
    )

    text = (
        text
        .rstrip("0")
        .rstrip(".")
    )

    return text or "0"


def format_amount(
    raw: Any,
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
            min(
                decimals,
                8,
            ),
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return str(
            raw or "0"
        )


def format_ton(
    raw: Any,
) -> str:

    return format_amount(
        raw,
        9,
    )


def fmt_time(
    timestamp: Any,
) -> str:

    try:

        if not timestamp:
            return "-"

        return (
            datetime
            .fromtimestamp(
                int(timestamp),
                tz=timezone.utc,
            )
            .astimezone(
                LOCAL_TZ
            )
            .strftime(
                "%d/%m/%Y %H:%M:%S"
            )
        )

    except (
        ValueError,
        TypeError,
        OSError,
    ):

        return "-"


# ============================================================
# TON ADDRESS CONVERSION
# ============================================================

def crc16_xmodem(
    data: bytes,
) -> int:

    crc = 0

    for byte in data:

        crc ^= (
            byte << 8
        )

        for _ in range(8):

            if crc & 0x8000:

                crc = (
                    (
                        crc << 1
                    )
                    ^ 0x1021
                ) & 0xFFFF

            else:

                crc = (
                    crc << 1
                ) & 0xFFFF

    return crc


def raw_to_uq(
    address: Any,
) -> str:
    """
    Mengubah alamat TON raw:

        0:abcdef...

    menjadi alamat friendly:

        UQ...

    UQ dipakai sebagai format non-bounceable mainnet.
    """

    text = str(
        address or ""
    ).strip()


    # Sudah friendly.
    if text.startswith(
        (
            "EQ",
            "UQ",
            "kQ",
            "0Q",
        )
    ):

        return text


    if ":" not in text:

        return text


    try:

        wc_text, hex_addr = (
            text.split(
                ":",
                1,
            )
        )


        if len(hex_addr) != 64:

            return text


        wc = int(
            wc_text
        )


        if wc < -128 or wc > 127:

            return text


        # 0x51 = non-bounceable mainnet.
        body = (
            bytes(
                [
                    0x51,
                    wc & 0xFF,
                ]
            )
            + bytes.fromhex(
                hex_addr
            )
        )


        crc = crc16_xmodem(
            body
        )


        encoded = (
            base64.urlsafe_b64encode(
                body
                + crc.to_bytes(
                    2,
                    "big",
                )
            )
            .decode()
            .rstrip("=")
        )


        return encoded


    except (
        ValueError,
        TypeError,
    ):

        return text


def friendly_address(
    address: Any,
) -> str:

    return raw_to_uq(
        address
    )


def explorer_url(
    address: Any,
) -> str:

    return (
        "https://tonviewer.com/"
        + friendly_address(
            address
        )
    )


# ============================================================
# TELEGRAM KEYBOARD
# ============================================================

def menu_keyboard() -> ReplyKeyboardMarkup:

    return ReplyKeyboardMarkup(
        [
            [
                "💰 Info Saldo",
                "🟣 10 Transaksi TON",
            ],
            [
                "🪙 10 Transaksi USDT",
                "🪙 Token Dimiliki",
            ],
            [
                "🔄 Refresh",
            ],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=(
            "Pilih menu..."
        ),
    )


def back_keyboard() -> ReplyKeyboardMarkup:

    return ReplyKeyboardMarkup(
        [
            [
                "⬅️ Kembali",
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ============================================================
# TONCENTER API
# ============================================================

async def api_get(
    path: str,
    params: dict[str, Any] | None = None,
    v2: bool = False,
) -> dict[str, Any]:

    if http_client is None:

        raise RuntimeError(
            "HTTP client belum siap"
        )


    base = (
        TONCENTER_V2
        if v2
        else TONCENTER_V3
    )


    headers: dict[str, str] = {}


    if TONCENTER_API_KEY:

        headers[
            "X-API-Key"
        ] = TONCENTER_API_KEY


    response = await http_client.get(
        (
            f"{base}/"
            f"{path.lstrip('/')}"
        ),
        params=params or {},
        headers=headers,
    )


    response.raise_for_status()


    data = response.json()


    if (
        isinstance(
            data,
            dict,
        )
        and data.get("error")
    ):

        raise RuntimeError(
            str(
                data["error"]
            )
        )


    return data


# ============================================================
# TON BALANCE
# ============================================================

async def get_ton_balance() -> str:

    # Gunakan API v2 address information
    # karena balance native TON tersedia langsung
    # dalam nanotons.

    data = await api_get(
        "getAddressInformation",
        {
            "address": WALLET_ADDRESS,
        },
        v2=True,
    )


    return format_ton(
        data.get(
            "balance",
            "0",
        )
    )


# ============================================================
# TRANSACTIONS
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


async def get_jetton_transfers(
    master: str,
    direction: str,
    limit: int = 100,
) -> list[dict[str, Any]]:

    data = await api_get(
        "jetton/transfers",
        {
            "owner_address": WALLET_ADDRESS,
            "jetton_master": master,
            "direction": direction,
            "limit": min(
                limit,
                1000,
            ),
            "sort": "desc",
        },
    )


    return data.get(
        "jetton_transfers",
        [],
    )


# ============================================================
# JETTON INFO
# ============================================================

def token_info_from_response(
    response: dict[str, Any],
    jetton_address: str,
) -> dict[str, Any]:

    metadata = (
        response.get(
            "metadata"
        )
        or {}
    )


    value = metadata.get(
        jetton_address
    )


    if isinstance(
        value,
        dict,
    ):

        info = (
            value.get(
                "token_info"
            )
            or []
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
            info.get(
                "extra"
            )
            or {}
        ).get(
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
# NORMALIZE TON EVENTS
# ============================================================

def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:

    events: list[
        dict[str, Any]
    ] = []


    now = int(
        tx.get(
            "now"
        )
        or tx.get(
            "utime"
        )
        or 0
    )


    transaction_id = (
        tx.get(
            "transaction_id"
        )
        or {}
    )


    tx_hash = str(
        tx.get(
            "hash"
        )
        or transaction_id.get(
            "hash"
        )
        or ""
    )


    lt = str(
        tx.get(
            "lt"
        )
        or transaction_id.get(
            "lt"
        )
        or ""
    )


    # -------------------------
    # INCOMING TON
    # -------------------------

    in_msg = (
        tx.get(
            "in_msg"
        )
        or {}
    )


    source = friendly_address(
        in_msg.get(
            "source"
        )
        or ""
    )


    value = int(
        in_msg.get(
            "value"
        )
        or 0
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
                "amount": format_ton(
                    value
                ),
                "source": source,
                "destination": WALLET_ADDRESS,
                "counterparty": source,
                "timestamp": now,
                "lt": lt,
                "hash": tx_hash,
                "event_id": (
                    f"ton-in:{tx_hash}"
                ),
            }
        )


    # -------------------------
    # OUTGOING TON
    # -------------------------

    out_messages = (
        tx.get(
            "out_msgs"
        )
        or []
    )


    for index, msg in enumerate(
        out_messages
    ):

        destination = friendly_address(
            msg.get(
                "destination"
            )
            or ""
        )


        msg_value = int(
            msg.get(
                "value"
            )
            or 0
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

    source = friendly_address(
        item.get(
            "source"
        )
        or ""
    )


    destination = friendly_address(
        item.get(
            "destination"
        )
        or ""
    )


    counterparty = (
        destination
        if direction == "out"
        else source
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
        "timestamp": int(
            item.get(
                "transaction_now"
            )
            or item.get(
                "utime"
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
            f"usdt:"
            f"{item.get('transaction_hash', '')}:"
            f"{item.get('transaction_lt', '')}:"
            f"{direction}"
        ),
    }


# ============================================================
# EVENT SORTING
# ============================================================

def sort_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    return sorted(
        events,
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


def unique_events(
    events: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:

    result: list[
        dict[str, Any]
    ] = []


    seen: set[str] = set()


    for event in sort_events(
        events
    ):

        event_id = str(
            event.get(
                "event_id"
            )
            or ""
        )


        if (
            not event_id
            or event_id in seen
        ):

            continue


        seen.add(
            event_id
        )


        result.append(
            event
        )


        if len(result) >= limit:

            break


    return result


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

        if item.get(
            "transaction_aborted"
        ):

            continue


        events.append(
            normalize_usdt_event(
                item,
                "in",
            )
        )


    for item in outgoing:

        if item.get(
            "transaction_aborted"
        ):

            continue


        events.append(
            normalize_usdt_event(
                item,
                "out",
            )
        )


    return unique_events(
        events,
        limit,
    )


# ============================================================
# ALL EVENTS FOR MONITOR
# ============================================================

async def get_monitor_events() -> list[
    dict[str, Any]
]:

    (
        ton_txs,
        usdt_in,
        usdt_out,
    ) = await asyncio.gather(

        get_ton_transactions(
            100
        ),

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

        if not item.get(
            "transaction_aborted"
        ):

            events.append(
                normalize_usdt_event(
                    item,
                    "in",
                )
            )


    for item in usdt_out:

        if not item.get(
            "transaction_aborted"
        ):

            events.append(
                normalize_usdt_event(
                    item,
                    "out",
                )
            )


    return sort_events(
        events
    )


# ============================================================
# HISTORY TEXT
# ============================================================

def history_text(
    title: str,
    events: list[dict[str, Any]],
) -> str:

    lines = [
        title,
        "",
    ]


    if not events:

        lines.append(
            "Tidak ada transaksi."
        )

        return "\n".join(
            lines
        )


    for index, event in enumerate(
        events,
        1,
    ):

        incoming = (
            event.get(
                "direction"
            )
            == "in"
        )


        if incoming:

            icon = "🟢"
            label = "MASUK"
            sign = "+"
            who = "Dari"

        else:

            icon = "🔴"
            label = "KELUAR"
            sign = "-"
            who = "Ke"


        symbol = event.get(
            "symbol",
            "TON",
        )


        amount = event.get(
            "amount",
            "0",
        )


        counterparty = friendly_address(
            event.get(
                "counterparty"
            )
            or "-"
        )


        lines.extend(
            [
                (
                    f"{index}. "
                    f"{icon} "
                    f"<b>{label} "
                    f"{html_escape(symbol)}"
                    f"</b>"
                ),
                (
                    f"Jumlah: "
                    f"{sign}"
                    f"{html_escape(amount)} "
                    f"{html_escape(symbol)}"
                ),
                f"{who}:",
                (
                    f"<code>"
                    f"{html_escape(counterparty)}"
                    f"</code>"
                ),
                (
                    f"🕐 "
                    f"{html_escape(fmt_time(event.get('timestamp')))} "
                    f"{html_escape(TIMEZONE_NAME)}"
                ),
                "",
            ]
        )


    return "\n".join(
        lines
    ).rstrip()


# ============================================================
# SEND TEXT
# ============================================================

async def send_text(
    update: Update,
    text: str,
    keyboard: ReplyKeyboardMarkup | None = None,
) -> None:

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=(
            keyboard
            or menu_keyboard()
        ),
        disable_web_page_preview=True,
    )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    chat_id = str(
        update.effective_chat.id
    )


    # Fallback kalau CHAT_ID belum diisi.
    # Setelah Railway restart, CHAT_ID tetap diperlukan.
    if not monitor_chats:

        monitor_chats.add(
            chat_id
        )


        logger.warning(
            "CHAT_ID belum diset; "
            "menggunakan chat %s "
            "sampai proses restart.",
            chat_id,
        )


    text = (
        "🏠 <b>TON WALLET MONITOR</b>\n\n"
        "Wallet:\n"
        f"<code>"
        f"{html_escape(WALLET_ADDRESS)}"
        f"</code>\n\n"
        "🟣 TON + 🪙 USDT "
        "dipantau otomatis 24 jam.\n\n"
        "Tidak ada tombol ON/OFF lagi.\n"
        "Monitoring berjalan otomatis "
        "selama Railway menjalankan bot."
    )


    await send_text(
        update,
        text,
        menu_keyboard(),
    )


# ============================================================
# /CHATID
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
# SALDO
# ============================================================

async def show_balance(
    update: Update,
) -> None:

    try:

        (
            ton_balance,
            wallets_response,
        ) = await asyncio.gather(

            get_ton_balance(),

            get_jetton_wallets(
                100
            ),
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
            (
                f"TON: "
                f"<b>"
                f"{html_escape(ton_balance)} "
                f"TON"
                f"</b>"
            ),
            "",
            "🪙 <b>JETTON</b>",
        ]


        if not wallets:

            lines.append(
                "Tidak ada Jetton "
                "dengan saldo &gt; 0."
            )


        else:

            for wallet in wallets[
                :30
            ]:

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


                lines.extend(
                    [
                        (
                            f"• <b>"
                            f"{html_escape(symbol)}"
                            f"</b> — "
                            f"{html_escape(balance)}"
                        ),
                        (
                            f"  "
                            f"{html_escape(name)}"
                        ),
                    ]
                )


        lines.extend(
            [
                "",
                (
                    "Wallet:\n"
                    f"<code>"
                    f"{html_escape(WALLET_ADDRESS)}"
                    f"</code>"
                ),
                (
                    f"🕐 Update: "
                    f"{html_escape(fmt_time(datetime.now(timezone.utc).timestamp()))} "
                    f"{html_escape(TIMEZONE_NAME)}"
                ),
            ]
        )


        await send_text(
            update,
            "\n".join(lines),
            back_keyboard(),
        )


    except Exception as exc:

        logger.exception(
            "Gagal mengambil saldo"
        )


        await send_text(
            update,
            (
                "❌ Gagal mengambil saldo.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>"
            ),
            back_keyboard(),
        )


# ============================================================
# TOKEN
# ============================================================

async def show_tokens(
    update: Update,
) -> None:

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
                "Tidak ada Jetton "
                "dengan saldo &gt; 0."
            )


        for index, wallet in enumerate(
            wallets[:50],
            1,
        ):

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


            lines.extend(
                [
                    (
                        f"<b>"
                        f"{index}. "
                        f"{html_escape(symbol)}"
                        f"</b> — "
                        f"{html_escape(balance)}"
                    ),
                    (
                        f"   "
                        f"{html_escape(name)}"
                    ),
                    (
                        f"   Master: "
                        f"<code>"
                        f"{html_escape(friendly_address(master))}"
                        f"</code>"
                    ),
                ]
            )


        await send_text(
            update,
            "\n".join(lines),
            back_keyboard(),
        )


    except Exception as exc:

        logger.exception(
            "Gagal mengambil token"
        )


        await send_text(
            update,
            (
                "❌ Gagal mengambil token.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>"
            ),
            back_keyboard(),
        )


# ============================================================
# TON HISTORY
# ============================================================

async def show_ton_transactions(
    update: Update,
) -> None:

    try:

        events = (
            await get_recent_ton_events(
                10
            )
        )


        text = history_text(
            "🟣 <b>10 TRANSAKSI TON TERAKHIR</b>",
            events,
        )


        await send_text(
            update,
            text,
            back_keyboard(),
        )


    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi TON"
        )


        await send_text(
            update,
            (
                "❌ Gagal mengambil "
                "transaksi TON.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>"
            ),
            back_keyboard(),
        )


# ============================================================
# USDT HISTORY
# ============================================================

async def show_usdt_transactions(
    update: Update,
) -> None:

    try:

        events = (
            await get_recent_usdt_events(
                10
            )
        )


        text = history_text(
            "🪙 <b>10 TRANSAKSI USDT TERAKHIR</b>",
            events,
        )


        await send_text(
            update,
            text,
            back_keyboard(),
        )


    except Exception as exc:

        logger.exception(
            "Gagal mengambil transaksi USDT"
        )


        await send_text(
            update,
            (
                "❌ Gagal mengambil "
                "transaksi USDT.\n"
                f"<code>"
                f"{html_escape(exc)}"
                f"</code>"
            ),
            back_keyboard(),
        )


# ============================================================
# AUTO NOTIFICATION
# ============================================================

async def send_event_notification(
    application: Application,
    event: dict[str, Any],
) -> None:

    if not monitor_chats:

        return


    incoming = (
        event.get(
            "direction"
        )
        == "in"
    )


    if incoming:

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


    source = friendly_address(
        event.get(
            "source"
        )
        or "-"
    )


    destination = friendly_address(
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


    tx_hash = str(
        event.get(
            "hash"
        )
        or ""
    )


    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"

        f"{icon} "
        f"<b>{label}</b>\n\n"

        f"💰 Jumlah: "
        f"<b>"
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
        f"{html_escape(fmt_time(event.get('timestamp')))} "
        f"{html_escape(TIMEZONE_NAME)}"
    )


    if tx_hash:

        text += (
            "\n\n"
            f'🔗 <a href="{explorer_url(tx_hash)}">'
            "Lihat transaksi di Tonviewer"
            "</a>"
        )


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


        except Exception:

            # JANGAN hapus CHAT_ID.
            # Error sementara tidak boleh
            # mematikan monitoring.
            logger.exception(
                "Gagal mengirim "
                "notifikasi ke CHAT_ID=%s. "
                "ID tidak dihapus.",
                chat_id,
            )


# ============================================================
# 24/7 MONITOR LOOP
# ============================================================

async def monitor_loop(
    application: Application,
) -> None:

    global baseline_ready


    logger.info(
        "=================================================="
    )

    logger.info(
        "AUTO MONITOR AKTIF"
    )

    logger.info(
        "Wallet: %s",
        WALLET_ADDRESS,
    )

    logger.info(
        "USDT Master: %s",
        USDT_JETTON_MASTER,
    )

    logger.info(
        "CHAT ID: %s",
        monitor_chats,
    )

    logger.info(
        "Interval: %s detik",
        POLL_SECONDS,
    )

    logger.info(
        "=================================================="
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


            # --------------------------------
            # BASELINE
            # --------------------------------

            if not baseline_ready:

                seen_event_ids.update(
                    current_ids
                )

                baseline_ready = True


                logger.info(
                    "Baseline dibuat: %d event",
                    len(
                        current_ids
                    ),
                )


            # --------------------------------
            # EVENT BARU
            # --------------------------------

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


                # Kirim dari yang paling lama
                # ke paling baru.

                for event in reversed(
                    new_events
                ):

                    await send_event_notification(
                        application,
                        event,
                    )


            # --------------------------------
            # MEMORY CLEANUP
            # --------------------------------

            if len(
                seen_event_ids
            ) > 10000:

                seen_event_ids.intersection_update(
                    current_ids
                )


        except asyncio.CancelledError:

            raise


        except Exception:

            logger.exception(
                "Error monitor. "
                "Bot TIDAK dimatikan. "
                "Akan mencoba lagi."
            )


        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# MENU HANDLER
# ============================================================

async def handle_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    text = (
        update.effective_message.text
        or ""
    ).strip()


    if text in {
        "💰 Info Saldo",
        "Info Saldo",
    }:

        await show_balance(
            update
        )


    elif text in {
        "🟣 10 Transaksi TON",
        "10 Transaksi TON",
    }:

        await show_ton_transactions(
            update
        )


    elif text in {
        "🪙 10 Transaksi USDT",
        "10 Transaksi USDT",
    }:

        await show_usdt_transactions(
            update
        )


    elif text in {
        "🪙 Token Dimiliki",
        "Token Dimiliki",
    }:

        await show_tokens(
            update
        )


    elif text in {
        "🔄 Refresh",
        "Refresh",
        "🏠 Menu",
        "Menu",
    }:

        await start(
            update,
            context,
        )


    elif text in {
        "⬅️ Kembali",
        "Kembali",
    }:

        await start(
            update,
            context,
        )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application: Application,
) -> None:

    global http_client
    global monitor_task


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
            timeout=timeout
        )
    )


    # Hapus daftar command Telegram
    # supaya panel biru "Menu" tidak lagi
    # menjadi menu utama kita.

    try:

        await application.bot.delete_my_commands()

    except Exception:

        logger.exception(
            "Gagal menghapus "
            "command menu Telegram."
        )


    if not monitor_chats:

        logger.warning(
            "CHAT_ID belum diset. "
            "Monitoring belum punya "
            "tujuan notifikasi."
        )


    # MONITOR LANGSUNG START.
    # Tidak perlu menekan ON.

    monitor_task = asyncio.create_task(
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
    global monitor_task


    if monitor_task:

        monitor_task.cancel()


        try:

            await monitor_task

        except asyncio.CancelledError:

            pass


        monitor_task = None


    if http_client:

        await http_client.aclose()

        http_client = None


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    if not TELEGRAM_BOT_TOKEN:

        raise SystemExit(
            "ERROR: "
            "TELEGRAM_BOT_TOKEN "
            "belum diset."
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
            "chatid",
            chat_id_command,
        )
    )


    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_menu,
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
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
