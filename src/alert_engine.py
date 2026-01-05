"""
异动检测引擎
实现三维度告警：价格变化、成交量突增、持仓量变化
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from loguru import logger

from .config_manager import Settings, VolumeTierConfig
from .models import AlertEvent, AlertType, TickerData
from .price_tracker import PriceTracker


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

        # 告警冷却记录: {(symbol, alert_type): last_alert_time}
        self._cooldowns: dict = defaultdict(lambda: datetime.min)

        # 按成交额排序的分层配置（从高到低）
        self._tiers = sorted(
            settings.volume_tiers,
            key=lambda t: t.min_quote_volume,
            reverse=True
        )

    def _get_tier(self, quote_volume: float) -> Optional[VolumeTierConfig]:
        """
        根据 24h 成交额确定分层

        Args:
            quote_volume: 24h 成交额(USDT)

        Returns:
            匹配的分层配置
        """
        for tier in self._tiers:
            if quote_volume >= tier.min_quote_volume:
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

        # 获取分层阈值
        quote_volume = self.tracker.get_quote_volume(symbol)
        tier = self._get_tier(quote_volume)
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
                "24h成交额": f"${quote_volume:,.0f}"
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

        # 获取分层阈值
        quote_volume = self.tracker.get_quote_volume(symbol)
        tier = self._get_tier(quote_volume)
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
                "24h成交额": f"${quote_volume:,.0f}"
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

        # 获取分层阈值
        quote_volume = self.tracker.get_quote_volume(symbol)
        tier = self._get_tier(quote_volume)
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
                "24h成交额": f"${quote_volume:,.0f}"
            }
        )

        self._record_alert(symbol, AlertType.OI_CHANGE)
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

        # 回调通知
        if self.on_alert:
            for event in events:
                try:
                    self.on_alert(event)
                except Exception as e:
                    logger.error(f"告警回调失败: {e}")

        return events

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
