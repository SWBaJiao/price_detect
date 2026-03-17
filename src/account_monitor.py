"""
账户监控与跟单后台服务
- 轮询监控账户持仓，有新开仓则 Telegram 提醒并更新快照
- 跟单：将源账户的持仓变化同步到跟单账户（通过币安 API 下单/平仓）
"""
import asyncio
from datetime import datetime
from typing import Callable, List, Optional

from loguru import logger

from .account_monitor_store import AccountMonitorStore, CopyTradingConfig, MonitoredAccount
from .binance_account_client import BinanceAccountClient, PositionInfo


def _pos_key(p) -> tuple:
    """生成持仓唯一标识 (symbol, position_side)"""
    if isinstance(p, dict):
        return (p.get("symbol", ""), p.get("position_side", "BOTH"))
    return (p.symbol, getattr(p, "position_side", "BOTH"))


def _position_to_dict(p: PositionInfo) -> dict:
    return {
        "symbol": p.symbol,
        "position_side": p.position_side,
        "position_amt": p.position_amt,
        "entry_price": p.entry_price,
        "mark_price": p.mark_price,
        "unrealized_profit": p.unrealized_profit,
        "leverage": p.leverage,
    }


def _format_position_message(account_name: str, p: PositionInfo, action: str = "开仓") -> str:
    side = "多" if p.position_amt > 0 else "空"
    notional = abs(p.position_amt) * p.entry_price
    margin = notional / p.leverage if p.leverage else notional
    return (
        f"🔔 *账户监控 - {action}提醒*\n\n"
        f"📌 账户: `{account_name}`\n"
        f"📊 交易对: `{p.symbol}`\n"
        f"📈 方向: {side} | 数量: {abs(p.position_amt)}\n"
        f"💵 开仓价: ${p.entry_price:.4f} | 标记价: ${p.mark_price:.4f}\n"
        f"🔧 杠杆: {p.leverage}x | 保证金: ${margin:.2f}\n"
        f"📉 未实现盈亏: ${p.unrealized_profit:.2f}\n"
        f"⏱ 时间: {datetime.now().strftime('%H:%M:%S')}"
    )


def _format_change_message(
    account_name: str,
    p: PositionInfo,
    old_amt: float,
    action: str,
) -> str:
    """格式化加仓/减仓消息，包含数量变化"""
    side = "多" if p.position_amt > 0 else "空"
    notional = abs(p.position_amt) * p.entry_price
    margin = notional / p.leverage if p.leverage else notional
    return (
        f"🔔 *账户监控 - {action}提醒*\n\n"
        f"📌 账户: `{account_name}`\n"
        f"📊 交易对: `{p.symbol}`\n"
        f"📈 方向: {side} | 数量: {abs(old_amt)} → {abs(p.position_amt)}\n"
        f"💵 开仓价: ${p.entry_price:.4f} | 标记价: ${p.mark_price:.4f}\n"
        f"🔧 杠杆: {p.leverage}x | 保证金: ${margin:.2f}\n"
        f"📉 未实现盈亏: ${p.unrealized_profit:.2f}\n"
        f"⏱ 时间: {datetime.now().strftime('%H:%M:%S')}"
    )


