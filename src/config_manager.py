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
