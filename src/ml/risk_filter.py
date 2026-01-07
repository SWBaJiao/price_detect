"""
风险过滤器
检测假异动、延迟问题、市场操纵等风险
"""
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from loguru import logger

from ..models import OrderBookSnapshot, RiskCheckResult, TickerData

if TYPE_CHECKING:
    from ..price_tracker import PriceTracker
    from ..orderbook_monitor import OrderBookMonitor


@dataclass
class RiskConfig:
    """风险过滤配置"""

    # 是否启用风险过滤
    enabled: bool = True

    # 是否在检测到假异动时过滤告警
    filter_alerts: bool = True

    # 延迟阈值
    max_ws_latency_ms: float = 500       # WebSocket最大延迟(毫秒)
    max_data_age_ms: float = 2000        # 数据最大年龄(毫秒)

    # 流动性阈值
    min_depth_value: float = 50000       # 最小深度价值(USDT)
    max_spread_bps: float = 50           # 最大价差(基点)

    # 假异动检测
    fake_signal_window: int = 30         # 假异动检测窗口(秒)
    fake_signal_revert_ratio: float = 0.8  # 反转比例阈值（回撤/涨幅 > 80%）
    fake_signal_min_change: float = 1.0  # 最小变化幅度(%)才检测

    # 操纵检测
    wall_flash_window: int = 10          # 闪单检测窗口(秒)
    wall_flash_count: int = 3            # 闪单次数阈值
    volume_spike_ratio: float = 5.0      # 成交量峰值倍数阈值


