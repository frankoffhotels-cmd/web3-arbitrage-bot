import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from dotenv import load_dotenv

import dex_arbitrage
from arbitrage_engine import (
    SYMBOLS,
    active_arbitrages,
    calculate_current_pnl,
    check_and_close_arbitrages,
    close_exchanges,
    find_arbitrage_opportunities,
    init_exchanges,
    open_arbitrage,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

STARTUP_CHAT_ID = int(TELEGRAM_CHAT_ID)
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="▶️ СТАРТ АРБИТРАЖА")],
        [KeyboardButton(text="🌐 DEX АРБИТРАЖ (Flash Loans)")],
        [KeyboardButton(text="⏹ СТОП")],
        [KeyboardButton(text="📊 Статистика")],
    ],
    resize_keyboard=True,
)

is_running = False
is_dex_running = False
tasks: list[asyncio.Task] = []
dex_task: asyncio.Task | None = None
exchanges = {}
closed_trades_history = []
pnl_message_id = None


async def on_startup(bot: Bot) -> None:
    try:
        await bot.send_message(
            chat_id=STARTUP_CHAT_ID,
            text="Привет, бот запущен, готов работать!",
        )
    except TelegramAPIError as e:
        logging.error("Unable to send startup notification to configured chat: %s", e)


dp.startup.register(on_startup)


@dp.message(Command("start"))
async def cmd_start(message):
    await message.answer(
        "Привет, бот запущен, готов работать!",
        reply_markup=keyboard,
    )


@dp.message(F.text == "▶️ СТАРТ АРБИТРАЖА")
async def start_arbitrage(message):
    global is_running, tasks, exchanges

    if is_running:
        await message.answer("Уже работает.")
        return

    try:
        exchanges = await init_exchanges()
    except Exception as e:
        logging.exception("Failed to initialize exchanges")
        await message.answer(f"Не удалось подключиться к биржам: {e}")
        return

    is_running = True
    tasks = [
        asyncio.create_task(scanner_loop()),
        asyncio.create_task(monitor_loop()),
        asyncio.create_task(live_pnl_loop()),
    ]

    await message.answer("Арбитраж запущен!")


@dp.message(F.text == "🌐 DEX АРБИТРАЖ (Flash Loans)")
async def start_dex_arbitrage(message):
    global is_dex_running, dex_task

    if is_dex_running:
        await message.answer("Уже работает.")
        return

    is_dex_running = True
    dex_task = asyncio.create_task(dex_scanner_loop())
    await message.answer("DEX сканер запущен.")


@dp.message(F.text == "⏹ СТОП")
async def stop_arbitrage(message):
    global is_running, is_dex_running, tasks, exchanges, dex_task

    stopped_dex = False
    if is_dex_running and dex_task is not None:
        dex_task.cancel()
        is_dex_running = False
        dex_task = None
        stopped_dex = True
        await message.answer("DEX сканер остановлен.")

    if not is_running:
        if not stopped_dex:
            await message.answer("Уже остановлено.")
        return

    is_running = False

    running_tasks = tasks
    tasks = []
    for task in running_tasks:
        task.cancel()
    if running_tasks:
        await asyncio.gather(*running_tasks, return_exceptions=True)

    if exchanges:
        try:
            await close_exchanges(exchanges)
        except Exception:
            logging.exception("Failed to close exchange connections")
        finally:
            exchanges = {}

    await message.answer("Арбитраж остановлен.")


@dp.message(F.text == "📊 Статистика")
async def show_stats(message):
    total_trades = len(closed_trades_history)
    wins = sum(1 for trade in closed_trades_history if trade["pnl_usd"] > 0)
    losses = sum(1 for trade in closed_trades_history if trade["pnl_usd"] < 0)
    total_pnl = sum(trade["pnl_usd"] for trade in closed_trades_history)
    winrate = wins / total_trades * 100 if total_trades else 0

    await message.answer(
        "📊 СТАТИСТИКА АРБИТРАЖА:\n"
        f"Сделок: {total_trades}\n"
        f"Прибыльных: {wins} | Убыточных: {losses}\n"
        f"Винрейт: {winrate:.2f}%\n"
        f"Общий PnL: {total_pnl:+.2f}$"
    )


