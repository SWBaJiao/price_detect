"""
Flask Web应用

提供Web界面查看：
- 模拟交易记录和收益
- ML训练数据统计
- 账户状态和权益曲线
"""
import threading
from datetime import datetime, timedelta
from typing import Optional

from flask import Flask, jsonify, render_template, request
from loguru import logger


def create_app(
    trading_store=None,
    ml_data_store=None,
    virtual_account=None,
    realtime_engine=None
):
    """
    创建Flask应用

    Args:
        trading_store: 交易数据存储实例
        ml_data_store: ML数据存储实例
        virtual_account: 虚拟账户实例
        realtime_engine: 实时交易引擎实例

    Returns:
        Flask应用实例
    """
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )

    # 存储依赖
    app.trading_store = trading_store
    app.ml_data_store = ml_data_store
    app.virtual_account = virtual_account
    app.realtime_engine = realtime_engine

    # ==================== 页面路由 ====================

    @app.route('/')
    def index():
        """首页 - 仪表板"""
        return render_template('index.html')

    @app.route('/trades')
    def trades_page():
        """交易记录页面"""
        return render_template('trades.html')

    @app.route('/positions')
    def positions_page():
        """持仓页面"""
        return render_template('positions.html')

    @app.route('/features')
    def features_page():
        """特征统计页面"""
        return render_template('features.html')

    # ==================== API路由 ====================

    @app.route('/api/account')
    def api_account():
        """获取账户状态"""
        try:
            # 优先从实时账户获取
            if app.virtual_account:
                account = app.virtual_account
                return jsonify({
                    'success': True,
                    'data': {
                        'balance': account.balance,
                        'equity': account.get_equity(),
                        'margin_used': account.get_margin_used(),
                        'margin_available': account.get_available_margin(),
                        'initial_balance': account.initial_balance,
                        'leverage': account.leverage,
                        'total_pnl': account.total_pnl,
                        'total_trades': account.total_trades,
                        'win_trades': account.win_trades,
                        'win_rate': account.win_trades / account.total_trades if account.total_trades > 0 else 0,
                        'max_drawdown': account.max_drawdown,
                        'return_pct': (account.get_equity() / account.initial_balance - 1) * 100,
                        'open_positions': len(account.positions),
                    }
                })

            # 从数据库获取
            if app.trading_store:
                state = app.trading_store.get_latest_account_state()
                if state:
                    return jsonify({
                        'success': True,
                        'data': state
                    })

            return jsonify({
                'success': False,
                'error': '账户数据不可用'
            })
        except Exception as e:
            logger.error(f"获取账户状态失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/positions')
    def api_positions():
        """获取当前持仓"""
        try:
            positions = []

            # 优先从实时账户获取
            if app.virtual_account:
                for pos in app.virtual_account.positions.values():
                    positions.append(pos.to_dict())

            # 从数据库获取
            elif app.trading_store:
                db_positions = app.trading_store.get_open_positions()
                positions = db_positions

            return jsonify({
                'success': True,
                'data': positions,
                'count': len(positions)
            })
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/trades')
    def api_trades():
        """获取交易记录"""
        try:
            if not app.trading_store:
                return jsonify({
                    'success': False,
                    'error': '交易数据存储不可用'
                })

            # 解析参数
            symbol = request.args.get('symbol')
            days = int(request.args.get('days', 7))
            limit = int(request.args.get('limit', 100))

            start_time = datetime.now() - timedelta(days=days)

            trades = app.trading_store.get_trades(
                symbol=symbol,
                start_time=start_time,
                limit=limit
            )

            return jsonify({
                'success': True,
                'data': [t.to_dict() for t in trades],
                'count': len(trades)
            })
        except Exception as e:
            logger.error(f"获取交易记录失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/trades/statistics')
    def api_trade_statistics():
        """获取交易统计"""
        try:
            if not app.trading_store:
                return jsonify({
                    'success': False,
                    'error': '交易数据存储不可用'
                })

            symbol = request.args.get('symbol')
            days = int(request.args.get('days', 30))

            start_time = datetime.now() - timedelta(days=days)

            stats = app.trading_store.get_trade_statistics(
                symbol=symbol,
                start_time=start_time
            )

            return jsonify({
                'success': True,
                'data': stats
            })
        except Exception as e:
            logger.error(f"获取交易统计失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/equity-curve')
    def api_equity_curve():
        """获取权益曲线"""
        try:
            if not app.trading_store:
                return jsonify({
                    'success': False,
                    'error': '交易数据存储不可用'
                })

            days = int(request.args.get('days', 7))
            start_time = datetime.now() - timedelta(days=days)

            curve = app.trading_store.get_equity_curve(
                symbol="ALL",
                start_time=start_time
            )

            return jsonify({
                'success': True,
                'data': curve
            })
        except Exception as e:
            logger.error(f"获取权益曲线失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/features/statistics')
    def api_feature_statistics():
        """获取特征统计"""
        try:
            if not app.ml_data_store:
                return jsonify({
                    'success': False,
                    'error': 'ML数据存储不可用'
                })

            days = int(request.args.get('days', 1))
            start_time = datetime.now() - timedelta(days=days)

            # 获取特征统计
            stats = app.ml_data_store.get_feature_statistics(start_time)

            return jsonify({
                'success': True,
                'data': stats
            })
        except Exception as e:
            logger.error(f"获取特征统计失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/labels/statistics')
    def api_label_statistics():
        """获取标签统计"""
        try:
            if not app.ml_data_store:
                return jsonify({
                    'success': False,
                    'error': 'ML数据存储不可用'
                })

            days = int(request.args.get('days', 1))
            start_time = datetime.now() - timedelta(days=days)

            # 获取标签统计
            stats = app.ml_data_store.get_label_statistics(start_time)

            return jsonify({
                'success': True,
                'data': stats
            })
        except Exception as e:
            logger.error(f"获取标签统计失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/alerts')
    def api_alerts():
        """获取告警记录"""
        try:
            if not app.ml_data_store:
                return jsonify({
                    'success': False,
                    'error': 'ML数据存储不可用'
                })

            days = int(request.args.get('days', 1))
            limit = int(request.args.get('limit', 100))
            start_time = datetime.now() - timedelta(days=days)

            alerts = app.ml_data_store.get_alerts(
                start_time=start_time,
                limit=limit
            )

            return jsonify({
                'success': True,
                'data': alerts,
                'count': len(alerts)
            })
        except Exception as e:
            logger.error(f"获取告警记录失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    @app.route('/api/system/status')
    def api_system_status():
        """获取系统状态"""
        try:
            status = {
                'trading_enabled': app.realtime_engine is not None,
                'trading_running': app.realtime_engine._running if app.realtime_engine else False,
                'ml_enabled': app.ml_data_store is not None,
                'timestamp': datetime.now().isoformat()
            }

            return jsonify({
                'success': True,
                'data': status
            })
        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            return jsonify({
                'success': False,
                'error': str(e)
            })

    return app


def run_web_server(
    app: Flask,
    host: str = '0.0.0.0',
    port: int = 5000,
    debug: bool = False,
    threaded: bool = True
):
    """
    运行Web服务器

    Args:
        app: Flask应用实例
        host: 主机地址
        port: 端口
        debug: 调试模式
        threaded: 多线程模式
    """
    logger.info(f"启动Web服务器: http://{host}:{port}")

    # 在独立线程中运行
    def run():
        app.run(
            host=host,
            port=port,
            debug=debug,
            threaded=threaded,
            use_reloader=False  # 禁用重载器避免双重启动
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    return thread
