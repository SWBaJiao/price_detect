"""
标签生成器
严格避免未来函数：标签只能在T+N时刻之后计算

关键设计：
1. 延迟标签生成 - 只有当未来数据到达后才能生成标签
2. 时间戳验证 - 确保标签时间点 > 特征时间点
3. 增量更新 - 持续为历史特征补充标签
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

from loguru import logger

from ..models import MLFeatureVector, MLLabel
from .data_store import MLDataStore

if TYPE_CHECKING:
    from ..price_tracker import PriceTracker


class LabelGenerator:
    """
    延迟标签生成器

    工作原理：
    1. 存储待标注的特征（带时间戳）
    2. 当新价格数据到达时，检查是否可以为历史特征生成标签
    3. 标签生成后标记为已完成

    关键原则：
    - 标签只能在 feature_timestamp + max_window 之后生成
    - label_generated_at 字段记录实际生成时间，用于验证无未来函数
    """

    # 标签时间窗口配置（秒）
    LABEL_WINDOWS = {
        '1m': 60,
        '5m': 300,
        '15m': 900,
        '30m': 1800
    }

    def __init__(
        self,
        tracker: "PriceTracker",
        data_store: Optional[MLDataStore] = None,
        direction_threshold: float = 0.1,  # 方向标签阈值（%）
        max_pending_per_symbol: int = 500   # 每个交易对最大待标注数量
    ):
        """
        初始化标签生成器

        Args:
            tracker: 价格追踪器
            data_store: 数据存储（用于获取历史价格）
            direction_threshold: 方向标签阈值（%），超过此值才判定为涨/跌
            max_pending_per_symbol: 每个交易对最大待标注数量
        """
        self.tracker = tracker
        self.data_store = data_store
        self.direction_threshold = direction_threshold
        self.max_pending_per_symbol = max_pending_per_symbol

        # 待标注队列: {symbol: [(feature_timestamp, MLFeatureVector), ...]}
        self._pending: Dict[str, List[Tuple[datetime, MLFeatureVector]]] = defaultdict(list)

        # 最大等待时间（超过此时间仍无法标注则丢弃）
        self._max_wait_seconds = max(self.LABEL_WINDOWS.values()) + 600  # 最大窗口 + 10分钟缓冲

        # 统计
        self._generated_count = 0
        self._dropped_count = 0

        logger.info(f"标签生成器初始化: 窗口={list(self.LABEL_WINDOWS.keys())}, 方向阈值={direction_threshold}%")

    def register_feature(self, feature: MLFeatureVector):
        """
        注册一个待标注的特征

        Args:
            feature: 刚计算出的特征向量
        """
        symbol = feature.symbol
        timestamp = feature.timestamp

        # 添加到待标注队列
        self._pending[symbol].append((timestamp, feature))

        # 清理过期和超量的待标注项
        self._cleanup_pending(symbol)

    def try_generate_labels(self, symbol: str) -> List[MLLabel]:
        """
        尝试为待标注特征生成标签

        只有当所有必需的未来数据都已到达时才生成标签

        Args:
            symbol: 交易对

        Returns:
            本次成功生成的标签列表
        """
        generated = []
        now = datetime.now()

        pending = self._pending.get(symbol, [])
        still_pending = []

        for feature_ts, feature in pending:
            # 检查最长标签窗口是否已经过去
            max_window = max(self.LABEL_WINDOWS.values())
            elapsed = (now - feature_ts).total_seconds()

            if elapsed < max_window:
                # 未来数据还未到达，保持待标注状态
                still_pending.append((feature_ts, feature))
                continue

            # 可以生成标签了
            label = self._compute_label(symbol, feature_ts, feature)
            if label:
                generated.append(label)
                self._generated_count += 1
            else:
                self._dropped_count += 1
                logger.debug(f"无法为 {symbol}@{feature_ts} 生成标签（数据不足）")

        self._pending[symbol] = still_pending

        if generated:
            logger.debug(f"为 {symbol} 生成 {len(generated)} 个标签")

        return generated

    def try_generate_all_labels(self) -> Dict[str, List[MLLabel]]:
        """
        尝试为所有交易对生成标签

        Returns:
            {symbol: [labels]} 字典
        """
        all_labels = {}
        for symbol in list(self._pending.keys()):
            labels = self.try_generate_labels(symbol)
            if labels:
                all_labels[symbol] = labels
        return all_labels

    def _compute_label(
        self,
        symbol: str,
        feature_ts: datetime,
        feature: MLFeatureVector
    ) -> Optional[MLLabel]:
        """
        计算标签值

        关键：使用 feature_ts 作为基准点，计算 T+N 的价格变化

        Args:
            symbol: 交易对
            feature_ts: 特征时间点
            feature: 特征向量

        Returns:
            MLLabel 或 None
        """
        base_price = feature.price
        if base_price == 0:
            return None

        now = datetime.now()

        # 验证：确保当前时间已经超过所有标签窗口
        max_window = max(self.LABEL_WINDOWS.values())
        if (now - feature_ts).total_seconds() < max_window:
            logger.warning(f"标签生成时间验证失败: {symbol}@{feature_ts}")
            return None

        # 获取各时间窗口的未来价格
        returns = {}
        max_profits = {}
        max_drawdowns = {}

        for window_name, window_seconds in self.LABEL_WINDOWS.items():
            target_ts = feature_ts + timedelta(seconds=window_seconds)

            # 获取目标时间点的价格
            future_price = self._get_price_at_time(symbol, target_ts)
            if future_price:
                returns[window_name] = ((future_price - base_price) / base_price) * 100
            else:
                returns[window_name] = 0.0

            # 计算窗口内的最大浮盈/回撤（仅对5分钟窗口）
            if window_name == '5m':
                max_profit, max_dd = self._get_extremes_in_window(
                    symbol, feature_ts, target_ts, base_price
                )
                max_profits['5m'] = max_profit
                max_drawdowns['5m'] = max_dd

        # 方向标签（使用配置的阈值）
        def get_direction(ret: float) -> int:
            if ret > self.direction_threshold:
                return 1  # 涨
            elif ret < -self.direction_threshold:
                return -1  # 跌
            return 0  # 平

        return MLLabel(
            symbol=symbol,
            timestamp=feature_ts,
            return_1m=returns.get('1m', 0.0),
            return_5m=returns.get('5m', 0.0),
            return_15m=returns.get('15m', 0.0),
            return_30m=returns.get('30m', 0.0),
            direction_5m=get_direction(returns.get('5m', 0.0)),
            direction_15m=get_direction(returns.get('15m', 0.0)),
            max_profit_5m=max_profits.get('5m', 0.0),
            max_drawdown_5m=max_drawdowns.get('5m', 0.0),
            label_generated_at=now  # 记录实际生成时间
        )

    def _get_price_at_time(self, symbol: str, target_ts: datetime) -> Optional[float]:
        """
        获取指定时间点的价格

        优先从 PriceTracker 内存获取，不足时尝试从数据库获取
        """
        # 首先尝试从 PriceTracker 获取
        tracker = self.tracker._trackers.get(symbol)
        if tracker and tracker.price_history:
            # 找到最接近 target_ts 的价格点
            closest = None
            min_diff = float('inf')

            for point in tracker.price_history:
                diff = abs((point.timestamp - target_ts).total_seconds())
                if diff < min_diff:
                    min_diff = diff
                    closest = point

            # 只接受5秒内的数据
            if closest and min_diff < 5:
                return closest.price

        # 尝试从数据库获取
        if self.data_store:
            return self.data_store.get_price_at_time(symbol, target_ts, tolerance_seconds=5)

        return None

    def _get_extremes_in_window(
        self,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        base_price: float
    ) -> Tuple[float, float]:
        """
        计算窗口内的最大浮盈和最大回撤

        Returns:
            (max_profit_pct, max_drawdown_pct)
        """
        if base_price == 0:
            return 0.0, 0.0

        prices = []

        # 从 PriceTracker 获取
        tracker = self.tracker._trackers.get(symbol)
        if tracker:
            prices = [
                p.price for p in tracker.price_history
                if start_ts <= p.timestamp <= end_ts
            ]

        # 从数据库补充
        if not prices and self.data_store:
            db_prices = self.data_store.get_prices_in_window(symbol, start_ts, end_ts)
            prices = [p[1] for p in db_prices]

        if not prices:
            return 0.0, 0.0

        max_price = max(prices)
        min_price = min(prices)

        max_profit = ((max_price - base_price) / base_price) * 100
        max_drawdown = ((base_price - min_price) / base_price) * 100

        return max(0, max_profit), max(0, max_drawdown)

    def _cleanup_pending(self, symbol: str):
        """清理过期和超量的待标注项"""
        now = datetime.now()
        cutoff = now - timedelta(seconds=self._max_wait_seconds)

        pending = self._pending.get(symbol, [])

        # 过滤掉过期的
        valid = [(ts, f) for ts, f in pending if ts > cutoff]

        # 如果超过最大数量，删除最旧的
        if len(valid) > self.max_pending_per_symbol:
            dropped = len(valid) - self.max_pending_per_symbol
            valid = valid[-self.max_pending_per_symbol:]
            self._dropped_count += dropped
            logger.debug(f"{symbol} 待标注队列超限，丢弃 {dropped} 条")

        self._pending[symbol] = valid

    def get_pending_count(self, symbol: Optional[str] = None) -> int:
        """获取待标注数量"""
        if symbol:
            return len(self._pending.get(symbol, []))
        return sum(len(v) for v in self._pending.values())

    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            'pending_total': self.get_pending_count(),
            'pending_by_symbol': {s: len(v) for s, v in self._pending.items() if v},
            'generated_count': self._generated_count,
            'dropped_count': self._dropped_count
        }

    def clear_pending(self, symbol: Optional[str] = None):
        """清空待标注队列"""
        if symbol:
            self._pending[symbol] = []
        else:
            self._pending.clear()


class OfflineLabelGenerator:
    """
    离线标签生成器

    用于从数据库中的历史数据生成标签
    适用于回测和模型训练
    """

    def __init__(
        self,
        data_store: MLDataStore,
        direction_threshold: float = 0.1
    ):
        self.data_store = data_store
        self.direction_threshold = direction_threshold

    def generate_labels_for_features(
        self,
        symbol: Optional[str] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None,
        batch_size: int = 1000
    ) -> int:
        """
        为数据库中的特征生成标签

        Args:
            symbol: 交易对（可选，None表示所有）
            start_ts: 开始时间
            end_ts: 结束时间
            batch_size: 批量处理大小

        Returns:
            生成的标签数量
        """
        # 获取未标注的特征
        unlabeled = self.data_store.get_unlabeled_features(
            symbol=symbol,
            min_age_seconds=max(LabelGenerator.LABEL_WINDOWS.values()),
            limit=batch_size
        )

        if not unlabeled:
            return 0

        labels = []
        now = datetime.now()

        for feature_data in unlabeled:
            feature_ts_str = feature_data.get('timestamp')
            if not feature_ts_str:
                continue

            feature_ts = datetime.fromisoformat(feature_ts_str)
            sym = feature_data.get('symbol')
            base_price = feature_data.get('price', 0)

            if not sym or base_price == 0:
                continue

            # 验证时间已过
            max_window = max(LabelGenerator.LABEL_WINDOWS.values())
            if (now - feature_ts).total_seconds() < max_window:
                continue

            # 计算各窗口收益
            returns = {}
            for window_name, window_seconds in LabelGenerator.LABEL_WINDOWS.items():
                target_ts = feature_ts + timedelta(seconds=window_seconds)
                future_price = self.data_store.get_price_at_time(sym, target_ts)

                if future_price:
                    returns[window_name] = ((future_price - base_price) / base_price) * 100
                else:
                    returns[window_name] = 0.0

            # 计算5分钟窗口的最大浮盈/回撤
            end_5m = feature_ts + timedelta(seconds=300)
            prices_5m = self.data_store.get_prices_in_window(sym, feature_ts, end_5m)
            prices = [p[1] for p in prices_5m]

            max_profit_5m = 0.0
            max_drawdown_5m = 0.0
            if prices and base_price > 0:
                max_price = max(prices)
                min_price = min(prices)
                max_profit_5m = max(0, ((max_price - base_price) / base_price) * 100)
                max_drawdown_5m = max(0, ((base_price - min_price) / base_price) * 100)

            # 方向标签
            def get_direction(ret: float) -> int:
                if ret > self.direction_threshold:
                    return 1
                elif ret < -self.direction_threshold:
                    return -1
                return 0

            label = MLLabel(
                symbol=sym,
                timestamp=feature_ts,
                return_1m=returns.get('1m', 0.0),
                return_5m=returns.get('5m', 0.0),
                return_15m=returns.get('15m', 0.0),
                return_30m=returns.get('30m', 0.0),
                direction_5m=get_direction(returns.get('5m', 0.0)),
                direction_15m=get_direction(returns.get('15m', 0.0)),
                max_profit_5m=max_profit_5m,
                max_drawdown_5m=max_drawdown_5m,
                label_generated_at=now
            )
            labels.append(label)

        # 批量保存
        if labels:
            self.data_store.save_labels_batch(labels)
            logger.info(f"离线生成 {len(labels)} 个标签")

        return len(labels)
