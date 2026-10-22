# -*- coding: utf-8 -*-
"""分析窗口：Agent 决定"什么时候才值得开启检测/分析/提醒"。

目的：
1. 省 token——数据每 5 分钟拉一次很便宜，但提醒判断只在窗口内做，
   LLM 润色只在真的要发提醒时调用（每天 ≤1 次）。
2. 省电/省打扰——手机只在窗口内才唤起小米运动健康，
   窗口外只做轻量云端拉取，不会闪屏。

窗口（全部圆周判定，不假设"晚上"）：
  pre_sleep: [预计入睡-90, 预计入睡+30]
  nap:       [小睡中心-30, 小睡中心+90]（小睡习惯学得准才有）
"""
from __future__ import annotations

from datetime import datetime

import memory
import pattern as pat_mod


def _in_ring(x: int, s: int, e: int) -> bool:
    return (x - s) % 1440 <= (e - s) % 1440


def windows(user_id: str = "") -> list:
    import planner
    prefs = memory.get_preferences(user_id)
    bed, _src = planner.tonight_bedtime(prefs, user_id)
    out = []
    if bed is not None:
        # 收窄：五弹最早 T−60、HR/OD 最晚 T+90，窗口只留必要区间（减少闪小米时段）
        out.append({"kind": "pre_sleep", "center": pat_mod.SleepPattern.fmt(bed),
                    "start": (bed - 75) % 1440, "end": (bed + 90) % 1440})
    nap = pat_mod.nap_pattern(user_id)
    if nap:
        c = nap["center"]
        out.append({"kind": "nap", "center": nap["center_str"],
                    "start": (c - 30) % 1440, "end": (c + 90) % 1440})
    return out


def now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def active(kind: str | None = None, user_id: str = "") -> bool:
    now = now_min()
    for w in windows(user_id):
        if kind and w["kind"] != kind:
            continue
        if _in_ring(now, w["start"], w["end"]):
            return True
    return False