async def scanner_loop():
    while is_running:
        opportunities = []
        try:
            opportunities = await find_arbitrage_opportunities(exchanges)

            for opp in opportunities:
                symbol = opp["symbol"]
                buy_exch = opp["buy_exchange"]
                sell_exch = opp["sell_exchange"]

                already_open = any(
                    trade["symbol"] == symbol
                    and trade["buy_exchange"] == buy_exch
                    and trade["sell_exchange"] == sell_exch
                    for trade in active_arbitrages.values()
                )

                if not already_open:
                    open_arbitrage(opp)
                    msg_text = (
                        f"📡 ОТКРЫТА АРБИТРАЖНАЯ ПАРА: {opp['symbol']}\n"
                        f"📈 Купили на {opp['buy_exchange']} по {opp['buy_price']}\n"
                        f"📉 Продали на {opp['sell_exchange']} по {opp['sell_price']}\n"
                        f"📊 Спред: {opp['spread_pct']}%"
                    )
                    try:
                        await bot.send_message(chat_id=STARTUP_CHAT_ID, text=msg_text)
                    except Exception as e:
                        logging.error("Failed to send message: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error("Scanner loop error: %s", e)

        print(
            f"[SCANNER] Проверено {len(SYMBOLS)} пар на {len(exchanges)} биржах. "
            f"Найдено вилок: {len(opportunities)}"
        )
        await asyncio.sleep(10)


async def dex_scanner_loop():
    while is_dex_running:
        try:
            result = await dex_arbitrage.check_dex_prices()
            spread = result.get("spread_pct", 0.0)

            if 1.0 < spread < 5.0:
                message_text = (
                    "*🌐 DEX ARBITRAGE SIGNAL*\n\n"
                    f"Пара: `{result.get('symbol', 'WETH/USDC')}`\n"
                    f"Спред: *{spread:.4f}%*\n"
                    f"🟢 Купить на: *{result.get('buy_dex')}* — {result.get('buy_price')} USDC\n"
                    f"🔴 Продать на: *{result.get('sell_dex')}* — {result.get('sell_price')} USDC\n\n"
                    "⚠️ *Внимание:* проверяйте ликвидность пула!"
                )
                try:
                    await bot.send_message(
                        chat_id=STARTUP_CHAT_ID,
                        text=message_text,
                        parse_mode="Markdown",
                    )
                except TelegramAPIError as exc:
                    logging.warning("Failed to send DEX arbitrage signal: %s", exc)

                if result.get("buy_dex") and result.get("sell_dex"):
                    await dex_arbitrage.execute_flash_loan_arbitrage(
                        symbol=result.get("symbol", "WETH/USDC"),
                        buy_dex=result["buy_dex"],
                        sell_dex=result["sell_dex"],
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logging.exception("DEX scanner loop error: %s", exc)

        await asyncio.sleep(15)


async def monitor_loop():
    while is_running:
        try:
            closed_trades = await check_and_close_arbitrages(exchanges)

            for trade in closed_trades:
                closed_trades_history.append(trade)
                msg_text = (
                    f"💰 АРБИТРАЖ ЗАКРЫТ: {trade['symbol']}\n"
                    f"Вход: {trade['entry_spread']}% → Выход: {trade['exit_spread']}%\n"
                    f"💵 PnL: {trade['pnl_usd']:+.2f}$ ({trade['pnl_pct']:+.2f}%)"
                )
                try:
                    await bot.send_message(chat_id=STARTUP_CHAT_ID, text=msg_text)
                except Exception as e:
                    logging.error("Failed to send message: %s", e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error("Monitor loop error: %s", e)

        print(f"[MONITOR] Активных сделок: {len(active_arbitrages)}")
        await asyncio.sleep(3)


async def live_pnl_loop():
    global pnl_message_id

    while is_running:
        try:
            if not active_arbitrages:
                if pnl_message_id is not None:
                    try:
                        await bot.edit_message_text(
                            chat_id=STARTUP_CHAT_ID,
                            message_id=pnl_message_id,
                            text="📊 Нет открытых пар. Жду сигналов...",
                        )
                    except TelegramBadRequest as e:
                        if "message is not modified" not in str(e).lower():
                            logging.warning("Unable to edit empty live PnL message: %s", e)
                            pnl_message_id = None
                await asyncio.sleep(3)
                continue

            current_pnl = await calculate_current_pnl(exchanges)
            if not current_pnl:
                await asyncio.sleep(3)
                continue

            lines = [
                f"{item['symbol']}: Спред {item['current_spread']:.2f}% | "
                f"PnL: {item['pnl_usd']:+.2f}$ ({item['pnl_pct']:+.2f}%)"
                for item in current_pnl
            ]
            text = "📊 ТЕКУЩИЙ PnL (Live):\n\n" + "\n".join(lines)

            if pnl_message_id is None:
                message = await bot.send_message(chat_id=STARTUP_CHAT_ID, text=text)
                pnl_message_id = message.message_id
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=STARTUP_CHAT_ID,
                        message_id=pnl_message_id,
                        text=text,
                    )
                except TelegramBadRequest as e:
                    if "message is not modified" in str(e).lower():
                        pass
                    else:
                        logging.warning("Unable to edit live PnL message; will send a new one: %s", e)
                        pnl_message_id = None
        except asyncio.CancelledError:
            break
        except TelegramAPIError as e:
            logging.error("Telegram error in live PnL loop: %s", e)
        except Exception as e:
            logging.exception("Live PnL loop error: %s", e)

        await asyncio.sleep(3)


async def main():
    global is_running, is_dex_running, exchanges, tasks, dex_task

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        is_running = False
        is_dex_running = False

        if dex_task is not None:
            dex_task.cancel()
            dex_task = None

        running_tasks = tasks
        tasks = []
        for task in running_tasks:
            task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        try:
            if exchanges:
                await close_exchanges(exchanges)
        finally:
            exchanges = {}
            await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
