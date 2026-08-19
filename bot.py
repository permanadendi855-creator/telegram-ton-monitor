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

WALLET_ADDRESS = "UQDSmBRtE-828x5LmsWN7r-aIpfjYEJzCBI2OIiyNunwACT5"
USDT_JETTON_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"

TONCENTER_BASE = os.getenv(
    "TONCENTER_BASE",
    "https://toncenter.com/api/v3",
).rstrip("/")

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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-ton-monitor")

monitor_chats: set[str] = set(AUTO_MONITOR_CHAT_IDS)
seen_event_ids: set[str] = set()
baseline_ready = False
http_client: httpx.AsyncClient | None = None


def format_decimal(value: Decimal, max_places: int = 8) -> str:
    q = value.quantize(Decimal("1." + "0" * max_places))
    text = format(q, "f").rstrip("0").rstrip(".")
    return text if text else "0"


def format_amount(raw: str | int | None, decimals: int) -> str:
    try:
        value = Decimal(str(raw or "0")) / (Decimal(10) ** decimals)
        return format_decimal(value, min(decimals, 8))
    except (InvalidOperation, ValueError):
        return str(raw or "0")


def format_ton(raw: str | int | None) -> str:
    return format_amount(raw, 9)


def fmt_time(timestamp: int | float | None) -> str:
    if not timestamp:
        return "-"
    dt = datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc,
    ).astimezone(LOCAL_TZ)
    return dt.strftime("%d/%m/%Y %H:%M:%S")


def html_escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def explorer_url(value: str) -> str:
    return f"https://tonviewer.com/{value}"


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


def monitor_markup(chat_id: str) -> InlineKeyboardMarkup:
    status = "🟢 ON" if chat_id in monitor_chats else "⚪ OFF"

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


async def api_get(
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global http_client

    if http_client is None:
        raise RuntimeError("HTTP client belum siap")

    headers: dict[str, str] = {}

    if TONCENTER_API_KEY:
        headers["X-API-Key"] = TONCENTER_API_KEY

    response = await http_client.get(
        f"{TONCENTER_BASE}/{path.lstrip('/')}",
        params=params or {},
        headers=headers,
    )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"]))

    return data


async def get_ton_transactions(
    limit: int = 100,
) -> list[dict[str, Any]]:
    data = await api_get(
        "transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": min(limit, 1000),
            "sort": "desc",
        },
    )

    return data.get("transactions", [])


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

    states = data.get("account_states", [])

    return states[0] if states else {}


async def get_jetton_transfers(
    jetton_master: str,
    direction: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "owner_address": WALLET_ADDRESS,
        "jetton_master": jetton_master,
        "limit": min(limit, 1000),
        "sort": "desc",
    }

    if direction in {"in", "out"}:
        params["direction"] = direction

    data = await api_get(
        "jetton/transfers",
        params,
    )

    return data.get("jetton_transfers", [])


def token_info_from_response(
    response: dict[str, Any],
    jetton_address: str,
) -> dict[str, Any]:
    metadata = response.get("metadata", {})

    for key in (
        jetton_address,
        jetton_address.replace("-", "_"),
    ):
        if key in metadata:
            info = metadata[key].get("token_info", [])

            if info:
                return info[0]

    for key, value in metadata.items():
        if str(key) == str(jetton_address):
            info = value.get("token_info", [])

            if info:
                return info[0]

    return {}


def token_decimals(
    info: dict[str, Any],
    default: int = 9,
) -> int:
    raw = info.get("decimals")

    if raw is None:
        raw = (info.get("extra") or {}).get("decimals")

    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def normalize_ton_events(
    tx: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    now = int(tx.get("now") or 0)
    tx_hash = str(tx.get("hash") or "")
    lt = str(tx.get("lt") or "")

    in_msg = tx.get("in_msg") or {}

    source = in_msg.get("source") or ""
    value = int(in_msg.get("value") or 0)

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
                "event_id": f"ton-in:{tx_hash}",
            }
        )

    for index, msg in enumerate(
        tx.get("out_msgs") or []
    ):
        destination = msg.get("destination") or ""
        msg_value = int(msg.get("value") or 0)

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
                    "amount": format_ton(msg_value),
                    "source": WALLET_ADDRESS,
                    "destination": destination,
                    "counterparty": destination,
                    "timestamp": now,
                    "lt": lt,
                    "hash": tx_hash,
                    "event_id": f"ton-out:{tx_hash}:{index}",
                }
            )

    return events


