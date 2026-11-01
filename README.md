# Web3 Arbitrage Bot & Flash Loan Smart Contract

A Python-based automated trading bot and smart contract for executing arbitrage opportunities on decentralized exchanges (DEX) using Flash Loans.

## Tech Stack
*   **Python:** `asyncio`, `aiogram` (Telegram bot), `web3.py`
*   **Smart Contracts:** Solidity `^0.8.20` (Aave V3, Uniswap V2)
*   **Network:** Arbitrum Sepolia (Testnet)

## Features
*   Scans DEX pools for price discrepancies.
*   Calculates PnL and sends signals via Telegram.
*   Deploys a Flash Loan smart contract to execute trades without initial capital.
*   **Deployed Contract Address (Arbitrum Sepolia):** `0xD0f8EE59Cf94ea5167623df288E61BC009bE99F3`

## Setup
1. Clone the repo.
2. Install dependencies: `pip install -r requirements.txt`.
3. Create a `.env` file with `PRIVATE_KEY` and `TELEGRAM_BOT_TOKEN`.
4. Run the bot: `python main.py`.
