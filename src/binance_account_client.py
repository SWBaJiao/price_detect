"""
币安合约账户 API 客户端（带签名）
用于账户监控与跟单：查询账户、持仓、下单、平仓
"""
import hashlib
import hmac
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger


@dataclass
class PositionInfo:
    """合约持仓信息（与 Binance positionRisk 对应）"""
    symbol: str
    position_amt: float          # 持仓数量，正数多头负数空头
    entry_price: float
    mark_price: float
    unrealized_profit: float
    leverage: int
    position_side: str = "BOTH"  # BOTH(单向) / LONG / SHORT(双向持仓)
    liquidation_price: Optional[float] = None
    margin_type: str = ""
    timestamp: Optional[int] = None


@dataclass
class AccountBalance:
    """账户余额信息"""
    total_wallet_balance: float
    available_balance: float
    total_unrealized_profit: float
    total_margin_balance: float


def _sign(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


class BinanceAccountClient:
    """
    币安 USDT 合约账户客户端（需 API Key/Secret）
    提供：账户信息、持仓、下单、平仓
    """
    REST_BASE_URL = "https://fapi.binance.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        proxy: Optional[str] = None
    ):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self._proxy = proxy or os.getenv("PROXY_URL", "")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _build_signed_params(self, params: Optional[Dict] = None) -> Dict[str, str]:
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        return p

    def _build_query_and_sign(self, params: Dict) -> tuple:
        from urllib.parse import urlencode
        query = urlencode(params)
        sig = _sign(self.api_secret, query)
        return query, sig

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """发送带签名的请求。GET 参数在 query，POST 参数在 body（form）并参与签名。"""
        from urllib.parse import urlencode
        session = await self._get_session()
        base_params = self._build_signed_params(params if method == "GET" else (params or {}))
        if data:
            base_params.update(data)
        query_str, signature = self._build_query_and_sign(base_params)
        url = f"{self.REST_BASE_URL}{path}"
        headers = {"X-MBX-APIKEY": self.api_key}
        kwargs = {"headers": headers}
        if self._proxy and self._proxy.startswith(("http://", "https://")):
            kwargs["proxy"] = self._proxy

        try:
            if method == "GET":
                full_url = f"{url}?{query_str}&signature={signature}"
                async with session.get(full_url, **kwargs) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"BinanceAccountClient {method} {path} status={resp.status} body={text[:200]}")
                        return None
                    return await resp.json()
            else:
                # POST / DELETE: 参数放 body，application/x-www-form-urlencoded
                base_params["signature"] = signature
                async with session.request(
                    method, url, data=base_params, **kwargs
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning(f"BinanceAccountClient {method} {path} status={resp.status} body={text[:200]}")
                        return None
                    return await resp.json()
        except Exception as e:
            logger.error(f"BinanceAccountClient request error: {e}")
            return None

    async def get_account(self) -> Optional[AccountBalance]:
        """获取账户信息"""
        raw = await self._signed_request("GET", "/fapi/v2/account")
        if not raw:
            return None
        try:
            return AccountBalance(
                total_wallet_balance=float(raw.get("totalWalletBalance", 0) or 0),
                available_balance=float(raw.get("availableBalance", 0) or 0),
                total_unrealized_profit=float(raw.get("totalUnrealizedProfit", 0) or 0),
                total_margin_balance=float(raw.get("totalMarginBalance", 0) or 0),
            )
        except (TypeError, ValueError) as e:
            logger.error(f"parse account error: {e}")
            return None

    async def get_account_full(self) -> Optional[dict]:
        """
        获取账户完整信息（含各持仓占用保证金）
        用于按保证金比例跟单：available_balance + positions[].initial_margin
        """
        raw = await self._signed_request("GET", "/fapi/v2/account")
        if not raw:
            return None
        try:
            available = float(raw.get("availableBalance", 0) or 0)
            positions = []
            for p in raw.get("positions", []) or []:
                amt = float(p.get("positionAmt", 0) or 0)
                if amt == 0:
                    continue
                initial_margin = float(p.get("initialMargin", 0) or 0)
                positions.append({
                    "symbol": str(p.get("symbol", "")),
                    "position_amt": amt,
                    "entry_price": float(p.get("entryPrice", 0) or 0),
                    "mark_price": float(p.get("markPrice", 0) or 0),
                    "leverage": int(p.get("leverage", 0) or 0),
                    "initial_margin": initial_margin,
                    "position_side": str(p.get("positionSide", "BOTH")),
                })
            return {"available_balance": available, "positions": positions}
        except (TypeError, ValueError, KeyError) as e:
            logger.error(f"parse account full error: {e}")
            return None

    async def get_position_risk(self) -> Optional[List[PositionInfo]]:
        """获取当前持仓（只返回有仓位的），API 失败返回 None。
        双向持仓模式下同一 symbol 可能返回 LONG 和 SHORT 两条记录。"""
        raw = await self._signed_request("GET", "/fapi/v2/positionRisk")
        if raw is None:
            return None
        if not raw:
            return []
        result = []
        for item in raw:
            amt = float(item.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            try:
                result.append(PositionInfo(
                    symbol=str(item.get("symbol", "")),
                    position_amt=amt,
                    entry_price=float(item.get("entryPrice", 0) or 0),
                    mark_price=float(item.get("markPrice", 0) or 0),
                    unrealized_profit=float(item.get("unRealizedProfit", 0) or 0),
                    leverage=int(item.get("leverage", 0) or 0),
                    position_side=str(item.get("positionSide", "BOTH")),
                    liquidation_price=float(item["liquidationPrice"]) if item.get("liquidationPrice") else None,
                    margin_type=str(item.get("marginType", "")),
                    timestamp=int(item["updateTime"]) if item.get("updateTime") else None,
                ))
            except (TypeError, ValueError, KeyError) as e:
                logger.debug(f"skip position row: {e}")
        return result

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reduce_only: bool = False,
        position_side: str = "BOTH",
    ) -> Optional[Dict]:
        """
        市价开仓/平仓
        side: BUY / SELL
        quantity: 数量（张数或币数，依合约）
        reduce_only: True 表示只减仓（平仓）— 仅单向持仓模式使用
        position_side: BOTH(单向) / LONG / SHORT(双向持仓)
            双向持仓时必须指定 LONG 或 SHORT，且不可使用 reduceOnly
        """
        from urllib.parse import urlencode
        params = self._build_signed_params({})
        params["symbol"] = symbol
        params["side"] = side
        params["type"] = "MARKET"
        params["quantity"] = str(abs(quantity))
        if position_side in ("LONG", "SHORT"):
            # 双向持仓模式：必须传 positionSide，不能传 reduceOnly
            params["positionSide"] = position_side
        else:
            # 单向持仓模式
            if reduce_only:
                params["reduceOnly"] = "true"
        query_str, sig = self._build_query_and_sign(params)
        params["signature"] = sig
        session = await self._get_session()
        url = f"{self.REST_BASE_URL}/fapi/v1/order"
        headers = {"X-MBX-APIKEY": self.api_key}
        kwargs = {"headers": headers}
        if self._proxy and self._proxy.startswith(("http://", "https://")):
            kwargs["proxy"] = self._proxy
        try:
            async with session.post(url, data=params, **kwargs) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"place_market_order {symbol} {side} positionSide={position_side} status={resp.status} body={text[:300]}")
                    return None
                return await resp.json()
        except Exception as e:
            logger.error(f"place_market_order error: {e}")
            return None

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """设置合约杠杆"""
        raw = await self._signed_request("POST", "/fapi/v1/leverage", data={
            "symbol": symbol,
            "leverage": leverage,
        })
        return raw is not None

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
