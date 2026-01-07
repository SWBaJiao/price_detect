"""
K 线图生成模块
使用 matplotlib 生成专业的蜡烛图
"""
import io
from datetime import datetime
from typing import List, Optional

import matplotlib
matplotlib.use('Agg')  # 无头模式，不需要显示器

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter, MaxNLocator
import numpy as np


class ChartGenerator:
    """K 线图生成器"""

    # 颜色配置（暗色主题）
    COLORS = {
        "background": "#1a1a2e",
        "grid": "#2d2d44",
        "text": "#e0e0e0",
        "up": "#00c853",       # 上涨 - 绿色
        "down": "#ff1744",     # 下跌 - 红色
        "volume_up": "#00c85366",
        "volume_down": "#ff174466",
        "ma5": "#ffd700",      # MA5 - 金色
        "ma20": "#00bfff",     # MA20 - 蓝色
    }

    def __init__(self, width: int = 10, height: int = 6):
        self.width = width
        self.height = height

    def _format_price(self, price: float) -> str:
        """
        根据价格大小动态格式化显示精度

        - 价格 >= 1000: 2 位小数 ($1,234.56)
        - 价格 >= 100: 3 位小数 ($123.456)
        - 价格 >= 1: 4 位小数 ($12.3456)
        - 价格 < 1: 6 位小数 ($0.123456)
        """
        if price >= 1000:
            return f"${price:,.2f}"
        elif price >= 100:
            return f"${price:,.3f}"
        elif price >= 1:
            return f"${price:,.4f}"
        elif price >= 0.01:
            return f"${price:,.6f}"
        else:
            return f"${price:,.8f}"

    def _price_formatter(self, x, pos):
        """Y 轴价格格式化器"""
        if x >= 1000:
            return f"${x:,.2f}"
        elif x >= 100:
            return f"{x:.3f}"
        elif x >= 1:
            return f"{x:.4f}"
        elif x >= 0.01:
            return f"{x:.6f}"
        else:
            return f"{x:.8f}"

    def generate_kline_chart(
        self,
        klines: List[dict],
        symbol: str,
        interval: str = "1h",
        show_volume: bool = True,
        show_ma: bool = True
    ) -> Optional[bytes]:
        """
        生成 K 线图

        Args:
            klines: K 线数据列表
            symbol: 交易对名称
            interval: K 线周期
            show_volume: 是否显示成交量
            show_ma: 是否显示均线

        Returns:
            PNG 图片字节数据
        """
        if not klines or len(klines) < 2:
            return None

        try:
            # 准备数据
            timestamps = [datetime.fromtimestamp(k["timestamp"] / 1000) for k in klines]
            opens = [k["open"] for k in klines]
            highs = [k["high"] for k in klines]
            lows = [k["low"] for k in klines]
            closes = [k["close"] for k in klines]
            volumes = [k["volume"] for k in klines]

            # 创建图表
            if show_volume:
                fig, (ax1, ax2) = plt.subplots(
                    2, 1,
                    figsize=(self.width, self.height),
                    gridspec_kw={'height_ratios': [3, 1]},
                    facecolor=self.COLORS["background"]
                )
            else:
                fig, ax1 = plt.subplots(
                    figsize=(self.width, self.height),
                    facecolor=self.COLORS["background"]
                )
                ax2 = None

            # 设置主图样式
            ax1.set_facecolor(self.COLORS["background"])
            ax1.tick_params(colors=self.COLORS["text"])
            ax1.spines['bottom'].set_color(self.COLORS["grid"])
            ax1.spines['top'].set_color(self.COLORS["grid"])
            ax1.spines['left'].set_color(self.COLORS["grid"])
            ax1.spines['right'].set_color(self.COLORS["grid"])
            ax1.grid(True, color=self.COLORS["grid"], alpha=0.3, linestyle='--')

            # 绘制 K 线
            bar_width = 0.6
            for i in range(len(klines)):
                color = self.COLORS["up"] if closes[i] >= opens[i] else self.COLORS["down"]

                # 影线
                ax1.plot(
                    [i, i],
                    [lows[i], highs[i]],
                    color=color,
                    linewidth=1
                )

                # 实体
                body_bottom = min(opens[i], closes[i])
                body_height = abs(closes[i] - opens[i])
                rect = Rectangle(
                    (i - bar_width / 2, body_bottom),
                    bar_width,
                    body_height if body_height > 0 else 0.0001,
                    facecolor=color,
                    edgecolor=color,
                    linewidth=0.5
                )
                ax1.add_patch(rect)

            # 绘制均线
            if show_ma and len(closes) >= 20:
                ma5 = self._calculate_ma(closes, 5)
                ma20 = self._calculate_ma(closes, 20)

                x_range = range(len(closes))
                ax1.plot(x_range, ma5, color=self.COLORS["ma5"], linewidth=1, label='MA5', alpha=0.8)
                ax1.plot(x_range, ma20, color=self.COLORS["ma20"], linewidth=1, label='MA20', alpha=0.8)

                # 图例
                legend = ax1.legend(
                    loc='upper left',
                    facecolor=self.COLORS["background"],
                    edgecolor=self.COLORS["grid"],
                    fontsize=8
                )
                for text in legend.get_texts():
                    text.set_color(self.COLORS["text"])

            # 设置 X 轴
            ax1.set_xlim(-1, len(klines))

            # 价格标签
            current_price = closes[-1]
            price_change = ((closes[-1] - opens[0]) / opens[0]) * 100
            change_color = self.COLORS["up"] if price_change >= 0 else self.COLORS["down"]

            # 标题（使用精确的价格格式）
            price_str = self._format_price(current_price)
            title = f"{symbol} | {interval} | {price_str} ({price_change:+.2f}%)"
            ax1.set_title(
                title,
                color=change_color,
                fontsize=14,
                fontweight='bold',
                pad=10
            )

            # Y 轴标签
            ax1.set_ylabel('Price (USDT)', color=self.COLORS["text"], fontsize=10)

            # 设置 Y 轴价格格式化器（更精确的小数位）
            ax1.yaxis.set_major_formatter(FuncFormatter(self._price_formatter))

            # 增加 Y 轴刻度密度（显示更多价格刻度）
            ax1.yaxis.set_major_locator(MaxNLocator(nbins=12, prune='both'))

            # 绘制成交量
            if show_volume and ax2:
                ax2.set_facecolor(self.COLORS["background"])
                ax2.tick_params(colors=self.COLORS["text"])
                ax2.spines['bottom'].set_color(self.COLORS["grid"])
                ax2.spines['top'].set_color(self.COLORS["grid"])
                ax2.spines['left'].set_color(self.COLORS["grid"])
                ax2.spines['right'].set_color(self.COLORS["grid"])
                ax2.grid(True, color=self.COLORS["grid"], alpha=0.3, linestyle='--')

                colors = [
                    self.COLORS["volume_up"] if closes[i] >= opens[i] else self.COLORS["volume_down"]
                    for i in range(len(volumes))
                ]
                ax2.bar(range(len(volumes)), volumes, color=colors, width=bar_width)
                ax2.set_xlim(-1, len(klines))
                ax2.set_ylabel('Volume', color=self.COLORS["text"], fontsize=10)

                # 隐藏 X 轴刻度标签（时间显示在底部）
                ax2.set_xticks([])

            # 隐藏主图 X 轴刻度
            ax1.set_xticks([])

            # 时间范围标注
            time_start = timestamps[0].strftime('%m-%d %H:%M')
            time_end = timestamps[-1].strftime('%m-%d %H:%M')
            fig.text(
                0.5, 0.02,
                f"{time_start}  →  {time_end}",
                ha='center',
                color=self.COLORS["text"],
                fontsize=9
            )

            plt.tight_layout()
            plt.subplots_adjust(bottom=0.08)

            # 保存到内存
            buf = io.BytesIO()
            plt.savefig(
                buf,
                format='png',
                dpi=100,
                facecolor=self.COLORS["background"],
                edgecolor='none',
                bbox_inches='tight'
            )
            buf.seek(0)
            plt.close(fig)

            return buf.getvalue()

        except Exception as e:
            plt.close('all')
            raise e

    def _calculate_ma(self, data: List[float], period: int) -> List[float]:
        """计算移动平均线"""
        ma = []
        for i in range(len(data)):
            if i < period - 1:
                ma.append(np.nan)
            else:
                ma.append(np.mean(data[i - period + 1:i + 1]))
        return ma
