"""
订单簿异动监控器
检测订单簿中的异常变化：大单墙、深度失衡、扫盘等

检测维度：
1. 大单墙检测 - 单个价位异常大的挂单
2. 深度失衡 - 买卖盘总量严重不对称
3. 扫盘检测 - 大单墙突然消失（被吃掉）
"""
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from .models import (
    AlertEvent, AlertType, OrderBookSnapshot,
    OrderBookWall, OrderBookEvent
)


@dataclass
class OrderBookConfig:
    """订单簿监控配置"""
    enabled: bool = True

    # 大单墙检测
    wall_detection: bool = True
    wall_value_threshold: float = 500000     # 大单墙最小价值阈值(USDT)
    wall_ratio_threshold: float = 3.0        # 单档挂单量 > 平均值 N 倍
    wall_distance_max: float = 2.0           # 距离当前价格最大百分比

    # 深度失衡检测
    imbalance_detection: bool = True
    imbalance_threshold: float = 0.6         # 失衡比率阈值 (0-1)
    imbalance_depth_levels: int = 10         # 计算失衡的档位数

    # 扫盘检测
    sweep_detection: bool = True
    sweep_value_threshold: float = 300000    # 被吃掉的最小价值(USDT)

    # 冷却时间
    cooldown_seconds: int = 300              # 同一币种告警冷却时间


@dataclass
class WallState:
    """大单墙状态记录"""
    price: float
    quantity: float
    value: float
    side: str
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)


