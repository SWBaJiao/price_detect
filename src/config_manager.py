"""
配置管理模块
加载并校验 YAML 配置和环境变量
"""
import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


class TelegramConfig(BaseModel):
    """Telegram 配置"""
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""


class PriceChangeAlert(BaseModel):
    """价格异动告警配置"""
    enabled: bool = True
    time_window: int = 60  # 秒


class VolumeSpikeAlert(BaseModel):
    """成交量突增告警配置"""
    enabled: bool = True
    lookback_periods: int = 10  # 对比最近N个周期的平均值


class OpenInterestAlert(BaseModel):
    """持仓量变化告警配置"""
    enabled: bool = True
    poll_interval: int = 30  # 轮询间隔（秒）
    time_window: int = 300   # 检测窗口（秒）


class SpotFuturesSpreadAlert(BaseModel):
    """现货-合约价差告警配置"""
    enabled: bool = True
    threshold: float = 0.3    # 价差阈值（%）
    time_window: int = 60     # 检测窗口（秒）
    poll_interval: int = 30   # 现货价格轮询间隔（秒）


class PriceReversalAlert(BaseModel):
    """价格反转告警配置"""
    enabled: bool = True
    time_window: int = 300    # 检测窗口（秒），默认5分钟


class OrderBookAlert(BaseModel):
    """订单簿异动告警配置"""
    enabled: bool = True
    # 监控的交易对
    symbols: List[str] = []
    # 大单墙检测
    wall_detection: bool = True
    wall_value_threshold: float = 500000    # 大单墙最小价值阈值(USDT)
    wall_ratio_threshold: float = 3.0       # 单档挂单量 > 平均值 N 倍
    wall_distance_max: float = 2.0          # 距离当前价格最大百分比
    # 深度失衡检测
    imbalance_detection: bool = True
    imbalance_threshold: float = 0.6        # 失衡比率阈值 (0-1)
    imbalance_depth_levels: int = 10        # 计算失衡的档位数
    # 扫盘检测
    sweep_detection: bool = True
    sweep_value_threshold: float = 300000   # 被吃掉的最小价值(USDT)
    # WebSocket 配置
    update_speed: str = "500ms"             # "100ms" 或 "500ms"
    depth_levels: int = 20                  # 订阅档位数 5/10/20


class AlertsConfig(BaseModel):
    """告警配置"""
    price_change: PriceChangeAlert = Field(default_factory=PriceChangeAlert)
    volume_spike: VolumeSpikeAlert = Field(default_factory=VolumeSpikeAlert)
    open_interest: OpenInterestAlert = Field(default_factory=OpenInterestAlert)
    spot_futures_spread: SpotFuturesSpreadAlert = Field(default_factory=SpotFuturesSpreadAlert)
    price_reversal: PriceReversalAlert = Field(default_factory=PriceReversalAlert)
    orderbook: OrderBookAlert = Field(default_factory=OrderBookAlert)
    # 告警冷却时间（秒）
    cooldown: int = 300


class VolumeTierConfig(BaseModel):
    """成交额分层阈值配置"""
    min_quote_volume: float        # 24h 成交额下限(USDT)
    price_threshold: float         # 价格变化阈值(%)
    volume_threshold: float = 3.0  # 成交量倍数阈值
    oi_threshold: float = 5.0      # 持仓量变化阈值(%)
    spread_threshold: float = 0.3  # 现货-合约价差阈值(%)
    label: str                     # 层级标签


class FilterConfig(BaseModel):
    """币种过滤配置"""
    mode: str = "all"              # all / whitelist / blacklist
    whitelist: List[str] = []
    blacklist: List[str] = []


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = "INFO"
    file_output: bool = True
    file_path: str = "logs/monitor.log"


# ==================== ML量化配置 ====================

class MLFeatureConfig(BaseModel):
    """ML特征计算配置"""
    windows: List[int] = [60, 300, 900, 1800]  # 特征时间窗口(秒)
    save_interval: int = 1                      # 特征保存间隔(秒)


class MLLabelConfig(BaseModel):
    """ML标签生成配置"""
    windows: List[int] = [60, 300, 900, 1800]  # 标签时间窗口(秒)
    direction_threshold: float = 0.1            # 方向标签阈值(%)


class MLIndicatorConfig(BaseModel):
    """技术指标配置"""
    ma_periods: List[int] = [5, 20, 60]        # 移动平均周期
    rsi_period: int = 14                        # RSI周期
    macd_fast: int = 12                         # MACD快线周期
    macd_slow: int = 26                         # MACD慢线周期
    macd_signal: int = 9                        # MACD信号线周期
    bb_period: int = 20                         # 布林带周期
    bb_std: float = 2.0                         # 布林带标准差倍数


class MLRiskConfig(BaseModel):
    """ML风险过滤配置"""
    enabled: bool = True                        # 是否启用风险过滤
    filter_alerts: bool = True                  # 假异动时是否过滤告警
    max_ws_latency_ms: float = 500              # 最大WebSocket延迟(ms)
    max_spread_bps: float = 50                  # 最大价差(基点)
    min_depth_value: float = 50000              # 最小深度价值(USDT)
    fake_signal_window: int = 30                # 假异动检测窗口(秒)
    fake_signal_revert_ratio: float = 0.8       # 反转比例阈值
    fake_signal_min_change: float = 1.0         # 最小变化幅度(%)


