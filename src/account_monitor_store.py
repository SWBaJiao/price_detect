"""
账户监控与跟单配置存储
SQLite + 可选 API Key 加密（Fernet，密钥来自环境变量 BINANCE_CONFIG_KEY）
"""
import base64
import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from loguru import logger

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    Fernet = None
    InvalidToken = Exception


def _get_fernet() -> Optional["Fernet"]:
    if not HAS_CRYPTO:
        return None
    key = os.getenv("BINANCE_CONFIG_KEY", "").strip()
    if not key or len(key) < 16:
        # 开发环境：用固定密钥派生（生产请设置 BINANCE_CONFIG_KEY）
        default = b"price_detect_dev_key_32bytes!!"
        key_b = hashlib.sha256(default).digest()
        key = base64.urlsafe_b64encode(key_b)
    else:
        key_b = key.encode() if isinstance(key, str) else key
        if len(key_b) != 44:
            key_b = hashlib.sha256(key_b).digest()
            key = base64.urlsafe_b64encode(key_b)
        else:
            key = key_b
    try:
        return Fernet(key)
    except Exception:
        return None


@dataclass
class MonitoredAccount:
    id: Optional[int]
    name: str
    api_key: str
    api_secret: str
    enabled: bool
    created_at: Optional[str] = None


@dataclass
class CopyTradingConfig:
    id: Optional[int]
    name: str                    # 跟单账户名称
    follower_api_key: str
    follower_api_secret: str
    source_account_id: int       # monitored_accounts.id
    enabled: bool
    leverage_scale: float = 1.0  # 按仓位倍数时的倍数
    # 跟单比例与杠杆
    copy_mode: str = "amount_scale"   # "margin_ratio" 按保证金比例 | "amount_scale" 按仓位倍数
    copy_ratio: float = 1.0           # 按保证金比例时：跟单比例（如 1.0=同比例，0.5=一半）
    leverage_mode: str = "same"      # "same" 与源相同杠杆 | "custom" 自定义杠杆
    custom_leverage: int = 20         # leverage_mode=custom 时使用的杠杆倍数
    is_simulation: bool = False       # 模拟跟单模式（不实际下单）
    sim_balance: float = 10000.0     # 模拟账户余额（USDT）
    max_slippage: float = 0.0        # 最大滑点比例（0=不限制，如0.02=2%）
    copy_rule: str = "sync"          # 开仓规则："sync"同步开单 | "better_price"仅价格更优时开单
    created_at: Optional[str] = None


def _default_db_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "data" / "account_monitor.db")


class AccountMonitorStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _default_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._fernet = _get_fernet()
        if not self._fernet and HAS_CRYPTO:
            logger.warning("BINANCE_CONFIG_KEY 未设置，API 密钥将明文存储，仅建议开发环境使用")
        self._init_db()

    def _encrypt(self, s: str) -> str:
        if not s:
            return ""
        if not self._fernet:
            return base64.b64encode(s.encode()).decode()
        return self._fernet.encrypt(s.encode()).decode()

    def _decrypt(self, s: str) -> str:
        if not s:
            return ""
        try:
            if self._fernet:
                return self._fernet.decrypt(s.encode()).decode()
            return base64.b64decode(s.encode()).decode()
        except Exception:
            return ""

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS monitored_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    api_key_enc TEXT NOT NULL,
                    api_secret_enc TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copy_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    follower_api_key_enc TEXT NOT NULL,
                    follower_api_secret_enc TEXT NOT NULL,
                    source_account_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    leverage_scale REAL NOT NULL DEFAULT 1.0,
                    copy_mode TEXT NOT NULL DEFAULT 'amount_scale',
                    copy_ratio REAL NOT NULL DEFAULT 1.0,
                    leverage_mode TEXT NOT NULL DEFAULT 'same',
                    custom_leverage INTEGER NOT NULL DEFAULT 20,
                    created_at TEXT,
                    FOREIGN KEY (source_account_id) REFERENCES monitored_accounts(id)
                )
            """)
            # 兼容旧表：补充新列（若已存在则忽略）
            for col_def in [
                "ALTER TABLE copy_configs ADD COLUMN copy_mode TEXT NOT NULL DEFAULT 'amount_scale'",
                "ALTER TABLE copy_configs ADD COLUMN copy_ratio REAL NOT NULL DEFAULT 1.0",
                "ALTER TABLE copy_configs ADD COLUMN leverage_mode TEXT NOT NULL DEFAULT 'same'",
                "ALTER TABLE copy_configs ADD COLUMN custom_leverage INTEGER NOT NULL DEFAULT 20",
                "ALTER TABLE copy_configs ADD COLUMN is_simulation INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE copy_configs ADD COLUMN sim_balance REAL NOT NULL DEFAULT 10000",
                "ALTER TABLE copy_configs ADD COLUMN max_slippage REAL NOT NULL DEFAULT 0",
                "ALTER TABLE copy_configs ADD COLUMN copy_rule TEXT NOT NULL DEFAULT 'sync'",
            ]:
                try:
                    conn.execute(col_def)
                except sqlite3.OperationalError:
                    pass
            # 迁移：旧 position_snapshots 无 position_side 列时重建
            try:
                conn.execute("ALTER TABLE position_snapshots ADD COLUMN position_side TEXT NOT NULL DEFAULT 'BOTH'")
                conn.execute("DROP TABLE position_snapshots")
            except sqlite3.OperationalError:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_snapshots (
                    account_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL DEFAULT 'BOTH',
                    position_amt REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    mark_price REAL NOT NULL,
                    unrealized_profit REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, symbol, position_side),
                    FOREIGN KEY (account_id) REFERENCES monitored_accounts(id)
                )
            """)
            # 迁移：旧 simulation_positions 无 position_side 列时重建
            try:
                conn.execute("ALTER TABLE simulation_positions ADD COLUMN position_side TEXT NOT NULL DEFAULT 'BOTH'")
                conn.execute("DROP TABLE simulation_positions")
            except sqlite3.OperationalError:
                pass
            # 模拟持仓表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS simulation_positions (
                    config_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL DEFAULT 'BOTH',
                    position_amt REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    leverage INTEGER NOT NULL,
                    opened_at TEXT NOT NULL,
                    PRIMARY KEY (config_id, symbol, position_side),
                    FOREIGN KEY (config_id) REFERENCES copy_configs(id)
                )
            """)
            # 模拟交易记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS simulation_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    amount REAL NOT NULL,
                    old_amt REAL NOT NULL DEFAULT 0,
                    new_amt REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL,
                    pnl REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (config_id) REFERENCES copy_configs(id)
                )
            """)
            # 兼容旧表：补充 simulation_trades 新列
            for col_def in [
                "ALTER TABLE simulation_trades ADD COLUMN old_amt REAL NOT NULL DEFAULT 0",
                "ALTER TABLE simulation_trades ADD COLUMN new_amt REAL NOT NULL DEFAULT 0",
                "ALTER TABLE simulation_trades ADD COLUMN position_side TEXT NOT NULL DEFAULT 'BOTH'",
            ]:
                try:
                    conn.execute(col_def)
                except sqlite3.OperationalError:
                    pass
            # 监控账户开平仓事件记录
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    old_amt REAL NOT NULL DEFAULT 0,
                    new_amt REAL NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    mark_price REAL NOT NULL DEFAULT 0,
                    leverage INTEGER NOT NULL DEFAULT 0,
                    unrealized_profit REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES monitored_accounts(id)
                )
            """)
            # 兼容旧表：position_events 补充 position_side
            try:
                conn.execute("ALTER TABLE position_events ADD COLUMN position_side TEXT NOT NULL DEFAULT 'BOTH'")
            except sqlite3.OperationalError:
                pass
            # 真实跟单交易记录表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copy_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL DEFAULT 'BOTH',
                    action TEXT NOT NULL,
                    old_amt REAL NOT NULL DEFAULT 0,
                    new_amt REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    order_id TEXT,
                    status TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (config_id) REFERENCES copy_configs(id)
                )
            """)
            # 跟单基线表：记录启用时源账户已有的仓位，不跟单这些仓位
            conn.execute("""
                CREATE TABLE IF NOT EXISTS copy_baselines (
                    config_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL DEFAULT 'BOTH',
                    PRIMARY KEY (config_id, symbol, position_side),
                    FOREIGN KEY (config_id) REFERENCES copy_configs(id)
                )
            """)
            conn.commit()

    # ---------- Monitored accounts ----------
    def add_monitored_account(self, name: str, api_key: str, api_secret: str) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO monitored_accounts (name, api_key_enc, api_secret_enc, enabled, created_at)
                   VALUES (?, ?, ?, 1, datetime('now'))""",
                (name.strip(), self._encrypt(api_key.strip()), self._encrypt(api_secret.strip()))
            )
            conn.commit()
            return cur.lastrowid

    def list_monitored_accounts(self) -> List[MonitoredAccount]:
        rows = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                "SELECT id, name, api_key_enc, api_secret_enc, enabled, created_at FROM monitored_accounts ORDER BY id"
            ).fetchall():
                rows.append(MonitoredAccount(
                    id=row["id"],
                    name=row["name"],
                    api_key=self._decrypt(row["api_key_enc"]),
                    api_secret=self._decrypt(row["api_secret_enc"]),
                    enabled=bool(row["enabled"]),
                    created_at=row["created_at"],
                ))
        return rows

    def get_monitored_account(self, account_id: int) -> Optional[MonitoredAccount]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, api_key_enc, api_secret_enc, enabled, created_at FROM monitored_accounts WHERE id = ?",
                (account_id,)
            ).fetchone()
            if not row:
                return None
            return MonitoredAccount(
                id=row["id"],
                name=row["name"],
                api_key=self._decrypt(row["api_key_enc"]),
                api_secret=self._decrypt(row["api_secret_enc"]),
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
            )

    def set_monitored_account_enabled(self, account_id: int, enabled: bool) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("UPDATE monitored_accounts SET enabled = ? WHERE id = ?", (1 if enabled else 0, account_id))
            conn.commit()
            return cur.rowcount > 0

    def delete_monitored_account(self, account_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM position_snapshots WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM position_events WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM copy_configs WHERE source_account_id = ?", (account_id,))
            cur = conn.execute("DELETE FROM monitored_accounts WHERE id = ?", (account_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---------- Position snapshots (for diff and alert) ----------
    def save_position_snapshot(self, account_id: int, positions: List[dict]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM position_snapshots WHERE account_id = ?", (account_id,))
            now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
            for p in positions:
                conn.execute(
                    """INSERT INTO position_snapshots (account_id, symbol, position_side, position_amt, entry_price, mark_price, unrealized_profit, leverage, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        p.get("symbol", ""),
                        p.get("position_side", "BOTH"),
                        p.get("position_amt", 0),
                        p.get("entry_price", 0),
                        p.get("mark_price", 0),
                        p.get("unrealized_profit", 0),
                        p.get("leverage", 0),
                        now,
                    )
                )
            conn.commit()

    def get_position_snapshot(self, account_id: int) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, position_side, position_amt, entry_price, mark_price, unrealized_profit, leverage, updated_at FROM position_snapshots WHERE account_id = ?",
                (account_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Copy configs ----------
    def add_copy_config(
        self,
        name: str,
        follower_api_key: str,
        follower_api_secret: str,
        source_account_id: int,
        leverage_scale: float = 1.0,
        copy_mode: str = "amount_scale",
        copy_ratio: float = 1.0,
        leverage_mode: str = "same",
        custom_leverage: int = 20,
        is_simulation: bool = False,
        sim_balance: float = 10000.0,
        max_slippage: float = 0.0,
        copy_rule: str = "sync",
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO copy_configs (name, follower_api_key_enc, follower_api_secret_enc, source_account_id, enabled, leverage_scale, copy_mode, copy_ratio, leverage_mode, custom_leverage, is_simulation, sim_balance, max_slippage, copy_rule, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (name.strip(), self._encrypt(follower_api_key.strip()), self._encrypt(follower_api_secret.strip()), source_account_id, leverage_scale, copy_mode, copy_ratio, leverage_mode, custom_leverage, 1 if is_simulation else 0, sim_balance, max_slippage, copy_rule)
            )
            conn.commit()
            return cur.lastrowid

    def list_copy_configs(self) -> List[CopyTradingConfig]:
        rows = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("""
                SELECT id, name, follower_api_key_enc, follower_api_secret_enc, source_account_id, enabled, leverage_scale,
                       copy_mode, copy_ratio, leverage_mode, custom_leverage, is_simulation, sim_balance, max_slippage, copy_rule, created_at
                FROM copy_configs ORDER BY id
            """).fetchall():
                rows.append(self._row_to_copy_config(row))
        return rows

    def _row_to_copy_config(self, row: sqlite3.Row) -> CopyTradingConfig:
        keys = row.keys() if hasattr(row, "keys") else []
        def _get(k, default):
            return row[k] if k in keys else default
        return CopyTradingConfig(
            id=row["id"],
            name=row["name"],
            follower_api_key=self._decrypt(row["follower_api_key_enc"]),
            follower_api_secret=self._decrypt(row["follower_api_secret_enc"]),
            source_account_id=row["source_account_id"],
            enabled=bool(row["enabled"]),
            leverage_scale=float(row["leverage_scale"] or 1.0),
            copy_mode=str(_get("copy_mode", "amount_scale") or "amount_scale"),
            copy_ratio=float(_get("copy_ratio", 1.0) or 1.0),
            leverage_mode=str(_get("leverage_mode", "same") or "same"),
            custom_leverage=int(_get("custom_leverage", 20) or 20),
            is_simulation=bool(_get("is_simulation", 0)),
            sim_balance=float(_get("sim_balance", 10000) or 10000),
            max_slippage=float(_get("max_slippage", 0) or 0),
            copy_rule=str(_get("copy_rule", "sync") or "sync"),
            created_at=row["created_at"],
        )

    def get_copy_config(self, config_id: int) -> Optional[CopyTradingConfig]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT id, name, follower_api_key_enc, follower_api_secret_enc, source_account_id, enabled, leverage_scale,
                       copy_mode, copy_ratio, leverage_mode, custom_leverage, is_simulation, sim_balance, max_slippage, copy_rule, created_at
                FROM copy_configs WHERE id = ?
            """, (config_id,)).fetchone()
            if not row:
                return None
            return self._row_to_copy_config(row)

    def update_copy_config(self, config_id: int, **kwargs) -> bool:
        """更新跟单配置（只更新传入的字段）"""
        allowed = {
            "name", "source_account_id", "leverage_scale",
            "copy_mode", "copy_ratio", "leverage_mode", "custom_leverage",
            "is_simulation", "sim_balance", "max_slippage", "copy_rule",
        }
        # API Key/Secret 需要加密
        key_fields = {"follower_api_key", "follower_api_secret"}
        sets = []
        values = []
        for k, v in kwargs.items():
            if k in allowed:
                if k == "is_simulation":
                    v = 1 if v else 0
                sets.append(f"{k} = ?")
                values.append(v)
            elif k in key_fields and v:
                enc_col = f"{k}_enc"
                sets.append(f"{enc_col} = ?")
                values.append(self._encrypt(str(v).strip()))
        if not sets:
            return False
        values.append(config_id)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                f"UPDATE copy_configs SET {', '.join(sets)} WHERE id = ?", values
            )
            conn.commit()
            return cur.rowcount > 0

    def set_copy_config_enabled(self, config_id: int, enabled: bool) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("UPDATE copy_configs SET enabled = ? WHERE id = ?", (1 if enabled else 0, config_id))
            if not enabled:
                # 关闭时清除基线，下次启用将重新初始化
                conn.execute("DELETE FROM copy_baselines WHERE config_id = ?", (config_id,))
            conn.commit()
            return cur.rowcount > 0

    def delete_copy_config(self, config_id: int) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM simulation_positions WHERE config_id = ?", (config_id,))
            conn.execute("DELETE FROM simulation_trades WHERE config_id = ?", (config_id,))
            conn.execute("DELETE FROM copy_trades WHERE config_id = ?", (config_id,))
            conn.execute("DELETE FROM copy_baselines WHERE config_id = ?", (config_id,))
            cur = conn.execute("DELETE FROM copy_configs WHERE id = ?", (config_id,))
            conn.commit()
            return cur.rowcount > 0

    # ---------- Simulation positions & trades ----------
    def get_simulation_positions(self, config_id: int) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT symbol, position_side, position_amt, entry_price, leverage, opened_at FROM simulation_positions WHERE config_id = ?",
                (config_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def save_simulation_position(self, config_id: int, symbol: str, position_amt: float, entry_price: float, leverage: int, position_side: str = "BOTH"):
        now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO simulation_positions (config_id, symbol, position_side, position_amt, entry_price, leverage, opened_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (config_id, symbol, position_side, position_amt, entry_price, leverage, now)
            )
            conn.commit()

    def delete_simulation_position(self, config_id: int, symbol: str, position_side: str = "BOTH"):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM simulation_positions WHERE config_id = ? AND symbol = ? AND position_side = ?", (config_id, symbol, position_side))
            conn.commit()

    def add_simulation_trade(self, config_id: int, symbol: str, action: str, amount: float, price: float, pnl: float = 0, old_amt: float = 0, new_amt: float = 0, position_side: str = "BOTH"):
        now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO simulation_trades (config_id, symbol, position_side, action, amount, old_amt, new_amt, price, pnl, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (config_id, symbol, position_side, action, amount, old_amt, new_amt, price, pnl, now)
            )
            conn.commit()

    def get_simulation_trades(self, config_id: int, limit: int = 100) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, symbol, position_side, action, amount, old_amt, new_amt, price, pnl, created_at FROM simulation_trades WHERE config_id = ? ORDER BY id DESC LIMIT ?",
                (config_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Position events (monitored account trade history) ----------
    def add_position_event(
        self,
        account_id: int,
        symbol: str,
        action: str,
        old_amt: float = 0,
        new_amt: float = 0,
        entry_price: float = 0,
        mark_price: float = 0,
        leverage: int = 0,
        unrealized_profit: float = 0,
        position_side: str = "BOTH",
    ):
        now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO position_events
                   (account_id, symbol, position_side, action, old_amt, new_amt, entry_price, mark_price, leverage, unrealized_profit, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, symbol, position_side, action, old_amt, new_amt, entry_price, mark_price, leverage, unrealized_profit, now)
            )
            conn.commit()

    def get_position_events(self, account_id: int, limit: int = 200) -> List[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT id, symbol, position_side, action, old_amt, new_amt, entry_price, mark_price, leverage, unrealized_profit, created_at
                   FROM position_events WHERE account_id = ? ORDER BY id DESC LIMIT ?""",
                (account_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Copy trades (real copy trading history) ----------
    def add_copy_trade(
        self, config_id: int, symbol: str, action: str,
        old_amt: float = 0, new_amt: float = 0, price: float = 0,
        order_id: str = "", status: str = "", position_side: str = "BOTH",
    ):
        now = __import__("datetime").datetime.utcnow().isoformat() + "Z"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO copy_trades (config_id, symbol, position_side, action, old_amt, new_amt, price, order_id, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (config_id, symbol, position_side, action, old_amt, new_amt, price, order_id, status, now)
            )
            conn.commit()

    def get_copy_trades(self, config_id: int, limit: int = 100) -> list:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, symbol, position_side, action, old_amt, new_amt, price, order_id, status, created_at FROM copy_trades WHERE config_id = ? ORDER BY id DESC LIMIT ?",
                (config_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Copy baselines (exclude pre-existing positions) ----------
    def is_baseline_initialized(self, config_id: int) -> bool:
        """检查基线是否已初始化（含标记行）"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM copy_baselines WHERE config_id = ? AND symbol = '__baseline__' LIMIT 1",
                (config_id,)
            ).fetchone()
            return row is not None

    def save_copy_baseline(self, config_id: int, position_keys: list):
        """保存源账户已有仓位为基线。空列表表示源无持仓。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM copy_baselines WHERE config_id = ?", (config_id,))
            # 标记行：表示基线已初始化
            conn.execute(
                "INSERT INTO copy_baselines (config_id, symbol, position_side) VALUES (?, '__baseline__', '__init__')",
                (config_id,)
            )
            for sym, ps in position_keys:
                conn.execute(
                    "INSERT INTO copy_baselines (config_id, symbol, position_side) VALUES (?, ?, ?)",
                    (config_id, sym, ps)
                )
            conn.commit()

    def get_copy_baseline(self, config_id: int) -> set:
        """获取基线仓位集合 {(symbol, position_side), ...}"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, position_side FROM copy_baselines WHERE config_id = ? AND symbol != '__baseline__'",
                (config_id,)
            ).fetchall()
            return {(r[0], r[1]) for r in rows}

    def remove_from_baseline(self, config_id: int, symbol: str, position_side: str = "BOTH"):
        """从基线移除已平仓的仓位（后续重新开仓将被跟单）"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM copy_baselines WHERE config_id = ? AND symbol = ? AND position_side = ?",
                (config_id, symbol, position_side)
            )
            conn.commit()
