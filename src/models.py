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


@dataclass
class ContractInfo:
    """合约基础信息"""
    symbol: str
    base_asset: str                    # 基础资产，如 BTC
    quote_asset: str                   # 报价资产，如 USDT
    price_precision: int               # 价格精度
    quantity_precision: int            # 数量精度
