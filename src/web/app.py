"""
Flask Web应用

提供Web界面查看系统状态
"""
import threading
from datetime import datetime

from flask import Flask, jsonify, render_template
from loguru import logger


def create_app():
    """创建Flask应用"""
    app = Flask(
        __name__,
        template_folder='templates',
        static_folder='static'
    )

    # ==================== 页面路由 ====================

    @app.route('/')
    def index():
        """首页 - 仪表板"""
        return render_template('index.html')

    # ==================== API路由 ====================

    @app.route('/api/system/status')
    def api_system_status():
        """获取系统状态"""
        try:
            status = {
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
