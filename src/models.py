"""
数据模型定义
定义系统中使用的所有数据结构
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AlertType(Enum):
    """告警类型枚举"""
    PRICE_CHANGE = "price_change"          # 价格异动
    VOLUME_SPIKE = "volume_spike"          # 成交量突增
    OI_CHANGE = "oi_change"                # 持仓量变化
    SPOT_FUTURES_SPREAD = "spot_futures_spread"  # 现货合约价差
    PRICE_REVERSAL = "price_reversal"      # 价格反转
    # 订单簿相关
    ORDERBOOK_WALL = "orderbook_wall"      # 大单墙（买墙/卖墙）
    ORDERBOOK_IMBALANCE = "orderbook_imbalance"  # 深度失衡
    ORDERBOOK_SWEEP = "orderbook_sweep"    # 大单扫盘


@dataclass
class TickerData:
    """
    行情快照数据
    来源: Binance !miniTicker@arr WebSocket 推送
    """
    symbol: str                        # 交易对，如 BTCUSDT
    price: float                       # 最新价格
    volume: float                      # 24h 成交量（基础货币）
    quote_volume: float                # 24h 成交额（USDT）
    timestamp: datetime = field(default_factory=datetime.now)

    # 持仓量（通过 REST API 获取，可能为空）
    open_interest: Optional[float] = None
    open_interest_value: Optional[float] = None  # 持仓价值(USDT)


@dataclass
class SpotTickerData:
    """
    现货行情快照数据
    来源: Binance Spot API
    """
    symbol: str                        # 交易对，如 BTCUSDT
    price: float                       # 最新价格
    volume: float                      # 24h 成交量（基础货币）
    quote_volume: float                # 24h 成交额（USDT）
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class PricePoint:
    """价格时间点，用于滑动窗口存储"""
    price: float
    volume: float
    timestamp: datetime


@dataclass
class VolumeTier:
    """成交额分层配置"""
    min_quote_volume: float            # 24h 成交额下限
    price_threshold: float             # 价格变化阈值(%)
    volume_threshold: float            # 成交量倍数阈值
    oi_threshold: float                # 持仓量变化阈值(%)
    label: str                         # 层级标签


@dataclass
class AlertEvent:
    """
    告警事件
    包含触发告警的所有相关信息
    """
    symbol: str                        # 交易对
    alert_type: AlertType              # 告警类型
    tier_label: str                    # 所属层级
    current_price: float               # 当前价格
    change_percent: float              # 变化百分比
    threshold: float                   # 触发阈值
    time_window: int                   # 时间窗口（秒）
    timestamp: datetime = field(default_factory=datetime.now)

    # 附加信息（根据告警类型填充）
    extra_info: dict = field(default_factory=dict)

    def format_message(self) -> str:
        """格式化告警消息"""
        # 现货合约价差专用格式
        if self.alert_type == AlertType.SPOT_FUTURES_SPREAD:
            return self._format_spread_message()

        # 价格反转专用格式
        if self.alert_type == AlertType.PRICE_REVERSAL:
            return self._format_reversal_message()

        # 订单簿告警专用格式
        if self.alert_type in (AlertType.ORDERBOOK_WALL, AlertType.ORDERBOOK_IMBALANCE, AlertType.ORDERBOOK_SWEEP):
            return self._format_orderbook_message()

        # 原有的合约告警格式
        emoji_map = {
            AlertType.PRICE_CHANGE: "📈" if self.change_percent > 0 else "📉",
            AlertType.VOLUME_SPIKE: "📊",
            AlertType.OI_CHANGE: "💰",
        }

        type_name_map = {
            AlertType.PRICE_CHANGE: "价格异动",
            AlertType.VOLUME_SPIKE: "成交量突增",
            AlertType.OI_CHANGE: "持仓量变化",
        }

        emoji = emoji_map.get(self.alert_type, "🚨")
        type_name = type_name_map.get(self.alert_type, "异动")

        # 基础消息
        lines = [
            f"{emoji} *{type_name}告警*",
            "",
            f"📌 币种: `{self.symbol}`",
            f"📊 层级: {self.tier_label}",
            f"💵 价格: ${self.current_price:.4f}",
            f"📈 变化: {self.change_percent:+.2f}%",
            f"⚡ 阈值: {self.threshold:.2f}%",
            f"⏱ 窗口: {self.time_window}秒",
            f"🕐 时间: {self.timestamp.strftime('%H:%M:%S')}",
        ]

        # 附加信息
        if self.extra_info:
            lines.append("")
            for key, value in self.extra_info.items():
                lines.append(f"• {key}: {value}")

        # 添加查询提示（提取基础币种名称）
        base_symbol = self.symbol.replace("USDT", "")
        lines.extend([
            "",
            f"💬 回复 `/info {base_symbol}` 查看K线详情"
        ])

        return "\n".join(lines)

    def _format_spread_message(self) -> str:
        """
        格式化现货合约价差告警消息
        使用独特的样式，与合约告警明显区分
        """
        # 判断价差方向
        spread_emoji = "🔺" if self.change_percent > 0 else "🔻"
        direction = "现货溢价" if self.change_percent > 0 else "合约溢价"

        lines = [
            "═" * 30,
            f"{spread_emoji} *现货-合约价差异动* {spread_emoji}",
            "═" * 30,
            "",
            f"🪙 币种: `{self.symbol}`",
            f"📊 层级: {self.tier_label}",
            "",
            f"💵 现货价格: {self.extra_info.get('现货价格', 'N/A')}",
            f"⚡ 合约价格: {self.extra_info.get('合约价格', 'N/A')}",
            "",
            f"📊 价差: *{self.change_percent:+.2f}%* ({direction})",
            f"⚠️ 阈值: {self.threshold:.2f}%",
            f"⏱ 检测窗口: {self.time_window}秒",
            "",
            f"🕐 时间: {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        # 添加套利提示
        if abs(self.change_percent) > self.threshold * 1.5:
            lines.extend([
                "",
                "⚡ *套利机会提示* ⚡",
                f"• 价差已超过阈值 {abs(self.change_percent/self.threshold):.1f} 倍"
            ])

        # 添加查询提示
        base_symbol = self.symbol.replace("USDT", "")
        lines.extend([
            "",
            "═" * 30,
            f"💬 回复 `/info {base_symbol}` 查看详情"
        ])

        return "\n".join(lines)

    def _format_reversal_message(self) -> str:
        """
        格式化价格反转告警消息
        """
        reversal_type = self.extra_info.get("反转类型", "unknown")
        start_price = self.extra_info.get("起始价", 0)
        extreme_price = self.extra_info.get("极值价", 0)
        rise_percent = self.extra_info.get("上涨幅度", 0)
        fall_percent = self.extra_info.get("下跌幅度", 0)

        if reversal_type == "top":
            # 见顶反转（涨转跌）
            emoji = "📉"
            type_name = "见顶反转 (涨转跌)"
            path_lines = [
                f"• 起始价: ${start_price:,.4f}",
                f"• 冲高至: ${extreme_price:,.4f} (+{rise_percent:.2f}%)",
                f"• 回落至: ${self.current_price:,.4f} (-{fall_percent:.2f}%)"
            ]
        else:
            # 见底反转（跌转涨）
            emoji = "📈"
            type_name = "见底反转 (跌转涨)"
            path_lines = [
                f"• 起始价: ${start_price:,.4f}",
                f"• 探底至: ${extreme_price:,.4f} (-{fall_percent:.2f}%)",
                f"• 反弹至: ${self.current_price:,.4f} (+{rise_percent:.2f}%)"
            ]

        lines = [
            f"🔄 *价格反转告警* {emoji}",
            "",
            f"📌 币种: `{self.symbol}`",
            f"📊 层级: {self.tier_label}",
            f"⚠️ 类型: {type_name}",
            "",
            "💹 *行情路径:*",
        ]
        lines.extend(path_lines)
        lines.extend([
            "",
            f"⚡ 触发阈值: {self.threshold:.2f}%",
            f"⏱ 检测窗口: {self.time_window}秒",
            f"🕐 时间: {self.timestamp.strftime('%H:%M:%S')}",
        ])

        # 添加查询提示
        base_symbol = self.symbol.replace("USDT", "")
        lines.extend([
            "",
            f"💬 回复 `/info {base_symbol}` 查看K线详情"
        ])

        return "\n".join(lines)

    def _format_orderbook_message(self) -> str:
        """
        格式化订单簿异动告警消息
        """
        # 根据告警类型选择表情和标题
        if self.alert_type == AlertType.ORDERBOOK_WALL:
            side = self.extra_info.get("类型", "大单墙")
            emoji = "🧱" if "买墙" in side else "🏔️"
            title = f"*订单簿大单墙告警* {emoji}"
        elif self.alert_type == AlertType.ORDERBOOK_IMBALANCE:
            direction = self.extra_info.get("方向", "")
            emoji = "📊"
            title = f"*订单簿深度失衡* {emoji}"
        elif self.alert_type == AlertType.ORDERBOOK_SWEEP:
            side = self.extra_info.get("类型", "扫盘")
            emoji = "💥"
            title = "*订单簿扫盘告警* 💥"
        else:
            emoji = "📋"
            title = "*订单簿异动*"

        lines = [
            "=" * 28,
            title,
            "=" * 28,
            "",
            f"📌 币种: `{self.symbol}`",
        ]

        # 添加附加信息
        if self.extra_info:
            for key, value in self.extra_info.items():
                lines.append(f"• {key}: {value}")

        lines.extend([
            "",
            f"🕐 时间: {self.timestamp.strftime('%H:%M:%S')}",
        ])

        # 添加查询提示
        base_symbol = self.symbol.replace("USDT", "")
        lines.extend([
            "",
            "=" * 28,
            f"💬 回复 `/info {base_symbol}` 查看详情"
        ])

        return "\n".join(lines)


@dataclass
class ContractInfo:
    """合约基础信息"""
    symbol: str
    base_asset: str                    # 基础资产，如 BTC
    quote_asset: str                   # 报价资产，如 USDT
    price_precision: int               # 价格精度
    quantity_precision: int            # 数量精度


# ==================== 订单簿相关模型 ====================

@dataclass
class OrderBookLevel:
    """订单簿单个价格档位"""
    price: float                       # 价格
    quantity: float                    # 数量
    value: float = 0                   # 价值(USDT) = price * quantity

    def __post_init__(self):
        self.value = self.price * self.quantity


@dataclass
class OrderBookSnapshot:
    """
    订单簿快照
    包含买卖盘各若干档位
    """
    symbol: str
    bids: list                         # 买盘 [(price, qty), ...] 降序
    asks: list                         # 卖盘 [(price, qty), ...] 升序
    last_update_id: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def best_bid(self) -> Optional[float]:
        """最高买价"""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """最低卖价"""
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        """买卖价差"""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_percent(self) -> Optional[float]:
        """买卖价差百分比"""
        if self.best_bid and self.best_ask:
            mid_price = (self.best_bid + self.best_ask) / 2
            return (self.best_ask - self.best_bid) / mid_price * 100
        return None

    def bid_depth(self, levels: int = 10) -> float:
        """买盘深度（USDT价值）"""
        return sum(p * q for p, q in self.bids[:levels])

    def ask_depth(self, levels: int = 10) -> float:
        """卖盘深度（USDT价值）"""
        return sum(p * q for p, q in self.asks[:levels])

    def imbalance_ratio(self, levels: int = 10) -> float:
        """
        深度失衡比率
        正值表示买盘强，负值表示卖盘强
        范围: -1 到 1
        """
        bid_depth = self.bid_depth(levels)
        ask_depth = self.ask_depth(levels)
        total = bid_depth + ask_depth
        if total == 0:
            return 0
        return (bid_depth - ask_depth) / total


@dataclass
class OrderBookWall:
    """
    大单墙信息
    检测订单簿中的大额挂单
    """
    symbol: str
    side: str                          # "bid" 或 "ask"
    price: float                       # 价格
    quantity: float                    # 数量
    value: float                       # 价值(USDT)
    distance_percent: float            # 距离当前价格的百分比
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OrderBookEvent:
    """
    订单簿异动事件
    用于告警系统
    """
    symbol: str
    event_type: str                    # "wall_detected", "wall_removed", "imbalance", "sweep"
    side: Optional[str] = None         # "bid", "ask", None
    price: Optional[float] = None
    quantity: Optional[float] = None
    value: Optional[float] = None
    imbalance_ratio: Optional[float] = None
    extra_info: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