def normalize_usdt_event(
    item: dict[str, Any],
    direction: str,
) -> dict[str, Any]:
    source = item.get("source") or ""
    destination = item.get("destination") or ""

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
        "amount": format_amount(
            item.get("amount", "0"),
            6,
        ),
        "source": source,
        "destination": destination,
        "counterparty": counterparty,
        "timestamp": int(
            item.get("transaction_now") or 0
        ),
        "lt": str(
            item.get("transaction_lt") or ""
        ),
        "hash": str(
            item.get("transaction_hash") or ""
        ),
        "trace_id": str(
            item.get("trace_id") or ""
        ),
        "aborted": bool(
            item.get("transaction_aborted")
        ),
        "event_id": (
            f"usdt:"
            f"{item.get('transaction_hash', '')}:"
            f"{item.get('transaction_lt', '')}:"
            f"{direction}"
        ),
    }


async def get_recent_ton_events(
    limit: int = 10,
) -> list[dict[str, Any]]:
    transactions = await get_ton_transactions(100)

    events: list[dict[str, Any]] = []

    for tx in transactions:
        events.extend(
            normalize_ton_events(tx)
        )

    events = [
        event
        for event in events
        if not event.get("aborted")
    ]

    events.sort(
        key=lambda event: (
            int(event.get("timestamp") or 0),
            (
                int(event.get("lt") or 0)
                if str(event.get("lt") or "").isdigit()
                else 0
            ),
        ),
        reverse=True,
    )

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()

    for event in events:
        event_id = str(
            event.get("event_id") or ""
        )

        if event_id and event_id in seen:
            continue

        if event_id:
            seen.add(event_id)

        unique.append(event)

        if len(unique) >= limit:
            break

    return unique


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
        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event["aborted"]:
            events.append(event)

    for item in outgoing:
        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event["aborted"]:
            events.append(event)

    events.sort(
        key=lambda event: (
            int(event.get("timestamp") or 0),
            (
                int(event.get("lt") or 0)
                if str(event.get("lt") or "").isdigit()
                else 0
            ),
        ),
        reverse=True,
    )

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()

    for event in events:
        event_id = str(
            event.get("event_id") or ""
        )

        if event_id and event_id in seen:
            continue

        if event_id:
            seen.add(event_id)

        unique.append(event)

        if len(unique) >= limit:
            break

    return unique


def format_history_event(
    event: dict[str, Any],
    number: int,
) -> str:
    direction = event.get("direction")

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
        event.get("timestamp")
    )

    return (
        f"{number}. {icon} "
        f"<b>{label} {html_escape(symbol)}</b>\n"
        f"Jumlah: {sign}"
        f"{html_escape(amount)} "
        f"{html_escape(symbol)}\n"
        f"{address_label}:\n"
        f"<code>{html_escape(counterparty)}</code>\n"
        f"🕐 {html_escape(timestamp)} "
        f"{html_escape(TIMEZONE_NAME)}"
    )


async def show_ton_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    await query.answer(
        "Mengambil 10 transaksi TON..."
    )

    try:
        events = await get_recent_ton_events(10)

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
            f"<code>{html_escape(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


async def show_usdt_transactions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    await query.answer(
        "Mengambil 10 transaksi USDT..."
    )

    try:
        events = await get_recent_usdt_events(10)

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
            f"<code>{html_escape(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


async def get_monitor_events() -> list[dict[str, Any]]:
    ton_txs, usdt_in, usdt_out = await asyncio.gather(
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
    )

    events: list[dict[str, Any]] = []

    for tx in ton_txs:
        events.extend(
            normalize_ton_events(tx)
        )

    for item in usdt_in:
        event = normalize_usdt_event(
            item,
            "in",
        )

        if not event["aborted"]:
            events.append(event)

    for item in usdt_out:
        event = normalize_usdt_event(
            item,
            "out",
        )

        if not event["aborted"]:
            events.append(event)

    events.sort(
        key=lambda event: (
            int(event.get("timestamp") or 0),
            (
                int(event.get("lt") or 0)
                if str(event.get("lt") or "").isdigit()
                else 0
            ),
        ),
        reverse=True,
    )

    return events


