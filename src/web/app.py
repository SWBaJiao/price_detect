"""
Flask Web应用

提供Web界面：系统状态、账户监控、跟单配置
"""
import threading
from datetime import datetime
from typing import Optional

from flask import Flask, jsonify, render_template, request
from loguru import logger

from ..account_monitor_store import AccountMonitorStore
from ..binance_account_client import BinanceAccountClient


def create_app(account_store: Optional[AccountMonitorStore] = None):
    """创建Flask应用。可注入 account_store，不传则使用默认路径创建。"""
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )
    app.account_store = account_store or AccountMonitorStore()

    # ==================== 页面路由 ====================

    @app.route('/')
    def index():
        """首页 - 仪表板"""
        return render_template('index.html')

    @app.route('/account-monitor')
    def account_monitor_page():
        """账户监控页面"""
        return render_template('account_monitor.html')

    @app.route('/copy-trading')
    def copy_trading_page():
        """跟单配置页面"""
        return render_template('copy_trading.html')

    # ==================== API路由 ====================

    @app.route('/api/system/status')
    def api_system_status():
        """获取系统状态"""
        try:
            status = {'timestamp': datetime.now().isoformat()}
            return jsonify({'success': True, 'data': status})
        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    # ---------- 账户监控 API ----------
    @app.route('/api/monitored-accounts', methods=['GET'])
    def api_list_monitored_accounts():
        try:
            accounts = app.account_store.list_monitored_accounts()
            out = []
            for a in accounts:
                out.append({
                    'id': a.id,
                    'name': a.name,
                    'enabled': a.enabled,
                    'created_at': a.created_at,
                })
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            logger.error(f"列出监控账户失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/monitored-accounts', methods=['POST'])
    def api_add_monitored_account():
        try:
            data = request.get_json() or {}
            name = (data.get('name') or '').strip()
            api_key = (data.get('api_key') or '').strip()
            api_secret = (data.get('api_secret') or '').strip()
            if not name or not api_key or not api_secret:
                return jsonify({'success': False, 'error': '请填写名称、API Key 和 API Secret'})
            aid = app.account_store.add_monitored_account(name, api_key, api_secret)
            return jsonify({'success': True, 'data': {'id': aid}})
        except Exception as e:
            logger.error(f"添加监控账户失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/monitored-accounts/<int:aid>/positions', methods=['GET'])
    def api_get_account_positions(aid):
        import asyncio
        try:
            acc = app.account_store.get_monitored_account(aid)
            if not acc:
                return jsonify({'success': False, 'error': '账户不存在'})
            async def fetch():
                client = BinanceAccountClient(acc.api_key, acc.api_secret)
                try:
                    return await client.get_position_risk()
                finally:
                    await client.close()
            positions = asyncio.run(fetch())
            out = [{'symbol': p.symbol, 'position_side': p.position_side, 'position_amt': p.position_amt, 'entry_price': p.entry_price, 'mark_price': p.mark_price, 'unrealized_profit': p.unrealized_profit, 'leverage': p.leverage} for p in positions]
            return jsonify({'success': True, 'data': out, 'account_name': acc.name})
        except Exception as e:
            logger.error(f"获取账户持仓失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/monitored-accounts/<int:aid>/enabled', methods=['PUT'])
    def api_set_monitored_enabled(aid):
        try:
            data = request.get_json() or {}
            enabled = bool(data.get('enabled', True))
            ok = app.account_store.set_monitored_account_enabled(aid, enabled)
            return jsonify({'success': ok})
        except Exception as e:
            logger.error(f"设置监控账户启用状态失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/monitored-accounts/<int:aid>/events', methods=['GET'])
    def api_get_account_events(aid):
        try:
            acc = app.account_store.get_monitored_account(aid)
            if not acc:
                return jsonify({'success': False, 'error': '账户不存在'})
            limit = request.args.get('limit', 200, type=int)
            events = app.account_store.get_position_events(aid, limit=limit)
            return jsonify({'success': True, 'data': events, 'account_name': acc.name})
        except Exception as e:
            logger.error(f"获取账户开平仓记录失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/monitored-accounts/<int:aid>', methods=['DELETE'])
    def api_delete_monitored_account(aid):
        try:
            ok = app.account_store.delete_monitored_account(aid)
            return jsonify({'success': ok})
        except Exception as e:
            logger.error(f"删除监控账户失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    # ---------- 跟单配置 API ----------
    @app.route('/api/copy-configs', methods=['GET'])
    def api_list_copy_configs():
        try:
            configs = app.account_store.list_copy_configs()
            accounts = {a.id: a.name for a in app.account_store.list_monitored_accounts()}
            out = []
            for c in configs:
                out.append({
                    'id': c.id,
                    'name': c.name,
                    'source_account_id': c.source_account_id,
                    'source_account_name': accounts.get(c.source_account_id, ''),
                    'enabled': c.enabled,
                    'leverage_scale': c.leverage_scale,
                    'copy_mode': getattr(c, 'copy_mode', 'amount_scale') or 'amount_scale',
                    'copy_ratio': float(getattr(c, 'copy_ratio', 1.0) or 1.0),
                    'leverage_mode': getattr(c, 'leverage_mode', 'same') or 'same',
                    'custom_leverage': int(getattr(c, 'custom_leverage', 20) or 20),
                    'is_simulation': getattr(c, 'is_simulation', False),
                    'sim_balance': float(getattr(c, 'sim_balance', 10000) or 10000),
                    'max_slippage': float(getattr(c, 'max_slippage', 0) or 0),
                    'copy_rule': getattr(c, 'copy_rule', 'sync') or 'sync',
                    'created_at': c.created_at,
                })
            return jsonify({'success': True, 'data': out})
        except Exception as e:
            logger.error(f"列出跟单配置失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/copy-configs', methods=['POST'])
    def api_add_copy_config():
        try:
            data = request.get_json() or {}
            name = (data.get('name') or '').strip()
            follower_api_key = (data.get('follower_api_key') or '').strip()
            follower_api_secret = (data.get('follower_api_secret') or '').strip()
            source_account_id = data.get('source_account_id')
            leverage_scale = float(data.get('leverage_scale', 1.0))
            copy_mode = (data.get('copy_mode') or 'amount_scale').strip() or 'amount_scale'
            copy_ratio = float(data.get('copy_ratio', 1.0))
            leverage_mode = (data.get('leverage_mode') or 'same').strip() or 'same'
            custom_leverage = int(data.get('custom_leverage', 20))
            is_simulation = bool(data.get('is_simulation', False))
            sim_balance = float(data.get('sim_balance', 10000) or 10000)
            max_slippage = float(data.get('max_slippage', 0)) / 100.0  # 前端传百分比，转为小数
            copy_rule = (data.get('copy_rule') or 'sync').strip() or 'sync'
            if not name or source_account_id is None:
                return jsonify({'success': False, 'error': '请填写名称并选择监控账户'})
            if not is_simulation and (not follower_api_key or not follower_api_secret):
                return jsonify({'success': False, 'error': '非模拟模式请填写跟单 API Key/Secret'})
            cid = app.account_store.add_copy_config(
                name, follower_api_key, follower_api_secret, int(source_account_id),
                leverage_scale=leverage_scale, copy_mode=copy_mode, copy_ratio=copy_ratio,
                leverage_mode=leverage_mode, custom_leverage=custom_leverage,
                is_simulation=is_simulation, sim_balance=sim_balance,
                max_slippage=max_slippage, copy_rule=copy_rule,
            )
            return jsonify({'success': True, 'data': {'id': cid}})
        except Exception as e:
            logger.error(f"添加跟单配置失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/copy-configs/<int:cid>/enabled', methods=['PUT'])
    def api_set_copy_enabled(cid):
        try:
            data = request.get_json() or {}
            enabled = bool(data.get('enabled', False))
            ok = app.account_store.set_copy_config_enabled(cid, enabled)
            return jsonify({'success': ok})
        except Exception as e:
            logger.error(f"设置跟单启用状态失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/copy-configs/<int:cid>', methods=['DELETE'])
    def api_delete_copy_config(cid):
        try:
            ok = app.account_store.delete_copy_config(cid)
            return jsonify({'success': ok})
        except Exception as e:
            logger.error(f"删除跟单配置失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/copy-configs/<int:cid>', methods=['PUT'])
    def api_update_copy_config(cid):
        try:
            data = request.get_json() or {}
            updates = {}
            for key in ['name', 'source_account_id', 'copy_mode', 'leverage_mode']:
                if key in data and data[key] is not None:
                    updates[key] = data[key]
            for key in ['leverage_scale', 'copy_ratio']:
                if key in data and data[key] is not None:
                    updates[key] = float(data[key])
            if 'custom_leverage' in data and data['custom_leverage'] is not None:
                updates['custom_leverage'] = int(data['custom_leverage'])
            if 'is_simulation' in data:
                updates['is_simulation'] = bool(data['is_simulation'])
            if 'sim_balance' in data and data['sim_balance'] is not None:
                updates['sim_balance'] = float(data['sim_balance'])
            if 'max_slippage' in data and data['max_slippage'] is not None:
                updates['max_slippage'] = float(data['max_slippage']) / 100.0
            if 'copy_rule' in data and data['copy_rule']:
                updates['copy_rule'] = data['copy_rule']
            if 'follower_api_key' in data and data['follower_api_key']:
                updates['follower_api_key'] = data['follower_api_key']
            if 'follower_api_secret' in data and data['follower_api_secret']:
                updates['follower_api_secret'] = data['follower_api_secret']
            ok = app.account_store.update_copy_config(cid, **updates)
            return jsonify({'success': ok})
        except Exception as e:
            logger.error(f"更新跟单配置失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    # ---------- 模拟交易记录 API ----------
    @app.route('/api/simulation-trades/<int:config_id>', methods=['GET'])
    def api_simulation_trades(config_id):
        try:
            limit = request.args.get('limit', 100, type=int)
            trades = app.account_store.get_simulation_trades(config_id, limit=limit)
            positions = app.account_store.get_simulation_positions(config_id)
            total_pnl = sum(t.get('pnl', 0) for t in trades)
            return jsonify({
                'success': True,
                'data': {
                    'trades': trades,
                    'positions': positions,
                    'total_pnl': round(total_pnl, 4),
                },
            })
        except Exception as e:
            logger.error(f"获取模拟交易记录失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    @app.route('/api/copy-trades/<int:config_id>', methods=['GET'])
    def api_copy_trades(config_id):
        """获取真实跟单交易记录"""
        try:
            limit = request.args.get('limit', 100, type=int)
            trades = app.account_store.get_copy_trades(config_id, limit=limit)
            return jsonify({
                'success': True,
                'data': {
                    'trades': trades,
                    'total_trades': len(trades),
                },
            })
        except Exception as e:
            logger.error(f"获取跟单交易记录失败: {e}")
            return jsonify({'success': False, 'error': str(e)})

    return app


def run_web_server(
    app: Flask,
    host: str = '0.0.0.0',
    port: int = 5000,
    debug: bool = False,
    threaded: bool = True
):
    """
    运行Web服务器（在子线程中启动，避免 app.run() 在主线程外注册信号导致崩溃）
    """
    if app is None:
        raise ValueError("run_web_server: app 不能为 None")
    logger.info(f"启动Web服务器: http://{host}:{port}")

    # 通过参数传入 app，避免子线程中闭包拿到 None
    def run(app_instance, h, p, th):
        from werkzeug.serving import make_server
        server = make_server(h, p, app_instance, threaded=th)
        server.serve_forever()

    thread = threading.Thread(target=run, args=(app, host, port, threaded), daemon=True)
    thread.start()

    return thread
