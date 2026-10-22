# -*- coding: utf-8 -*-
"""Agent 三层记忆（JSON 文件落盘，线程安全）：

- preferences  偏好：作息目标（与手机 App SleepGoalPrefs 同源，手机推送过来）
- history      历史：Agent 对话与决策的滚动记录（截断保留最近 N 条）
- feedback     反馈：用户对 Agent 建议/叫醒的评分与备注（学习素材）

多用户（方案 A，2026-10-15）：所有函数带 user_id 参数，
按 memory_data/<user_id>/ 子目录隔离；user_id 空 → 根 memory_data（兼容单用户旧数据）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

import config

_LOCK = threading.Lock()
MAX_HISTORY = 200


def _path(name: str, user_id: str = "") -> str:
    d = config.data_dir_for(user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name + ".json")


def _read(name: str, default, user_id: str = ""):
    p = _path(name, user_id)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write(name: str, obj, user_id: str = "") -> None:
    p = _path(name, user_id)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------- preferences ----------

def get_preferences(user_id: str = "") -> dict:
    with _LOCK:
        return _read("preferences", {}, user_id)


def update_preferences_shallow(d: dict, user_id: str = "") -> dict:
    """浅合并（只更新顶层键，如 sent_log），不动其他偏好。"""
    with _LOCK:
        cur = _read("preferences", {}, user_id)
        cur.update(d)
        _write("preferences", cur, user_id)
        return cur


def save_preferences(d: dict, user_id: str = "") -> dict:
    with _LOCK:
        cur = _read("preferences", {}, user_id)
        cur.update(d)
        cur["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write("preferences", cur, user_id)
        return cur


# ---------- history ----------

def add_history(role: str, content: str, user_id: str = "") -> None:
    with _LOCK:
        h = _read("history", [], user_id)
        h.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "role": role,
            "content": content[:2000],
        })
        _write("history", h[-MAX_HISTORY:], user_id)


def recent_history(n: int = 12, user_id: str = "") -> list:
    with _LOCK:
        return _read("history", [], user_id)[-n:]


# ---------- feedback ----------

def add_feedback(entry: dict, user_id: str = "") -> None:
    """entry: {kind: wake|advice|..., rating: 1-5, note: str}"""
    with _LOCK:
        fb = _read("feedback", [], user_id)
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        fb.append(entry)
        _write("feedback", fb[-500:], user_id)


def all_feedback(user_id: str = "") -> list:
    with _LOCK:
        return _read("feedback", [], user_id)


# ---------- tolerance（耐受性画像，独立文件，动态学习） ----------
# 记录"这个人能接受提前多久睡 / 困了能熬多久 / 能接受延后多久睡"。
# 与偏好不同：偏好是静态设定，耐受性是随实际入睡 vs 推荐偏差不断更新的画像。
# 数据不足时用保守默认值，样本越多越准。

_TOL_DEFAULT = {
    "early_shift": {        # 能接受"比推荐早睡"多少分钟（提前耐受）
        "max_min": 30,       # 最多能提前 30 分（超过可能睡不着）
        "avg_min": 15,       # 平均提前量
        "samples": 0,
    },
    "late_shift": {         # 能接受"比推荐晚睡"多少分钟（延后耐受）
        "max_min": 60,       # 最多能延后 60 分
        "avg_min": 30,
        "samples": 0,
    },
    "sleepiness": {         # 困意耐受：困了之后还能撑多久（分钟）
        "max_min": 45,       # 困了最多还能撑 45 分
        "avg_min": 20,
        "samples": 0,
    },
    "updated_at": None,
}


def get_tolerance(user_id: str = "") -> dict:
    """读取耐受性画像；无文件/损坏 → 返回保守默认值（深拷贝防污染）。"""
    with _LOCK:
        raw = _read("tolerance", None, user_id)
        if not isinstance(raw, dict):
            import copy
            return copy.deepcopy(_TOL_DEFAULT)
        # 合并默认，确保字段齐全
        import copy
        out = copy.deepcopy(_TOL_DEFAULT)
        for k in ("early_shift", "late_shift", "sleepiness"):
            if isinstance(raw.get(k), dict):
                out[k].update({kk: raw[k][kk] for kk in out[k] if kk in raw[k]})
        out["updated_at"] = raw.get("updated_at")
        return out


def save_tolerance(tol: dict, user_id: str = "") -> dict:
    """写耐受性画像（线程安全，原子写）。"""
    with _LOCK:
        tol["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _write("tolerance", tol, user_id)
        return tol


def update_tolerance_ema(key: str, observed_min: int, user_id: str = "") -> dict:
    """EMA 更新耐受画像的某个维度。

    key: early_shift | late_shift | sleepiness
    observed_min: 本次观测到的耐受分钟数（正数）。
    用 EMA（指数移动平均）平滑：样本少时权重高，样本多时趋于稳定。
    同时更新 max_min（历史最大值，保守上限）。
    """
    tol = get_tolerance(user_id)
    dim = tol.get(key)
    if not isinstance(dim, dict):
        import copy
        dim = copy.deepcopy(_TOL_DEFAULT[key])
        tol[key] = dim
    n = int(dim.get("samples") or 0)
    if n == 0:
        dim["avg_min"] = observed_min
    else:
        alpha = 2.0 / (n + 1)          # EMA 系数：样本越多越平滑
        dim["avg_min"] = round(dim["avg_min"] * (1 - alpha) + observed_min * alpha)
    dim["max_min"] = max(int(dim.get("max_min") or 0), observed_min)
    dim["samples"] = n + 1
    return save_tolerance(tol, user_id)
