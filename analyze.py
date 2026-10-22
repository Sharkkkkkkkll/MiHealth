#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析 mi_fitness_export.json，输出人类可读的健康趋势报告（markdown）。

用法：
    Python314\\python.exe D:/MIwatchAI/analyze.py
默认自动读取 D:\\MIwatchAI\\JSON 中最新的 .json，报告写 D:\\MIwatchAI\\health_report.md。
"""
from __future__ import annotations
import json, sys, argparse, statistics, os
from collections import defaultdict, Counter
from datetime import datetime

# 导出 JSON 的固定目录（与 pull_to_json.py 保持一致）
EXPORT_DIR = r"D:\MIwatchAI\JSON"

HR_RESTING_ZONES = "resting"
LEVEL_CN = {"relax": "放松", "normal": "正常", "medium": "中等", "high": "偏高"}


def pctl(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def iso_week(d: str):
    dt = datetime.fromisoformat(d[:10])
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def analyze(data):
    recs = data["records"]
    out = []
    w = out.append
    w(f"# 小米手环健康趋势报告\n")
    w(f"- 数据来源：{data.get('source')}（schema {data.get('schema_version')}）")
    w(f"- 导出时间：{data.get('generated_at')}")
    w(f"- 覆盖区间：2026-07-11 ~ 2026-10-08（约 90 天）")
    w("")
    w("## 一、数据覆盖概览\n")
    for t, arr in recs.items():
        if arr:
            w(f"- **{t}**：{len(arr):,} 条")
    w("")

    # ---------------- 日活 / 步数 ----------------
    da = recs.get("daily_activity", [])
    if da:
        steps = [r["steps"] for r in da if r.get("steps") is not None]
        dist = [r["distance_m"] for r in da if r.get("distance_m") is not None]
        kcal = [r["active_kcal"] for r in da if r.get("active_kcal") is not None]
        w("## 二、日常活动（步数 / 距离 / 消耗）\n")
        w(f"- 记录天数：**{len(da)}** 天")
        if steps:
            w(f"- 日均步数：**{statistics.mean(steps):,.0f}**（中位 {statistics.median(steps):,.0f}，"
              f"最高 {max(steps):,}，最低 {min(steps):,}）")
            w(f"- 步数 P25/P75：{pctl(steps,0.25):,.0f} / {pctl(steps,0.75):,.0f}")
            sed = sum(1 for s in steps if s < 5000)
            w(f"- 偏低活动日（<5000 步）：{sed} 天（占比 {sed/len(steps)*100:.0f}%）")
        if dist:
            w(f"- 日均距离：{statistics.mean(dist)/1000:.2f} km（累计 {sum(dist)/1000:,.0f} km）")
        if kcal:
            w(f"- 日均活动消耗：{statistics.mean(kcal):,.0f} kcal（累计 {sum(kcal):,.0f} kcal）")
        # 周趋势
        wk = defaultdict(list)
        for r in da:
            if r.get("steps") is not None:
                wk[iso_week(r["date"])].append(r["steps"])
        w("\n  **周均步数趋势：**")
        for wk_key in sorted(wk):
            vals = wk[wk_key]
            w(f"  - {wk_key}：{statistics.mean(vals):,.0f} 步/天（{len(vals)}天）")
        w("")

    # ---------------- 睡眠 ----------------
    sl = recs.get("sleep", [])
    if sl:
        main = [r for r in sl if r.get("is_nap") == 0]
        naps = [r for r in sl if r.get("is_nap") != 0]
        dur = [r["time_asleep_minutes"] for r in sl if r.get("time_asleep_minutes") is not None]
        mdur = [r["time_asleep_minutes"] for r in main if r.get("time_asleep_minutes") is not None]
        score = [r["sleep_score"] for r in sl if r.get("sleep_score") is not None]
        mscore = [r["sleep_score"] for r in main if r.get("sleep_score") is not None]
        deep_pct, rem_pct, light_pct = [], [], []
        for r in main:
            stages = r.get("stages", []) or []
            if isinstance(stages, str):
                try:
                    stages = json.loads(stages)
                except Exception:
                    stages = []
            dd = sum(s.get("minutes", 0) for s in stages if isinstance(s, dict) and s.get("stage") == "deep")
            rr = sum(s.get("minutes", 0) for s in stages if isinstance(s, dict) and s.get("stage") == "rem")
            ll = sum(s.get("minutes", 0) for s in stages if isinstance(s, dict) and s.get("stage") == "light")
            tot_s = dd + rr + ll
            if tot_s > 0:
                deep_pct.append(dd / tot_s * 100)
                rem_pct.append(rr / tot_s * 100)
                light_pct.append(ll / tot_s * 100)
        w("## 三、睡眠\n")
        w(f"- 睡眠记录总数：**{len(sl)}** 条（主睡眠 {len(main)} 晚，小睡/碎片 {len(naps)} 段）")
        if dur:
            w(f"- 全部记录平均时长：{statistics.mean(dur):.0f} 分钟（含小睡，仅供参考）")
        if mdur:
            w(f"- **主睡眠**平均时长：**{statistics.mean(mdur):.0f} 分钟**（≈{statistics.mean(mdur)/60:.1f} 小时），"
              f"中位 {statistics.median(mdur):.0f} 分，最短 {min(mdur)}，最长 {max(mdur)}")
        if mscore:
            w(f"- **主睡眠**平均评分：**{statistics.mean(mscore):.0f}** 分（中位 {statistics.median(mscore):.0f}，"
              f"最高 {max(mscore)}，最低 {min(mscore)}）")
            good = sum(1 for s in mscore if s >= 80)
            mid = sum(1 for s in mscore if 60 <= s < 80)
            poor = sum(1 for s in mscore if s < 60)
            w(f"- 主睡眠评分分布：优(≥80) {good} 晚 / 中(60-79) {mid} 晚 / 差(<60) {poor} 晚")
        if deep_pct and rem_pct and light_pct:
            w(f"- 主睡眠结构均值：深睡 {statistics.mean(deep_pct):.0f}% / "
              f"REM {statistics.mean(rem_pct):.0f}% / 浅睡 {statistics.mean(light_pct):.0f}%")
        if mscore:
            best = max(main, key=lambda r: r.get("sleep_score") or 0)
            worst = min(main, key=lambda r: r.get("sleep_score") or 999)
            w(f"- 最佳夜：{best.get('date') or best.get('start_at','')[:10]} 评分 {best.get('sleep_score')} "
              f"({best.get('time_asleep_minutes')}分)")
            w(f"- 最差夜：{worst.get('date') or worst.get('start_at','')[:10]} 评分 {worst.get('sleep_score')} "
              f"({worst.get('time_asleep_minutes')}分)")
        w("")

    # ---------------- 心率 ----------------
    hr = recs.get("heart_rate", [])
    if hr:
        all_bpm = [r["bpm"] for r in hr if r.get("bpm") is not None]
        resting = [r["bpm"] for r in hr if r.get("sample_type") == "resting" and r.get("bpm") is not None]
        w("## 四、心率\n")
        w(f"- 心率读数总量：**{len(hr):,}** 条")
        if resting:
            w(f"- 静息心率（每日快照）：**{statistics.mean(resting):.0f} bpm** "
              f"（中位 {statistics.median(resting):.0f}，最低 {min(resting)}，最高 {max(resting)}）")
            wk = defaultdict(list)
            for r in hr:
                if r.get("sample_type") == "resting" and r.get("bpm") is not None:
                    wk[iso_week(r["timestamp"][:10])].append(r["bpm"])
            w("\n  **周均静息心率趋势：**")
            for k in sorted(wk):
                w(f"  - {k}：{statistics.mean(wk[k]):.0f} bpm")
        if all_bpm:
            w(f"- 全量心率（含日间）：均值 {statistics.mean(all_bpm):.0f} bpm，"
              f"P10 {pctl(all_bpm,0.10):.0f} / P90 {pctl(all_bpm,0.90):.0f}，"
              f"最低 {min(all_bpm)}，最高 {max(all_bpm)}")
        w("")

    # ---------------- 血氧 ----------------
    sp = recs.get("spo2", [])
    if sp:
        vals = [r["spo2_pct"] for r in sp if r.get("spo2_pct") is not None]
        w("## 五、血氧（SpO₂）\n")
        w(f"- 读数：**{len(sp):,}** 条")
        if vals:
            low95 = sum(1 for v in vals if v < 95)
            low90 = sum(1 for v in vals if v < 90)
            w(f"- 平均值：**{statistics.mean(vals):.1f}%**（中位 {statistics.median(vals):.0f}%，"
              f"最低 {min(vals)}%，最高 {max(vals)}%）")
            w(f"- 低于 95% 的读数：{low95} 条（{low95/len(vals)*100:.1f}%）；"
              f"低于 90%：{low90} 条（{low90/len(vals)*100:.1f}%）")
        w("")

    # ---------------- 压力 ----------------
    st = recs.get("stress", [])
    if st:
        scores = [r["stress_score"] for r in st if r.get("stress_score") is not None]
        lv = Counter(r.get("level") for r in st)
        w("## 六、压力\n")
        w(f"- 读数：**{len(st):,}** 条")
        if scores:
            w(f"- 平均压力指数：**{statistics.mean(scores):.0f}**（中位 {statistics.median(scores):.0f}，"
              f"最低 {min(scores)}，最高 {max(scores)}）")
        w("- 等级分布：")
        for lk in ("relax", "normal", "medium", "high"):
            n = lv.get(lk, 0)
            if n:
                w(f"  - {LEVEL_CN.get(lk, lk)}（{lk}）：{n} 条（{n/len(st)*100:.1f}%）")
        wk = defaultdict(list)
        for r in st:
            if r.get("stress_score") is not None:
                wk[iso_week(r["timestamp"][:10])].append(r["stress_score"])
        w("\n  **周均压力趋势：**")
        for k in sorted(wk):
            w(f"  - {k}：{statistics.mean(wk[k]):.0f}")
        w("")

    # ---------------- 运动 ----------------
    wo = recs.get("workouts", [])
    if wo:
        w("## 七、运动记录\n")
        w(f"- 共 **{len(wo)}** 次运动")
        for r in wo:
            w(f"- {r.get('start_at','')[:10]} ｜ {r.get('activity_type')} ｜ "
              f"{r.get('duration_minutes')} 分钟 ｜ {r.get('distance_m',0)/1000:.2f} km ｜ "
              f"{r.get('calories_kcal',0):.0f} kcal ｜ 平均心率 {r.get('avg_heart_rate_bpm')} / "
              f"峰值 {r.get('max_heart_rate_bpm')} bpm")
        w("")

    w("## 八、说明与提示\n")
    w("- 本报告由 `analyze.py` 基于 `mi_fitness_export.json` 自动生成，所有数值来自小米云原始数据。")
    w("- 静息心率取小米每日静息快照；全量心率含日间波动，仅供分布参考。")
    w("- 若某指标长期偏离常规（如静息心率持续偏高、血氧频繁 <90%、睡眠不足），建议结合医学建议解读，本报告不构成诊断。")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json", nargs="?", default=None,
                   help=f"导出 JSON 路径（默认：自动取 {EXPORT_DIR} 中最新的 .json）")
    ap.add_argument("--out", default=r"D:\MIwatchAI\health_report.md")
    args = ap.parse_args()
    if not args.json:
        cands = [os.path.join(EXPORT_DIR, f) for f in os.listdir(EXPORT_DIR)] if os.path.isdir(EXPORT_DIR) else []
        cands = [c for c in cands if c.lower().endswith(".json")]
        if not cands:
            print(f"未在 {EXPORT_DIR} 找到 JSON，请先运行 pull_to_json.py 或手动指定 JSON 路径。", file=sys.stderr)
            raise SystemExit(2)
        args.json = max(cands, key=os.path.getmtime)  # 取最新一份
        print(f"[输入] 自动选用最新导出：{args.json}")
    data = load(args.json)
    report = analyze(data)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"\n[已写出报告] {args.out}")


if __name__ == "__main__":
    main()
