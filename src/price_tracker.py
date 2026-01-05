"""
价格追踪器
使用滑动窗口存储历史数据，计算变化率
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .models import PricePoint, TickerData


@dataclass
class SymbolTracker:
    """
    单个交易对的追踪器
    使用 deque 实现时间窗口滑动
    """
    # 价格历史（用于价格异动检测）
    price_history: deque = field(default_factory=lambda: deque(maxlen=1000))

    # 成交量历史（用于成交量突增检测）
    volume_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # 持仓量历史（用于 OI 变化检测）
    oi_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # 最新数据缓存
    latest_price: float = 0.0
    latest_volume: float = 0.0
    latest_quote_volume: float = 0.0
    latest_oi: float = 0.0
    last_update: Optional[datetime] = None


class PriceTracker:
    """
    多交易对价格追踪器
    管理所有交易对的历史数据和变化率计算
    """

    def __init__(
        self,
        price_window: int = 60,      # 价格检测窗口（秒）
        volume_periods: int = 10,    # 成交量对比周期数
        oi_window: int = 300         # OI 检测窗口（秒）
    ):
        self.price_window = price_window
        self.volume_periods = volume_periods
        self.oi_window = oi_window

        # symbol -> SymbolTracker
        self._trackers: Dict[str, SymbolTracker] = defaultdict(SymbolTracker)

    def update(self, ticker: TickerData) -> None:
        """
        更新交易对数据

        Args:
            ticker: 最新行情数据
        """
        tracker = self._trackers[ticker.symbol]

        # 记录价格点
        price_point = PricePoint(
            price=ticker.price,
            volume=ticker.volume,
            timestamp=ticker.timestamp
        )
        tracker.price_history.append(price_point)

        # 更新最新值
        tracker.latest_price = ticker.price
        tracker.latest_volume = ticker.volume
        tracker.latest_quote_volume = ticker.quote_volume
        tracker.last_update = ticker.timestamp

        # 如果有持仓量数据
        if ticker.open_interest is not None:
            tracker.latest_oi = ticker.open_interest
            tracker.oi_history.append((ticker.timestamp, ticker.open_interest))

    def update_oi(self, symbol: str, oi: float, timestamp: datetime = None) -> None:
        """单独更新持仓量（来自 REST API）"""
        if timestamp is None:
            timestamp = datetime.now()

        tracker = self._trackers[symbol]
        tracker.latest_oi = oi
        tracker.oi_history.append((timestamp, oi))

    def batch_update(self, tickers: List[TickerData]) -> None:
        """批量更新"""
        for ticker in tickers:
            self.update(ticker)

    # ==================== 变化率计算 ====================

    def get_price_change(self, symbol: str) -> Optional[Tuple[float, float, float]]:
        """
        计算时间窗口内的价格变化

        Returns:
            (change_percent, window_low, window_high) 或 None
            change_percent: 相对于窗口起点的变化百分比
        """
        tracker = self._trackers.get(symbol)
        if not tracker or len(tracker.price_history) < 2:
            return None

        now = datetime.now()
        window_start = now - timedelta(seconds=self.price_window)

        # 筛选窗口内的价格点
        window_prices = [
            p for p in tracker.price_history
            if p.timestamp >= window_start
        ]

        if len(window_prices) < 2:
            return None

        start_price = window_prices[0].price
        current_price = tracker.latest_price
        low = min(p.price for p in window_prices)
        high = max(p.price for p in window_prices)

        if start_price == 0:
            return None

        change_percent = ((current_price - start_price) / start_price) * 100
        return (change_percent, low, high)

    def get_volume_ratio(self, symbol: str) -> Optional[float]:
        """
        计算当前成交量相对于历史平均的倍数

        Returns:
            volume_ratio: 当前成交量 / 历史平均成交量
        """
        tracker = self._trackers.get(symbol)
        if not tracker or len(tracker.price_history) < self.volume_periods:
            return None

        # 取最近 N 个周期的成交量
        recent = list(tracker.price_history)[-self.volume_periods:]
        volumes = [p.volume for p in recent[:-1]]  # 排除当前

        if not volumes or sum(volumes) == 0:
            return None

        avg_volume = sum(volumes) / len(volumes)
        current_volume = tracker.latest_volume

        if avg_volume == 0:
            return None

        return current_volume / avg_volume

    def get_oi_change(self, symbol: str) -> Optional[float]:
        """
        计算持仓量变化百分比

        Returns:
            oi_change_percent: OI 变化百分比
        """
        tracker = self._trackers.get(symbol)
        if not tracker or len(tracker.oi_history) < 2:
            return None

        now = datetime.now()
        window_start = now - timedelta(seconds=self.oi_window)

        # 筛选窗口内的 OI 数据
        window_oi = [
            (ts, oi) for ts, oi in tracker.oi_history
            if ts >= window_start
        ]

        if len(window_oi) < 2:
            return None

        start_oi = window_oi[0][1]
        current_oi = tracker.latest_oi

        if start_oi == 0:
            return None

        change_percent = ((current_oi - start_oi) / start_oi) * 100
        return change_percent

    # ==================== 数据访问 ====================

    def get_latest(self, symbol: str) -> Optional[TickerData]:
        """获取最新行情快照"""
        tracker = self._trackers.get(symbol)
        if not tracker or tracker.last_update is None:
            return None

        return TickerData(
            symbol=symbol,
            price=tracker.latest_price,
            volume=tracker.latest_volume,
            quote_volume=tracker.latest_quote_volume,
            timestamp=tracker.last_update,
            open_interest=tracker.latest_oi if tracker.latest_oi > 0 else None
        )

    def get_quote_volume(self, symbol: str) -> float:
        """获取 24h 成交额"""
        tracker = self._trackers.get(symbol)
        return tracker.latest_quote_volume if tracker else 0.0

    def get_all_symbols(self) -> List[str]:
        """获取所有追踪中的交易对"""
        return list(self._trackers.keys())

    def cleanup_old_data(self, max_age: int = 3600) -> None:
        """
        清理过期数据

        Args:
            max_age: 最大保留时间（秒）
        """
        cutoff = datetime.now() - timedelta(seconds=max_age)

        for symbol, tracker in self._trackers.items():
            # 清理价格历史
            while tracker.price_history:
                if tracker.price_history[0].timestamp < cutoff:
                    tracker.price_history.popleft()
                else:
                    break

            # 清理 OI 历史
            while tracker.oi_history:
                if tracker.oi_history[0][0] < cutoff:
                    tracker.oi_history.popleft()
                else:
                    break
