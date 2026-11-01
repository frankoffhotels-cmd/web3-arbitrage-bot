import base64
import hashlib
import json
import os
import posixpath
import re
import tarfile
import tempfile
import urllib.request
from io import BytesIO
from pathlib import Path, PurePosixPath

import solcx
from web3 import Web3


RPC_URL = "https://sepolia-rollup.arbitrum.io/rpc"
CHAIN_ID = 421614
SOLC_VERSION = "0.8.20"
AAVE_POOL_ADDRESSES_PROVIDER = "0xB25a5D144626a0D488e52AE717A051a2E9997076"
CONTRACT_NAME = "FlashLoanArbitrage"
IMPORT_PATTERN = re.compile(
    r"\bimport\s+(?:[^;]*?\s+from\s+)?[\"']([^\"']+)[\"']\s*;"
)

PROJECT_DIR = Path(__file__).resolve().parent
CONTRACT_PATH = PROJECT_DIR / "FlashLoanArbitrage.sol"

PACKAGES = (
    {
        "prefix": "@aave/core-v3/",
        "url": "https://registry.npmjs.org/@aave/core-v3/-/core-v3-1.19.3.tgz",
        "integrity": "sha512-Xr7+VcoU5b4mPwM4IUCnskw3lciwrnL4Xloyad8GOddYeixnri2lZYobNmErm1LwE50vqjI0O+9QGNKY2TDkeQ==",
        "destination": ("@aave", "core-v3"),
    },
    {
        "prefix": "@openzeppelin/contracts/",
        "url": "https://registry.npmjs.org/@openzeppelin/contracts/-/contracts-5.0.2.tgz",
        "integrity": "sha512-ytPc6eLGcHHnapAZ9S+5qsdomhjo6QBHTDRRBFfTxXIpsicMhVPouPgmUPebZZZGX7vt9USA+Z+0M0dSVtSUEA==",
        "destination": ("@openzeppelin", "contracts"),
    },
    {
        "prefix": "@uniswap/v2-periphery/",
        "url": "https://registry.npmjs.org/@uniswap/v2-periphery/-/v2-periphery-1.1.0-beta.0.tgz",
        "integrity": "sha512-6dkwAMKza8nzqYiXEr2D86dgW3TTavUvCR0w2Tu33bAbM8Ah43LKAzH7oKKPRT5VJQaMi1jtkGs1E8JPor1n5g==",
        "destination": ("@uniswap", "v2-periphery"),
    },
)


def read_private_key() -> str:
    key = os.environ.get("PRIVATE_KEY")
    if key:
        return key.strip().strip("\"'")

    env_path = PROJECT_DIR / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(
            "PRIVATE_KEY was not found in the environment or project .env file."
        )

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        name, separator, value = stripped.partition("=")
        if separator and name.strip() == "PRIVATE_KEY":
            return value.strip().strip("\"'")

    raise ValueError("The project .env file does not define PRIVATE_KEY.")