class OrderBookMonitor:
    """
    订单簿异动监控器

    通过对比历史订单簿快照，检测：
    1. 新出现的大单墙
    2. 深度失衡变化
    3. 大单被吃掉（扫盘）
    """

    def __init__(
        self,
        config: OrderBookConfig,
        on_event: Optional[Callable[[OrderBookEvent], None]] = None
    ):
        self.config = config
        self.on_event = on_event

        # 历史快照: {symbol: last_snapshot}
        self._snapshots: Dict[str, OrderBookSnapshot] = {}

        # 跟踪的大单墙: {symbol: {price: WallState}}
        self._tracked_walls: Dict[str, Dict[float, WallState]] = defaultdict(dict)

        # 告警冷却: {(symbol, event_type): last_alert_time}
        self._cooldowns: Dict[Tuple[str, str], datetime] = defaultdict(
            lambda: datetime.min
        )

    def _is_cooled_down(self, symbol: str, event_type: str) -> bool:
        """检查是否过了冷却期"""
        key = (symbol, event_type)
        last_alert = self._cooldowns[key]
        return datetime.now() - last_alert > timedelta(
            seconds=self.config.cooldown_seconds
        )

    def _record_cooldown(self, symbol: str, event_type: str):
        """记录告警时间"""
        self._cooldowns[(symbol, event_type)] = datetime.now()

    async def process_snapshot(
        self,
        snapshot: OrderBookSnapshot
    ) -> List[OrderBookEvent]:
        """
        处理订单簿快照，检测异动

        Args:
            snapshot: 新的订单簿快照

        Returns:
            检测到的异动事件列表
        """
        if not self.config.enabled:
            return []

        symbol = snapshot.symbol
        events = []

        # 获取上一次快照
        prev_snapshot = self._snapshots.get(symbol)

        # 1. 检测大单墙
        if self.config.wall_detection:
            wall_events = self._detect_walls(snapshot, prev_snapshot)
            events.extend(wall_events)

        # 2. 检测深度失衡
        if self.config.imbalance_detection:
            imbalance_event = self._detect_imbalance(snapshot, prev_snapshot)
            if imbalance_event:
                events.append(imbalance_event)

        # 3. 检测扫盘（需要历史快照）
        if self.config.sweep_detection and prev_snapshot:
            sweep_events = self._detect_sweep(snapshot, prev_snapshot)
            events.extend(sweep_events)

        # 更新快照
        self._snapshots[symbol] = snapshot

        # 触发事件回调
        for event in events:
            if self.on_event:
                try:
                    self.on_event(event)
                except Exception as e:
                    logger.error(f"订单簿事件回调失败: {e}")

        return events

    def _detect_walls(
        self,
        snapshot: OrderBookSnapshot,
        prev_snapshot: Optional[OrderBookSnapshot]
    ) -> List[OrderBookEvent]:
        """
        检测大单墙

        策略：
        1. 单档挂单价值超过阈值
        2. 单档挂单量是平均值的 N 倍
        3. 距离当前价格在合理范围内
        """
        events = []
        symbol = snapshot.symbol

        if not snapshot.best_bid or not snapshot.best_ask:
            return events

        mid_price = (snapshot.best_bid + snapshot.best_ask) / 2

        # 计算买卖盘平均单档价值
        bid_values = [p * q for p, q in snapshot.bids[:20]]
        ask_values = [p * q for p, q in snapshot.asks[:20]]

        avg_bid_value = sum(bid_values) / len(bid_values) if bid_values else 0
        avg_ask_value = sum(ask_values) / len(ask_values) if ask_values else 0

        current_walls = {}

        # 检测买墙
        for price, qty in snapshot.bids[:20]:
            value = price * qty
            distance = (mid_price - price) / mid_price * 100

            if distance > self.config.wall_distance_max:
                continue

            # 满足条件：价值超阈值 且 是平均值的N倍
            is_wall = (
                value >= self.config.wall_value_threshold and
                avg_bid_value > 0 and
                value >= avg_bid_value * self.config.wall_ratio_threshold
            )

            if is_wall:
                current_walls[price] = WallState(
                    price=price,
                    quantity=qty,
                    value=value,
                    side="bid"
                )

        # 检测卖墙
        for price, qty in snapshot.asks[:20]:
            value = price * qty
            distance = (price - mid_price) / mid_price * 100

            if distance > self.config.wall_distance_max:
                continue

            is_wall = (
                value >= self.config.wall_value_threshold and
                avg_ask_value > 0 and
                value >= avg_ask_value * self.config.wall_ratio_threshold
            )

            if is_wall:
                current_walls[price] = WallState(
                    price=price,
                    quantity=qty,
                    value=value,
                    side="ask"
                )

        # 与历史对比，发现新墙
        tracked = self._tracked_walls[symbol]
        for price, wall in current_walls.items():
            if price not in tracked:
                # 新发现的墙
                if self._is_cooled_down(symbol, f"wall_{wall.side}"):
                    event = OrderBookEvent(
                        symbol=symbol,
                        event_type="wall_detected",
                        side=wall.side,
                        price=wall.price,
                        quantity=wall.quantity,
                        value=wall.value,
                        extra_info={
                            "类型": "买墙" if wall.side == "bid" else "卖墙",
                            "价值": f"${wall.value:,.0f}",
                            "数量": f"{wall.quantity:,.2f}"
                        }
                    )
                    events.append(event)
                    self._record_cooldown(symbol, f"wall_{wall.side}")
                    logger.info(
                        f"[大单墙] {symbol} {'买墙' if wall.side == 'bid' else '卖墙'} "
                        f"价格={wall.price:.4f} 价值=${wall.value:,.0f}"
                    )

        # 更新跟踪的墙
        self._tracked_walls[symbol] = current_walls

        return events

    def _detect_imbalance(
        self,
        snapshot: OrderBookSnapshot,
        prev_snapshot: Optional[OrderBookSnapshot]
    ) -> Optional[OrderBookEvent]:
        """
        检测深度失衡

        失衡比率 = (买盘深度 - 卖盘深度) / (买盘深度 + 卖盘深度)
        正值表示买盘强，负值表示卖盘强
        """
        symbol = snapshot.symbol
        levels = self.config.imbalance_depth_levels

        ratio = snapshot.imbalance_ratio(levels)

        # 检查是否超过阈值
        if abs(ratio) < self.config.imbalance_threshold:
            return None

        # 检查冷却
        event_type = "imbalance_bid" if ratio > 0 else "imbalance_ask"
        if not self._is_cooled_down(symbol, event_type):
            return None

        # 计算具体深度值
        bid_depth = snapshot.bid_depth(levels)
        ask_depth = snapshot.ask_depth(levels)

        direction = "买盘强势" if ratio > 0 else "卖盘强势"
        event = OrderBookEvent(
            symbol=symbol,
            event_type="imbalance",
            side="bid" if ratio > 0 else "ask",
            imbalance_ratio=ratio,
            extra_info={
                "方向": direction,
                "失衡比": f"{ratio:.2%}",
                "买盘深度": f"${bid_depth:,.0f}",
                "卖盘深度": f"${ask_depth:,.0f}",
                "比值": f"{bid_depth/ask_depth:.2f}x" if ask_depth > 0 else "N/A"
            }
        )

        self._record_cooldown(symbol, event_type)
        logger.info(
            f"[深度失衡] {symbol} {direction} "
            f"比率={ratio:.2%} 买={bid_depth:,.0f} 卖={ask_depth:,.0f}"
        )

        return event

    def _detect_sweep(
        self,
        snapshot: OrderBookSnapshot,
        prev_snapshot: OrderBookSnapshot
    ) -> List[OrderBookEvent]:
        """
        检测扫盘（大单墙被吃）

        策略：
        1. 跟踪的大单墙突然消失
        2. 消失的价值超过阈值
        """
        events = []
        symbol = snapshot.symbol

        # 获取之前跟踪的墙
        tracked = self._tracked_walls.get(symbol, {})
        if not tracked:
            return events

        # 构建当前订单簿的价格->数量映射
        current_bids = {p: q for p, q in snapshot.bids}
        current_asks = {p: q for p, q in snapshot.asks}

        for price, wall in list(tracked.items()):
            # 检查墙是否还存在
            if wall.side == "bid":
                current_qty = current_bids.get(price, 0)
            else:
                current_qty = current_asks.get(price, 0)

            # 墙消失或大幅减少（减少80%以上）
            if current_qty < wall.quantity * 0.2:
                removed_value = wall.value - (price * current_qty)

                if removed_value >= self.config.sweep_value_threshold:
                    if self._is_cooled_down(symbol, f"sweep_{wall.side}"):
                        event = OrderBookEvent(
                            symbol=symbol,
                            event_type="sweep",
                            side=wall.side,
                            price=wall.price,
                            value=removed_value,
                            extra_info={
                                "类型": "买墙被吃" if wall.side == "bid" else "卖墙被吃",
                                "原始价值": f"${wall.value:,.0f}",
                                "被吃价值": f"${removed_value:,.0f}",
                                "价格": f"${wall.price:.4f}"
                            }
                        )
                        events.append(event)
                        self._record_cooldown(symbol, f"sweep_{wall.side}")
                        logger.info(
                            f"[扫盘] {symbol} "
                            f"{'买墙被吃' if wall.side == 'bid' else '卖墙被吃'} "
                            f"价值=${removed_value:,.0f}"
                        )

        return events

    def get_tracked_walls(self, symbol: str) -> List[WallState]:
        """获取某个交易对当前跟踪的大单墙"""
        return list(self._tracked_walls.get(symbol, {}).values())

    def get_depth_info(self, symbol: str) -> Optional[dict]:
        """获取某个交易对的深度信息"""
        snapshot = self._snapshots.get(symbol)
        if not snapshot:
            return None

        levels = self.config.imbalance_depth_levels
        return {
            "symbol": symbol,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "spread": snapshot.spread,
            "spread_percent": snapshot.spread_percent,
            "bid_depth": snapshot.bid_depth(levels),
            "ask_depth": snapshot.ask_depth(levels),
            "imbalance_ratio": snapshot.imbalance_ratio(levels),
            "timestamp": snapshot.timestamp
        }


def create_orderbook_alert(event: OrderBookEvent, tier_label: str = "默认") -> AlertEvent:
    """
    将订单簿事件转换为标准告警事件

    Args:
        event: 订单簿事件
        tier_label: 层级标签

    Returns:
        标准告警事件
    """
    # 映射事件类型到告警类型
    type_map = {
        "wall_detected": AlertType.ORDERBOOK_WALL,
        "imbalance": AlertType.ORDERBOOK_IMBALANCE,
        "sweep": AlertType.ORDERBOOK_SWEEP,
    }

    alert_type = type_map.get(event.event_type, AlertType.ORDERBOOK_WALL)

    return AlertEvent(
        symbol=event.symbol,
        alert_type=alert_type,
        tier_label=tier_label,
        current_price=event.price or 0,
        change_percent=event.imbalance_ratio * 100 if event.imbalance_ratio else 0,
        threshold=0,
        time_window=0,
        extra_info=event.extra_info
    )