class MLConfig(BaseModel):
    """ML量化模块总配置"""
    enabled: bool = False                       # 是否启用ML模块
    db_path: str = "data/ml_data.db"           # SQLite数据库路径
    feature: MLFeatureConfig = Field(default_factory=MLFeatureConfig)
    label: MLLabelConfig = Field(default_factory=MLLabelConfig)
    indicators: MLIndicatorConfig = Field(default_factory=MLIndicatorConfig)
    risk: MLRiskConfig = Field(default_factory=MLRiskConfig)


# ==================== 模拟交易配置 ====================

class TradingAccountConfig(BaseModel):
    """交易账户配置"""
    initial_balance: float = 10000.0            # 初始资金(USDT)
    leverage: int = 15                          # 固定杠杆倍数
    maker_fee: float = 0.0002                   # 挂单手续费(0.02%)
    taker_fee: float = 0.0005                   # 吃单手续费(0.05%)
    max_positions: int = 5                      # 最大同时持仓数
    position_risk_pct: float = 2.0              # 单笔风险占权益百分比


class TradingStrategyConfig(BaseModel):
    """交易策略配置"""
    min_confidence: float = 0.5                 # 最小信号置信度
    signal_threshold: float = 0.4               # 信号分数阈值
    use_ml_model: bool = False                  # 是否使用ML模型（暂用规则）
    indicator_filter: bool = True               # 启用技术指标过滤
    # RSI阈值
    rsi_oversold: float = 30                    # RSI超卖阈值
    rsi_overbought: float = 70                  # RSI超买阈值
    # 波动率和成交量过滤
    min_volatility: float = 0.3                 # 最小波动率(%)
    min_volume_ratio: float = 0.5               # 最小成交量倍数
    # 订单簿失衡阈值
    imbalance_long_threshold: float = 0.65      # 买盘强阈值
    imbalance_short_threshold: float = 0.35     # 卖盘强阈值
    # 趋势过滤
    trend_filter_pct: float = 1.0               # 趋势一致性过滤阈值(%)


class TradingStopLossConfig(BaseModel):
    """止损配置"""
    method: str = "multiple"                    # "fixed" | "atr" | "trailing" | "multiple"
    fixed_stop_pct: float = 1.5                 # 固定止损(%)
    take_profit_pct: float = 3.0                # 固定止盈(%)
    atr_multiplier: float = 2.0                 # ATR止损倍数
    atr_period: int = 14                        # ATR计算周期
    trailing_distance: float = 1.0              # 移动止损距离(%)
    trailing_activation: float = 1.0            # 激活移动止损的盈利阈值(%)
    max_hold_seconds: int = 900                 # 最大持仓时间(秒)，默认15分钟


class TradingBacktestConfig(BaseModel):
    """回测配置"""
    start_date: str = "2024-01-01"              # 回测开始日期
    end_date: str = "2024-12-31"                # 回测结束日期
    symbols: List[str] = []                     # 回测币种列表，空为全部
    save_trades: bool = True                    # 是否保存交易记录到数据库


class TradingRealtimeConfig(BaseModel):
    """实时模拟配置"""
    enabled: bool = True                        # 是否启用实时模拟
    save_interval: int = 60                     # 账户状态保存间隔(秒)
    log_trades: bool = True                     # 是否记录交易日志
    max_positions_per_symbol: int = 1           # 每个交易对最大持仓数
    allowed_symbols: List[str] = []             # 允许交易的币种，空为全部


class TradingConfig(BaseModel):
    """模拟交易模块总配置"""
    enabled: bool = False                       # 是否启用交易模块
    mode: str = "realtime"                      # "backtest" | "realtime" | "both"
    account: TradingAccountConfig = Field(default_factory=TradingAccountConfig)
    strategy: TradingStrategyConfig = Field(default_factory=TradingStrategyConfig)
    stop_loss: TradingStopLossConfig = Field(default_factory=TradingStopLossConfig)
    backtest: TradingBacktestConfig = Field(default_factory=TradingBacktestConfig)
    realtime: TradingRealtimeConfig = Field(default_factory=TradingRealtimeConfig)


class Settings(BaseSettings):
    """
    主配置类
    优先级: 环境变量 > .env 文件 > YAML 配置 > 默认值
    """
    # Telegram（从环境变量读取）
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # 以下从 YAML 加载
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    volume_tiers: List[VolumeTierConfig] = []
    filter: FilterConfig = Field(default_factory=FilterConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ml: MLConfig = Field(default_factory=MLConfig)  # ML量化模块配置
    trading: TradingConfig = Field(default_factory=TradingConfig)  # 模拟交易模块配置

    class Config:
        env_file = ".env"
        extra = "ignore"


def load_yaml_config(config_path: Optional[str] = None) -> dict:
    """加载 YAML 配置文件"""
    if config_path is None:
        # 默认路径
        config_path = Path(__file__).parent.parent / "config" / "settings.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        return {}

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_settings(config_path: Optional[str] = None) -> Settings:
    """
    获取配置实例
    合并环境变量和 YAML 配置
    """
    yaml_config = load_yaml_config(config_path)

    # 环境变量优先
    settings = Settings(**yaml_config)

    # 将环境变量的 token 合并到 telegram 配置
    if settings.telegram_bot_token:
        settings.telegram.bot_token = settings.telegram_bot_token
    if settings.telegram_chat_id:
        settings.telegram.chat_id = settings.telegram_chat_id

    return settings


# 全局配置实例
_settings: Optional[Settings] = None


def get_config() -> Settings:
    """获取全局配置单例"""
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def reload_config(config_path: Optional[str] = None) -> Settings:
    """重新加载配置"""
    global _settings
    _settings = get_settings(config_path)
    return _settings