async def send_event_notification(
    application: Application,
    event: dict[str, Any],
) -> None:
    if not monitor_chats:
        return

    direction = event.get("direction")

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

    source = event.get(
        "source"
    ) or "-"

    destination = event.get(
        "destination"
    ) or "-"

    amount = event.get(
        "amount"
    ) or "0"

    timestamp = fmt_time(
        event.get("timestamp")
    )

    tx_hash = event.get(
        "hash"
    ) or ""

    text = (
        "🚨 <b>TRANSAKSI BARU</b>\n\n"
        f"{icon} <b>{label}</b>\n\n"
        f"💰 Jumlah: <b>{sign}"
        f"{html_escape(amount)} "
        f"{html_escape(symbol)}</b>\n\n"
        f"📤 Pengirim:\n"
        f"<code>{html_escape(source)}</code>\n\n"
        f"📥 Penerima:\n"
        f"<code>{html_escape(destination)}</code>\n\n"
        f"📅 {html_escape(timestamp)} "
        f"{html_escape(TIMEZONE_NAME)}\n"
    )

    if tx_hash:
        text += (
            f'\n🔗 <a href="{explorer_url(tx_hash)}">'
            "Lihat transaksi di Tonviewer</a>"
        )

    failed: list[str] = []

    for chat_id in list(monitor_chats):
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
            failed.append(chat_id)

    for chat_id in failed:
        monitor_chats.discard(chat_id)


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
            events = await get_monitor_events()

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
                new_events = [
                    event
                    for event in events
                    if event.get("event_id")
                    and event["event_id"]
                    not in seen_event_ids
                    and not event.get("aborted")
                ]

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

                if len(seen_event_ids) > 5000:
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


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    text = (
        "👋 <b>TON Wallet Monitor</b>\n\n"
        "Wallet yang dipantau:\n"
        f"<code>{html_escape(WALLET_ADDRESS)}</code>\n\n"
        "🟣 TON + 🪙 USDT dipantau.\n\n"
        "Pilih menu di bawah:"
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=menu_markup(),
        disable_web_page_preview=True,
    )


async def chat_id_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        f"Chat ID Anda:\n"
        f"<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def show_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    await query.answer(
        "Mengambil saldo..."
    )

    try:
        state, wallets_response = await asyncio.gather(
            get_account_state(),
            get_jetton_wallets(100),
        )

        ton_balance = format_ton(
            state.get("balance", "0")
        )

        wallets = wallets_response.get(
            "jetton_wallets",
            [],
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
                "Tidak ada Jetton dengan saldo &gt; 0."
            )

        else:
            for wallet in wallets[:30]:
                master = wallet.get(
                    "jetton"
                ) or ""

                info = token_info_from_response(
                    wallets_response,
                    master,
                )

                symbol = (
                    info.get("symbol")
                    or (
                        "USDT"
                        if master == USDT_JETTON_MASTER
                        else "JETTON"
                    )
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
                    f"</b> — "
                    f"{html_escape(balance)}\n"
                    f"  {html_escape(name)}"
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
            f"<code>{html_escape(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
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
                "Tidak ada Jetton dengan saldo &gt; 0."
            )

        else:
            for i, wallet in enumerate(
                wallets[:50],
                1,
            ):
                master = wallet.get(
                    "jetton"
                ) or ""

                info = token_info_from_response(
                    response,
                    master,
                )

                symbol = (
                    info.get("symbol")
                    or (
                        "USDT"
                        if master == USDT_JETTON_MASTER
                        else "JETTON"
                    )
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
                    f"<b>{i}. "
                    f"{html_escape(symbol)}</b> — "
                    f"{html_escape(balance)}\n"
                    f"   {html_escape(name)}\n"
                    f"   Master: "
                    f"<code>{html_escape(master)}</code>"
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
            f"<code>{html_escape(exc)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_markup(),
        )


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
        monitor_chats.remove(chat_id)

        status_text = (
            "⚪ Notifikasi otomatis "
            "<b>DIMATIKAN</b>."
        )

    else:
        monitor_chats.add(chat_id)

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


async def post_init(
    application: Application,
) -> None:
    global http_client

    timeout = httpx.Timeout(
        20.0,
        connect=10.0,
    )

    http_client = httpx.AsyncClient(
        timeout=timeout
    )

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN belum diset. "
            "Tambahkan di Railway Variables."
        )

    if not TONCENTER_API_KEY:
        logger.warning(
            "TONCENTER_API_KEY belum diset; "
            "request API v3 akan terkena "
            "rate limit publik."
        )

    application.bot_data[
        "monitor_task"
    ] = asyncio.create_task(
        monitor_loop(application)
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


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "ERROR: TELEGRAM_BOT_TOKEN "
            "belum diset sebagai environment variable."
        )

    application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
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
```0
