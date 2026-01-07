"""
持仓管理器

负责持仓的生命周期管理：
- 开仓、平仓
- 盈亏更新
- 止盈止损检查
"""
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from loguru import logger

from .models import ExitReason, OrderSide, Position, Trade, TradingSignal
from .account import VirtualAccount

if TYPE_CHECKING:
    from .stop_loss import StopLossManager
    from ...models import MLFeatureVector


class PositionManager:
    """持仓管理器"""

    def __init__(
        self,
        account: VirtualAccount,
        stop_loss_manager: Optional["StopLossManager"] = None
    ):
        """
        初始化持仓管理器

        Args:
            account: 虚拟账户
            stop_loss_manager: 止损管理器
        """
        self.account = account
        self.stop_loss_manager = stop_loss_manager

        # 按 symbol 索引持仓 (symbol -> [Position])
        self._positions_by_symbol: Dict[str, List[Position]] = {}

    def open_position(
        self,
        symbol: str,
        side: OrderSide,
        price: float,
        signal: Optional[TradingSignal] = None,
        quantity: Optional[float] = None,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        trailing_distance: Optional[float] = None,
        max_hold_seconds: int = 900,
        timestamp: Optional[datetime] = None
    ) -> Optional[Position]:
        """
        开仓

        Args:
            symbol: 交易对
            side: 方向
            price: 开仓价格
            signal: 交易信号（可选）
            quantity: 持仓数量，None时自动计算
            take_profit: 止盈价
            stop_loss: 止损价
            trailing_distance: 移动止损距离%
            max_hold_seconds: 最大持仓时间
            timestamp: 时间戳

        Returns:
            开仓成功返回 Position，失败返回 None
        """
        if timestamp is None:
            timestamp = datetime.now()

        # 从信号获取参数
        if signal:
            if take_profit is None:
                take_profit = signal.take_profit
            if stop_loss is None:
                stop_loss = signal.stop_loss

        # 计算止损距离
        stop_loss_pct = 1.5  # 默认止损距离
        if stop_loss and price > 0:
            if side == OrderSide.LONG:
                stop_loss_pct = abs(price - stop_loss) / price * 100
            else:
                stop_loss_pct = abs(stop_loss - price) / price * 100

        # 计算仓位大小
        if quantity is None:
            quantity = self.account.calculate_position_size(price, stop_loss_pct)

        if quantity <= 0:
            logger.warning(f"计算仓位为0，无法开仓: {symbol}")
            return None

        # 计算保证金
        margin = self.account.calculate_margin(quantity, price)

        # 检查是否可以开仓
        can_open, reason = self.account.can_open_position(margin)
        if not can_open:
            logger.warning(f"无法开仓 {symbol}: {reason}")
            return None

        # 计算手续费（开仓）
        commission = self.account.calculate_commission(quantity, price, is_maker=False)
        self.account.balance -= commission

        # 创建持仓
        position = Position(
            position_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=price,
            entry_time=timestamp,
            leverage=self.account.leverage,
            margin=margin,
            take_profit_price=take_profit,
            stop_loss_price=stop_loss,
            trailing_stop_distance=trailing_distance,
            max_hold_seconds=max_hold_seconds,
            current_price=price,
            highest_price=price,
            lowest_price=price,
            signal_confidence=signal.confidence if signal else 0,
            signal_reason=signal.reason if signal else "",
        )

        # 添加到账户和索引
        self.account.add_position(position)
        if symbol not in self._positions_by_symbol:
            self._positions_by_symbol[symbol] = []
        self._positions_by_symbol[symbol].append(position)

        logger.info(
            f"[开仓] {symbol} {side.value.upper()} "
            f"数量={quantity:.4f} @ ${price:.4f} "
            f"保证金=${margin:.2f} "
            f"止损=${stop_loss:.4f if stop_loss else 'N/A'} "
            f"止盈=${take_profit:.4f if take_profit else 'N/A'}"
        )

        return position

    def close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: ExitReason,
        timestamp: Optional[datetime] = None
    ) -> Trade:
        """
        平仓

        Args:
            position: 要平仓的持仓
            exit_price: 平仓价格
            exit_reason: 平仓原因
            timestamp: 时间戳

        Returns:
            Trade 交易记录
        """
        if timestamp is None:
            timestamp = datetime.now()

        # 计算手续费（平仓）
        commission = self.account.calculate_commission(
            position.quantity, exit_price, is_maker=False
        )

        # 创建交易记录
        trade = Trade(
            trade_id=str(uuid.uuid4())[:8],
            symbol=position.symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            exit_price=exit_price,
            exit_time=timestamp,
            exit_reason=exit_reason,
            leverage=position.leverage,
            commission=commission,
            signal_confidence=position.signal_confidence,
            signal_reason=position.signal_reason,
            margin=position.margin,
        )

        # 从账户和索引移除持仓
        self.account.remove_position(position.position_id)
        if position.symbol in self._positions_by_symbol:
            self._positions_by_symbol[position.symbol] = [
                p for p in self._positions_by_symbol[position.symbol]
                if p.position_id != position.position_id
            ]

        # 记录交易
        self.account.record_trade(trade)

        logger.info(
            f"[平仓] {position.symbol} {position.side.value.upper()} "
            f"@ ${exit_price:.4f} "
            f"PnL=${trade.realized_pnl:+.2f} ({trade.roi:+.2f}% ROI) "
            f"原因={exit_reason.value}"
        )

        return trade

    def update_positions_pnl(self, prices: Dict[str, float]):
        """
        更新所有持仓的盈亏

        Args:
            prices: {symbol: current_price}
        """
        for symbol, price in prices.items():
            if symbol in self._positions_by_symbol:
                for position in self._positions_by_symbol[symbol]:
                    position.update_pnl(price)

    def check_exit(
        self,
        position: Position,
        current_price: float,
        feature: Optional["MLFeatureVector"] = None,
        timestamp: Optional[datetime] = None
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        检查是否需要平仓

        Args:
            position: 持仓
            current_price: 当前价格
            feature: ML特征（用于高级止损）
            timestamp: 时间戳

        Returns:
            (是否需要平仓, 平仓原因)
        """
        if timestamp is None:
            timestamp = datetime.now()

        # 1. 检查止盈
        if position.take_profit_price:
            if position.side == OrderSide.LONG:
                if current_price >= position.take_profit_price:
                    return True, ExitReason.TAKE_PROFIT
            else:
                if current_price <= position.take_profit_price:
                    return True, ExitReason.TAKE_PROFIT

        # 2. 使用止损管理器检查（如果有）
        if self.stop_loss_manager:
            triggered, reason = self.stop_loss_manager.check_stop_loss(
                position, current_price, feature
            )
            if triggered:
                return True, reason

        # 3. 简单止损检查（如果没有止损管理器）
        else:
            if position.stop_loss_price:
                if position.side == OrderSide.LONG:
                    if current_price <= position.stop_loss_price:
                        return True, ExitReason.STOP_LOSS
                else:
                    if current_price >= position.stop_loss_price:
                        return True, ExitReason.STOP_LOSS

        # 4. 时间止损
        hold_duration = (timestamp - position.entry_time).total_seconds()
        if hold_duration > position.max_hold_seconds:
            return True, ExitReason.TIME_EXIT

        # 5. 检查强制平仓（爆仓）
        # 15倍杠杆，亏损超过 100%/15 ≈ 6.67% 时爆仓
        if position.unrealized_pnl_pct < -100 / position.leverage:
            return True, ExitReason.LIQUIDATION

        return False, None

    def get_positions(self, symbol: Optional[str] = None) -> List[Position]:
        """
        获取持仓

        Args:
            symbol: 交易对，None返回所有

        Returns:
            持仓列表
        """
        if symbol:
            return self._positions_by_symbol.get(symbol, [])
        return list(self.account.positions.values())

    def has_position(self, symbol: str, side: Optional[OrderSide] = None) -> bool:
        """
        检查是否有持仓

        Args:
            symbol: 交易对
            side: 方向，None时检查任意方向

        Returns:
            是否有持仓
        """
        positions = self._positions_by_symbol.get(symbol, [])
        if not positions:
            return False

        if side is None:
            return True

        return any(p.side == side for p in positions)

    def get_position_by_side(
        self, symbol: str, side: OrderSide
    ) -> Optional[Position]:
        """
        获取指定方向的持仓

        Args:
            symbol: 交易对
            side: 方向

        Returns:
            持仓，不存在返回 None
        """
        positions = self._positions_by_symbol.get(symbol, [])
        for p in positions:
            if p.side == side:
                return p
        return None

    def close_all_positions(
        self,
        prices: Dict[str, float],
        reason: ExitReason = ExitReason.MANUAL,
        timestamp: Optional[datetime] = None
    ) -> List[Trade]:
        """
        平掉所有持仓

        Args:
            prices: {symbol: exit_price}
            reason: 平仓原因
            timestamp: 时间戳

        Returns:
            交易记录列表
        """
        trades = []
        for position in list(self.account.positions.values()):
            price = prices.get(position.symbol, position.current_price)
            trade = self.close_position(position, price, reason, timestamp)
            trades.append(trade)

        return trades

    def get_total_unrealized_pnl(self) -> float:
        """获取总未实现盈亏"""
        return sum(p.unrealized_pnl for p in self.account.positions.values())

    def get_open_position_count(self) -> int:
        """获取持仓数量"""
        return len(self.account.positions)
