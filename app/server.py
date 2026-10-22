#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""小米手环数据可视化本地服务。

读取 D:\MIwatchAI\JSON\ 中**最新**的导出 JSON（即 pull_to_json.py 的产物），
预处理/聚合为轻量图表数据，通过 /api/data 提供给网页仪表盘。
仪表盘刷新即自动呈现最新数据；也可点「重新同步」让本服务直接调用
pull_to_json.py 重新拉取（复用钥匙串里缓存的 token）。

运行：
    C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe D:\MIwatchAI\app\server.py
然后浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import defaultdict
from datetime import date as _date, datetime, timedelta as _td
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------- 路径 ----------
EXPORT_DIR = r"D:\MIwatchAI\JSON"                       # pull_to_json.py 默认导出目录
PULL_SCRIPT = r"D:\MIwatchAI\pull_to_json.py"          # 联动的拉数脚本
PY = r"C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe"
ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "index.html")

# 缓存：仅当最新 JSON 的 mtime 变化时才重新解析（避免每次请求都读 64MB）
# 多用户（方案 A）：按 user_id 分 key 缓存，互不污染。
_cache = {}
_pull_proc: dict = {}
_pull_lock = threading.Lock()


def export_dir_for(user_id: str = "") -> str:
    """按 user_id 返回数据源 JSON 子目录；空 → 根 JSON（兼容单用户旧数据）。"""
    uid = (user_id or "").strip()
    if not uid:
        return EXPORT_DIR
    return os.path.join(EXPORT_DIR, uid)


# ---------- 工具 ----------
def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def find_latest_json(user_id: str = ""):
    d = export_dir_for(user_id)
    if not os.path.isdir(d):
        # 账号目录不存在 → 回退根目录（兼容旧数据/未拉取账号）
        d = EXPORT_DIR
    cands = [os.path.join(d, f) for f in os.listdir(d)]
    cands = [c for c in cands if c.lower().endswith(".json")]
    if cands:
        return max(cands, key=os.path.getmtime)
    # 账号目录存在但为空 → 也回退根目录
    if user_id and os.path.isdir(export_dir_for(user_id)):
        d = EXPORT_DIR
        cands = [os.path.join(d, f) for f in os.listdir(d)]
        cands = [c for c in cands if c.lower().endswith(".json")]
        if cands:
            return max(cands, key=os.path.getmtime)
    return None


# ---------- 睡眠板块工具 ----------
def _is_nap(r):
    """白天 10:00–17:59 视为小睡/打盹（启发式）。"""
    t = r.get("start_at") or ""
    h = int(t[11:13]) if len(t) >= 13 else -1
    return 10 <= h <= 17


def _day_sleeps_agent(recs: list, today: str) -> list:
    """今天的白天睡眠（与 Agent planner._day_sleeps 同口径）：
    start_at 在 06:00~21:00、时长 10~240min 的记录（清晨补觉也算）。
    云端 is_nap 字段不可靠（实测全 0），必须按时间+时长判断。"""
    out = []
    for r in recs:
        s = r.get("start_at") or ""
        if s[:10] != today:
            continue
        try:
            hh = int(s[11:13])
        except Exception:
            continue
        dur = r.get("duration_minutes") or r.get("time_asleep_minutes") or 0
        if 6 <= hh <= 21 and 10 <= dur < 240:
            out.append(r)
    return out


def _last_main_sleep(recs: list, user_id: str = ""):
    """昨晚主睡眠（与 Agent planner._last_sleep_info 同口径）：
    从后往前找入睡在 18:00~次日06:00、时长>=60min 的最近一条。
    返回 (时长分钟, 质量系数, 醒来时刻分钟)；取不到返回 (None, None, None)。"""
    for last in reversed(recs):
        t = last.get("start_at") or ""
        if len(t) < 13:
            continue
        h = int(t[11:13])
        if not (h >= 18 or h < 6):
            continue
        dur = last.get("duration_minutes") or last.get("time_asleep_minutes") or 0
        if dur < 60:
            continue
        q = _recovery_factor(last, user_id)
        try:
            en = datetime.fromisoformat(last["end_at"])
        except Exception:
            continue
        wake_min = en.hour * 60 + en.minute
        return dur, q, wake_min
    return None, None, None


