"""
特征工程引擎
将现有监控信号转换为结构化ML特征向量
"""
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, TYPE_CHECKING
import math

from loguru import logger

from ..models import MLFeatureVector, OrderBookSnapshot, TickerData
from .indicators import IndicatorCalculator, TechnicalIndicators

if TYPE_CHECKING:
    from ..price_tracker import PriceTracker
    from ..orderbook_monitor import OrderBookMonitor


class FeatureEngine:
    """
    特征工程引擎

    职责：
    1. 聚合各模块数据生成统一特征向量
    2. 多时间窗口特征计算
    3. 整合技术指标
    """

    def __init__(
        self,
        tracker: "PriceTracker",
        orderbook_monitor: Optional["OrderBookMonitor"] = None,
        tier_classifier: Optional[Callable[[float], any]] = None,
        indicator_config: dict = None
    ):
        """
        初始化特征引擎

        Args:
            tracker: 价格追踪器实例
            orderbook_monitor: 订单簿监控器实例（可选）
            tier_classifier: 分层分类函数 (quote_volume) -> VolumeTier
            indicator_config: 技术指标配置
        """
        self.tracker = tracker
        self.orderbook_monitor = orderbook_monitor
        self.tier_classifier = tier_classifier

        # 技术指标计算器
        config = indicator_config or {}
        self.indicator_calc = IndicatorCalculator(
            ma_periods=config.get('ma_periods', [5, 20, 60]),
            rsi_period=config.get('rsi_period', 14),
            macd_fast=config.get('macd_fast', 12),
            macd_slow=config.get('macd_slow', 26),
            macd_signal=config.get('macd_signal', 9),
            bb_period=config.get('bb_period', 20),
            bb_std=config.get('bb_std', 2.0)
        )

        # 特征缓存（用于批量处理）
        self._feature_cache: Dict[str, List[MLFeatureVector]] = {}

        logger.info("特征引擎初始化完成")

    def compute_features(
        self,
        symbol: str,
        snapshot: Optional[OrderBookSnapshot] = None,
        ticker: Optional[TickerData] = None
    ) -> Optional[MLFeatureVector]:
        """
        计算单个交易对的完整特征向量

        Args:
            symbol: 交易对
            snapshot: 订单簿快照（可选）
            ticker: 最新行情数据（可选）

        Returns:
            MLFeatureVector 或 None（数据不足时）
        """
        try:
            # 获取基础追踪器
            sym_tracker = self.tracker._trackers.get(symbol)
            if not sym_tracker or len(sym_tracker.price_history) < 5:
                return None

            now = datetime.now()
            current_price = sym_tracker.latest_price
            if current_price == 0:
                return None

            # === 价格特征（多时间窗口）===
            price_1m = self._get_price_change_window(symbol, 60)
            price_5m = self._get_price_change_window(symbol, 300)
            price_15m = self._get_price_change_window(symbol, 900)

            # === 波动率特征 ===
            volatility_1m = self._compute_volatility(symbol, 60)
            volatility_5m = self._compute_volatility(symbol, 300)

            # === 成交量特征 ===
            volume_ratio_1m = self._get_volume_ratio_window(symbol, periods=6)
            volume_ratio_5m = self._get_volume_ratio_window(symbol, periods=30)

            # === OI特征 ===
            oi_5m = self.tracker.get_oi_change(symbol) or 0.0
            oi_15m = self._get_oi_change_window(symbol, 900)

            # === 现货-合约价差 ===
            spread_data = self.tracker.get_spot_futures_spread(symbol)
            spot_spread = spread_data[0] if spread_data else 0.0

            # === 订单簿特征 ===
            ob_features = self._extract_orderbook_features(symbol, snapshot)

            # === 技术指标 ===
            prices = [p.price for p in sym_tracker.price_history]
            indicators = self.indicator_calc.calculate_all(prices)

            # === 反转检测 ===
            reversal = self.tracker.get_price_reversal(symbol)
            reversal_type = None
            reversal_rise = 0.0
            reversal_fall = 0.0
            if reversal:
                reversal_type = reversal.get("type")
                reversal_rise = reversal.get("rise_percent", 0.0)
                reversal_fall = reversal.get("fall_percent", 0.0)

            # === 分层 ===
            tier_label = ""
            if self.tier_classifier:
                tier = self.tier_classifier(sym_tracker.latest_quote_volume)
                if tier:
                    tier_label = tier.label if hasattr(tier, 'label') else str(tier)

            return MLFeatureVector(
                symbol=symbol,
                timestamp=now,
                price=current_price,
                price_change_1m=price_1m,
                price_change_5m=price_5m,
                price_change_15m=price_15m,
                volatility_1m=volatility_1m,
                volatility_5m=volatility_5m,
                volume_ratio_1m=volume_ratio_1m,
                volume_ratio_5m=volume_ratio_5m,
                quote_volume=sym_tracker.latest_quote_volume,
                oi_change_5m=oi_5m,
                oi_change_15m=oi_15m,
                spot_futures_spread=spot_spread,
                funding_rate=None,  # 需要额外API调用
                imbalance_ratio_5=ob_features.get('imbalance_ratio_5', 0.0),
                imbalance_ratio_10=ob_features.get('imbalance_ratio_10', 0.0),
                imbalance_ratio_20=ob_features.get('imbalance_ratio_20', 0.0),
                bid_wall_distance=ob_features.get('bid_wall_distance'),
                ask_wall_distance=ob_features.get('ask_wall_distance'),
                bid_wall_value=ob_features.get('bid_wall_value'),
                ask_wall_value=ob_features.get('ask_wall_value'),
                spread_bps=ob_features.get('spread_bps', 0.0),
                ma_5=indicators.get('ma_5', 0.0),
                ma_20=indicators.get('ma_20', 0.0),
                ma_60=indicators.get('ma_60', 0.0),
                ema_12=indicators.get('ema_12', 0.0),
                ema_26=indicators.get('ema_26', 0.0),
                rsi_14=indicators.get('rsi_14', 50.0),
                macd_line=indicators.get('macd_line', 0.0),
                macd_signal=indicators.get('macd_signal', 0.0),
                macd_histogram=indicators.get('macd_histogram', 0.0),
                bollinger_upper=indicators.get('bollinger_upper', 0.0),
                bollinger_middle=indicators.get('bollinger_middle', 0.0),
                bollinger_lower=indicators.get('bollinger_lower', 0.0),
                reversal_type=reversal_type,
                reversal_rise_pct=reversal_rise,
                reversal_fall_pct=reversal_fall,
                tier_label=tier_label,
                alert_triggered=False,
                alert_types=[]
            )

        except Exception as e:
            logger.error(f"计算特征失败 {symbol}: {e}")
            return None

    def compute_features_batch(
        self,
        symbols: List[str],
        snapshots: Dict[str, OrderBookSnapshot] = None
    ) -> List[MLFeatureVector]:
        """
        批量计算多个交易对的特征

        Args:
            symbols: 交易对列表
            snapshots: 订单簿快照字典 {symbol: snapshot}

        Returns:
            特征向量列表
        """
        snapshots = snapshots or {}
        features = []

        for symbol in symbols:
            feature = self.compute_features(
                symbol,
                snapshot=snapshots.get(symbol)
            )
            if feature:
                features.append(feature)

        return features

    def _get_price_change_window(self, symbol: str, window_seconds: int) -> float:
        """计算指定时间窗口的价格变化百分比"""
        sym_tracker = self.tracker._trackers.get(symbol)
        if not sym_tracker or len(sym_tracker.price_history) < 2:
            return 0.0

        now = datetime.now()
        window_start = now - timedelta(seconds=window_seconds)

        # 找到窗口起始点的价格
        window_prices = [
            p for p in sym_tracker.price_history
            if p.timestamp >= window_start
        ]

        if len(window_prices) < 2:
            return 0.0

        start_price = window_prices[0].price
        current_price = sym_tracker.latest_price

        if start_price == 0:
            return 0.0

        return ((current_price - start_price) / start_price) * 100

    def _compute_volatility(self, symbol: str, window_seconds: int) -> float:
        """计算指定窗口内的价格波动率（收益率标准差）"""
        sym_tracker = self.tracker._trackers.get(symbol)
        if not sym_tracker:
            return 0.0

        now = datetime.now()
        window_start = now - timedelta(seconds=window_seconds)

        prices = [
            p.price for p in sym_tracker.price_history
            if p.timestamp >= window_start
        ]

        if len(prices) < 3:
            return 0.0

        # 计算收益率
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
                returns.append(ret)

        if len(returns) < 2:
            return 0.0

        # 计算标准差
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)

        return math.sqrt(variance)

    def _get_volume_ratio_window(self, symbol: str, periods: int) -> float:
        """计算成交量比率"""
        sym_tracker = self.tracker._trackers.get(symbol)
        if not sym_tracker or len(sym_tracker.price_history) < periods + 1:
            return 1.0

        recent = list(sym_tracker.price_history)[-periods-1:-1]
        volumes = [p.volume for p in recent]

        if not volumes or sum(volumes) == 0:
            return 1.0

        avg_volume = sum(volumes) / len(volumes)
        if avg_volume == 0:
            return 1.0

        return sym_tracker.latest_volume / avg_volume

    def _get_oi_change_window(self, symbol: str, window_seconds: int) -> float:
        """计算指定窗口内的OI变化"""
        sym_tracker = self.tracker._trackers.get(symbol)
        if not sym_tracker or len(sym_tracker.oi_history) < 2:
            return 0.0

        now = datetime.now()
        window_start = now - timedelta(seconds=window_seconds)

        window_oi = [
            (ts, oi) for ts, oi in sym_tracker.oi_history
            if ts >= window_start
        ]

        if len(window_oi) < 2:
            return 0.0

        start_oi = window_oi[0][1]
        current_oi = sym_tracker.latest_oi

        if start_oi == 0:
            return 0.0

        return ((current_oi - start_oi) / start_oi) * 100

    def _extract_orderbook_features(
        self,
        symbol: str,
        snapshot: Optional[OrderBookSnapshot]
    ) -> dict:
        """提取订单簿特征"""
        default = {
            "imbalance_ratio_5": 0.0,
            "imbalance_ratio_10": 0.0,
            "imbalance_ratio_20": 0.0,
            "bid_wall_distance": None,
            "ask_wall_distance": None,
            "bid_wall_value": None,
            "ask_wall_value": None,
            "spread_bps": 0.0
        }

        # 如果有直接传入的快照，优先使用
        if snapshot:
            try:
                mid_price = (snapshot.best_bid + snapshot.best_ask) / 2 if snapshot.best_bid and snapshot.best_ask else 0

                result = {
                    "imbalance_ratio_5": snapshot.imbalance_ratio(5),
                    "imbalance_ratio_10": snapshot.imbalance_ratio(10),
                    "imbalance_ratio_20": snapshot.imbalance_ratio(20),
                    "spread_bps": (snapshot.spread_percent or 0) * 100,
                    "bid_wall_distance": None,
                    "ask_wall_distance": None,
                    "bid_wall_value": None,
                    "ask_wall_value": None
                }

                # 尝试从订单簿监控器获取大单墙信息
                if self.orderbook_monitor and mid_price > 0:
                    walls = self._get_wall_info(symbol, mid_price)
                    result.update(walls)

                return result

            except Exception as e:
                logger.debug(f"提取订单簿特征失败 {symbol}: {e}")
                return default

        # 没有快照时，尝试从监控器获取
        if self.orderbook_monitor:
            try:
                # 获取深度信息
                depth_info = self.orderbook_monitor.get_depth_info(symbol) if hasattr(self.orderbook_monitor, 'get_depth_info') else None
                if depth_info:
                    return {
                        "imbalance_ratio_5": 0.0,
                        "imbalance_ratio_10": depth_info.get("imbalance_ratio", 0.0),
                        "imbalance_ratio_20": 0.0,
                        "bid_wall_distance": None,
                        "ask_wall_distance": None,
                        "bid_wall_value": None,
                        "ask_wall_value": None,
                        "spread_bps": depth_info.get("spread_percent", 0.0) * 100
                    }
            except Exception:
                pass

        return default

    def _get_wall_info(self, symbol: str, mid_price: float) -> dict:
        """从订单簿监控器获取大单墙信息"""
        result = {
            "bid_wall_distance": None,
            "ask_wall_distance": None,
            "bid_wall_value": None,
            "ask_wall_value": None
        }

        if not self.orderbook_monitor:
            return result

        try:
            # 获取跟踪的大单墙
            if hasattr(self.orderbook_monitor, 'get_tracked_walls'):
                walls = self.orderbook_monitor.get_tracked_walls(symbol)
                if walls:
                    bid_walls = [w for w in walls if w.side == "bid"]
                    ask_walls = [w for w in walls if w.side == "ask"]

                    if bid_walls and mid_price > 0:
                        closest_bid = max(bid_walls, key=lambda w: w.price)
                        result["bid_wall_distance"] = ((mid_price - closest_bid.price) / mid_price) * 100
                        result["bid_wall_value"] = max(w.value for w in bid_walls)

                    if ask_walls and mid_price > 0:
                        closest_ask = min(ask_walls, key=lambda w: w.price)
                        result["ask_wall_distance"] = ((closest_ask.price - mid_price) / mid_price) * 100
                        result["ask_wall_value"] = max(w.value for w in ask_walls)

        except Exception as e:
            logger.debug(f"获取大单墙信息失败 {symbol}: {e}")

        return result

    def mark_alert(
        self,
        feature: MLFeatureVector,
        alert_type: str,
        triggered: bool = True
    ):
        """标记特征向量的告警状态"""
        feature.alert_triggered = triggered
        if alert_type not in feature.alert_types:
            feature.alert_types.append(alert_type)

    def get_all_symbols(self) -> List[str]:
        """获取所有正在追踪的交易对"""
        return list(self.tracker._trackers.keys())

    def to_dict(self, feature: MLFeatureVector) -> dict:
        """将特征向量转换为字典"""
        from dataclasses import asdict
        result = asdict(feature)
        result['timestamp'] = feature.timestamp.isoformat()
        return result

    def to_array(self, feature: MLFeatureVector) -> List[float]:
        """
        将特征向量转换为数值数组（用于ML模型输入）

        Returns:
            数值特征列表（不含symbol、timestamp等非数值字段）
        """
        numeric_fields = [
            'price', 'price_change_1m', 'price_change_5m', 'price_change_15m',
            'volatility_1m', 'volatility_5m', 'volume_ratio_1m', 'volume_ratio_5m',
            'quote_volume', 'oi_change_5m', 'oi_change_15m', 'spot_futures_spread',
            'imbalance_ratio_5', 'imbalance_ratio_10', 'imbalance_ratio_20',
            'spread_bps', 'ma_5', 'ma_20', 'ma_60', 'ema_12', 'ema_26',
            'rsi_14', 'macd_line', 'macd_signal', 'macd_histogram',
            'bollinger_upper', 'bollinger_middle', 'bollinger_lower',
            'reversal_rise_pct', 'reversal_fall_pct'
        ]

        values = []
        for field in numeric_fields:
            v = getattr(feature, field, 0)
            values.append(float(v) if v is not None else 0.0)

        return values

    @staticmethod
    def get_feature_names() -> List[str]:
        """获取数值特征名称列表"""
        return [
            'price', 'price_change_1m', 'price_change_5m', 'price_change_15m',
            'volatility_1m', 'volatility_5m', 'volume_ratio_1m', 'volume_ratio_5m',
            'quote_volume', 'oi_change_5m', 'oi_change_15m', 'spot_futures_spread',
            'imbalance_ratio_5', 'imbalance_ratio_10', 'imbalance_ratio_20',
            'spread_bps', 'ma_5', 'ma_20', 'ma_60', 'ema_12', 'ema_26',
            'rsi_14', 'macd_line', 'macd_signal', 'macd_histogram',
            'bollinger_upper', 'bollinger_middle', 'bollinger_lower',
            'reversal_rise_pct', 'reversal_fall_pct'
        ]
