import asyncio
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from web3 import AsyncWeb3, Web3
from web3.providers.rpc import AsyncHTTPProvider

from logging_config import setup_logging

logger = setup_logging()

RPC_URL = "https://arb1.arbitrum.io/rpc"
ARBITRUM_SEPOLIA_RPC_URL = "https://sepolia-rollup.arbitrum.io/rpc"
FLASH_LOAN_CONTRACT_ADDRESS = "0xD0f8EE59Cf94ea5167623df288E61BC009bE99F3"
ARBITRUM_SEPOLIA_CHAIN_ID = 421614
ARBITRUM_SEPOLIA_WETH_ADDRESS = "0x1dF462e2712496373A347f8ad10802a5E95f053D"
ARBITRUM_SEPOLIA_USDC_ADDRESS = "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d"
TEST_FLASH_LOAN_AMOUNT = 1_000_000_000_000_000
TEST_MIN_AMOUNT_OUT = 1
UNISWAP_V2_ROUTER = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
SUSHISWAP_ROUTER = "0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506"
WETH_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
WBTC_ADDRESS = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"
USDC_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

FLASH_LOAN_CONTRACT_ABI = [
    {
        "type": "function",
        "name": "executeArbitrage",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "params", "type": "bytes"},
        ],
        "outputs": [],
    }
]

ROUTER_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    }
]


def _to_decimal(value: Any, *, decimals: int = 18) -> Decimal:
    """Convert raw integer-like value to Decimal while preserving precision."""
    if value is None:
        raise ValueError("Value must not be None")

    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, int):
            return Decimal(value) / (Decimal(10) ** Decimal(decimals))
        if isinstance(value, str):
            return Decimal(value) / (Decimal(10) ** Decimal(decimals))
        return Decimal(str(value)) / (Decimal(10) ** Decimal(decimals))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Unable to convert value to Decimal: {value!r}") from exc


def _validate_amounts(amounts: Any) -> List[int]:
    """Validate raw getAmountsOut result and return normalized list of integers."""
    if not isinstance(amounts, (list, tuple)) or not amounts:
        raise ValueError(f"Invalid amounts output: {amounts!r}")

    normalized: List[int] = []
    for idx, amount in enumerate(amounts):
        if not isinstance(amount, int):
            try:
                amount = int(amount)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid amount at index {idx}: {amount!r}") from exc
        normalized.append(amount)

    if len(normalized) < 2:
        raise ValueError(f"getAmountsOut returned less than 2 values: {normalized!r}")

    return normalized


async def _quote_token_to_usdc(
    router_name: str,
    router_address: str,
    symbol: str,
    token_address: str,
    token_decimals: int,
    min_price_usdc: Decimal,
    max_price_usdc: Decimal,
) -> Dict[str, Any]:
    """Fetch a token's USDC price on a specific DEX router."""
    web3 = AsyncWeb3(AsyncHTTPProvider(RPC_URL))
    router = web3.eth.contract(address=router_address, abi=ROUTER_ABI)

    amount_in_one_token = 10**token_decimals
    path = [token_address, USDC_ADDRESS]

    try:
        raw_amounts = await router.functions.getAmountsOut(amount_in_one_token, path).call()
        amounts = _validate_amounts(raw_amounts)
        amount_out = amounts[1]

        price_usdc = _to_decimal(amount_out, decimals=6)

        # Reject implausible prices so empty or severely imbalanced pools cannot create fake spreads.
        if not (min_price_usdc < price_usdc < max_price_usdc):
            logger.warning(f"Аномальная цена на {router_name}: {price_usdc}. Игнорируем.")
            return {
                "symbol": symbol,
                "dex": router_name,
                "router": router_address,
                "price_usdc": 0.0,
            }

        return {
            "symbol": symbol,
            "dex": router_name,
            "router": router_address,
            "price_usdc": float(price_usdc),
        }
    except Exception as exc:
        logger.exception("DEX quote failed for %s on %s: %s", router_name, symbol, exc)
        return {
            "symbol": symbol,
            "dex": router_name,
            "router": router_address,
            "price_usdc": 0.0,
        }
    finally:
        await web3.provider.disconnect()


