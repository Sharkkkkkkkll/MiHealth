# -*- coding: utf-8 -*-
"""MiHealth 睡眠智能体系统架构图（动态堆叠布局）"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyArrowPatch, Rectangle


FONT_FILE = r"C:\Windows\Fonts\msyh.ttc"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "architecture_v2.png")

matplotlib.font_manager.fontManager.addfont(FONT_FILE)
FONT_NAME = matplotlib.font_manager.FontProperties(fname=FONT_FILE).get_name()
plt.rcParams["font.family"] = FONT_NAME
plt.rcParams["axes.unicode_minus"] = False


W_IN = 15.0
LEFT_MARGIN = 0.9
RIGHT_LANE = 2.3
BOX_W = W_IN - LEFT_MARGIN - RIGHT_LANE
HEADER_H = 0.46
PAD_TOP = 0.16
PAD_BOTTOM = 0.16
LINE_H = 0.235
GAP = 0.72

TITLE_FS = 13.5
HEADER_FS = 11.5
ITEM_FS = 9.5
ARROW_FS = 8.5


LAYERS = [
    {
        "title": "数据源",
        "subtitle": "小米手环 · 小米运动健康 App · 云端",
        "body": "#EAF7F0",
        "header": "#1B7A4B",
        "edge": "#14613C",
        "items": [
            "小米手环采集：心率 / 睡眠 / 血氧 / 压力 / 步数",
            "小米运动健康 App 云端同步",
            "米桥扫码登录（userId + passToken，凭据存系统钥匙串）",
            "pull_to_json.py：增量同步 + 90 天滚动窗口导出 JSON",
        ],
    },
    {
        "title": "仪表盘服务器 :8000",
        "subtitle": "数据聚合与展示接口",
        "body": "#E8F1FB",
        "header": "#1E5FA8",
        "edge": "#16497F",
        "items": [
            "app/server.py：多用户缓存 + mtime 增量解析",
            "睡眠分析：_day_sleeps_agent / _last_main_sleep / _cumulative_awake",
            "精力算法：_energy_plan 与 Agent S 算法同口径",
            "接口：/api/data /api/sleep /api/hr /api/pull /api/refresh",
        ],
    },
    {
        "title": "Agent 服务器 :8001",
        "subtitle": "AI 决策核心（LLM + 规则 + 记忆）",
        "body": "#FDF1E3",
        "header": "#C65A1E",
        "edge": "#9A4314",
        "items": [
            "planner.py：S/P 双锚点睡眠规划 + 耐受画像",
            "notifier.py：睡前提醒 + 闹钟包生成 + 文案预热",
            "wake_engine.py：智能闹钟策略生成 + 唤醒复盘",
            "coach.py：事件状态机（P/STAY/HR/OD）",
            "memory.py：三层记忆（preferences/history/feedback）",
            "pattern.py：睡眠模式学习（圆周统计）",
            "接口：/api/agent/plan /alarm_pack /wake_plan /wake_review /hr",
        ],
    },
    {
        "title": "Android App（Kotlin / Compose）",
        "subtitle": "用户交互与本地执行",
        "body": "#EEF0F2",
        "header": "#3B4856",
        "edge": "#2A3340",
        "items": [
            "MainActivity：4 Tab + 二维码登录",
            "AgentSyncService：前台服务，快线 / 慢线同步 + WakeLock 保活",
            "AlarmPackScheduler：系统级闹钟排程 + 每日保底闹钟",
            "WakeEngine：智能闹钟执行状态机（IDLE → RECORDING → ARMED → RINGING）",
            "HyperateHrClient：实时心率 WebSocket + 15s 自动重连",
            "WakeCurveStore：整晚心率曲线本地存储与查看",
        ],
    },
]


ARROWS = [
    {
        "from": 0,
        "to": 1,
        "label": "JSON 导出 / 增量同步（90 天滚动窗口）",
    },
    {
        "from": 1,
        "to": 2,
        "label": "HTTP /api/*",
    },
    {
        "from": 2,
        "to": 3,
        "label": "HTTP /api/agent/* · 闹钟包 / 策略 / 文案",
    },
]


def build_fig():
    fig = plt.figure(figsize=(W_IN, 12.0), dpi=180)
    renderer = None
    item_texts = []
    header_texts = []
    free_texts = []
    box_rects = []

    def text_w_in(text, fontsize):
        nonlocal renderer
        prop = FontProperties(fname=FONT_FILE, size=fontsize)
        if renderer is None:
            renderer = fig.canvas.get_renderer()
        w_px, _h_px, _d_px = renderer.get_text_width_height_descent(text, prop, ismath=False)
        return w_px / fig.dpi

    def wrap(text, max_w, fontsize):
        lines = []
        for raw in text.split("\n"):
            cur = ""
            for ch in raw:
                if cur and text_w_in(cur + ch, fontsize) > max_w:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            if cur:
                lines.append(cur)
        return lines

    wrapped = []
    total_h = 0.0
    for layer in LAYERS:
        lines = []
        for item in layer["items"]:
            lines.extend(wrap(item, BOX_W - 0.5, ITEM_FS))
        body_h = PAD_TOP + len(lines) * LINE_H + PAD_BOTTOM
        box_h = HEADER_H + body_h
        wrapped.append({"layer": layer, "lines": lines, "box_h": box_h})
        total_h += box_h

    total_h += (len(LAYERS) - 1) * GAP
    H_IN = total_h + 1.85

    fig.set_size_inches(W_IN, H_IN)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W_IN)
    ax.set_ylim(0, H_IN)
    ax.axis("off")

    # 顶部标题
    ax.text(
        LEFT_MARGIN,
        H_IN - 0.72,
        "MiHealth 睡眠智能体系统 · 三端架构图",
        fontsize=TITLE_FS,
        fontweight="bold",
        color="#222831",
        ha="left",
        va="center",
    )
    free_texts.append(ax.texts[-1])
    ax.text(
        W_IN - RIGHT_LANE - 0.0,
        H_IN - 0.72,
        "数据源 → 仪表盘 → Agent → Android",
        fontsize=9,
        color="#607080",
        ha="right",
        va="center",
    )
    free_texts.append(ax.texts[-1])

    y_top = H_IN - 1.35
    boxes = []
    for idx, w in enumerate(wrapped):
        layer = w["layer"]
        box_h = w["box_h"]
        y_bottom = y_top - box_h
        x = LEFT_MARGIN

        # 外框 + 正文区
        ax.add_patch(
            Rectangle(
                (x, y_bottom),
                BOX_W,
                box_h,
                facecolor=layer["body"],
                edgecolor=layer["edge"],
                linewidth=1.4,
                zorder=2,
            )
        )
        # 标题带
        body_h = box_h - HEADER_H
        ax.add_patch(
            Rectangle(
                (x, y_bottom + body_h),
                BOX_W,
                HEADER_H,
                facecolor=layer["header"],
                edgecolor=layer["edge"],
                linewidth=1.4,
                zorder=3,
            )
        )
        ax.text(
            x + 0.22,
            y_bottom + body_h + HEADER_H / 2,
            layer["title"],
            fontsize=HEADER_FS,
            fontweight="bold",
            color="white",
            ha="left",
            va="center",
            zorder=4,
        )
        header_texts.append((ax.texts[-1], (x, y_bottom + body_h, x + BOX_W, y_top)))
        ax.text(
            x + BOX_W - 0.22,
            y_bottom + body_h + HEADER_H / 2,
            layer["subtitle"],
            fontsize=9,
            color="white",
            alpha=0.94,
            ha="right",
            va="center",
            zorder=4,
        )
        header_texts.append((ax.texts[-1], (x, y_bottom + body_h, x + BOX_W, y_top)))

        for i, line in enumerate(w["lines"]):
            y = y_bottom + body_h - PAD_TOP - LINE_H * (i + 0.5)
            ax.text(
                x + 0.28,
                y,
                line,
                fontsize=ITEM_FS,
                color="#1F2937",
                ha="left",
                va="center",
                zorder=4,
            )
            item_texts.append((ax.texts[-1], (x, y_bottom, x + BOX_W, y_bottom + body_h)))

        boxes.append({"x": x, "y_top": y_top, "y_bottom": y_bottom, "box_h": box_h})
        box_rects.append((x, y_bottom, x + BOX_W, y_top))
        y_top = y_bottom - GAP

    arrow_x = LEFT_MARGIN + BOX_W / 2
    for a in ARROWS:
        src = boxes[a["from"]]
        dst = boxes[a["to"]]
        y1 = src["y_bottom"] - 0.10
        y2 = dst["y_top"] + 0.10
        ax.add_patch(
            FancyArrowPatch(
                (arrow_x, y1),
                (arrow_x, y2),
                arrowstyle="-|>",
                mutation_scale=18,
                linewidth=1.6,
                color="#5B6B7A",
                zorder=1,
            )
        )
        mid = (y1 + y2) / 2
        ax.text(
            arrow_x,
            mid,
            a["label"],
            fontsize=ARROW_FS,
            color="#334155",
            ha="center",
            va="center",
            bbox=dict(boxstyle="round,pad=0.28", fc="white", ec="#C9D2DA", lw=0.8),
            zorder=5,
        )
        free_texts.append(ax.texts[-1])

    # 右侧回流通道：Android 实时数据 / 复盘 上传 Agent
    android = boxes[3]
    agent = boxes[2]
    lane_x = W_IN - RIGHT_LANE + 0.55
    y_agent_edge = agent["y_bottom"] + agent["box_h"] * 0.45
    y_android_edge = android["y_top"] - android["box_h"] * 0.35
    ax.add_patch(
        FancyArrowPatch(
            (lane_x, y_android_edge),
            (lane_x, y_agent_edge + 0.22),
            arrowstyle="-|>",
            mutation_scale=16,
            linewidth=1.4,
            linestyle=(0, (5, 3)),
            color="#8A6D3B",
            zorder=1,
        )
    )
    ax.text(
        lane_x,
        (y_android_edge + y_agent_edge) / 2,
        "实时心率 / 整晚曲线\n唤醒回执 · 复盘",
        fontsize=8.5,
        color="#6B4F1F",
        ha="center",
        va="center",
        bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#E0C98A", lw=0.8),
        zorder=5,
    )
    free_texts.append(ax.texts[-1])

    # 布局校验：文字包围盒必须落在目标框内，自由标注不得与任何方框相交
    renderer = fig.canvas.get_renderer()
    inv = ax.transData.inverted()

    def text_data_bbox(t):
        bb = t.get_window_extent(renderer)
        p0 = inv.transform((bb.x0, bb.y0))
        p1 = inv.transform((bb.x1, bb.y1))
        return (min(p0[0], p1[0]), min(p0[1], p1[1]), max(p0[0], p1[0]), max(p0[1], p1[1]))

    def inside(inner, outer, tol=0.02):
        return (
            inner[0] >= outer[0] - tol
            and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol
            and inner[3] <= outer[3] + tol
        )

    def intersects(a, b, tol=0.02):
        return not (
            a[2] <= b[0] + tol
            or a[0] >= b[2] - tol
            or a[3] <= b[1] + tol
            or a[1] >= b[3] - tol
        )

    problems = []
    for t, rect in item_texts + header_texts:
        if not inside(text_data_bbox(t), rect):
            problems.append(("框内文字出界", t.get_text(), text_data_bbox(t), rect))
    for t in free_texts:
        bb = text_data_bbox(t)
        for rect in box_rects:
            if intersects(bb, rect):
                problems.append(("自由标注与方框相交", t.get_text(), bb, rect))
    for i in range(len(box_rects)):
        for j in range(i + 1, len(box_rects)):
            if intersects(box_rects[i], box_rects[j], tol=0.0):
                problems.append(("方框重叠", i, j, box_rects[i], box_rects[j]))

    if problems:
        print("布局问题:")
        for p in problems:
            print(p)
    else:
        print("布局校验通过：无文字出框、无标注侵框、无方框重叠")

    fig.savefig(OUT_PATH, dpi=200)
    plt.close(fig)
    print(f"OK: {OUT_PATH}  ({W_IN:.1f} x {H_IN:.1f} in)")


if __name__ == "__main__":
    build_fig()
