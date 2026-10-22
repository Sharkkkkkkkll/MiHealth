# -*- coding: utf-8 -*-
"""今晚睡眠计划：AI 每天在"学到的入睡基线前约3小时"生成一次。

为什么锚定基线-3h 而不是固定傍晚：每个人睡点不同（有人白天睡、有人傍晚睡），
且白天数据要足够新才准（小盹回血→可稍晚睡；睡太早→影响明早）。
计划内容：今晚几点睡(T)、为什么、给文案组的一条白天素材。

模式互斥（App 保证 + 服务器双保险）：
  手动填了入睡目标 → AI 不生成不插手，弹点锚定用户时间（零 token 固定骨架）
  打开 AI 自动     → 每天睡前3小时生成计划，弹点锚定计划 T
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import brain
import data_source
import memory
import pattern as pat_mod

PLAN_TTL_H = 10          # 计划有效期：生成后 10 小时内算"今晚的计划"
GEN_LO, GEN_HI = 240, 60  # 基线前 4h ~ 1h 之间生成（3h 左右最准）
PLAN_TTL_H = 10           # （兼容保留；新鲜度主判据改为"覆盖今晚的入睡窗口"）


def _plan_covers_tonight(p: dict) -> bool:
    """计划里 bedtime 字符串 → 绝对分钟（下一个occurrence），判断是否属今晚。"""
    s = str(p.get("bedtime") or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return False
    b = int(m.group(1)) * 60 + int(m.group(2))
    b_abs = _next_occurrence(b, datetime.now().astimezone())
    return 0 <= b_abs - int(datetime.now().astimezone().timestamp() // 60) < 20 * 60


def plan_age_min(prefs: dict):
    """当前 AI 计划：返回 (距生成分钟数, 距计划点剩余分钟)；无有效计划返回 None。
    剩余>20h 视为无效（那是明晚的点，今晚不该被昨日的旧点困住）。"""
    p = prefs.get("tonight_plan") or {}
    s = str(p.get("bedtime") or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    b = int(m.group(1)) * 60 + int(m.group(2))
    now_dt = datetime.now().astimezone()
    remain = _next_occurrence(b, now_dt) - int(now_dt.timestamp() // 60)
    if remain > 20 * 60:
        return None
    try:
        gen = datetime.fromisoformat(p.get("generated_at") or "")
        return (datetime.now() - gen).total_seconds() / 60, remain
    except Exception:
        return None


def _now_min() -> int:
    n = datetime.now()
    return n.hour * 60 + n.minute


def ai_enabled(prefs: dict) -> bool:
    return ((prefs.get("agent_auth") or {}).get("ai_plan")) is True


def manual_bedtime(prefs: dict) -> int | None:
    s = str(((prefs.get("sleep_goal") or {}).get("bedtime")) or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    return (int(m.group(1)) * 60 + int(m.group(2))) % 1440


def fresh_plan(prefs: dict, user_id: str = "") -> dict | None:
    """今晚是否已有有效计划：bedtime 指向今晚且 10 小时内生成（兼容旧字段）。
    新判据（_plan_covers_tonight）让"中午开启AI立刻生成"的计划到晚上仍然有效。"""
    p = prefs.get("tonight_plan") or {}
    if p.get("bedtime") and _plan_covers_tonight(p):
        try:
            gen = datetime.fromisoformat(p.get("generated_at") or "")
            if datetime.now() - gen <= timedelta(hours=PLAN_TTL_H * 3):
                return p
        except Exception:
            pass
    return None


def expired_tonight_plan(prefs: dict) -> dict | None:
    """AI 计划入睡点已过、但当晚（18:00~次日06:00）尚未结束时返回该计划。

    用途：fresh_plan 会把"刚过点的今晚计划"判成明天（_next_occurrence 取到
    明天的同一时刻），导致 tonight_bedtime 回落学习基线、闹钟包被偷偷重建。
    这里识别出它仍是"今晚刚过点的计划"：同晚结束前不回落基线，等真实重规划。
    """
    p = prefs.get("tonight_plan") or {}
    if not (p.get("bedtime") and p.get("date")):
        return None
    if not ai_enabled(prefs):
        return None
    s = str(p.get("bedtime") or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return None
    b = int(m.group(1)) * 60 + int(m.group(2))
    now_dt = datetime.now().astimezone()
    now_min = now_dt.hour * 60 + now_dt.minute
    # 当晚 = 18:00 ~ 次日 06:00
    if not (now_min >= 18 * 60 or now_min < 6 * 60):
        return None
    today = now_dt.strftime("%Y-%m-%d")
    yesterday = (now_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    # date 校验：当天计划；凌晨也接受"昨晚生成、入睡点在凌晨"的计划
    if p.get("date") != today:
        if not (now_min < 6 * 60 and p.get("date") == yesterday):
            return None
    now_abs = int(now_dt.timestamp() // 60)
    prev_abs = now_abs + ((b - now_min) % 1440) - 1440
    passed = now_abs - prev_abs
    # 刚过点且不超过 12h（更早的旧点不算"今晚刚过点的计划"）
    if not (0 < passed <= 12 * 60):
        return None
    return p


def tonight_bedtime(prefs: dict, user_id: str = "") -> tuple:
    """今晚锚点：手动目标 > AI计划 > 学习基线。返回 (分钟, 来源)。"""
    mb = manual_bedtime(prefs)
    if mb is not None:
        return mb, "manual"
    if ai_enabled(prefs):
        p = fresh_plan(prefs, user_id)
        if p:
            # 优先解析字符串字段（_abs_fmt 产物，恒正确）；bedtime_min 曾有时区坑
            s = str(p.get("bedtime") or "")
            m = re.match(r"^(\d{1,2}):(\d{2})$", s.strip())
            if m:
                return (int(m.group(1)) * 60 + int(m.group(2))) % 1440, "plan"
            if p.get("bedtime_min") is not None:
                return int(p["bedtime_min"]), "plan"
        # ★ 2026-10-17 修复：AI 计划刚过点但同晚未结束 → 不回落学习基线，
        #   避免闹钟包被 02:46 这类历史基线偷偷重建（等 maybe_generate 真实重规划）
        if expired_tonight_plan(prefs):
            return None, "expired"
    pat = pat_mod.analyze()
    return pat.bedtime_min, "learned"


def _wake_constraint(prefs: dict, user_id: str = "") -> tuple:
    """明早起床约束：返回 (硬约束起床分钟, 用户设定最早分钟)。

    ★ 起床 = 硬约束（用户说了算）：
      设了 08:00-08:30 起 → 就是必须这个点起（有事情），不拿实际醒来替代。
      实际醒来中位数只作为背景信息给 LLM 参考，不参与硬约束计算。

    返回 (硬约束起床, 用户设定最早)；两者皆无返回 (None, None)。
    """
    s = str(((prefs.get("sleep_goal") or {}).get("wake")) or "")
    m = re.findall(r"(\d{1,2}):(\d{2})", s)
    user_min = None
    if m:
        mins = sorted((int(h) * 60 + int(mm)) % 1440 for h, mm in m)
        user_min = mins[0]          # 硬约束 = 用户设定的最早起床时间
    if user_min is None:
        # 用户没设起床 → 退回实际醒来中位数（唯一兜底）
        pat = pat_mod.analyze()
        learned = pat.waketime_min
        return learned, None
    return user_min, user_min


def _context_text(prefs: dict, user_id: str = "") -> str:
    """给 LLM 的决策素材：清醒压力 + 起床约束 + 基线 + 今天白天。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []
    flag = data_source.sleep_health_flag(user_id=user_id)
    if flag and flag["abnormal"]:
        lines.append("⚠ 异常信号（健康人辅助定位，不做诊断）：" + "；".join(flag["flags"])
                     + "。今晚建议仅供参考，文案需温和提示'长期这样建议看下睡眠门诊'。")
    # —— 清醒压力 + 睡眠质量（最高优先级素材）——
    recs = data_source.main_sleeps(8, user_id)
    if recs:
        last = recs[-1]
        dur = int(last.get("duration_minutes") or 0)
        qf = data_source.quality_factor(last, user_id)
        st = data_source._stages_of(last)
        tot = st["deep"] + st["rem"] + st["light"]
        q_desc = ""
        if tot > 0:
            q_desc = (f"，深睡占{st['deep'] / tot * 100:.0f}%、REM占"
                      f"{st['rem'] / tot * 100:.0f}%，恢复效率{qf:.2f}"
                      + ("（质量偏低：同样时长回电更少，可能喝酒/心事/多梦）"
                         if qf < 0.92 else "（质量不错）" if qf >= 1.0 else ""))
        try:
            wake_dt = datetime.fromisoformat(last["end_at"]).astimezone()
            hrs_awake = (datetime.now().astimezone() - wake_dt).total_seconds() / 3600
            lines.append(f"- 上次睡醒：{(last.get('end_at') or '')[5:16]}，"
                         f"已清醒 {hrs_awake:.1f} 小时（清醒越久睡眠压力越大）")
        except Exception:
            pass
        lines.append(f"- 昨晚睡了 {dur // 60} 小时 {dur % 60} 分{q_desc}"
                     + ("，时长明显不足" if dur < 420 else ""))
        lines.append(f"- 近3夜入睡时刻：{'、'.join((r.get('start_at') or '')[5:16] for r in recs[-3:])}")
        bad = sum(1 for r in recs[-7:] if data_source.quality_factor(r, user_id) < 0.92)
        if bad:
            lines.append(f"- 近7夜有 {bad} 晚质量偏低（深睡不足/多梦），欠的是'恢复'不只是'时长'")
    wc = _wake_constraint(prefs, user_id)
    need_h = _sleep_need_min(user_id)
    ideal_h = _sleep_ideal_min(user_id)
    if wc[0] is not None:
        lines.append(f"- 明早起床硬约束：{wc[0] // 60:02d}:{wc[0] % 60:02d}"
                     + (f"（你设的 {wc[1] // 60:02d}:{wc[1] % 60:02d}，必须这个点起）"
                        if wc[1] is not None and wc[1] != wc[0] else "（按你设定的）")
                     + f" → 最晚入睡(计划锚点P)={_fmt(wc[0] - need_h) if wc[0] >= need_h else _fmt((wc[0] - need_h) % 1440)}"
                     + f"（按现实睡眠需求 {need_h // 60}小时{need_h % 60}分）")
    else:
        lines.append("- 明早起床约束：未设置（按个体睡眠需求倒推）")
    if ideal_h > need_h:
        lines.append(f"- 健康理想：睡 {ideal_h // 60}小时{ideal_h % 60}分 更佳（你实际只睡 {need_h // 60}小时{need_h % 60}分，"
                     "这是长期缺觉的负债，但今天先保证能做到，再逐步向理想靠拢）")
    pat = pat_mod.analyze(user_id=user_id)
    if pat.bedtime_min is not None:
        lines.append(f"- 作息基线（90天学习，仅作参考不是答案）：约 "
                     f"{pat.fmt(pat.bedtime_min)} 入睡 / {pat.fmt(pat.waketime_min)} 醒")
    data = data_source.load(user_id)
    if data:
        da = [r for r in data.get("records", {}).get("daily_activity", [])
              if r.get("date") == today]
        if da:
            r = da[0]
            lines.append(f"- 今天白天：步数 {r.get('steps') or 0}，"
                         f"距离 {round((r.get('distance_m') or 0) / 1000, 1)}km，"
                         f"消耗 {int(r.get('active_kcal') or 0)}kcal")
        naps = _day_sleeps(today)
        if naps:
            ns = "、".join(
                f"{(r.get('start_at') or '')[11:16]}睡{int(r.get('duration_minutes') or 0)}分"
                for r in naps)
            lines.append(f"- 今天小盹：{ns}（已回血，可小幅延后入睡）")
        wos = [r for r in data.get("records", {}).get("workouts", [])
               if (r.get("start_at") or "")[:10] == today]
        if wos:
            lines.append(f"- 今天运动 {len(wos)} 次")
        hr = data_source.resting_hr_by_day(2, user_id)
        if today in hr:
            base = [v for k, v in hr.items() if k != today]
            extra = f"（平时约 {sum(base) / len(base):.0f}）" if base else ""
            lines.append(f"- 今天静息心率均值 {hr[today]:.0f}{extra}")
    return "\n".join(lines) if lines else "（今天暂无数据）"