def _sleep_stats(recs: list, user_id: str = ""):
    """近3月主睡眠统计：返回 (avg_awake, avg_sleep, avg_q, mains) 或 None。
    抽成公共函数，凌晨/非凌晨分支复用，避免改一处漏另一处。"""
    best: dict = {}
    for r in recs:
        if r.get("is_nap"):
            continue
        k = r.get("start_at") or ""
        if not k:
            continue
        cur = best.get(k)
        if cur is None or (r.get("duration_minutes") or 0) > (cur.get("duration_minutes") or 0):
            best[k] = r
    mains = [r for r in best.values() if (r.get("duration_minutes") or 0) >= 180]
    mains.sort(key=lambda r: r.get("start_at", ""))
    mains = mains[-90:]
    if len(mains) < 7:
        return None

    awake_mins = []
    for r in mains:
        try:
            st = datetime.fromisoformat(r["start_at"])
            en = datetime.fromisoformat(r["end_at"])
            sm = st.hour * 60 + st.minute
            em = en.hour * 60 + en.minute
            awake = (sm - em) % 1440
            wake_date = en.date().isoformat()
            nap_m = sum(n.get("duration_minutes") or n.get("time_asleep_minutes") or 0
                        for n in _day_sleeps_agent(recs, wake_date))
            awake = max(60, awake - nap_m)
            awake_mins.append(awake)
        except Exception:
            continue
    if not awake_mins:
        return None
    avg_awake = sum(awake_mins) // len(awake_mins)
    avg_sleep = sum(r.get("duration_minutes") or 0 for r in mains) // len(mains)
    qs = [_recovery_factor(r, user_id) for r in mains]
    avg_q = sum(qs) / len(qs) if qs else 1.0
    return avg_awake, avg_sleep, avg_q, mains


def _cumulative_awake(recs: list, now_dt) -> int:
    """今天累积清醒时长（分钟，与 Agent planner._cumulative_awake 同口径）：
    把今天所有清醒片段累加（每次醒来→下次睡，最后→现在）。
    清醒 = 今天 00:00 → 现在 的总跨度 − 今天 00:00 后的睡眠时长。"""
    today = now_dt.strftime("%Y-%m-%d")
    now_min = now_dt.hour * 60 + now_dt.minute
    sleeps = []   # (start_min, end_min) 当天分钟坐标
    for r in recs:
        try:
            st = datetime.fromisoformat(r["start_at"])
            en = datetime.fromisoformat(r["end_at"])
        except Exception:
            continue
        if st.date().isoformat() != today and en.date().isoformat() != today:
            continue
        s_min = st.hour * 60 + st.minute
        e_min = en.hour * 60 + en.minute
        if st.date().isoformat() != today:
            s_min = -999   # 昨天开始的睡眠 → 今天 00:00 前
        sleeps.append((max(0, s_min), min(1440, e_min)))
    if not sleeps:
        # 今天没有任何睡眠记录 → 从 00:00 到现在都是清醒
        return now_min
    sleep_today = 0
    for s, e in sleeps:
        if e <= 0:
            continue
        sleep_today += max(0, min(e, now_min) - max(s, 0))
    return max(0, now_min - sleep_today)


