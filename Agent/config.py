# -*- coding: utf-8 -*-
"""全局配置：读取 .env（零依赖简易解析，不引入 python-dotenv）。"""
from __future__ import annotations

import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "memory_data")          # 三层记忆 JSON 落盘处
EXPORT_DIR = r"D:\MIwatchAI\JSON"                     # pull_to_json.py 的产物目录


def data_dir_for(user_id: str = "") -> str:
    """按 user_id 返回记忆子目录；user_id 空 → 根 memory_data（兼容单用户旧数据）。

    多用户（方案 A）：每个用户独立 memory_data/<user_id>/ 子目录，
    preferences/history/feedback/tolerance/session_state 等全部按用户隔离。
    """
    uid = (user_id or "").strip()
    if not uid:
        return DATA_DIR
    return os.path.join(DATA_DIR, uid)


def export_dir_for(user_id: str = "") -> str:
    """按 user_id 返回数据源 JSON 子目录；user_id 空 → 根 JSON（兼容单用户旧数据）。"""
    uid = (user_id or "").strip()
    if not uid:
        return EXPORT_DIR
    return os.path.join(EXPORT_DIR, uid)


def list_users() -> list:
    """列出所有已注册用户（memory_data 下的子目录名）。

    返回 [""] 表示只有单用户（根目录）；多用户返回 ["", "uid1", "uid2", ...]，
    空字符串代表"根目录用户"（兼容旧数据，未扫码登录的设备）。
    """
    out = [""]
    if os.path.isdir(DATA_DIR):
        for name in sorted(os.listdir(DATA_DIR)):
            if os.path.isdir(os.path.join(DATA_DIR, name)):
                out.append(name)
    return out


# 复制粘贴常混入不可见字符（零宽空格/软连字符 U+00AD 等），HTTP 头要求 latin-1，必须清洗
_INVISIBLE = re.compile(
    "[\\u00ad\\u200b-\\u200f\\u2028\\u2029\\u2060-\\u2064\\ufeff]"
)


def _load_env() -> dict:
    """解析同目录 .env：KEY=VALUE，支持 # 注释；不覆盖已存在的环境变量。"""
    out = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = _INVISIBLE.sub("", v.strip())
    return out


_ENV = _load_env()


def get(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key) or default


API_KEY = get("DASHSCOPE_API_KEY")
BASE_URL = get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = get("AGENT_MODEL", "qwen3.7-flash-2026-07-15")
PROXY = get("HTTP_PROXY")
PORT = int(get("AGENT_PORT", "8001"))