class AccountMonitorService:
    """
    账户监控服务
    - 定期拉取监控账户持仓，与上次快照对比
    - 新开仓 -> 发送 Telegram + 更新快照
    - 平仓 -> 可选通知 + 更新快照
    """

    def __init__(
        self,
        store: AccountMonitorStore,
        on_position_alert: Optional[Callable[[str, str], None]] = None,
        poll_interval_seconds: int = 30,
    ):
        self.store = store
        self.on_position_alert = on_position_alert  # (account_name, message_text)
        self.poll_interval = poll_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _fetch_positions(self, account: MonitoredAccount) -> Optional[List[PositionInfo]]:
        """拉取持仓，失败返回 None（区别于空仓的 []）"""
        client = BinanceAccountClient(account.api_key, account.api_secret)
        try:
            positions = await client.get_position_risk()
            if positions is None:
                logger.error(f"拉取监控账户 {account.name} 持仓返回 None")
                return None
            return positions
        except Exception as e:
            logger.error(f"拉取监控账户 {account.name} 持仓失败: {e}")
            return None
        finally:
            await client.close()

    def _diff_positions(
        self,
        old: List[dict],
        new: List[PositionInfo],
    ) -> tuple:
        """返回 (opened, closed, increased, decreased)

        - opened:    新开仓 PositionInfo 列表
        - closed:    已平仓 (symbol, position_side, old_entry) 列表
        - increased: 加仓 (PositionInfo, old_amt) 列表
        - decreased: 减仓 (PositionInfo, old_amt) 列表
        方向翻转视为平仓+开仓。key 为 (symbol, position_side)。
        """
        old_by_key = {_pos_key(x): x for x in old}
        new_by_key = {_pos_key(p): p for p in new}
        opened = []
        closed = []    # [(symbol, position_side, old_entry)]
        increased = []  # [(PositionInfo, old_amt)]
        decreased = []  # [(PositionInfo, old_amt)]

        for key, p in new_by_key.items():
            old_entry = old_by_key.get(key)
            old_amt = old_entry.get("position_amt", 0) if old_entry else 0

            if old_amt == 0 and p.position_amt != 0:
                opened.append(p)
            elif old_amt != 0 and p.position_amt != 0:
                if (old_amt > 0) != (p.position_amt > 0):
                    closed.append((p.symbol, p.position_side, old_entry))
                    opened.append(p)
                elif abs(p.position_amt) > abs(old_amt):
                    increased.append((p, old_amt))
                elif abs(p.position_amt) < abs(old_amt):
                    decreased.append((p, old_amt))

        for key, o in old_by_key.items():
            if o.get("position_amt", 0) != 0 and (key not in new_by_key or new_by_key[key].position_amt == 0):
                closed.append((o["symbol"], o.get("position_side", "BOTH"), o))

        return opened, closed, increased, decreased

    async def _poll_once(self) -> None:
        for account in self.store.list_monitored_accounts():
            if not account.enabled:
                continue
            positions = await self._fetch_positions(account)
            if positions is None:
                # API 失败，跳过本轮，避免误判为全部平仓
                continue
            current = [_position_to_dict(p) for p in positions]
            previous = self.store.get_position_snapshot(account.id)
            opened, closed, increased, decreased = self._diff_positions(previous, positions)

            for p in opened:
                msg = _format_position_message(account.name, p, "开仓")
                if self.on_position_alert:
                    self.on_position_alert(account.name, msg)
                logger.info(f"监控账户 {account.name} 新开仓: {p.symbol}({p.position_side}) {p.position_amt}")
                self.store.add_position_event(
                    account.id, p.symbol, "open",
                    old_amt=0, new_amt=p.position_amt,
                    entry_price=p.entry_price, mark_price=p.mark_price,
                    leverage=p.leverage, unrealized_profit=p.unrealized_profit,
                    position_side=p.position_side,
                )

            for sym, ps, old_entry in closed:
                if self.on_position_alert:
                    self.on_position_alert(account.name, f"🔔 *账户监控 - 平仓提醒*\n\n📌 账户: `{account.name}`\n📊 交易对: `{sym}` ({ps}) 已平仓")
                self.store.add_position_event(
                    account.id, sym, "close",
                    old_amt=old_entry.get("position_amt", 0) if old_entry else 0, new_amt=0,
                    entry_price=old_entry.get("entry_price", 0) if old_entry else 0,
                    mark_price=old_entry.get("mark_price", 0) if old_entry else 0,
                    leverage=old_entry.get("leverage", 0) if old_entry else 0,
                    position_side=ps,
                )

            for p, old_amt in increased:
                msg = _format_change_message(account.name, p, old_amt, "加仓")
                if self.on_position_alert:
                    self.on_position_alert(account.name, msg)
                logger.info(f"监控账户 {account.name} 加仓: {p.symbol}({p.position_side}) {old_amt} → {p.position_amt}")
                self.store.add_position_event(
                    account.id, p.symbol, "increase",
                    old_amt=old_amt, new_amt=p.position_amt,
                    entry_price=p.entry_price, mark_price=p.mark_price,
                    leverage=p.leverage, unrealized_profit=p.unrealized_profit,
                    position_side=p.position_side,
                )

            for p, old_amt in decreased:
                msg = _format_change_message(account.name, p, old_amt, "减仓")
                if self.on_position_alert:
                    self.on_position_alert(account.name, msg)
                logger.info(f"监控账户 {account.name} 减仓: {p.symbol}({p.position_side}) {old_amt} → {p.position_amt}")
                self.store.add_position_event(
                    account.id, p.symbol, "decrease",
                    old_amt=old_amt, new_amt=p.position_amt,
                    entry_price=p.entry_price, mark_price=p.mark_price,
                    leverage=p.leverage, unrealized_profit=p.unrealized_profit,
                    position_side=p.position_side,
                )

            self.store.save_position_snapshot(account.id, current)

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                logger.warning("账户监控 _loop 收到 CancelledError")
                raise
            except Exception as e:
                logger.exception(f"账户监控轮询异常: {e}")
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                logger.warning("账户监控 _loop sleep 被取消")
                return

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务结束回调：非主动停止时自动重启"""
        if not self._running:
            return
        if task.cancelled():
            logger.error("账户监控 task 被意外取消，尝试重启")
        else:
            exc = task.exception()
            if exc:
                logger.error(f"账户监控 task 异常退出: {exc}，尝试重启")
            else:
                logger.warning("账户监控 task 意外退出，尝试重启")
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)
        logger.info("账户监控服务已启动")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("账户监控服务已停止")


class CopyTradingService:
    """
    跟单服务
    支持：按保证金比例等比例跟单、按仓位倍数跟单；杠杆可选与源相同或自定义
    """

    def __init__(
        self,
        store: AccountMonitorStore,
        poll_interval_seconds: int = 30,
    ):
        self.store = store
        self.poll_interval = poll_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._follower_hedge_mode: dict = {}  # config_id -> bool (跟单账户持仓模式缓存)

    def _should_skip_open(
        self, config: CopyTradingConfig, target_amt: float,
        source_entry_price: float, current_mark_price: float,
    ) -> bool:
        """检查是否应跳过新开仓（滑点或价格规则）"""
        if source_entry_price <= 0 or current_mark_price <= 0:
            return False
        max_slippage = getattr(config, "max_slippage", 0) or 0
        copy_rule = getattr(config, "copy_rule", "sync") or "sync"
        # 滑点检查
        if max_slippage > 0:
            slippage = abs(current_mark_price - source_entry_price) / source_entry_price
            if slippage > max_slippage:
                logger.info(
                    f"跟单 {config.name} 滑点 {slippage:.4%} 超过阈值 {max_slippage:.4%}，跳过开仓"
                )
                return True
        # 价格规则检查
        if copy_rule == "better_price":
            if target_amt > 0 and current_mark_price > source_entry_price:
                logger.info(
                    f"跟单 {config.name} 做多但当前价 {current_mark_price} > 源开仓价 {source_entry_price}，价格不优，跳过"
                )
                return True
            if target_amt < 0 and current_mark_price < source_entry_price:
                logger.info(
                    f"跟单 {config.name} 做空但当前价 {current_mark_price} < 源开仓价 {source_entry_price}，价格不优，跳过"
                )
                return True
        return False

    def _get_leverage(self, config: CopyTradingConfig, source_leverage: int) -> int:
        if getattr(config, "leverage_mode", "same") == "custom":
            return max(1, min(125, getattr(config, "custom_leverage", 20) or 20))
        return source_leverage or 20

    async def _sync_follower_to_source(
        self,
        config: CopyTradingConfig,
        source_positions: Optional[List[PositionInfo]] = None,
        source_account_full: Optional[dict] = None,
    ) -> None:
        """
        根据源账户持仓同步跟单账户。
        - margin_ratio：按双方可用保证金与开仓保证金等比例计算跟单仓位
        - amount_scale：按仓位倍数（leverage_scale）计算
        杠杆：leverage_mode 为 same 用源杠杆，custom 用 custom_leverage
        """
        if config.is_simulation:
            await self._simulate_sync(config, source_positions, source_account_full)
            return

        follower = BinanceAccountClient(config.follower_api_key, config.follower_api_secret)
        try:
            follower_positions = await follower.get_position_risk()
            if follower_positions is None:
                logger.warning(f"跟单 {config.name} 无法获取跟单账户持仓，跳过本轮")
                return
            current_by_key = {_pos_key(p): p for p in follower_positions}

            copy_mode = getattr(config, "copy_mode", "amount_scale") or "amount_scale"
            copy_ratio = float(getattr(config, "copy_ratio", 1.0) or 1.0)

            # 计算目标仓位：(symbol, position_side) -> (target_amt, leverage, entry_price, mark_price)
            target_map: dict = {}

            if copy_mode == "margin_ratio" and source_account_full:
                source_available = source_account_full.get("available_balance") or 0
                follower_full = await follower.get_account_full()
                if not follower_full:
                    logger.warning(f"跟单 {config.name} 无法获取跟单账户信息")
                    return
                follower_available = follower_full.get("available_balance") or 0
                if source_available <= 0:
                    logger.debug(f"跟单 {config.name} 源账户可用保证金为 0，跳过按比例跟单")
                    return
                for pos in source_account_full.get("positions") or []:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    ps = pos.get("position_side", "BOTH")
                    initial_margin = float(pos.get("initial_margin") or 0)
                    entry_price = float(pos.get("entry_price") or 0)
                    mark_price = float(pos.get("mark_price") or 0)
                    pos_amt = float(pos.get("position_amt") or 0)
                    src_leverage = int(pos.get("leverage") or 0)
                    leverage = self._get_leverage(config, src_leverage)
                    if mark_price <= 0:
                        continue
                    follower_margin = follower_available * (initial_margin / source_available) * copy_ratio
                    target_notional = follower_margin * leverage
                    target_amt = (target_notional / mark_price) * (1 if pos_amt >= 0 else -1)
                    target_map[(symbol, ps)] = (target_amt, leverage, entry_price, mark_price)
            elif copy_mode == "same_margin" and source_account_full:
                for pos in source_account_full.get("positions") or []:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    ps = pos.get("position_side", "BOTH")
                    initial_margin = float(pos.get("initial_margin") or 0)
                    entry_price = float(pos.get("entry_price") or 0)
                    mark_price = float(pos.get("mark_price") or 0)
                    pos_amt = float(pos.get("position_amt") or 0)
                    src_leverage = int(pos.get("leverage") or 0)
                    leverage = self._get_leverage(config, src_leverage)
                    if mark_price <= 0 or initial_margin <= 0:
                        continue
                    target_notional = initial_margin * leverage
                    target_amt = (target_notional / mark_price) * (1 if pos_amt >= 0 else -1)
                    target_map[(symbol, ps)] = (target_amt, leverage, entry_price, mark_price)
            else:
                leverage_scale = float(getattr(config, "leverage_scale", 1.0) or 1.0)
                for p in source_positions or []:
                    target_amt = p.position_amt * leverage_scale
                    leverage = self._get_leverage(config, p.leverage)
                    target_map[_pos_key(p)] = (target_amt, leverage, p.entry_price, p.mark_price)

            # --- 基线过滤：只跟单启用后新产生的仓位 ---
            if not self.store.is_baseline_initialized(config.id):
                if not target_map:
                    # 首次同步但源仓位为空（可能 API 暂未返回数据），跳过不初始化，等下一轮
                    logger.info(f"跟单 {config.name} 首次同步但源仓位为空，跳过初始化基线")
                    return
                baseline_keys = list(target_map.keys())
                self.store.save_copy_baseline(config.id, baseline_keys)
                logger.info(f"跟单 {config.name} 初始化基线，排除 {len(baseline_keys)} 个已有仓位")
                # 首次同步只记录基线，不执行任何开/平仓操作，避免误操作
                return
            baseline = self.store.get_copy_baseline(config.id)
            # 源已平仓的基线仓位移除（后续重新开仓将被跟单）
            for bkey in list(baseline):
                if bkey not in target_map:
                    self.store.remove_from_baseline(config.id, bkey[0], bkey[1])
                    baseline.discard(bkey)
            # 从目标中移除基线仓位
            for bkey in list(baseline):
                if bkey in target_map:
                    del target_map[bkey]

            # --- 获取跟单账户持仓模式，映射 position_side ---
            if config.id not in self._follower_hedge_mode:
                follower_is_hedge = await follower.get_position_mode()
                if follower_is_hedge is None:
                    logger.warning(f"跟单 {config.name} 无法查询跟单账户持仓模式，跳过本轮")
                    return
                self._follower_hedge_mode[config.id] = follower_is_hedge
                logger.info(f"跟单 {config.name} 跟单账户持仓模式: {'双向持仓' if follower_is_hedge else '单向持仓'}")
            follower_is_hedge = self._follower_hedge_mode[config.id]

            # 将 target_map 的 position_side 映射为跟单账户的格式
            if not follower_is_hedge:
                # 跟单账户是单向持仓，将 LONG/SHORT 合并为 BOTH
                remapped: dict = {}
                for (sym, src_ps), val in target_map.items():
                    new_key = (sym, "BOTH")
                    if new_key in remapped:
                        old_val = remapped[new_key]
                        remapped[new_key] = (old_val[0] + val[0], val[1], val[2], val[3])
                    else:
                        remapped[new_key] = val
                target_map = remapped
                # 基线也映射为单向格式（用于清仓循环对比）
                follower_baseline = {(sym, "BOTH") for sym, _ in baseline}
            elif any(ps == "BOTH" for _, ps in target_map):
                # 跟单账户是双向持仓，但源是单向 → 按方向映射
                remapped = {}
                for (sym, src_ps), (t_amt, lev, entry, mark) in target_map.items():
                    if src_ps == "BOTH":
                        new_ps = "LONG" if t_amt >= 0 else "SHORT"
                        remapped[(sym, new_ps)] = (t_amt, lev, entry, mark)
                    else:
                        remapped[(sym, src_ps)] = (t_amt, lev, entry, mark)
                target_map = remapped
                follower_baseline = set()
                for sym, ps in baseline:
                    if ps == "BOTH":
                        follower_baseline.add((sym, "LONG"))
                        follower_baseline.add((sym, "SHORT"))
                    else:
                        follower_baseline.add((sym, ps))
            else:
                follower_baseline = baseline

            for (symbol, ps), (target_amt, leverage, src_entry, mark_price) in target_map.items():
                current_pos = current_by_key.get((symbol, ps))
                current = current_pos.position_amt if current_pos else 0
                if abs(target_amt - current) < 1e-8:
                    continue
                # 新开仓时检查滑点和价格规则
                if abs(current) < 1e-8 and abs(target_amt) >= 1e-8:
                    if self._should_skip_open(config, target_amt, src_entry, mark_price):
                        continue
                # 判断操作类型
                if abs(current) < 1e-8:
                    action = "open"
                elif abs(target_amt) < 1e-8:
                    action = "close"
                elif (current > 0) != (target_amt > 0):
                    action = "close"  # 翻转：先记 close，后续 open 单独记
                elif abs(target_amt) > abs(current):
                    action = "add"
                else:
                    action = "reduce"
                last_result = None
                if current != 0:
                    side = "SELL" if current > 0 else "BUY"
                    qty = round(abs(current), 8)
                    last_result = await follower.place_market_order(symbol, side, qty, position_side=ps)
                    # 翻转/平仓/减仓/加仓 先平旧仓 → 记录 close
                    if action in ("close", "reduce") or ((current > 0) != (target_amt > 0)):
                        exec_price = float(last_result.get("avgPrice", 0)) if last_result and last_result.get("avgPrice") else mark_price
                        oid = str(last_result.get("orderId", "")) if last_result else ""
                        st = str(last_result.get("status", "")) if last_result else "FAILED"
                        self.store.add_copy_trade(
                            config.id, symbol, "close" if action != "reduce" else action,
                            old_amt=current, new_amt=0 if action == "close" else target_amt,
                            price=exec_price, order_id=oid, status=st, position_side=ps,
                        )
                    if last_result is None:
                        logger.warning(f"跟单 {config.name} 平旧仓 {symbol}({ps}) 失败，跳过后续操作")
                        continue
                    await asyncio.sleep(0.3)
                if abs(target_amt) >= 1e-8:
                    side = "BUY" if target_amt > 0 else "SELL"
                    qty = round(abs(target_amt), 8)
                    await follower.set_leverage(symbol, leverage)
                    last_result = await follower.place_market_order(symbol, side, qty, position_side=ps)
                    exec_price = float(last_result.get("avgPrice", 0)) if last_result and last_result.get("avgPrice") else mark_price
                    oid = str(last_result.get("orderId", "")) if last_result else ""
                    st = str(last_result.get("status", "")) if last_result else "FAILED"
                    # 纯开仓 / 翻转后开仓 / 加仓
                    rec_action = "open" if action in ("open", "close") else action  # close 翻转后这里记 open
                    if action == "add":
                        rec_action = "add"
                    self.store.add_copy_trade(
                        config.id, symbol, rec_action,
                        old_amt=0 if action in ("open", "close") else current, new_amt=target_amt,
                        price=exec_price, order_id=oid, status=st, position_side=ps,
                    )
                    if last_result:
                        logger.info(f"跟单 {config.name} 同步 {symbol}({ps}) {side} {qty} 杠杆{leverage}x")
                    else:
                        logger.warning(f"跟单 {config.name} 同步 {symbol}({ps}) {side} {qty} 下单失败")
            for key, current_pos in current_by_key.items():
                if key not in target_map and current_pos.position_amt != 0:
                    if key in follower_baseline:
                        continue  # 基线仓位不主动平仓
                    symbol, ps = key
                    side = "SELL" if current_pos.position_amt > 0 else "BUY"
                    qty = round(abs(current_pos.position_amt), 8)
                    result = await follower.place_market_order(symbol, side, qty, position_side=ps)
                    exec_price = float(result.get("avgPrice", 0)) if result and result.get("avgPrice") else 0
                    oid = str(result.get("orderId", "")) if result else ""
                    st = str(result.get("status", "")) if result else "FAILED"
                    self.store.add_copy_trade(
                        config.id, symbol, "close",
                        old_amt=current_pos.position_amt, new_amt=0,
                        price=exec_price, order_id=oid, status=st, position_side=ps,
                    )
                    if result:
                        logger.info(f"跟单 {config.name} 平仓 {symbol}({ps})")
                    else:
                        logger.warning(f"跟单 {config.name} 平仓 {symbol}({ps}) 下单失败")
        except Exception as e:
            logger.error(f"跟单 {config.name} 同步失败: {e}")
        finally:
            await follower.close()

    async def _simulate_sync(
        self,
        config: CopyTradingConfig,
        source_positions: Optional[List[PositionInfo]] = None,
        source_account_full: Optional[dict] = None,
    ) -> None:
        """模拟跟单：不实际下单，根据源仓位变化记录模拟交易和持仓"""
        try:
            # 1. 从 DB 读取当前模拟持仓
            sim_positions = self.store.get_simulation_positions(config.id)
            current_by_key = {_pos_key(p): p for p in sim_positions}

            copy_mode = getattr(config, "copy_mode", "amount_scale") or "amount_scale"

            # 2. 计算目标仓位 (symbol, position_side) -> (target_amt, leverage, entry_price, mark_price)
            target_map: dict = {}

            if copy_mode == "margin_ratio" and source_account_full:
                source_available = source_account_full.get("available_balance") or 0
                if source_available <= 0:
                    logger.debug(f"模拟跟单 {config.name} 源账户可用保证金为 0，跳过")
                    return
                copy_ratio = float(getattr(config, "copy_ratio", 1.0) or 1.0)
                follower_available = float(getattr(config, "sim_balance", 10000) or 10000)
                for pos in source_account_full.get("positions") or []:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    ps = pos.get("position_side", "BOTH")
                    initial_margin = float(pos.get("initial_margin") or 0)
                    entry_price = float(pos.get("entry_price") or 0)
                    mark_price = float(pos.get("mark_price") or 0)
                    pos_amt = float(pos.get("position_amt") or 0)
                    src_leverage = int(pos.get("leverage") or 0)
                    leverage = self._get_leverage(config, src_leverage)
                    if mark_price <= 0:
                        continue
                    follower_margin = follower_available * (initial_margin / source_available) * copy_ratio
                    target_notional = follower_margin * leverage
                    target_amt = (target_notional / mark_price) * (1 if pos_amt >= 0 else -1)
                    target_map[(symbol, ps)] = (target_amt, leverage, entry_price, mark_price)
            elif copy_mode == "same_margin" and source_account_full:
                for pos in source_account_full.get("positions") or []:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    ps = pos.get("position_side", "BOTH")
                    initial_margin = float(pos.get("initial_margin") or 0)
                    entry_price = float(pos.get("entry_price") or 0)
                    mark_price = float(pos.get("mark_price") or 0)
                    pos_amt = float(pos.get("position_amt") or 0)
                    src_leverage = int(pos.get("leverage") or 0)
                    leverage = self._get_leverage(config, src_leverage)
                    if mark_price <= 0 or initial_margin <= 0:
                        continue
                    target_notional = initial_margin * leverage
                    target_amt = (target_notional / mark_price) * (1 if pos_amt >= 0 else -1)
                    target_map[(symbol, ps)] = (target_amt, leverage, entry_price, mark_price)
            else:
                leverage_scale = float(getattr(config, "leverage_scale", 1.0) or 1.0)
                for p in source_positions or []:
                    target_amt = p.position_amt * leverage_scale
                    leverage = self._get_leverage(config, p.leverage)
                    target_map[_pos_key(p)] = (target_amt, leverage, p.entry_price, p.mark_price)

            # --- 基线过滤：只跟单启用后新产生的仓位 ---
            if not self.store.is_baseline_initialized(config.id):
                if not target_map:
                    logger.info(f"模拟跟单 {config.name} 首次同步但源仓位为空，跳过初始化基线")
                    return
                baseline_keys = list(target_map.keys())
                self.store.save_copy_baseline(config.id, baseline_keys)
                logger.info(f"模拟跟单 {config.name} 初始化基线，排除 {len(baseline_keys)} 个已有仓位")
                return  # 首次同步只记录基线，不执行操作
            baseline = self.store.get_copy_baseline(config.id)
            for bkey in list(baseline):
                if bkey not in target_map:
                    self.store.remove_from_baseline(config.id, bkey[0], bkey[1])
                    baseline.discard(bkey)
            for bkey in list(baseline):
                if bkey in target_map:
                    del target_map[bkey]

            # 3. 对比差异，执行模拟操作
            for (symbol, ps), (target_amt, leverage, src_entry, mark_price) in target_map.items():
                current = current_by_key.get((symbol, ps))
                current_amt = current["position_amt"] if current else 0
                current_entry = current["entry_price"] if current else 0

                if abs(target_amt - current_amt) < 1e-8:
                    continue

                # 新开仓时检查滑点和价格规则
                if abs(current_amt) < 1e-8 and abs(target_amt) >= 1e-8:
                    if self._should_skip_open(config, target_amt, src_entry, mark_price):
                        continue

                if current_amt == 0:
                    self.store.save_simulation_position(config.id, symbol, target_amt, mark_price, leverage, position_side=ps)
                    self.store.add_simulation_trade(config.id, symbol, "open", target_amt, mark_price, 0,
                                                    old_amt=0, new_amt=target_amt, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 开仓 {symbol}({ps}) 数量={target_amt} 价格={mark_price}")

                elif abs(target_amt) < 1e-8:
                    pnl = (mark_price - current_entry) * current_amt
                    self.store.add_simulation_trade(config.id, symbol, "close", current_amt, mark_price, pnl,
                                                    old_amt=current_amt, new_amt=0, position_side=ps)
                    self.store.delete_simulation_position(config.id, symbol, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 平仓 {symbol}({ps}) PnL={pnl:.4f}")

                elif (current_amt > 0) != (target_amt > 0):
                    pnl = (mark_price - current_entry) * current_amt
                    self.store.add_simulation_trade(config.id, symbol, "close", current_amt, mark_price, pnl,
                                                    old_amt=current_amt, new_amt=0, position_side=ps)
                    self.store.save_simulation_position(config.id, symbol, target_amt, mark_price, leverage, position_side=ps)
                    self.store.add_simulation_trade(config.id, symbol, "open", target_amt, mark_price, 0,
                                                    old_amt=0, new_amt=target_amt, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 翻转 {symbol}({ps}) PnL={pnl:.4f} 新开 {target_amt}")

                elif abs(target_amt) > abs(current_amt):
                    delta = target_amt - current_amt
                    new_entry = (current_entry * abs(current_amt) + mark_price * abs(delta)) / abs(target_amt)
                    self.store.save_simulation_position(config.id, symbol, target_amt, new_entry, leverage, position_side=ps)
                    self.store.add_simulation_trade(config.id, symbol, "add", delta, mark_price, 0,
                                                    old_amt=current_amt, new_amt=target_amt, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 加仓 {symbol}({ps}) {current_amt}->{target_amt}")

                else:
                    delta = target_amt - current_amt
                    pnl = (mark_price - current_entry) * (current_amt - target_amt) * (1 if current_amt > 0 else -1)
                    self.store.save_simulation_position(config.id, symbol, target_amt, current_entry, leverage, position_side=ps)
                    self.store.add_simulation_trade(config.id, symbol, "reduce", delta, mark_price, pnl,
                                                    old_amt=current_amt, new_amt=target_amt, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 减仓 {symbol}({ps}) {current_amt}->{target_amt} PnL={pnl:.4f}")

            # 4. 源头已无仓位但模拟还有 -> 平仓（跳过基线仓位）
            for key, current in current_by_key.items():
                if key not in target_map and current["position_amt"] != 0:
                    if key in baseline:
                        continue
                    symbol, ps = key
                    cur_amt = current["position_amt"]
                    pnl_price = 0
                    for p in (source_positions or []):
                        if _pos_key(p) == key:
                            pnl_price = p.mark_price
                            break
                    if pnl_price == 0:
                        pnl_price = current["entry_price"]
                    pnl = (pnl_price - current["entry_price"]) * cur_amt
                    self.store.add_simulation_trade(config.id, symbol, "close", cur_amt, pnl_price, pnl,
                                                    old_amt=cur_amt, new_amt=0, position_side=ps)
                    self.store.delete_simulation_position(config.id, symbol, position_side=ps)
                    logger.info(f"模拟跟单 {config.name} 平仓(源已无) {symbol}({ps}) PnL={pnl:.4f}")

        except Exception as e:
            logger.error(f"模拟跟单 {config.name} 同步失败: {e}")

    async def _poll_once(self) -> None:
        for config in self.store.list_copy_configs():
            if not config.enabled:
                continue
            source = self.store.get_monitored_account(config.source_account_id)
            if not source or not source.enabled:
                continue
            source_client = BinanceAccountClient(source.api_key, source.api_secret)
            try:
                copy_mode = getattr(config, "copy_mode", "amount_scale") or "amount_scale"
                if copy_mode in ("margin_ratio", "same_margin"):
                    source_full = await source_client.get_account_full()
                    if source_full:
                        src_pos_count = len(source_full.get("positions") or [])
                        if src_pos_count > 0:
                            logger.info(f"跟单 {config.name} 源账户有 {src_pos_count} 个持仓(mode={copy_mode})，开始同步")
                        await self._sync_follower_to_source(config, source_account_full=source_full)
                    else:
                        logger.warning(f"跟单 {config.name} 无法获取源账户完整信息(mode={copy_mode})，跳过本轮")
                else:
                    positions = await source_client.get_position_risk()
                    if positions is None:
                        logger.warning(f"跟单 {config.name} 无法获取源账户持仓，跳过本轮")
                        continue
                    if positions:
                        logger.info(f"跟单 {config.name} 源账户有 {len(positions)} 个持仓(mode={copy_mode})，开始同步")
                    await self._sync_follower_to_source(config, source_positions=positions)
            except Exception as e:
                logger.error(f"跟单拉取源账户 {source.name} 失败: {e}")
            finally:
                await source_client.close()

    async def _loop(self) -> None:
        poll_count = 0
        while self._running:
            try:
                await self._poll_once()
                poll_count += 1
                # 每 60 次轮询输出一次心跳日志（约 5 分钟一次）
                if poll_count % 60 == 0:
                    logger.info(f"跟单服务运行中，已完成 {poll_count} 次轮询")
            except asyncio.CancelledError:
                logger.warning("跟单服务 _loop 收到 CancelledError")
                raise  # 重新抛出让 task 正常结束
            except Exception as e:
                logger.exception(f"跟单轮询异常: {e}")
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                logger.warning("跟单服务 _loop sleep 被取消")
                return
        logger.warning("跟单服务 _loop 正常退出 (_running=False)")

    def _on_task_done(self, task: asyncio.Task) -> None:
        """任务结束回调：非主动停止时自动重启"""
        if not self._running:
            return  # 主动 stop，不重启
        exc = task.exception() if not task.cancelled() else None
        if task.cancelled():
            logger.error("跟单服务 task 被意外取消，尝试重启")
        elif exc:
            logger.error(f"跟单服务 task 异常退出: {exc}，尝试重启")
        else:
            logger.warning("跟单服务 task 意外正常退出，尝试重启")
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)
        logger.info("跟单服务已启动")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("跟单服务已停止")
