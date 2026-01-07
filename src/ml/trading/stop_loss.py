"""
止损策略管理

实现多种止损策略：
- 固定百分比止损
- ATR动态止损
- 移动止损（Trailing Stop）
- 时间止损
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, TYPE_CHECKING

from loguru import logger

from .models import ExitReason, OrderSide, Position

if TYPE_CHECKING:
    from ...models import MLFeatureVector


@dataclass
class StopLossConfig:
    """止损配置"""
    method: str = "multiple"         # "fixed" | "atr" | "trailing" | "multiple"

    # 固定止损
    fixed_stop_pct: float = 1.5      # 固定止损 1.5%
    take_profit_pct: float = 3.0     # 固定止盈 3.0%

    # ATR止损
    atr_multiplier: float = 2.0      # ATR止损倍数
    atr_period: int = 14             # ATR计算周期

    # 移动止损
    trailing_distance: float = 1.0   # 移动止损距离 1%
    trailing_activation: float = 1.0 # 盈利多少%后激活移动止损

    # 时间止损
    max_hold_seconds: int = 900      # 最大持仓时间 15分钟


class StopLossManager:
    """止损策略管理器"""

    def __init__(self, config: Optional[StopLossConfig] = None):
        """
        初始化止损管理器

        Args:
            config: 止损配置，None时使用默认配置
        """
        if config is None:
            config = StopLossConfig()

        self.config = config
        logger.info(f"止损管理器初始化: method={config.method}")

    def check_stop_loss(
        self,
        position: Position,
        current_price: float,
        feature: Optional["MLFeatureVector"] = None
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查是否触发止损

        Args:
            position: 持仓
            current_price: 当前价格
            feature: ML特征（用于ATR等）

        Returns:
            (是否触发, 平仓原因)
        """
        method = self.config.method

        if method == "fixed":
            return self._check_fixed_stop(position, current_price)
        elif method == "trailing":
            return self._check_trailing_stop(position, current_price)
        elif method == "atr":
            return self._check_atr_stop(position, current_price, feature)
        elif method == "multiple":
            return self._check_multiple_stops(position, current_price, feature)
        else:
            return False, None

    def _check_fixed_stop(
        self,
        position: Position,
        current_price: float
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查固定止损

        Args:
            position: 持仓
            current_price: 当前价格

        Returns:
            (是否触发, 原因)
        """
        # 使用持仓的止损价
        if position.stop_loss_price:
            if position.side == OrderSide.LONG:
                if current_price <= position.stop_loss_price:
                    return True, ExitReason.STOP_LOSS
            else:
                if current_price >= position.stop_loss_price:
                    return True, ExitReason.STOP_LOSS

        # 使用配置的止损百分比
        stop_pct = self.config.fixed_stop_pct / 100

        if position.side == OrderSide.LONG:
            stop_price = position.entry_price * (1 - stop_pct)
            if current_price <= stop_price:
                return True, ExitReason.STOP_LOSS
        else:
            stop_price = position.entry_price * (1 + stop_pct)
            if current_price >= stop_price:
                return True, ExitReason.STOP_LOSS

        return False, None

    def _check_trailing_stop(
        self,
        position: Position,
        current_price: float
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查移动止损

        移动止损规则：
        - 做多：从最高价回撤超过 trailing_distance% 时止损
        - 做空：从最低价反弹超过 trailing_distance% 时止损

        Args:
            position: 持仓
            current_price: 当前价格

        Returns:
            (是否触发, 原因)
        """
        # 使用持仓配置的距离或默认距离
        distance_pct = position.trailing_stop_distance
        if distance_pct is None:
            distance_pct = self.config.trailing_distance

        # 检查是否满足激活条件（先盈利一定幅度）
        activation_pct = self.config.trailing_activation
        if position.unrealized_pnl_pct < activation_pct:
            return False, None

        if position.side == OrderSide.LONG:
            # 做多：从最高点回撤
            if position.highest_price > 0:
                trailing_stop = position.highest_price * (1 - distance_pct / 100)
                if current_price <= trailing_stop:
                    logger.debug(
                        f"移动止损触发: {position.symbol} 最高价=${position.highest_price:.4f} "
                        f"当前价=${current_price:.4f} 止损价=${trailing_stop:.4f}"
                    )
                    return True, ExitReason.TRAILING_STOP
        else:
            # 做空：从最低点反弹
            if position.lowest_price > 0:
                trailing_stop = position.lowest_price * (1 + distance_pct / 100)
                if current_price >= trailing_stop:
                    logger.debug(
                        f"移动止损触发: {position.symbol} 最低价=${position.lowest_price:.4f} "
                        f"当前价=${current_price:.4f} 止损价=${trailing_stop:.4f}"
                    )
                    return True, ExitReason.TRAILING_STOP

        return False, None

    def _check_atr_stop(
        self,
        position: Position,
        current_price: float,
        feature: Optional["MLFeatureVector"] = None
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查ATR动态止损

        ATR止损规则：
        - 止损价 = 入场价 ± ATR * multiplier

        Args:
            position: 持仓
            current_price: 当前价格
            feature: ML特征（需要包含ATR）

        Returns:
            (是否触发, 原因)
        """
        # 尝试获取ATR（从特征或估算）
        atr = self._get_atr(position, feature)
        if atr is None or atr <= 0:
            # 无ATR数据，回退到固定止损
            return self._check_fixed_stop(position, current_price)

        multiplier = self.config.atr_multiplier

        if position.side == OrderSide.LONG:
            stop_price = position.entry_price - atr * multiplier
            if current_price <= stop_price:
                return True, ExitReason.STOP_LOSS
        else:
            stop_price = position.entry_price + atr * multiplier
            if current_price >= stop_price:
                return True, ExitReason.STOP_LOSS

        return False, None

    def _check_time_stop(
        self,
        position: Position
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查时间止损

        Args:
            position: 持仓

        Returns:
            (是否触发, 原因)
        """
        max_hold = position.max_hold_seconds
        if max_hold <= 0:
            max_hold = self.config.max_hold_seconds

        hold_duration = (datetime.now() - position.entry_time).total_seconds()
        if hold_duration > max_hold:
            return True, ExitReason.TIME_EXIT

        return False, None

    def _check_multiple_stops(
        self,
        position: Position,
        current_price: float,
        feature: Optional["MLFeatureVector"] = None
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查多种止损（综合策略）

        优先级：
        1. 固定止损
        2. 移动止损
        3. 时间止损

        Args:
            position: 持仓
            current_price: 当前价格
            feature: ML特征

        Returns:
            (是否触发, 原因)
        """
        # 1. 固定止损（最高优先级）
        triggered, reason = self._check_fixed_stop(position, current_price)
        if triggered:
            return True, reason

        # 2. 移动止损（盈利后）
        triggered, reason = self._check_trailing_stop(position, current_price)
        if triggered:
            return True, reason

        # 3. 时间止损
        triggered, reason = self._check_time_stop(position)
        if triggered:
            return True, reason

        return False, None

    def _get_atr(
        self,
        position: Position,
        feature: Optional["MLFeatureVector"] = None
    ) -> Optional[float]:
        """
        获取ATR值

        Args:
            position: 持仓
            feature: ML特征

        Returns:
            ATR值
        """
        # 从特征获取（如果有）
        if feature and hasattr(feature, 'volatility_5m'):
            # 使用波动率估算ATR
            # ATR ≈ 价格 × 波动率% × sqrt(周期)
            volatility = feature.volatility_5m
            if volatility and volatility > 0:
                return position.entry_price * (volatility / 100)

        return None

    def calculate_stop_loss(
        self,
        entry_price: float,
        side: OrderSide,
        method: Optional[str] = None,
        atr: Optional[float] = None
    ) -> float:
        """
        计算止损价格

        Args:
            entry_price: 入场价格
            side: 方向
            method: 止损方法，None使用配置
            atr: ATR值（用于ATR止损）

        Returns:
            止损价格
        """
        if method is None:
            method = self.config.method

        if method == "fixed" or method == "multiple":
            pct = self.config.fixed_stop_pct / 100
            if side == OrderSide.LONG:
                return entry_price * (1 - pct)
            return entry_price * (1 + pct)

        elif method == "atr" and atr:
            multiplier = self.config.atr_multiplier
            if side == OrderSide.LONG:
                return entry_price - atr * multiplier
            return entry_price + atr * multiplier

        # 默认使用固定止损
        pct = self.config.fixed_stop_pct / 100
        if side == OrderSide.LONG:
            return entry_price * (1 - pct)
        return entry_price * (1 + pct)

    def calculate_take_profit(
        self,
        entry_price: float,
        side: OrderSide,
        risk_reward_ratio: Optional[float] = None
    ) -> float:
        """
        计算止盈价格

        Args:
            entry_price: 入场价格
            side: 方向
            risk_reward_ratio: 风险收益比，None使用配置

        Returns:
            止盈价格
        """
        pct = self.config.take_profit_pct / 100

        if side == OrderSide.LONG:
            return entry_price * (1 + pct)
        return entry_price * (1 - pct)
