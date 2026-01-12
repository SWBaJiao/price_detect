"""
Telegram 推送模块
异步发送告警消息到指定频道/群组
"""
import asyncio
import os
from typing import Optional

import aiohttp
from loguru import logger

from .models import AlertEvent


class TelegramBot:
    """
    Telegram Bot 消息推送

    使用 Bot API 直接发送消息，无需 python-telegram-bot 库的长轮询
    """

    API_BASE = "https://api.telegram.org/bot"

    def __init__(self, token: str, chat_id: str, proxy: Optional[str] = None):
        """
        Args:
            token: Bot API Token
            chat_id: 目标频道/群组/用户 ID
            proxy: 代理地址，如果为 None 则从环境变量 PROXY_URL 读取
        """
        self.token = token
        self.chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(token and chat_id)

        # 代理配置
        self._proxy = proxy or os.getenv("PROXY_URL", "")
        if self._proxy:
            logger.info(f"Telegram 使用代理: {self._mask_proxy(self._proxy)}")

        if not self._enabled:
            logger.warning("Telegram 未配置或配置不完整，消息推送已禁用")

    def _mask_proxy(self, proxy: str) -> str:
        """隐藏代理中的敏感信息"""
        if "@" in proxy:
            parts = proxy.split("@")
            return f"***@{parts[-1]}"
        return proxy

    def _get_http_proxy(self) -> Optional[str]:
        """获取 HTTP 代理地址"""
        if self._proxy and self._proxy.startswith(("http://", "https://")):
            return self._proxy
        return None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """关闭会话"""
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def is_enabled(self) -> bool:
        """是否已启用"""
        return self._enabled

    async def send_message(
        self,
        text: str,
        parse_mode: str = "Markdown",
        disable_notification: bool = False
    ) -> bool:
        """
        发送文本消息

        Args:
            text: 消息内容
            parse_mode: 解析模式 (Markdown / HTML)
            disable_notification: 是否静音发送

        Returns:
            是否发送成功
        """
        if not self._enabled:
            logger.debug(f"Telegram 未启用，跳过消息: {text[:50]}...")
            return False

        url = f"{self.API_BASE}{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }

        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.post(url, json=payload, proxy=proxy) as resp:
                if resp.status == 200:
                    logger.debug(f"Telegram 消息发送成功")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Telegram 发送失败: HTTP {resp.status}, {error}")
                    return False
        except aiohttp.ClientError as e:
            logger.error(f"Telegram 网络错误: {e}")
            return False
        except Exception as e:
            logger.error(f"Telegram 未知错误: {e}")
            return False

    async def send_alert(self, event: AlertEvent) -> bool:
        """
        发送告警事件

        Args:
            event: 告警事件对象

        Returns:
            是否发送成功
        """
        message = event.format_message()
        return await self.send_message(message)

    async def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = (
            "🚀 *价格异动监控系统已启动*\n\n"
            "*监控功能：*\n"
            "• 价格异动检测 ✅\n"
            "• 成交量突增检测 ✅\n"
            "• 持仓量变化检测 ✅\n\n"
            "*查询命令：*\n"
            "• 发送 `BTC` `ETH` 等查询合约信息\n"
            "• `/p <币种>` 快速查看价格\n"
            "• `/top` 查看成交额 Top 10\n"
            "• `/help` 查看帮助"
        )
        return await self.send_message(message)

    async def send_shutdown_message(self) -> bool:
        """发送关闭通知"""
        message = "⏹ *价格异动监控系统已停止*"
        return await self.send_message(message)

    async def test_connection(self) -> bool:
        """
        测试 Bot 连接

        Returns:
            连接是否正常
        """
        if not self._enabled:
            return False

        url = f"{self.API_BASE}{self.token}/getMe"

        try:
            session = await self._get_session()
            proxy = self._get_http_proxy()
            async with session.get(url, proxy=proxy) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    bot_name = data.get("result", {}).get("username", "Unknown")
                    logger.info(f"Telegram Bot 连接成功: @{bot_name}")
                    return True
                else:
                    logger.error(f"Telegram Bot 连接失败: HTTP {resp.status}")
                    return False
        except Exception as e:
            logger.error(f"Telegram Bot 连接测试失败: {e}")
            return False


class AlertNotifier:
    """
    告警通知管理器
    可扩展支持多种通知渠道
    """

    def __init__(self, telegram: Optional[TelegramBot] = None):
        self.telegram = telegram
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def start(self):
        """启动通知队列处理"""
        self._running = True
        logger.info("AlertNotifier 正在启动...")
        asyncio.create_task(self._process_queue())
        logger.info("AlertNotifier 队列处理任务已创建")

        if self.telegram and self.telegram.is_enabled:
            logger.info("准备发送启动通知...")
            result = await self.telegram.send_startup_message()
            logger.info(f"启动通知发送结果: {result}")

    async def stop(self):
        """停止通知"""
        self._running = False

        if self.telegram and self.telegram.is_enabled:
            await self.telegram.send_shutdown_message()
            await self.telegram.close()

    async def notify(self, event: AlertEvent):
        """
        发送告警通知

        Args:
            event: 告警事件
        """
        logger.info(f"[通知队列] 收到告警: {event.symbol} {event.alert_type.value}")
        await self._queue.put(event)
        logger.info(f"[通知队列] 告警已入队, 队列大小: {self._queue.qsize()}")

    async def _process_queue(self):
        """处理通知队列"""
        logger.info("[队列处理] 队列处理协程已启动")
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )
                logger.info(f"[队列处理] 从队列取出告警: {event.symbol}")

                if self.telegram and self.telegram.is_enabled:
                    logger.info(f"[队列处理] 正在发送到Telegram: {event.symbol}")
                    result = await self.telegram.send_alert(event)
                    logger.info(f"[队列处理] Telegram发送结果: {result}")
                else:
                    logger.warning("[队列处理] Telegram未启用，跳过发送")

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"处理通知队列出错: {e}")
        logger.info("[队列处理] 队列处理协程已停止")
