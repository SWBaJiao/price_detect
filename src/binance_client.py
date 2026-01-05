"""
Binance 客户端
提供 WebSocket 行情订阅和 REST API 调用
支持 HTTP/SOCKS5 代理
"""
import asyncio
import json
import os
import ssl
from datetime import datetime
from typing import AsyncGenerator, Callable, Dict, List, Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from loguru import logger

from .models import TickerData


class BinanceClient:
    """
    Binance USDT-M Futures 客户端
    - WebSocket: 实时行情推送
    - REST: 持仓量查询
    - 支持代理: HTTP/HTTPS/SOCKS5
    """

    # API 端点
    WS_BASE_URL = "wss://fstream.binance.com"
    REST_BASE_URL = "https://fapi.binance.com"

    def __init__(self, proxy: Optional[str] = None):
        """
        Args:
            proxy: 代理地址，支持格式：
                - http://host:port
                - http://user:pass@host:port
                - socks5://host:port
                - socks5://user:pass@host:port
                如果为 None，则从环境变量 PROXY_URL 读取
        """
        self._ws = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._reconnect_delay = 1  # 重连延迟（秒）
        self._max_reconnect_delay = 60

        # 代理配置
        self._proxy = proxy or os.getenv("PROXY_URL", "")
        if self._proxy:
            logger.info(f"使用代理: {self._mask_proxy(self._proxy)}")

        # 创建宽松的 SSL 上下文（解决 SSL 连接问题）
        self._ssl_context = self._create_ssl_context()

    def _mask_proxy(self, proxy: str) -> str:
        """隐藏代理密码"""
        if "@" in proxy:
            # 隐藏密码部分
            parts = proxy.split("@")
            return parts[0].rsplit(":", 1)[0] + ":***@" + parts[1]
        return proxy

    def _create_ssl_context(self) -> ssl.SSLContext:
        """
        创建优化的 SSL 上下文
        解决 macOS 上常见的 SSL 连接问题
        """
        # 创建 SSL 上下文，使用系统默认证书
        ssl_context = ssl.create_default_context()

        # 设置更宽松的 SSL 选项
        ssl_context.check_hostname = True  # 保持主机名检查
        ssl_context.verify_mode = ssl.CERT_REQUIRED  # 要求证书验证

        # 兼容更多 TLS 版本（包括 TLS 1.2 和 1.3）
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2

        # 如果遇到证书问题，可以临时启用以下选项进行诊断：
        # ssl_context.check_hostname = False
        # ssl_context.verify_mode = ssl.CERT_NONE
        # 注意：生产环境不建议禁用证书验证

        return ssl_context

    def _create_connector(self) -> aiohttp.BaseConnector:
        """
        创建优化的连接器
        配置连接池、超时、DNS 缓存等参数
        """
        # 基础 TCPConnector 配置
        connector_kwargs = {
            "limit": 100,  # 连接池大小
            "limit_per_host": 30,  # 每个主机的最大连接数
            "ttl_dns_cache": 300,  # DNS 缓存时间（秒）
            "ssl": self._ssl_context,  # 使用自定义 SSL 上下文
            "force_close": False,  # 保持连接复用
            "enable_cleanup_closed": True,  # 启用关闭连接清理
        }

        try:
            if self._proxy and self._proxy.startswith(("socks5://", "socks4://")):
                # SOCKS 代理：使用 ProxyConnector
                return ProxyConnector.from_url(
                    self._proxy,
                    ssl=self._ssl_context,
                    limit=100,
                    limit_per_host=30
                )
            else:
                # 无代理或 HTTP 代理：使用 TCPConnector
                return aiohttp.TCPConnector(**connector_kwargs)
        except Exception as e:
            logger.error(f"创建连接器失败: {e}，使用默认配置")
            return aiohttp.TCPConnector(**connector_kwargs)

    async def _get_session(self) -> aiohttp.ClientSession:
        """
        获取或创建 HTTP 会话
        配置超时、连接器等参数
        """
        if self._session is None or self._session.closed:
            connector = self._create_connector()

            # 配置超时参数（秒）
            timeout = aiohttp.ClientTimeout(
                total=30,  # 总超时
                connect=10,  # 连接超时
                sock_read=20,  # 读取超时
            )

            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            logger.debug("已创建新的 HTTP 会话")

        return self._session

    def _get_http_proxy(self) -> Optional[str]:
        """获取 HTTP 代理地址（用于 aiohttp 原生代理支持）"""
        if self._proxy and self._proxy.startswith(("http://", "https://")):
            return self._proxy
        return None

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs
    ) -> Optional[aiohttp.ClientResponse]:
        """
        带重试机制的 HTTP 请求
        Args:
            method: HTTP 方法 (GET/POST/etc)
            url: 请求 URL
            max_retries: 最大重试次数
            **kwargs: 传递给 session.request 的其他参数
        """
        session = await self._get_session()
        http_proxy = self._get_http_proxy()
        if http_proxy and 'proxy' not in kwargs:
            kwargs['proxy'] = http_proxy

        last_error = None
        for attempt in range(max_retries):
            try:
                response = await session.request(method, url, **kwargs)
                return response
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 指数退避: 1s, 2s, 4s
                    logger.debug(f"请求失败 ({type(e).__name__}), {wait_time}s 后重试... [{attempt+1}/{max_retries}]")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"请求失败，已达最大重试次数: {e}")

        return None

    async def close(self):
        """关闭所有连接"""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # ==================== WebSocket ====================

    async def subscribe_all_tickers(
        self,
        callback: Callable[[List[TickerData]], None]
    ) -> None:
        """
        订阅全量 miniTicker 行情流
        每秒推送所有合约的最新行情

        Args:
            callback: 接收行情数据的回调函数
        """
        url = f"{self.WS_BASE_URL}/ws/!miniTicker@arr"
        self._running = True

        while self._running:
            try:
                session = await self._get_session()
                # WebSocket 连接（HTTP 代理通过 proxy 参数传递）
                ws_kwargs = {"heartbeat": 30}
                http_proxy = self._get_http_proxy()
                if http_proxy:
                    ws_kwargs["proxy"] = http_proxy

                async with session.ws_connect(url, **ws_kwargs) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1  # 重置重连延迟
                    logger.info("WebSocket 已连接: !miniTicker@arr")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            tickers = self._parse_mini_tickers(data)
                            if tickers:
                                await callback(tickers)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error(f"WebSocket 错误: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.warning("WebSocket 连接已关闭")
                            break

            except aiohttp.ClientError as e:
                logger.error(f"WebSocket 连接失败: {e}")
            except Exception as e:
                logger.error(f"WebSocket 未知错误: {e}")

            # 重连逻辑
            if self._running:
                logger.info(f"将在 {self._reconnect_delay}s 后重连...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._max_reconnect_delay
                )

    def _parse_mini_tickers(self, data: List[dict]) -> List[TickerData]:
        """
        解析 miniTicker 数据

        miniTicker 格式:
        {
            "e": "24hrMiniTicker",
            "s": "BTCUSDT",
            "c": "43000.00",   // 最新价
            "o": "42500.00",   // 开盘价
            "h": "43500.00",   // 最高价
            "l": "42000.00",   // 最低价
            "v": "10000",      // 成交量(基础货币)
            "q": "430000000"   // 成交额(报价货币)
        }
        """
        tickers = []
        now = datetime.now()

        for item in data:
            # 只处理 USDT 合约
            symbol = item.get("s", "")
            if not symbol.endswith("USDT"):
                continue

            try:
                ticker = TickerData(
                    symbol=symbol,
                    price=float(item.get("c", 0)),
                    volume=float(item.get("v", 0)),
                    quote_volume=float(item.get("q", 0)),
                    timestamp=now
                )
                tickers.append(ticker)
            except (ValueError, KeyError) as e:
                logger.debug(f"解析 {symbol} 失败: {e}")

        return tickers

    # ==================== REST API ====================

    async def get_open_interest(self, symbol: str) -> Optional[float]:
        """
        获取单个合约的持仓量

        Args:
            symbol: 交易对，如 BTCUSDT

        Returns:
            持仓量（基础货币），失败返回 None
        """
        url = f"{self.REST_BASE_URL}/fapi/v1/openInterest"
        params = {"symbol": symbol}

        try:
            resp = await self._request_with_retry("GET", url, params=params)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                return float(data.get("openInterest", 0))
            elif resp:
                logger.warning(f"获取 {symbol} OI 失败: HTTP {resp.status}")
                await resp.release()
        except Exception as e:
            logger.error(f"获取 {symbol} OI 异常: {e}")

        return None

    async def get_all_open_interest(
        self,
        symbols: Optional[List[str]] = None
    ) -> Dict[str, float]:
        """
        批量获取持仓量

        Args:
            symbols: 要查询的交易对列表，None 表示查询全部

        Returns:
            {symbol: open_interest} 字典
        """
        if symbols is None:
            # 获取所有交易对
            symbols = await self.get_all_symbols()

        result = {}

        # 并发请求，但限制并发数
        semaphore = asyncio.Semaphore(10)

        async def fetch_one(symbol: str):
            async with semaphore:
                oi = await self.get_open_interest(symbol)
                if oi is not None:
                    result[symbol] = oi

        await asyncio.gather(*[fetch_one(s) for s in symbols])
        return result

    async def get_all_symbols(self) -> List[str]:
        """获取所有 USDT 永续合约交易对"""
        url = f"{self.REST_BASE_URL}/fapi/v1/exchangeInfo"

        try:
            resp = await self._request_with_retry("GET", url)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                symbols = [
                    s["symbol"]
                    for s in data.get("symbols", [])
                    if s.get("contractType") == "PERPETUAL"
                    and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"
                ]
                return symbols
            elif resp:
                await resp.release()
        except Exception as e:
            logger.error(f"获取交易对列表失败: {e}")

        return []

    async def get_ticker_24h(self, symbol: str) -> Optional[dict]:
        """获取 24 小时价格统计"""
        url = f"{self.REST_BASE_URL}/fapi/v1/ticker/24hr"
        params = {"symbol": symbol}

        try:
            resp = await self._request_with_retry("GET", url, params=params)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                return data
            elif resp:
                await resp.release()
        except Exception as e:
            logger.error(f"获取 {symbol} 24h 统计失败: {e}")

        return None

    async def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """
        获取合约完整信息（用于查询命令）

        Returns:
            包含价格、持仓量、24h统计等完整信息的字典
        """
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        try:
            # 并发获取多个数据源
            ticker_task = self.get_ticker_24h(symbol)
            oi_task = self.get_open_interest(symbol)
            funding_task = self._get_funding_rate(symbol)
            mark_price_task = self._get_mark_price(symbol)

            ticker, oi, funding, mark_data = await asyncio.gather(
                ticker_task, oi_task, funding_task, mark_price_task,
                return_exceptions=True
            )

            # 检查是否获取成功
            if isinstance(ticker, Exception) or ticker is None:
                return None

            result = {
                "symbol": symbol,
                "price": float(ticker.get("lastPrice", 0)),
                "price_change": float(ticker.get("priceChange", 0)),
                "price_change_percent": float(ticker.get("priceChangePercent", 0)),
                "high_24h": float(ticker.get("highPrice", 0)),
                "low_24h": float(ticker.get("lowPrice", 0)),
                "volume_24h": float(ticker.get("volume", 0)),
                "quote_volume_24h": float(ticker.get("quoteVolume", 0)),
                "open_interest": oi if not isinstance(oi, Exception) else None,
                "funding_rate": funding if not isinstance(funding, Exception) else None,
            }

            # 添加标记价格和预估清算价格
            if mark_data and not isinstance(mark_data, Exception):
                result["mark_price"] = float(mark_data.get("markPrice", 0))
                result["index_price"] = float(mark_data.get("indexPrice", 0))

            # 计算持仓价值
            if result["open_interest"] and result["price"]:
                result["oi_value"] = result["open_interest"] * result["price"]

            return result

        except Exception as e:
            logger.error(f"获取 {symbol} 完整信息失败: {e}")
            return None

    async def _get_funding_rate(self, symbol: str) -> Optional[float]:
        """获取资金费率"""
        url = f"{self.REST_BASE_URL}/fapi/v1/fundingRate"
        params = {"symbol": symbol, "limit": 1}

        try:
            resp = await self._request_with_retry("GET", url, params=params, max_retries=2)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                if data:
                    return float(data[0].get("fundingRate", 0))
            elif resp:
                await resp.release()
        except Exception as e:
            logger.debug(f"获取 {symbol} 资金费率失败: {e}")

        return None

    async def _get_mark_price(self, symbol: str) -> Optional[dict]:
        """获取标记价格"""
        url = f"{self.REST_BASE_URL}/fapi/v1/premiumIndex"
        params = {"symbol": symbol}

        try:
            resp = await self._request_with_retry("GET", url, params=params, max_retries=2)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                return data
            elif resp:
                await resp.release()
        except Exception as e:
            logger.debug(f"获取 {symbol} 标记价格失败: {e}")

        return None

    async def search_symbols(self, keyword: str) -> List[str]:
        """搜索匹配的交易对"""
        all_symbols = await self.get_all_symbols()
        keyword = keyword.upper()

        # 精确匹配优先
        exact_matches = [s for s in all_symbols if s == f"{keyword}USDT"]
        if exact_matches:
            return exact_matches

        # 模糊匹配
        matches = [s for s in all_symbols if keyword in s]
        return matches[:10]  # 最多返回10个

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 48
    ) -> Optional[List[dict]]:
        """
        获取 K 线数据

        Args:
            symbol: 交易对
            interval: K 线周期 (1m, 5m, 15m, 1h, 4h, 1d)
            limit: 获取数量

        Returns:
            K 线数据列表，每条包含 open, high, low, close, volume, timestamp
        """
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            symbol = f"{symbol}USDT"

        url = f"{self.REST_BASE_URL}/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        try:
            resp = await self._request_with_retry("GET", url, params=params)
            if resp and resp.status == 200:
                data = await resp.json()
                await resp.release()
                klines = []
                for k in data:
                    klines.append({
                        "timestamp": k[0],
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "close_time": k[6],
                        "quote_volume": float(k[7]),
                    })
                return klines
            elif resp:
                await resp.release()
        except Exception as e:
            logger.error(f"获取 {symbol} K线失败: {e}")

        return None
