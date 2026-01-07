"""
交易数据存储

使用SQLite持久化：
- 持仓记录
- 交易记录
- 账户状态
- 权益曲线
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .models import (
    AccountState,
    ExitReason,
    OrderSide,
    Position,
    Trade
)


class TradingDataStore:
    """交易数据存储"""

    def __init__(self, db_path: str = "data/ml_data.db"):
        """
        初始化存储

        Args:
            db_path: SQLite数据库路径
        """
        self.db_path = db_path

        # 确保目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # 初始化数据库
        self._init_db()

        logger.info(f"交易数据存储初始化: {db_path}")

    def _init_db(self):
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # 持仓表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    leverage INTEGER DEFAULT 15,
                    margin REAL NOT NULL,
                    take_profit_price REAL,
                    stop_loss_price REAL,
                    trailing_stop_distance REAL,
                    max_hold_seconds INTEGER DEFAULT 900,
                    signal_confidence REAL,
                    signal_reason TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 交易表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_price REAL NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_reason TEXT NOT NULL,
                    leverage INTEGER DEFAULT 15,
                    realized_pnl REAL NOT NULL,
                    realized_pnl_pct REAL NOT NULL,
                    roi REAL NOT NULL,
                    commission REAL NOT NULL,
                    signal_confidence REAL,
                    signal_reason TEXT,
                    margin REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 账户状态表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS account_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    balance REAL NOT NULL,
                    equity REAL NOT NULL,
                    margin_used REAL NOT NULL,
                    margin_available REAL NOT NULL,
                    margin_ratio REAL,
                    open_positions INTEGER NOT NULL,
                    total_trades INTEGER NOT NULL,
                    win_trades INTEGER,
                    total_pnl REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    win_rate REAL NOT NULL,
                    UNIQUE(timestamp)
                )
            """)

            # 权益曲线表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS equity_curve (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    balance REAL NOT NULL,
                    drawdown REAL DEFAULT 0,
                    UNIQUE(symbol, timestamp)
                )
            """)

            # 创建索引
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_exit_time
                ON trades(exit_time)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_positions_symbol
                ON positions(symbol)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_account_states_timestamp
                ON account_states(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_equity_curve_timestamp
                ON equity_curve(timestamp)
            """)

            conn.commit()

    # ==================== 持仓操作 ====================

    def save_position(self, position: Position):
        """
        保存持仓

        Args:
            position: 持仓对象
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO positions
                (position_id, symbol, side, quantity, entry_price, entry_time,
                 leverage, margin, take_profit_price, stop_loss_price,
                 trailing_stop_distance, max_hold_seconds, signal_confidence,
                 signal_reason, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                position.position_id,
                position.symbol,
                position.side.value,
                position.quantity,
                position.entry_price,
                position.entry_time.isoformat(),
                position.leverage,
                position.margin,
                position.take_profit_price,
                position.stop_loss_price,
                position.trailing_stop_distance,
                position.max_hold_seconds,
                position.signal_confidence,
                position.signal_reason,
                'open'
            ))
            conn.commit()

    def close_position_in_db(self, position_id: str):
        """
        标记持仓为已平仓

        Args:
            position_id: 持仓ID
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE positions SET status = 'closed'
                WHERE position_id = ?
            """, (position_id,))
            conn.commit()

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取未平仓持仓

        Args:
            symbol: 交易对，None返回所有

        Returns:
            持仓列表
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if symbol:
                cursor.execute("""
                    SELECT * FROM positions
                    WHERE status = 'open' AND symbol = ?
                    ORDER BY entry_time DESC
                """, (symbol,))
            else:
                cursor.execute("""
                    SELECT * FROM positions
                    WHERE status = 'open'
                    ORDER BY entry_time DESC
                """)

            return [dict(row) for row in cursor.fetchall()]

    # ==================== 交易操作 ====================

    def save_trade(self, trade: Trade):
        """
        保存交易记录

        Args:
            trade: 交易对象
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO trades
                (trade_id, symbol, side, quantity, entry_price, entry_time,
                 exit_price, exit_time, exit_reason, leverage, realized_pnl,
                 realized_pnl_pct, roi, commission, signal_confidence,
                 signal_reason, margin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.trade_id,
                trade.symbol,
                trade.side.value,
                trade.quantity,
                trade.entry_price,
                trade.entry_time.isoformat(),
                trade.exit_price,
                trade.exit_time.isoformat(),
                trade.exit_reason.value,
                trade.leverage,
                trade.realized_pnl,
                trade.realized_pnl_pct,
                trade.roi,
                trade.commission,
                trade.signal_confidence,
                trade.signal_reason,
                trade.margin
            ))
            conn.commit()

        # 同时关闭对应的持仓记录
        self.close_position_in_db(trade.trade_id[:8])

    def get_trades(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 1000
    ) -> List[Trade]:
        """
        获取交易记录

        Args:
            symbol: 交易对
            start_time: 开始时间
            end_time: 结束时间
            limit: 最大数量

        Returns:
            Trade 对象列表
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT * FROM trades WHERE 1=1"
            params = []

            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            if start_time:
                query += " AND exit_time >= ?"
                params.append(start_time.isoformat())
            if end_time:
                query += " AND exit_time <= ?"
                params.append(end_time.isoformat())

            query += " ORDER BY exit_time DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)

            trades = []
            for row in cursor.fetchall():
                trade = Trade(
                    trade_id=row['trade_id'],
                    symbol=row['symbol'],
                    side=OrderSide(row['side']),
                    quantity=row['quantity'],
                    entry_price=row['entry_price'],
                    entry_time=datetime.fromisoformat(row['entry_time']),
                    exit_price=row['exit_price'],
                    exit_time=datetime.fromisoformat(row['exit_time']),
                    exit_reason=ExitReason(row['exit_reason']),
                    leverage=row['leverage'],
                    realized_pnl=row['realized_pnl'],
                    realized_pnl_pct=row['realized_pnl_pct'],
                    roi=row['roi'],
                    commission=row['commission'],
                    signal_confidence=row['signal_confidence'] or 0,
                    signal_reason=row['signal_reason'] or '',
                    margin=row['margin'] or 0
                )
                trades.append(trade)

            return trades

    def get_trade_statistics(
        self,
        symbol: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> Dict:
        """
        获取交易统计

        Args:
            symbol: 交易对
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            统计字典
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            where_clause = "WHERE 1=1"
            params = []

            if symbol:
                where_clause += " AND symbol = ?"
                params.append(symbol)
            if start_time:
                where_clause += " AND exit_time >= ?"
                params.append(start_time.isoformat())
            if end_time:
                where_clause += " AND exit_time <= ?"
                params.append(end_time.isoformat())

            # 基本统计
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as win_trades,
                    SUM(realized_pnl) as total_pnl,
                    AVG(realized_pnl) as avg_pnl,
                    AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) as avg_win,
                    AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) as avg_loss,
                    MAX(realized_pnl) as max_win,
                    MIN(realized_pnl) as min_loss,
                    AVG(roi) as avg_roi
                FROM trades
                {where_clause}
            """, params)

            row = cursor.fetchone()

            if not row or row[0] == 0:
                return {
                    'total_trades': 0,
                    'win_trades': 0,
                    'win_rate': 0,
                    'total_pnl': 0,
                    'avg_pnl': 0,
                }

            total_trades = row[0]
            win_trades = row[1] or 0
            total_pnl = row[2] or 0
            avg_pnl = row[3] or 0
            avg_win = row[4] or 0
            avg_loss = abs(row[5] or 0)
            max_win = row[6] or 0
            min_loss = row[7] or 0
            avg_roi = row[8] or 0

            # 盈亏比
            profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')

            return {
                'total_trades': total_trades,
                'win_trades': win_trades,
                'loss_trades': total_trades - win_trades,
                'win_rate': win_trades / total_trades,
                'total_pnl': total_pnl,
                'avg_pnl': avg_pnl,
                'avg_win': avg_win,
                'avg_loss': avg_loss,
                'max_win': max_win,
                'max_loss': min_loss,
                'profit_factor': profit_factor,
                'avg_roi': avg_roi
            }

    # ==================== 账户状态操作 ====================

    def save_account_state(self, state: AccountState):
        """
        保存账户状态

        Args:
            state: 账户状态对象
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO account_states
                (timestamp, balance, equity, margin_used, margin_available,
                 margin_ratio, open_positions, total_trades, win_trades,
                 total_pnl, max_drawdown, win_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.timestamp.isoformat(),
                state.balance,
                state.equity,
                state.margin_used,
                state.margin_available,
                state.margin_ratio,
                state.open_positions,
                state.total_trades,
                state.win_trades,
                state.total_pnl,
                state.max_drawdown,
                state.win_rate
            ))
            conn.commit()

    def get_latest_account_state(self) -> Optional[Dict]:
        """
        获取最新账户状态

        Returns:
            账户状态字典
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM account_states
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None

    # ==================== 权益曲线操作 ====================

    def save_equity_point(
        self,
        timestamp: datetime,
        equity: float,
        balance: float,
        drawdown: float = 0,
        symbol: str = "ALL"
    ):
        """
        保存权益曲线点

        Args:
            timestamp: 时间戳
            equity: 权益
            balance: 余额
            drawdown: 回撤
            symbol: 交易对
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO equity_curve
                (symbol, timestamp, equity, balance, drawdown)
                VALUES (?, ?, ?, ?, ?)
            """, (
                symbol,
                timestamp.isoformat(),
                equity,
                balance,
                drawdown
            ))
            conn.commit()

    def get_equity_curve(
        self,
        symbol: str = "ALL",
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[Dict]:
        """
        获取权益曲线

        Args:
            symbol: 交易对
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            权益曲线数据列表
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT * FROM equity_curve WHERE symbol = ?"
            params = [symbol]

            if start_time:
                query += " AND timestamp >= ?"
                params.append(start_time.isoformat())
            if end_time:
                query += " AND timestamp <= ?"
                params.append(end_time.isoformat())

            query += " ORDER BY timestamp ASC"

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    # ==================== 清理操作 ====================

    def cleanup_old_data(self, days: int = 30):
        """
        清理旧数据

        Args:
            days: 保留最近多少天的数据
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # 清理已平仓的持仓
            cursor.execute("""
                DELETE FROM positions
                WHERE status = 'closed' AND entry_time < ?
            """, (cutoff,))

            # 清理旧交易（保留统计）
            cursor.execute("""
                DELETE FROM trades WHERE exit_time < ?
            """, (cutoff,))

            # 清理旧账户状态
            cursor.execute("""
                DELETE FROM account_states WHERE timestamp < ?
            """, (cutoff,))

            # 清理旧权益曲线
            cursor.execute("""
                DELETE FROM equity_curve WHERE timestamp < ?
            """, (cutoff,))

            conn.commit()

        logger.info(f"清理 {days} 天前的旧数据完成")
