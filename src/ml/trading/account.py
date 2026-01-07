"""
虚拟账户管理

实现模拟交易账户功能：
- 资金管理（余额、权益、保证金）
- 仓位计算（风险百分比法）
- 统计跟踪（胜率、最大回撤）
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from loguru import logger

from .models import AccountState, Position, Trade

if TYPE_CHECKING:
    from ...config_manager import TradingAccountConfig


@dataclass
class AccountConfig:
    """账户配置"""
    initial_balance: float = 10000.0   # 初始资金 USDT
    leverage: int = 15                  # 固定15倍杠杆
    maker_fee: float = 0.0002           # 挂单手续费 0.02%
    taker_fee: float = 0.0005           # 吃单手续费 0.05%
    max_positions: int = 5              # 最大同时持仓数
    position_risk_pct: float = 2.0      # 单笔风险占比%
    max_margin_ratio: float = 0.8       # 最大保证金使用率


class VirtualAccount:
    """虚拟交易账户"""

    def __init__(self, config: Optional[AccountConfig] = None):
        """
        初始化虚拟账户

        Args:
            config: 账户配置，None时使用默认配置
        """
        if config is None:
            config = AccountConfig()

        self.config = config
        self.initial_balance = config.initial_balance
        self.balance = self.initial_balance
        self.leverage = config.leverage
        self.maker_fee = config.maker_fee
        self.taker_fee = config.taker_fee

        # 持仓和交易记录
        self.positions: Dict[str, Position] = {}  # position_id -> Position
        self.trades: List[Trade] = []

        # 权益曲线
        self.equity_history: List[Tuple[datetime, float]] = []
        self._last_equity_time: Optional[datetime] = None

        # 统计
        self.total_trades = 0
        self.win_trades = 0
        self.total_pnl = 0.0
        self.max_equity = self.initial_balance
        self.min_equity = self.initial_balance
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0

        logger.info(
            f"虚拟账户初始化: 初始资金=${self.initial_balance:,.2f}, "
            f"杠杆={self.leverage}x, 最大持仓={config.max_positions}"
        )

    def get_equity(self) -> float:
        """
        计算当前权益

        权益 = 余额 + 所有持仓的未实现盈亏
        """
        unrealized_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        return self.balance + unrealized_pnl

    def get_margin_used(self) -> float:
        """计算已用保证金"""
        return sum(p.margin for p in self.positions.values())

    def get_available_margin(self) -> float:
        """计算可用保证金"""
        return self.balance - self.get_margin_used()

    def get_margin_ratio(self) -> float:
        """计算保证金使用率"""
        if self.balance <= 0:
            return 1.0
        return self.get_margin_used() / self.balance

    def can_open_position(self, margin_required: float) -> Tuple[bool, str]:
        """
        检查是否可以开仓

        Args:
            margin_required: 需要的保证金

        Returns:
            (是否可以, 原因)
        """
        # 检查持仓数量
        if len(self.positions) >= self.config.max_positions:
            return False, f"已达最大持仓数 {self.config.max_positions}"

        # 检查可用保证金
        available = self.get_available_margin()
        if available < margin_required:
            return False, f"保证金不足: 需要${margin_required:.2f}, 可用${available:.2f}"

        # 检查保证金使用率
        new_ratio = (self.get_margin_used() + margin_required) / self.balance
        if new_ratio > self.config.max_margin_ratio:
            return False, f"保证金使用率过高: {new_ratio*100:.1f}% > {self.config.max_margin_ratio*100:.1f}%"

        return True, ""

    def calculate_position_size(
        self,
        price: float,
        stop_loss_pct: float = 1.5,
        risk_pct: Optional[float] = None
    ) -> float:
        """
        计算仓位大小（风险百分比法）

        Args:
            price: 当前价格
            stop_loss_pct: 止损距离百分比
            risk_pct: 单笔风险占权益百分比，None时使用配置值

        Returns:
            建议的持仓数量
        """
        if risk_pct is None:
            risk_pct = self.config.position_risk_pct

        equity = self.get_equity()

        # 单笔最大风险金额
        risk_amount = equity * (risk_pct / 100)

        # 根据止损距离计算仓位价值
        # 仓位价值 = 风险金额 / (止损距离% / 杠杆)
        position_value = risk_amount / (stop_loss_pct / 100) * self.leverage

        # 计算需要的保证金
        margin_required = position_value / self.leverage

        # 不超过可用保证金的 50%（留有余地）
        max_margin = self.get_available_margin() * 0.5
        margin_required = min(margin_required, max_margin)

        # 计算数量
        quantity = (margin_required * self.leverage) / price

        return quantity

    def calculate_margin(self, quantity: float, price: float) -> float:
        """
        计算所需保证金

        Args:
            quantity: 持仓数量
            price: 开仓价格

        Returns:
            所需保证金
        """
        position_value = quantity * price
        return position_value / self.leverage

    def calculate_commission(self, quantity: float, price: float, is_maker: bool = False) -> float:
        """
        计算手续费

        Args:
            quantity: 成交数量
            price: 成交价格
            is_maker: 是否为挂单

        Returns:
            手续费
        """
        fee_rate = self.maker_fee if is_maker else self.taker_fee
        return quantity * price * fee_rate

    def add_position(self, position: Position):
        """添加持仓"""
        self.positions[position.position_id] = position
        logger.debug(f"添加持仓: {position.symbol} {position.side.value} x{position.quantity}")

    def remove_position(self, position_id: str) -> Optional[Position]:
        """移除持仓"""
        return self.positions.pop(position_id, None)

    def record_trade(self, trade: Trade):
        """
        记录交易

        Args:
            trade: 交易记录
        """
        self.trades.append(trade)
        self.total_trades += 1

        if trade.is_win:
            self.win_trades += 1

        self.total_pnl += trade.realized_pnl
        self.balance += trade.realized_pnl

        # 更新最大回撤
        self._update_drawdown()

        logger.info(
            f"交易记录: {trade.symbol} {trade.side.value} "
            f"PnL=${trade.realized_pnl:+.2f} ({trade.roi:+.2f}% ROI) "
            f"原因={trade.exit_reason.value}"
        )

    def _update_drawdown(self):
        """更新最大回撤"""
        equity = self.get_equity()

        if equity > self.max_equity:
            self.max_equity = equity

        if equity < self.min_equity or self.min_equity == self.initial_balance:
            self.min_equity = equity

        # 计算当前回撤
        if self.max_equity > 0:
            current_drawdown = self.max_equity - equity
            current_drawdown_pct = current_drawdown / self.max_equity * 100

            if current_drawdown > self.max_drawdown:
                self.max_drawdown = current_drawdown
                self.max_drawdown_pct = current_drawdown_pct

    def record_equity(self, timestamp: Optional[datetime] = None):
        """
        记录当前权益（用于权益曲线）

        Args:
            timestamp: 时间戳，None时使用当前时间
        """
        if timestamp is None:
            timestamp = datetime.now()

        # 避免重复记录（同一秒）
        if self._last_equity_time and (timestamp - self._last_equity_time).total_seconds() < 1:
            return

        equity = self.get_equity()
        self.equity_history.append((timestamp, equity))
        self._last_equity_time = timestamp

        # 更新回撤
        self._update_drawdown()

    def get_state(self, timestamp: Optional[datetime] = None) -> AccountState:
        """
        获取账户状态快照

        Args:
            timestamp: 时间戳

        Returns:
            AccountState 对象
        """
        if timestamp is None:
            timestamp = datetime.now()

        return AccountState(
            timestamp=timestamp,
            balance=self.balance,
            equity=self.get_equity(),
            margin_used=self.get_margin_used(),
            margin_available=self.get_available_margin(),
            margin_ratio=self.get_margin_ratio(),
            open_positions=len(self.positions),
            total_trades=self.total_trades,
            win_trades=self.win_trades,
            total_pnl=self.total_pnl,
            max_drawdown=self.max_drawdown_pct,
            win_rate=self.win_trades / self.total_trades if self.total_trades > 0 else 0
        )

    def get_statistics(self) -> Dict:
        """
        获取交易统计

        Returns:
            统计字典
        """
        if not self.trades:
            return {
                "total_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_pnl": 0,
                "max_drawdown_pct": 0,
            }

        wins = [t for t in self.trades if t.is_win]
        losses = [t for t in self.trades if not t.is_win]

        avg_win = sum(t.realized_pnl for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t.realized_pnl for t in losses) / len(losses)) if losses else 0

        # 盈亏比
        profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')

        return {
            "total_trades": self.total_trades,
            "win_trades": self.win_trades,
            "loss_trades": len(losses),
            "win_rate": self.win_trades / self.total_trades,
            "total_pnl": self.total_pnl,
            "avg_pnl": self.total_pnl / self.total_trades,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "return_pct": (self.get_equity() / self.initial_balance - 1) * 100,
        }

    def reset(self):
        """重置账户到初始状态"""
        self.balance = self.initial_balance
        self.positions.clear()
        self.trades.clear()
        self.equity_history.clear()
        self._last_equity_time = None

        self.total_trades = 0
        self.win_trades = 0
        self.total_pnl = 0.0
        self.max_equity = self.initial_balance
        self.min_equity = self.initial_balance
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0

        logger.info("账户已重置")

    def __repr__(self) -> str:
        return (
            f"VirtualAccount(balance=${self.balance:,.2f}, "
            f"equity=${self.get_equity():,.2f}, "
            f"positions={len(self.positions)}, "
            f"trades={self.total_trades})"
        )
