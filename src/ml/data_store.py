"""
ML数据持久化层
使用SQLite存储特征、标签和价格快照，支持增量更新和批量查询
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from loguru import logger

from ..models import MLFeatureVector, MLLabel


class MLDataStore:
    """
    ML数据存储

    表结构：
    - features: 特征向量表
    - labels: 标签表
    - price_snapshots: 价格快照表（用于标签回填）
    - alerts: 告警记录表（用于回测验证）
    """

    def __init__(self, db_path: str = "data/ml_data.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f"ML数据库初始化完成: {self.db_path}")

    def _init_db(self):
        """初始化数据库表"""
        with self._connect() as conn:
            conn.executescript("""
                -- 特征表
                CREATE TABLE IF NOT EXISTS features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    feature_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_features_symbol_ts
                ON features(symbol, timestamp);

                CREATE INDEX IF NOT EXISTS idx_features_timestamp
                ON features(timestamp);

                -- 标签表
                CREATE TABLE IF NOT EXISTS labels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    feature_timestamp TEXT NOT NULL,
                    return_1m REAL,
                    return_5m REAL,
                    return_15m REAL,
                    return_30m REAL,
                    direction_5m INTEGER,
                    direction_15m INTEGER,
                    max_profit_5m REAL,
                    max_drawdown_5m REAL,
                    label_generated_at TEXT,
                    UNIQUE(symbol, feature_timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_labels_symbol_ts
                ON labels(symbol, feature_timestamp);

                -- 价格快照表（用于标签回填）
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    price REAL NOT NULL,
                    volume REAL,
                    quote_volume REAL,
                    UNIQUE(symbol, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_prices_symbol_ts
                ON price_snapshots(symbol, timestamp);

                -- 告警记录表
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    tier_label TEXT,
                    change_percent REAL,
                    threshold REAL,
                    was_filtered INTEGER DEFAULT 0,
                    filter_reason TEXT,
                    extra_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_symbol_ts
                ON alerts(symbol, timestamp);

                CREATE INDEX IF NOT EXISTS idx_alerts_filtered
                ON alerts(was_filtered);
            """)

    @contextmanager
    def _connect(self):
        """数据库连接上下文管理"""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    # ==================== 特征存储 ====================

    def save_feature(self, feature: MLFeatureVector):
        """保存单个特征向量"""
        try:
            with self._connect() as conn:
                feature_dict = asdict(feature)
                # 转换datetime为字符串
                feature_dict['timestamp'] = feature.timestamp.isoformat()
                # 转换alert_types列表为JSON
                feature_dict['alert_types'] = json.dumps(feature_dict.get('alert_types', []))

                conn.execute("""
                    INSERT OR REPLACE INTO features (symbol, timestamp, feature_json)
                    VALUES (?, ?, ?)
                """, (
                    feature.symbol,
                    feature.timestamp.isoformat(),
                    json.dumps(feature_dict, default=str)
                ))
        except Exception as e:
            logger.error(f"保存特征失败 {feature.symbol}: {e}")

    def save_features_batch(self, features: List[MLFeatureVector]):
        """批量保存特征"""
        if not features:
            return

        try:
            with self._connect() as conn:
                data = []
                for f in features:
                    feature_dict = asdict(f)
                    feature_dict['timestamp'] = f.timestamp.isoformat()
                    feature_dict['alert_types'] = json.dumps(feature_dict.get('alert_types', []))
                    data.append((
                        f.symbol,
                        f.timestamp.isoformat(),
                        json.dumps(feature_dict, default=str)
                    ))

                conn.executemany("""
                    INSERT OR REPLACE INTO features (symbol, timestamp, feature_json)
                    VALUES (?, ?, ?)
                """, data)
            logger.debug(f"批量保存 {len(features)} 条特征")
        except Exception as e:
            logger.error(f"批量保存特征失败: {e}")

    def get_features(
        self,
        symbol: Optional[str] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None,
        limit: int = 10000
    ) -> List[Dict]:
        """查询特征数据"""
        query = "SELECT * FROM features WHERE 1=1"
        params = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if start_ts:
            query += " AND timestamp >= ?"
            params.append(start_ts.isoformat())
        if end_ts:
            query += " AND timestamp <= ?"
            params.append(end_ts.isoformat())

        query += f" ORDER BY timestamp DESC LIMIT {limit}"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            results = []
            for row in rows:
                feature_data = json.loads(row['feature_json'])
                results.append(feature_data)
            return results

    def get_unlabeled_features(
        self,
        symbol: Optional[str] = None,
        min_age_seconds: int = 1800,  # 默认30分钟前的数据才需要标签
        limit: int = 1000
    ) -> List[Dict]:
        """获取尚未生成标签的特征"""
        cutoff = datetime.now().isoformat()

        query = """
            SELECT f.* FROM features f
            LEFT JOIN labels l ON f.symbol = l.symbol AND f.timestamp = l.feature_timestamp
            WHERE l.id IS NULL
            AND datetime(f.timestamp) <= datetime(?, '-' || ? || ' seconds')
        """
        params = [cutoff, min_age_seconds]

        if symbol:
            query += " AND f.symbol = ?"
            params.append(symbol)

        query += f" ORDER BY f.timestamp ASC LIMIT {limit}"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [json.loads(row['feature_json']) for row in rows]

    # ==================== 标签存储 ====================

    def save_label(self, label: MLLabel):
        """保存标签"""
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO labels
                    (symbol, feature_timestamp, return_1m, return_5m, return_15m, return_30m,
                     direction_5m, direction_15m, max_profit_5m, max_drawdown_5m, label_generated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    label.symbol,
                    label.timestamp.isoformat(),
                    label.return_1m,
                    label.return_5m,
                    label.return_15m,
                    label.return_30m,
                    label.direction_5m,
                    label.direction_15m,
                    label.max_profit_5m,
                    label.max_drawdown_5m,
                    label.label_generated_at.isoformat() if label.label_generated_at else None
                ))
        except Exception as e:
            logger.error(f"保存标签失败 {label.symbol}: {e}")

    def save_labels_batch(self, labels: List[MLLabel]):
        """批量保存标签"""
        if not labels:
            return

        try:
            with self._connect() as conn:
                data = [
                    (
                        l.symbol,
                        l.timestamp.isoformat(),
                        l.return_1m,
                        l.return_5m,
                        l.return_15m,
                        l.return_30m,
                        l.direction_5m,
                        l.direction_15m,
                        l.max_profit_5m,
                        l.max_drawdown_5m,
                        l.label_generated_at.isoformat() if l.label_generated_at else None
                    )
                    for l in labels
                ]

                conn.executemany("""
                    INSERT OR REPLACE INTO labels
                    (symbol, feature_timestamp, return_1m, return_5m, return_15m, return_30m,
                     direction_5m, direction_15m, max_profit_5m, max_drawdown_5m, label_generated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, data)
            logger.debug(f"批量保存 {len(labels)} 条标签")
        except Exception as e:
            logger.error(f"批量保存标签失败: {e}")

    # ==================== 价格快照 ====================

    def save_price_snapshot(
        self,
        symbol: str,
        timestamp: datetime,
        price: float,
        volume: float = 0,
        quote_volume: float = 0
    ):
        """保存价格快照（用于标签回填）"""
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO price_snapshots
                    (symbol, timestamp, price, volume, quote_volume)
                    VALUES (?, ?, ?, ?, ?)
                """, (symbol, timestamp.isoformat(), price, volume, quote_volume))
        except Exception as e:
            logger.error(f"保存价格快照失败 {symbol}: {e}")

    def save_price_snapshots_batch(
        self,
        snapshots: List[tuple]  # [(symbol, timestamp, price, volume, quote_volume), ...]
    ):
        """批量保存价格快照"""
        if not snapshots:
            return

        try:
            with self._connect() as conn:
                data = [
                    (s[0], s[1].isoformat() if isinstance(s[1], datetime) else s[1],
                     s[2], s[3] if len(s) > 3 else 0, s[4] if len(s) > 4 else 0)
                    for s in snapshots
                ]
                conn.executemany("""
                    INSERT OR REPLACE INTO price_snapshots
                    (symbol, timestamp, price, volume, quote_volume)
                    VALUES (?, ?, ?, ?, ?)
                """, data)
        except Exception as e:
            logger.error(f"批量保存价格快照失败: {e}")

    def get_price_at_time(
        self,
        symbol: str,
        target_ts: datetime,
        tolerance_seconds: int = 5
    ) -> Optional[float]:
        """
        获取指定时间点的价格
        在tolerance_seconds范围内找最接近的价格
        """
        with self._connect() as conn:
            # 查找最接近的价格点
            row = conn.execute("""
                SELECT price, timestamp,
                       ABS(julianday(timestamp) - julianday(?)) * 86400 as diff_seconds
                FROM price_snapshots
                WHERE symbol = ?
                AND datetime(timestamp) BETWEEN datetime(?, '-' || ? || ' seconds')
                                           AND datetime(?, '+' || ? || ' seconds')
                ORDER BY diff_seconds ASC
                LIMIT 1
            """, (
                target_ts.isoformat(),
                symbol,
                target_ts.isoformat(), tolerance_seconds,
                target_ts.isoformat(), tolerance_seconds
            )).fetchone()

            if row and row['diff_seconds'] <= tolerance_seconds:
                return row['price']
            return None

    def get_prices_in_window(
        self,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime
    ) -> List[tuple]:
        """获取时间窗口内的所有价格"""
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT timestamp, price FROM price_snapshots
                WHERE symbol = ?
                AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC
            """, (symbol, start_ts.isoformat(), end_ts.isoformat())).fetchall()

            return [(row['timestamp'], row['price']) for row in rows]

    # ==================== 告警记录 ====================

    def save_alert(
        self,
        symbol: str,
        timestamp: datetime,
        alert_type: str,
        tier_label: str = "",
        change_percent: float = 0,
        threshold: float = 0,
        was_filtered: bool = False,
        filter_reason: str = "",
        extra_info: dict = None
    ):
        """保存告警记录"""
        try:
            with self._connect() as conn:
                conn.execute("""
                    INSERT INTO alerts
                    (symbol, timestamp, alert_type, tier_label, change_percent,
                     threshold, was_filtered, filter_reason, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    symbol,
                    timestamp.isoformat(),
                    alert_type,
                    tier_label,
                    change_percent,
                    threshold,
                    1 if was_filtered else 0,
                    filter_reason,
                    json.dumps(extra_info or {})
                ))
        except Exception as e:
            logger.error(f"保存告警记录失败 {symbol}: {e}")

    def get_filtered_alerts(
        self,
        symbol: Optional[str] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """获取被过滤的告警记录"""
        query = "SELECT * FROM alerts WHERE was_filtered = 1"
        params = []

        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if start_ts:
            query += " AND timestamp >= ?"
            params.append(start_ts.isoformat())
        if end_ts:
            query += " AND timestamp <= ?"
            params.append(end_ts.isoformat())

        query += f" ORDER BY timestamp DESC LIMIT {limit}"

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    # ==================== 训练数据导出 ====================

    def export_training_data(
        self,
        symbol: Optional[str] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None
    ) -> Iterator[Dict]:
        """
        导出训练数据（特征+标签联合）

        只返回有标签的数据
        """
        query = """
            SELECT f.feature_json, l.*
            FROM features f
            INNER JOIN labels l ON f.symbol = l.symbol AND f.timestamp = l.feature_timestamp
            WHERE 1=1
        """
        params = []

        if symbol:
            query += " AND f.symbol = ?"
            params.append(symbol)
        if start_ts:
            query += " AND f.timestamp >= ?"
            params.append(start_ts.isoformat())
        if end_ts:
            query += " AND f.timestamp <= ?"
            params.append(end_ts.isoformat())

        query += " ORDER BY f.timestamp ASC"

        with self._connect() as conn:
            for row in conn.execute(query, params):
                feature_data = json.loads(row['feature_json'])
                label_data = {
                    'return_1m': row['return_1m'],
                    'return_5m': row['return_5m'],
                    'return_15m': row['return_15m'],
                    'return_30m': row['return_30m'],
                    'direction_5m': row['direction_5m'],
                    'direction_15m': row['direction_15m'],
                    'max_profit_5m': row['max_profit_5m'],
                    'max_drawdown_5m': row['max_drawdown_5m'],
                }
                yield {**feature_data, **label_data}

    def export_to_csv(
        self,
        output_path: str,
        symbol: Optional[str] = None,
        start_ts: Optional[datetime] = None,
        end_ts: Optional[datetime] = None
    ) -> int:
        """导出训练数据到CSV文件"""
        import csv

        count = 0
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = None

            for row in self.export_training_data(symbol, start_ts, end_ts):
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=row.keys())
                    writer.writeheader()
                writer.writerow(row)
                count += 1

        logger.info(f"导出 {count} 条训练数据到 {output_path}")
        return count

    # ==================== 统计信息 ====================

    def get_feature_statistics(self, start_time: datetime = None) -> Dict:
        """
        获取特征统计信息

        Args:
            start_time: 开始时间

        Returns:
            统计字典
        """
        with self._connect() as conn:
            # 构建时间条件
            time_condition = ""
            params = []
            if start_time:
                time_condition = "WHERE timestamp >= ?"
                params.append(start_time.isoformat())

            # 总数
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM features {time_condition}",
                params
            ).fetchone()
            total_count = row['cnt']

            # 各特征的统计（从最近100条数据计算）
            features_stats = []
            if total_count > 0:
                # 获取最近的特征样本
                rows = conn.execute(f"""
                    SELECT feature_json FROM features
                    {time_condition}
                    ORDER BY timestamp DESC
                    LIMIT 100
                """, params).fetchall()

                if rows:
                    # 解析特征并计算统计
                    feature_values = {}
                    for row in rows:
                        feature_data = json.loads(row['feature_json'])
                        for key, value in feature_data.items():
                            if isinstance(value, (int, float)) and key not in ['timestamp', 'symbol']:
                                if key not in feature_values:
                                    feature_values[key] = []
                                feature_values[key].append(value)

                    # 计算每个特征的统计
                    for name, values in feature_values.items():
                        if values:
                            import statistics
                            features_stats.append({
                                'name': name,
                                'min': min(values),
                                'max': max(values),
                                'mean': statistics.mean(values),
                                'std': statistics.stdev(values) if len(values) > 1 else 0
                            })

            return {
                'total_count': total_count,
                'features': features_stats[:20]  # 限制返回前20个特征
            }

    def get_label_statistics(self, start_time: datetime = None) -> Dict:
        """
        获取标签统计信息

        Args:
            start_time: 开始时间

        Returns:
            统计字典
        """
        with self._connect() as conn:
            time_condition = ""
            params = []
            if start_time:
                time_condition = "WHERE feature_timestamp >= ?"
                params.append(start_time.isoformat())

            # 总数
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM labels {time_condition}",
                params
            ).fetchone()
            total_count = row['cnt']

            # 方向分布
            direction_dist = {'up': 0, 'flat': 0, 'down': 0}
            if total_count > 0:
                rows = conn.execute(f"""
                    SELECT direction_5m, COUNT(*) as cnt
                    FROM labels
                    {time_condition}
                    GROUP BY direction_5m
                """, params).fetchall()

                for row in rows:
                    direction = row['direction_5m']
                    if direction == 1:
                        direction_dist['up'] = row['cnt']
                    elif direction == 0:
                        direction_dist['flat'] = row['cnt']
                    elif direction == -1:
                        direction_dist['down'] = row['cnt']

            # 收益分布（分成6个区间）
            return_dist = [0, 0, 0, 0, 0, 0]  # <-5, -5~-2, -2~0, 0~2, 2~5, >5
            if total_count > 0:
                rows = conn.execute(f"""
                    SELECT return_5m FROM labels
                    {time_condition}
                """, params).fetchall()

                for row in rows:
                    ret = row['return_5m'] or 0
                    if ret < -5:
                        return_dist[0] += 1
                    elif ret < -2:
                        return_dist[1] += 1
                    elif ret < 0:
                        return_dist[2] += 1
                    elif ret < 2:
                        return_dist[3] += 1
                    elif ret < 5:
                        return_dist[4] += 1
                    else:
                        return_dist[5] += 1

            return {
                'total_count': total_count,
                'direction_distribution': direction_dist,
                'return_distribution': return_dist
            }

    def get_alerts(
        self,
        start_time: datetime = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        获取告警列表

        Args:
            start_time: 开始时间
            limit: 最大数量

        Returns:
            告警列表
        """
        with self._connect() as conn:
            query = "SELECT * FROM alerts"
            params = []

            if start_time:
                query += " WHERE timestamp >= ?"
                params.append(start_time.isoformat())

            query += f" ORDER BY timestamp DESC LIMIT {limit}"

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]

    def get_stats(self) -> Dict:
        """获取数据库统计信息"""
        with self._connect() as conn:
            stats = {}

            # 特征数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM features").fetchone()
            stats['feature_count'] = row['cnt']

            # 标签数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM labels").fetchone()
            stats['label_count'] = row['cnt']

            # 价格快照数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM price_snapshots").fetchone()
            stats['snapshot_count'] = row['cnt']

            # 告警数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM alerts").fetchone()
            stats['alert_count'] = row['cnt']

            # 被过滤告警数量
            row = conn.execute("SELECT COUNT(*) as cnt FROM alerts WHERE was_filtered = 1").fetchone()
            stats['filtered_alert_count'] = row['cnt']

            # 交易对数量
            row = conn.execute("SELECT COUNT(DISTINCT symbol) as cnt FROM features").fetchone()
            stats['symbol_count'] = row['cnt']

            # 时间范围
            row = conn.execute("""
                SELECT MIN(timestamp) as min_ts, MAX(timestamp) as max_ts
                FROM features
            """).fetchone()
            stats['time_range'] = {
                'start': row['min_ts'],
                'end': row['max_ts']
            }

            return stats

    def cleanup_old_data(self, max_age_days: int = 30):
        """清理超过指定天数的旧数据"""
        cutoff = datetime.now().isoformat()

        with self._connect() as conn:
            # 清理特征
            result = conn.execute("""
                DELETE FROM features
                WHERE datetime(timestamp) < datetime(?, '-' || ? || ' days')
            """, (cutoff, max_age_days))
            feature_deleted = result.rowcount

            # 清理标签
            result = conn.execute("""
                DELETE FROM labels
                WHERE datetime(feature_timestamp) < datetime(?, '-' || ? || ' days')
            """, (cutoff, max_age_days))
            label_deleted = result.rowcount

            # 清理价格快照
            result = conn.execute("""
                DELETE FROM price_snapshots
                WHERE datetime(timestamp) < datetime(?, '-' || ? || ' days')
            """, (cutoff, max_age_days))
            snapshot_deleted = result.rowcount

            logger.info(
                f"清理旧数据完成: 特征={feature_deleted}, 标签={label_deleted}, "
                f"快照={snapshot_deleted}"
            )

            return {
                'features_deleted': feature_deleted,
                'labels_deleted': label_deleted,
                'snapshots_deleted': snapshot_deleted
            }

    def close(self):
        """关闭数据存储（SQLite使用上下文管理器，无需显式关闭）"""
        logger.debug(f"ML数据存储已关闭: {self.db_path}")
