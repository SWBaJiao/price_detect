"""
合约价格异动监控系统
主程序入口
"""
import asyncio
import signal
import sys
from datetime import datetime
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

# ML量化模块
from src.ml import MLDataStore, FeatureEngine, LabelGenerator, RiskFilter, RiskConfig

# 模拟交易模块
from src.ml.trading import (
    RealtimeSimEngine, RealtimeConfig,
    TradingDataStore,
    AccountConfig, StrategyConfig, StopLossConfig
)

# Web仪表板模块
from src.web import create_app, run_web_server


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

        # ==================== ML量化模块初始化 ====================
        self.ml_enabled = settings.ml.enabled
        self.ml_data_store: Optional[MLDataStore] = None
        self.ml_feature_engine: Optional[FeatureEngine] = None
        self.ml_label_generator: Optional[LabelGenerator] = None
        self.ml_risk_filter: Optional[RiskFilter] = None

        if self.ml_enabled:
            self._init_ml_modules()

        # ==================== 模拟交易模块初始化 ====================
        self.trading_enabled = settings.trading.enabled
        self.trading_engine: Optional[RealtimeSimEngine] = None
        self.trading_store: Optional[TradingDataStore] = None

        if self.trading_enabled:
            self._init_trading_modules()

        # 初始化Web服务器
        self._init_web_server()

        # 最新特征缓存（用于交易引擎）
        self._last_features: dict = {}

        # OI 轮询任务
        self._oi_task = None
        # 现货价格轮询任务
        self._spot_price_task = None
        # Bot 命令处理任务
        self._bot_task = None
        # 订单簿 WebSocket 任务
        self._orderbook_task = None
        # ML标签生成任务
        self._ml_label_task = None
        # ML特征保存时间记录
        self._ml_last_save: datetime = datetime.min

    def _on_alert(self, event: AlertEvent):
        """告警回调"""
        logger.info(f"[告警回调] 收到告警事件: {event.symbol} {event.alert_type.value}")
        try:
            task = asyncio.create_task(self.notifier.notify(event))
            logger.info(f"[告警回调] 创建通知任务成功: {task}")
        except Exception as e:
            logger.error(f"[告警回调] 创建通知任务失败: {e}")

    def _on_orderbook_event(self, event: OrderBookEvent):
        """订单簿事件回调"""
        # 转换为标准告警事件并发送
        alert = create_orderbook_alert(event, tier_label="订单簿")
        asyncio.create_task(self.notifier.notify(alert))

    def _init_ml_modules(self):
        """初始化ML量化模块"""
        ml_cfg = self.settings.ml

        # 确保数据目录存在
        db_path = Path(ml_cfg.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # 初始化数据存储
        self.ml_data_store = MLDataStore(str(db_path))
        logger.info(f"ML数据存储初始化: {db_path}")

        # 初始化风险过滤器
        risk_cfg = ml_cfg.risk
        self.ml_risk_filter = RiskFilter(
            config=RiskConfig(
                enabled=risk_cfg.enabled,
                filter_alerts=risk_cfg.filter_alerts,
                max_ws_latency_ms=risk_cfg.max_ws_latency_ms,
                max_spread_bps=risk_cfg.max_spread_bps,
                min_depth_value=risk_cfg.min_depth_value,
                fake_signal_window=risk_cfg.fake_signal_window,
                fake_signal_revert_ratio=risk_cfg.fake_signal_revert_ratio,
                fake_signal_min_change=risk_cfg.fake_signal_min_change
            ),
            tracker=self.tracker,
            orderbook_monitor=self.orderbook_monitor
        )

        # 设置AlertEngine的风险过滤器
        self.alert_engine.set_risk_filter(self.ml_risk_filter)
        logger.info("风险过滤器已集成到告警引擎")

        # 初始化特征工程引擎
        ind_cfg = ml_cfg.indicators
        indicator_config = {
            'ma_periods': ind_cfg.ma_periods,
            'rsi_period': ind_cfg.rsi_period,
            'macd_fast': ind_cfg.macd_fast,
            'macd_slow': ind_cfg.macd_slow,
            'macd_signal': ind_cfg.macd_signal,
            'bb_period': ind_cfg.bb_period,
            'bb_std': ind_cfg.bb_std
        }
        self.ml_feature_engine = FeatureEngine(
            tracker=self.tracker,
            orderbook_monitor=self.orderbook_monitor,
            indicator_config=indicator_config
        )
        logger.info("特征工程引擎初始化完成")

        # 初始化标签生成器
        label_cfg = ml_cfg.label
        self.ml_label_generator = LabelGenerator(
            tracker=self.tracker,
            data_store=self.ml_data_store,
            direction_threshold=label_cfg.direction_threshold
        )
        logger.info("标签生成器初始化完成")

    def _init_trading_modules(self):
        """初始化模拟交易模块"""
        trading_cfg = self.settings.trading
        ml_cfg = self.settings.ml

        # 确保数据目录存在
        db_path = Path(ml_cfg.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # 初始化交易数据存储
        self.trading_store = TradingDataStore(str(db_path))
        logger.info("交易数据存储初始化完成")

        # 配置账户
        account_cfg = trading_cfg.account
        account_config = AccountConfig(
            initial_balance=account_cfg.initial_balance,
            leverage=account_cfg.leverage,
            maker_fee=account_cfg.maker_fee,
            taker_fee=account_cfg.taker_fee,
            max_positions=account_cfg.max_positions,
            position_risk_pct=account_cfg.position_risk_pct
        )

        # 配置策略
        strategy_cfg = trading_cfg.strategy
        strategy_config = StrategyConfig(
            min_confidence=strategy_cfg.min_confidence,
            signal_threshold=strategy_cfg.signal_threshold,
            use_ml_model=strategy_cfg.use_ml_model,
            indicator_filter=strategy_cfg.indicator_filter,
            rsi_oversold=strategy_cfg.rsi_oversold,
            rsi_overbought=strategy_cfg.rsi_overbought,
            min_volatility=strategy_cfg.min_volatility,
            min_volume_ratio=strategy_cfg.min_volume_ratio,
            imbalance_long_threshold=strategy_cfg.imbalance_long_threshold,
            imbalance_short_threshold=strategy_cfg.imbalance_short_threshold,
            trend_filter_pct=strategy_cfg.trend_filter_pct
        )

        # 配置止损
        stop_loss_cfg = trading_cfg.stop_loss
        stop_loss_config = StopLossConfig(
            method=stop_loss_cfg.method,
            fixed_stop_pct=stop_loss_cfg.fixed_stop_pct,
            take_profit_pct=stop_loss_cfg.take_profit_pct,
            atr_multiplier=stop_loss_cfg.atr_multiplier,
            atr_period=stop_loss_cfg.atr_period,
            trailing_distance=stop_loss_cfg.trailing_distance,
            trailing_activation=stop_loss_cfg.trailing_activation,
            max_hold_seconds=stop_loss_cfg.max_hold_seconds
        )

        # 配置实时模拟
        realtime_cfg = trading_cfg.realtime
        realtime_config = RealtimeConfig(
            enabled=realtime_cfg.enabled,
            save_interval=realtime_cfg.save_interval,
            log_trades=realtime_cfg.log_trades,
            max_positions_per_symbol=realtime_cfg.max_positions_per_symbol,
            allowed_symbols=realtime_cfg.allowed_symbols
        )

        # 初始化实时模拟引擎
        self.trading_engine = RealtimeSimEngine(
            trading_store=self.trading_store,
            account_config=account_config,
            strategy_config=strategy_config,
            stop_loss_config=stop_loss_config,
            realtime_config=realtime_config
        )

        logger.info(
            f"模拟交易引擎初始化完成: "
            f"初始资金=${account_cfg.initial_balance}, "
            f"杠杆={account_cfg.leverage}x, "
            f"模式={trading_cfg.mode}"
        )

    def _init_web_server(self):
        """初始化Web仪表板服务器"""
        # 获取虚拟账户引用（如果交易模块启用）
        virtual_account = None
        if self.trading_enabled and self.trading_engine:
            virtual_account = self.trading_engine.account

        # 创建Flask应用
        self.web_app = create_app(
            trading_store=self.trading_store,
            ml_data_store=self.ml_data_store,
            virtual_account=virtual_account,
            realtime_engine=self.trading_engine
        )

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
        # 原有告警检测逻辑（风险过滤已集成到AlertEngine）
        await self.alert_engine.process_tickers(tickers)

        # ML特征计算和存储（如果启用）
        if self.ml_enabled and self.ml_feature_engine and self.ml_data_store:
            await self._process_ml_features(tickers)

    async def _process_ml_features(self, tickers: List[TickerData]):
        """处理ML特征计算和存储"""
        now = datetime.now()
        save_interval = self.settings.ml.feature.save_interval

        # 检查是否到了保存间隔
        if (now - self._ml_last_save).total_seconds() < save_interval:
            return

        self._ml_last_save = now

        # 批量计算特征并存储
        features_to_save = []
        for ticker in tickers:
            try:
                # 计算特征
                feature = self.ml_feature_engine.compute_features(
                    symbol=ticker.symbol,
                    ticker=ticker
                )

                if feature:
                    features_to_save.append(feature)

                    # 缓存最新特征（用于交易引擎）
                    self._last_features[ticker.symbol] = feature

                    # 注册到标签生成器（用于后续延迟标签生成）
                    if self.ml_label_generator:
                        self.ml_label_generator.register_feature(feature)

                    # 存储价格快照（用于标签回填验证）
                    self.ml_data_store.save_price_snapshot(
                        symbol=ticker.symbol,
                        timestamp=ticker.timestamp,
                        price=ticker.price,
                        volume=ticker.volume
                    )

                    # 调用交易引擎处理（如果启用）
                    if self.trading_enabled and self.trading_engine:
                        await self.trading_engine.on_feature_update(
                            symbol=ticker.symbol,
                            feature=feature,
                            current_price=ticker.price
                        )

            except Exception as e:
                logger.error(f"计算特征失败 {ticker.symbol}: {e}")

        # 批量存储特征
        if features_to_save:
            try:
                self.ml_data_store.save_features_batch(features_to_save)
            except Exception as e:
                logger.error(f"批量存储特征失败: {e}")

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

    async def _generate_ml_labels(self):
        """定时生成ML标签（延迟标签生成，避免未来函数）"""
        interval = 10  # 每10秒尝试生成一次标签

        while self._running:
            try:
                if self.ml_label_generator and self.ml_data_store:
                    # 尝试为所有待标注特征生成标签
                    all_labels = self.ml_label_generator.try_generate_all_labels()

                    # 批量保存标签
                    for symbol, labels in all_labels.items():
                        if labels:
                            self.ml_data_store.save_labels_batch(labels)

                    # 定期输出统计
                    stats = self.ml_label_generator.get_stats()
                    if stats['generated_count'] > 0 and stats['generated_count'] % 100 == 0:
                        logger.info(
                            f"标签生成统计: 已生成={stats['generated_count']}, "
                            f"待标注={stats['pending_total']}, "
                            f"丢弃={stats['dropped_count']}"
                        )

                    # 清理风险过滤器过期数据
                    if self.ml_risk_filter:
                        self.ml_risk_filter.cleanup(max_age_seconds=300)

            except Exception as e:
                logger.error(f"标签生成失败: {e}")

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

        # 启动ML标签生成任务
        if self.ml_enabled:
            self._ml_label_task = asyncio.create_task(self._generate_ml_labels())
            logger.info("ML标签生成任务已启动")

        # 启动模拟交易引擎
        if self.trading_enabled and self.trading_engine:
            if not self.ml_enabled:
                logger.warning("模拟交易依赖ML特征，请同时启用 ml.enabled")
            self.trading_engine.start()
            logger.info("模拟交易引擎已启动")

        # 启动Web仪表板服务器
        self._start_web_server()

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

        # 取消ML标签生成任务
        if self._ml_label_task:
            self._ml_label_task.cancel()
            try:
                await self._ml_label_task
            except asyncio.CancelledError:
                pass

        # 停止模拟交易引擎并输出统计
        if self.trading_enabled and self.trading_engine:
            # 平掉所有持仓
            await self.trading_engine.close_all_positions()
            # 停止引擎（会输出统计信息）
            self.trading_engine.stop()

        # 关闭ML数据存储
        if self.ml_data_store:
            self.ml_data_store.close()
            logger.info("ML数据存储已关闭")

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
