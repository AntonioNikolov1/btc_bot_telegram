"""
Bitcoin Price Alert Telegram Bot
----------------------------------
Commands:
  /start            - Welcome message
  /setbuy <price>   - Get notified when BTC drops below this price
  /setsell <price>  - Get notified when BTC rises above this price
  /alerts           - View your current alert settings
  /price            - Check current BTC price
  /clear            - Clear all your alerts
"""

import os
import asyncio
import aiohttp
import aiosqlite
import logging
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHECK_INTERVAL = 60          # seconds between price checks
DB_PATH        = "alerts.db"
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────

async def db_init(app) -> aiosqlite.Connection:
    """Initialize DB table and attach the connection to app state."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            chat_id INTEGER PRIMARY KEY,
            buy     REAL,
            sell    REAL
        )
    """)
    await db.commit()
    app.bot_data["db"] = db
    logger.info("Database initialized and connected.")
    return db


async def db_get(db: aiosqlite.Connection, chat_id: int) -> dict:
    async with db.execute("SELECT buy, sell FROM alerts WHERE chat_id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
    return {"buy": row["buy"], "sell": row["sell"]} if row else {"buy": None, "sell": None}


async def db_set_buy(db: aiosqlite.Connection, chat_id: int, price: float | None):
    if price is None:
        await db.execute("UPDATE alerts SET buy = NULL WHERE chat_id = ?", (chat_id,))
    else:
        # Always include both columns so a new user always gets a clean row
        await db.execute("""
            INSERT INTO alerts (chat_id, buy, sell) VALUES (?, ?, NULL)
            ON CONFLICT(chat_id) DO UPDATE SET buy = excluded.buy
        """, (chat_id, price))
    await db.commit()


async def db_set_sell(db: aiosqlite.Connection, chat_id: int, price: float | None):
    if price is None:
        await db.execute("UPDATE alerts SET sell = NULL WHERE chat_id = ?", (chat_id,))
    else:
        # Always include both columns so a new user always gets a clean row
        await db.execute("""
            INSERT INTO alerts (chat_id, buy, sell) VALUES (?, NULL, ?)
            ON CONFLICT(chat_id) DO UPDATE SET sell = excluded.sell
        """, (chat_id, price))
    await db.commit()


async def db_clear(db: aiosqlite.Connection, chat_id: int):
    await db.execute("DELETE FROM alerts WHERE chat_id = ?", (chat_id,))
    await db.commit()


async def db_get_triggered(db: aiosqlite.Connection, price: float) -> list[aiosqlite.Row]:
    """Return only users whose alert targets have been hit."""
    async with db.execute("""
        SELECT chat_id, buy, sell FROM alerts
        WHERE (buy  IS NOT NULL AND ? < buy)
           OR (sell IS NOT NULL AND ? > sell)
    """, (price, price)) as cursor:
        return await cursor.fetchall()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_btc_price() -> float | None:
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 429:
                    logger.warning("CoinGecko rate limit hit (429). Skipping this cycle.")
                    return None
                if resp.status != 200:
                    logger.warning(f"CoinGecko HTTP error: {resp.status}")
                    return None
                data = await resp.json()
                return float(data["bitcoin"]["usd"])
    except asyncio.TimeoutError:
        logger.error("CoinGecko request timed out.")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch BTC price: {e}")
        return None


def fmt(price: float) -> str:
    return f"${price:,.2f}"


# ── Command Handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Bitcoin Alert Bot*\n\n"
        "I'll notify you when Bitcoin hits your target prices.\n\n"
        "*Commands:*\n"
        "• `/price` — Current BTC price\n"
        "• `/setbuy 60000` — Alert when BTC drops *below* $60,000\n"
        "• `/setsell 80000` — Alert when BTC rises *above* $80,000\n"
        "• `/alerts` — View your active alerts\n"
        "• `/clear` — Remove all your alerts",
        parse_mode="Markdown",
    )


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = await get_btc_price()
    if price is None:
        await update.message.reply_text("⚠️ Could not fetch price right now. Try again shortly.")
        return
    await update.message.reply_text(f"₿ BTC is currently *{fmt(price)}*", parse_mode="Markdown")


