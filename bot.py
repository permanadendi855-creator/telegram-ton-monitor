import os
import asyncio
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
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

TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "",
).strip()

API_KEY = os.getenv(
    "TONCENTER_API_KEY",
    "",
).strip()

try:
    POLL_SECONDS = max(
        10,
        int(os.getenv("POLL_SECONDS", "20")),
    )
except (TypeError, ValueError):
    POLL_SECONDS = 20

TIMEZONE_NAME = os.getenv(
    "TIMEZONE",
    "Asia/Jakarta",
).strip()

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TIMEZONE_NAME = "Asia/Jakarta"
    TZ = ZoneInfo(TIMEZONE_NAME)

# Support CHAT_ID dan CHAT_IDS
CHAT_ID = os.getenv(
    "CHAT_ID",
    "",
).strip()

CHAT_IDS_ENV = os.getenv(
    "CHAT_IDS",
    "",
).strip()

CHAT_IDS: set[str] = set()

if CHAT_ID:
    CHAT_IDS.add(CHAT_ID)

if CHAT_IDS_ENV:
    CHAT_IDS.update(
        x.strip()
        for x in CHAT_IDS_ENV.split(",")
        if x.strip()
    )


# ============================================================
# GLOBAL STATE
# ============================================================

monitor_users: set[str] = set()
seen_events: set[str] = set()
baseline_ready = False

client: httpx.AsyncClient | None = None


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(
    "telegram-ton-monitor"
)


# ============================================================
# HELPERS
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

        if isinstance(value, str):

            value = value.strip()

            if not value:
                return default

        return int(value)

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):

        return default


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


def esc(text):

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def short(addr):

    if not addr:
        return "-"

    addr = str(addr)

    if len(addr) < 18:
        return addr

    return addr[:9] + "..." + addr[-8:]


def fmt_time(ts):

    timestamp = safe_int(ts, 0)

    if timestamp <= 0:
        return "-"

    try:

        dt = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).astimezone(TZ)

        return dt.strftime(
            "%d/%m/%Y %H:%M:%S"
        )

    except (
        TypeError,
        ValueError,
        OverflowError,
    ):

        return "-"


def amount(raw, decimals_count):

    try:

        if raw is None:
            raw = "0"

        value = (
            Decimal(str(raw))
            / (Decimal(10) ** decimals_count)
        )

        text = (
            format(value, "f")
            .rstrip("0")
            .rstrip(".")
        )

        return text if text else "0"

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return "0"


# ============================================================
# API
# ============================================================

async def api(
    path,
    params=None,
):

    if client is None:
        raise RuntimeError(
            "HTTP client belum siap."
        )

    headers = {}

    if API_KEY:
        headers["X-API-Key"] = API_KEY

    response = await client.get(
        TONCENTER_BASE + path,
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

    return data


# ============================================================
# WALLET DATA
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
        [],
    )

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
            "sort": "desc",
        },
    )


async def ton_transactions():

    return await api(
        "/transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": 50,
            "sort": "desc",
        },
    )


async def jetton_transfers():

    return await api(
        "/jetton/transfers",
        {
            "owner_address": WALLET_ADDRESS,
            "limit": 100,
            "sort": "desc",
        },
    )


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
# TOKEN METADATA
# ============================================================

def metadata(
    response,
    master,
):

    if not isinstance(response, dict):
        return {}

    data = response.get(
        "metadata",
        {},
    )

    if not isinstance(data, dict):
        return {}

    if master in data:

        value = data.get(master)

        if isinstance(value, dict):

            info = value.get(
                "token_info",
                [],
            )

            if info and isinstance(info[0], dict):
                return info[0]

    return {}


