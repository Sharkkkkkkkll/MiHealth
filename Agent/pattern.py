# -*- coding: utf-8 -*-
"""宿主睡眠模式分析：从 90 天主睡眠数据学出"这个人几点睡"。

不假设任何人按"晚上"睡觉——夜班、跨午夜、昼夜节律延迟统统适用。
入睡/起床时刻是"圆周量"（0-24h 首尾相接），必须用圆形统计：
  例：23:00 和 01:00 的算术平均是 01:00（错），圆形平均是 00:00（对）。

输出 SleepPattern：
  - bedtime_min / waketime_min：圆周平均的入睡/起床时刻（分钟，0-1439）
  - bedtime_spread_min：入睡离散度（圆形标准差）——小而稳=规律
  - dual_phase：双峰标志（如夜班+补觉两段睡）
  - confidence：样本量+离散度综合出的置信度 0-1
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import data_source


def _to_min(iso_ts: str) -> int | None:
    """ISO 时间戳 → 本地时刻分钟数 (0-1439)。"""
    try:
        dt = datetime.fromisoformat(iso_ts).astimezone()
        return dt.hour * 60 + dt.minute
    except Exception:
        return None


def _circ_mean(mins: list) -> tuple:
    """圆形平均：返回 (平均分钟, 合力R 0-1)。R 越大越集中。"""
    if not mins:
        return None, 0.0
    x = sum(math.cos(2 * math.pi * m / 1440) for m in mins) / len(mins)
    y = sum(math.sin(2 * math.pi * m / 1440) for m in mins) / len(mins)
    r = math.hypot(x, y)
    ang = math.atan2(y, x) % (2 * math.pi)
    return int(round(ang * 1440 / (2 * math.pi))) % 1440, r


@dataclass
class SleepPattern:
    n: int = 0
    bedtime_min: int | None = None
    waketime_min: int | None = None
    bedtime_spread_min: int = 0          # 由 R 换算：R=1→0min，R 小→大
    dual_phase: bool = False             # 是否存在两段显著分离的睡眠
    second_bedtime_min: int | None = None
    confidence: float = 0.0
    source: str = "learned"              # learned | explicit
    notes: list = field(default_factory=list)

    @staticmethod
    def fmt(mins) -> str:
        if mins is None:
            return "未知"
        return f"{(mins // 60) % 24:02d}:{mins % 60:02d}"

    def to_dict(self) -> dict:
        return {
            "n_nights": self.n,
            "bedtime": self.fmt(self.bedtime_min),
            "waketime": self.fmt(self.waketime_min),
            "bedtime_spread_min": self.bedtime_spread_min,
            "dual_phase": self.dual_phase,
            "second_bedtime": self.fmt(self.second_bedtime_min),
            "confidence": round(self.confidence, 2),
            "source": self.source,
            "notes": self.notes,
        }


def _split_dual(nights: list) -> tuple:
    """把入睡时刻按最大间隔切成两簇（检测夜班+白天补觉的双相睡眠）。

    返回 (主簇入睡分钟列表, 次簇入睡分钟列表或 None)。
    """
    mins = sorted(nights)
    if len(mins) < 8:
        return mins, None
    # 找圆周上相邻两点最大间隙
    gaps = [(mins[(i + 1) % len(mins)] - mins[i]) % 1440 for i in range(len(mins))]
    gi = gaps.index(max(gaps))
    if max(gaps) < 300:                   # 最大间隙<5小时：单峰，无需切
        return mins, None
    ring = mins[gi + 1:] + mins[:gi + 1]  # 从间隙后展开成连续序列
    # 回绕段 +1440 保持单调递增，才能比较相邻差
    mono = [ring[0]]
    for v in ring[1:]:
        mono.append(v if v >= mono[-1] else v + 1440)
    # 在展开序列里找 >4h 的断点作为簇分界（最多两簇）
    for i in range(1, len(mono)):
        if mono[i] - mono[i - 1] > 240:
            a, b = mono[:i], mono[i:]
            big, small = (a, b) if len(a) >= len(b) else (b, a)
            if len(small) >= max(3, len(mins) * 0.15):
                return [v % 1440 for v in big], [v % 1440 for v in small]
    return mins, None


def analyze(nights_window: int = 90, user_id: str = "") -> SleepPattern:
    recs = data_source.main_sleeps(nights_window, user_id)
    p = SleepPattern()
    if not recs:
        p.notes.append("无主睡眠记录，无法学习模式")
        return p
    bed_mins, wake_mins = [], []
    for r in recs:
        b = _to_min(r.get("start_at") or "")
        w = _to_min(r.get("end_at") or "")
        if b is None or w is None:
            continue
        bed_mins.append(b)
        wake_mins.append(w)
    main, second = _split_dual(bed_mins)
    p.n = len(bed_mins)
    bm, rb = _circ_mean(main)
    wm, rw = _circ_mean(wake_mins)
    p.bedtime_min, p.waketime_min = bm, wm
    # 合力R→离散度近似（分钟）
    p.bedtime_spread_min = int(-1440 / (2 * math.pi) * math.log(max(rb, 1e-3)) / 2) if rb > 0 else 720
    if second:
        sm, rs = _circ_mean(second)
        p.dual_phase = True
        p.second_bedtime_min = sm
        p.notes.append(
            f"检测到双相睡眠：主段 {SleepPattern.fmt(bm)} 入睡（{len(main)}夜），"
            f"次段约 {SleepPattern.fmt(sm)} 入睡（{len(second)}夜）——可能是夜班作息"
        )
    if p.bedtime_spread_min > 180:
        p.notes.append("入睡时间波动很大（>3小时），提醒时间会偏保守")
    # 置信度：样本量 × 集中度
    n_score = min(1.0, p.n / 21)                     # 3 周即满分
    conc = math.exp(-p.bedtime_spread_min / 120)      # 离散度衰减
    p.confidence = round(0.6 * n_score + 0.4 * conc, 2)
    p.notes.append(f"基于最近 {p.n} 夜主睡眠学习")
    return p


def _parse_clock(s: str) -> int | None:
    try:
        h, m = s.strip().split(":")
        return (int(h) * 60 + int(m)) % 1440
    except Exception:
        return None


def nap_records(days: int = 30, user_id: str = "") -> list:
    """小睡记录（is_nap=1，20-180分钟），按 start_at 去重。"""
    data = data_source.load(user_id)
    if not data:
        return []
    best: dict = {}
    for r in data.get("records", {}).get("sleep", []):
        if not r.get("is_nap"):
            continue
        k = r.get("start_at")
        if not k:
            continue
        cur = best.get(k)
        if cur is None or (r.get("duration_minutes") or 0) > (cur.get("duration_minutes") or 0):
            best[k] = r
    out = [r for r in best.values() if 15 <= (r.get("duration_minutes") or 0) <= 180]
    out.sort(key=lambda r: r.get("start_at", ""))
    return out[-days:]


def nap_pattern(user_id: str = "") -> dict | None:
    """学习小睡习惯：≥5次且时间集中（R≥0.5）才给出窗口，否则 None（不瞎猜）。"""
    recs = nap_records(30, user_id)
    mins = [m for m in (_to_min(r.get("start_at") or "") for r in recs) if m is not None]
    if len(mins) < 5:
        return None
    mean, r_ = _circ_mean(mins)
    if r_ < 0.5:
        return None
    spread = int(-1440 / (2 * math.pi) * math.log(max(r_, 1e-3)) / 2)
    return {"center": mean, "center_str": SleepPattern.fmt(mean),
            "spread_min": spread, "n": len(mins)}


def effective_bedtime(preferences: dict, user_id: str = "") -> SleepPattern:
    """最终生效的作息模式：用户显式设定 > 数据学习。"""
    p = analyze(user_id=user_id)
    goal = (preferences or {}).get("sleep_goal") or {}
    eb = _parse_clock(str(goal.get("bedtime", "")))
    if eb is not None:
        p.bedtime_min = eb
        p.source = "explicit"
        p.notes.append(f"用户显式设定入睡目标 {SleepPattern.fmt(eb)}，优先于学习值")
    return p
