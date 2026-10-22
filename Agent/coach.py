# -*- coding: utf-8 -*-
"""睡前教练状态机（决策 B 核心）：不固定弹几次，盯实时信号决定"这一拍开不开口、说什么"。

每 5 分钟（后台同步节奏）被体检一次，输入三组信号：
  1) 实时心率：最近10分钟被动心率均值 vs "睡前基线心率"（过去14天同一时段均值）
     —— 心率不降 = 身体还醒着/兴奋；心率降了 = 该催闭眼
  2) 是否已入睡：今天的 sleep 记录里是否已有主睡眠（有=已睡，闭嘴）
  3) 距预计入睡时间的圆周差（提前档 / 过点档）

事件表（每晚每个事件最多一次，全局冷却 20 分钟，整晚最多 4 条）：
  E1 wind_down   T-75~T-45  收工预告（心率已偏高则附带降温建议）
  E2 high_hr     T-45~T-5   到点了但心率仍高：先降心率，别急着躺
  E3 on_time     T-45~T-5   心率正常：状态不错，按时躺
  E4 overdue     T+5~T+90   过点还没睡着(无睡眠记录+心率仍高)：别刷手机，呼吸法
  E5 drowsy      T+5~T+120  过点但心率已降：困意来了，快闭眼别抓手机
  检测到已入睡 → 全静默。
  次日早上：睡眠记录 start_at 会证明"到底几点睡着"，话术可引用（E4/E5 间体现）。

状态持久化 memory_data/session_state.json：{date, fired:{event:ts}, count}
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import config
import data_source
import memory
import pattern as pat_mod

STATE_PATH = os.path.join(config.DATA_DIR, "session_state.json")

COOLDOWN_MIN = 8       # （已弃用全局冷却，保留常量兼容）
MAX_NIGHT = 6          # 整晚最多条数
HR_HIGH_RATIO = 1.05   # 实时心率 / 睡前基线 ≥ 此值 = 身体还兴奋


def _state_path(user_id: str = "") -> str:
    """session_state.json 按用户隔离：memory_data/<user_id>/session_state.json。"""
    d = config.data_dir_for(user_id)
    return os.path.join(d, "session_state.json")


def _load_state(anchor: int | None = None, user_id: str = "") -> dict:
    try:
        with open(_state_path(user_id), "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("date") != today:
        st = {"date": today, "fired": {}, "count": 0}
    # 锚点变了（用户改目标/新计划）→ 重置当晚弹点，否则旧 fired 会吃掉新窗口
    if anchor is not None and st.get("anchor") != anchor:
        st = {"date": today, "fired": {}, "count": 0}
    st.setdefault("fired", {})
    st.setdefault("count", 0)
    st["anchor"] = anchor
    return st


def _save_state(st: dict, user_id: str = "") -> None:
    p = _state_path(user_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, p)


def _mark(st: dict, event: str) -> None:
    st["fired"][event] = datetime.now().isoformat(timespec="seconds")
    st["count"] += 1


def _mins_to_bed(bedtime_min: int) -> int:
    """现在距预计入睡还有多少分钟（圆周，负数=已过点）。"""
    n = datetime.now()
    now = n.hour * 60 + n.minute
    d = (bedtime_min - now) % 1440
    return d - 1440 if d > 720 else d


def current_energy(user_id: str = "") -> float | None:
    """当前能量（与手机能量条同一模型）：昨晚有效睡眠充电 - 醒来时长×6.25%/h。
    有效睡眠 = 时长×恢复效率（深睡少/多梦/评分低 → 回电打折）。"""
    recs = data_source.main_sleeps(2, user_id)
    if not recs:
        return None
    last = recs[-1]
    try:
        wake = datetime.fromisoformat(last["end_at"]).astimezone()
    except Exception:
        return None
    eff = data_source.effective_minutes(last, user_id)
    charge = min(100.0, eff / 480 * 100)
    hrs = (datetime.now().astimezone() - wake).total_seconds() / 3600
    if hrs < 0:
        return charge
    return round(max(2.0, min(100.0, charge - hrs * (100.0 / 16.0))), 0)


def _asleep_recently(hours: int = 3, user_id: str = "") -> bool:
    """最近 N 小时内是否有主睡眠开始 = "今晚已入睡"。

    注意不能用"今天有记录"判断：02:56 入睡的记录 start_at 是明天，
    而今天凌晨那觉的记录会害得整晚误判已睡全夜静默。
    """
    from datetime import timedelta
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    for r in reversed(data_source.main_sleeps(3, user_id)):
        try:
            st = datetime.fromisoformat(r.get("start_at") or "").astimezone()
        except Exception:
            continue
        return st >= cutoff
    return False


def screen_awake(prefs: dict) -> bool:
    """屏幕亮 = 肯定醒着（单向信号：允许弹）。
    注意反向不成立——息屏≠睡着（可能在玩电脑），不可用作静默依据。"""
    mins = prefs.get("last_screen_off_min")
    ts = prefs.get("last_screen_report")
    if mins is None or not ts:
        return False
    try:
        from datetime import timedelta
        age = datetime.now() - datetime.fromisoformat(ts)
        if age > timedelta(minutes=10):
            return False               # 上报过期不可信
        return int(mins) == 0          # 亮屏中
    except Exception:
        return False


def hr_asleep(prefs: dict, user_id: str = "") -> bool:
    """实时心率睡着判定（手环BLE广播上报，睡前窗口专用）。

    判据：最近15分钟内有广播心率数据，且均值 < 睡前基线×0.92
    （入睡后心率会掉到睡眠水平，明显低于清醒静息）。
    数据由手机服务每30秒 POST /api/agent/hr 写入 realtime_hr。
    """
    from datetime import timedelta
    pts = prefs.get("realtime_hr") or []
    if not pts:
        return False
    now = datetime.now()
    recent = []
    for p in pts[-40:]:
        try:
            t = datetime.fromisoformat(p["ts"])
            if (now - t) <= timedelta(minutes=15):
                recent.append(float(p["bpm"]))
        except Exception:
            continue
    if len(recent) < 5:                # 数据太少不下结论
        return False
    avg = sum(recent) / len(recent)
    bed = prefs.get("_bed_min_for_hr")
    base = data_source.evening_baseline(bed, user_id=user_id) if bed is not None else None
    if not base:
        return avg < 55                # 无基线时兜底阈值（成人睡眠心率常见区）
    return avg < base * 0.92


def decide(bedtime_min: int, force: bool = False, user_id: str = "") -> dict | None:
    """返回事件 dict 或 None。固定五弹 + 屏幕续弹（2026-10-16 用户定版）：

    P60/P40/P30/P20/P10 到点各弹一次（共5条，无条件）
    五弹之后：屏幕亮=还醒着→STAY每≥10分钟"赶紧睡"；屏幕灭→不弹
    心率广播已与睡前提醒解耦：不再影响弹点，只作为独立实时心率功能。
    """
    st = _load_state(bedtime_min, user_id)
    _prefs = memory.get_preferences(user_id)
    max_night = 12

    # 静默：云端已入睡（两模式通用）；广播模式加实时心率判睡
    if not force and _asleep_recently(user_id=user_id):
        return None
    _prefs["_bed_min_for_hr"] = bedtime_min

    if st["count"] >= max_night and not force:
        return None

    mins = _mins_to_bed(bedtime_min)          # 正=还早，负=过点
    hr_now = data_source.hr_recent(10, user_id)
    hr_base = data_source.evening_baseline(bedtime_min, user_id=user_id)
    # 实时广播心率优先（云端在"人在电脑前"场景下是旧数据）
    _rt = _prefs.get("realtime_hr") or []
    if _rt:
        try:
            from datetime import timedelta
            recent_rt = [float(p["bpm"]) for p in _rt[-10:]
                         if (datetime.now() - datetime.fromisoformat(p["ts"]))
                         <= timedelta(minutes=5)]
            if len(recent_rt) >= 3:
                hr_now = sum(recent_rt) / len(recent_rt)
        except Exception:
            pass
    hr_high = bool(hr_now and hr_base and hr_now >= hr_base * HR_HIGH_RATIO)
    trend = data_source.hr_trend(user_id=user_id)            # {direction, first, last, now}

    def fired(key: str) -> bool:
        return key in st["fired"]

    def mins_since(key: str):
        ts = st["fired"].get(key)
        if not ts:
            return None
        try:
            return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 60
        except Exception:
            return None

    facts = []
    if hr_now:
        facts.append(f"当前心率约{hr_now:.0f}")
    if hr_base:
        facts.append(f"睡前基线约{hr_base:.0f}")

    event = None
    urgency = "normal"
    lead = 0            # 该事件对应的"距入睡X分钟"（时间词唯一合法来源）

    # —— 固定五弹 + 屏幕续弹 ——
    # 窗口"宁晚勿早"：mins 从名义提前量向下走（如 P60=[45,60] 即 T−60 起、
    # 最晚 T−45 命中；调度节拍 20s 保证窗口内必踩点，早一分钟都不弹）
    for key, lo, hi, ld in (("P60", 45, 60, 60), ("P40", 30, 40, 40),
                            ("P30", 20, 30, 30), ("P20", 10, 20, 20),
                            ("P10", 0, 10, 10)):
        if lo <= mins <= hi and not fired(key):
            event, lead = key, ld
            break
    if event is None and mins < 0:
        # 五弹后：屏幕亮=还醒着→续弹；灭=不弹
        gap = mins_since("STAY")
        if mins > -90 and screen_awake(_prefs) and (gap is None or gap >= 10):
            event, urgency = "STAY", "high"

    if event is None:
        if force:   # 测试模式：不带时间词，事件取最接近的形态
            event = "HR" if hr_high else "P30"
        else:
            return None

    out = {
        "event": event,
        "urgency": urgency,
        "mins_to_bed": mins,
        "lead_min": lead,                 # 时间词唯一合法来源（0=不许提时间）
        "hr_now": round(hr_now) if hr_now else None,
        "hr_base": round(hr_base) if hr_base else None,
        "hr_high": hr_high,
        "trend": trend,
        "energy": current_energy(user_id),
        "facts": facts,
    }
    if not force:
        _mark(st, event)
        _save_state(st, user_id)
    return out


EVENT_TEXT = {
    "P60": "提前一小时预告",
    "P40": "提前四十分钟：赶紧准备",
    "P30": "提前半小时：该收尾了",
    "P20": "提前二十分钟：快去洗漱",
    "P10": "最后十分钟：躺下闭眼",
    "STAY": "五弹之后屏幕还亮着：用户明显还没睡，催睡",
    "HR": "睡前区间心率仍高：隔十分钟劝一次降温",
    "OD": "过点未睡且心率仍高：别烙饼",
}
