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

    # 现货价格历史（用于现货-合约价差检测）
    spot_price_history: deque = field(default_factory=lambda: deque(maxlen=100))

    # 最新数据缓存
    latest_price: float = 0.0
    latest_volume: float = 0.0
    latest_quote_volume: float = 0.0
    latest_oi: float = 0.0
    latest_spot_price: float = 0.0  # 最新现货价格
    last_update: Optional[datetime] = None
    last_spot_update: Optional[datetime] = None  # 最新现货更新时间


class PriceTracker:
    """
    多交易对价格追踪器
    管理所有交易对的历史数据和变化率计算
    """

    def __init__(
        self,
        price_window: int = 60,      # 价格检测窗口（秒）
        volume_periods: int = 10,    # 成交量对比周期数
        oi_window: int = 300,        # OI 检测窗口（秒）
        spread_window: int = 60      # 现货-合约价差检测窗口（秒）
    ):
        self.price_window = price_window
        self.volume_periods = volume_periods
        self.oi_window = oi_window
        self.spread_window = spread_window

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

    def update_spot_price(self, symbol: str, spot_price: float, timestamp: datetime = None) -> None:
        """
        更新现货价格（来自 REST API）

        Args:
            symbol: 交易对
            spot_price: 现货价格
            timestamp: 时间戳
        """
        if timestamp is None:
            timestamp = datetime.now()

        tracker = self._trackers[symbol]
        tracker.latest_spot_price = spot_price
        tracker.last_spot_update = timestamp
        tracker.spot_price_history.append((timestamp, spot_price))

    def batch_update_spot_prices(self, spot_prices: Dict[str, float]) -> None:
        """
        批量更新现货价格

        Args:
            spot_prices: {symbol: price} 字典
        """
        timestamp = datetime.now()
        for symbol, price in spot_prices.items():
            self.update_spot_price(symbol, price, timestamp)

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

    def get_spot_futures_spread(self, symbol: str) -> Optional[Tuple[float, float, float]]:
        """
        计算现货-合约价差百分比

        Returns:
            (spread_percent, spot_price, futures_price) 或 None
            spread_percent: (现货价格 - 合约价格) / 合约价格 * 100
            正值表示现货溢价，负值表示合约溢价
        """
        tracker = self._trackers.get(symbol)
        if not tracker:
            return None

        # 检查是否有现货价格数据
        if tracker.latest_spot_price == 0 or tracker.last_spot_update is None:
            return None

        # 检查是否有合约价格数据
        if tracker.latest_price == 0:
            return None

        # 检查现货数据是否过期（超过检测窗口）
        now = datetime.now()
        if now - tracker.last_spot_update > timedelta(seconds=self.spread_window * 2):
            return None

        spot_price = tracker.latest_spot_price
        futures_price = tracker.latest_price

        if futures_price == 0:
            return None

        # 计算价差百分比
        spread_percent = ((spot_price - futures_price) / futures_price) * 100

        return (spread_percent, spot_price, futures_price)

    def get_price_reversal(self, symbol: str, time_window: int = 300) -> Optional[dict]:
        """
        检测价格反转

        在指定时间窗口内检测：
        - 见顶反转：先涨后跌，最高点在窗口前半段
        - 见底反转：先跌后涨，最低点在窗口前半段

        Args:
            symbol: 交易对
            time_window: 检测窗口（秒），默认5分钟

        Returns:
            {
                "type": "top" | "bottom",  # 反转类型
                "start_price": float,       # 窗口起始价
                "high_price": float,        # 窗口最高价
                "low_price": float,         # 窗口最低价
                "current_price": float,     # 当前价格
                "rise_percent": float,      # 上涨幅度 (从起点到高点 或 从低点到当前)
                "fall_percent": float,      # 下跌幅度 (从高点到当前 或 从起点到低点)
                "extreme_time": datetime,   # 极值出现时间
            }
            或 None（无反转）
        """
        tracker = self._trackers.get(symbol)
        if not tracker or len(tracker.price_history) < 5:
            return None

        now = datetime.now()
        window_start = now - timedelta(seconds=time_window)
        window_mid = now - timedelta(seconds=time_window / 2)

        # 筛选窗口内的价格点
        window_prices = [
            p for p in tracker.price_history
            if p.timestamp >= window_start
        ]

        if len(window_prices) < 5:
            return None

        start_price = window_prices[0].price
        current_price = tracker.latest_price

        if start_price == 0:
            return None

        # 找到最高点和最低点及其时间
        high_point = max(window_prices, key=lambda p: p.price)
        low_point = min(window_prices, key=lambda p: p.price)

        high_price = high_point.price
        low_price = low_point.price
        high_time = high_point.timestamp
        low_time = low_point.timestamp

        # 判断见顶反转：先涨后跌
        # 条件：最高点在窗口前半段，且从起点涨幅、从高点跌幅都足够
        if high_time <= window_mid and high_time > window_start:
            rise_from_start = ((high_price - start_price) / start_price) * 100
            fall_from_high = ((high_price - current_price) / high_price) * 100

            if rise_from_start > 0 and fall_from_high > 0:
                return {
                    "type": "top",
                    "start_price": start_price,
                    "high_price": high_price,
                    "low_price": low_price,
                    "current_price": current_price,
                    "rise_percent": rise_from_start,
                    "fall_percent": fall_from_high,
                    "extreme_time": high_time,
                }

        # 判断见底反转：先跌后涨
        # 条件：最低点在窗口前半段，且从起点跌幅、从低点涨幅都足够
        if low_time <= window_mid and low_time > window_start:
            fall_from_start = ((start_price - low_price) / start_price) * 100
            rise_from_low = ((current_price - low_price) / low_price) * 100

            if fall_from_start > 0 and rise_from_low > 0:
                return {
                    "type": "bottom",
                    "start_price": start_price,
                    "high_price": high_price,
                    "low_price": low_price,
                    "current_price": current_price,
                    "rise_percent": rise_from_low,
                    "fall_percent": fall_from_start,
                    "extreme_time": low_time,
                }

        return None

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

    def get_oi_value(self, symbol: str) -> float:
        """
        获取持仓价值（现价 × 持仓量）
        用于大盘/超大盘分层判断

        Returns:
            持仓价值(USDT)，如果没有数据返回0.0
        """
        tracker = self._trackers.get(symbol)
        if not tracker:
            return 0.0

        # 持仓价值 = 现价 × 持仓量
        if tracker.latest_price > 0 and tracker.latest_oi > 0:
            return tracker.latest_price * tracker.latest_oi

        return 0.0

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
