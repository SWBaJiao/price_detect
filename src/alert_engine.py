"""
异动检测引擎
实现三维度告警：价格变化、成交量突增、持仓量变化
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, List, Optional, TYPE_CHECKING

from loguru import logger

from .config_manager import Settings, VolumeTierConfig
from .models import AlertEvent, AlertType, TickerData
from .price_tracker import PriceTracker

if TYPE_CHECKING:
    from .ml.risk_filter import RiskFilter


class AlertEngine:
    """
    多维度异动检测引擎

    检测维度：
    1. 价格异动 - 时间窗口内涨跌幅超阈值
    2. 成交量突增 - 当前成交量 > N倍历史平均
    3. 持仓量变化 - OI 短期变化率超阈值
    """

    def __init__(
        self,
        settings: Settings,
        tracker: PriceTracker,
        on_alert: Optional[Callable[[AlertEvent], None]] = None
    ):
        self.settings = settings
        self.tracker = tracker
        self.on_alert = on_alert

        # 风险过滤器（可选，用于过滤假异动）
        self._risk_filter: Optional["RiskFilter"] = None

        # 告警冷却记录: {(symbol, alert_type): last_alert_time}
        self._cooldowns: dict = defaultdict(lambda: datetime.min)

        # 按成交额排序的分层配置（从高到低）
        self._tiers = sorted(
            settings.volume_tiers,
            key=lambda t: t.min_quote_volume,
            reverse=True
        )

    def set_risk_filter(self, risk_filter: "RiskFilter"):
        """设置风险过滤器"""
        self._risk_filter = risk_filter
        logger.info("风险过滤器已设置到AlertEngine")

    def _get_tier(self, oi_value: float) -> Optional[VolumeTierConfig]:
        """
        根据持仓价值（现价×持仓量）确定分层

        Args:
            oi_value: 持仓价值(USDT) = 现价 × 持仓量

        Returns:
            匹配的分层配置
        """
        for tier in self._tiers:
            if oi_value >= tier.min_quote_volume:
                return tier
        return None

    def _is_cooled_down(self, symbol: str, alert_type: AlertType) -> bool:
        """检查是否过了冷却期"""
        key = (symbol, alert_type)
        last_alert = self._cooldowns[key]
        cooldown = self.settings.alerts.cooldown

        return datetime.now() - last_alert > timedelta(seconds=cooldown)

    def _record_alert(self, symbol: str, alert_type: AlertType) -> None:
        """记录告警时间（用于冷却）"""
        self._cooldowns[(symbol, alert_type)] = datetime.now()

    def _should_filter(self, symbol: str) -> bool:
        """检查是否应该过滤该交易对"""
        filter_cfg = self.settings.filter
        mode = filter_cfg.mode

        if mode == "whitelist":
            return symbol not in filter_cfg.whitelist
        elif mode == "blacklist":
            return symbol in filter_cfg.blacklist

        return False

    # ==================== 检测逻辑 ====================

    def check_price_change(self, symbol: str) -> Optional[AlertEvent]:
        """检测价格异动"""
        if not self.settings.alerts.price_change.enabled:
            return None

        if self._should_filter(symbol):
            return None

        if not self._is_cooled_down(symbol, AlertType.PRICE_CHANGE):
            return None

        # 获取变化数据
        change_data = self.tracker.get_price_change(symbol)
        if change_data is None:
            return None

        change_percent, window_low, window_high = change_data

        # 获取分层阈值（基于持仓价值）
        oi_value = self.tracker.get_oi_value(symbol)
        tier = self._get_tier(oi_value)
        if tier is None:
            return None

        # 检查是否超阈值（绝对值）
        if abs(change_percent) < tier.price_threshold:
            return None

        # 获取当前价格
        latest = self.tracker.get_latest(symbol)
        if latest is None:
            return None

        # 触发告警
        event = AlertEvent(
            symbol=symbol,
            alert_type=AlertType.PRICE_CHANGE,
            tier_label=tier.label,
            current_price=latest.price,
            change_percent=change_percent,
            threshold=tier.price_threshold,
            time_window=self.settings.alerts.price_change.time_window,
            extra_info={
                "窗口最低": f"${window_low:.4f}",
                "窗口最高": f"${window_high:.4f}",
                "持仓价值": f"${oi_value:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.PRICE_CHANGE)
        return event

    def check_volume_spike(self, symbol: str) -> Optional[AlertEvent]:
        """检测成交量突增"""
        if not self.settings.alerts.volume_spike.enabled:
            return None

        if self._should_filter(symbol):
            return None

        if not self._is_cooled_down(symbol, AlertType.VOLUME_SPIKE):
            return None

        # 获取成交量倍数
        volume_ratio = self.tracker.get_volume_ratio(symbol)
        if volume_ratio is None:
            return None

        # 获取分层阈值（基于持仓价值）
        oi_value = self.tracker.get_oi_value(symbol)
        tier = self._get_tier(oi_value)
        if tier is None:
            return None

        # 检查是否超阈值
        if volume_ratio < tier.volume_threshold:
            return None

        latest = self.tracker.get_latest(symbol)
        if latest is None:
            return None

        # 触发告警
        event = AlertEvent(
            symbol=symbol,
            alert_type=AlertType.VOLUME_SPIKE,
            tier_label=tier.label,
            current_price=latest.price,
            change_percent=0,  # 成交量不用这个字段
            threshold=tier.volume_threshold,
            time_window=self.settings.alerts.price_change.time_window,
            extra_info={
                "成交量倍数": f"{volume_ratio:.1f}x",
                "持仓价值": f"${oi_value:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.VOLUME_SPIKE)
        return event

    def check_oi_change(self, symbol: str) -> Optional[AlertEvent]:
        """检测持仓量变化"""
        if not self.settings.alerts.open_interest.enabled:
            return None

        if self._should_filter(symbol):
            return None

        if not self._is_cooled_down(symbol, AlertType.OI_CHANGE):
            return None

        # 获取 OI 变化
        oi_change = self.tracker.get_oi_change(symbol)
        if oi_change is None:
            return None

        # 获取分层阈值（基于持仓价值）
        oi_value = self.tracker.get_oi_value(symbol)
        tier = self._get_tier(oi_value)
        if tier is None:
            return None

        # 检查是否超阈值（绝对值）
        if abs(oi_change) < tier.oi_threshold:
            return None

        latest = self.tracker.get_latest(symbol)
        if latest is None:
            return None

        # 触发告警
        event = AlertEvent(
            symbol=symbol,
            alert_type=AlertType.OI_CHANGE,
            tier_label=tier.label,
            current_price=latest.price,
            change_percent=oi_change,
            threshold=tier.oi_threshold,
            time_window=self.settings.alerts.open_interest.time_window,
            extra_info={
                "当前持仓量": f"{latest.open_interest:,.0f}" if latest.open_interest else "N/A",
                "持仓价值": f"${oi_value:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.OI_CHANGE)
        return event

    def check_spot_futures_spread(self, symbol: str) -> Optional[AlertEvent]:
        """检测现货-合约价差"""
        if not self.settings.alerts.spot_futures_spread.enabled:
            return None

        if self._should_filter(symbol):
            return None

        if not self._is_cooled_down(symbol, AlertType.SPOT_FUTURES_SPREAD):
            return None

        # 获取价差数据
        spread_data = self.tracker.get_spot_futures_spread(symbol)
        if spread_data is None:
            return None

        spread_percent, spot_price, futures_price = spread_data

        # 获取分层阈值（基于持仓价值）
        oi_value = self.tracker.get_oi_value(symbol)
        tier = self._get_tier(oi_value)
        if tier is None:
            return None

        # 检查是否超阈值（绝对值）
        threshold = tier.spread_threshold
        if abs(spread_percent) < threshold:
            return None

        # 触发告警
        event = AlertEvent(
            symbol=symbol,
            alert_type=AlertType.SPOT_FUTURES_SPREAD,
            tier_label=tier.label,
            current_price=futures_price,
            change_percent=spread_percent,
            threshold=threshold,
            time_window=self.settings.alerts.spot_futures_spread.time_window,
            extra_info={
                "现货价格": f"${spot_price:.4f}",
                "合约价格": f"${futures_price:.4f}",
                "持仓价值": f"${oi_value:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.SPOT_FUTURES_SPREAD)
        return event

    def check_price_reversal(self, symbol: str) -> Optional[AlertEvent]:
        """检测价格反转（见顶/见底反转）"""
        if not self.settings.alerts.price_reversal.enabled:
            return None

        if self._should_filter(symbol):
            return None

        if not self._is_cooled_down(symbol, AlertType.PRICE_REVERSAL):
            return None

        # 获取反转数据
        time_window = self.settings.alerts.price_reversal.time_window
        reversal_data = self.tracker.get_price_reversal(symbol, time_window)
        if reversal_data is None:
            return None

        # 获取分层阈值（基于持仓价值）
        oi_value = self.tracker.get_oi_value(symbol)
        tier = self._get_tier(oi_value)
        if tier is None:
            return None

        rise_percent = reversal_data["rise_percent"]
        fall_percent = reversal_data["fall_percent"]
        threshold = tier.price_threshold

        # 判断是否超过阈值（涨幅和跌幅都需要超过阈值才算有效反转）
        if rise_percent < threshold or fall_percent < threshold:
            return None

        reversal_type = reversal_data["type"]
        current_price = reversal_data["current_price"]

        # 构建附加信息
        if reversal_type == "top":
            extreme_price = reversal_data["high_price"]
        else:
            extreme_price = reversal_data["low_price"]

        # 触发告警
        event = AlertEvent(
            symbol=symbol,
            alert_type=AlertType.PRICE_REVERSAL,
            tier_label=tier.label,
            current_price=current_price,
            change_percent=rise_percent if reversal_type == "bottom" else -fall_percent,
            threshold=threshold,
            time_window=time_window,
            extra_info={
                "反转类型": reversal_type,
                "起始价": reversal_data["start_price"],
                "极值价": extreme_price,
                "上涨幅度": rise_percent,
                "下跌幅度": fall_percent,
                "持仓价值": f"${oi_value:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.PRICE_REVERSAL)
        return event

    def check_all(self, symbol: str) -> List[AlertEvent]:
        """
        检查所有类型的异动

        Returns:
            触发的告警事件列表
        """
        events = []

        # 价格异动
        price_event = self.check_price_change(symbol)
        if price_event:
            events.append(price_event)
            logger.info(f"[价格异动] {symbol}: {price_event.change_percent:+.2f}%")

        # 成交量突增
        volume_event = self.check_volume_spike(symbol)
        if volume_event:
            events.append(volume_event)
            logger.info(f"[成交量突增] {symbol}: {volume_event.extra_info.get('成交量倍数')}")

        # 持仓量变化
        oi_event = self.check_oi_change(symbol)
        if oi_event:
            events.append(oi_event)
            logger.info(f"[持仓量变化] {symbol}: {oi_event.change_percent:+.2f}%")

        # 现货-合约价差
        spread_event = self.check_spot_futures_spread(symbol)
        if spread_event:
            events.append(spread_event)
            direction = "现货溢价" if spread_event.change_percent > 0 else "合约溢价"
            logger.info(f"[现货-合约价差] {symbol}: {spread_event.change_percent:+.2f}% ({direction})")

        # 价格反转
        reversal_event = self.check_price_reversal(symbol)
        if reversal_event:
            events.append(reversal_event)
            reversal_type = "见顶反转" if reversal_event.extra_info.get("反转类型") == "top" else "见底反转"
            logger.info(f"[价格反转] {symbol}: {reversal_type} (涨{reversal_event.extra_info.get('上涨幅度', 0):.2f}%/跌{reversal_event.extra_info.get('下跌幅度', 0):.2f}%)")

        # 回调通知（集成风险过滤）
        if self.on_alert:
            for event in events:
                try:
                    # 风险过滤检查
                    should_send = self._check_risk_filter(event.symbol, event.alert_type)

                    if should_send:
                        self.on_alert(event)
                    else:
                        logger.info(f"[风险过滤] {event.symbol} {event.alert_type.value} 告警已过滤")

                except Exception as e:
                    logger.error(f"告警回调失败: {e}")

        return events

    def _check_risk_filter(self, symbol: str, alert_type: AlertType) -> bool:
        """
        检查是否应该发送告警（风险过滤）

        Args:
            symbol: 交易对
            alert_type: 告警类型

        Returns:
            True=应该发送, False=应该过滤
        """
        if not self._risk_filter:
            return True

        try:
            # 获取当前ticker数据
            ticker = self.tracker.get_latest(symbol)

            # 执行风险检查
            risk_result = self._risk_filter.check_risk(
                symbol=symbol,
                ticker=ticker
            )

            # 判断是否应该过滤
            should_filter, filter_reason = self._risk_filter.should_filter_alert(risk_result)

            if should_filter:
                logger.debug(f"风险过滤触发 {symbol}: {filter_reason}")
                return False

            return True

        except Exception as e:
            logger.error(f"风险检查失败 {symbol}: {e}")
            return True  # 失败时不过滤，保守处理

    async def process_tickers(self, tickers: List[TickerData]) -> List[AlertEvent]:
        """
        处理一批行情数据

        Args:
            tickers: 行情数据列表

        Returns:
            触发的告警事件列表
        """
        # 先更新追踪器
        self.tracker.batch_update(tickers)

        # 检查所有币种
        all_events = []
        for ticker in tickers:
            events = self.check_all(ticker.symbol)
            all_events.extend(events)

        return all_events
