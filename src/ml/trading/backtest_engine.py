"""
回测引擎

基于历史特征数据进行离线回测：
- 加载历史ML特征
- 按时间顺序模拟交易
- 生成回测报告
"""
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, TYPE_CHECKING

from loguru import logger

from .models import BacktestResult, ExitReason, OrderSide, Trade
from .account import VirtualAccount, AccountConfig
from .position_manager import PositionManager
from .stop_loss import StopLossManager, StopLossConfig
from .strategy import MLStrategy, StrategyConfig
from .trading_store import TradingDataStore

if TYPE_CHECKING:
    from ..data_store import MLDataStore
    from ...models import MLFeatureVector


@dataclass
class BacktestConfig:
    """回测配置"""
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"
    symbols: List[str] = None  # None表示所有

    # 是否保存交易到数据库
    save_trades: bool = False

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = []


class BacktestEngine:
    """回测引擎"""

    def __init__(
        self,
        ml_data_store: "MLDataStore",
        trading_store: Optional[TradingDataStore] = None,
        account_config: Optional[AccountConfig] = None,
        strategy_config: Optional[StrategyConfig] = None,
        stop_loss_config: Optional[StopLossConfig] = None,
        backtest_config: Optional[BacktestConfig] = None
    ):
        """
        初始化回测引擎

        Args:
            ml_data_store: ML数据存储（特征、标签）
            trading_store: 交易数据存储
            account_config: 账户配置
            strategy_config: 策略配置
            stop_loss_config: 止损配置
            backtest_config: 回测配置
        """
        self.ml_data_store = ml_data_store
        self.trading_store = trading_store
        self.backtest_config = backtest_config or BacktestConfig()

        # 初始化账户和策略
        self.account = VirtualAccount(account_config)
        self.stop_loss_manager = StopLossManager(stop_loss_config)
        self.position_manager = PositionManager(self.account, self.stop_loss_manager)
        self.strategy = MLStrategy(strategy_config, stop_loss_config)

        logger.info("回测引擎初始化完成")

    def run(
        self,
        symbol: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> BacktestResult:
        """
        执行单币种回测

        Args:
            symbol: 交易对
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            回测结果
        """
        # 解析时间
        if start_time is None:
            start_time = datetime.fromisoformat(self.backtest_config.start_date)
        if end_time is None:
            end_time = datetime.fromisoformat(self.backtest_config.end_date)

        logger.info(f"开始回测 {symbol}: {start_time} ~ {end_time}")

        # 重置账户
        self.account.reset()

        # 加载历史数据
        features = self._load_features(symbol, start_time, end_time)
        prices = self._load_prices(symbol, start_time, end_time)

        if not features:
            logger.warning(f"无历史特征数据: {symbol}")
            return self._empty_result(symbol, start_time, end_time)

        logger.info(f"加载 {len(features)} 条特征数据, {len(prices)} 条价格数据")

        # 按时间排序
        features = sorted(features, key=lambda x: x.get('timestamp', ''))
        price_map = {p.get('timestamp'): p.get('price') for p in prices}

        # 遍历特征进行回测
        for feature_dict in features:
            timestamp_str = feature_dict.get('timestamp', '')
            try:
                timestamp = datetime.fromisoformat(timestamp_str)
            except ValueError:
                continue

            # 获取当前价格
            current_price = price_map.get(timestamp_str)
            if current_price is None:
                # 尝试从特征中获取
                feature_json = feature_dict.get('feature_json', '{}')
                try:
                    feature_data = json.loads(feature_json) if isinstance(feature_json, str) else feature_json
                    current_price = feature_data.get('price', 0)
                except json.JSONDecodeError:
                    continue

            if current_price <= 0:
                continue

            # 转换特征
            feature = self._dict_to_feature(feature_dict)

            # 执行回测步骤
            self._backtest_step(symbol, feature, current_price, timestamp)

        # 平掉所有剩余持仓
        self._close_all_positions(symbol)

        # 生成报告
        result = self._generate_report(symbol, start_time, end_time)

        logger.info(f"回测完成: {result.summary()}")

        return result

    def run_multi(
        self,
        symbols: List[str],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> Dict[str, BacktestResult]:
        """
        执行多币种回测

        Args:
            symbols: 交易对列表
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            {symbol: BacktestResult}
        """
        results = {}
        for symbol in symbols:
            try:
                result = self.run(symbol, start_time, end_time)
                results[symbol] = result
            except Exception as e:
                logger.error(f"回测 {symbol} 失败: {e}")
                results[symbol] = self._empty_result(
                    symbol,
                    start_time or datetime.now(),
                    end_time or datetime.now()
                )

        return results

    def _backtest_step(
        self,
        symbol: str,
        feature: "MLFeatureVector",
        current_price: float,
        timestamp: datetime
    ):
        """
        执行单步回测

        Args:
            symbol: 交易对
            feature: 特征
            current_price: 当前价格
            timestamp: 时间戳
        """
        # 1. 更新持仓盈亏
        self.position_manager.update_positions_pnl({symbol: current_price})

        # 2. 检查止损/止盈
        positions = self.position_manager.get_positions(symbol)
        for position in positions:
            should_exit, reason = self.position_manager.check_exit(
                position, current_price, feature, timestamp
            )
            if should_exit:
                trade = self.position_manager.close_position(
                    position, current_price, reason, timestamp
                )
                if self.backtest_config.save_trades and self.trading_store:
                    self.trading_store.save_trade(trade)

        # 3. 生成新信号
        signal = self.strategy.generate_signal(symbol, feature, current_price)

        # 4. 执行开仓（如果有信号且没有同方向持仓）
        if signal:
            if not self.position_manager.has_position(symbol, signal.side):
                position = self.position_manager.open_position(
                    symbol=symbol,
                    side=signal.side,
                    price=current_price,
                    signal=signal,
                    timestamp=timestamp
                )
                if position and self.backtest_config.save_trades and self.trading_store:
                    self.trading_store.save_position(position)

        # 5. 记录权益曲线
        self.account.record_equity(timestamp)

    def _close_all_positions(self, symbol: str):
        """平掉所有持仓"""
        positions = self.position_manager.get_positions(symbol)
        for position in positions:
            trade = self.position_manager.close_position(
                position,
                position.current_price,
                ExitReason.MANUAL,
                datetime.now()
            )
            if self.backtest_config.save_trades and self.trading_store:
                self.trading_store.save_trade(trade)

    def _load_features(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Dict]:
        """加载历史特征"""
        try:
            return self.ml_data_store.get_features(symbol, start_time, end_time)
        except Exception as e:
            logger.error(f"加载特征失败: {e}")
            return []

    def _load_prices(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> List[Dict]:
        """加载历史价格"""
        try:
            return self.ml_data_store.get_price_snapshots(symbol, start_time, end_time)
        except Exception as e:
            logger.error(f"加载价格失败: {e}")
            return []

    def _dict_to_feature(self, feature_dict: Dict) -> "MLFeatureVector":
        """
        字典转特征对象

        Args:
            feature_dict: 特征字典

        Returns:
            MLFeatureVector 对象
        """
        from ...models import MLFeatureVector

        # 解析JSON
        feature_json = feature_dict.get('feature_json', '{}')
        try:
            data = json.loads(feature_json) if isinstance(feature_json, str) else feature_json
        except json.JSONDecodeError:
            data = {}

        # 解析时间戳
        timestamp_str = feature_dict.get('timestamp', '')
        try:
            timestamp = datetime.fromisoformat(timestamp_str)
        except ValueError:
            timestamp = datetime.now()

        # 构建特征对象
        return MLFeatureVector(
            symbol=feature_dict.get('symbol', ''),
            timestamp=timestamp,
            price=data.get('price', 0),
            price_change_1m=data.get('price_change_1m', 0),
            price_change_5m=data.get('price_change_5m', 0),
            price_change_15m=data.get('price_change_15m', 0),
            price_change_30m=data.get('price_change_30m', 0),
            volatility_1m=data.get('volatility_1m', 0),
            volatility_5m=data.get('volatility_5m', 0),
            volume_ratio_1m=data.get('volume_ratio_1m', 1),
            volume_ratio_5m=data.get('volume_ratio_5m', 1),
            quote_volume=data.get('quote_volume', 0),
            oi_change_5m=data.get('oi_change_5m', 0),
            spot_futures_spread=data.get('spot_futures_spread', 0),
            funding_rate=data.get('funding_rate'),
            imbalance_ratio_5=data.get('imbalance_ratio_5', 0.5),
            imbalance_ratio_10=data.get('imbalance_ratio_10', 0.5),
            bid_wall_distance=data.get('bid_wall_distance'),
            ask_wall_distance=data.get('ask_wall_distance'),
            spread_bps=data.get('spread_bps', 0),
            ma_5=data.get('ma_5', 0),
            ma_20=data.get('ma_20', 0),
            rsi_14=data.get('rsi_14', 50),
            macd_line=data.get('macd_line', 0),
            macd_signal=data.get('macd_signal', 0),
            macd_histogram=data.get('macd_histogram', 0),
            bb_upper=data.get('bb_upper', 0),
            bb_middle=data.get('bb_middle', 0),
            bb_lower=data.get('bb_lower', 0),
            reversal_type=data.get('reversal_type'),
            tier_label=data.get('tier_label', ''),
            alert_triggered=data.get('alert_triggered', False),
        )

    def _generate_report(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> BacktestResult:
        """生成回测报告"""
        trades = self.account.trades
        stats = self.account.get_statistics()

        # 计算夏普比率
        sharpe = self._calculate_sharpe_ratio()

        # 计算盈亏比
        profit_factor = stats.get('profit_factor', 0)

        # 计算平均持仓时间
        avg_hold_time = 0
        if trades:
            avg_hold_time = sum(t.hold_duration for t in trades) / len(trades)

        return BacktestResult(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            initial_balance=self.account.initial_balance,
            final_balance=self.account.balance,
            final_equity=self.account.get_equity(),
            total_return_pct=(self.account.get_equity() / self.account.initial_balance - 1) * 100,
            total_trades=len(trades),
            win_trades=sum(1 for t in trades if t.is_win),
            max_drawdown=self.account.max_drawdown_pct,
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
            avg_trade_pnl=stats.get('avg_pnl', 0),
            avg_win=stats.get('avg_win', 0),
            avg_loss=stats.get('avg_loss', 0),
            avg_hold_time=avg_hold_time,
            trades=trades,
            equity_curve=self.account.equity_history
        )

    def _calculate_sharpe_ratio(self, risk_free_rate: float = 0.02) -> float:
        """
        计算夏普比率

        Args:
            risk_free_rate: 无风险利率（年化）

        Returns:
            夏普比率
        """
        if len(self.account.equity_history) < 2:
            return 0

        # 计算每日收益率
        returns = []
        equity_values = [e[1] for e in self.account.equity_history]

        for i in range(1, len(equity_values)):
            if equity_values[i - 1] > 0:
                ret = (equity_values[i] - equity_values[i - 1]) / equity_values[i - 1]
                returns.append(ret)

        if not returns:
            return 0

        # 计算平均收益和标准差
        avg_return = sum(returns) / len(returns)
        variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
        std_return = math.sqrt(variance) if variance > 0 else 0

        if std_return == 0:
            return 0

        # 年化（假设每天一个数据点）
        annual_return = avg_return * 252
        annual_std = std_return * math.sqrt(252)

        sharpe = (annual_return - risk_free_rate) / annual_std if annual_std > 0 else 0

        return round(sharpe, 2)

    def _empty_result(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime
    ) -> BacktestResult:
        """生成空结果"""
        return BacktestResult(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            initial_balance=self.account.initial_balance,
            final_balance=self.account.initial_balance,
            final_equity=self.account.initial_balance,
            total_return_pct=0,
            total_trades=0,
            win_trades=0,
            max_drawdown=0,
            sharpe_ratio=0,
            profit_factor=0,
            avg_trade_pnl=0,
            avg_win=0,
            avg_loss=0
        )
