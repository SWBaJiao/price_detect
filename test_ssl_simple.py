#!/usr/bin/env python3
"""
简化的 SSL 连接测试
不依赖第三方库，仅使用标准库
"""
import asyncio
import ssl
import socket
import sys


def test_socket_connection():
    """测试基础 TCP 连接"""
    print("\n[测试 1] TCP 连接测试...")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex(('fapi.binance.com', 443))
        sock.close()

        if result == 0:
            print("✓ TCP 连接成功 (端口 443 可达)")
            return True
        else:
            print(f"✗ TCP 连接失败 (错误代码: {result})")
            return False
    except Exception as e:
        print(f"✗ TCP 连接异常: {e}")
        return False


def test_ssl_connection_basic():
    """测试基础 SSL 连接（宽松模式）"""
    print("\n[测试 2] SSL 连接测试 (宽松模式)...")
    try:
        # 创建宽松的 SSL 上下文
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        with socket.create_connection(('fapi.binance.com', 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname='fapi.binance.com') as ssock:
                print(f"✓ SSL 连接成功")
                print(f"  - SSL 版本: {ssock.version()}")
                print(f"  - 加密套件: {ssock.cipher()[0]}")
                return True
    except Exception as e:
        print(f"✗ SSL 连接失败: {e}")
        return False


def test_ssl_connection_strict():
    """测试严格 SSL 连接（证书验证）"""
    print("\n[测试 3] SSL 连接测试 (严格模式 - 证书验证)...")
    try:
        # 创建严格的 SSL 上下文
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        with socket.create_connection(('fapi.binance.com', 443), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname='fapi.binance.com') as ssock:
                cert = ssock.getpeercert()
                print(f"✓ SSL 证书验证成功")
                print(f"  - 证书主题: {dict(x[0] for x in cert['subject'])}")
                print(f"  - 证书颁发者: {dict(x[0] for x in cert['issuer'])}")
                return True
    except ssl.SSLCertVerificationError as e:
        print(f"✗ SSL 证书验证失败: {e}")
        print("  建议: 可能需要更新系统 CA 证书")
        return False
    except Exception as e:
        print(f"✗ SSL 连接失败: {e}")
        return False


def test_http_request():
    """测试简单的 HTTP 请求"""
    print("\n[测试 4] HTTPS 请求测试...")
    try:
        import urllib.request
        import urllib.error
        import json

        # 创建宽松的 SSL 上下文
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2

        url = "https://fapi.binance.com/fapi/v1/ping"
        req = urllib.request.Request(url)

        with urllib.request.urlopen(req, context=context, timeout=10) as response:
            if response.status == 200:
                print(f"✓ HTTPS 请求成功")
                print(f"  - 状态码: {response.status}")
                return True
    except urllib.error.URLError as e:
        print(f"✗ HTTPS 请求失败: {e}")
        return False
    except Exception as e:
        print(f"✗ HTTPS 请求异常: {e}")
        return False


def diagnose_system():
    """诊断系统环境"""
    print("\n" + "=" * 60)
    print("系统环境诊断")
    print("=" * 60)

    import platform
    print(f"操作系统: {platform.system()} {platform.release()}")
    print(f"Python 版本: {platform.python_version()}")
    print(f"OpenSSL 版本: {ssl.OPENSSL_VERSION}")

    # 检查系统 CA 证书
    try:
        context = ssl.create_default_context()
        print(f"默认 CA 证书路径: {context.ca_certs or '(使用系统默认)'}")
    except Exception as e:
        print(f"获取 CA 证书信息失败: {e}")


def main():
    print("=" * 60)
    print("Binance API SSL 连接诊断工具")
    print("=" * 60)

    # 系统诊断
    diagnose_system()

    # 运行测试
    results = []
    results.append(("TCP 连接", test_socket_connection()))
    results.append(("SSL 宽松模式", test_ssl_connection_basic()))
    results.append(("SSL 严格模式", test_ssl_connection_strict()))
    results.append(("HTTPS 请求", test_http_request()))

    # 总结
    print("\n" + "=" * 60)
    print("测试结果总结")
    print("=" * 60)

    for name, success in results:
        status = "✓ 通过" if success else "✗ 失败"
        print(f"{name}: {status}")

    print("\n" + "=" * 60)

    # 根据结果给出建议
    tcp_ok, ssl_loose_ok, ssl_strict_ok, https_ok = [r[1] for r in results]

    if not tcp_ok:
        print("\n问题诊断: TCP 连接失败")
        print("建议:")
        print("  1. 检查防火墙设置")
        print("  2. 检查网络连接")
        print("  3. 确认 DNS 解析正常 (ping fapi.binance.com)")
    elif not ssl_loose_ok:
        print("\n问题诊断: SSL 连接失败 (即使在宽松模式下)")
        print("建议:")
        print("  1. 检查是否有代理或防火墙拦截 SSL 连接")
        print("  2. 更新 Python OpenSSL 库")
        print("  3. 检查系统时间是否正确")
    elif not ssl_strict_ok:
        print("\n问题诊断: SSL 证书验证失败")
        print("建议:")
        print("  1. 更新系统 CA 证书:")
        print("     macOS: brew install ca-certificates")
        print("     或者运行: /Applications/Python\\ 3.x/Install\\ Certificates.command")
        print("  2. 临时方案: 在代码中使用宽松的 SSL 验证 (不推荐生产环境)")
    elif not https_ok:
        print("\n问题诊断: HTTPS 请求失败")
        print("建议: 检查网络稳定性和超时设置")
    else:
        print("\n✓ 所有测试通过！SSL 连接正常")
        print("\n如果 aiohttp 仍然报错，可能是:")
        print("  1. aiohttp 的 SSL 上下文配置问题")
        print("  2. 需要设置 connector 参数")
        print("  3. 已在代码中优化，请重新测试应用")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n测试被中断")
        sys.exit(0)
