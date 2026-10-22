# AI 智能闹钟（Wake Engine）设计文档

> 2026-10-15 定稿。严格按用户确认的策略实现，不擅自扩展。

## 1. 目标

在用户设定的起床区间内，结合整晚心率曲线 + Agent 历史预测，在浅睡/REM 期用渐进式闹钟叫醒；若窗口内没有合适时机，保底时刻强制叫醒。叫醒必须零同步延迟、不丢、不被系统音量限制。

## 2. 核心架构

```
入睡后（心率广播开 + 到达入睡时间前10分钟）
  → 手机本地记录整晚心率曲线（带时间戳，不上传）
  → 窗口最早时间前 10 分钟：打包整晚曲线 → 一次性 POST 给 Agent
  → Agent 结合历史数据 + 当前曲线 → 10 分钟内制定唤醒策略 → 下发手机
  → 手机在窗口内本地执行策略（渐进式响铃 / 深睡保护 / 保底）
  → 唤醒后：手机把完整曲线（到唤醒时刻）→ 再 POST 给 Agent 复盘
  → Agent 比对预测 vs 实际 → 存入 memory → 迭代进化
```

## 3. 数据流与接口

### 3.1 手机本地存储

- 新文件：`filesDir/wake_hr_curves/<date>.json`
- 字段：`{"date":"2026-10-15","start_ts":...,"points":[{"ts":...,"bpm":...}]}`
- 与云端 JSON 完全分开（实时记录，非云端拉取）
- 保留最近 30 天，超出自动清理最旧

### 3.2 Agent 接口

#### POST `/api/agent/wake_plan`

请求：
```json
{
  "user_id": "xxx",
  "date": "2026-10-15",
  "curve": [{"ts": 1790000000, "bpm": 58}, ...],
  "wake_start": "08:00",
  "wake_end": "08:30"
}
```

响应（wake_pack）：
```json
{
  "ok": true,
  "pack": {
    "window": {"start": "08:00", "end": "08:30"},
    "strategy": "light_sleep_mid" | "light_sleep_late" | "rem" | "deep_protect" | "fallback_only",
    "params": {
      "hr_baseline": 58,
      "light_sleep_hr_rise": 6,
      "light_sleep_duration_min": 2,
      "deep_protect_trigger": "08:12",
      "prefer_second_light": true,
      "second_light_expected": "08:18",
      "awake_confirm": {"hr_rise_pct": 15, "duration_sec": 30}
    },
    "condition_label": "浅睡中期唤醒"
  }
}
```

#### POST `/api/agent/wake_review`

请求：
```json
{
  "user_id": "xxx",
  "date": "2026-10-15",
  "curve_full": [{"ts":..., "bpm":...}],
  "wake_pack": {...},
  "actual_wake_time": "08:14",
  "outcome": "light_sleep_woke" | "deep_protect_woke" | "fallback_woke" | "missed"
}
```

Agent 比对预测 vs 实际，更新 tolerance / 预测模型，存入 memory。

## 4. 手机 WakeEngine 执行规则（状态机）

```
状态：IDLE → RECORDING → WAITING_PLAN → ARMED → RINGING → CONFIRMING → DONE

IDLE：
  心率广播开 + 到达入睡时间前10分钟 → RECORDING

RECORDING：
  本地记录心率曲线（1s 粒度）
  到达 wake_start - 10min → 打包上传 wake_plan → WAITING_PLAN

WAITING_PLAN：
  等待 Agent 返回 wake_pack（重试 3 次，失败用本地默认策略）
  到达 wake_start → ARMED

ARMED（窗口内，每 1s 看心率）：
  - 窗口开始前：绝不叫（硬边界）
  - 浅睡/REM 信号（HR 回升+波动，连续 N 分钟）→ RINGING
    - 若 prefer_second_light=true 且预测有第二次浅睡 → 跳过第一次，等第二次
    - 否则立即叫醒
  - 深睡保护：到达 deep_protect_trigger → RINGING
  - 保底：到达 wake_end → RINGING（setAlarmClock，App 被杀也响）

RINGING：
  渐进式：每 5 秒升一档音量（30% → 100%）
  30 秒确认：心率未达 awake_confirm 标准 → 疯狂震动 + 最大音量
  用户关闭 → DONE

DONE：
  上传完整曲线 wake_review
  清理本地曲线（保留 30 天）
```

## 5. 固定唤醒条件文案（不生成）

| 策略 | 文案 |
|---|---|
| 浅睡前期 | 浅睡前期唤醒 |
| 浅睡中期 | 浅睡中期唤醒 |
| 浅睡后期 | 浅睡后期唤醒 |
| REM | 快速眼动期唤醒 |
| 深睡保护 | 深睡保护方案唤醒 |
| 保底 | 保底时刻唤醒 |

## 6. 音量控制

- 响铃前 `AudioManager.setStreamVolume(STREAM_ALARM, max, 0)` 主动拉高闹钟音量
- 申请 `MODIFY_AUDIO_SETTINGS` 权限
- 渐进式：先 30% 音量 → 每 5 秒升一档 → 30s 后若没醒 → 100% + 震动
- 保底时刻：直接 100% + 震动

## 7. 失败兜底

- 窗口前 10 分钟上传失败：重试 3 次，仍失败用本地默认策略（保守：第一次浅睡就叫 + 保底），不阻塞唤醒
- 唤醒后复盘上传失败：重试 3 次，失败保留本地曲线，下次启动补传
- Hyperate 断了：保底响
- 引擎死了：保底响（setAlarmClock 系统级）
- 判定一直不满足：保底响

## 8. 与现有系统关系

- 睡前提醒（alarm_pack / P 系列）不动，那是入睡侧
- 智能闹钟是新的起床侧：新增 wake_pack + WakeEngine，复用 setAlarmClock、AlarmRingActivity、Hyperate 心率
- 智能闹钟开关前置校验（必须心率连接）已实现