async def cmd_setbuy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/setbuy <price>`", parse_mode="Markdown")
        return
    try:
        target = float(ctx.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number. Example: `/setbuy 60000`", parse_mode="Markdown")
        return

    db = ctx.application.bot_data["db"]
    await db_set_buy(db, chat_id, target)
    await update.message.reply_text(
        f"✅ *Buy alert set!*\nI'll notify you when BTC drops below *{fmt(target)}*.",
        parse_mode="Markdown",
    )


async def cmd_setsell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not ctx.args:
        await update.message.reply_text("Usage: `/setsell <price>`", parse_mode="Markdown")
        return
    try:
        target = float(ctx.args[0].replace(",", ""))
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number. Example: `/setsell 80000`", parse_mode="Markdown")
        return

    db = ctx.application.bot_data["db"]
    await db_set_sell(db, chat_id, target)
    await update.message.reply_text(
        f"✅ *Sell alert set!*\nI'll notify you when BTC rises above *{fmt(target)}*.",
        parse_mode="Markdown",
    )


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = ctx.application.bot_data["db"]
    alerts = await db_get(db, chat_id)

    buy_str  = f"🟢 Buy below:  *{fmt(alerts['buy'])}*"  if alerts["buy"]  else "🟢 Buy alert:  _not set_"
    sell_str = f"🔴 Sell above: *{fmt(alerts['sell'])}*" if alerts["sell"] else "🔴 Sell alert: _not set_"

    await update.message.reply_text(
        f"*Your active alerts:*\n\n{buy_str}\n{sell_str}",
        parse_mode="Markdown",
    )


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = ctx.application.bot_data["db"]
    await db_clear(db, chat_id)
    await update.message.reply_text("🗑️ All your alerts have been cleared.")


# ── Background Price Checker ──────────────────────────────────────────────────

async def price_checker(app):
    logger.info("Price checker started.")
    db = app.bot_data["db"]

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        price = await get_btc_price()
        if price is None:
            continue

        logger.info(f"BTC: {fmt(price)}")
        triggered = await db_get_triggered(db, price)

        for row in triggered:
            chat_id = row["chat_id"]

            # Buy alert — price dropped below target
            if row["buy"] and price < row["buy"]:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🚨 *BUY SIGNAL!*\n\n"
                            f"BTC dropped to *{fmt(price)}*\n"
                            f"Below your target of *{fmt(row['buy'])}* 📉\n\n"
                            f"_Alert cleared. Use /setbuy to set a new one._"
                        ),
                        parse_mode="Markdown",
                    )
                    await db_set_buy(db, chat_id, None)
                except Forbidden:
                    logger.info(f"User {chat_id} blocked the bot. Clearing their data.")
                    await db_clear(db, chat_id)
                except Exception as e:
                    logger.error(f"Buy alert error for {chat_id}: {e}")

            # Sell alert — price rose above target
            if row["sell"] and price > row["sell"]:
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🚀 *SELL SIGNAL!*\n\n"
                            f"BTC rose to *{fmt(price)}*\n"
                            f"Above your target of *{fmt(row['sell'])}* 📈\n\n"
                            f"_Alert cleared. Use /setsell to set a new one._"
                        ),
                        parse_mode="Markdown",
                    )
                    await db_set_sell(db, chat_id, None)
                except Forbidden:
                    logger.info(f"User {chat_id} blocked the bot. Clearing their data.")
                    await db_clear(db, chat_id)
                except Exception as e:
                    logger.error(f"Sell alert error for {chat_id}: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def post_init(app):
    await db_init(app)
    asyncio.create_task(price_checker(app))


async def post_shutdown(app):
    db = app.bot_data.get("db")
    if db:
        await db.close()
        logger.info("Database connection closed gracefully.")


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("price",   cmd_price))
    app.add_handler(CommandHandler("setbuy",  cmd_setbuy))
    app.add_handler(CommandHandler("setsell", cmd_setsell))
    app.add_handler(CommandHandler("alerts",  cmd_alerts))
    app.add_handler(CommandHandler("clear",   cmd_clear))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()