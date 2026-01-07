"""
交易数据模型

定义交易系统核心数据结构：
- 订单方向、状态、平仓原因
- 交易信号、持仓、成交记录
- 账户状态、回测结果
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple


class OrderSide(Enum):
    """订单方向"""
    LONG = "long"       # 做多
    SHORT = "short"     # 做空


class OrderStatus(Enum):
    """订单状态"""
    PENDING = "pending"       # 挂单中
    FILLED = "filled"         # 已成交
    CANCELLED = "cancelled"   # 已取消
    REJECTED = "rejected"     # 已拒绝


class ExitReason(Enum):
    """平仓原因"""
    TAKE_PROFIT = "take_profit"       # 止盈
    STOP_LOSS = "stop_loss"           # 止损
    TRAILING_STOP = "trailing_stop"   # 移动止损
    TIME_EXIT = "time_exit"           # 时间止损
    SIGNAL_EXIT = "signal_exit"       # 信号平仓
    LIQUIDATION = "liquidation"       # 强制平仓
    MANUAL = "manual"                 # 手动平仓


@dataclass
class TradingSignal:
    """交易信号"""
    symbol: str
    timestamp: datetime
    side: OrderSide                   # 方向
    confidence: float                 # 置信度 0-1
    entry_price: float                # 建议入场价
    take_profit: Optional[float] = None   # 建议止盈价
    stop_loss: Optional[float] = None     # 建议止损价
    reason: str = ""                  # 信号原因

    def __post_init__(self):
        if isinstance(self.side, str):
            self.side = OrderSide(self.side)


@dataclass
class Position:
    """持仓"""
    position_id: str
    symbol: str
    side: OrderSide
    quantity: float               # 持仓数量
    entry_price: float            # 开仓均价
    entry_time: datetime
    leverage: int = 15            # 固定15倍杠杆
    margin: float = 0.0           # 占用保证金

    # 止盈止损
    take_profit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    trailing_stop_distance: Optional[float] = None  # 移动止损距离%
    max_hold_seconds: int = 900   # 最大持仓时间（默认15分钟）

    # 动态字段（实时更新）
    current_price: float = 0.0
    unrealized_pnl: float = 0.0       # 未实现盈亏(USDT)
    unrealized_pnl_pct: float = 0.0   # 未实现盈亏%
    highest_price: float = 0.0        # 持仓期间最高价（用于移动止损）
    lowest_price: float = 0.0         # 持仓期间最低价

    # 信号元数据
    signal_confidence: float = 0.0
    signal_reason: str = ""

    def __post_init__(self):
        if isinstance(self.side, str):
            self.side = OrderSide(self.side)
        # 初始化最高/最低价
        if self.highest_price == 0:
            self.highest_price = self.entry_price
        if self.lowest_price == 0:
            self.lowest_price = self.entry_price

    def update_pnl(self, current_price: float):
        """更新未实现盈亏"""
        self.current_price = current_price

        # 更新最高/最低价
        if current_price > self.highest_price:
            self.highest_price = current_price
        if current_price < self.lowest_price or self.lowest_price == 0:
            self.lowest_price = current_price

        # 计算盈亏
        if self.side == OrderSide.LONG:
            self.unrealized_pnl_pct = (current_price - self.entry_price) / self.entry_price * 100
        else:
            self.unrealized_pnl_pct = (self.entry_price - current_price) / self.entry_price * 100

        # 含杠杆的盈亏金额
        self.unrealized_pnl = self.margin * self.unrealized_pnl_pct / 100 * self.leverage

    @property
    def position_value(self) -> float:
        """持仓价值"""
        return self.quantity * self.current_price

    @property
    def hold_duration(self) -> float:
        """持仓时长（秒）"""
        return (datetime.now() - self.entry_time).total_seconds()

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "leverage": self.leverage,
            "margin": self.margin,
            "take_profit_price": self.take_profit_price,
            "stop_loss_price": self.stop_loss_price,
            "trailing_stop_distance": self.trailing_stop_distance,
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "signal_confidence": self.signal_confidence,
            "signal_reason": self.signal_reason,
        }


@dataclass
class Trade:
    """已平仓的交易记录"""
    trade_id: str
    symbol: str
    side: OrderSide
    quantity: float
    entry_price: float
    entry_time: datetime
    exit_price: float
    exit_time: datetime
    exit_reason: ExitReason
    leverage: int = 15

    # 盈亏
    realized_pnl: float = 0.0         # 已实现盈亏(USDT)
    realized_pnl_pct: float = 0.0     # 已实现盈亏%（不含杠杆）
    roi: float = 0.0                  # ROI（含杠杆）= pnl_pct * leverage

    # 费用
    commission: float = 0.0           # 手续费

    # 元数据
    signal_confidence: float = 0.0    # 入场信号置信度
    signal_reason: str = ""           # 入场原因
    margin: float = 0.0               # 使用保证金

    def __post_init__(self):
        if isinstance(self.side, str):
            self.side = OrderSide(self.side)
        if isinstance(self.exit_reason, str):
            self.exit_reason = ExitReason(self.exit_reason)

        # 自动计算盈亏
        if self.realized_pnl == 0 and self.entry_price > 0:
            self._calculate_pnl()

    def _calculate_pnl(self):
        """计算盈亏"""
        if self.side == OrderSide.LONG:
            self.realized_pnl_pct = (self.exit_price - self.entry_price) / self.entry_price * 100
        else:
            self.realized_pnl_pct = (self.entry_price - self.exit_price) / self.entry_price * 100

        # ROI = 收益率 * 杠杆
        self.roi = self.realized_pnl_pct * self.leverage

        # 盈亏金额 = 保证金 * ROI%
        if self.margin > 0:
            self.realized_pnl = self.margin * self.roi / 100 - self.commission

    @property
    def is_win(self) -> bool:
        """是否盈利"""
        return self.realized_pnl > 0

    @property
    def hold_duration(self) -> float:
        """持仓时长（秒）"""
        return (self.exit_time - self.entry_time).total_seconds()

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat(),
            "exit_reason": self.exit_reason.value,
            "leverage": self.leverage,
            "realized_pnl": self.realized_pnl,
            "realized_pnl_pct": self.realized_pnl_pct,
            "roi": self.roi,
            "commission": self.commission,
            "signal_confidence": self.signal_confidence,
            "signal_reason": self.signal_reason,
            "margin": self.margin,
        }


@dataclass
class AccountState:
    """账户状态快照"""
    timestamp: datetime
    balance: float                # 账户余额
    equity: float                 # 权益 = 余额 + 未实现盈亏
    margin_used: float            # 已用保证金
    margin_available: float       # 可用保证金
    margin_ratio: float = 0.0     # 保证金率
    open_positions: int = 0       # 持仓数量
    total_trades: int = 0         # 总交易次数
    win_trades: int = 0           # 盈利次数
    total_pnl: float = 0.0        # 累计盈亏
    max_drawdown: float = 0.0     # 最大回撤
    win_rate: float = 0.0         # 胜率

    def __post_init__(self):
        if self.total_trades > 0:
            self.win_rate = self.win_trades / self.total_trades

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "timestamp": self.timestamp.isoformat(),
            "balance": self.balance,
            "equity": self.equity,
            "margin_used": self.margin_used,
            "margin_available": self.margin_available,
            "margin_ratio": self.margin_ratio,
            "open_positions": self.open_positions,
            "total_trades": self.total_trades,
            "win_trades": self.win_trades,
            "total_pnl": self.total_pnl,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
        }


@dataclass
class BacktestResult:
    """回测结果"""
    symbol: str
    start_time: datetime
    end_time: datetime

    # 资金
    initial_balance: float
    final_balance: float
    final_equity: float
    total_return_pct: float       # 总收益率%

    # 交易统计
    total_trades: int
    win_trades: int
    loss_trades: int = 0
    win_rate: float = 0.0

    # 风险指标
    max_drawdown: float = 0.0     # 最大回撤%
    sharpe_ratio: float = 0.0     # 夏普比率
    profit_factor: float = 0.0    # 盈亏比
    calmar_ratio: float = 0.0     # 卡玛比率

    # 平均值
    avg_trade_pnl: float = 0.0    # 平均每笔盈亏
    avg_win: float = 0.0          # 平均盈利
    avg_loss: float = 0.0         # 平均亏损
    avg_hold_time: float = 0.0    # 平均持仓时间（秒）

    # 详细数据
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)

    def __post_init__(self):
        self.loss_trades = self.total_trades - self.win_trades
        if self.total_trades > 0:
            self.win_rate = self.win_trades / self.total_trades

    def summary(self) -> str:
        """生成回测摘要"""
        return f"""
========== 回测报告 ==========
交易对: {self.symbol}
时间范围: {self.start_time.strftime('%Y-%m-%d')} ~ {self.end_time.strftime('%Y-%m-%d')}

【资金表现】
初始资金: ${self.initial_balance:,.2f}
最终权益: ${self.final_equity:,.2f}
总收益率: {self.total_return_pct:+.2f}%

【交易统计】
总交易数: {self.total_trades}
盈利交易: {self.win_trades} ({self.win_rate*100:.1f}%)
亏损交易: {self.loss_trades}
平均每笔: ${self.avg_trade_pnl:+.2f}

【风险指标】
最大回撤: {self.max_drawdown:.2f}%
夏普比率: {self.sharpe_ratio:.2f}
盈亏比: {self.profit_factor:.2f}
================================
"""

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "symbol": self.symbol,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "initial_balance": self.initial_balance,
            "final_balance": self.final_balance,
            "final_equity": self.final_equity,
            "total_return_pct": self.total_return_pct,
            "total_trades": self.total_trades,
            "win_trades": self.win_trades,
            "loss_trades": self.loss_trades,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "profit_factor": self.profit_factor,
            "avg_trade_pnl": self.avg_trade_pnl,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
        }