class RiskFilter:
    """
    风险过滤器

    检测维度：
    1. 延迟风险 - WebSocket/数据延迟
    2. 流动性风险 - 深度不足/价差过大
    3. 假异动 - 快速反转的价格变化
    4. 操纵风险 - 闪单/对敲
    """

    def __init__(
        self,
        config: RiskConfig,
        tracker: "PriceTracker",
        orderbook_monitor: Optional["OrderBookMonitor"] = None
    ):
        self.config = config
        self.tracker = tracker
        self.orderbook_monitor = orderbook_monitor

        # 墙体出现/消失记录（用于闪单检测）
        # {symbol: deque[(timestamp, event_type)]}
        self._wall_events: Dict[str, deque] = {}

        # 已检测到的假异动（用于冷却）
        # {symbol: last_fake_signal_time}
        self._fake_signal_cooldown: Dict[str, datetime] = {}

        # 统计
        self._stats = {
            'total_checks': 0,
            'fake_signals': 0,
            'latency_issues': 0,
            'liquidity_issues': 0,
            'manipulation_detected': 0
        }

        logger.info(f"风险过滤器初始化: enabled={config.enabled}, filter_alerts={config.filter_alerts}")

    def check_risk(
        self,
        symbol: str,
        ticker: Optional[TickerData] = None,
        snapshot: Optional[OrderBookSnapshot] = None,
        ws_receive_time: Optional[datetime] = None
    ) -> RiskCheckResult:
        """
        执行全面风险检查

        Args:
            symbol: 交易对
            ticker: 行情数据（可选）
            snapshot: 订单簿快照（可选）
            ws_receive_time: WebSocket接收时间（可选）

        Returns:
            RiskCheckResult 包含各项风险指标
        """
        self._stats['total_checks'] += 1
        now = datetime.now()

        # 初始化结果
        result = RiskCheckResult(
            symbol=symbol,
            timestamp=now
        )

        if not self.config.enabled:
            return result

        # 1. 延迟检查
        if ticker:
            result.ws_latency_ms = self._check_latency(ticker, ws_receive_time)
            result.data_age_ms = (now - ticker.timestamp).total_seconds() * 1000

            if result.ws_latency_ms > self.config.max_ws_latency_ms:
                self._stats['latency_issues'] += 1

        # 2. 流动性检查
        spread_wide, depth_thin = self._check_liquidity(symbol, snapshot)
        result.spread_too_wide = spread_wide
        result.depth_too_thin = depth_thin

        if spread_wide or depth_thin:
            self._stats['liquidity_issues'] += 1

        # 3. 假异动检测
        is_fake, fake_reason = self._check_fake_signal(symbol)
        result.is_fake_signal = is_fake
        result.fake_reason = fake_reason

        if is_fake:
            self._stats['fake_signals'] += 1
            self._fake_signal_cooldown[symbol] = now

        # 4. 操纵检测
        result.wall_manipulation = self._check_wall_manipulation(symbol)
        result.volume_manipulation = self._check_volume_manipulation(symbol)

        if result.wall_manipulation or result.volume_manipulation:
            self._stats['manipulation_detected'] += 1

        return result

    def _check_latency(
        self,
        ticker: TickerData,
        ws_receive_time: Optional[datetime]
    ) -> float:
        """检查WebSocket延迟"""
        if not ws_receive_time:
            return 0.0

        return (ws_receive_time - ticker.timestamp).total_seconds() * 1000

    def _check_liquidity(
        self,
        symbol: str,
        snapshot: Optional[OrderBookSnapshot]
    ) -> Tuple[bool, bool]:
        """
        检查流动性

        Returns:
            (spread_too_wide, depth_too_thin)
        """
        spread_wide = False
        depth_thin = False

        if snapshot:
            # 从快照直接计算
            spread_bps = (snapshot.spread_percent or 0) * 100
            total_depth = snapshot.bid_depth(10) + snapshot.ask_depth(10)

            spread_wide = spread_bps > self.config.max_spread_bps
            depth_thin = total_depth < self.config.min_depth_value * 2

        elif self.orderbook_monitor:
            # 尝试从订单簿监控器获取
            try:
                if hasattr(self.orderbook_monitor, 'get_depth_info'):
                    depth_info = self.orderbook_monitor.get_depth_info(symbol)
                    if depth_info:
                        spread_bps = (depth_info.get("spread_percent", 0) or 0) * 100
                        total_depth = depth_info.get("bid_depth", 0) + depth_info.get("ask_depth", 0)

                        spread_wide = spread_bps > self.config.max_spread_bps
                        depth_thin = total_depth < self.config.min_depth_value * 2
            except Exception:
                pass

        return spread_wide, depth_thin

    def _check_fake_signal(self, symbol: str) -> Tuple[bool, Optional[str]]:
        """
        检测假异动（价格快速反转）

        策略：
        - 价格在短时间内大幅变化后快速回归
        - 典型的假突破模式

        Returns:
            (is_fake, reason)
        """
        tracker = self.tracker._trackers.get(symbol)
        if not tracker or len(tracker.price_history) < 10:
            return False, None

        now = datetime.now()
        window_start = now - timedelta(seconds=self.config.fake_signal_window)

        # 获取窗口内价格
        window_prices = [
            p for p in tracker.price_history
            if p.timestamp >= window_start
        ]

        if len(window_prices) < 5:
            return False, None

        # 计算价格路径
        start_price = window_prices[0].price
        current_price = window_prices[-1].price

        prices = [p.price for p in window_prices]
        high = max(prices)
        low = min(prices)

        if start_price == 0:
            return False, None

        # 检测模式1：冲高回落（假涨）
        rise_to_high = ((high - start_price) / start_price) * 100
        fall_from_high = ((high - current_price) / high) * 100 if high > 0 else 0

        if rise_to_high > self.config.fake_signal_min_change:
            revert_ratio = fall_from_high / rise_to_high if rise_to_high > 0 else 0
            if revert_ratio > self.config.fake_signal_revert_ratio:
                return True, f"冲高回落: 涨{rise_to_high:.2f}%后回落{fall_from_high:.2f}%"

        # 检测模式2：急跌反弹（假跌）
        fall_to_low = ((start_price - low) / start_price) * 100
        rise_from_low = ((current_price - low) / low) * 100 if low > 0 else 0

        if fall_to_low > self.config.fake_signal_min_change:
            revert_ratio = rise_from_low / fall_to_low if fall_to_low > 0 else 0
            if revert_ratio > self.config.fake_signal_revert_ratio:
                return True, f"急跌反弹: 跌{fall_to_low:.2f}%后反弹{rise_from_low:.2f}%"

        return False, None

    def _check_wall_manipulation(self, symbol: str) -> bool:
        """
        检测挂单墙操纵（闪单）

        策略：
        - 大单墙频繁出现/消失
        - 短时间内多次闪现
        """
        if symbol not in self._wall_events:
            return False

        events = self._wall_events[symbol]
        now = datetime.now()
        window_start = now - timedelta(seconds=self.config.wall_flash_window)

        # 统计窗口内的墙体事件
        recent_events = [e for ts, e in events if ts >= window_start]

        # 闪单判定：短时间内多次出现+消失
        appear_count = sum(1 for e in recent_events if e == 'appear')
        disappear_count = sum(1 for e in recent_events if e == 'disappear')

        # 如果出现和消失都超过阈值，可能是闪单
        if appear_count >= self.config.wall_flash_count and disappear_count >= self.config.wall_flash_count:
            return True

        return False

    def _check_volume_manipulation(self, symbol: str) -> bool:
        """
        检测成交量操纵

        策略：
        - 孤立的成交量峰值（无后续跟随）
        - 成交量与价格变化不匹配
        """
        tracker = self.tracker._trackers.get(symbol)
        if not tracker or len(tracker.price_history) < 20:
            return False

        # 获取最近成交量
        recent = list(tracker.price_history)[-20:]
        volumes = [p.volume for p in recent]

        if not volumes or sum(volumes) == 0:
            return False

        avg_volume = sum(volumes) / len(volumes)
        max_volume = max(volumes)
        max_idx = volumes.index(max_volume)

        # 检测孤立峰值
        if max_volume > avg_volume * self.config.volume_spike_ratio:
            # 检查峰值前后是否有跟随
            start_idx = max(0, max_idx - 3)
            end_idx = min(len(volumes), max_idx + 4)

            before_avg = sum(volumes[start_idx:max_idx]) / max(1, max_idx - start_idx) if max_idx > start_idx else avg_volume
            after_avg = sum(volumes[max_idx + 1:end_idx]) / max(1, end_idx - max_idx - 1) if end_idx > max_idx + 1 else avg_volume

            # 前后都没有跟随，可能是操纵
            if before_avg < avg_volume * 1.5 and after_avg < avg_volume * 1.5:
                return True

        return False

    def record_wall_event(self, symbol: str, event_type: str):
        """
        记录墙体事件（供外部调用）

        Args:
            symbol: 交易对
            event_type: "appear" 或 "disappear"
        """
        if symbol not in self._wall_events:
            self._wall_events[symbol] = deque(maxlen=100)

        self._wall_events[symbol].append((datetime.now(), event_type))

    def should_filter_alert(self, risk_result: RiskCheckResult) -> Tuple[bool, str]:
        """
        判断是否应该过滤该告警

        Args:
            risk_result: 风险检查结果

        Returns:
            (should_filter, reason)
        """
        if not self.config.filter_alerts:
            return False, ""

        reasons = risk_result.get_filter_reasons()
        if reasons:
            return True, "; ".join(reasons)

        return False, ""

    def is_in_cooldown(self, symbol: str, cooldown_seconds: int = 60) -> bool:
        """
        检查交易对是否在假异动冷却期内

        Args:
            symbol: 交易对
            cooldown_seconds: 冷却时间（秒）

        Returns:
            是否在冷却期
        """
        last_fake = self._fake_signal_cooldown.get(symbol)
        if not last_fake:
            return False

        elapsed = (datetime.now() - last_fake).total_seconds()
        return elapsed < cooldown_seconds

    def get_stats(self) -> dict:
        """获取统计信息"""
        return self._stats.copy()

    def reset_stats(self):
        """重置统计"""
        self._stats = {
            'total_checks': 0,
            'fake_signals': 0,
            'latency_issues': 0,
            'liquidity_issues': 0,
            'manipulation_detected': 0
        }

    def cleanup(self, max_age_seconds: int = 300):
        """清理过期的事件记录"""
        cutoff = datetime.now() - timedelta(seconds=max_age_seconds)

        # 清理墙体事件
        for symbol in list(self._wall_events.keys()):
            events = self._wall_events[symbol]
            self._wall_events[symbol] = deque(
                [(ts, e) for ts, e in events if ts > cutoff],
                maxlen=100
            )
            if not self._wall_events[symbol]:
                del self._wall_events[symbol]

        # 清理冷却记录
        for symbol in list(self._fake_signal_cooldown.keys()):
            if self._fake_signal_cooldown[symbol] < cutoff:
                del self._fake_signal_cooldown[symbol]


