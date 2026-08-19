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
    Application,
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

CHAT_IDS = {
    x.strip()
    for x in os.getenv("CHAT_IDS", "").split(",")
    if x.strip()
}

monitor_users = set()
seen_events = set()
baseline_ready = False
client = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


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

    if len(addr) < 18:
        return addr

    return addr[:9] + "..." + addr[-8:]


def fmt_time(ts):

    if not ts:
        return "-"

    dt = datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).astimezone(TZ)

    return dt.strftime("%d/%m/%Y %H:%M:%S")


def amount(raw, decimals):

    try:

        value = Decimal(str(raw)) / (Decimal(10) ** decimals)

        text = format(value, "f").rstrip("0").rstrip(".")

        return text if text else "0"

    except InvalidOperation:

        return "0"


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


async def wallet_state():

    data = await api(
        "/walletStates",
        {"address": WALLET_ADDRESS},
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


async def ton_transactions():

    return await api(
        "/transactions",
        {
            "account": WALLET_ADDRESS,
            "limit": 50,
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


def metadata(response, master):

    data = response.get("metadata", {})

    if master in data:

        info = data[master].get("token_info", [])

        if info:
            return info[0]

    for value in data.values():

        info = value.get("token_info", [])

        if info:

            return info[0]

    return {}


def decimals(info, master):

    if master == USDT_JETTON_MASTER:
        return 6

    extra = info.get("extra", {})

    for key in ["decimals", "decimal"]:

        if key in extra:

            try:
                return int(extra[key])
            except:
                pass

    try:
        return int(info.get("decimals", 9))
    except:
        return 9


def normalize_jetton(item, response, direction):

    master = item.get("jetton_master", "")

    info = metadata(response, master)

    symbol = info.get("symbol") or "JETTON"

    if master == USDT_JETTON_MASTER:
        symbol = "USDT"

    name = info.get("name") or symbol

    dec = decimals(info, master)

    source = item.get("source", "")
    destination = item.get("destination", "")

    return {
        "id": "J:" + item.get("transaction_hash", "") + ":" + direction + ":" + master,
        "kind": "JETTON",
        "direction": direction,
        "symbol": symbol,
        "name": name,
        "amount": amount(item.get("amount", "0"), dec),
        "source": source,
        "destination": destination,
        "timestamp": int(item.get("transaction_now", 0)),
        "hash": item.get("transaction_hash", ""),
        "master": master,
        "aborted": item.get("transaction_aborted", False),
    }


def normalize_ton(tx):

    events = []

    ts = int(tx.get("now", 0))
    txhash = tx.get("hash", "")

    incoming = tx.get("in_msg") or {}

    source = incoming.get("source", "")
    value = int(incoming.get("value", 0))

    if source and value > 0:

        events.append({
            "id": "TI:" + txhash,
            "kind": "TON",
            "direction": "in",
            "symbol": "TON",
            "amount": amount(value, 9),
            "source": source,
            "destination": WALLET_ADDRESS,
            "timestamp": ts,
            "hash": txhash,
        })

    for i, msg in enumerate(tx.get("out_msgs") or []):

        dst = msg.get("destination", "")
        val = int(msg.get("value", 0))

        if dst and val > 0:

            events.append({
                "id": "TO:" + txhash + ":" + str(i),
                "kind": "TON",
                "direction": "out",
                "symbol": "TON",
                "amount": amount(val, 9),
                "source": WALLET_ADDRESS,
                "destination": dst,
                "timestamp": ts,
                "hash": txhash,
            })

    return events


async def recent_events():

    ton, jin, jout = await asyncio.gather(
        ton_transactions(),
        jetton_in(),
        jetton_out(),
    )

    events = []

    for tx in ton.get("transactions", []):

        events.extend(normalize_ton(tx))

    for item in jin.get("jetton_transfers", []):

        if not item.get("transaction_aborted", False):

            events.append(
                normalize_jetton(item, jin, "in")
            )

    for item in jout.get("jetton_transfers", []):

        if not item.get("transaction_aborted", False):

            events.append(
                normalize_jetton(item, jout, "out")
            )

    events.sort(
        key=lambda x: x["timestamp"],
        reverse=True,
    )

    unique = []
    ids = set()

    for e in events:

        if e["id"] in ids:
            continue

        ids.add(e["id"])
        unique.append(e)

    return unique


async def send_notification(app, event):

    recipients = CHAT_IDS | monitor_users

    if not recipients:
        return

    icon = "🟢" if event["direction"] == "in" else "🔴"

    title = "MASUK" if event["direction"] == "in" else "KELUAR"

    sign = "+" if event["direction"] == "in" else "-"

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

🪙 Jetton Master:
<code>{esc(event["master"])}</code>
"""

    text += f"""

🔗 <a href="https://tonviewer.com/{event["hash"]}">Lihat transaksi</a>
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

            logger.warning(str(e))


async def monitor(app):

    global baseline_ready

    while True:

        try:

            events = await recent_events()

            ids = {x["id"] for x in events}

            if not baseline_ready:

                seen_events.update(ids)

                baseline_ready = True

            else:

                fresh = []

                for e in events:

                    if e["id"] not in seen_events:

                        fresh.append(e)

                for e in fresh:

                    seen_events.add(e["id"])

                for e in reversed(fresh):

                    await send_notification(app, e)

        except Exception as e:

            logger.exception(e)

        await asyncio.sleep(POLL_SECONDS)


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


async def chatid(update, context):

    await update.message.reply_text(
        f"<code>{update.effective_chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def balance(update, context):

    try:

        state, jets = await asyncio.gather(
            wallet_state(),
            jetton_wallets(),
        )

        ton = amount(state.get("balance", "0"), 9)

        lines = [
            "💰 <b>INFO SALDO</b>",
            "",
            f"🟣 TON: <b>{ton} TON</b>",
            "",
            "🪙 <b>JETTON</b>",
        ]

        for w in jets.get("jetton_wallets", []):

            master = w.get("jetton", "")

            info = metadata(jets, master)

            symbol = info.get("symbol") or "JETTON"

            if master == USDT_JETTON_MASTER:
                symbol = "USDT"

            name = info.get("name") or symbol

            bal = amount(
                w.get("balance", "0"),
                decimals(info, master),
            )

            lines.append(
                f"• <b>{esc(symbol)}</b>: {esc(bal)} ({esc(name)})"
            )

        lines.append("")
        lines.append("Wallet:")
        lines.append(f"<code>{WALLET_ADDRESS}</code>")
        lines.append("")
        lines.append(f"🕐 {fmt_time(datetime.now().timestamp())} {TIMEZONE_NAME}")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Error membaca saldo\n\n" + str(e),
            reply_markup=menu(),
        )


async def tokens(update, context):

    try:

        jets = await jetton_wallets()

        lines = [
            "🪙 <b>TOKEN YANG DIMILIKI</b>",
            "",
        ]

        for i, w in enumerate(jets.get("jetton_wallets", []), 1):

            master = w.get("jetton", "")

            info = metadata(jets, master)

            symbol = info.get("symbol") or "JETTON"

            if master == USDT_JETTON_MASTER:
                symbol = "USDT"

            name = info.get("name") or symbol

            bal = amount(
                w.get("balance", "0"),
                decimals(info, master),
            )

            lines.append(
                f"{i}. <b>{esc(symbol)}</b>\n"
                f"Saldo: {esc(bal)}\n"
                f"{esc(name)}\n"
                f"<code>{master}</code>\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Error token\n" + str(e),
            reply_markup=menu(),
        )


async def transactions(update, context):

    try:

        events = await recent_events()

        lines = [
            "📜 <b>20 TRANSAKSI TERAKHIR</b>",
            "",
        ]

        for i, e in enumerate(events[:20], 1):

            icon = "🟢" if e["direction"] == "in" else "🔴"

            title = "MASUK" if e["direction"] == "in" else "KELUAR"

            addr = e["source"] if e["direction"] == "in" else e["destination"]

            lines.append(
                f"<b>{i}. {icon} {title} {esc(e['symbol'])}</b>\n"
                f"Jumlah: {esc(e['amount'])} {esc(e['symbol'])}\n"
                f"{'Dari' if e['direction']=='in' else 'Ke'}:\n"
                f"<code>{esc(addr)}</code>\n"
                f"🕐 {fmt_time(e['timestamp'])}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=menu(),
        )

    except Exception as e:

        await update.message.reply_text(
            "❌ Error transaksi\n" + str(e),
            reply_markup=menu(),
        )


async def monitor_wallet(update, context):

    cid = str(update.effective_chat.id)

    if cid in monitor_users:

        monitor_users.remove(cid)

        status = "OFF 🔴"

    else:

        monitor_users.add(cid)

        status = "ON 🟢"

    await update.message.reply_text(
        f"👁 Memantau Wallet\n\nStatus: <b>{status}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu(),
    )


async def refresh(update, context):

    await update.message.reply_text(
        "🔄 Menu diperbarui",
        reply_markup=menu(),
    )


async def text_handler(update, context):

    t = update.message.text

    if t == "💰 Info Saldo":
        await balance(update, context)

    elif t == "📜 20 Transaksi":
        await transactions(update, context)

    elif t == "🪙 Token Dimiliki":
        await tokens(update, context)

    elif t == "👁 Memantau Wallet":
        await monitor_wallet(update, context)

    elif t == "🔄 Refresh":
        await refresh(update, context)


async def post_init(app):

    global client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0)
    )

    app.create_task(monitor(app))


async def post_shutdown(app):

    global client

    if client:
        await client.aclose()


def main():

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
