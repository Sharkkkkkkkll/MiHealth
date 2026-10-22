# -*- coding: utf-8 -*-
"""MiHealth 睡眠 Agent · 一键启动脚本（与 start_server.py 同款体验）。

用法：
    1. 用 PyCharm 打开 D:\\MIwatchAI\\Agent（或整个 MIwatchAI 项目）。
    2. 解释器选 Python 3.14（与仪表盘同一个 python.exe——
       防火墙当初是按这个程序放行的，换别的 python 起服务手机会连不上！）。
    3. 右键本文件 -> Run 'start_agent'。
    4. 手机 App 只需要填仪表盘地址(http://IP:8000)，Agent 端口 8001 自动跟随。

换 WiFi/网络后：电脑 IP 会变，只需在手机 App 设置里改仪表盘地址，
Agent 地址（同 IP:8001）会自动跟着变，不用单独填。
"""
import os
import runpy
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "agent_server.py")
PORT = 8001


def get_lan_ip() -> str:
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    return ip


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        s.close()
        return False
    except OSError:
        return True


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    lan_ip = get_lan_ip()
    print("=" * 54)
    print("  MiHealth 睡眠 Agent（大脑）")
    print("=" * 54)
    if port_in_use(PORT):
        print(f"  Agent 已在运行中！ http://{lan_ip}:{PORT}")
        print("  如需重启，先关闭已运行的 Agent 窗口/进程。")
    else:
        print(f"  本机访问: http://127.0.0.1:{PORT}")
        print(f"  手机访问: http://{lan_ip}:{PORT}")
        print("  （手机 App 里只填仪表盘 :8000 地址，:8001 自动跟随）")
        print("  停止：控制台停止按钮 / Ctrl+C")
        print("=" * 54)
        runpy.run_path(SERVER, run_name="__main__")
        return
    print("=" * 54)


if __name__ == "__main__":
    main()