def _energy_plan(recs: list, user_id: str = "") -> dict | None:
    """精力百分比（与 Agent planner._sleepiness_anchor 同口径）：
      精力 = (应该清醒总时长 − 累积清醒) / 应该清醒总时长 × 100%
    应该清醒总时长 = 今天总睡（昨晚主睡+今天小睡）× 平均清醒/平均睡眠 × 质量比
    到达困意时间 S 时精力 ≈ 0（显示 5% 以内）。
    返回 {should_awake, cum_awake, energy_pct, s_clock}；数据不足返回 None。"""
    try:
        now_dt = datetime.now()

        # ★ 凌晨保护（2026-10-15 与 Agent planner._sleepiness_anchor 同口径）：
        #   00:00~06:00 用户还醒着，"醒来+平均清醒"会算出 S=中午 12:29 荒谬值。
        #   凌晨精力 = 现在 + 30min 余量（再撑半小时就困）。
        now_h = now_dt.hour
        if 0 <= now_h < 6:
            now_min = now_dt.hour * 60 + now_dt.minute
            s_clock = (now_min + 30) % 1440
            # ★ 凌晨精力（2026-10-16 用户确认方案）：
            #   - 应该清醒总时长 = 今天总睡（昨晚主睡 + 今天小睡）× 平均清醒/平均睡眠 × 质量比
            #   - 累积清醒 = (现在 − 昨晚主睡醒来) − 今天小睡总时长
            #     （不是"最后一次醒来→现在"，否则下午小睡后精力会虚高）
            #   - 精力 = (应该清醒 − 累积清醒) / 应该清醒 × 100%，兜底 5%
            #   凌晨 S 锚点仍用 现在+30min（防 S 算成中午荒谬值）。
            stats = _sleep_stats(recs, user_id)
            if stats is None:
                return None
            avg_awake, avg_sleep, avg_q, _mains = stats

            last_dur, last_q, wake_min = _last_main_sleep(recs, user_id)
            if last_dur is None:
                return None
            today = now_dt.strftime("%Y-%m-%d")
            today_nap = sum(r.get("duration_minutes") or r.get("time_asleep_minutes") or 0
                            for r in _day_sleeps_agent(recs, today))
            total_sleep = last_dur + today_nap
            should = total_sleep * (avg_awake / avg_sleep) * (last_q / avg_q)
            should = max(60, min(round(should), avg_awake + 240))

            # 累积清醒 = (现在 − 昨晚主睡醒来) − 今天小睡总时长
            now_abs_min = int(now_dt.timestamp() // 60)
            last_wake_abs = now_abs_min - (now_min - wake_min) % 1440
            if last_wake_abs > now_abs_min:
                last_wake_abs -= 1440
            cum = max(0, (now_abs_min - last_wake_abs) - today_nap)
            energy = (should - min(cum, should)) / should * 100 if should > 0 else 0.0
            energy = max(5.0, min(100.0, round(energy, 1)))
            return {
                "should_awake": should,
                "cum_awake": min(cum, should),
                "energy_pct": energy,
                "s_clock": s_clock,
            }

        # 近3月主睡眠统计（公共函数）
        stats = _sleep_stats(recs, user_id)
        if stats is None:
            return None
        avg_awake, avg_sleep, avg_q, _mains = stats

        last_dur, last_q, wake_min = _last_main_sleep(recs, user_id)
        if last_dur is None:
            return None

        today = now_dt.strftime("%Y-%m-%d")
        today_nap = sum(r.get("duration_minutes") or r.get("time_asleep_minutes") or 0
                        for r in _day_sleeps_agent(recs, today))
        cum = _cumulative_awake(recs, now_dt)
        if today_nap > 0:
            # 白天有小睡 → 累积清醒方案
            total_sleep = last_dur + today_nap
            should = total_sleep * (avg_awake / avg_sleep) * (last_q / avg_q)
            should = max(60, min(round(should), avg_awake + 240))
            remain = should - cum
            now_min = now_dt.hour * 60 + now_dt.minute
            s_clock = (now_min + remain) % 1440
        else:
            # 白天无小睡 → 正常流程：S = 醒来 + 今天清醒
            today_awake = last_dur * (avg_awake / avg_sleep) * (last_q / avg_q)
            today_awake = max(60, min(round(today_awake), avg_awake + 240))
            should = today_awake
            cum = min(cum, should)
            s_clock = (wake_min + today_awake) % 1440

        energy = (should - min(cum, should)) / should * 100 if should > 0 else 0.0
        energy = max(5.0, min(100.0, round(energy, 1)))
        return {
            "should_awake": should,
            "cum_awake": min(cum, should),
            "energy_pct": round(energy, 1),
            "s_clock": s_clock,
        }
    except Exception:
        return None


def _get_stages(r):
    """stages 字段可能是 stringified JSON，统一解析为 list[{stage,minutes}]。"""
    st = r.get("stages")
    if isinstance(st, str):
        try:
            st = json.loads(st)
        except Exception:
            return []
    return st or []


def _stages_dict(r):
    """解析分期分钟数 {deep, rem, light}（与 Agent data_source._stages_of 同口径）。
    ★ 2026-10-14 修复：漏了 light 映射导致 light 恒 0、深睡占比虚高、
      质量系数 100% 顶格 1.15（无区分度）。"""
    out = {"deep": 0, "rem": 0, "light": 0}
    for s in _get_stages(r):
        if not isinstance(s, dict):
            continue
        k = {"deep": "deep", "rem": "rem", "dream": "rem",
             "light": "light"}.get(s.get("stage"))
        if k:
            out[k] += s.get("minutes") or 0
    return out


def _recovery_factor(r, user_id: str = ""):
    """睡眠恢复效率 0.70~1.15：深睡占比为主、REM/评分为辅。
    同样睡8h，深睡足→回满；喝酒/心事→浅睡长深睡短→回得少。
    ★ 2026-10-14 个人基准标定（与 Agent data_source.quality_factor 同口径）：
      旧公式用通用人群基准（deep 17%/rem 22%），对高深睡占比用户
      （实测均值 54.7%）永远顶格 1.15，无区分度。
      新公式用【个人近3个月均值】做基准：
        q = 1.0 + (deep_ratio − 个人平均deep) × 1.2
                + (rem_ratio  − 个人平均rem)  × 0.6
                + (score      − 个人平均score) × 0.0025
      个人深睡占比 54.7% 是"你的常态" → q≈1.0；
      某晚深睡 40%（低于常态）→ q<1.0（质量差）；
      某晚深睡 60%（高于常态）→ q>1.0（质量好）。"""
    st = _stages_dict(r)
    tot = st["deep"] + st["rem"] + st["light"]
    sc = r.get("sleep_score")
    if tot <= 0:
        return 1.0 if not sc else max(0.75, min(1.10, 0.6 + sc / 200))
    base = _quality_baseline(user_id)
    f = 1.0 + (st["deep"] / tot - base["deep"]) * 1.2 \
            + (st["rem"] / tot - base["rem"]) * 0.6
    if sc:
        f += (sc - base["score"]) * 0.0025
    return round(max(0.70, min(1.15, f)), 2)


_QB_CACHE = {}


def _quality_baseline(user_id: str = ""):
    """个人睡眠质量基准：近3个月主睡眠的 deep/rem 占比与评分均值。
    用 mtime 缓存（与 find_latest_json 同 key），避免每条记录都重算。"""
    path = find_latest_json(user_id)
    if not path:
        return {"deep": 0.17, "rem": 0.22, "score": 70}
    key = (user_id, path, os.path.getmtime(path))
    qb = _QB_CACHE.get(key)
    if qb is not None:
        return qb
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    recs = [r for r in data.get("records", {}).get("sleep", [])
            if not r.get("is_nap") and (r.get("duration_minutes") or 0) >= 180][-90:]
    deeps, rems, scores = [], [], []
    for r in recs:
        st = _stages_dict(r)
        tot = st["deep"] + st["rem"] + st["light"]
        if tot <= 0:
            continue
        deeps.append(st["deep"] / tot)
        rems.append(st["rem"] / tot)
        if r.get("sleep_score"):
            scores.append(r["sleep_score"])
    base = {
        "deep": sum(deeps) / len(deeps) if deeps else 0.17,
        "rem": sum(rems) / len(rems) if rems else 0.22,
        "score": sum(scores) / len(scores) if scores else 70,
    }
    _QB_CACHE[key] = base
    return base


def _fmt_hm(minutes):
    """分钟 -> 'X小时Y分钟'（不足1小时仅显示分钟）。"""
    if minutes is None:
        return "—"
    minutes = int(round(minutes))
    h, m = divmod(minutes, 60)
    return f"{h}小时{m}分钟" if h else f"{m}分钟"


def _score_label(s):
    if s is None:
        return "—"
    if s >= 85:
        return "优秀"
    if s >= 75:
        return "良好"
    if s >= 60:
        return "一般"
    return "偏差"


# ---------- 聚合 ----------
def aggregate(path: str, user_id: str = "") -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("records", {})

    # 全跨度连续日期（以 daily_activity 起止为准）
    da = sorted(recs.get("daily_activity", []), key=lambda r: r.get("date") or "")
    if da:
        d0 = min(r["date"] for r in da if r.get("date"))
        d1 = max(r["date"] for r in da if r.get("date"))
        span = [(_date.fromisoformat(d0) + _td(days=i)).isoformat()
                for i in range((_date.fromisoformat(d1) - _date.fromisoformat(d0)).days + 1)]
    else:
        span = []

    out = {
        "source": data.get("source"),
        "generated_at": data.get("generated_at"),
        "file": os.path.basename(path),
        "span": span,                 # 连续日期轴，缺失日填 null -> 图表断点
        "summary": {},
        "steps": {"labels": [], "values": [], "avg7": []},
        "sleep": {"labels": [], "duration": [], "score": [],
                  "stages_avg": {"deep": 0, "rem": 0, "light": 0}},
        "resting_hr": {"labels": [], "values": []},
        "spo2": {"labels": [], "values": [], "avg": None, "low_pct": None},
        "stress": {"labels": [], "values": [], "levels": {}},
        "workouts": [],
    }

    def _daymap(pairs):
        m = defaultdict(list)
        for d, v in pairs:
            if d:
                m[d[:10]].append(v)
        return m

    def _align(m, reducer="mean"):
        res = []
        for d in span:
            if d in m and m[d]:
                xs = [x for x in m[d] if x is not None]
                if not xs:
                    res.append(None)
                elif reducer == "mean":
                    res.append(round(sum(xs) / len(xs), 2))
                else:
                    res.append(xs[0])
            else:
                res.append(None)
        return res

    # ---- 步数 ----
    steps_map = _daymap([(r.get("date"), r.get("steps")) for r in da])
    out["steps"]["labels"] = span
    out["steps"]["values"] = _align(steps_map)
    vals = out["steps"]["values"]
    out["steps"]["avg7"] = [
        round(_mean([v for v in vals[max(0, i - 6):i + 1] if v is not None]))
        if any(v is not None for v in vals[max(0, i - 6):i + 1]) else None
        for i in range(len(vals))
    ]
    if da:
        out["summary"]["avg_steps"] = _mean([r.get("steps") for r in da])
        out["summary"]["total_distance_km"] = round(
            sum(r.get("distance_m", 0) or 0 for r in da) / 1000, 1)
        out["summary"]["total_kcal"] = sum(r.get("active_kcal", 0) or 0 for r in da)
        out["summary"]["days"] = len(da)
        # 今日体力条数据：今日活跃消耗 + 历史最大单日消耗（作为 100% 基准）
        out["summary"]["today_kcal"] = round((da[-1].get("active_kcal") or 0), 0)
        out["summary"]["kcal_max_day"] = round(
            max((r.get("active_kcal") or 0) for r in da), 0)

    # ---- 睡眠：拆分为夜间主睡眠 + 白天小睡/打盹 ----
    sl = recs.get("sleep", [])
    def _is_nap(r):
        t = r.get("start_at") or r.get("date") or ""
        h = int(t[11:13]) if len(t) >= 13 else -1
        return 10 <= h <= 17   # 白天 10:00–17:59 视为小睡/打盹（启发式）
    main = [r for r in sl if not _is_nap(r)]
    nap = [r for r in sl if _is_nap(r)]
    out["nap_count"] = len(nap)
    out["naps"] = [{
        "date": (r.get("start_at") or "")[:10],
        "start": (r.get("start_at") or "")[11:16],
        "end": (r.get("end_at") or "")[11:16],
        "duration_min": r.get("time_asleep_minutes"),
        "score": r.get("sleep_score"),
    } for r in sorted(nap, key=lambda r: r.get("start_at") or "")]
    dur_map = _daymap([(r.get("date") or (r.get("start_at") or "")[:10],
                        r.get("time_asleep_minutes")) for r in main])
    score_map = _daymap([(r.get("date") or (r.get("start_at") or "")[:10],
                          r.get("sleep_score")) for r in main])
    out["sleep"]["labels"] = span
    out["sleep"]["duration"] = _align(dur_map)
    out["sleep"]["score"] = _align(score_map)
    deep_p, rem_p, light_p = [], [], []
    for r in main:
        stages = r.get("stages") or []
        if isinstance(stages, str):
            try:
                stages = json.loads(stages)
            except Exception:
                stages = []
        dd = sum(s.get("minutes", 0) for s in stages
                 if isinstance(s, dict) and s.get("stage") == "deep")
        rr = sum(s.get("minutes", 0) for s in stages
                 if isinstance(s, dict) and s.get("stage") == "rem")
        ll = sum(s.get("minutes", 0) for s in stages
                 if isinstance(s, dict) and s.get("stage") == "light")
        tot = dd + rr + ll
        if tot > 0:
            deep_p.append(dd / tot * 100)
            rem_p.append(rr / tot * 100)
            light_p.append(ll / tot * 100)
    if deep_p:
        out["sleep"]["stages_avg"] = {
            "deep": round(_mean(deep_p)),
            "rem": round(_mean(rem_p)),
            "light": round(_mean(light_p)),
        }
    if dur_map:
        out["summary"]["avg_sleep_min"] = _mean(
            [x for xs in dur_map.values() for x in xs if x is not None])
        score_all = [x for xs in score_map.values() for x in xs
                     if x not in (None, 0)]
        out["summary"]["avg_sleep_score"] = _mean(score_all)

    # ---- 能量条数据：最近一晚主睡眠 + 今日小睡充电 ----
    if main:
        by_start: dict = {}
        for r in main:
            k = r.get("start_at") or ""
            cur = by_start.get(k)
            if cur is None or (r.get("time_asleep_minutes") or 0) > (
                    cur.get("time_asleep_minutes") or 0):
                by_start[k] = r
        last = max(by_start.values(), key=lambda r: r.get("start_at") or "")
        dur = last.get("time_asleep_minutes") or last.get("duration_minutes") or 0
        rec = _recovery_factor(last, user_id)
        today = _date.today().isoformat()
        nap_today = sum(
            (r.get("time_asleep_minutes") or 0)
            for r in nap if (r.get("start_at") or "")[:10] == today)
        # 充电模型：有效睡眠(时长×恢复效率) 8h=100%，小睡按五折算
        charge = min(100.0, dur * rec / 480 * 100 + nap_today / 480 * 100 * 0.5)
        out["summary"]["last_sleep"] = {
            "date": (last.get("start_at") or "")[:10],
            "duration_min": dur,
            "wake_at": last.get("end_at") or "",
            "nap_today_min": nap_today,
            "recovery": round(rec, 2),
            "charge_pct": round(charge, 1),
        }

    # ---- 精力百分比（与 Agent S 算法同口径，主页精力条联动清醒时间） ----
    ep = _energy_plan(sl, user_id)
    if ep:
        out["summary"]["energy_plan"] = ep

    # ---- 心率（静息快照，按天） ----
    resting = [r for r in recs.get("heart_rate", [])
               if r.get("sample_type") == "resting" and r.get("timestamp")]
    hr_map = _daymap([(r.get("timestamp"), r.get("bpm")) for r in resting])
    out["resting_hr"]["labels"] = span
    out["resting_hr"]["values"] = _align(hr_map)
    if resting:
        out["summary"]["resting_hr"] = _mean([r.get("bpm") for r in resting])

    # ---- 心率详情概览（passive 连续测量，每分钟一条） ----
    passive = [r.get("bpm") for r in recs.get("heart_rate", [])
               if r.get("sample_type") == "passive" and r.get("bpm") is not None]
    if passive:
        ps = sorted(passive)
        out["hr_overview"] = {
            "count": len(ps),
            "min": ps[0], "max": ps[-1],
            "avg": round(sum(ps) / len(ps), 1),
            "p10": ps[int(len(ps) * 0.10)],
            "p90": ps[int(len(ps) * 0.90)],
            "resting_avg": out["summary"].get("resting_hr"),
        }
    hr_days = sorted({(r.get("timestamp") or "")[:10]
                      for r in recs.get("heart_rate", [])
                      if r.get("sample_type") == "passive" and r.get("timestamp")})
    out["hr_detail_days"] = hr_days

    # ---- 血氧（按日均值） ----
    sp = recs.get("spo2", [])
    sp_map = _daymap([(r.get("timestamp"), r.get("spo2_pct")) for r in sp])
    out["spo2"]["labels"] = span
    out["spo2"]["values"] = _align(sp_map)
    allsp = [r.get("spo2_pct") for r in sp if r.get("spo2_pct") is not None]
    if allsp:
        out["spo2"]["avg"] = round(_mean(allsp), 1)
        out["spo2"]["low_pct"] = round(
            sum(1 for v in allsp if v < 95) / len(allsp) * 100, 1)

    # ---- 压力（按日均值 + 等级分布） ----
    st = recs.get("stress", [])
    st_map = _daymap([(r.get("timestamp"), r.get("stress_score")) for r in st])
    out["stress"]["labels"] = span
    out["stress"]["values"] = _align(st_map)
    lv = defaultdict(int)
    for r in st:
        l = r.get("level")
        if l:
            lv[l] += 1
    out["stress"]["levels"] = dict(lv)
    if st_map:
        out["summary"]["stress_avg"] = _mean(
            [x for xs in st_map.values() for x in xs if x is not None])

    # ---- 运动 ----
    for r in recs.get("workouts", []):
        out["workouts"].append({
            "date": (r.get("start_at") or "")[:10],
            "type": r.get("activity_type"),
            "duration": r.get("duration_minutes"),
            "distance_km": round((r.get("distance_m") or 0) / 1000, 2),
            "kcal": r.get("calories_kcal"),
            "avg_hr": r.get("avg_heart_rate_bpm"),
            "max_hr": r.get("max_heart_rate_bpm"),
        })

    return out


def get_hr_day(day: str | None, user_id: str = "") -> dict:
    """返回某天每分钟的 passive 心率曲线（用于全天心率详情下钻）。"""
    if not day:
        return {"error": "need ?day=YYYY-MM-DD"}
    path = find_latest_json(user_id)
    if not path:
        return {"error": "未找到导出 JSON"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("records", {}).get("heart_rate", [])
    pts = [(r.get("timestamp"), r.get("bpm")) for r in recs
           if r.get("sample_type") == "passive"
           and (r.get("timestamp") or "").startswith(day)
           and r.get("bpm") is not None]
    pts.sort()
    return {
        "day": day,
        "count": len(pts),
        "labels": [t[11:16] for t, _ in pts],   # HH:MM
        "values": [b for _, b in pts],
    }


def get_sleep_overview(user_id: str = "") -> dict:
    """睡眠总览：三个月平均睡眠时长/评分/睡眠心率/各阶段占比/小睡次数。"""
    path = find_latest_json(user_id)
    if not path:
        return {"error": "未在 D:\\MIwatchAI\\JSON\\ 找到导出 JSON"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("records", {})
    sl = recs.get("sleep", [])
    main = [r for r in sl if not _is_nap(r)]

    durations = [r.get("time_asleep_minutes") for r in main
                 if r.get("time_asleep_minutes")]
    scores = [r.get("sleep_score") for r in main
              if r.get("sleep_score") not in (None, 0)]

    # 各睡眠阶段分钟合计 -> 占比
    cat = {"awake": 0, "light": 0, "deep": 0, "rem": 0}
    for r in main:
        for s in _get_stages(r):
            stg = s.get("stage")
            if stg in cat:
                cat[stg] += s.get("minutes", 0)
    tot = sum(cat.values()) or 1
    comp = {k: round(v / tot * 100) for k, v in cat.items()}

    # 睡眠期内心率均值（按主睡眠窗口切 passive 心率）
    wins_by_date = {}
    for r in main:
        if r.get("start_at") and r.get("end_at"):
            d = r["start_at"][:10]
            wins_by_date.setdefault(d, []).append((r["start_at"], r["end_at"]))
    sh_sum, sh_n = 0, 0
    for r in recs.get("heart_rate", []):
        if r.get("sample_type") != "passive" or r.get("bpm") is None:
            continue
        ts = r.get("timestamp", "")
        w = wins_by_date.get(ts[:10])
        if not w:
            continue
        for s, e in w:
            if s <= ts <= e:
                sh_sum += r["bpm"]
                sh_n += 1
                break
    resting = [r.get("bpm") for r in recs.get("heart_rate", [])
               if r.get("sample_type") == "resting" and r.get("bpm")]

    avg_sleep_min = _mean(durations) if durations else None

    # 近10天入睡时刻（主睡眠，用于入睡规律性）
    by_date = {}
    for r in main:
        s = r.get("start_at")
        if s and len(s) >= 16:
            by_date[s[:10]] = s[11:16]
    bedtimes = [{"date": d, "time": by_date[d]} for d in sorted(by_date)[-10:]]

    return {
        "avg_sleep_min": avg_sleep_min,
        "avg_sleep_hm": _fmt_hm(avg_sleep_min),
        "avg_score": _mean(scores),
        "score_label": _score_label(_mean(scores)),
        "composition": comp,
        "sleeping_hr_avg": round(sh_sum / sh_n, 1) if sh_n else None,
        "resting_hr_avg": round(sum(resting) / len(resting), 1) if resting else None,
        "nap_count": len([r for r in sl if _is_nap(r)]),
        "sleep_days": len(main),
        "main_dates": sorted({(r.get("start_at") or "")[:10]
                             for r in main if r.get("start_at")}),
        "bedtimes": bedtimes,
    }


def get_sleep_day(day: str, user_id: str = "") -> dict:
    """某一天（夜间主睡眠）详情：睡眠曲线分段、小睡、睡眠心率、睡眠血氧。"""
    if not day:
        return {"error": "need ?day=YYYY-MM-DD"}
    path = find_latest_json(user_id)
    if not path:
        return {"error": "未找到导出 JSON"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    recs = data.get("records", {})
    sl = recs.get("sleep", [])

    cands = [r for r in sl if not _is_nap(r)
             and (r.get("start_at") or "")[:10] == day]
    if not cands:
        return {"error": f"该日（{day}）无夜间主睡眠记录"}
    # 同一天可能有多条主睡眠记录（拆分/重复），取睡眠时长最长的那条
    rec = max(cands, key=lambda r: r.get("time_asleep_minutes") or 0)

    sa, ea = rec["start_at"], rec["end_at"]
    sa_d = datetime.fromisoformat(sa)
    pre0 = sa_d - _td(minutes=30)
    pre0_iso = pre0.isoformat()

    # 重建时间轴睡眠分段（stages 按时间顺序累加）
    segs, t = [], sa_d
    for s in _get_stages(rec):
        mins = s.get("minutes", 0)
        end = t + _td(minutes=mins)
        segs.append({"stage": s.get("stage"), "start": t.strftime("%H:%M"),
                     "end": end.strftime("%H:%M"), "minutes": mins})
        t = end

    stage_counts = {k: sum(x["minutes"] for x in segs if x["stage"] == k)
                    for k in ("awake", "light", "deep", "rem")}

    # 当天小睡（完整记录，含时间戳，用于切小睡前后心率）
    nap_recs = [r for r in sl if _is_nap(r) and (r.get("start_at") or "")[:10] == day]

    # 主睡眠心率：睡前30分 + 睡眠期（独立一张图）
    combo = []
    for r in recs.get("heart_rate", []):
        if r.get("sample_type") != "passive" or r.get("bpm") is None:
            continue
        ts = r.get("timestamp", "")
        if pre0_iso <= ts <= ea:
            phase = "pre" if ts < sa else "sleep"
            combo.append({"t": ts[11:16], "bpm": r["bpm"], "phase": phase})
    combo.sort(key=lambda x: x["t"])

    # 每个小睡：独立一份心率（小睡前30分 + 小睡期 + 小睡后15分），不与主睡眠混表
    hrt = recs.get("heart_rate", [])
    naps = []
    for nr in nap_recs:
        sa_n, ea_n = nr.get("start_at"), nr.get("end_at")
        nap_hr = []
        if sa_n and ea_n:
            pre_n = post_n = ""
            try:
                ns = datetime.fromisoformat(sa_n)
                ne = datetime.fromisoformat(ea_n)
                pre_n = (ns - _td(minutes=30)).isoformat()
                post_n = (ne + _td(minutes=15)).isoformat()
            except ValueError:
                pass
            if pre_n and post_n:
                for r in hrt:
                    if r.get("sample_type") != "passive" or r.get("bpm") is None:
                        continue
                    ts = r.get("timestamp", "")
                    if pre_n <= ts <= post_n:
                        if ts < sa_n:
                            phase = "nap_pre"
                        elif ts <= ea_n:
                            phase = "nap"
                        else:
                            phase = "nap_post"
                        nap_hr.append({"t": ts[11:16], "bpm": r["bpm"], "phase": phase})
                nap_hr.sort(key=lambda x: x["t"])
        naps.append({
            "start": (sa_n or "")[11:16],
            "end": (ea_n or "")[11:16],
            "duration_min": nr.get("time_asleep_minutes"),
            "hr": nap_hr,
        })

    # 睡眠期内血氧
    sp = [r for r in recs.get("spo2", [])
          if sa <= r.get("timestamp", "") <= ea and r.get("spo2_pct") is not None]
    sp.sort(key=lambda r: r["timestamp"])
    spo2 = [{"t": r["timestamp"][11:16], "v": r["spo2_pct"]} for r in sp]
    spo2_min = min((x["v"] for x in spo2), default=None)
    spo2_avg = round(sum(x["v"] for x in spo2) / len(spo2), 1) if spo2 else None
    spo2_low = sum(1 for x in spo2 if x["v"] < 92)

    inbed_min = round((datetime.fromisoformat(ea) - sa_d).total_seconds() / 60)
    total_min_v = rec.get("time_asleep_minutes") or 0
    efficiency = round(total_min_v / inbed_min * 100) if inbed_min else None
    awake_count = sum(1 for x in segs if x["stage"] == "awake")
    return {
        "day": day,
        "start": sa[11:16], "end": ea[11:16],
        "score": rec.get("sleep_score"),
        "total_min": rec.get("time_asleep_minutes"),
        "total_hm": _fmt_hm(rec.get("time_asleep_minutes")),
        "inbed_min": inbed_min, "inbed_hm": _fmt_hm(inbed_min),
        "efficiency": efficiency, "awake_count": awake_count,
        "segments": segs, "stage_counts": stage_counts,
        "naps": naps,
        "hr": combo,
        "hr_pre_count": sum(1 for c in combo if c["phase"] == "pre"),
        "spo2": spo2, "spo2_min": spo2_min,
        "spo2_avg": spo2_avg, "spo2_low": spo2_low,
    }


def get_data(force: bool = False, user_id: str = "") -> dict:
    path = find_latest_json(user_id)
    if not path:
        return {"error": "未在 D:\\MIwatchAI\\JSON\\ 找到导出 JSON，请先运行 pull_to_json.py"}
    mtime = os.path.getmtime(path)
    c = _cache.get(user_id)
    if (not force) and c and c["path"] == path and c["mtime"] == mtime and c["data"]:
        return c["data"]
    data = aggregate(path, user_id)
    _cache[user_id] = {"path": path, "mtime": mtime, "data": data}
    return data


def do_pull(user_id: str = "") -> dict:
    """联动 pull_to_json.py：触发一次重新拉取（复用钥匙串 token）。
    多账号：user_id 非空 → 拉该账号的数据（pull_to_json.py --user-id）。"""
    if not os.path.exists(PULL_SCRIPT):
        return {"status": "error", "msg": f"未找到 {PULL_SCRIPT}"}
    cmd = [PY, PULL_SCRIPT]
    uid = (user_id or "").strip()
    if uid:
        cmd += ["--user-id", uid]
    with _pull_lock:
        proc = _pull_proc.get(uid)
        if proc is not None and proc.poll() is None:
            return {"status": "running", "pid": proc.pid}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _pull_proc[uid] = proc
    return {"status": "started", "pid": proc.pid}


def pull_status(user_id: str = "") -> dict:
    """按用户返回拉取状态，避免两个账号互相看到对方的 running。"""
    uid = (user_id or "").strip()
    with _pull_lock:
        proc = _pull_proc.get(uid)
        running = proc is not None and proc.poll() is None
        return {"status": "running" if running else "idle"}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # 多用户（方案 A）：所有数据接口支持 ?user_id=xxx 隔离；空 = 单用户根目录
        uid = (q.get("user_id") or [""])[0].strip()
        if u.path in ("/", "/index.html"):
            if os.path.exists(INDEX):
                self._send(200, open(INDEX, encoding="utf-8").read(),
                           "text/html; charset=utf-8")
            else:
                self._send(404, {"error": "index.html not found"})
        elif u.path == "/api/data":
            self._send(200, get_data(user_id=uid))
        elif u.path == "/api/refresh":
            self._send(200, get_data(force=True, user_id=uid))
        elif u.path == "/api/pull":
            self._send(200, do_pull(user_id=uid))
        elif u.path == "/api/pull_status":
            self._send(200, pull_status(user_id=uid))
        elif u.path.startswith("/api/hr"):
            self._send(200, get_hr_day(q.get("day", [None])[0], uid))
        elif u.path.startswith("/api/sleep"):
            day = q.get("day", [None])[0]
            self._send(200, get_sleep_day(day, uid) if day else get_sleep_overview(uid))
        else:
            # 静态文件（如 chart.umd.min.js），仅限 ROOT 目录内
            rel = u.path.lstrip("/")
            fp = os.path.normpath(os.path.join(ROOT, rel))
            if fp.startswith(ROOT) and os.path.isfile(fp):
                ctype = ("application/javascript; charset=utf-8"
                         if fp.endswith(".js") else "text/html; charset=utf-8")
                self._send(200, open(fp, "rb").read(), ctype)
            else:
                self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = 8000
    # 监听 0.0.0.0 以便同一局域网内的手机 App / 浏览器访问；
    # 仅本机访问时仍是 http://127.0.0.1:8000
    host = "0.0.0.0"
    print(f"小米手环仪表盘: http://127.0.0.1:{port}  (局域网手机访问需填电脑内网IP)")
    print(f"数据目录: {EXPORT_DIR}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