def decimals(
    info,
    master,
):

    if master == USDT_JETTON_MASTER:
        return 6

    if not isinstance(info, dict):
        return 9

    extra = info.get(
        "extra",
        {},
    )

    if isinstance(extra, dict):

        for key in (
            "decimals",
            "decimal",
        ):

            if key in extra:

                try:
                    return int(
                        extra[key]
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    pass

    try:

        return int(
            info.get(
                "decimals",
                9,
            )
        )

    except (
        TypeError,
        ValueError,
    ):

        return 9


# ============================================================
# NORMALIZE JETTON
# ============================================================

def normalize_jetton(
    item,
    response,
    direction=None,
):

    if not isinstance(item, dict):
        return None

    master = str(
        item.get("jetton_master")
        or ""
    )

    info = metadata(
        response,
        master,
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
        master,
    )

    source = str(
        item.get("source")
        or ""
    )

    destination = str(
        item.get("destination")
        or ""
    )

    if direction not in (
        "in",
        "out",
    ):

        if (
            destination
            == WALLET_ADDRESS
        ):

            direction = "in"

        elif (
            source
            == WALLET_ADDRESS
        ):

            direction = "out"

        else:

            direction = "?"

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

    timestamp = safe_int(
        item.get(
            "transaction_now"
        ),
        safe_int(
            item.get("now"),
            0,
        ),
    )

    event_id = (
        "J:"
        + transaction_hash
        + ":"
        + transaction_lt
        + ":"
        + direction
        + ":"
        + master
    )

    return {
        "id": event_id,
        "kind": "JETTON",
        "direction": direction,
        "symbol": symbol,
        "name": name,
        "amount": amount(
            item.get(
                "amount",
                "0",
            ),
            dec,
        ),
        "source": source,
        "destination": destination,
        "timestamp": timestamp,
        "hash": transaction_hash,
        "master": master,
        "aborted": bool(
            item.get(
                "transaction_aborted",
                False,
            )
        ),
    }


# ============================================================
# NORMALIZE TON
# ============================================================

def normalize_ton(tx):

    events = []

    if not isinstance(tx, dict):
        return events

    ts = safe_int(
        tx.get("now"),
        0,
    )

    txhash = str(
        tx.get("hash")
        or ""
    )

    # --------------------------------------------------------
    # INCOMING
    # --------------------------------------------------------

    incoming = (
        tx.get("in_msg")
        or {}
    )

    if not isinstance(
        incoming,
        dict,
    ):
        incoming = {}

    source = str(
        incoming.get("source")
        or ""
    )

    value = safe_int(
        incoming.get("value"),
        0,
    )

    if (
        source
        and source != WALLET_ADDRESS
        and value > 0
    ):

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
                        9,
                    ),

                "source":
                    source,

                "destination":
                    WALLET_ADDRESS,

                "timestamp":
                    ts,

                "hash":
                    txhash,

                "aborted":
                    False,
            }
        )

    # --------------------------------------------------------
    # OUTGOING
    # --------------------------------------------------------

    out_msgs = (
        tx.get("out_msgs")
        or []
    )

    if not isinstance(
        out_msgs,
        list,
    ):
        out_msgs = []

    for i, msg in enumerate(
        out_msgs
    ):

        if not isinstance(
            msg,
            dict,
        ):
            continue

        dst = str(
            msg.get("destination")
            or ""
        )

        val = safe_int(
            msg.get("value"),
            0,
        )

        if (
            dst
            and dst != WALLET_ADDRESS
            and val > 0
        ):

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
                            val,
                            9,
                        ),

                    "source":
                        WALLET_ADDRESS,

                    "destination":
                        dst,

                    "timestamp":
                        ts,

                    "hash":
                        txhash,

                    "aborted":
                        False,
                }
            )

    return events


# ============================================================
# RECENT EVENTS
# ============================================================

async def recent_events():

    ton, jettons = await asyncio.gather(
        ton_transactions(),
        jetton_transfers(),
    )

    events = []

    # --------------------------------------------------------
    # TON
    # --------------------------------------------------------

    for tx in ton.get(
        "transactions",
        [],
    ):

        events.extend(
            normalize_ton(tx)
        )

    # --------------------------------------------------------
    # JETTON
    # --------------------------------------------------------

    for item in jettons.get(
        "jetton_transfers",
        [],
    ):

        event = normalize_jetton(
            item,
            jettons,
        )

        if event is None:
            continue

        if event.get("aborted"):
            continue

        if event.get("direction") not in (
            "in",
            "out",
        ):
            continue

        events.append(event)

    # --------------------------------------------------------
    # SORT
    # --------------------------------------------------------

    events.sort(
        key=lambda x: (
            safe_int(
                x.get("timestamp"),
                0,
            ),
            str(
                x.get("id")
                or ""
            ),
        ),
        reverse=True,
    )

    # --------------------------------------------------------
    # UNIQUE
    # --------------------------------------------------------

    unique = []
    ids = set()

    for event in events:

        event_id = event.get(
            "id"
        )

        if not event_id:
            continue

        if event_id in ids:
            continue

        ids.add(event_id)

        unique.append(event)

    return unique


