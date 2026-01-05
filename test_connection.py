#!/usr/bin/env python3
"""
Binance API 连接测试脚本
用于验证 SSL 优化后的连接稳定性
"""
import asyncio
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from src.binance_client import BinanceClient


async def test_basic_connection():
    """测试基础连接"""
    logger.info("=" * 60)
    logger.info("开始测试 Binance API 连接...")
    logger.info("=" * 60)

    client = BinanceClient()

    try:
        # 测试 1: 获取交易对列表
        logger.info("\n[测试 1] 获取交易对列表...")
        symbols = await client.get_all_symbols()
        if symbols:
            logger.success(f"✓ 成功获取 {len(symbols)} 个交易对")
            logger.info(f"  示例: {', '.join(symbols[:5])}")
        else:
            logger.error("✗ 获取交易对列表失败")
            return False

        # 测试 2: 获取 BTC 价格信息
        logger.info("\n[测试 2] 获取 BTCUSDT 24h 统计...")
        ticker = await client.get_ticker_24h("BTCUSDT")
        if ticker:
            price = float(ticker.get("lastPrice", 0))
            change = float(ticker.get("priceChangePercent", 0))
            logger.success(f"✓ BTCUSDT 价格: ${price:,.2f}, 24h涨跌: {change:+.2f}%")
        else:
            logger.error("✗ 获取价格信息失败")
            return False

        # 测试 3: 获取持仓量
        logger.info("\n[测试 3] 获取 BTCUSDT 持仓量...")
        oi = await client.get_open_interest("BTCUSDT")
        if oi:
            logger.success(f"✓ BTCUSDT 持仓量: {oi:,.2f}")
        else:
            logger.error("✗ 获取持仓量失败")
            return False

        # 测试 4: 获取 K 线数据
        logger.info("\n[测试 4] 获取 ETHUSDT K 线数据...")
        klines = await client.get_klines("ETH", interval="1h", limit=10)
        if klines:
            logger.success(f"✓ 成功获取 {len(klines)} 条 K 线数据")
            if klines:
                latest = klines[-1]
                logger.info(f"  最新价格: ${latest['close']:,.2f}")
        else:
            logger.error("✗ 获取 K 线数据失败")
            return False

        # 测试 5: 批量请求测试（模拟实际使用场景）
        logger.info("\n[测试 5] 批量获取多个合约持仓量...")
        test_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
        oi_data = await client.get_all_open_interest(test_symbols)
        if oi_data:
            logger.success(f"✓ 成功获取 {len(oi_data)}/{len(test_symbols)} 个合约的持仓量")
            for symbol, value in list(oi_data.items())[:3]:
                logger.info(f"  {symbol}: {value:,.2f}")
        else:
            logger.warning("⚠ 批量获取部分失败")

        logger.info("\n" + "=" * 60)
        logger.success("✓ 所有测试通过！连接正常")
        logger.info("=" * 60)
        return True

    except Exception as e:
        logger.error(f"\n✗ 测试出错: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await client.close()


async def test_stress_connection():
    """压力测试：连续请求"""
    logger.info("\n" + "=" * 60)
    logger.info("开始压力测试（连续 20 次请求）...")
    logger.info("=" * 60)

    client = BinanceClient()
    success_count = 0
    fail_count = 0

    try:
        for i in range(20):
            ticker = await client.get_ticker_24h("BTCUSDT")
            if ticker:
                success_count += 1
                logger.info(f"  [{i+1}/20] ✓ 成功")
            else:
                fail_count += 1
                logger.warning(f"  [{i+1}/20] ✗ 失败")

            await asyncio.sleep(0.1)  # 短暂延迟

        logger.info(f"\n压力测试结果: 成功 {success_count}/20, 失败 {fail_count}/20")
        logger.info(f"成功率: {success_count/20*100:.1f}%")

    finally:
        await client.close()


async def main():
    """主函数"""
    # 基础连接测试
    success = await test_basic_connection()

    if success:
        # 如果基础测试通过，进行压力测试
        await asyncio.sleep(2)
        await test_stress_connection()
    else:
        logger.error("\n基础测试失败，请检查网络连接或 SSL 配置")
        sys.exit(1)


if __name__ == "__main__":
    # 配置日志
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.warning("\n测试被用户中断")
        sys.exit(0)
