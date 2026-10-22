# -*- coding: utf-8 -*-
"""健康数据源：读取 pull_to_json.py 的最新导出 JSON，加工成 LLM 友好的摘要。

64MB 的 JSON 只按 mtime 缓存解析一次；对外只暴露"文字摘要"，
避免把 10 万条心率原始点塞进 prompt。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

import config

_cache: dict = {}

# 当前用户上下文（多用户方案 A）：请求入口设置，load() 等按它找数据目录。
# ★ 2026-10-20 修复：必须用线程局部变量，否则两个用户同时请求会互相串数据。
_CUR_USER = threading.local()


def set_user(user_id: str = "") -> None:
    """设置当前用户（每个请求线程独立，不串用户）。"""
    _CUR_USER.id = (user_id or "").strip()


def _cur_user() -> str:
    return getattr(_CUR_USER, "id", "")


def _latest_export(user_id: str = "") -> str | None:
    d = config.export_dir_for(user_id or _cur_user())
    if not os.path.isdir(d):
        return None
    files = [
        f for f in os.listdir(d)
        if f.endswith(".json") and not f.startswith("mi_fitness")
    ]
    if not files:
        return None
    files.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)))
    return os.path.join(d, files[-1])


def load(user_id: str = "") -> dict | None:
    """返回最新导出 JSON（带 mtime 缓存，按用户隔离）。"""
    uid = user_id or _cur_user()
    path = _latest_export(uid)
    if not path:
        return None
    mt = os.path.getmtime(path)
    c = _cache.get(uid) or {}
    if c.get("path") == path and c.get("mtime") == mt:
        return c.get("data")
    # ★ 2026-10-17：pull_to_json.py 异步写 JSON 时可能读到半截/空文件，
    #   偶发 JSONDecodeError。加重试：读到坏文件等 1s 重试，最多 3 次。
    data = None
    for attempt in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            break
        except Exception:
            if attempt < 2:
                import time
                time.sleep(1)
            else:
                return None
    _cache[uid] = {"path": path, "mtime": mt, "data": data}
    return data


def _stages_of(rec: dict) -> dict:
    """解析分期分钟数 {deep, rem, light}。"""
    try:
        stages = json.loads(rec.get("stages") or "[]")
    except Exception:
        stages = []
    out = {"deep": 0, "rem": 0, "light": 0}
    for s in stages:
        if not isinstance(s, dict):
            continue
        # ★ 2026-10-13 修复：漏了 light 映射导致 light 恒 0、
        #   深睡占比虚高、质量系数 100% 顶格 1.15（无区分度）
        k = {"deep": "deep", "rem": "rem", "dream": "rem",
             "light": "light"}.get(s.get("stage"))
        if k:
            out[k] += s.get("minutes") or 0
    return out


def quality_factor(rec: dict, user_id: str = "") -> float:
    """睡眠恢复效率 0.70~1.15：深睡占比为主，REM/评分为辅。

    同样睡8小时：深睡足→回满；喝酒/心事导致浅睡长、深睡短→回得少。

    ★ 2026-10-13 个人基准标定（用户方案，配合 S 困意算法升级）：
      旧公式用通用人群基准（deep 17%/rem 22%），对高深睡占比用户
      （实测均值 54.7%）永远顶格 1.15，无区分度。
      新公式用【个人近3个月均值】做基准：
        q = 1.0 + (deep_ratio − 个人平均deep) × w_deep
                + (rem_ratio  − 个人平均rem)  × w_rem
                + (score      − 个人平均score) × w_score
      个人深睡占比 54.7% 是"你的常态" → q≈1.0；
      某晚深睡 40%（低于常态）→ q<1.0（质量差）；
      某晚深睡 60%（高于常态）→ q>1.0（质量好）。
      权重先用保守默认（deep 1.2 / rem 0.6 / score 0.0025），
      积累极端样本后可用历史数据反推调优（见 planner._sleepiness_anchor）。
    """
    st = _stages_of(rec)
    tot = st["deep"] + st["rem"] + st["light"]
    sc = rec.get("sleep_score")
    if tot <= 0:
        # 无分期数据（如小睡）：退回评分；都没有则中性
        return 1.0 if not sc else max(0.75, min(1.10, 0.6 + sc / 200))
    deep_r = st["deep"] / tot
    rem_r = st["rem"] / tot
    # 个人基准：近3个月均值（缓存，避免每次重算）
    base = _quality_baseline(user_id)
    f = 1.0 + (deep_r - base["deep"]) * 1.2 \
            + (rem_r - base["rem"]) * 0.6
    if sc:
        f += (sc - base["score"]) * 0.0025
    return round(max(0.70, min(1.15, f)), 2)


_QB_CACHE: dict = {}


def _quality_baseline(user_id: str = "") -> dict:
    """个人睡眠质量基准：近3个月主睡眠的 deep/rem 占比与评分均值。

    用 mtime 缓存（与 load 同 key），避免每条记录都重算。
    """
    uid = user_id or _cur_user()
    data = load(uid)
    if not data:
        return {"deep": 0.17, "rem": 0.22, "score": 70}
    path = _latest_export(uid)
    key = (path, os.path.getmtime(path))
    c = _QB_CACHE.get(uid)
    if c and c.get("key") == key and c.get("base") is not None:
        return c["base"]
    recs = [r for r in _sleep_records(data)
            if (r.get("duration_minutes") or 0) >= 180][-90:]
    deeps, rems, scores = [], [], []
    for r in recs:
        st = _stages_of(r)
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
    _QB_CACHE[uid] = {"key": key, "base": base}
    return base


def effective_minutes(rec: dict, user_id: str = "") -> float:
    """有效睡眠 = 时长 × 恢复效率（负债计算用这个，不用裸时长）。"""
    return (rec.get("duration_minutes") or 0) * quality_factor(rec, user_id)


def sleep_health_flag(days: int = 7, user_id: str = "") -> dict | None:
    """健康人视角的异常信号检测（不诊断，只提醒就医）。

    极差判据（近N夜）：平均时长<4h / 睡眠效率<75%（长期躺而睡不着）/
    评分均值<45。命中任一 → abnormal，AI 建议降级为"仅供参考+温和就医提示"。
    """
    recs = main_sleeps(days, user_id)
    if len(recs) < 5:
        return None
    durs = [r.get("duration_minutes") or 0 for r in recs]
    avg_d = sum(durs) / len(durs)
    effs = []
    for r in recs:
        try:
            a = datetime.fromisoformat(r["start_at"]).astimezone()
            b = datetime.fromisoformat(r["end_at"]).astimezone()
            w = (b - a).total_seconds() / 60
            if w > 30:
                effs.append((r.get("duration_minutes") or 0) / w)
        except Exception:
            continue
    avg_e = sum(effs) / len(effs) if effs else 1.0
    scores = [r.get("sleep_score") for r in recs if r.get("sleep_score")]
    avg_s = sum(scores) / len(scores) if scores else None
    flags = []
    if avg_d < 240:
        flags.append(f"近{len(recs)}夜平均只睡{avg_d / 60:.1f}小时")
    elif avg_d < 330:
        flags.append(f"近{len(recs)}夜平均不足5.5小时")
    if avg_e < 0.75:
        flags.append(f"睡眠效率仅{avg_e * 100:.0f}%（长时间躺而睡不着）")
    if avg_s is not None and avg_s < 45:
        flags.append(f"睡眠评分持续偏低（均值{avg_s:.0f}）")
    return {"abnormal": bool(flags), "flags": flags,
            "avg_dur": round(avg_d), "avg_eff": round(avg_e, 2)}


def _sleep_records(data: dict) -> list:
    """当晚主睡眠（剔除小睡），按开始时间排序。"""
    recs = [r for r in data.get("records", {}).get("sleep", []) if not r.get("is_nap")]
    recs.sort(key=lambda r: r.get("start_at", ""))
    return recs


def main_sleeps(days: int = 90, user_id: str = "") -> list:
    """主睡眠序列：剔除小睡 + 去重（同 start_at 保留时长最长的一条）+ 时长阈值。

    云端存在同一晚多条重复记录（start_at 相同 duration 不同），必须先去重，
    否则模式分析会被重复夜拉偏。
    """
    data = load(user_id)
    if not data:
        return []
    best: dict = {}
    for r in _sleep_records(data):
        k = r.get("start_at")
        if not k:
            continue
        cur = best.get(k)
        if cur is None or (r.get("duration_minutes") or 0) > (cur.get("duration_minutes") or 0):
            best[k] = r
    out = [r for r in best.values() if (r.get("duration_minutes") or 0) >= 180]
    out.sort(key=lambda r: r.get("start_at", ""))
    return out[-days:]


# ---------- 静息心率（精确到本地日期，带 mtime 级缓存） ----------

_hr_cache: dict = {}


def resting_hr_by_day(days: int = 30, user_id: str = "") -> dict:
    """{本地日期: 平均静息bpm}，最近 days 天。10 万条原始点只解析近期窗口。"""
    uid = user_id or _cur_user()
    data = load(uid)
    if not data:
        return {}
    cutoff_utc = (datetime.utcnow() - timedelta(days=days + 1)).strftime("%Y-%m-%d")
    acc: dict = {}
    for r in data.get("records", {}).get("heart_rate", []):
        ts = r.get("timestamp") or ""
        if ts[:10] < cutoff_utc:
            continue
        if r.get("sample_type") != "resting" or not r.get("bpm"):
            continue
        try:
            d = datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d")
        except Exception:
            continue
        acc.setdefault(d, []).append(float(r["bpm"]))
    by_day = {d: sum(v) / len(v) for d, v in acc.items()}
    path = _latest_export(uid)
    key = (path, os.path.getmtime(path), days)
    _hr_cache[uid] = {"key": key, "by_day": by_day}
    return by_day


def latest_sleep(user_id: str = "") -> dict | None:
    recs = main_sleeps(365, user_id)
    return recs[-1] if recs else None


def data_age_minutes(user_id: str = "") -> float | None:
    """最新导出距今多少分钟（数据新鲜度）。"""
    data = load(user_id)
    if not data:
        return None
    try:
        gen = datetime.fromisoformat(data["generated_at"]).astimezone()
        return (datetime.now().astimezone() - gen).total_seconds() / 60
    except Exception:
        return None


# ---------- 被动心率（分钟级连续测量，状态机的"实时"信号源） ----------

_pass_cache: dict = {}


def _passive_points(user_id: str = "") -> list:
    """全部 passive 心率点 [(aware datetime, bpm)]，升序，带 mtime 缓存。"""
    uid = user_id or _cur_user()
    data = load(uid)
    if not data:
        return []
    path = _latest_export(uid)
    key = (path, os.path.getmtime(path))
    c = _pass_cache.get(uid)
    if c and c.get("key") == key:
        return c["pts"]
    pts = []
    for r in data.get("records", {}).get("heart_rate", []):
        if r.get("sample_type") != "passive" or not r.get("bpm"):
            continue
        try:
            pts.append((datetime.fromisoformat(r["timestamp"]).astimezone(),
                        float(r["bpm"])))
        except Exception:
            continue
    pts.sort(key=lambda x: x[0])
    _pass_cache[uid] = {"key": key, "pts": pts}
    return pts


def hr_recent(minutes: int = 10, user_id: str = "") -> float | None:
    """最近 N 分钟被动心率均值（数据新鲜度受同步节奏限制，约5分钟级）。"""
    now = datetime.now().astimezone()
    xs = [v for t, v in _passive_points(user_id)
          if -120 <= (now - t).total_seconds() <= minutes * 60]
    return sum(xs) / len(xs) if xs else None


def hr_trend(window_min: int = 40, step_min: int = 10, user_id: str = "") -> dict | None:
    """近期心率趋势：把最近 window_min 分钟按时间分桶，比较首尾桶均值
    → 判断"在降/在升/平稳"，供话术引用真实走向（"心率慢慢降下来了"）。

    返回 {direction: falling|rising|steady, first, last, delta, now}
    """
    pts = _passive_points(user_id)
    if not pts:
        return None
    now = datetime.now().astimezone()
    lo = now - timedelta(minutes=window_min)
    win = [(t, v) for t, v in pts if lo <= t <= now]
    if len(win) < 3:
        return None
    n_buckets = max(2, window_min // step_min)
    size = max(1, len(win) // n_buckets)
    buckets = [
        sum(v for _, v in win[i:i + size]) / max(1, len(win[i:i + size]))
        for i in range(0, len(win), size)
    ]
    if len(buckets) < 2:
        return None
    first, last = buckets[0], buckets[-1]
    delta = last - first
    direction = ("falling" if delta <= -1.5
                 else "rising" if delta >= 1.5 else "steady")
    tail = [v for _, v in win[-3:]]
    return {"direction": direction, "first": round(first),
            "last": round(last), "delta": round(delta, 1),
            "now": round(sum(tail) / len(tail))}


def evening_baseline(bedtime_min: int, days: int = 14, user_id: str = "") -> float | None:
    """睡前基线心率：过去 days 天里，每天 [bed-90, bed-40] 时段的被动心率均值。"""
    pts = _passive_points(user_id)
    if not pts:
        return None
    lo = (bedtime_min - 90) % 1440
    hi = (bedtime_min - 40) % 1440
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    vals = []
    for t, v in pts:
        if t < cutoff:
            continue
        m = t.hour * 60 + t.minute
        if (m - lo) % 1440 <= (hi - lo) % 1440:
            vals.append(v)
    return sum(vals) / len(vals) if vals else None


def sleep_summary(days: int = 7, user_id: str = "") -> str:
    data = load(user_id)
    if not data:
        return "暂无导出数据，请先在电脑运行 pull_to_json.py 或让手机点拉取云端。"
    recs = _sleep_records(data)[-days:]
    if not recs:
        return "近" + str(days) + "天没有睡眠记录。"
    lines = []
    for r in recs:
        start = (r.get("start_at") or "")[5:16].replace("T", " ")
        end = (r.get("end_at") or "")[5:16].replace("T", " ")
        dur = r.get("duration_minutes") or 0
        score = r.get("sleep_score")
        # 分期占比
        light = deep = rem = 0
        try:
            for st in json.loads(r.get("stages") or "[]"):
                name = st.get("stage")
                mins = st.get("minutes") or 0
                if name == "deep":
                    deep += mins
                elif name == "light":
                    light += mins
                elif name in ("rem", "dream"):
                    rem += mins
        except Exception:
            pass
        lines.append(
            f"{start}~{end} 睡{dur}分 评分{score} 深睡{deep}分 浅睡{light}分 REM{rem}分"
        )
    return "最近睡眠（每晚：起止 时长 评分 深/浅/REM）：\n" + "\n".join(lines)


def hr_summary(days: int = 7, user_id: str = "") -> str:
    data = load(user_id)
    if not data:
        return "暂无数据。"
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    by_day: dict = {}
    for r in data.get("records", {}).get("heart_rate", []):
        d = (r.get("timestamp") or "")[:10]
        if d >= cutoff and r.get("bpm"):
            by_day.setdefault(d, []).append(float(r["bpm"]))
    if not by_day:
        return "近期没有心率记录。"
    lines = []
    for d in sorted(by_day):
        xs = by_day[d]
        lines.append(f"{d} 平均{sum(xs)/len(xs):.0f} 最低{min(xs):.0f} 最高{max(xs):.0f}bpm")
    return "每日心率：\n" + "\n".join(lines)


def activity_summary(days: int = 7, user_id: str = "") -> str:
    data = load(user_id)
    if not data:
        return "暂无数据。"
    recs = data.get("records", {}).get("daily_activity", [])[-days:]
    lines = []
    for r in recs:
        lines.append(
            f"{r.get('date')} 步数{r.get('steps') or 0} 消耗{int(r.get('active_kcal') or 0)}kcal"
        )
    return ("暂无活动数据。" if not lines else "每日活动：\n" + "\n".join(lines))


def snapshot(user_id: str = "") -> str:
    """给 LLM 的全量健康快照（system prompt 用，控制在一屏内）。"""
    data = load(user_id)
    gen = data.get("generated_at", "未知") if data else "无导出数据"
    return (
        f"【数据快照】导出生成时间：{gen}（数据滞后于此刻属正常）\n"
        + sleep_summary(7, user_id) + "\n" + hr_summary(7, user_id)
        + "\n" + activity_summary(7, user_id)
    )