def _compare_pair_prices(symbol: str, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a spread result for one token pair from valid DEX quotes."""
    prices = {
        item["dex"]: item["price_usdc"]
        for item in results
        if item["price_usdc"] > 0
    }

    if len(prices) < 2:
        logger.warning("Not enough valid DEX quotes to calculate a spread for %s", symbol)
        return {
            "symbol": symbol,
            "buy_dex": None,
            "sell_dex": None,
            "buy_price": 0.0,
            "sell_price": 0.0,
            "spread_pct": 0.0,
            "prices": {key: round(float(value), 2) for key, value in prices.items()},
        }

    lower_dex, lower_price = min(prices.items(), key=lambda item: item[1])
    higher_dex, higher_price = max(prices.items(), key=lambda item: item[1])

    lower_decimal = Decimal(str(lower_price))
    higher_decimal = Decimal(str(higher_price))
    spread_pct = ((higher_decimal - lower_decimal) / lower_decimal) * Decimal("100")

    return {
        "symbol": symbol,
        "buy_dex": lower_dex,
        "sell_dex": higher_dex,
        "buy_price": round(float(lower_price), 2),
        "sell_price": round(float(higher_price), 2),
        "spread_pct": round(float(spread_pct), 4),
        "prices": {key: round(float(value), 2) for key, value in prices.items()},
    }


async def check_dex_prices() -> Dict[str, Any]:
    """Compare WETH/USDC and WBTC/USDC prices across Arbitrum DEX routers."""
    quote_tasks = [
        _quote_token_to_usdc(
            router_name,
            router_address,
            "WETH/USDC",
            WETH_ADDRESS,
            18,
            Decimal("2000.0"),
            Decimal("5000.0"),
        )
        for router_name, router_address in (
            ("Uniswap V2", UNISWAP_V2_ROUTER),
            ("SushiSwap", SUSHISWAP_ROUTER),
        )
    ]
    quote_tasks.extend(
        _quote_token_to_usdc(
            router_name,
            router_address,
            "WBTC/USDC",
            WBTC_ADDRESS,
            8,
            Decimal("40000.0"),
            Decimal("150000.0"),
        )
        for router_name, router_address in (
            ("Uniswap V2", UNISWAP_V2_ROUTER),
            ("SushiSwap", SUSHISWAP_ROUTER),
        )
    )
    quote_results = await asyncio.gather(*quote_tasks)

    pair_results = []
    for symbol in ("WETH/USDC", "WBTC/USDC"):
        pair_quotes = [item for item in quote_results if item["symbol"] == symbol]
        pair_results.append(_compare_pair_prices(symbol, pair_quotes))

    best_result = max(pair_results, key=lambda item: item["spread_pct"])
    return {**best_result, "pairs": pair_results}


async def execute_flash_loan_arbitrage(symbol: str, buy_dex: str, sell_dex: str) -> None:
    """Submit the configured test call to the deployed Arbitrum Sepolia contract."""
    logger.info(
        "Submitting test flash-loan arbitrage: symbol=%s, buy_on=%s, sell_on=%s",
        symbol,
        buy_dex,
        sell_dex,
    )

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("PRIVATE_KEY is not set in the environment.")
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key.strip()):
        raise ValueError("PRIVATE_KEY must be a 32-byte hexadecimal private key.")

    web3 = AsyncWeb3(
        AsyncHTTPProvider(ARBITRUM_SEPOLIA_RPC_URL, request_kwargs={"timeout": 30})
    )
    try:
        if not await web3.is_connected():
            raise ConnectionError("Could not connect to the Arbitrum Sepolia RPC.")
        if await web3.eth.chain_id != ARBITRUM_SEPOLIA_CHAIN_ID:
            raise RuntimeError("RPC endpoint is not connected to Arbitrum Sepolia.")

        account = Web3().eth.account.from_key(private_key.strip())
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(FLASH_LOAN_CONTRACT_ADDRESS),
            abi=FLASH_LOAN_CONTRACT_ABI,
        )

        latest_block = await web3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if base_fee is None:
            raise RuntimeError("Latest block does not expose an EIP-1559 base fee.")

        priority_fee = await web3.eth.max_priority_fee
        max_fee_per_gas = max(base_fee * 3 // 2, base_fee + priority_fee)
        nonce = await web3.eth.get_transaction_count(account.address)
        params = web3.codec.encode(
            ["address", "uint256"],
            [
                Web3.to_checksum_address(ARBITRUM_SEPOLIA_USDC_ADDRESS),
                TEST_MIN_AMOUNT_OUT,
            ],
        )
        transaction = await contract.functions.executeArbitrage(
            Web3.to_checksum_address(ARBITRUM_SEPOLIA_WETH_ADDRESS),
            TEST_FLASH_LOAN_AMOUNT,
            params,
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": ARBITRUM_SEPOLIA_CHAIN_ID,
                "gas": 300_000,
                "maxFeePerGas": max_fee_per_gas,
                "maxPriorityFeePerGas": priority_fee,
            }
        )

        signed_transaction = account.sign_transaction(transaction)
        transaction_hash = await web3.eth.send_raw_transaction(
            signed_transaction.raw_transaction
        )
        logger.info("TX HASH: %s", Web3.to_hex(transaction_hash))

        receipt = await web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=300,
        )
        if receipt["status"] == 0:
            logger.warning(
                "Транзакция отправлена, но отменена контрактом (ожидаемо для теста)"
            )
        else:
            logger.info("Flash-loan transaction confirmed successfully.")
    finally:
        await web3.provider.disconnect()
