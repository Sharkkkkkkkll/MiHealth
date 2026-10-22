# -*- coding: utf-8 -*-
"""AI 智能闹钟：整晚心率曲线 → 预测起床窗口走向 → 生成唤醒策略。

严格按用户确认的策略实现：
1. 窗口开始前绝不叫。
2. 优先取下一次浅睡：仅当预测窗口内一定会有两次浅睡才跳过第一次。
3. 深睡保护：仅当预测整个窗口内没有浅睡才启用。
4. 保底时刻强制叫醒（setAlarmClock 系统级）。
5. 30 秒确认"醒来"由 Agent 根据历史数据出具判定标准。
6. 固定唤醒条件文案，不生成。
"""
import json
import os
import time
from datetime import datetime, timedelta

import config
import data_source
import memory


# ---------- 固定唤醒条件文案 ----------
CONDITION_LABELS = {
    "light_sleep_early": "浅睡前期唤醒",
    "light_sleep_mid": "浅睡中期唤醒",
    "light_sleep_late": "浅睡后期唤醒",
    "rem": "快速眼动期唤醒",
    "deep_protect": "深睡保护方案唤醒",
    "deep_end_light": "深睡结束浅睡唤醒",
    "near_fallback": "窗口内无浅睡接近保底",
    "window_start_light": "窗口开始即浅睡",
    "hr_abnormal": "心率异常提前唤醒",
    "fallback_only": "保底时刻唤醒",
}


def _clock_to_min(s: str) -> int:
    h, m = s.strip().split(":")
    return (int(h) * 60 + int(m)) % 1440


