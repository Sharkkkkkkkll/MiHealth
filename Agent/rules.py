# -*- coding: utf-8 -*-
"""规则引擎（打底）：LLM 只负责措辞，"发不发、发什么类型"由规则拍板。

决策 B（睡前提醒），全部基于 AI/用户确定的入睡时间，不假设晚上睡觉：
  正常模式（AI 定时）：
    R0 数据新鲜度：导出超过 stale_min 分钟 → 不发
    R1 时机：在 [入睡-75, 入睡-10] 窗口内，按档触发——
       - 档 1：提前 1 小时（入睡-75 ~ 入睡-46）
       - 档 2：提前 30 分钟（入睡-45 ~ 入睡-10）
       每档每晚最多一条（sent_log 记 done/d-60、done/d-30）
    R2 触发条件（任一命中才发，无事不打扰）：
       a) 作息偏离：近 3 夜平均入睡比基线晚 ≥ 45min（圆周差）
       b) 生理唤醒：今日静息心率均值比个人 28 天基线高 ≥ 8%
       c) 睡眠负债：近 7 夜平均时长比基线短 ≥ 45min
       d) 低置信兜底：模式学得不准时发温和通用提醒
  测试模式（prefs.test_mode.notify=true，验证弹链路）：
    每次 App 打开都会 force 生成一条（跳过 R0/R1/R2/R3），
    档位按当前时间靠近哪档算哪档，纯兜底话术。
"""
from __future__ import annotations

from datetime import datetime

import data_source
import pattern as pat_mod

STALE_MIN = 240        # 数据超过多少分钟视为过期
DEVIATION_MIN = 45     # 近3夜 vs 基线 的入睡偏离阈值（分钟）
HR_RATIO = 1.08        # 今日静息心率 / 28天基线 的高出比例阈值
DEBT_MIN = 45          # 近7夜平均时长比基线短多少算负债

# 两档提醒：(档名, 触发区起点=入睡-75/-45, 触发区终点=入睡-46/-10)
GEAR_1H = ("d-60", 75, 46)   # 提前1小时档：入睡-75 ~ 入睡-46
GEAR_30M = ("d-30", 45, 10)  # 提前30分钟档：入睡-45 ~ 入睡-10


def _circ_diff(a: int, b: int) -> int:
    """两个时刻的圆周差（分钟，带符号：a 相对 b 晚为正）。"""
    d = (a - b) % 1440
    return d - 1440 if d > 720 else d


def _now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def _gear_hit(bedtime_min: int) -> str | None:
    """当前时刻落在哪一档的触发区（圆周判定）。返回档位名或 None。"""
    now = _now_min()
    for name, s_off, e_off in (GEAR_1H, GEAR_30M):
        s = (bedtime_min - s_off) % 1440
        e = (bedtime_min - e_off) % 1440
        if (now - s) % 1440 <= (e - s) % 1440:
            return name
    return None


def _gear_near(bedtime_min: int) -> str:
    """测试模式：不管在不在窗口，按当前时间离哪档近算哪档。"""
    name = _gear_hit(bedtime_min)
    return name or GEAR_1H[0]


def _facts(pattern: pat_mod.SleepPattern, user_id: str = "") -> tuple:
    """采集规则事实：(触发原因列表, 事实字典)。"""
    recs = data_source.main_sleeps(14, user_id)
    facts, reasons = {}, []

    # a) 作息偏离
    if len(recs) >= 4:
        recent = [pat_mod._to_min(r["start_at"]) for r in recs[-3:]]
        recent = [m for m in recent if m is not None]
        if recent:
            rm, _ = pat_mod._circ_mean(recent)
            dev = _circ_diff(rm, pattern.bedtime_min)
            facts["recent_bed"] = pat_mod.SleepPattern.fmt(rm)
            if dev >= DEVIATION_MIN:
                facts["deviation_min"] = dev
                reasons.append(f"近3夜平均{facts['recent_bed']}才睡，比常态{pattern.fmt(pattern.bedtime_min)}晚了约{dev}分钟")

    # b) 生理唤醒（今日静息心率 vs 基线）
    hr = data_source.resting_hr_by_day(30, user_id)
    if hr:
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        vals = [v for k, v in sorted(hr.items()) if k != today]
        if today in hr and len(vals) >= 7:
            base = sum(vals) / len(vals)
            ratio = hr[today] / base if base else 1
            facts["hr_today"] = round(hr[today])
            facts["hr_base"] = round(base)
            if ratio >= HR_RATIO:
                reasons.append(f"今天静息心率{facts['hr_today']}，比你平时({facts['hr_base']})高了{int((ratio - 1) * 100)}%，身体还兴奋着")

    # c) 睡眠负债
    if len(recs) >= 8:
        durs = [r.get("duration_minutes") or 0 for r in recs[-7:]]
        base_durs = [r.get("duration_minutes") or 0 for r in recs]
        avg, base = sum(durs) / len(durs), sum(base_durs) / len(base_durs)
        facts["avg_dur_7d"] = int(avg)
        if base - avg >= DEBT_MIN:
            reasons.append(f"近7晚平均只睡{int(avg / 60)}小时{int(avg) % 60}分，比你自己基线少了{int(base - avg)}分钟")

    return reasons, facts


def evaluate(pattern: pat_mod.SleepPattern, prefs: dict, today_key: str,
             force: bool = False, user_id: str = "") -> dict | None:
    """返回 None=不发；否则 {kind, gear, bedtime, reasons, facts, urgency}。"""
    if pattern.bedtime_min is None:
        return None

    if force:
        gear = _gear_near(pattern.bedtime_min)
    else:
        fresh = data_source.data_age_minutes(user_id)
        if fresh is None or fresh > STALE_MIN:
            return None                               # R0
        gear = _gear_hit(pattern.bedtime_min)
        if gear is None:
            return None                               # R1
        sent = (prefs or {}).get("sent_log") or {}
        if f"{today_key}:{gear}" in sent:             # R3 档内去重
            return None

    reasons, facts = _facts(pattern, user_id)
    if not reasons:
        # d) 低置信兜底 / 测试模式兜底
        if pattern.confidence < 0.4 or force:
            reasons = [f"今晚预计约 {pat_mod.SleepPattern.fmt(pattern.bedtime_min)} 入睡"]
        else:
            return None

    return {
        "kind": "pre_sleep",
        "gear": gear,
        "bedtime": pat_mod.SleepPattern.fmt(pattern.bedtime_min),
        "reasons": reasons,
        "facts": facts,
        "urgency": "high" if len(reasons) >= 2 else "normal",
        "dual_phase": pattern.dual_phase,
    }
