"""
交易策略

实现 ML预测 + 技术指标过滤的混合策略：
- 基于RSI、MACD、订单簿失衡等指标生成方向信号
- 技术指标过滤（波动率、成交量、趋势一致性）
- 风险检查
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, List, TYPE_CHECKING

from loguru import logger

from .models import OrderSide, TradingSignal
from .stop_loss import StopLossManager, StopLossConfig

if TYPE_CHECKING:
    from ...models import MLFeatureVector


@dataclass
class StrategyConfig:
    """策略配置"""
    # 信号阈值
    min_confidence: float = 0.5       # 最小信号置信度
    signal_threshold: float = 0.4     # 信号分数阈值

    # 是否使用ML模型
    use_ml_model: bool = False        # 暂时只用规则
    indicator_filter: bool = True      # 启用技术指标过滤

    # RSI阈值
    rsi_oversold: float = 30          # RSI超卖阈值
    rsi_overbought: float = 70        # RSI超买阈值

    # 波动率和成交量过滤
    min_volatility: float = 0.3       # 最小波动率%
    min_volume_ratio: float = 0.5     # 最小成交量倍数

    # 订单簿失衡
    imbalance_long_threshold: float = 0.65   # 买盘强阈值
    imbalance_short_threshold: float = 0.35  # 卖盘强阈值

    # 趋势过滤
    trend_filter_pct: float = 1.0     # 趋势一致性过滤阈值%


class MLStrategy:
    """ML + 规则混合策略"""

    def __init__(
        self,
        config: Optional[StrategyConfig] = None,
        stop_loss_config: Optional[StopLossConfig] = None
    ):
        """
        初始化策略

        Args:
            config: 策略配置
            stop_loss_config: 止损配置
        """
        if config is None:
            config = StrategyConfig()
        if stop_loss_config is None:
            stop_loss_config = StopLossConfig()

        self.config = config
        self.stop_loss_manager = StopLossManager(stop_loss_config)
        self.ml_model = None  # 可选的ML模型

        logger.info(
            f"策略初始化: use_ml={config.use_ml_model}, "
            f"min_confidence={config.min_confidence}, "
            f"indicator_filter={config.indicator_filter}"
        )

    def generate_signal(
        self,
        symbol: str,
        feature: "MLFeatureVector",
        current_price: float
    ) -> Optional[TradingSignal]:
        """
        生成交易信号

        流程:
        1. ML模型预测（如有）或规则判断
        2. 技术指标过滤
        3. 风险检查
        4. 计算止盈止损
        5. 生成信号

        Args:
            symbol: 交易对
            feature: ML特征向量
            current_price: 当前价格

        Returns:
            交易信号，无信号返回 None
        """
        # 1. 方向预测
        direction, confidence, reasons = self._predict_direction(feature)

        if direction == 0:
            return None

        if confidence < self.config.min_confidence:
            logger.debug(f"{symbol} 信号置信度不足: {confidence:.2f} < {self.config.min_confidence}")
            return None

        # 2. 技术指标过滤
        if self.config.indicator_filter:
            passed, filter_reason = self._pass_indicator_filter(feature, direction)
            if not passed:
                logger.debug(f"{symbol} 指标过滤未通过: {filter_reason}")
                return None

        # 3. 风险检查
        passed, risk_reason = self._pass_risk_check(symbol, feature)
        if not passed:
            logger.debug(f"{symbol} 风险检查未通过: {risk_reason}")
            return None

        # 4. 计算止盈止损
        side = OrderSide.LONG if direction > 0 else OrderSide.SHORT
        stop_loss = self.stop_loss_manager.calculate_stop_loss(current_price, side)
        take_profit = self.stop_loss_manager.calculate_take_profit(current_price, side)

        # 5. 生成信号
        signal_reason = self._format_signal_reason(direction, reasons)

        signal = TradingSignal(
            symbol=symbol,
            timestamp=feature.timestamp if hasattr(feature, 'timestamp') else datetime.now(),
            side=side,
            confidence=confidence,
            entry_price=current_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason=signal_reason
        )

        logger.info(
            f"[信号] {symbol} {side.value.upper()} "
            f"置信度={confidence:.2f} 原因={signal_reason}"
        )

        return signal

    def _predict_direction(
        self,
        feature: "MLFeatureVector"
    ) -> Tuple[int, float, List[str]]:
        """
        预测方向

        使用规则策略生成方向和置信度：
        - RSI < 30 且 MACD金叉 → 做多
        - RSI > 70 且 MACD死叉 → 做空
        - 订单簿严重失衡 → 跟随方向
        - 价格反转信号 → 反向开仓

        Args:
            feature: ML特征向量

        Returns:
            (direction, confidence, reasons)
            direction: 1=做多, -1=做空, 0=无方向
            confidence: 置信度 0-1
            reasons: 触发原因列表
        """
        score = 0.0
        reasons = []

        # 获取特征值（带默认值）
        rsi = getattr(feature, 'rsi_14', 50)
        macd_line = getattr(feature, 'macd_line', 0)
        macd_signal = getattr(feature, 'macd_signal', 0)
        imbalance = getattr(feature, 'imbalance_ratio_10', 0.5)
        price_change_1m = getattr(feature, 'price_change_1m', 0)
        reversal_type = getattr(feature, 'reversal_type', None)

        # 1. RSI信号
        if rsi and rsi < self.config.rsi_oversold:
            score += 0.3
            reasons.append(f"RSI超卖({rsi:.1f})")
        elif rsi and rsi > self.config.rsi_overbought:
            score -= 0.3
            reasons.append(f"RSI超买({rsi:.1f})")

        # 2. MACD信号
        if macd_line is not None and macd_signal is not None:
            if macd_line > macd_signal:
                score += 0.2
                if "RSI超卖" in str(reasons):
                    reasons.append("MACD金叉")
            else:
                score -= 0.2
                if "RSI超买" in str(reasons):
                    reasons.append("MACD死叉")

        # 3. 订单簿失衡
        if imbalance is not None:
            if imbalance > self.config.imbalance_long_threshold:
                score += 0.25
                reasons.append(f"买盘强({imbalance:.2f})")
            elif imbalance < self.config.imbalance_short_threshold:
                score -= 0.25
                reasons.append(f"卖盘强({imbalance:.2f})")

        # 4. 短期动量
        if price_change_1m is not None:
            if price_change_1m > 0.5:
                score += 0.15
                reasons.append(f"1m涨{price_change_1m:.2f}%")
            elif price_change_1m < -0.5:
                score -= 0.15
                reasons.append(f"1m跌{price_change_1m:.2f}%")

        # 5. 反转信号（反向操作）
        if reversal_type:
            if reversal_type == "top":
                score -= 0.3
                reasons.append("见顶反转")
            elif reversal_type == "bottom":
                score += 0.3
                reasons.append("见底反转")

        # 转换为方向和置信度
        confidence = min(abs(score), 1.0)
        threshold = self.config.signal_threshold

        if score > threshold:
            return 1, confidence, reasons
        elif score < -threshold:
            return -1, confidence, reasons

        return 0, 0.0, reasons

    def _pass_indicator_filter(
        self,
        feature: "MLFeatureVector",
        direction: int
    ) -> Tuple[bool, str]:
        """
        技术指标过滤

        检查：
        - 波动率是否足够
        - 成交量是否足够
        - 趋势是否与信号一致

        Args:
            feature: ML特征
            direction: 信号方向

        Returns:
            (是否通过, 原因)
        """
        # 1. 波动率过滤（波动太小不开仓）
        volatility = getattr(feature, 'volatility_5m', 0)
        if volatility is not None and volatility < self.config.min_volatility:
            return False, f"波动率不足({volatility:.2f}%)"

        # 2. 成交量过滤（量能不足不开仓）
        volume_ratio = getattr(feature, 'volume_ratio_5m', 1)
        if volume_ratio is not None and volume_ratio < self.config.min_volume_ratio:
            return False, f"成交量不足({volume_ratio:.2f}x)"

        # 3. 趋势一致性过滤
        price_change_5m = getattr(feature, 'price_change_5m', 0)
        filter_pct = self.config.trend_filter_pct

        if price_change_5m is not None:
            if direction > 0 and price_change_5m < -filter_pct:
                return False, f"5m趋势向下({price_change_5m:.2f}%)"
            if direction < 0 and price_change_5m > filter_pct:
                return False, f"5m趋势向上({price_change_5m:.2f}%)"

        return True, ""

    def _pass_risk_check(
        self,
        symbol: str,
        feature: "MLFeatureVector"
    ) -> Tuple[bool, str]:
        """
        风险检查

        检查：
        - 价差是否过大
        - 深度是否充足
        - 是否存在假异动风险

        Args:
            symbol: 交易对
            feature: ML特征

        Returns:
            (是否通过, 原因)
        """
        # 1. 价差检查
        spread_bps = getattr(feature, 'spread_bps', 0)
        if spread_bps is not None and spread_bps > 100:  # 价差 > 1%
            return False, f"价差过大({spread_bps:.0f}bps)"

        # 2. 深度检查（通过失衡比判断）
        imbalance = getattr(feature, 'imbalance_ratio_10', 0.5)
        if imbalance is not None:
            # 极端失衡可能表示深度不足
            if imbalance > 0.95 or imbalance < 0.05:
                return False, f"深度失衡严重({imbalance:.2f})"

        return True, ""

    def _format_signal_reason(
        self,
        direction: int,
        reasons: List[str]
    ) -> str:
        """
        格式化信号原因

        Args:
            direction: 方向
            reasons: 原因列表

        Returns:
            格式化的原因字符串
        """
        if not reasons:
            return "规则信号"

        direction_str = "多" if direction > 0 else "空"
        return f"{direction_str}|{','.join(reasons[:3])}"

    def should_close(
        self,
        feature: "MLFeatureVector",
        current_side: OrderSide
    ) -> Tuple[bool, str]:
        """
        检查是否应该平仓（反向信号）

        Args:
            feature: ML特征
            current_side: 当前持仓方向

        Returns:
            (是否应该平仓, 原因)
        """
        direction, confidence, reasons = self._predict_direction(feature)

        # 如果有反向信号且置信度高
        if direction != 0 and confidence >= self.config.min_confidence:
            if current_side == OrderSide.LONG and direction < 0:
                return True, f"反向信号(空): {','.join(reasons[:2])}"
            if current_side == OrderSide.SHORT and direction > 0:
                return True, f"反向信号(多): {','.join(reasons[:2])}"

        return False, ""