# ============================================================
# NOTIFICATION
# ============================================================

async def send_notification(
    app,
    event,
):

    recipients = (
        set(CHAT_IDS)
        | set(monitor_users)
    )

    if not recipients:
        logger.warning(
            "Tidak ada penerima notifikasi."
        )
        return

    direction = event.get(
        "direction"
    )

    icon = (
        "🟢"
        if direction == "in"
        else "🔴"
    )

    title = (
        "MASUK"
        if direction == "in"
        else "KELUAR"
    )

    sign = (
        "+"
        if direction == "in"
        else "-"
    )

    source = str(
        event.get("source")
        or "-"
    )

    destination = str(
        event.get("destination")
        or "-"
    )

    event_amount = str(
        event.get("amount")
        or "0"
    )

    symbol = str(
        event.get("symbol")
        or "TON"
    )

    timestamp = fmt_time(
        event.get("timestamp")
    )

    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"
        f"{icon} <b>{title}</b>\n\n"
        "💰 Jumlah:\n"
        f"<b>{sign}"
        f"{esc(event_amount)} "
        f"{esc(symbol)}</b>\n\n"
        "📤 Pengirim:\n"
        f"<code>{esc(source)}</code>\n\n"
        "📥 Penerima:\n"
        f"<code>{esc(destination)}</code>\n\n"
        f"📅 {esc(timestamp)} "
        f"{esc(TIMEZONE_NAME)}"
    )

    if event.get("kind") == "JETTON":

        master = str(
            event.get("master")
            or "-"
        )

        text += (
            "\n\n"
            "🪙 Jetton Master:\n"
            f"<code>{esc(master)}</code>"
        )

    tx_hash = str(
        event.get("hash")
        or ""
    )

    if tx_hash:

        text += (
            "\n\n"
            f'🔗 <a href="'
            f"https://tonviewer.com/"
            f"{esc(tx_hash)}"
            f'">Lihat transaksi</a>'
        )

    for cid in sorted(
        recipients
    ):

        try:

            await app.bot.send_message(
                chat_id=cid,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

            logger.info(
                "Notifikasi transaksi dikirim ke %s",
                cid,
            )

        except Exception as e:

            logger.warning(
                "Gagal mengirim notifikasi ke %s: %s",
                cid,
                e,
            )

            if cid not in CHAT_IDS:

                monitor_users.discard(
                    cid
                )


# ============================================================
# MONITOR
# ============================================================

async def monitor(app):

    global baseline_ready

    logger.info(
        "Wallet monitoring aktif."
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
            # BASELINE
            # ------------------------------------------------

            if not baseline_ready:

                seen_events.update(
                    ids
                )

                baseline_ready = True

                logger.info(
                    "Baseline dibuat: %d event.",
                    len(ids),
                )

            # ------------------------------------------------
            # NEW EVENTS
            # ------------------------------------------------

            else:

                fresh = []

                for event in events:

                    event_id = event.get(
                        "id"
                    )

                    if not event_id:
                        continue

                    if event_id not in seen_events:

                        fresh.append(
                            event
                        )

                for event in fresh:

                    seen_events.add(
                        event["id"]
                    )

                # Kirim dari yang paling lama
                # ke yang paling baru

                for event in reversed(
                    fresh
                ):

                    await send_notification(
                        app,
                        event,
                    )

                # ------------------------------------------------
                # PREVENT MEMORY GROWTH
                # ------------------------------------------------

                if len(seen_events) > 5000:

                    seen_events.clear()

                    seen_events.update(
                        ids
                    )

        except asyncio.CancelledError:

            raise

        except Exception as e:

            logger.exception(
                "Error monitoring wallet: %s",
                e,
            )

        await asyncio.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

async def start(
    update,
    context,
):

    text = (
        "<b>UPDATE WALLET PORTAL</b>\n\n"
        "Wallet:\n\n"
        f"<code>{esc(WALLET_ADDRESS)}</code>\n\n"
        "🟣 TON\n"
        "🪙 Semua Jetton\n"
        "💵 USDT\n\n"
        "Pilih menu di bawah."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
        disable_web_page_preview=True,
    )


# ============================================================
# CHAT ID
# ============================================================

async def chatid(
    update,
    context,
):

    await update.message.reply_text(
        "Chat ID Anda:\n\n"
        f"<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(
    update,
    context,
):

    try:

        state, jets = await asyncio.gather(
            wallet_state(),
            jetton_wallets(),
        )

        ton = amount(
            state.get(
                "balance",
                "0",
            ),
            9,
        )

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"🟣 TON: <b>{esc(ton)} TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        wallets = jets.get(
            "jetton_wallets",
            [],
        )

        if not wallets:

            lines.append(
                "Tidak ada Jetton dengan saldo."
            )

        else:

            for w in wallets:

                master = str(
                    w.get("jetton")
                    or ""
                )

                info = metadata(
                    jets,
                    master,
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
                    w.get(
                        "balance",
                        "0",
                    ),
                    decimals(
                        info,
                        master,
                    ),
                )

                lines.append(
                    f"• <b>{esc(symbol)}</b>: "
                    f"{esc(bal)} "
                    f"({esc(name)})"
                )

        lines.extend(
            [
                "",
                "Wallet:",
                f"<code>{esc(WALLET_ADDRESS)}</code>",
                "",
                f"🕐 {esc(fmt_time(datetime.now().timestamp()))} "
                f"{esc(TIMEZONE_NAME)}",
            ]
        )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
            disable_web_page_preview=True,
        )

    except Exception as e:

        logger.exception(
            "Error membaca saldo."
        )

        await update.message.reply_text(
            "❌ <b>Error membaca saldo</b>\n\n"
            f"<code>{esc(str(e))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# TOKENS
# ============================================================

async def tokens(
    update,
    context,
):

    try:

        jets = await jetton_wallets()

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        wallets = jets.get(
            "jetton_wallets",
            [],
        )

        if not wallets:

            lines.append(
                "Tidak ada Jetton."
            )

        else:

            for i, w in enumerate(
                wallets,
                1,
            ):

                master = str(
                    w.get("jetton")
                    or ""
                )

                info = metadata(
                    jets,
                    master,
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
                    w.get(
                        "balance",
                        "0",
                    ),
                    decimals(
                        info,
                        master,
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
            disable_web_page_preview=True,
        )

    except Exception as e:

        logger.exception(
            "Error token."
        )

        await update.message.reply_text(
            "❌ <b>Error token</b>\n\n"
            f"<code>{esc(str(e))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# TRANSACTIONS
# ============================================================

async def transactions(
    update,
    context,
):

    try:

        events = await recent_events()

        lines = [
            "📜 <b>20 TRANSAKSI TERAKHIR</b>",
            "",
        ]

        if not events:

            lines.append(
                "Belum ada transaksi."
            )

        else:

            for i, event in enumerate(
                events[:20],
                1,
            ):

                direction = event.get(
                    "direction"
                )

                if direction == "in":

                    icon = "🟢"
                    title = "MASUK"
                    addr = (
                        event.get("source")
                        or "-"
                    )
                    label = "Dari"

                else:

                    icon = "🔴"
                    title = "KELUAR"
                    addr = (
                        event.get("destination")
                        or "-"
                    )
                    label = "Ke"

                symbol = str(
                    event.get(
                        "symbol",
                        "TON",
                    )
                    or "TON"
                )

                event_amount = str(
                    event.get(
                        "amount",
                        "0",
                    )
                    or "0"
                )

                lines.append(
                    f"<b>{i}. "
                    f"{icon} {title} "
                    f"{esc(symbol)}</b>\n"
                    f"Jumlah: "
                    f"<b>{esc(event_amount)} "
                    f"{esc(symbol)}</b>\n"
                    f"{label}:\n"
                    f"<code>{esc(addr)}</code>\n"
                    f"🕐 "
                    f"{esc(fmt_time(event.get('timestamp')))} "
                    f"{esc(TIMEZONE_NAME)}\n"
                )

        text = "\n".join(lines)

        # Telegram memiliki batas pesan.
        if len(text) > 3900:

            text = text[:3850]

            text += (
                "\n\n"
                "⚠️ Daftar dipotong karena "
                "batas panjang pesan Telegram."
            )

        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
            disable_web_page_preview=True,
        )

    except Exception as e:

        logger.exception(
            "Error transaksi."
        )

        await update.message.reply_text(
            "❌ <b>Error transaksi</b>\n\n"
            f"<code>{esc(str(e))}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )


# ============================================================
# MONITOR BUTTON
# ============================================================

async def monitor_wallet(
    update,
    context,
):

    cid = str(
        update.effective_chat.id
    )

    if cid in monitor_users:

        monitor_users.remove(
            cid
        )

        status = "OFF 🔴"

    else:

        monitor_users.add(
            cid
        )

        status = "ON 🟢"

    await update.message.reply_text(
        "👁 <b>Memantau Wallet</b>\n\n"
        f"Status: <b>{status}</b>\n\n"
        "Bot memantau:\n"
        "🟣 TON masuk/keluar\n"
        "🪙 Jetton masuk/keluar\n"
        "💵 USDT masuk/keluar\n\n"
        f"⏱ Interval: <b>{POLL_SECONDS} detik</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


# ============================================================
# REFRESH
# ============================================================

async def refresh(
    update,
    context,
):

    await update.message.reply_text(
        "🔄 Menu diperbarui",
        reply_markup=menu(),
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context,
):

    if not update.message:
        return

    t = update.message.text

    if t == "💰 Info Saldo":

        await balance(
            update,
            context,
        )

    elif t == "📜 20 Transaksi":

        await transactions(
            update,
            context,
        )

    elif t == "🪙 Token Dimiliki":

        await tokens(
            update,
            context,
        )

    elif t == "👁 Memantau Wallet":

        await monitor_wallet(
            update,
            context,
        )

    elif t == "🔄 Refresh":

        await refresh(
            update,
            context,
        )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    app,
):

    global client

    if not TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset."
        )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            20.0,
            connect=10.0,
        )
    )

    if not API_KEY:

        logger.warning(
            "TONCENTER_API_KEY belum diset."
        )

    logger.info(
        "CHAT_ID aktif: %s",
        sorted(CHAT_IDS),
    )

    # --------------------------------------------------------
    # START MONITOR
    # --------------------------------------------------------

    app.create_task(
        monitor(app)
    )

    # --------------------------------------------------------
    # STARTUP NOTIFICATION
    # --------------------------------------------------------

    if CHAT_IDS:

        startup_text = (
            "🟢 <b>UPDATE WALLET PORTAL AKTIF</b>\n\n"
            "Bot berhasil dijalankan.\n\n"
            "🟣 Wallet:\n"
            f"<code>{esc(WALLET_ADDRESS)}</code>\n\n"
            f"⏱ Interval: <b>{POLL_SECONDS} detik</b>\n"
            f"🌏 Timezone: <b>{esc(TIMEZONE_NAME)}</b>\n\n"
            "👁 Monitoring transaksi aktif."
        )

        for cid in sorted(
            CHAT_IDS
        ):

            try:

                await app.bot.send_message(
                    chat_id=cid,
                    text=startup_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

                logger.info(
                    "Startup notification dikirim ke %s",
                    cid,
                )

            except Exception as e:

                logger.warning(
                    "Gagal startup notification ke %s: %s",
                    cid,
                    e,
                )

    else:

        logger.warning(
            "CHAT_ID / CHAT_IDS belum diset. "
            "Notifikasi otomatis tidak memiliki penerima."
        )


# ============================================================
# SHUTDOWN
# ============================================================

async def post_shutdown(
    app,
):

    global client

    if client:

        await client.aclose()

        client = None


# ============================================================
# MAIN
# ============================================================

def main():

    if not TOKEN:

        raise SystemExit(
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
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "menu",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "chatid",
            chatid,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info(
        "TON Wallet Monitor started."
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
