import logging
import os
import re
from typing import Any

from web3 import AsyncWeb3, Web3
from web3.providers.rpc import AsyncHTTPProvider


RPC_URL = "https://sepolia-rollup.arbitrum.io/rpc"
CONTRACT_ADDRESS = "0xD0f8EE59Cf94ea5167623df288E61BC009bE99F3"
CHAIN_ID = 421614
WETH_ADDRESS = "0x1dF462e2712496373A347f8ad10802a5E95f053D"
TEST_AMOUNT = 1_000_000_000_000_000

CONTRACT_ABI = [
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def execute_flash_loan_arbitrage(
    symbol: str,
    buy_dex: str,
    sell_dex: str,
) -> Any | None:
    logger.info(
        "Submitting test arbitrage for %s: %s -> %s",
        symbol,
        buy_dex,
        sell_dex,
    )

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise RuntimeError("PRIVATE_KEY is not set in the environment.")
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key.strip()):
        raise ValueError("PRIVATE_KEY must be a 32-byte hexadecimal private key.")

    web3 = AsyncWeb3(AsyncHTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    try:
        if not await web3.is_connected():
            raise ConnectionError("Could not connect to the Arbitrum Sepolia RPC.")
        if await web3.eth.chain_id != CHAIN_ID:
            raise RuntimeError("RPC endpoint is not connected to Arbitrum Sepolia.")

        account = Web3().eth.account.from_key(private_key.strip())
        contract = web3.eth.contract(
            address=Web3.to_checksum_address(CONTRACT_ADDRESS),
            abi=CONTRACT_ABI,
        )

        latest_block = await web3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if base_fee is None:
            raise RuntimeError("Latest block does not expose an EIP-1559 base fee.")

        priority_fee = await web3.eth.max_priority_fee
        max_fee_per_gas = max(base_fee * 3 // 2, base_fee + priority_fee)
        nonce = await web3.eth.get_transaction_count(account.address)
        transaction = await contract.functions.executeArbitrage(
            Web3.to_checksum_address(WETH_ADDRESS),
            TEST_AMOUNT,
            b"",
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": CHAIN_ID,
                "gas": 300_000,
                "maxFeePerGas": max_fee_per_gas,
                "maxPriorityFeePerGas": priority_fee,
            }
        )

        signed_transaction = account.sign_transaction(transaction)
        transaction_hash = await web3.eth.send_raw_transaction(
            signed_transaction.raw_transaction
        )
        tx_hash_hex = Web3.to_hex(transaction_hash)
        logger.info("TX HASH: %s", tx_hash_hex)

        receipt = await web3.eth.wait_for_transaction_receipt(
            transaction_hash,
            timeout=300,
        )
        if receipt["status"] == 0:
            logger.warning(
                "Транзакция отправлена, но отменена контрактом (ожидаемо для теста)"
            )
        else:
            logger.info("Transaction confirmed successfully.")

        return receipt
    finally:
        await web3.provider.disconnect()
