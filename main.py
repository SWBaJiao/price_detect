"""
合约价格异动监控系统
主程序入口
"""
import asyncio
import signal
import sys
from pathlib import Path
from typing import List

from loguru import logger

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from src.config_manager import get_config, Settings
from src.binance_client import BinanceClient
from src.price_tracker import PriceTracker
from src.alert_engine import AlertEngine
from src.telegram_bot import TelegramBot, AlertNotifier
from src.bot_handler import BotCommandHandler
from src.models import TickerData, AlertEvent


class MonitorApp:
    """
    价格异动监控应用
    协调各模块运行
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._running = False

        # 初始化组件
        self.binance = BinanceClient()
        self.tracker = PriceTracker(
            price_window=settings.alerts.price_change.time_window,
            volume_periods=settings.alerts.volume_spike.lookback_periods,
            oi_window=settings.alerts.open_interest.time_window,
            spread_window=settings.alerts.spot_futures_spread.time_window
        )

        # Telegram 通知
        self.telegram = TelegramBot(
            token=settings.telegram.bot_token,
            chat_id=settings.telegram.chat_id
        )
        self.notifier = AlertNotifier(telegram=self.telegram)

        # 异动检测引擎
        self.alert_engine = AlertEngine(
            settings=settings,
            tracker=self.tracker,
            on_alert=self._on_alert
        )

        # Bot 命令处理器（用于查询功能）
        self.bot_handler = BotCommandHandler(
            token=settings.telegram.bot_token,
            binance=self.binance
        )

        # OI 轮询任务
        self._oi_task = None
        # 现货价格轮询任务
        self._spot_price_task = None
        # Bot 命令处理任务
        self._bot_task = None

    def _on_alert(self, event: AlertEvent):
        """告警回调"""
        asyncio.create_task(self.notifier.notify(event))

    async def _handle_tickers(self, tickers: List[TickerData]):
        """处理行情数据"""
        await self.alert_engine.process_tickers(tickers)

    async def _poll_open_interest(self):
        """定时轮询持仓量"""
        interval = self.settings.alerts.open_interest.poll_interval

        while self._running:
            try:
                # 获取当前追踪的所有交易对
                symbols = self.tracker.get_all_symbols()
                if symbols:
                    # 批量获取 OI
                    oi_data = await self.binance.get_all_open_interest(symbols)

                    # 更新追踪器
                    for symbol, oi in oi_data.items():
                        self.tracker.update_oi(symbol, oi)

                    logger.debug(f"已更新 {len(oi_data)} 个合约的持仓量")

            except Exception as e:
                logger.error(f"轮询持仓量失败: {e}")

            await asyncio.sleep(interval)

    async def _poll_spot_prices(self):
        """定时轮询现货价格"""
        interval = self.settings.alerts.spot_futures_spread.poll_interval

        while self._running:
            try:
                # 批量获取所有现货价格
                spot_prices = await self.binance.get_all_spot_tickers()

                if spot_prices:
                    # 批量更新追踪器
                    self.tracker.batch_update_spot_prices(spot_prices)
                    logger.debug(f"已更新 {len(spot_prices)} 个现货价格")

            except Exception as e:
                logger.error(f"轮询现货价格失败: {e}")

            await asyncio.sleep(interval)

    async def start(self):
        """启动监控"""
        self._running = True

        # 配置日志
        self._setup_logging()

        logger.info("=" * 50)
        logger.info("合约价格异动监控系统启动")
        logger.info("=" * 50)

        # 测试 Telegram 连接
        if self.telegram.is_enabled:
            if await self.telegram.test_connection():
                logger.info("Telegram 连接正常")
            else:
                logger.warning("Telegram 连接失败，消息推送可能不可用")

        # 启动通知器
        await self.notifier.start()

        # 启动 OI 轮询任务
        if self.settings.alerts.open_interest.enabled:
            self._oi_task = asyncio.create_task(self._poll_open_interest())
            logger.info("持仓量轮询已启动")

        # 启动现货价格轮询任务
        if self.settings.alerts.spot_futures_spread.enabled:
            self._spot_price_task = asyncio.create_task(self._poll_spot_prices())
            logger.info("现货价格轮询已启动")

        # 启动 Bot 命令处理器（用于查询功能）
        if self.telegram.is_enabled:
            self._bot_task = asyncio.create_task(self.bot_handler.start_polling())
            logger.info("Bot 查询功能已启动")

        # 定时清理过期数据
        asyncio.create_task(self._cleanup_task())

        # 启动 WebSocket 订阅（阻塞）
        logger.info("开始订阅 Binance 行情...")
        await self.binance.subscribe_all_tickers(self._handle_tickers)

    async def stop(self):
        """停止监控"""
        logger.info("正在停止监控系统...")
        self._running = False

        # 取消 OI 轮询
        if self._oi_task:
            self._oi_task.cancel()
            try:
                await self._oi_task
            except asyncio.CancelledError:
                pass

        # 取消现货价格轮询
        if self._spot_price_task:
            self._spot_price_task.cancel()
            try:
                await self._spot_price_task
            except asyncio.CancelledError:
                pass

        # 取消 Bot 轮询
        if self._bot_task:
            self._bot_task.cancel()
            try:
                await self._bot_task
            except asyncio.CancelledError:
                pass
            await self.bot_handler.close()

        # 关闭连接
        await self.binance.close()
        await self.notifier.stop()

        logger.info("监控系统已停止")

    async def _cleanup_task(self):
        """定时清理过期数据"""
        while self._running:
            await asyncio.sleep(300)  # 每5分钟清理一次
            self.tracker.cleanup_old_data(max_age=3600)
            logger.debug("已清理过期数据")

    def _setup_logging(self):
        """配置日志"""
        log_cfg = self.settings.logging

        # 移除默认处理器
        logger.remove()

        # 控制台输出
        logger.add(
            sys.stderr,
            level=log_cfg.level,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>"
        )

        # 文件输出
        if log_cfg.file_output:
            log_path = Path(log_cfg.file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            logger.add(
                str(log_path),
                level=log_cfg.level,
                rotation="10 MB",
                retention="7 days",
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"
            )


async def main():
    """主函数"""
    # 加载配置
    settings = get_config()

    # 检查配置
    if not settings.volume_tiers:
        logger.error("未配置分层阈值 (volume_tiers)，请检查 config/settings.yaml")
        return

    # 创建应用
    app = MonitorApp(settings)

    # 信号处理
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("收到终止信号...")
        asyncio.create_task(app.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await app.start()
    except Exception as e:
        logger.error(f"运行出错: {e}")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