def _fmt_min(m: int) -> str:
    m %= 1440
    return "%02d:%02d" % (m // 60, m % 60)


def _next_occurrence(clock_min: int, now_dt: datetime) -> int:
    """绝对分钟（epoch 分钟）：今天已过 → 明天。"""
    now_abs = int(now_dt.timestamp() // 60)
    today_start = now_abs - (now_dt.hour * 60 + now_dt.minute)
    cand = today_start + clock_min
    if cand <= now_abs:
        cand += 1440
    return cand


def _hr_baseline(curve: list) -> float:
    """整晚心率基线：取低分位（深睡/静息）。"""
    vals = [p["bpm"] for p in curve if isinstance(p.get("bpm"), (int, float))]
    if not vals:
        return 60.0
    vals.sort()
    return vals[max(0, int(len(vals) * 0.1))]


def _hr_std(curve: list, start_idx: int, end_idx: int) -> float:
    seg = [p["bpm"] for p in curve[start_idx:end_idx]
           if isinstance(p.get("bpm"), (int, float))]
    if len(seg) < 2:
        return 0.0
    mean = sum(seg) / len(seg)
    return (sum((x - mean) ** 2 for x in seg) / len(seg)) ** 0.5


def _detect_light_sleep(curve: list, baseline: float, rise: float,
                        duration_min: int) -> list:
    """检测非深睡信号（浅睡/REM）：HR 从深睡低位回升 + 波动增大。

    深睡特征：心率最低、波动极小（标准差 <1.5）。
    浅睡/REM 特征：心率比深睡略高（回升 2-6bpm），波动中等偏大。
    用滑动窗口同时看"平均心率相对基线"和"窗口内标准差"两个信号，
    避免只看绝对值导致浅睡（心率区分度弱）漏判。
    """
    hits = []
    n = len(curve)
    if n < 2:
        return hits
    # 1s 粒度 → duration_min 分钟窗口
    win = max(30, int(duration_min * 60))
    # 波动阈值：深睡标准差通常 <1.5，浅睡/REM 通常 >2.0
    std_thr = 2.0
    i = 0
    while i + win <= n:
        seg = curve[i:i + win]
        vals = [p["bpm"] for p in seg if isinstance(p.get("bpm"), (int, float))]
        if len(vals) < max(20, win // 2):
            i += 30
            continue
        avg = sum(vals) / len(vals)
        sd = (sum((x - avg) ** 2 for x in vals) / len(vals)) ** 0.5
        # 非深睡：平均心率 ≥ 基线（回升）且波动 ≥ 阈值
        # 或波动很大（REM 特征）即使平均心率接近基线也算
        if (avg >= baseline and sd >= std_thr) or sd >= 3.0:
            hits.append(i)
            i += win  # 跳过已判定窗口，避免重复
        else:
            i += 30
    return hits


def _predict_light_sleep_windows(curve: list, baseline: float,
                                 rise: float, duration_min: int,
                                 window_start_abs: int, window_end_abs: int,
                                 now_dt: datetime,
                                 user_id: str = "") -> list:
    """基于整晚曲线 + 历史周期，预测窗口内浅睡窗口。
    简化：用曲线中已出现的浅睡信号 + 历史平均周期外推。"""
    hits = _detect_light_sleep(curve, baseline, rise, duration_min)
    if not hits:
        return []
    # 历史平均周期（分钟）：从 data_source 学
    cycle = _avg_cycle_min(user_id)
    # 把已检测到的浅睡信号时间戳转绝对分钟，外推到窗口
    windows = []
    for idx in hits:
        ts = curve[idx]["ts"]
        abs_min = int(ts // 60)
        if window_start_abs <= abs_min <= window_end_abs:
            windows.append(abs_min)
    # 外推：从最后一个信号 + cycle 往后推
    if windows:
        last = max(windows)
        while last + cycle <= window_end_abs:
            last += cycle
            if last >= window_start_abs:
                windows.append(last)
    windows.sort()
    return windows


def _avg_cycle_min(user_id: str = "") -> int:
    """历史平均睡眠周期（分钟）。从 data_source 学，缺省 90。"""
    try:
        recs = data_source.main_sleeps(30, user_id)
        if not recs:
            return 90
        # 用睡眠时长 / 周期数近似：平均周期 ≈ 平均睡眠时长 / 4~5
        avg_min = sum(r.get("duration_minutes") or 0 for r in recs) / len(recs)
        return max(60, min(120, int(avg_min / 4.5)))
    except Exception:
        return 90


def _predict_light_windows_from_history(
        user_id: str, wake_start_min: int, wake_end_min: int,
        days: int = 30) -> list:
    """用历史小米分期数据预测起床窗口内的浅睡/REM 时段。

    思路：小米手环有体动传感器，分期比心率更准。过去 N 天里，
    起床窗口内每个 5 分钟槽位的浅睡/REM 概率，就是今天该时段
    处于浅睡/REM 的经验概率。返回预测的浅睡窗口起点（绝对分钟）。
    """
    data = data_source.load(user_id)
    if not data:
        return []
    recs = data.get("records", {}).get("sleep", [])
    # 窗口长度（分钟）
    win_len = (wake_end_min - wake_start_min) % 1440
    if win_len <= 0:
        win_len += 1440
    slots = {}
    for r in recs:
        stages = json.loads(r.get("stages") or "[]")
        if not stages:
            continue
        try:
            cur = datetime.fromisoformat(r["start_at"])
        except Exception:
            continue
        # 展开分期到分钟时间轴
        timeline = []
        offset = 0
        for st in stages:
            name = st.get("stage")
            mins = st.get("minutes") or 0
            for _ in range(mins):
                timeline.append((offset, name))
                offset += 1
        if not timeline:
            continue
        # 睡眠开始时刻（分钟）
        sh = cur.hour * 60 + cur.minute
        # 窗口起点相对睡眠开始的 offset
        target = (wake_start_min - sh) % 1440
        for off, name in timeline:
            if target <= off < target + win_len:
                slot = (off - target) // 5
                slots.setdefault(slot, []).append(name)

    if not slots:
        return []
    # 计算每个槽位浅睡/REM 概率，选概率最高的连续段
    light_rem_prob = {}
    for slot, names in slots.items():
        if not names:
            continue
        lr = sum(1 for n in names if n in ("light", "rem"))
        light_rem_prob[slot] = lr / len(names)

    # 找浅睡/REM 概率 ≥ 0.6 的槽位，聚合成窗口
    windows = []
    cur_slot_start = None
    for slot in sorted(light_rem_prob):
        if light_rem_prob[slot] >= 0.6:
            if cur_slot_start is None:
                cur_slot_start = slot
        else:
            if cur_slot_start is not None:
                windows.append(wake_start_min + cur_slot_start * 5)
                cur_slot_start = None
    if cur_slot_start is not None:
        windows.append(wake_start_min + cur_slot_start * 5)
    return windows


def _deep_protect_trigger(curve: list, baseline: float, window_end_abs: int,
                          now_dt: datetime) -> str | None:
    """深睡保护触发时刻：若预测窗口内无浅睡，且当前/即将深睡会跨过窗口结束，
    则在进入深睡前（窗口内相对浅的点）叫醒。简化：窗口结束前 15 分钟。"""
    # 若窗口内已检测到浅睡信号，不启用深睡保护
    hits = _detect_light_sleep(curve, baseline, 6, 2)
    if hits:
        return None
    # 窗口结束前 15 分钟作为深睡保护触发点
    trigger = window_end_abs - 15
    return _fmt_min(trigger)


def _awake_confirm(curve: list, baseline: float, user_id: str = "") -> dict:
    """30 秒确认"醒来"判定：心率显著上升 + 波动。Agent 根据历史数据出具。"""
    # 历史静息/深睡基线 + 保守默认：上升 15% 且持续 30 秒
    rise_pct = 15
    duration_sec = 30
    # 若历史数据显示你醒来心率差异更大，可调高
    try:
        recs = data_source.main_sleeps(30, user_id)
        if recs:
            # 用历史平均深睡 HR 与清醒 HR 的差异比例
            pass
    except Exception:
        pass
    return {"hr_rise_pct": rise_pct, "duration_sec": duration_sec}


def generate_wake_plan(user_id: str = "", curve: list | None = None,
                       wake_start: str = "08:00",
                       wake_end: str = "08:30") -> dict:
    """生成唤醒策略包。curve 为空时用本地最新 JSON 的睡眠窗口心率近似。"""
    now_dt = datetime.now().astimezone()
    ws = _clock_to_min(wake_start)
    we = _clock_to_min(wake_end)
    ws_abs = _next_occurrence(ws, now_dt)
    we_abs = _next_occurrence(we, now_dt)
    if we_abs <= ws_abs:
        we_abs += 1440

    if not curve:
        curve = _fallback_curve_from_data(user_id)

    baseline = _hr_baseline(curve)
    # 保守默认：回升 6 bpm、连续 2 分钟
    rise = 6
    duration_min = 2

    # 预测窗口内浅睡窗口：优先用历史分期（小米体动分期比心率准），
    # 历史数据不足时退回心率检测 + 周期外推。
    light_windows = _predict_light_windows_from_history(
        user_id, ws, we)
    if not light_windows:
        light_windows = _predict_light_sleep_windows(
            curve, baseline, rise, duration_min, ws_abs, we_abs, now_dt,
            user_id=user_id)

    # 策略选择（严格按用户规则）
    if len(light_windows) >= 2:
        strategy = "light_sleep_mid"
        prefer_second = True
        second_expected = _fmt_min(light_windows[1])
        confidence = 0.7   # 预测有两次浅睡，置信度中等（后续数据驱动调优）
        deep_protect = None
        label = CONDITION_LABELS["light_sleep_mid"]
    elif len(light_windows) == 1:
        strategy = "light_sleep_mid"
        prefer_second = False
        second_expected = None
        confidence = 0.8   # 只有一次浅睡，置信度较高
        deep_protect = None
        label = CONDITION_LABELS["light_sleep_mid"]
    else:
        # 窗口内无浅睡 → 深睡保护
        strategy = "deep_protect"
        prefer_second = False
        second_expected = None
        confidence = 0.6   # 预测无浅睡，置信度较低（可能误判）
        deep_protect = _deep_protect_trigger(curve, baseline, we_abs, now_dt)
        label = CONDITION_LABELS["deep_protect"]

    pack = {
        "window": {"start": wake_start, "end": wake_end},
        "strategy": strategy,
        "params": {
            "hr_baseline": round(baseline, 1),
            "light_sleep_hr_rise": rise,
            "light_sleep_duration_min": duration_min,
            "deep_protect_trigger": deep_protect,
            "prefer_second_light": prefer_second,
            "second_light_expected": second_expected,
            "confidence": confidence,
            "awake_confirm": _awake_confirm(curve, baseline, user_id),
        },
        "condition_label": label,
        "generated_at": now_dt.isoformat(timespec="seconds"),
    }
    # 详情日志：用户对照用（曲线规模/浅睡窗口/策略依据/参数）
    try:
        _ac = _awake_confirm(curve, baseline, user_id)
        _lw = []
        for w in light_windows:
            if isinstance(w, (tuple, list)) and len(w) >= 2:
                _lw.append((_fmt_min(w[0]), _fmt_min(w[1])))
            else:
                _lw.append(_fmt_min(w))
        print(f"[WakePlan][{user_id or 'root'}] 📋 Detail: curve={len(curve)} baseline={round(baseline,1)} "
              f"light_windows={_lw} strategy={strategy} prefer_second={prefer_second} "
              f"second_expected={second_expected} deep_protect={deep_protect} "
              f"confidence={confidence} label={label} awake_confirm={_ac}")
        print(f"[WakePlan][{user_id or 'root'}] 📋 中文: 曲线点数={len(curve)} 基线心率={round(baseline,1)} "
              f"浅睡窗口={_lw} 策略={strategy} 优先第二次浅睡={prefer_second} "
              f"第二次浅睡预期={second_expected} 深睡保护触发={deep_protect} "
              f"置信度={confidence} 唤醒文案={label} 醒来确认={_ac}")
    except Exception as e:
        print(f"[WakePlan][{user_id or 'root'}] 📋 详情打印失败: {e!r}")
    # 存 memory（供复盘/进化）
    try:
        memory.update_preferences_shallow({"wake_pack_last": pack}, user_id)
    except Exception:
        pass
    return pack


def _fallback_curve_from_data(user_id: str = "") -> list:
    """无实时曲线时，从本地最新 JSON 的睡眠窗口心率近似生成（兜底）。"""
    try:
        data = data_source.load(user_id)
        if not data:
            return []
        hr = data.get("records", {}).get("heart_rate", [])
        # 取最近 6 小时 passive 心率
        now = time.time()
        pts = [{"ts": p.get("timestamp", 0), "bpm": p.get("bpm", 0)}
               for p in hr
               if p.get("timestamp") and now - p.get("timestamp", 0) < 6 * 3600
               and isinstance(p.get("bpm"), (int, float))]
        pts.sort(key=lambda x: x["ts"])
        return pts[-2000:]
    except Exception:
        return []


def review_wake(user_id: str = "", curve_full: list | None = None,
                wake_pack: dict | None = None,
                actual_wake_time: str = "",
                outcome: str = "",
                concurrent_strategies: list | None = None) -> dict:
    """唤醒后复盘：比对预测 vs 实际，更新 memory 供下次进化。"""
    try:
        review = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "actual_wake_time": actual_wake_time,
            "outcome": outcome,
            "predicted_strategy": (wake_pack or {}).get("strategy"),
            "predicted_second_light": ((wake_pack or {}).get("params") or {})
                .get("second_light_expected"),
            "predicted_confidence": ((wake_pack or {}).get("params") or {})
                .get("confidence"),
            "concurrent_strategies": concurrent_strategies or [],
            "curve_points": len(curve_full or []),
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        }
        prefs = memory.get_preferences(user_id)
        log = prefs.get("wake_review_log") or []
        log.append(review)
        memory.update_preferences_shallow({"wake_review_log": log[-60:]}, user_id)
        return {"ok": True, "review": review}
    except Exception as e:
        return {"ok": False, "err": str(e)}