def _sleep_need_min(user_id: str = "") -> int:
    """个体睡眠需求（现实锚点）：近30天【裸时长】中位数（数据学），
    clamp 到成人健康区间 [7.0h, 8.5h]（吸收 sleep_schedule_method.md Step3）。

    ★ 2026-10-13 融合：旧版下限 6h，等于默认"睡6h就够"，长期慢性睡眠负债。
      新版 clamp 到 [7.0, 8.5]——以用户近期实际睡眠能力为锚（不要凭空按8h推荐），
      但守住健康底线 7h（成人最低需求）。
      若 HR 信号显示睡眠负债（静息HR显著高于历史）→ 下限提到 7.5h（见 _hr_debt_flag）。

    用裸时长（不是有效睡眠）：质量因子是"恢复效率"，已经体现在
    "睡 6.5h 质量好 = 恢复够"里。质量差时通过负债机制（debt_min）
    让推荐提前（欠恢复 → 早睡补），而不是改 need_min 虚高需求。

    注意：这是"用户实际睡多少"的现实值，不是健康理想值。
    健康理想（数据学）单独给 LLM 做引导，硬上限用现实值（保证能做到）。
    """
    recs = data_source.main_sleeps(30, user_id)
    durs = sorted(r.get("duration_minutes") or 0 for r in recs
                  if r.get("duration_minutes"))
    if len(durs) < 7:
        return 420
    med = durs[len(durs) // 2]
    # 健康区间 [7.0h, 8.5h]；HR 负债 → 下限 7.5h
    lo = 450 if _hr_debt_flag() else 420
    return int(max(lo, min(510, med)))


def _sleep_ideal_min(user_id: str = "") -> int:
    """健康理想睡眠时长（LLM 引导用）：近30天【裸时长】中位数（数据学），
    clamp 到 [7.0h, 8.5h]（吸收文件 Step3）。

    不固定 7.5h——每个人的理想时长不一样，从真实数据学：
      睡 6h 就够的人 → 理想 6h；睡 8h 才够的人 → 理想 8h。
    但守住健康底线 7h（成人最低需求），上限 8.5h（睡太多也非最优）。
    用裸时长（不是有效时长）：质量是"恢复效率"，理想是"睡多久才饱"，
    两个维度分开，避免质量满分把理想时长虚高。
    """
    recs = data_source.main_sleeps(30, user_id)
    durs = sorted(r.get("duration_minutes") or 0 for r in recs
                  if r.get("duration_minutes"))
    if len(durs) < 7:
        return 450                       # 数据不足：保守 7.5h
    med = durs[len(durs) // 2]
    return int(max(420, min(510, med)))


def _hr_debt_flag(user_id: str = "") -> bool:
    """HR 睡眠负债信号（吸收文件 Step1/3）：近期静息 HR 显著高于历史均值 → 睡眠负债。

    用 resting_hr_by_day（已有，notifier 在用）：
      近期（近7天）平均静息 HR vs 历史（近90天）平均静息 HR，
      升高 ≥5bpm → 负债信号（身体恢复不足，需要更多睡眠）。
    """
    try:
        by_day = data_source.resting_hr_by_day(90, user_id)
        if not by_day:
            return False
        days = sorted(by_day.keys())
        recent = [by_day[d] for d in days[-7:] if by_day.get(d)]
        hist = [by_day[d] for d in days[:-7] if by_day.get(d)]
        if not recent or not hist:
            return False
        import statistics
        r_avg = statistics.mean(recent)
        h_avg = statistics.mean(hist)
        return (r_avg - h_avg) >= 5.0
    except Exception:
        return False


def _sleepiness_anchor(user_id: str = "") -> tuple:
    """生理困意锚点 S（今晚几点会困）。

    ★ 2026-10-14 算法升级（用户方案，第四次修正）——【累积清醒】口径：
      用户指出：S 应该是"今天这 24 小时我清醒了多久"的累积，不是从某个
      时间点一刀切。白天有小睡时：
        今天总睡眠   = 昨晚主睡 + 今天白天小睡（含清晨补觉）
        应该清醒总时长 = 今天总睡眠 × (平均清醒/平均睡眠) × (昨晚质量/平均质量)
        累积清醒     = 今天所有清醒片段之和（每次醒来→下次睡，最后→现在）
        还需要清醒   = 应该清醒总时长 − 累积清醒
        S            = 现在 + 还需要清醒
      逻辑：
        · 白天小睡多 → 累积清醒少 → 还需要清醒多 → S 后移（补了觉不早困）
        · 昨晚没睡够 → 总睡眠少 → 应该清醒少 → S 提前（缺觉早困）
        · 质量差 → 恢复少 → 应该清醒少 → S 提前
      白天【无】小睡 → 正常流程：S = 醒来 + 平均清醒时长（比例+质量缩放）。

    历史演进：
      v1 固定平均清醒 → v2 比例+质量缩放 → v3 昨晚睡眠+白天小睡合并
      → v4（本次）累积清醒口径，S 反映截止到现在的实时状态。

    返回 (S_clock_min, 今天清醒时长分钟)；数据不足返回 (None, None)。
    """
    now_dt = datetime.now().astimezone()

    # ★ 凌晨保护（2026-10-15 修复）：00:00~06:00 用户还醒着（昨晚计划没执行完），
    #   "醒来+平均清醒"会算出 S=中午 12:29 这种荒谬值（实测：凌晨 00:00:52 重规划
    #   算出 S=12:29 → 起床护栏兜底 03:00）。凌晨的困意锚点 = 现在 + 少量余量
    #   （30min，表示"再撑半小时就困"），让 P/S 双锚点正常收敛到"现在附近"。
    now_h = now_dt.hour
    if 0 <= now_h < 6:
        now_min = now_dt.hour * 60 + now_dt.minute
        s_clock = (now_min + 30) % 1440
        return s_clock, 30

    recs = data_source.main_sleeps(90, user_id)   # 近3个月（主睡眠，>=180min）
    if len(recs) < 7:
        return None, None
    # —— 平均清醒（近3个月：醒来→下次入睡 − 当天白天小睡）——
    awake_mins = []
    for r in recs:
        try:
            st = datetime.fromisoformat(r["start_at"]).astimezone()
            en = datetime.fromisoformat(r["end_at"]).astimezone()
            sm = st.hour * 60 + st.minute
            em = en.hour * 60 + en.minute
            awake = (sm - em) % 1440
            # 白天睡眠抵扣：醒来当天 06:00~21:00 的睡眠（补觉冲掉清醒压力）
            wake_date = en.date()
            nap_m = 0
            for n in _day_sleeps(wake_date.strftime("%Y-%m-%d")):
                nap_m += n.get("duration_minutes") or 0
            awake = max(60, awake - nap_m)   # 至少保留 1h 清醒（防过冲）
            awake_mins.append(awake)
        except Exception:
            continue
    if not awake_mins:
        return None, None
    avg_awake = round(sum(awake_mins) / len(awake_mins))

    # —— 平均睡眠时长（近3个月主睡眠）——
    avg_sleep = round(sum(r.get("duration_minutes") or 0 for r in recs) / len(recs))

    # —— 平均质量系数（近3个月主睡眠）——
    qs = [data_source.quality_factor(r, user_id) for r in recs]
    avg_q = sum(qs) / len(qs) if qs else 1.0

    # —— 昨晚主睡眠（原始记录从后往前找，含短睡<180min）——
    last_dur, last_q, wake_min = _last_sleep_info(user_id)
    if last_dur is None:
        # 取不到昨晚睡眠 → 退回旧算法（固定平均清醒）
        last = recs[-1]
        try:
            wake_dt = datetime.fromisoformat(last["end_at"]).astimezone()
            wake_min = wake_dt.hour * 60 + wake_dt.minute
        except Exception:
            return None, avg_awake
        s_clock = (wake_min + avg_awake) % 1440
        return s_clock, avg_awake

    today = now_dt.strftime("%Y-%m-%d")
    today_nap = sum(r.get("duration_minutes") or 0 for r in _day_sleeps(today, user_id))

    if today_nap > 0:
        # ★ 白天有小睡 → 累积清醒方案（用户 2026-10-14 定案）
        # 1. 今天总睡眠 = 昨晚主睡 + 今天白天小睡
        total_sleep = last_dur + today_nap
        # 2. 应该清醒总时长 = 今天总睡 × 比例 × 质量比
        should_awake = total_sleep * (avg_awake / avg_sleep) * (last_q / avg_q)
        should_awake = round(min(should_awake, avg_awake + 240))   # 封顶
        should_awake = max(60, should_awake)
        # 3. 累积清醒 = 今天所有清醒片段之和（每次醒来→下次睡，最后→现在）
        cum_awake = _cumulative_awake(now_dt, user_id)
        # 4. 还需要清醒 = 应该清醒 − 累积清醒
        remain = should_awake - cum_awake
        # 5. S = 现在 + 还需要清醒（★ 用今天钟表分钟，不是 Unix 时间戳！
        #    旧代码 int(timestamp()//60)%1440 得到的是 1970-01-01 的钟表分钟，
        #    会算错。2026-10-14 修复。）
        now_min = now_dt.hour * 60 + now_dt.minute
        s_clock = (now_min + remain) % 1440
        return s_clock, cum_awake
    else:
        # 白天无小睡 → 正常流程：S = 醒来 + 今天清醒（比例+质量缩放）
        today_awake = last_dur * (avg_awake / avg_sleep) * (last_q / avg_q)
        today_awake = round(min(today_awake, avg_awake + 120))
        today_awake = max(60, today_awake)
        s_clock = (wake_min + today_awake) % 1440
        return s_clock, today_awake


def _cumulative_awake(now_dt, user_id: str = "") -> int:
    """今天累积清醒时长（分钟）：把今天所有清醒片段累加。

    清醒片段 = 每次醒来 → 下次入睡（最后一段 → 现在）。
    用原始睡眠记录（含小睡），从今天 00:00 起：
      · 昨晚主睡醒来（end_at 在今天）→ 今天第一次清醒开始
      · 每次小睡（start/end 在今天）→ 打断清醒，睡完继续
    返回累积清醒分钟数。
    """
    data = data_source.load(user_id)
    if not data:
        return 0
    recs = data_source._sleep_records(data)
    if not recs:
        return 0
    today = now_dt.strftime("%Y-%m-%d")
    now_min = now_dt.hour * 60 + now_dt.minute
    # 收集今天所有"睡眠块"（含昨晚跨午夜的尾部 + 今天小睡）
    sleeps = []   # (start_min, end_min) 当天分钟坐标
    for r in recs:
        try:
            st = datetime.fromisoformat(r["start_at"]).astimezone()
            en = datetime.fromisoformat(r["end_at"]).astimezone()
        except Exception:
            continue
        # 睡眠块与今天有交集才纳入
        if st.date().isoformat() == today or en.date().isoformat() == today:
            # 转成当天分钟坐标（跨午夜：start 可能是昨天）
            s_min = st.hour * 60 + st.minute
            e_min = en.hour * 60 + en.minute
            if st.date().isoformat() != today:
                s_min = -999   # 昨天开始的睡眠 → 今天 00:00 前，标记为"今天开始"
            sleeps.append((max(0, s_min), min(1440, e_min)))
    if not sleeps:
        return 0
    sleeps.sort()
    # 累积清醒 = 总跨度（今天 00:00 → 现在）− 睡眠总时长
    # 但"今天 00:00 前"的睡眠不算（那是昨晚的），只算今天 00:00 后的
    total_span = now_min   # 今天 00:00 → 现在
    sleep_today = 0
    for s, e in sleeps:
        # 只累加今天 00:00 之后的睡眠
        if e <= 0:
            continue
        sleep_today += max(0, min(e, now_min) - max(s, 0))
    cum = total_span - sleep_today
    return max(0, cum)


def _last_sleep_info(user_id: str = "") -> tuple:
    """昨晚主睡眠信息：(时长分钟, 质量系数, 醒来时刻分钟)。

    从原始记录【从后往前】找最近一条主睡眠（含短睡<180min，不过 main_sleeps
    的阈值）——否则"昨晚只睡2h"会被过滤掉，取不到。
    ★ 2026-10-14 修复：旧代码取 recs[-1]（最后一条），但最后一条可能是
      白天小睡（如 16:12~17:03）→ 返回 None → 退回固定平均清醒 → S 算错。
      改为从后往前找：入睡在 18:00~次日06:00（夜间主睡眠窗口，排除清晨补觉
      h=7 和白天小睡 h=15/16），时长>=60min。返回 (None, None, None) 表示取不到。
    """
    data = data_source.load(user_id)
    if not data:
        return None, None, None
    recs = data_source._sleep_records(data)
    if not recs:
        return None, None, None
    # 从后往前找：入睡在 18:00~次日06:00（夜间主睡眠窗口）
    for last in reversed(recs):
        try:
            st = datetime.fromisoformat(last["start_at"]).astimezone()
            en = datetime.fromisoformat(last["end_at"]).astimezone()
        except Exception:
            continue
        h = st.hour
        if not (h >= 18 or h < 6):
            continue                      # 白天小睡/清晨补觉，跳过
        dur = last.get("duration_minutes") or 0
        if dur < 60:
            continue
        q = data_source.quality_factor(last, user_id)
        wake_min = en.hour * 60 + en.minute
        return dur, q, wake_min
    return None, None, None


def _next_occurrence(clock_min: int, now_dt) -> int:
    """clock_min(当日分钟数) 在 now 之后的第一次出现（绝对分钟，跨午夜安全）。"""
    now_abs = int(now_dt.timestamp() // 60)
    m = now_dt.hour * 60 + now_dt.minute
    return now_abs + (clock_min - m) % 1440


def params_changed(prefs: dict) -> bool:
    """AI 规划输入参数是否已变化（起床范围/入睡目标/AI开关）。
    生成计划时存了 params 快照；任一变化 → 旧计划失效，需强制重规划。"""
    p = prefs.get("tonight_plan") or {}
    snap = p.get("params") or {}
    if not snap:                       # 旧计划没存快照 → 保守视为已变化（重规划）
        return True
    cur = {
        "wake": str(((prefs.get("sleep_goal") or {}).get("wake")) or ""),
        "bedtime": str(((prefs.get("sleep_goal") or {}).get("bedtime")) or ""),
        "ai_plan": ai_enabled(prefs),
    }
    return cur != {k: snap.get(k) for k in cur}


def _day_sleeps(today: str | None = None, user_id: str = "") -> list:
    """今天的白天睡眠（小盹/午睡/傍晚补觉/清晨补觉）：start_at 在 06:00~21:00 之间、
    且不是主睡眠（时长 < 3h 或与昨晚主睡眠间隔 > 4h）的记录。
    云端 is_nap 字段不可靠（实测全 0），必须按时间+时长判断。
    ★ 2026-10-14：窗口从 10:00~21:00 放宽到 06:00~21:00——清晨补觉
      （如 07:02~08:11）也是白天睡眠，漏算会导致"今天总睡眠"偏少。
    ★ 2026-10-17：排除"与昨晚主睡眠连续"的记录——否则主睡眠片段（如昨晚
      23:00 睡到今早 07:00，手环拆成 06:30 一条）会被误判成白天小睡，
      导致凌晨 day_sleep_changed 误触发重规划。"""
    data = data_source.load(user_id)
    if not data:
        return []
    today = today or datetime.now().strftime("%Y-%m-%d")
    # 昨晚主睡眠醒来时刻（钟表分钟），用于排除"主睡眠延续片段"
    last_main = _last_sleep_info(user_id)
    last_wake_abs = None
    if last_main[0] is not None:
        try:
            wake_min = last_main[2]
            now_dt = datetime.now().astimezone()
            today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            # 昨晚醒来：今天 wake_min 已过 → 昨天；否则今天
            last_wake = today_start.replace(hour=wake_min // 60, minute=wake_min % 60)
            if last_wake > now_dt:
                last_wake -= timedelta(days=1)
            last_wake_abs = last_wake.timestamp()
        except Exception:
            last_wake_abs = None
    out = []
    for r in data.get("records", {}).get("sleep", []):
        s = r.get("start_at") or ""
        if s[:10] != today:
            continue
        try:
            st = datetime.fromisoformat(s).astimezone()
            hh = st.hour
        except Exception:
            continue
        dur = r.get("duration_minutes") or 0
        # 白天窗口 06:00~21:00；主睡眠（夜间长觉）不在此窗口
        if 6 <= hh <= 21 and 10 <= dur < 240:
            # 排除与昨晚主睡眠连续的记录：这条记录开始时间距昨晚醒来 < 2h
            # （视为主睡眠的延续/清晨补觉，不是独立白天小睡）
            if last_wake_abs is not None:
                gap = (st.timestamp() - last_wake_abs) / 60
                if 0 <= gap < 120:
                    continue
            out.append(r)
    return out


def day_sleep_changed(prefs: dict) -> bool:
    """今天是否出现了新的白天睡眠（相对生成计划时的快照）。
    白天睡了（小盹/午睡）→ 晚上入睡时间要变 → 触发重新规划。"""
    p = prefs.get("tonight_plan") or {}
    snap = p.get("params") or {}
    if not snap:
        return False                    # 无快照：由 params_changed 兜底
    now = datetime.now().strftime("%Y-%m-%d")
    # 快照里的白天睡眠指纹（start_at|duration 列表）
    old = snap.get("day_sleeps") or []
    cur = sorted(f"{r.get('start_at')}|{r.get('duration_minutes')}"
                 for r in _day_sleeps(now))
    changed = cur != old
    if changed:
        print(f"[Plan] day_sleep_changed: old={old} cur={cur}")
    return changed


def _abs_fmt(abs_min: int) -> str:
    dt = datetime.fromtimestamp(abs_min * 60, tz=datetime.now().astimezone().tzinfo)
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _fmt(mins: int) -> str:
    m = mins % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def compute_candidates(prefs: dict, user_id: str = "") -> dict:
    """规则算候选入睡点（LLM 的决策框架）。全程用绝对分钟做坐标，避免跨午夜混乱。

    ★ 双锚点（用户核心设计，2026-10-13 定案）：
      S 生理困意锚点 = 醒来 + 今天清醒时长（比例+质量系数缩放，见 _sleepiness_anchor）
      P 计划睡眠锚点 = 明早硬约束起床 − 理想睡眠时长（起床=硬约束，你说了算）
      P 和 S 之间选一个点，但受"能接受早/晚睡多久"约束，区间以 S 为基准展开：
        · P 更晚（身体先困）→ 区间 [S, S+late]（困意后最多延后 late 分）
        · P 更早（计划先到）→ 区间 [S−early, S]（困意前最多提前 early 分）
      LLM 在区间内权衡（靠近 P 还是靠近 S）。

    ★ 耐受性画像（memory_data/tolerance.json，动态学习）：
      early_shift = 实际入睡 vs 困意时间 S 的提前量平均值（近3个月）
      late_shift  = 实际入睡 vs 困意时间 S 的延后量平均值（近3个月）
      数据不足用保守默认（早30/晚60），样本越多越准。
    """
    pat = pat_mod.analyze(user_id=user_id)
    need = _sleep_need_min(user_id)
    ideal = _sleep_ideal_min(user_id)
    now_dt = datetime.now().astimezone()
    wake_anchor, wake_user = _wake_constraint(prefs, user_id)
    wake = wake_anchor
    if wake is None and pat.waketime_min is not None:
        wake = pat.waketime_min
    tol = memory.get_tolerance(user_id)
    recs = data_source.main_sleeps(14, user_id)

    # —— 锚点 S：生理困意（醒来 + 今天清醒时长，比例+质量缩放）——
    s_clock, med_awake = _sleepiness_anchor(user_id)
    s_abs = _next_occurrence(s_clock, now_dt) if s_clock is not None else None

    # —— 锚点 P：起床倒推（硬约束起床 − 理想时长）——
    p_abs = None
    if wake is not None:
        p_abs = _next_occurrence(wake, now_dt) - ideal

    # —— 融合：以 S（困意）为基准，用耐受性展开选择区间 ——
    # 用户核心逻辑（2026-10-13 明确）：
    #   · P（计划睡眠时间）和 S（身体困意时间）之间选一个点
    #   · 但选择受"能接受早/晚睡多久"约束，区间以 S 为基准展开：
    #       P 更晚（身体先困）→ 在 S 基础上往后延到 S+late，区间 [S, S+late]
    #       P 更早（计划先到）→ 在 S 基础上往前提到 S−early，区间 [S−early, S]
    #   · LLM 在区间内权衡（靠近 P 还是靠近 S）
    early = int(tol.get("early_shift", {}).get("avg_min") or 30)
    late = int(tol.get("late_shift", {}).get("avg_min") or 60)
    now_abs = int(now_dt.timestamp() // 60)
    lo = now_abs + 20
    hi = lo + 240
    f_abs = None
    f_notes = []
    if s_abs is not None:
        if p_abs is not None:
            if p_abs < s_abs:
                # P 更早（计划先到，S 更晚）→ 区间 [S−early, S]，参考点 S−early
                tol_lo = s_abs - early
                tol_hi = s_abs
                f_abs = min(max(p_abs, tol_lo), tol_hi)
                f_notes.append(f"计划({_abs_fmt(p_abs)})早于困意({_abs_fmt(s_abs)})，"
                               f"区间[{_abs_fmt(tol_lo)}, {_abs_fmt(tol_hi)}]"
                               f"（困意前最多提前{early}分），推荐{_abs_fmt(f_abs)}")
            else:
                # P 更晚（身体先困，P 更晚）→ 区间 [S, S+late]，参考点 S+late
                tol_lo = s_abs
                tol_hi = s_abs + late
                f_abs = min(max(p_abs, tol_lo), tol_hi)
                f_notes.append(f"困意({_abs_fmt(s_abs)})早于计划({_abs_fmt(p_abs)})，"
                               f"区间[{_abs_fmt(tol_lo)}, {_abs_fmt(tol_hi)}]"
                               f"（困意后最多延后{late}分），推荐{_abs_fmt(f_abs)}")
        else:
            f_abs = s_abs
            f_notes.append(f"无起床约束，按生理困意({_abs_fmt(s_abs)})推荐")
        lo = max(lo, tol_lo)
        hi = min(max(hi, tol_hi), max(tol_hi, lo))
        hi = max(hi, lo)
    elif p_abs is not None:
        f_abs = p_abs
        f_notes.append("无生理困意数据，按起床倒推推荐")
    if f_abs is not None:
        lo = max(lo, min(f_abs, lo))
        hi = max(hi, f_abs)

    # 起床护栏：推荐不得晚于「起床 − 最短可接受睡眠(5h)」。
    # 依从性不再硬平移推荐点（旧逻辑 f_abs+shift 把 01:42 推到 04:14 是"6:30睡8点起"根因），
    # 只把依从性信息留给 LLM 参考，由 LLM 在可行区间内权衡。
    wake_guard = None
    if wake is not None:
        wake_guard = _next_occurrence(wake, now_dt) - 300   # 起床 − 5h
        hi = min(hi, wake_guard)   # 可行区间上限也压进护栏（LLM 越界校验最多 hi+15，防突破）
        hi = max(hi, lo)           # 防区间倒挂
    if f_abs is not None and wake_guard is not None and f_abs > wake_guard:
        f_abs = wake_guard
        f_notes.append(f"起床护栏：最晚 {_abs_fmt(wake_guard)} 睡（{_abs_fmt(wake)} 起 − 5h），推荐压到 {_abs_fmt(wake_guard)}")

    # D 当日修正：小盹推迟 / 活动量提前（在融合点基础上微调）
    adj, d_notes = 0, []
    data = data_source.load(user_id)
    today = now_dt.strftime("%Y-%m-%d")
    if data:
        nap_min = sum(r.get("duration_minutes") or 0
                      for r in _day_sleeps(today, user_id))
        if nap_min > 45:
            adj += 20
            d_notes.append(f"白天小盹{int(nap_min)}分冲掉部分睡眠压力，可推迟20分钟")
        da = [r for r in data.get("records", {}).get("daily_activity", [])
              if r.get("date") == today]
        steps = da[0].get("steps") or 0 if da else 0
        wos = [r for r in data.get("records", {}).get("workouts", [])
               if (r.get("start_at") or "")[:10] == today]
        if steps > 10000 or wos:
            adj -= 15
            d_notes.append(f"今天活动量大(步数{steps}·运动{len(wos)}次)，可提前15分钟")
    if adj:
        f_abs = f_abs + adj if f_abs is not None else None
        f_notes.extend(d_notes)
    # D 修正后再过一遍起床护栏（小盹推迟可能突破护栏）
    if f_abs is not None and wake_guard is not None and f_abs > wake_guard:
        f_abs = wake_guard
        f_notes.append(f"起床护栏：小盹/活动修正后仍超最晚 {_abs_fmt(wake_guard)}，压回")

    center = f_abs if f_abs is not None else (lo + hi) // 2
    center = min(max(center, lo), hi)
    comp = _compliance(prefs, user_id)   # 依从性仅作 LLM 参考，不再平移推荐点
    return {
        "need_min": need, "ideal_min": ideal,
        "wake_min": wake, "wake_user_min": wake_user,
        "debt_min": int(need - (sum(data_source.effective_minutes(r, user_id)
                                    for r in recs if r.get("duration_minutes")) / len(recs)
                                 if recs else need)),
        "compliance_min": comp,
        "S": _abs_fmt(s_abs) if s_abs else None,          # 生理困意锚点
        "P": _abs_fmt(p_abs) if p_abs else None,          # 起床倒推锚点
        "early_min": early, "late_min": late,             # 耐受范围
        "lo": _abs_fmt(lo), "hi": _abs_fmt(hi), "center": _abs_fmt(center),
        "lo_abs": lo, "hi_abs": hi, "center_abs": center,
        "wake_guard_abs": wake_guard,                     # 起床护栏（绝对分钟）
        "f_notes": f_notes,
    }


def _compliance(prefs: dict, user_id: str = "") -> int | None:
    """依从性：最近计划 vs 实际入睡的平均偏差（正=用户实际比推荐更晚睡）。

    校验：计划 bedtime 必须在正常入睡窗口（20:00~06:00），否则视为脏数据跳过。
    偏差超过 6h 也跳过（跨天匹配错误或异常夜）。
    """
    log = prefs.get("plan_log") or []
    if not log:
        return None
    starts = {}
    for r in data_source.main_sleeps(12, user_id):
        d = (r.get("start_at") or "")[:10]
        m = pat_mod._to_min(r.get("start_at") or "")
        if d and m is not None:
            starts[d] = m
    diffs = []
    for e in log[-6:]:
        d = e.get("date")
        p = e.get("bedtime_min")
        if not d or p is None:
            continue
        # 脏数据校验：计划必须在 20:00~06:00 之间（正常入睡窗口）
        if not (1200 <= p < 1440 or 0 <= p < 360):
            continue
        nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        actual = starts.get(nxt) if nxt in starts else starts.get(d)
        if actual is None:
            continue
        dd = (actual - p) % 1440
        dd = dd - 1440 if dd > 720 else dd
        if abs(dd) > 360:              # 偏差 >6h：跨天匹配错误或异常夜，跳过
            continue
        diffs.append(dd)
    return round(sum(diffs) / len(diffs)) if diffs else None


def _rebuild_tolerance(user_id: str = "") -> None:
    """耐受性画像全量重建（用户 2026-10-13 方案，第二次修正）。

    锚点 = 每天【困意时间 S】= 醒来 + 该天实际清醒时长（醒来→下次入睡，
    最后一条用平均清醒兜底；与 _sleepiness_anchor 的"比例+质量"思路一致）。
    实际入睡 vs 困意时间的偏移：
      · 实际 < 困意时间（比困意早睡）→ early_shift 样本（能接受提前多久睡）
      · 实际 > 困意时间（比困意晚睡）→ late_shift 样本（能接受延后多久睡）
    分别取近3个月样本的平均值，覆盖旧值。

    与旧逻辑的区别：
      · 锚点从"起床倒推(醒来−ideal)"改为"困意时间 S"——用户明确：
        early/late 是看实际睡眠时间和【困意时间】的偏差，不是和起床倒推的偏差。
      · 清醒时长用平均值（用户明确），不用中位数/百分位。
      · 全量重算而非单夜 EMA——一次性修正被污染的画像。
      · 每天只跑一次（generate 入口），样本滚动近3个月，越新越准。
    """
    recs = data_source.main_sleeps(90, user_id)   # 近3个月
    if len(recs) < 7:
        return                       # 数据不足：保持默认（早30/晚60）
    # 平均清醒时长（兜底用：最后一条记录没有"下次入睡"，用平均清醒）
    _, avg_awake = _sleepiness_anchor(user_id)
    if avg_awake is None:
        avg_awake = 1030
    early_s, late_s = [], []
    for i, r in enumerate(recs):
        try:
            st = datetime.fromisoformat(r["start_at"]).astimezone()
            en = datetime.fromisoformat(r["end_at"]).astimezone()
        except Exception:
            continue
        actual = st.hour * 60 + st.minute
        wake = en.hour * 60 + en.minute
        # ★ 2026-10-13：该天困意锚点 S = 醒来 + 该天实际清醒时长
        #   （醒来 → 下次入睡，即相邻记录间隔；最后一条用平均清醒兜底）。
        #   不再用"醒来 + 全局平均清醒"——每条记录自己的清醒时长更准，
        #   与 _sleepiness_anchor 的"比例+质量"思路一致。
        if i + 1 < len(recs):
            try:
                nxt = datetime.fromisoformat(recs[i + 1]["start_at"]).astimezone()
                awake = (nxt - en).total_seconds() / 60
                awake = max(60, min(awake, 24 * 60))   # 1h~24h 合理范围
            except Exception:
                awake = avg_awake
        else:
            awake = avg_awake
        s_clock = (wake + awake) % 1440
        diff = (actual - s_clock) % 1440
        diff = diff - 1440 if diff > 720 else diff
        if abs(diff) > 360:          # 偏差 >6h：异常夜，不学
            continue
        if diff < 0:
            early_s.append(-diff)
        elif diff > 0:
            late_s.append(diff)
    tol = memory.get_tolerance(user_id)
    changed = False
    if early_s:
        avg = round(sum(early_s) / len(early_s))
        tol["early_shift"] = {"max_min": max(30, avg), "avg_min": avg,
                              "samples": len(early_s)}
        changed = True
    if late_s:
        avg = round(sum(late_s) / len(late_s))
        tol["late_shift"] = {"max_min": max(60, avg), "avg_min": avg,
                             "samples": len(late_s)}
        changed = True
    if changed:
        memory.save_tolerance(tol, user_id)
        print(f"[Plan] 🧬 耐受画像重建: early={tol['early_shift']['avg_min']}分"
              f"({tol['early_shift']['samples']}样本) late={tol['late_shift']['avg_min']}分"
              f"({tol['late_shift']['samples']}样本)")


def generate(user_id: str = "") -> dict | None:
    """规则算候选 → LLM 在可行区间内权衡定夺 → 越界回退中心值。"""
    # 直观反馈（用户对照用）：开始规划
    print(f"[Plan][{user_id or 'root'}] 🚀 开始规划入睡时间 @ {datetime.now().strftime('%H:%M:%S')}")
    prefs = memory.get_preferences(user_id)
    # 耐受画像重建（用户方案：起床倒推为锚点，全量重算修正污染值）
    _rebuild_tolerance(user_id)
    tol = memory.get_tolerance(user_id)
    cand = compute_candidates(prefs, user_id)
    comp = _compliance(prefs, user_id)
    comp_line = ""
    if comp is not None:
        if comp > 20:
            comp_line = (f"- 依从性：过去你平均比 AI 推荐晚睡 {comp} 分钟。"
                         "推荐要更现实（往可行区间晚侧靠），别推一个对方做不到的点。\n")
        elif comp < -20:
            comp_line = f"- 依从性：过去你平均比推荐早睡 {-comp} 分钟，身体自己也想早睡。\n"
        else:
            comp_line = "- 依从性：历史推荐与实际情况吻合，照框架放心推。\n"
    prompt = (
        "你是睡眠规划师。规则引擎已按生理模型算出候选值（P/S/区间），"
        "你的职责不是选个数，而是【判断今晚是什么情境】+【定策略】+【给分层建议】。\n"
        + f"- 现实睡眠需求：约 {cand['need_min']//60}小时{cand['need_min']%60}分（你实际能睡到的）\n"
        + f"- 健康理想睡眠：约 {cand['ideal_min']//60}小时{cand['ideal_min']%60}分（数据学，长期目标）\n"
        + (f"- 明早起床硬约束：{_fmt(cand['wake_min'])} → 最晚入睡(计划锚点P)={cand['P']}\n"
           if cand["wake_min"] is not None else "")
        + (f"- 生理困意锚点(S)={cand['S']}：醒来+今天清醒时长（昨晚睡眠×清醒/睡眠比例"
           f"×质量系数缩放，你今晚生理上最早几点会困\n" if cand["S"] else "")
        + (f"- 耐受范围：能接受比推荐早睡{cand['early_min']}分、晚睡{cand['late_min']}分"
           "（数据学，先保证能做到）\n")
        + (f"- 融合说明：" + "；".join(cand["f_notes"]) + "\n" if cand["f_notes"] else "")
        + f"- 可行区间：{cand['lo']} ~ {cand['hi']}，中心参考={cand['center']}\n"
        + (f"- HR负债信号：近期静息心率较历史升高≥5bpm → 睡眠负债，优先保时长，"
           "推荐贴近区间晚侧（多睡）\n" if _hr_debt_flag() else
           "- HR负债信号：无（静息心率平稳，无显著睡眠负债）\n")
        + comp_line
        + "\n" + _context_text(prefs, user_id) + "\n\n"
        + "使命（最重要）：这不是纪律命令，是引导。目标是让用户明天的状态更好、"
        "并一步步把他的生物钟引向更健康，而不是一步到位的硬性要求。\n"
        "第一步【判断今晚情境】（从以下选一个，这是你的核心价值）：\n"
        "  - 正常夜：睡够+质量好+无负债 → 策略：温和引导生物钟往前（推荐贴近P）\n"
        "  - 负债夜：HR升高或昨晚没睡够 → 策略：优先保睡眠时长，推荐贴近区间晚侧（多睡）\n"
        "  - 质量差夜：睡了但深睡少/恢复差 → 策略：建议早睡补恢复（推荐贴近P）\n"
        "  - 硬事夜：明天有重要事必须早起 → 策略：保起床，给保底方案\n"
        "第二步【输出建议】（都在可行区间内）：\n"
        "  - ideal_bedtime：理想睡着时刻（健康目标，贴近P但不得早于区间下限lo）\n"
        "  - fallback_bedtime：可行保底时刻（保底6h，做不到理想时的退路，"
        "    最晚 = 起床 − 6h，且不得晚于区间上限hi）\n"
        "  （上床时刻由规则引擎算：入睡 − 30min，你不用输出）\n"
        "关键原则：\n"
        "1. 起床是【硬约束】——用户设了 08:00 起就是必须这个点起（有事情），"
        "计划锚点 P 是明天状态最好的目标，推荐必须落在可行区间内。\n"
        "2. 生理困意锚点 S 是【基准】——身体几点会困。选择区间以 S 为基准展开："
        "P 更晚（身体先困）→ 区间 [S, S+late]；P 更早（计划先到）→ 区间 [S−early, S]。\n"
        "3. 推荐在 [P, S] 之间权衡——P 是明天状态最好的点，S 是身体能自然入睡的点。"
        "区间由耐受性（early/late）以 S 为基准框定，推荐尽量贴近 P，但不得突破区间。\n"
        "4. 健康理想（数据学）是【长期引导】——如果用户现实已经接近理想就推理想值；"
        "如果现实差得远，就推一个比现实【稍微早一点】的点（每天≤45分钟温和拨钟），"
        "让用户容易做到、正反馈循环，而不是一步到位。\n"
        "5. 依从性仅作背景参考——不要因为历史晚睡就大幅推迟推荐，"
        "推荐仍以 [P, S] 和耐受区间为准，温和引导。\n"
        "6. 质量差的夜晚（深睡少/多梦）优先保证时长而非提早太多。\n"
        "生理优先级：能睡着(S耐受) > 睡够(P硬约束) > 生物钟温和纠偏 > 现实依从性。\n"
        '只输出 JSON：{"situation":"正常夜|负债夜|质量差夜|硬事夜",'
        '"ideal_bedtime":"HH:MM","fallback_bedtime":"HH:MM",'
        '"reason":"一句话依据(引用具体候选值)",'
        '"note_for_copy":"给晚间文案组的一条白天素材"}'
    )
    r = brain.ask(prompt, snapshot=False, temperature=0.4, timeout=60,
                  use_history=False, tag="plan", lean=True, user_id=user_id)
    b_abs = None
    reason = note = ""
    situation = "正常夜"
    fallback_abs = None
    inbed_abs = None
    if r["ok"]:
        m = re.search(r"\{.*\}", r["reply"], re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                hh, mm = str(obj["ideal_bedtime"] or obj["bedtime"]).strip().split(":")
                bmin = (int(hh) * 60 + int(mm)) % 1440
                b_abs = _next_occurrence(bmin, datetime.now().astimezone())
                reason = str(obj.get("reason") or "")[:120]
                note = str(obj.get("note_for_copy") or "")[:120]
                situation = str(obj.get("situation") or "正常夜")[:10]
                # 保底时刻（可选）：解析并校验在区间内
                fb = str(obj.get("fallback_bedtime") or "").strip()
                if fb:
                    fh, fm = fb.split(":")
                    fbmin = (int(fh) * 60 + int(fm)) % 1440
                    fallback_abs = _next_occurrence(fbmin, datetime.now().astimezone())
                # ★ 2026-10-14：上床时刻不再用 LLM 输出（会算出 16:51 荒谬值），
                #   改为规则计算：in_bed_by = 入睡 − 30min（潜伏期20min + 10min余量）
            except Exception:
                b_abs = None
    if b_abs is None:
        # LLM 失败/解析失败：直接采用规则中心值（系统照转，不依赖网络）
        b_abs = cand["center_abs"]
        reason = f"规则中心值（S={cand['S']} P={cand['P']}，区间{cand['lo']}~{cand['hi']}）"
        note = ""
        # 上床时刻（规则计算）：入睡 − 30min
        inbed_abs = b_abs - 30
    else:
        # 越界校验：不得早于 lo，最多晚于 hi 15 分钟；且不得突破起床护栏
        b_abs = max(cand["lo_abs"], min(cand["hi_abs"] + 15, b_abs))
        guard = cand.get("wake_guard_abs")
        if guard is not None and b_abs > guard:
            b_abs = guard
        # 保底时刻校验：不得早于 lo，不得晚于起床护栏（保底6h）
        if fallback_abs is not None:
            fallback_abs = max(cand["lo_abs"], min(cand["hi_abs"] + 15, fallback_abs))
            if guard is not None and fallback_abs > guard:
                fallback_abs = guard
        # ★ 上床时刻（规则计算，不依赖 LLM）：入睡 − 30min（潜伏期20min + 10min余量）
        #   用户 2026-10-14 明确：上床时间以入睡时间为准，提前 30min 上床
        inbed_abs = b_abs - 30
    # 绝对分钟 → 本地钟表分钟（复用 _abs_fmt，避免 UTC/本地混用）
    _h, _m = _abs_fmt(b_abs).split(":")
    bmin = int(_h) * 60 + int(_m)
    plan = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "bedtime": _abs_fmt(b_abs),
        "bedtime_min": bmin,
        "reason": reason,
        "note": note,
        "situation": situation,
        "fallback": _abs_fmt(fallback_abs) if fallback_abs else None,
        "in_bed_by": _abs_fmt(inbed_abs) if inbed_abs else None,
        "cand": {k: cand[k] for k in ("S", "P", "early_min", "late_min", "lo", "hi", "center", "debt_min")},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {   # 生成时的输入参数快照：任一变化 → 计划失效需重规划
            "wake": str(((prefs.get("sleep_goal") or {}).get("wake")) or ""),
            "bedtime": str(((prefs.get("sleep_goal") or {}).get("bedtime")) or ""),
            "ai_plan": ai_enabled(prefs),
            "day_sleeps": sorted(
                f"{r.get('start_at')}|{r.get('duration_minutes')}"
                for r in _day_sleeps()),
        },
    }
    log = [e for e in (prefs.get("plan_log") or [])
           if e.get("date") != plan["date"]]
    log.append({"date": plan["date"], "bedtime_min": bmin})
    memory.update_preferences_shallow({"tonight_plan": plan, "plan_log": log[-14:]},
                                      user_id)
    # 关键：AI 规划后【立即重建闹钟包】——与手动输入同一套流程。
    # get_or_build_pack 检测 bed_min 变化 → 重建包(模板版先顶上) + 同步触发
    # pregen_next(只生成最近的 1 条, 带 _REFILLING 防重入) → 手机拉包排闹钟。
    # 不做这步，闹钟包要靠 maybe_prewarm(窗口激活才跑) 才重建，
    # AI 规划好了也可能不弹闹钟（用户担心的点）。
    try:
        import notifier
        pack = notifier.get_or_build_pack(user_id)
    except Exception:
        pack = None
    # 直观反馈（用户对照用）：规划完成 + 闹钟包已重建（手机下一拍拉包排闹钟）
    print(f"[Plan][{user_id or 'root'}] ✅ 规划完成 {plan['bedtime']} (reason={reason[:30]}) "
          f"@ {datetime.now().strftime('%H:%M:%S')}")
    _c = plan.get("cand") or {}
    print(f"[Plan][{user_id or 'root'}] 📋 详情: 情景={plan.get('situation')} "
          f"入睡={plan['bedtime']} 保底={plan.get('fallback')} 上床={plan.get('in_bed_by')} "
          f"| 候选 S={_c.get('S')} P={_c.get('P')} 区间={_c.get('lo')}~{_c.get('hi')} "
          f"中心={_c.get('center')} 负债={_c.get('debt_min')}分 | "
          f"reason={plan.get('reason')} note={plan.get('note')}")
    print(f"[Plan][{user_id or 'root'}] 📦 闹钟包已重建(version={pack.get('version') if pack else '?'}) "
          f"→ 手机拉包排闹钟")
    return plan


def maybe_generate(user_id: str = "") -> dict | None:
    """调度线程每拍调用（窗口内20s/外5min）。事件驱动，不轮询刷新：

    触发条件（满足任一即生成，否则零成本返回）：
      ① 新的一天（date 变了）→ 生成今晚计划（早上起床后第一拍就定今晚几点睡）
      ② 输入参数变了（起床范围/入睡目标/AI开关）→ 强制重新规划
      ③ 今天新增了白天睡眠（小盹/午睡）→ 白天睡了，晚上入睡时间要跟着变
      ④ AI 模式 + 今晚还没计划 → 立即生成（打开开关就能见到结果）
      ⑤ 关闭AI/手动模式 → 不插手

    ★ 关闭 AI（2026-10-13 修复）：AI 开关关闭时【清掉已生成的 tonight_plan 和
      alarm_pack】——用户关了就不该再有 AI 规划，也不该再弹 AI 闹钟。
      否则旧计划残留，手机端还会继续响。

    不再有"计划超过45分钟就刷新"的轮询——JSON 不一定更新，白天没睡觉
    就不需要改晚上睡眠时间，每几分钟重读 JSON 重规划纯属浪费 token。
    """
    prefs = memory.get_preferences(user_id)
    if manual_bedtime(prefs) is not None:
        # 手动模式：AI 不插手；若残留 AI 计划，清掉（避免旧计划影响手动闹钟）
        if (prefs.get("tonight_plan") or {}).get("date") == datetime.now().strftime("%Y-%m-%d"):
            memory.update_preferences_shallow({"tonight_plan": {}}, user_id)
        return None
    if not ai_enabled(prefs):
        # ★ AI 关闭：清掉今晚 AI 计划 + 闹钟包（用户关了就不该再弹）
        p = prefs.get("tonight_plan") or {}
        pack = prefs.get("alarm_pack") or {}
        today = datetime.now().strftime("%Y-%m-%d")
        changed = False
        if p.get("date") == today:
            memory.update_preferences_shallow({"tonight_plan": {}}, user_id)
            changed = True
        if pack.get("items"):
            empty = {"date": today, "bedtime": None, "bed_min": None,
                     "version": int(datetime.now().timestamp()), "items": []}
            memory.update_preferences_shallow({"alarm_pack": empty}, user_id)
            changed = True
        if changed:
            print(f"[Plan][{user_id or 'root'}] 🚫 AI 规划已关闭，清空今晚 AI 计划与闹钟包")
        return None
    pat = pat_mod.analyze(user_id=user_id)
    if pat.bedtime_min is None:
        return None
    p = prefs.get("tonight_plan") or {}
    today = datetime.now().strftime("%Y-%m-%d")

    # ★ 凌晨保护窗口（2026-10-15 修复）：00:00~06:00 之间，昨晚的"今晚计划"
    #   还没执行完（用户可能 00:23 才睡），此时"新的一天"触发重规划会覆盖掉
    #   正在执行的计划（实测：23:52 弹完 P30，00:00:52 被重规划成 03:00，
    #   闹钟包重建，用户困惑）。凌晨只允许：
    #     · 手动触发（plan_now，走 /api/agent/plan_now 不经过这里）
    #     · 参数变化（起床/入睡目标/AI开关，params_changed）
    #     · 今天新增白天睡眠（day_sleep_changed，凌晨不可能有）
    #   禁止"新的一天"自动重规划——旧计划（昨晚生成的）继续执行到 06:00。
    #   06:00 之后旧计划已过期（入睡点早过了），正常重规划今晚。
    now_h = datetime.now().hour
    is_early_morning = 0 <= now_h < 6
    if is_early_morning and p.get("bedtime"):
        # 凌晨 + 已有计划 → 只响应参数变化/白天睡眠，不因"新的一天"重规划
        if p.get("date") == today:
            if params_changed(prefs):
                return generate(user_id)
            if day_sleep_changed(prefs):
                return generate(user_id)
            return None
        # 凌晨 + 计划是昨天的（date 是昨天）→ 视为"昨晚的计划"继续执行
        if params_changed(prefs):
            return generate(user_id)
        if day_sleep_changed(prefs):
            return generate(user_id)
        return None

    # ★ 2026-10-17 修复：AI 模式 + 今晚有计划 + 计划入睡点已过（当晚未结束）
    #   → 真实重新规划一次，而不是让闹钟包回落学习基线偷偷重建。
    #   注意：00:00~06:00 仍走上面的凌晨保护，不在这里重规划（用户可能刚睡）。
    if p.get("bedtime") and p.get("date") == today and expired_tonight_plan(prefs):
        print(f"[Plan][{user_id or 'root'}] 计划入睡点已过且当晚未结束，重新规划")
        return generate(user_id)

    # ④ 今晚还没计划 → 立即生成
    if not p.get("bedtime") or p.get("date") != today:
        return generate(user_id)
    # ② 输入参数变了（起床时间/入睡目标/AI开关）
    if params_changed(prefs):
        return generate(user_id)
    # ③ 今天新增了白天睡眠 → 重新规划
    if day_sleep_changed(prefs):
        return generate(user_id)
    return None
