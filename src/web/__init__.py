"""
Web仪表板模块

提供Web界面查看：
- 模拟交易记录和收益
- ML训练数据统计
- 账户状态和权益曲线
"""
from .app import create_app, run_web_server

__all__ = ['create_app', 'run_web_server']
