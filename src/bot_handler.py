"""
Telegram Bot 命令处理器
支持 @ 机器人查询合约数据，带 K 线图
"""
import asyncio
import io
import os
import re
from datetime import datetime
from typing import Optional, Tuple

import aiohttp
from loguru import logger

from .binance_client import BinanceClient
from .chart_generator import ChartGenerator


class BotCommandHandler:
    """
    Telegram Bot 命令处理器

    支持的命令：
    - /price <symbol> 或 /p <symbol> - 查询价格
    - /info <symbol> 或 /i <symbol> - 查询完整信息
    - /top - 查询成交额 Top 10
    - /help - 显示帮助

    也支持直接发送币种名称查询，如：BTC、ETH
    """

    API_BASE = "https://api.telegram.org/bot"

    def __init__(self, token: str, binance: BinanceClient):
        self.token = token
        self.binance = binance
        self.chart = ChartGenerator()
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._last_update_id = 0
        self._bot_username: Optional[str] = None

        # 代理配置
        self._proxy = os.getenv("PROXY_URL", "")

    def _get_http_proxy(self) -> Optional[str]:
        """获取 HTTP 代理地址"""
        if self._proxy and self._proxy.startswith(("http://", "https://")):
            return self._proxy
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    async def start_polling(self):
        """开始轮询消息"""
        self._running = True

        # 获取 Bot 用户名
        await self._get_bot_info()

        logger.info(f"Bot 命令处理器已启动: @{self._bot_username}")

        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._process_update(update)
            except Exception as e:
                logger.error(f"轮询消息出错: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(0.5)  # 轮询间隔

    async def _get_bot_info(self):
        """获取 Bot 信息"""
        url = f"{self.API_BASE}{self.token}/getMe"
        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.get(url, proxy=proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._bot_username = data.get("result", {}).get("username")
        except Exception as e:
            logger.error(f"获取 Bot 信息失败: {e}")

    async def _get_updates(self):
        """获取新消息"""
        url = f"{self.API_BASE}{self.token}/getUpdates"
        params = {
            "offset": self._last_update_id + 1,
            "timeout": 30,
            "allowed_updates": ["message"]
        }

        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.get(url, params=params, proxy=proxy, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    updates = data.get("result", [])
                    if updates:
                        self._last_update_id = updates[-1]["update_id"]
                    return updates
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            logger.debug(f"获取更新失败: {e}")

        return []

    async def _process_update(self, update: dict):
        """处理单条更新"""
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = message.get("chat", {}).get("id")

        if not text or not chat_id:
            return

        # 检查是否 @ 了机器人（群组中）
        if self._bot_username:
            # 移除 @ 提及
            text = re.sub(rf"@{self._bot_username}\s*", "", text, flags=re.IGNORECASE)

        # 解析命令
        result = await self._handle_command(text)
        if result:
            if isinstance(result, tuple):
                # 带图片的响应 (text, image_bytes)
                text_msg, image_data = result
                await self._send_photo_with_caption(chat_id, image_data, text_msg)
            else:
                # 纯文本响应
                await self._send_reply(chat_id, result)

    def _parse_interval(self, time_value: str) -> str:
        """
        解析时间参数，转换为 Binance interval 格式

        支持的输入：
        - 1, 3, 5, 15, 30 -> 1m, 3m, 5m, 15m, 30m (分钟)
        - 60 -> 1h, 120 -> 2h, 240 -> 4h (小时)
        - 1440 -> 1d (日)

        默认返回 1h
        """
        try:
            minutes = int(time_value)

            # 分钟级别 (1-59)
            if minutes < 60:
                valid_minutes = [1, 3, 5, 15, 30]
                if minutes in valid_minutes:
                    return f"{minutes}m"
                # 不在有效列表中，返回最接近的
                for vm in valid_minutes:
                    if minutes <= vm:
                        return f"{vm}m"
                return "30m"

            # 小时级别 (60, 120, 240, 360, 720)
            elif minutes < 1440:
                hours = minutes // 60
                valid_hours = [1, 2, 4, 6, 12]
                if hours in valid_hours:
                    return f"{hours}h"
                # 返回最接近的
                for vh in valid_hours:
                    if hours <= vh:
                        return f"{vh}h"
                return "12h"

            # 日级别
            elif minutes >= 1440:
                days = minutes // 1440
                if days >= 7:
                    return "1w"
                elif days >= 3:
                    return "3d"
                return "1d"

        except (ValueError, TypeError):
            pass

        return "1h"  # 默认返回 1h

    async def _handle_command(self, text: str) -> Optional[str]:
        """处理命令并返回响应"""
        text = text.strip()

        # /help 或 /start
        if text.lower() in ["/help", "/start", "帮助", "help"]:
            return self._get_help_message()

        # /top 查询 Top 10
        if text.lower() in ["/top", "/top10", "top", "top10"]:
            return await self._handle_top_command()

        # /price 或 /p 命令
        match = re.match(r"^[/]?(price|p)\s+(\w+)$", text, re.IGNORECASE)
        if match:
            symbol = match.group(2)
            return await self._handle_price_command(symbol)

        # /info 或 /i 命令（支持可选的时间参数）
        match = re.match(r"^[/]?(info|i)\s+(\w+)(?:\s+(\d+))?$", text, re.IGNORECASE)
        if match:
            symbol = match.group(2)
            time_param = match.group(3)  # 可能为 None
            interval = self._parse_interval(time_param) if time_param else "1h"
            return await self._handle_info_command(symbol, interval)

        # 直接输入币种名称（如 BTC、ETH、btc），支持可选时间参数
        match = re.match(r"^([A-Za-z]{2,10})(?:\s+(\d+))?$", text)
        if match:
            symbol = match.group(1)
            time_param = match.group(2)
            interval = self._parse_interval(time_param) if time_param else "1h"
            return await self._handle_info_command(symbol, interval)

        # 带 USDT 后缀，支持可选时间参数
        match = re.match(r"^([A-Za-z]{2,10})USDT(?:\s+(\d+))?$", text, re.IGNORECASE)
        if match:
            symbol = match.group(1)
            time_param = match.group(2)
            interval = self._parse_interval(time_param) if time_param else "1h"
            return await self._handle_info_command(symbol, interval)

        return None  # 不识别的消息不回复

    async def _handle_price_command(self, symbol: str) -> str:
        """处理价格查询命令"""
        info = await self.binance.get_symbol_info(symbol)

        if not info:
            # 尝试搜索
            matches = await self.binance.search_symbols(symbol)
            if matches:
                return f"❌ 未找到 `{symbol.upper()}USDT`\n\n你是否要查询：\n" + "\n".join(
                    f"• `{m}`" for m in matches[:5]
                )
            return f"❌ 未找到交易对: `{symbol.upper()}USDT`"

        # 简洁的价格信息
        change_emoji = "📈" if info["price_change_percent"] >= 0 else "📉"
        return (
            f"💰 *{info['symbol']}*\n\n"
            f"价格: `${info['price']:,.4f}`\n"
            f"{change_emoji} 24h: `{info['price_change_percent']:+.2f}%`"
        )

    async def _handle_info_command(self, symbol: str, interval: str = "1h"):
        """处理完整信息查询命令，返回带 K 线图"""
        # 标准化 symbol
        symbol_upper = symbol.upper()
        if not symbol_upper.endswith("USDT"):
            symbol_upper = f"{symbol_upper}USDT"

        # 根据时间级别确定数据量
        if interval.endswith("m"):
            limit = 100  # 分钟级别，显示更多数据点
        elif interval.endswith("h"):
            limit = 48   # 小时级别，2天数据
        elif interval.endswith("d"):
            limit = 30   # 日级别，1个月数据
        elif interval.endswith("w"):
            limit = 24   # 周级别，半年数据
        else:
            limit = 48

        # 并发获取信息和 K 线数据
        info_task = self.binance.get_symbol_info(symbol)
        klines_task = self.binance.get_klines(symbol, interval=interval, limit=limit)

        info, klines = await asyncio.gather(info_task, klines_task)

        if not info:
            matches = await self.binance.search_symbols(symbol)
            if matches:
                return f"❌ 未找到 `{symbol.upper()}USDT`\n\n你是否要查询：\n" + "\n".join(
                    f"• `{m}`" for m in matches[:5]
                )
            return f"❌ 未找到交易对: `{symbol.upper()}USDT`"

        # 格式化输出
        change_emoji = "📈" if info["price_change_percent"] >= 0 else "📉"

        lines = [
            f"📊 *{info['symbol']} 合约信息*",
            "",
            f"💵 *价格*",
            f"   最新价: `${info['price']:,.4f}`",
            f"   标记价: `${info.get('mark_price', 0):,.4f}`",
            "",
            f"{change_emoji} *24h 统计*",
            f"   涨跌幅: `{info['price_change_percent']:+.2f}%`",
            f"   最高价: `${info['high_24h']:,.4f}`",
            f"   最低价: `${info['low_24h']:,.4f}`",
            f"   成交额: `{self._format_volume(info['quote_volume_24h'])}`",
        ]

        # 持仓量
        if info.get("open_interest"):
            oi = info["open_interest"]
            oi_value = info.get("oi_value", 0)
            lines.extend([
                "",
                f"💰 *持仓量*: `{self._format_volume(oi_value)}`",
            ])

        # 资金费率
        if info.get("funding_rate") is not None:
            funding = info["funding_rate"] * 100
            funding_emoji = "🟢" if funding >= 0 else "🔴"
            next_funding_time = info.get("next_funding_time")
            if next_funding_time:
                from datetime import datetime, timezone
                now_ts = datetime.now(tz=timezone.utc).timestamp() * 1000
                remaining_ms = next_funding_time - now_ts
                if remaining_ms > 0:
                    remaining_hours = int(remaining_ms / (1000 * 60 * 60))
                    remaining_mins = int((remaining_ms % (1000 * 60 * 60)) / (1000 * 60))
                    lines.append(f"{funding_emoji} *资金费率*: `{funding:+.4f}%` | 距结算 {remaining_hours}h{remaining_mins}m")
                else:
                    lines.append(f"{funding_emoji} *资金费率*: `{funding:+.4f}%`")
            else:
                lines.append(f"{funding_emoji} *资金费率*: `{funding:+.4f}%`")

        lines.extend([
            "",
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        ])

        text_msg = "\n".join(lines)

        # 生成 K 线图
        if klines and len(klines) >= 10:
            try:
                chart_data = self.chart.generate_kline_chart(
                    klines=klines,
                    symbol=info['symbol'],
                    interval=interval,
                    show_volume=True,
                    show_ma=True
                )
                if chart_data:
                    return (text_msg, chart_data)
            except Exception as e:
                logger.error(f"生成 K 线图失败: {e}")

        # 无法生成图表时返回纯文本
        return text_msg

    async def _handle_top_command(self) -> str:
        """处理 Top 10 查询"""
        try:
            url = f"{self.binance.REST_BASE_URL}/fapi/v1/ticker/24hr"
            session = await self.binance._get_session()

            async with session.get(url) as resp:
                if resp.status != 200:
                    return "❌ 获取数据失败"

                data = await resp.json()

            # 过滤 USDT 永续合约并按成交额排序
            usdt_tickers = [
                t for t in data
                if t.get("symbol", "").endswith("USDT")
            ]
            sorted_tickers = sorted(
                usdt_tickers,
                key=lambda x: float(x.get("quoteVolume", 0)),
                reverse=True
            )[:10]

            lines = ["📊 *成交额 Top 10*", ""]

            for i, t in enumerate(sorted_tickers, 1):
                symbol = t["symbol"]
                price = float(t.get("lastPrice", 0))
                change = float(t.get("priceChangePercent", 0))
                volume = float(t.get("quoteVolume", 0))

                emoji = "📈" if change >= 0 else "📉"
                vol_str = self._format_volume(volume)

                lines.append(
                    f"{i}. `{symbol}` {emoji}`{change:+.2f}%` | {vol_str}"
                )

            lines.extend([
                "",
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            ])

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"获取 Top 10 失败: {e}")
            return "❌ 获取数据失败"

    def _format_volume(self, volume: float) -> str:
        """格式化成交额"""
        if volume >= 1_000_000_000:
            return f"${volume/1_000_000_000:.1f}B"
        elif volume >= 1_000_000:
            return f"${volume/1_000_000:.1f}M"
        elif volume >= 1_000:
            return f"${volume/1_000:.1f}K"
        return f"${volume:.0f}"

    def _get_help_message(self) -> str:
        """返回帮助信息"""
        return (
            "🤖 *合约数据查询机器人*\n\n"
            "*查询命令：*\n"
            "• 直接发送币种名称，如 `BTC` `ETH` `SOL`\n"
            "• `/info <币种> [时间]` - k线图等详细信息\n"
            "• `/p <币种>` - 快速查看价格\n"
            "• `/i <币种> [时间]` - 查看完整信息\n"
            "• `/top` - 成交额 Top 10\n\n"
            "*时间参数（可选，单位：分钟）：*\n"
            "• `5` - 5分钟 K线\n"
            "• `15` - 15分钟 K线\n"
            "• `60` - 1小时 K线\n"
            "• `240` - 4小时 K线\n"
            "• 不传默认为 1小时\n\n"
            "*示例：*\n"
            "• `BTC` - 查询 BTCUSDT (1h K线)\n"
            "• `BTC 5` - 查询 BTCUSDT (5分钟 K线)\n"
            "• `/info RIVER 15` - 查询 RIVERUSDT (15分钟 K线)\n"
            "• `/p eth` - 查询 ETH 价格\n"
            "• `/top` - 查看热门合约\n\n"
            "*数据说明：*\n"
            "• 价格数据来自 Binance Futures\n"
            "• 持仓量为合约未平仓数量\n"
            "• 资金费率每 8 小时结算一次"
        )

    async def _send_reply(self, chat_id: int, text: str):
        """发送回复消息"""
        url = f"{self.API_BASE}{self.token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.post(url, json=payload, proxy=proxy) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"发送回复失败: {error}")
        except Exception as e:
            logger.error(f"发送回复异常: {e}")

    async def _send_photo_with_caption(self, chat_id: int, photo_bytes: bytes, caption: str):
        """发送带图片的消息"""
        url = f"{self.API_BASE}{self.token}/sendPhoto"

        # 使用 multipart/form-data 上传图片
        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('caption', caption)
        data.add_field('parse_mode', 'Markdown')
        data.add_field(
            'photo',
            photo_bytes,
            filename='chart.png',
            content_type='image/png'
        )

        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.post(url, data=data, proxy=proxy) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"发送图片失败: {error}")
                    # 图片发送失败时尝试发送纯文本
                    await self._send_reply(chat_id, caption)
        except Exception as e:
            logger.error(f"发送图片异常: {e}")
            await self._send_reply(chat_id, caption)
