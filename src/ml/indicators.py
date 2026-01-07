"""
技术指标计算模块
提供MA、EMA、RSI、MACD、布林带等经典技术指标
"""
from typing import List, Optional, Tuple
import math


class TechnicalIndicators:
    """
    技术指标计算工具类

    所有方法都是静态方法，可直接调用
    输入: 价格列表（从旧到新排序）
    输出: 指标值
    """

    @staticmethod
    def sma(prices: List[float], period: int) -> Optional[float]:
        """
        简单移动平均 (Simple Moving Average)

        Args:
            prices: 价格列表（从旧到新）
            period: 周期数

        Returns:
            移动平均值，数据不足时返回None
        """
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    @staticmethod
    def ema(prices: List[float], period: int) -> Optional[float]:
        """
        指数移动平均 (Exponential Moving Average)

        Args:
            prices: 价格列表（从旧到新）
            period: 周期数

        Returns:
            EMA值，数据不足时返回None
        """
        if len(prices) < period:
            return None

        # 计算平滑系数
        multiplier = 2 / (period + 1)

        # 使用前period个数据的SMA作为初始EMA
        ema_value = sum(prices[:period]) / period

        # 从第period个数据开始计算EMA
        for price in prices[period:]:
            ema_value = (price - ema_value) * multiplier + ema_value

        return ema_value

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """
        相对强弱指标 (Relative Strength Index)

        Args:
            prices: 价格列表（从旧到新）
            period: 周期数，默认14

        Returns:
            RSI值(0-100)，数据不足时返回None
        """
        if len(prices) < period + 1:
            return None

        # 计算价格变化
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]

        # 分离涨跌
        gains = [max(0, c) for c in changes]
        losses = [max(0, -c) for c in changes]

        # 使用最近period个变化
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]

        # 计算平均涨跌
        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period

        if avg_loss == 0:
            return 100.0  # 全涨

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    @staticmethod
    def rsi_smooth(prices: List[float], period: int = 14) -> Optional[float]:
        """
        平滑RSI (使用EMA计算平均涨跌)

        更接近标准RSI实现
        """
        if len(prices) < period + 1:
            return None

        # 计算价格变化
        changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]

        gains = [max(0, c) for c in changes]
        losses = [max(0, -c) for c in changes]

        # 初始平均
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # 平滑计算
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def macd(
        prices: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Optional[Tuple[float, float, float]]:
        """
        MACD指标 (Moving Average Convergence Divergence)

        Args:
            prices: 价格列表（从旧到新）
            fast_period: 快线周期，默认12
            slow_period: 慢线周期，默认26
            signal_period: 信号线周期，默认9

        Returns:
            (macd_line, signal_line, histogram)
            数据不足时返回None
        """
        min_required = slow_period + signal_period
        if len(prices) < min_required:
            return None

        # 计算快慢EMA
        fast_ema = TechnicalIndicators.ema(prices, fast_period)
        slow_ema = TechnicalIndicators.ema(prices, slow_period)

        if fast_ema is None or slow_ema is None:
            return None

        # MACD线 = 快EMA - 慢EMA
        macd_line = fast_ema - slow_ema

        # 计算MACD线的历史值用于信号线
        macd_values = []
        for i in range(slow_period, len(prices) + 1):
            sub_prices = prices[:i]
            fast = TechnicalIndicators.ema(sub_prices, fast_period)
            slow = TechnicalIndicators.ema(sub_prices, slow_period)
            if fast is not None and slow is not None:
                macd_values.append(fast - slow)

        if len(macd_values) < signal_period:
            return None

        # 信号线 = MACD线的EMA
        signal_line = TechnicalIndicators.ema(macd_values, signal_period)
        if signal_line is None:
            signal_line = sum(macd_values[-signal_period:]) / signal_period

        # 柱状图 = MACD线 - 信号线
        histogram = macd_line - signal_line

        return (macd_line, signal_line, histogram)

    @staticmethod
    def bollinger_bands(
        prices: List[float],
        period: int = 20,
        std_dev: float = 2.0
    ) -> Optional[Tuple[float, float, float]]:
        """
        布林带 (Bollinger Bands)

        Args:
            prices: 价格列表（从旧到新）
            period: 周期数，默认20
            std_dev: 标准差倍数，默认2.0

        Returns:
            (upper_band, middle_band, lower_band)
            数据不足时返回None
        """
        if len(prices) < period:
            return None

        # 中轨 = SMA
        middle = TechnicalIndicators.sma(prices, period)
        if middle is None:
            return None

        # 计算标准差
        recent_prices = prices[-period:]
        variance = sum((p - middle) ** 2 for p in recent_prices) / period
        std = math.sqrt(variance)

        # 上下轨
        upper = middle + std_dev * std
        lower = middle - std_dev * std

        return (upper, middle, lower)

    @staticmethod
    def atr(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14
    ) -> Optional[float]:
        """
        平均真实波幅 (Average True Range)

        Args:
            highs: 最高价列表
            lows: 最低价列表
            closes: 收盘价列表
            period: 周期数，默认14

        Returns:
            ATR值，数据不足时返回None
        """
        if len(highs) < period + 1 or len(lows) < period + 1 or len(closes) < period + 1:
            return None

        tr_values = []
        for i in range(1, len(closes)):
            high = highs[i]
            low = lows[i]
            prev_close = closes[i-1]

            # 真实波幅 = max(H-L, |H-PC|, |L-PC|)
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        return sum(tr_values[-period:]) / period

    @staticmethod
    def volatility(prices: List[float], period: int = 20) -> Optional[float]:
        """
        价格波动率（对数收益率的标准差）

        Args:
            prices: 价格列表
            period: 周期数

        Returns:
            波动率（年化百分比），数据不足时返回None
        """
        if len(prices) < period + 1:
            return None

        # 计算对数收益率
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0 and prices[i] > 0:
                log_return = math.log(prices[i] / prices[i-1])
                returns.append(log_return)

        if len(returns) < period:
            return None

        recent_returns = returns[-period:]
        mean_return = sum(recent_returns) / len(recent_returns)
        variance = sum((r - mean_return) ** 2 for r in recent_returns) / len(recent_returns)

        # 返回标准差（百分比形式）
        return math.sqrt(variance) * 100

    @staticmethod
    def price_change_percent(prices: List[float], lookback: int = 1) -> Optional[float]:
        """
        价格变化百分比

        Args:
            prices: 价格列表
            lookback: 回看周期数

        Returns:
            变化百分比
        """
        if len(prices) < lookback + 1:
            return None

        old_price = prices[-(lookback + 1)]
        new_price = prices[-1]

        if old_price == 0:
            return None

        return ((new_price - old_price) / old_price) * 100

    @staticmethod
    def volume_ratio(volumes: List[float], current_volume: float, period: int = 10) -> Optional[float]:
        """
        成交量比率

        Args:
            volumes: 历史成交量列表
            current_volume: 当前成交量
            period: 计算平均的周期数

        Returns:
            当前成交量/平均成交量
        """
        if len(volumes) < period:
            return None

        avg_volume = sum(volumes[-period:]) / period
        if avg_volume == 0:
            return None

        return current_volume / avg_volume

    @staticmethod
    def momentum(prices: List[float], period: int = 10) -> Optional[float]:
        """
        动量指标

        Args:
            prices: 价格列表
            period: 周期数

        Returns:
            动量值 = 当前价格 - N周期前价格
        """
        if len(prices) < period + 1:
            return None

        return prices[-1] - prices[-(period + 1)]

    @staticmethod
    def roc(prices: List[float], period: int = 10) -> Optional[float]:
        """
        变动率 (Rate of Change)

        Args:
            prices: 价格列表
            period: 周期数

        Returns:
            ROC百分比
        """
        if len(prices) < period + 1:
            return None

        old_price = prices[-(period + 1)]
        if old_price == 0:
            return None

        return ((prices[-1] - old_price) / old_price) * 100

    @staticmethod
    def stochastic(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        k_period: int = 14,
        d_period: int = 3
    ) -> Optional[Tuple[float, float]]:
        """
        随机指标 (Stochastic Oscillator)

        Args:
            highs: 最高价列表
            lows: 最低价列表
            closes: 收盘价列表
            k_period: %K周期，默认14
            d_period: %D周期，默认3

        Returns:
            (%K, %D) 或 None
        """
        if len(highs) < k_period or len(lows) < k_period or len(closes) < k_period:
            return None

        # 计算%K值序列
        k_values = []
        for i in range(k_period - 1, len(closes)):
            start = i - k_period + 1
            end = i + 1

            highest_high = max(highs[start:end])
            lowest_low = min(lows[start:end])
            current_close = closes[i]

            if highest_high == lowest_low:
                k = 50.0  # 无波动时取中值
            else:
                k = ((current_close - lowest_low) / (highest_high - lowest_low)) * 100

            k_values.append(k)

        if len(k_values) < d_period:
            return None

        # %K = 最新的K值
        k = k_values[-1]

        # %D = %K的SMA
        d = sum(k_values[-d_period:]) / d_period

        return (k, d)

    @staticmethod
    def williams_r(
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14
    ) -> Optional[float]:
        """
        威廉指标 (Williams %R)

        Args:
            highs: 最高价列表
            lows: 最低价列表
            closes: 收盘价列表
            period: 周期数

        Returns:
            Williams %R值 (-100 到 0)
        """
        if len(highs) < period or len(lows) < period or len(closes) < period:
            return None

        highest_high = max(highs[-period:])
        lowest_low = min(lows[-period:])
        current_close = closes[-1]

        if highest_high == lowest_low:
            return -50.0

        return ((highest_high - current_close) / (highest_high - lowest_low)) * -100


class IndicatorCalculator:
    """
    指标计算器
    对价格序列计算多种技术指标并返回结果字典
    """

    def __init__(
        self,
        ma_periods: List[int] = None,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bb_period: int = 20,
        bb_std: float = 2.0
    ):
        self.ma_periods = ma_periods or [5, 20, 60]
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.bb_period = bb_period
        self.bb_std = bb_std

    def calculate_all(self, prices: List[float]) -> dict:
        """
        计算所有技术指标

        Args:
            prices: 价格列表（从旧到新）

        Returns:
            包含所有指标的字典
        """
        result = {}

        # 移动平均
        for period in self.ma_periods:
            result[f'ma_{period}'] = TechnicalIndicators.sma(prices, period) or 0.0

        # EMA
        result['ema_12'] = TechnicalIndicators.ema(prices, 12) or 0.0
        result['ema_26'] = TechnicalIndicators.ema(prices, 26) or 0.0

        # RSI
        result['rsi_14'] = TechnicalIndicators.rsi_smooth(prices, self.rsi_period) or 50.0

        # MACD
        macd = TechnicalIndicators.macd(
            prices, self.macd_fast, self.macd_slow, self.macd_signal
        )
        if macd:
            result['macd_line'] = macd[0]
            result['macd_signal'] = macd[1]
            result['macd_histogram'] = macd[2]
        else:
            result['macd_line'] = 0.0
            result['macd_signal'] = 0.0
            result['macd_histogram'] = 0.0

        # 布林带
        bb = TechnicalIndicators.bollinger_bands(prices, self.bb_period, self.bb_std)
        if bb:
            result['bollinger_upper'] = bb[0]
            result['bollinger_middle'] = bb[1]
            result['bollinger_lower'] = bb[2]
        else:
            result['bollinger_upper'] = 0.0
            result['bollinger_middle'] = prices[-1] if prices else 0.0
            result['bollinger_lower'] = 0.0

        # 波动率
        result['volatility'] = TechnicalIndicators.volatility(prices, 20) or 0.0

        # 动量
        result['momentum'] = TechnicalIndicators.momentum(prices, 10) or 0.0
        result['roc'] = TechnicalIndicators.roc(prices, 10) or 0.0

        return result
