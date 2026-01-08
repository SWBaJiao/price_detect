"""
实时模拟交易引擎

与WebSocket行情数据同步：
- 实时接收特征更新
- 生成交易信号并执行
- 管理持仓和止损
- 定期保存账户状态
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, TYPE_CHECKING

from loguru import logger

from .models import AccountState, ExitReason, OrderSide, Trade
from .account import VirtualAccount, AccountConfig
from .position_manager import PositionManager
from .stop_loss import StopLossManager, StopLossConfig
from .strategy import MLStrategy, StrategyConfig
from .trading_store import TradingDataStore

if TYPE_CHECKING:
    from ...models import MLFeatureVector


@dataclass
class RealtimeConfig:
    """实时模拟配置"""
    enabled: bool = True
    save_interval: int = 60           # 账户状态保存间隔（秒）
    log_trades: bool = True           # 记录交易日志
    max_positions_per_symbol: int = 1 # 每个交易对最大持仓数
    allowed_symbols: List[str] = None # 允许交易的币种，None为全部

    def __post_init__(self):
        if self.allowed_symbols is None:
            self.allowed_symbols = []


class RealtimeSimEngine:
    """实时模拟交易引擎"""

    def __init__(
        self,
        trading_store: Optional[TradingDataStore] = None,
        account_config: Optional[AccountConfig] = None,
        strategy_config: Optional[StrategyConfig] = None,
        stop_loss_config: Optional[StopLossConfig] = None,
        realtime_config: Optional[RealtimeConfig] = None
    ):
        """
        初始化实时模拟引擎

        Args:
            trading_store: 交易数据存储
            account_config: 账户配置
            strategy_config: 策略配置
            stop_loss_config: 止损配置
            realtime_config: 实时配置
        """
        self.trading_store = trading_store
        self.config = realtime_config or RealtimeConfig()

        # 初始化账户和策略
        self.account = VirtualAccount(account_config)
        self.stop_loss_manager = StopLossManager(stop_loss_config)
        self.position_manager = PositionManager(self.account, self.stop_loss_manager)
        self.strategy = MLStrategy(strategy_config, stop_loss_config)

        # 状态管理
        self._running = False
        self._last_save_time = datetime.now()
        self._processed_symbols: Set[str] = set()

        # 最新价格缓存
        self._latest_prices: Dict[str, float] = {}

        # 统计
        self._signal_count = 0
        self._trade_count = 0

        logger.info(
            f"实时模拟引擎初始化: "
            f"初始资金=${self.account.initial_balance}, "
            f"杠杆={self.account.leverage}x"
        )

    async def on_feature_update(
        self,
        symbol: str,
        feature: "MLFeatureVector",
        current_price: float
    ):
        """
        特征更新时调用（与WebSocket数据同步）

        集成点: main.py 的 _handle_tickers() 中调用

        Args:
            symbol: 交易对
            feature: ML特征向量
            current_price: 当前价格
        """
        if not self._running:
            return

        # 检查是否允许交易该币种
        if self.config.allowed_symbols and symbol not in self.config.allowed_symbols:
            return

        # 更新价格缓存
        self._latest_prices[symbol] = current_price
        self._processed_symbols.add(symbol)

        timestamp = getattr(feature, 'timestamp', datetime.now())

        try:
            # 1. 更新持仓盈亏
            self.position_manager.update_positions_pnl({symbol: current_price})

            # 2. 检查止损/止盈
            await self._check_and_close_positions(symbol, current_price, feature, timestamp)

            # 3. 生成新信号并开仓
            await self._check_and_open_positions(symbol, feature, current_price, timestamp)

            # 4. 定期保存账户状态
            await self._maybe_save_state()

        except Exception as e:
            logger.error(f"处理 {symbol} 特征更新失败: {e}")

    async def _check_and_close_positions(
        self,
        symbol: str,
        current_price: float,
        feature: "MLFeatureVector",
        timestamp: datetime
    ):
        """检查并平仓"""
        positions = self.position_manager.get_positions(symbol)

        for position in positions:
            should_exit, reason = self.position_manager.check_exit(
                position, current_price, feature, timestamp
            )

            if should_exit:
                trade = self.position_manager.close_position(
                    position, current_price, reason, timestamp
                )

                # 保存交易记录
                if self.trading_store:
                    self.trading_store.save_trade(trade)

                self._trade_count += 1

                if self.config.log_trades:
                    self._log_trade(trade, "平仓")

    async def _check_and_open_positions(
        self,
        symbol: str,
        feature: "MLFeatureVector",
        current_price: float,
        timestamp: datetime
    ):
        """检查并开仓"""
        # 检查是否已有持仓
        current_positions = len(self.position_manager.get_positions(symbol))
        if current_positions >= self.config.max_positions_per_symbol:
            return

        # 生成信号
        signal = self.strategy.generate_signal(symbol, feature, current_price)

        if signal is None:
            return

        self._signal_count += 1

        # 检查是否已有同方向持仓
        if self.position_manager.has_position(symbol, signal.side):
            return

        # 执行开仓
        position = self.position_manager.open_position(
            symbol=symbol,
            side=signal.side,
            price=current_price,
            signal=signal,
            timestamp=timestamp
        )

        if position:
            # 保存持仓记录
            if self.trading_store:
                self.trading_store.save_position(position)

            if self.config.log_trades:
                logger.info(
                    f"[模拟开仓] {symbol} {signal.side.value.upper()} "
                    f"@ ${current_price:.4f} "
                    f"数量={position.quantity:.4f} "
                    f"置信度={signal.confidence:.2f} "
                    f"原因={signal.reason}"
                )

    async def _maybe_save_state(self):
        """定期保存账户状态"""
        now = datetime.now()
        elapsed = (now - self._last_save_time).total_seconds()

        if elapsed >= self.config.save_interval:
            self._last_save_time = now
            await self._save_account_state()

    async def _save_account_state(self):
        """保存账户状态"""
        if not self.trading_store:
            return

        state = self._get_account_state()
        self.trading_store.save_account_state(state)

        # 保存权益曲线点
        self.trading_store.save_equity_point(
            timestamp=state.timestamp,
            equity=state.equity,
            balance=state.balance,
            drawdown=state.max_drawdown,
            symbol="ALL"
        )

    def _get_account_state(self) -> AccountState:
        """获取当前账户状态"""
        stats = self.account.get_statistics()

        return AccountState(
            timestamp=datetime.now(),
            balance=self.account.balance,
            equity=self.account.get_equity(),
            margin_used=self.account.get_margin_used(),
            margin_available=self.account.get_available_margin(),
            margin_ratio=self.account.get_margin_ratio(),
            open_positions=len(self.account.positions),
            total_trades=len(self.account.trades),
            win_trades=stats.get('win_trades', 0),
            total_pnl=stats.get('total_pnl', 0),
            max_drawdown=self.account.max_drawdown_pct,
            win_rate=stats.get('win_rate', 0)
        )

    def _log_trade(self, trade: Trade, action: str):
        """记录交易日志"""
        pnl_emoji = "🟢" if trade.realized_pnl > 0 else "🔴"
        logger.info(
            f"[模拟{action}] {pnl_emoji} {trade.symbol} {trade.side.value.upper()} "
            f"入场=${trade.entry_price:.4f} → 出场=${trade.exit_price:.4f} "
            f"PnL=${trade.realized_pnl:+.2f} ({trade.roi:+.2f}% ROI) "
            f"原因={trade.exit_reason.value}"
        )

    def start(self):
        """启动实时模拟"""
        self._running = True
        self._last_save_time = datetime.now()

        logger.info(
            f"实时模拟交易引擎已启动 | "
            f"初始资金: ${self.account.initial_balance:.2f} | "
            f"杠杆: {self.account.leverage}x"
        )

    def stop(self):
        """停止实时模拟"""
        self._running = False

        # 保存最终状态
        if self.trading_store:
            state = self._get_account_state()
            self.trading_store.save_account_state(state)

        # 输出统计
        stats = self.get_statistics()
        logger.info(
            f"实时模拟交易引擎已停止 | "
            f"总信号: {stats['signal_count']} | "
            f"总交易: {stats['trade_count']} | "
            f"最终权益: ${stats['final_equity']:.2f} | "
            f"总收益: {stats['total_return_pct']:+.2f}%"
        )

    def is_running(self) -> bool:
        """检查是否正在运行"""
        return self._running

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        stats = self.account.get_statistics()

        return {
            'signal_count': self._signal_count,
            'trade_count': self._trade_count,
            'initial_balance': self.account.initial_balance,
            'current_balance': self.account.balance,
            'final_equity': self.account.get_equity(),
            'total_return_pct': (self.account.get_equity() / self.account.initial_balance - 1) * 100,
            'open_positions': len(self.account.positions),
            'total_trades': len(self.account.trades),
            'win_rate': stats.get('win_rate', 0),
            'profit_factor': stats.get('profit_factor', 0),
            'max_drawdown': self.account.max_drawdown_pct,
            'processed_symbols': len(self._processed_symbols)
        }

    def get_open_positions(self, symbol: Optional[str] = None) -> List:
        """获取当前持仓"""
        return self.position_manager.get_positions(symbol)

    def get_recent_trades(self, limit: int = 10) -> List[Trade]:
        """获取最近交易"""
        return self.account.trades[-limit:]

    async def close_all_positions(self, reason: ExitReason = ExitReason.MANUAL):
        """平掉所有持仓"""
        trades = self.position_manager.close_all_positions(
            prices=self._latest_prices,
            reason=reason,
            timestamp=datetime.now()
        )

        for trade in trades:
            if self.trading_store:
                self.trading_store.save_trade(trade)
            if self.config.log_trades:
                self._log_trade(trade, "强制平仓")

        return trades

    def reset(self):
        """重置引擎状态"""
        self.account.reset()
        self.position_manager = PositionManager(self.account, self.stop_loss_manager)
        self._signal_count = 0
        self._trade_count = 0
        self._processed_symbols.clear()
        self._latest_prices.clear()
        self._last_save_time = datetime.now()

        logger.info("实时模拟引擎已重置")

    def get_equity_curve(self) -> List[tuple]:
        """获取权益曲线"""
        return self.account.equity_history.copy()

    def format_status(self) -> str:
        """格式化当前状态（用于显示）"""
        stats = self.get_statistics()
        positions = self.get_open_positions()

        lines = [
            "═" * 50,
            "📊 实时模拟交易状态",
            "═" * 50,
            f"状态: {'🟢 运行中' if self._running else '🔴 已停止'}",
            f"初始资金: ${stats['initial_balance']:.2f}",
            f"当前权益: ${stats['final_equity']:.2f}",
            f"总收益率: {stats['total_return_pct']:+.2f}%",
            f"最大回撤: {stats['max_drawdown']:.2f}%",
            f"",
            f"信号数: {stats['signal_count']}",
            f"交易数: {stats['trade_count']}",
            f"胜率: {stats['win_rate']*100:.1f}%",
            f"盈亏比: {stats['profit_factor']:.2f}",
            f"",
            f"当前持仓: {len(positions)}",
        ]

        if positions:
            lines.append("-" * 50)
            for p in positions:
                pnl_emoji = "🟢" if p.unrealized_pnl >= 0 else "🔴"
                lines.append(
                    f"  {pnl_emoji} {p.symbol} {p.side.value.upper()} "
                    f"@ ${p.entry_price:.4f} "
                    f"PnL: ${p.unrealized_pnl:+.2f} ({p.unrealized_pnl_pct:+.2f}%)"
                )

        lines.append("═" * 50)

        return "\n".join(lines)