def extract_solidity_sources(archive_bytes: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if not member.isfile() or path.suffix != ".sol":
                continue
            if not path.parts or path.parts[0] != "package":
                continue

            relative_path = Path(*path.parts[1:])
            target = (destination / relative_path).resolve()
            if destination.resolve() not in target.parents:
                raise ValueError("Dependency archive contains an unsafe path.")

            source = archive.extractfile(member)
            if source is None:
                raise ValueError("Could not read Solidity source from dependency archive.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())


def install_solidity_sources(node_modules: Path) -> list[tuple[str, Path]]:
    package_locations = []
    for package in PACKAGES:
        url = package["url"]
        request = urllib.request.Request(
            url, headers={"User-Agent": "FlashLoanArbitrage-deployer/1.0"}
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            archive_bytes = response.read()

        digest = base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode()
        if f"sha512-{digest}" != package["integrity"]:
            raise ValueError(f"Integrity verification failed for {package['prefix']}")

        destination = node_modules.joinpath(*package["destination"])
        extract_solidity_sources(archive_bytes, destination)
        package_locations.append((package["prefix"], destination))

    return package_locations


def resolve_import(import_path: str, importer: str, package_locations) -> tuple[str, Path]:
    if import_path.startswith("./") or import_path.startswith("../"):
        source_unit = posixpath.normpath(
            posixpath.join(posixpath.dirname(importer), import_path)
        )
    else:
        source_unit = posixpath.normpath(import_path)

    for prefix, package_root in package_locations:
        if source_unit.startswith(prefix):
            relative_path = source_unit[len(prefix) :]
            source_path = package_root.joinpath(*PurePosixPath(relative_path).parts)
            if source_path.is_file():
                return source_unit, source_path
            break

    raise FileNotFoundError(
        f"Unable to resolve Solidity import '{import_path}' from '{importer}'."
    )


def compile_contract() -> tuple[list, str]:
    if not CONTRACT_PATH.is_file():
        raise FileNotFoundError(f"Contract source not found: {CONTRACT_PATH}")

    if SOLC_VERSION not in {str(version) for version in solcx.get_installed_solc_versions()}:
        solcx.install_solc(SOLC_VERSION)

    with tempfile.TemporaryDirectory(prefix="flashloan-solidity-") as temp_dir:
        package_locations = install_solidity_sources(Path(temp_dir) / "node_modules")
        sources = {
            CONTRACT_PATH.name: {
                "content": CONTRACT_PATH.read_text(encoding="utf-8")
            }
        }
        pending = [CONTRACT_PATH.name]
        while pending:
            importer = pending.pop()
            content = sources[importer]["content"]
            for import_path in IMPORT_PATTERN.findall(content):
                source_unit, source_path = resolve_import(
                    import_path, importer, package_locations
                )
                if source_unit not in sources:
                    sources[source_unit] = {
                        "content": source_path.read_text(encoding="utf-8")
                    }
                    pending.append(source_unit)

        standard_input = {
            "language": "Solidity",
            "sources": sources,
            "settings": {
                "outputSelection": {
                    CONTRACT_PATH.name: {
                        CONTRACT_NAME: ["abi", "evm.bytecode.object"]
                    }
                }
            },
        }
        output = solcx.compile_standard(
            standard_input,
            solc_version=SOLC_VERSION,
        )

        errors = [
            item
            for item in output.get("errors", [])
            if item.get("severity") == "error"
        ]
        if errors:
            messages = "\n".join(item.get("formattedMessage", str(item)) for item in errors)
            raise RuntimeError(f"Solidity compilation failed:\n{messages}")

        artifact = output["contracts"][CONTRACT_PATH.name][CONTRACT_NAME]
        return artifact["abi"], artifact["evm"]["bytecode"]["object"]


def main() -> None:
    web3 = Web3(
        Web3.HTTPProvider(
            RPC_URL,
            request_kwargs={"timeout": 30},
        )
    )
    if not web3.is_connected():
        raise ConnectionError("Could not connect to the Arbitrum Sepolia RPC.")
    if web3.eth.chain_id != CHAIN_ID:
        raise RuntimeError("RPC endpoint is not connected to Arbitrum Sepolia.")

    provider = Web3.to_checksum_address(AAVE_POOL_ADDRESSES_PROVIDER)
    if not web3.eth.get_code(provider):
        raise RuntimeError("Aave PoolAddressesProvider has no contract code on this network.")

    private_key = read_private_key()
    if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", private_key):
        raise ValueError("PRIVATE_KEY must be a 32-byte hexadecimal private key.")

    account = web3.eth.account.from_key(private_key)
    abi, bytecode = compile_contract()
    factory = web3.eth.contract(abi=abi, bytecode=bytecode)
    latest_block = web3.eth.get_block("latest")
    base_fee = latest_block.get("baseFeePerGas")
    if base_fee is None:
        raise RuntimeError("The latest block does not expose an EIP-1559 base fee.")

    max_priority_fee = web3.to_wei(2, "gwei")
    transaction = {
        "from": account.address,
        "nonce": web3.eth.get_transaction_count(account.address),
        "chainId": CHAIN_ID,
        "maxPriorityFeePerGas": max_priority_fee,
        "maxFeePerGas": base_fee * 2 + max_priority_fee,
    }
    transaction["gas"] = factory.constructor(provider).estimate_gas(transaction) * 120 // 100
    deployment = factory.constructor(provider).build_transaction(transaction)
    signed_transaction = account.sign_transaction(deployment)
    transaction_hash = web3.eth.send_raw_transaction(signed_transaction.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(transaction_hash, timeout=300)

    if receipt.status != 1 or receipt.contractAddress is None:
        raise RuntimeError(f"Deployment failed. Transaction: {transaction_hash.hex()}")

    print(f"Contract deployed at: {receipt.contractAddress}")
    print(f"Transaction: {transaction_hash.hex()}")


if __name__ == "__main__":
    main()