class AlertRiskIntegrator:
    """
    告警风险集成器

    用于在告警流程中集成风险过滤
    """

    def __init__(self, risk_filter: RiskFilter, data_store=None):
        self.risk_filter = risk_filter
        self.data_store = data_store

    def process_alert(
        self,
        symbol: str,
        alert_type: str,
        ticker: Optional[TickerData] = None,
        snapshot: Optional[OrderBookSnapshot] = None
    ) -> Tuple[bool, Optional[RiskCheckResult]]:
        """
        处理告警，检查风险并决定是否过滤

        Args:
            symbol: 交易对
            alert_type: 告警类型
            ticker: 行情数据
            snapshot: 订单簿快照

        Returns:
            (should_send, risk_result)
            should_send: 是否应该发送告警
            risk_result: 风险检查结果
        """
        # 执行风险检查
        risk_result = self.risk_filter.check_risk(
            symbol=symbol,
            ticker=ticker,
            snapshot=snapshot
        )

        # 判断是否过滤
        should_filter, filter_reason = self.risk_filter.should_filter_alert(risk_result)

        # 记录到数据库
        if self.data_store and should_filter:
            try:
                from datetime import datetime
                self.data_store.save_alert(
                    symbol=symbol,
                    timestamp=datetime.now(),
                    alert_type=alert_type,
                    was_filtered=True,
                    filter_reason=filter_reason
                )
            except Exception as e:
                logger.error(f"保存过滤告警记录失败: {e}")

        should_send = not should_filter

        if should_filter:
            logger.info(f"[风险过滤] {symbol} {alert_type}: {filter_reason}")

        return should_send, risk_result
