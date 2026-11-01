import ccxt.async_support as ccxt
import asyncio
import logging
import math
import uuid
import time
from itertools import permutations

MIN_SPREAD_PCT = 0.25  # Минимальный спред для входа (в процентах, чтобы покрыть комиссии 4-х ног)

EXCHANGES_CONFIG = {
    "bybit": {
        "class": ccxt.bybit,
        "options": {
            "defaultType": "swap",
            "enableRateLimit": True,
        },
    },
    "kucoin": {
        "class": ccxt.kucoin,
        "options": {
            "defaultType": "swap",
            "enableRateLimit": True,
        },
    },
    "bitget": {
        "class": ccxt.bitget,
        "options": {
            "defaultType": "swap",
            "enableRateLimit": True,
        },
    },
}

SYMBOLS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
]


async def init_exchanges() -> dict:
    """Initialize exchange instances and load markets."""
    exchanges = {}
    try:
        for name, config in EXCHANGES_CONFIG.items():
            exchange = config["class"](config["options"])
            exchanges[name] = exchange
            await exchange.load_markets()
    except Exception:
        try:
            await close_exchanges(exchanges)
        except Exception:
            logging.exception("Failed to close exchanges after initialization failed")
        raise
    return exchanges


async def close_exchanges(exchanges: dict):
    """Close all exchange connections."""
    results = await asyncio.gather(
        *(exchange.close() for exchange in exchanges.values()),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        for error in errors:
            logging.error("Failed to close exchange: %s", error)
        raise RuntimeError(f"Failed to close {len(errors)} exchange connection(s)") from errors[0]


async def find_arbitrage_opportunities(exchanges: dict) -> list[dict]:
    """Find arbitrage opportunities across exchanges."""
    opportunities = []

    # Collect all tickers in parallel
    async def fetch_ticker(exchange_name, exchange, symbol):
        try:
            ticker = await exchange.fetch_ticker(symbol)
            if not isinstance(ticker, dict):
                raise ValueError("Ticker response is not an object")

            bid = ticker.get("bid")
            ask = ticker.get("ask")
            if (
                not isinstance(bid, (int, float))
                or not isinstance(ask, (int, float))
                or not math.isfinite(bid)
                or not math.isfinite(ask)
                or bid <= 0
                or ask <= 0
            ):
                raise ValueError("Ticker has invalid bid/ask prices")

            return (exchange_name, symbol, {"bid": bid, "ask": ask})
        except Exception as e:
            logging.warning("Failed to fetch valid ticker from %s for %s: %s", exchange_name, symbol, e)
            return (exchange_name, symbol, None)

    # Fetch all tickers for all symbols from all exchanges
    ticker_tasks = []
    for exchange_name, exchange in exchanges.items():
        for symbol in SYMBOLS:
            ticker_tasks.append(fetch_ticker(exchange_name, exchange, symbol))

    results = await asyncio.gather(*ticker_tasks)

    # Organize results: {exchange_name: {symbol: ticker}}
    exchange_tickers = {}
    for exchange_name, symbol, ticker in results:
        if ticker is None:
            continue
        if exchange_name not in exchange_tickers:
            exchange_tickers[exchange_name] = {}
        exchange_tickers[exchange_name][symbol] = ticker

    # For each symbol, check arbitrage between all pairs of exchanges
    for symbol in SYMBOLS:
        # Collect tickers for this symbol from all exchanges
        tickers = {}
        for exchange_name in exchanges:
            if symbol in exchange_tickers.get(exchange_name, {}):
                ticker = exchange_tickers[exchange_name][symbol]
                tickers[exchange_name] = {
                    "bid": ticker["bid"],
                    "ask": ticker["ask"],
                }

        # Check all pairs of exchanges
        exchange_names = list(tickers.keys())
        for buy_exch, sell_exch in permutations(exchange_names, 2):
            buy_ticker = tickers[buy_exch]
            sell_ticker = tickers[sell_exch]

            if buy_ticker["ask"] is None or sell_ticker["bid"] is None:
                continue

            # Spread: LONG on buy_exch / SHORT on sell_exch
            spread_pct = (sell_ticker["bid"] - buy_ticker["ask"]) / buy_ticker["ask"] * 100

            if spread_pct >= MIN_SPREAD_PCT:
                opportunities.append({
                    "symbol": symbol,
                    "buy_exchange": buy_exch,
                    "sell_exchange": sell_exch,
                    "buy_price": buy_ticker["ask"],
                    "sell_price": sell_ticker["bid"],
                    "spread_pct": round(spread_pct, 4),
                })

    # Deduplicate and sort by spread
    seen = set()
    unique_opportunities = []
    for opp in opportunities:
        key = (opp["symbol"], opp["buy_exchange"], opp["sell_exchange"])
        if key not in seen:
            seen.add(key)
            unique_opportunities.append(opp)

    # Sort by spread percentage descending
    unique_opportunities.sort(key=lambda x: x["spread_pct"], reverse=True)

    return unique_opportunities


# Шаг 3: Виртуальный двигатель сделок

CLOSE_SPREAD_PCT = 0.05  # При каком спреде закрываем
FEE_PER_LEG_PCT = 0.06   # Комиссия одной ноги (taker) в процентах
STAKE_USD = 100.0        # Размер каждой ноги

active_arbitrages = {}  # ключ — uuid, значение — данные о сделке


def open_arbitrage(opp: dict) -> str:
    """Открыть виртуальную арбитражную сделку."""
    arb_id = str(uuid.uuid4())
    trade_data = {
        "symbol": opp["symbol"],
        "buy_exchange": opp["buy_exchange"],
        "sell_exchange": opp["sell_exchange"],
        "buy_price": opp["buy_price"],
        "sell_price": opp["sell_price"],
        "entry_spread": opp["spread_pct"],
        "opened_at": time.time(),
    }
    active_arbitrages[arb_id] = trade_data
    return arb_id


async def _calculate_trade_metrics(trade: dict, exchanges: dict) -> dict | None:
    buy_exchange = exchanges.get(trade["buy_exchange"])
    sell_exchange = exchanges.get(trade["sell_exchange"])
    if buy_exchange is None or sell_exchange is None:
        return None

    try:
        ticker_buy, ticker_sell = await asyncio.gather(
            buy_exchange.fetch_ticker(trade["symbol"]),
            sell_exchange.fetch_ticker(trade["symbol"]),
        )
        current_ask_buy = ticker_buy.get("ask")
        current_bid_buy = ticker_buy.get("bid")
        current_bid_sell = ticker_sell.get("bid")
        current_ask_sell = ticker_sell.get("ask")
        prices = (
            current_ask_buy,
            current_bid_buy,
            current_bid_sell,
            current_ask_sell,
            trade["buy_price"],
            trade["sell_price"],
        )
        if any(
            not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0
            for price in prices
        ):
            logging.warning("Invalid ticker prices while calculating PnL for %s", trade["symbol"])
            return None
    except Exception as e:
        logging.warning("Failed to fetch current ticker for %s: %s", trade["symbol"], e)
        return None

    current_spread_pct = (current_bid_sell - current_ask_buy) / current_ask_buy * 100
    pnl_buy_pct = (current_bid_buy - trade["buy_price"]) / trade["buy_price"] * 100
    pnl_sell_pct = (trade["sell_price"] - current_ask_sell) / trade["sell_price"] * 100
    pnl_pct = pnl_buy_pct + pnl_sell_pct - 4 * FEE_PER_LEG_PCT
    pnl_usd = (STAKE_USD / 100) * pnl_pct

    return {
        "current_spread": current_spread_pct,
        "pnl_pct": round(pnl_pct, 4),
        "pnl_usd": round(pnl_usd, 2),
    }


async def calculate_current_pnl(exchanges: dict) -> list[dict]:
    """Calculate unrealized PnL for active virtual arbitrages without closing them."""
    results = []
    for trade in list(active_arbitrages.values()):
        metrics = await _calculate_trade_metrics(trade, exchanges)
        if metrics is not None:
            results.append({
                "symbol": trade["symbol"],
                "current_spread": round(metrics["current_spread"], 4),
                "pnl_pct": metrics["pnl_pct"],
                "pnl_usd": metrics["pnl_usd"],
            })
    return results


async def check_and_close_arbitrages(exchanges: dict) -> list[dict]:
    """Проверить все активные сделки и закрыть celles, где спред схлопнулся."""
    closed_trades = []

    for arb_id, trade in list(active_arbitrages.items()):
        metrics = await _calculate_trade_metrics(trade, exchanges)
        if metrics is None:
            continue

        # Если спред упал до порога закрытия — закрываем сделку
        if metrics["current_spread"] <= CLOSE_SPREAD_PCT:
            closed_trade = {
                "symbol": trade["symbol"],
                "buy_exchange": trade["buy_exchange"],
                "sell_exchange": trade["sell_exchange"],
                "entry_spread": trade["entry_spread"],
                "exit_spread": round(metrics["current_spread"], 4),
                "pnl_pct": metrics["pnl_pct"],
                "pnl_usd": metrics["pnl_usd"],
            }
            closed_trades.append(closed_trade)
            del active_arbitrages[arb_id]

    return closed_trades