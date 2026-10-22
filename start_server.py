# -*- coding: utf-8 -*-
"""
小米手环健康仪表盘 · 一键启动脚本

在 PyCharm 里直接 Run 本文件即可启动本地服务（复用 app/server.py）。

用法：
    1. 用 PyCharm 打开 D:\\MIwatchAI 项目，选好 Python 3.x 解释器。
    2. 右键本文件 -> Run 'start_server'。
    3. 脚本会自动打印「电脑访问」和「手机访问」两个地址。
    4. 手机浏览器 / App 填 http://<手机访问IP>:8000 即可看数据。

停止：PyCharm 控制台点红色停止按钮，或命令行 Ctrl+C。
"""
import os
import runpy
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "app", "server.py")
PORT = 8000


def _force_utf8() -> None:
    """强制控制台 UTF-8 输出，避免 Windows cmd 下中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def get_lan_ip() -> str:
    """获取本机局域网 IPv4（用于手机访问）。失败则回退 127.0.0.1。"""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # UDP connect 不发包，仅取路由出口 IP
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    return ip


def port_in_use(port: int) -> bool:
    """检测端口是否已被占用（用于提示服务已在运行）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return False
    except OSError:
        return True


def main() -> None:
    _force_utf8()
    if not os.path.exists(SERVER):
        print(f"[错误] 找不到服务文件: {SERVER}")
        sys.exit(1)

    lan_ip = get_lan_ip()
    print("=" * 54)
    print("  小米手环健康仪表盘")
    print("=" * 54)

    if port_in_use(PORT):
        print("  服务已在运行中！")
        print(f"  电脑访问: http://127.0.0.1:{PORT}")
        print(f"  手机访问: http://{lan_ip}:{PORT}")
        print("  如需重启，请先关闭已运行的服务进程。")
        print("=" * 54)
        return

    print(f"  电脑访问: http://127.0.0.1:{PORT}")
    print(f"  手机访问: http://{lan_ip}:{PORT}")
    print("  （手机需与电脑连同一 WiFi）")
    print("  停止：控制台停止按钮 / Ctrl+C")
    print("=" * 54)
    # 在当前进程内运行 app/server.py，输出与控制台停止均正常
    runpy.run_path(SERVER, run_name="__main__")


if __name__ == "__main__":
    main()
