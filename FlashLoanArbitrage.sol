// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@aave/core-v3/contracts/flashloan/base/FlashLoanSimpleReceiverBase.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@uniswap/v2-periphery/contracts/interfaces/IUniswapV2Router02.sol";

contract FlashLoanArbitrage is FlashLoanSimpleReceiverBase, Ownable {
    using SafeERC20 for IERC20;

    address public constant UNISWAP_ROUTER =
        0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24;
    address public constant SUSHISWAP_ROUTER =
        0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506;

    bool private arbitrageActive;
    address private activeAsset;
    uint256 private startingAssetBalance;

    constructor(IPoolAddressesProvider provider)
        FlashLoanSimpleReceiverBase(provider)
        Ownable(msg.sender)
    {}

    function executeArbitrage(
        address asset,
        uint256 amount,
        bytes calldata params
    ) external onlyOwner {
        require(!arbitrageActive, "Arbitrage already active");
        require(asset != address(0), "Invalid asset");
        require(amount > 0, "Invalid amount");

        arbitrageActive = true;
        activeAsset = asset;
        startingAssetBalance = IERC20(asset).balanceOf(address(this));

        POOL.flashLoanSimple(address(this), asset, amount, params, 0);

        arbitrageActive = false;
        activeAsset = address(0);
        startingAssetBalance = 0;
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(POOL), "Caller is not Aave Pool");
        require(arbitrageActive, "No active arbitrage");
        require(initiator == address(this), "Invalid initiator");
        require(asset == activeAsset, "Unexpected asset");

        (address tokenOut, uint256 minAmountOut) =
            abi.decode(params, (address, uint256));
        require(tokenOut != address(0), "Invalid output token");
        require(tokenOut != asset, "Tokens must differ");

        IERC20 assetToken = IERC20(asset);
        IERC20 tokenOutToken = IERC20(tokenOut);

        address[] memory buyPath = new address[](2);
        buyPath[0] = asset;
        buyPath[1] = tokenOut;

        assetToken.forceApprove(UNISWAP_ROUTER, amount);
        uint256[] memory buyAmounts =
            IUniswapV2Router02(UNISWAP_ROUTER).swapExactTokensForTokens(
                amount,
                minAmountOut,
                buyPath,
                address(this),
                block.timestamp
            );
        assetToken.forceApprove(UNISWAP_ROUTER, 0);
        require(buyAmounts.length >= 2, "Invalid buy swap result");

        uint256 tokenOutAmount = buyAmounts[buyAmounts.length - 1];
        require(tokenOutAmount > 0, "No output tokens received");

        address[] memory sellPath = new address[](2);
        sellPath[0] = tokenOut;
        sellPath[1] = asset;

        tokenOutToken.forceApprove(SUSHISWAP_ROUTER, tokenOutAmount);
        IUniswapV2Router02(SUSHISWAP_ROUTER).swapExactTokensForTokens(
            tokenOutAmount,
            0,
            sellPath,
            address(this),
            block.timestamp
        );
        tokenOutToken.forceApprove(SUSHISWAP_ROUTER, 0);

        uint256 amountOwed = amount + premium;
        uint256 requiredBalance = startingAssetBalance + amountOwed;
        uint256 assetBalance = assetToken.balanceOf(address(this));
        require(assetBalance >= requiredBalance, "Arbitrage is not profitable");

        assetToken.forceApprove(address(POOL), amountOwed);

        uint256 profit = assetBalance - requiredBalance;
        if (profit > 0) {
            assetToken.safeTransfer(owner(), profit);
        }

        return true;
    }
}
