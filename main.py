"""
合约价格异动监控系统
主程序入口
"""
import asyncio
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from src.config_manager import get_config, Settings
from src.binance_client import BinanceClient
from src.price_tracker import PriceTracker
from src.alert_engine import AlertEngine
from src.telegram_bot import TelegramBot, AlertNotifier
from src.bot_handler import BotCommandHandler
from src.models import TickerData, AlertEvent, OrderBookSnapshot, OrderBookEvent
from src.orderbook_monitor import OrderBookMonitor, OrderBookConfig, create_orderbook_alert
from src.risk_filter import RiskFilter, RiskConfig

# Web仪表板模块
from src.web import create_app, run_web_server

# 账户监控与跟单
from src.account_monitor_store import AccountMonitorStore
from src.account_monitor import AccountMonitorService, CopyTradingService


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

        # 订单簿监控器
        orderbook_cfg = settings.alerts.orderbook
        self.orderbook_monitor = OrderBookMonitor(
            config=OrderBookConfig(
                enabled=orderbook_cfg.enabled,
                wall_detection=orderbook_cfg.wall_detection,
                wall_value_threshold=orderbook_cfg.wall_value_threshold,
                wall_ratio_threshold=orderbook_cfg.wall_ratio_threshold,
                wall_distance_max=orderbook_cfg.wall_distance_max,
                imbalance_detection=orderbook_cfg.imbalance_detection,
                imbalance_threshold=orderbook_cfg.imbalance_threshold,
                imbalance_depth_levels=orderbook_cfg.imbalance_depth_levels,
                sweep_detection=orderbook_cfg.sweep_detection,
                sweep_value_threshold=orderbook_cfg.sweep_value_threshold,
                cooldown_seconds=settings.alerts.cooldown
            ),
            on_event=self._on_orderbook_event
        )

        # ==================== 风险过滤器初始化 ====================
        self.risk_filter: Optional[RiskFilter] = None
        if settings.risk.enabled:
            self.risk_filter = RiskFilter(
                config=RiskConfig(
                    enabled=settings.risk.enabled,
                    filter_alerts=settings.risk.filter_alerts,
                    max_ws_latency_ms=settings.risk.max_ws_latency_ms,
                    max_spread_bps=settings.risk.max_spread_bps,
                    min_depth_value=settings.risk.min_depth_value,
                    fake_signal_window=settings.risk.fake_signal_window,
                    fake_signal_revert_ratio=settings.risk.fake_signal_revert_ratio,
                    fake_signal_min_change=settings.risk.fake_signal_min_change
                ),
                tracker=self.tracker,
                orderbook_monitor=self.orderbook_monitor
            )
            self.alert_engine.set_risk_filter(self.risk_filter)
            logger.info("风险过滤器已集成到告警引擎")

        # 初始化Web服务器（含账户监控/跟单存储）
        self.account_store = AccountMonitorStore()
        self._init_web_server()

        # 账户监控与跟单服务
        self._account_monitor_service: Optional[AccountMonitorService] = None
        self._copy_trading_service: Optional[CopyTradingService] = None
        monitor_interval = max(5, self.settings.account_monitor.monitor_poll_interval)
        copy_interval = max(5, self.settings.account_monitor.copy_poll_interval)
        self._account_monitor_service = AccountMonitorService(
            self.account_store,
            on_position_alert=lambda name, msg: asyncio.create_task(self.telegram.send_message(msg)),
            poll_interval_seconds=monitor_interval,
        )
        self._copy_trading_service = CopyTradingService(self.account_store, poll_interval_seconds=copy_interval)

        # OI 轮询任务
        self._oi_task = None
        # 现货价格轮询任务
        self._spot_price_task = None
        # Bot 命令处理任务
        self._bot_task = None
        # 订单簿 WebSocket 任务
        self._orderbook_task = None

    def _on_alert(self, event: AlertEvent):
        """告警回调"""
        # 异步获取资金流数据并发送告警
        asyncio.create_task(self._send_alert_with_money_flow(event))

    async def _send_alert_with_money_flow(self, event: AlertEvent):
        """获取资金流和资金费率数据后发送告警"""
        try:
            # 并发获取资金流和资金费率
            money_flow_task = self.binance.get_money_flow(event.symbol, minutes=5)
            funding_rate_task = self.binance._get_funding_rate(event.symbol)

            money_flow, funding_rate = await asyncio.gather(
                money_flow_task, funding_rate_task,
                return_exceptions=True
            )

            # 处理资金流数据
            if money_flow and not isinstance(money_flow, Exception):
                net_flow = money_flow["net_flow"]
                flow_emoji = "🟢" if net_flow > 0 else "🔴"
                flow_direction = "流入" if net_flow > 0 else "流出"

                # 添加资金流信息
                event.extra_info["资金流向"] = f"{flow_emoji} 净{flow_direction} ${abs(net_flow):,.0f}"
                event.extra_info["5分钟流入"] = f"${money_flow['inflow']:,.0f}"
                event.extra_info["5分钟流出"] = f"${money_flow['outflow']:,.0f}"

            # 处理资金费率数据
            if funding_rate is not None and not isinstance(funding_rate, Exception):
                # 资金费率转换为百分比显示（原始值如0.0001表示0.01%）
                rate_value = funding_rate.get("funding_rate", 0)
                next_funding_time = funding_rate.get("next_funding_time", 0)
                rate_percent = rate_value * 100

                # 判断费率情绪
                if rate_percent > 0.05:
                    rate_emoji = "🔥"  # 费率偏高，市场偏多
                    rate_hint = "偏多"
                elif rate_percent < -0.05:
                    rate_emoji = "❄️"  # 费率偏低，市场偏空
                    rate_hint = "偏空"
                else:
                    rate_emoji = "➖"  # 费率中性
                    rate_hint = "中性"

                # 计算距离下次结算的时间
                if next_funding_time > 0:
                    now_ts = datetime.now(tz=timezone.utc).timestamp() * 1000
                    remaining_ms = next_funding_time - now_ts
                    if remaining_ms > 0:
                        remaining_hours = remaining_ms / (1000 * 60 * 60)
                        remaining_mins = (remaining_ms % (1000 * 60 * 60)) / (1000 * 60)
                        event.extra_info["资金费率"] = f"{rate_emoji} {rate_percent:+.4f}% ({rate_hint}) | 距结算 {int(remaining_hours)}h{int(remaining_mins)}m"
                    else:
                        event.extra_info["资金费率"] = f"{rate_emoji} {rate_percent:+.4f}% ({rate_hint})"
                else:
                    event.extra_info["资金费率"] = f"{rate_emoji} {rate_percent:+.4f}% ({rate_hint})"

        except Exception as e:
            logger.debug(f"获取 {event.symbol} 资金流/费率失败: {e}")

        # 发送告警
        await self.notifier.notify(event)

    def _on_orderbook_event(self, event: OrderBookEvent):
        """订单簿事件回调"""
        # 转换为标准告警事件并发送
        alert = create_orderbook_alert(event, tier_label="订单簿")
        asyncio.create_task(self.notifier.notify(alert))

    def _init_web_server(self):
        """初始化Web仪表板服务器"""
        self.web_app = create_app(account_store=self.account_store)

        logger.info("Web仪表板初始化完成")

    def _start_web_server(self):
        """启动Web服务器（独立线程）"""
        self._web_thread = run_web_server(
            app=self.web_app,
            host='0.0.0.0',
            port=15000,
            debug=False
        )
        logger.info("Web仪表板已启动: http://localhost:15000")

    async def _handle_orderbook(self, snapshot: OrderBookSnapshot):
        """处理订单簿数据"""
        await self.orderbook_monitor.process_snapshot(snapshot)

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

        # 启动Web仪表板服务器
        self._start_web_server()

        # 启动账户监控与跟单服务
        if self._account_monitor_service:
            self._account_monitor_service.start()
        if self._copy_trading_service:
            self._copy_trading_service.start()

        # 启动订单簿监控（如果启用）
        orderbook_cfg = self.settings.alerts.orderbook
        if orderbook_cfg.enabled:
            # 确定要监控的交易对
            symbols = orderbook_cfg.symbols
            if not symbols:
                # 如果没有指定，使用白名单或默认的大盘币
                if self.settings.filter.mode == "whitelist":
                    symbols = self.settings.filter.whitelist
                else:
                    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

            logger.info(f"准备启动订单簿监控: {symbols}")
            self._orderbook_task = asyncio.create_task(
                self.binance.subscribe_orderbook(
                    symbols=symbols,
                    callback=self._handle_orderbook,
                    depth_levels=orderbook_cfg.depth_levels,
                    update_speed=orderbook_cfg.update_speed
                )
            )
            logger.info(f"订单簿监控已启动: {len(symbols)} 个交易对")

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

        # 取消订单簿监控
        if self._orderbook_task:
            self._orderbook_task.cancel()
            try:
                await self._orderbook_task
            except asyncio.CancelledError:
                pass

        # 定期清理风险过滤器（若启用）
        if self.risk_filter:
            self.risk_filter.cleanup(max_age_seconds=300)

        # 停止账户监控与跟单服务
        if self._account_monitor_service:
            self._account_monitor_service.stop()
        if self._copy_trading_service:
            self._copy_trading_service.stop()

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

        # 文件输出（按日期分割）
        if log_cfg.file_output:
            log_path = Path(log_cfg.file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # 构建带日期的日志文件名: logs/monitor_2024-01-07.log
            log_dir = log_path.parent
            log_name = log_path.stem  # "monitor"
            log_ext = log_path.suffix  # ".log"
            daily_log_path = log_dir / f"{log_name}_{{time:YYYY-MM-DD}}{log_ext}"

            logger.add(
                str(daily_log_path),
                level=log_cfg.level,
                rotation="00:00",       # 每天午夜轮转
                retention="30 days",    # 保留30天日志
                compression="gz",       # 旧日志压缩为 .gz
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
