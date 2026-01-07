"""
模拟交易模块

实现基于ML特征的模拟交易系统，支持：
- 虚拟账户管理（15倍杠杆）
- 多种止损策略
- 离线回测和实时模拟
"""
from .models import (
    OrderSide,
    OrderStatus,
    ExitReason,
    TradingSignal,
    Position,
    Trade,
    AccountState,
    BacktestResult
)
from .account import VirtualAccount, AccountConfig
from .position_manager import PositionManager
from .stop_loss import StopLossManager, StopLossConfig
from .strategy import MLStrategy, StrategyConfig
from .trading_store import TradingDataStore
from .backtest_engine import BacktestEngine, BacktestConfig
from .realtime_engine import RealtimeSimEngine, RealtimeConfig

__all__ = [
    # 数据模型
    "OrderSide",
    "OrderStatus",
    "ExitReason",
    "TradingSignal",
    "Position",
    "Trade",
    "AccountState",
    "BacktestResult",
    # 配置类
    "AccountConfig",
    "StopLossConfig",
    "StrategyConfig",
    "BacktestConfig",
    "RealtimeConfig",
    # 核心组件
    "VirtualAccount",
    "PositionManager",
    "StopLossManager",
    "MLStrategy",
    "TradingDataStore",
    # 引擎
    "BacktestEngine",
    "RealtimeSimEngine",
]
