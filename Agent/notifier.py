# -*- coding: utf-8 -*-
"""通知编排（决策 B 主流程）：coach 状态机拍板事件 → LLM 按事件生成话术 → pending 队列。

手机 App 轮询 GET /api/agent/pending 取走通知并弹消息；取走即从队列删除。
LLM 挂了也不误事：每个事件自带本地模板兜底，话术仍贴事件。

事件驱动（非固定次数）：心率是主信号——
  收工预告(E1) / 心率仍高(E2) / 心率正常催躺(E3) / 过点未睡心率仍高(E4) / 过点心率已降(E5)。
频控：任意两条间隔≥20分钟、整晚≤4条、每事件每晚一次、检测到已入睡全夜静默。
测试模式：/api/agent/test_notify 或 preferences.test_mode.notify=true 强制生成一条。
"""
from __future__ import annotations

import json
import os
import random
import threading
from datetime import datetime

import coach
import config
import memory
import pattern as pat_mod
import windows

_PENDING_PATH = os.path.join(config.DATA_DIR, "pending.json")
_POOL_PATH = os.path.join(config.DATA_DIR, "copy_pool.json")
_LOCK = threading.Lock()

# 文案预生成池：user_id -> event -> [{text,date}...]（计划生成时预热 + 弹后补货，弹时秒取）
_POOL: dict = {}
# 最近用过的文案：user_id -> list（防近似重复，滚动 12 条）
_RECENT: dict = {}
# 正在补货的事件：user_id -> set（防 LLM 挂时疯狂起线程）
_REFILLING: dict = {}


def _pending_path(user_id: str = "") -> str:
    d = config.data_dir_for(user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "pending.json")


def _pool_path(user_id: str = "") -> str:
    d = config.data_dir_for(user_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "copy_pool.json")


def _load_pool(user_id: str = "") -> None:
    try:
        p = _pool_path(user_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                _POOL[user_id] = json.load(f)
    except Exception:
        _POOL[user_id] = {}


def _save_pool(user_id: str = "") -> None:
    try:
        p = _pool_path(user_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_POOL.get(user_id, {}), f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def _pool_of(user_id: str = "") -> dict:
    """取某用户的文案池（惰性加载）。"""
    if user_id not in _POOL:
        _load_pool(user_id)
    return _POOL.setdefault(user_id, {})


def _recent_of(user_id: str = "") -> list:
    return _RECENT.setdefault(user_id, [])


def _refilling_of(user_id: str = "") -> set:
    return _REFILLING.setdefault(user_id, set())


_load_pool("")

# 事件本地兜底模板（LLM 不可用时随机抽，话术仍贴事件）——哄的语气，不用命令句
# 注意：P60/P40/P30 模板里的时间词与弹点严格一致；HR/OD 不提时间词
_EVENT_FALLBACK = {
    "P60": [
        "还有一个小时就要睡啦，先把明天的事写两行，让脑子提前下班。",
        "距离睡觉还有一小时，灯光调暗一点、消息回完，剩下的明天满电再收拾。",
    ],
    "P40": [
        "还有四十分钟就到点了，赶紧准备一下：洗漱、收拾桌面，给睡觉腾出仪式感。",
        "四十分钟后开睡，现在正是收尾杂事的好时候。",
    ],
    "P30": [
        "只剩半小时啦，去洗漱吧。水温调暖一点，身体放松了，心率也会跟着降下来。",
        "半小时后开睡，手机该去充电了，你也一样。",
    ],
    "P20": [
        "还有二十分钟就到点了，牙膏牙刷走起，剩下的杂事明天再收。",
        "二十分钟后开睡，现在放下手机去洗把脸，时间刚刚好。",
    ],
    "P10": [
        "最后十分钟啦，灯光调到最暗，躺下慢慢呼吸，让身体先睡着。",
        "十分钟后可爱熄灯～把手机放到伸手够不着的地方，今晚就拜托啦。",
    ],
    "STAY": [
        "屏幕还亮着呢——说好的睡觉呀。手机放远，闭上眼，剩下的交给黑暗。",
        "还没睡吗？越刷越清醒哦。把这条消息划掉，关灯，晚安。",
    ],
    "HR": [
        "看你心率还有点高，身体好像还兴奋着呢。先坐着做几次慢呼吸，等它凉下来再躺会舒服很多。",
        "心率还没降下来，现在躺下可能翻来覆去。陪我做个几分钟的深呼吸，把身体哄安静？",
    ],
    "OD": [
        "还没睡着呀？越躺可能越着急。起来坐两分钟慢慢呼吸，有困意了再回床上，我陪你。",
        "睡不着没关系，先别刷手机——光会让大脑以为还没到晚上。闭眼做几次深呼吸，困意会回来的。",
    ],
}

_TONES = [
    "像心疼朋友的关心，语气软、带劝不带命令，多用'呀/呢/吧'这类缓和词",
    "像晚间电台主播，温柔低声地劝睡",
    "像家人一样念叨但不可爱说教，是担心你累的那种口吻",
    "像一个温柔的护理师，先共情（你好累/能量低）再轻声建议",
]


def _load_pending(user_id: str = "") -> list:
    p = _pending_path(user_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_pending(items: list, user_id: str = "") -> None:
    p = _pending_path(user_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items[-20:], f, ensure_ascii=False)
    os.replace(tmp, p)


def push(item: dict, user_id: str = "") -> None:
    with _LOCK:
        items = _load_pending(user_id)
        item["id"] = f"{int(datetime.now().timestamp() * 1000)}"
        item["created_at"] = datetime.now().isoformat(timespec="seconds")
        items.append(item)
        _save_pending(items, user_id)


def pop_all(user_id: str = "") -> list:
    """手机取走全部待发通知（消费式）。"""
    with _LOCK:
        items = _load_pending(user_id)
        _save_pending([], user_id)
        return items


def _polish(ev: dict, bedtime_str: str, user_id: str = "") -> tuple:
    """返回 (文案, 来源)。弹的时候绝不现场等 LLM——从池子秒取（计划生成时已预热），
    池子空就用模板，同时后台补货。"""
    event = ev["event"]
    today = datetime.now().strftime("%Y-%m-%d")
    pool = _pool_of(user_id)
    with _LOCK:
        plist = [x for x in pool.get(event, [])
                 if (x.get("date") if isinstance(x, dict) else today) == today]
        if plist:
            text = plist[0]["text"] if isinstance(plist[0], dict) else plist[0]
            pool[event] = plist[1:]
            _save_pool(user_id)
            return text, "llm"
    # 无库存：模板顶上，异步生成一批（同事件同时只有一个补货线程）
    ref = _refilling_of(user_id)
    with _LOCK:
        busy = event in ref
        if not busy:
            ref.add(event)
    if not busy:
        threading.Thread(target=_refill, args=(ev, bedtime_str, user_id), daemon=True).start()
    fallback = random.choice(_EVENT_FALLBACK.get(event, ["该休息啦。"]))
    return fallback, "template"


def _pending_events(bed_min: int, user_id: str = "") -> tuple:
    """按当前时刻过滤出"还没过点"的弹点，就近优先排序。

    核心规则（用户定版，防白烧 token）：
      距入睡时间 > 60min  → 生成全部 5 个（P60/P40/P30/P20/P10）
      40~60min            → 只生成 P40/P30/P20/P10（P60 已过，不生成）
      30~40min            → 只生成 P30/P20/P10
      20~30min            → 只生成 P20/P10
      10~20min            → 只生成 P10
      ≤10min              → 只生成 P10（若刚好到点也算，LLM 快还能赶上）
      已过入睡点          → 不生成（异常状态：剩 5 分钟时 P10 也过了，0 个）
    心率广播已与睡前提醒解耦（2026-10-16）：固定五弹始终保留。

    实现：弹点 at 必须落在 [now_abs, bed_abs] 区间内才算"今晚还没过"。
    注意不能用 _next_occurrence(at) 单独判——它会把已过点的弹点跨午夜推到明天，
    导致永远"没过点"。必须同时约束 at_abs ≤ bed_abs（入睡点之前）。
    """
    import planner
    now_dt = datetime.now().astimezone()
    now_abs = int(now_dt.timestamp() // 60)
    now_min = now_dt.hour * 60 + now_dt.minute
    # 距入睡点 >12h → 现在已过今晚入睡点（_next_occurrence 会推到明天），今晚周期结束
    if (bed_min - now_min) % 1440 > 720:
        return ()
    bed_abs = planner._next_occurrence(bed_min, now_dt)
    events = (("P60", 60), ("P40", 40), ("P30", 30), ("P20", 20), ("P10", 10))
    out = []
    for e, lead in events:
        at = (bed_min - lead) % 1440
        at_abs = planner._next_occurrence(at, now_dt)
        # 严格小于：弹点刚好到点(=now)算已过，不生成（LLM 来不及，白烧 token）
        if now_abs < at_abs <= bed_abs:   # 在(现在, 入睡点]内 = 今晚还没过
            out.append((e, lead, at_abs - now_abs))
    # 就近优先：剩余分钟少的排前（最近的先发起、先入池）
    out.sort(key=lambda x: x[2])
    return tuple((e, lead) for e, lead, _ in out)


def pregen_next(bed_min: int, bedtime_str: str, user_id: str = "") -> dict | None:
    """弹前 2 分钟调用：只生成"即将到来的弹点"的 1 条文案，注入最新实时数据。

    与 pregenerate（批量预生成）的区别：
      · 只取 _pending_events 的第一个（就近优先 = 即将弹的那个）
      · 生成时注入 coach 实时数据（hr_now/trend/energy）——刚弹过小米同步、
        刚拉过云端，数据是新的 → 文案有温度（"监测你的人"而不是"文案机器"）
      · 生成成功 → 直接覆盖闹钟包对应弹点的文案（version 变，手机重定）
      · 失败 → 闹钟包保持原兜底文案，到点照响（到点必响永远保底）

    返回 {event, text} 或 None（无可生成弹点/失败）。
    """
    import coach
    pending = _pending_events(bed_min, user_id)
    if not pending:
        return None
    event, lead = pending[0]
    # 直观反馈（用户对照用）：开始生成
    print(f"[PreGenNext][{user_id or 'root'}] 🚀 开始生成 {event} (bedtime={bedtime_str}) "
          f"@ {datetime.now().strftime('%H:%M:%S')}")
    # 用 coach 实时数据构建 ev（decide 会读最新云端+广播心率）。
    # force=True：不 mark 状态（pregen 只是取数据，不该消费弹点），且总返回事件
    try:
        ev = coach.decide(bed_min, force=True, user_id=user_id) or {}
    except Exception:
        ev = {}
    ev = {
        "event": event, "lead_min": lead, "mins_to_bed": lead,
        "hr_now": ev.get("hr_now"), "hr_base": ev.get("hr_base"),
        "trend": ev.get("trend"), "energy": ev.get("energy"),
        "urgency": ev.get("urgency", "normal"),
    }
    try:
        import brain
        r = brain.ask(_build_prompt(ev, bedtime_str, [], user_id),
                      snapshot=False, temperature=1.0, timeout=60,
                      use_history=False, tag="pregen_next", lean=True,
                      user_id=user_id)
        if not r["ok"]:
            return None
        text = r["reply"].strip().strip('"“”')
        if not (8 <= len(text) <= 140):
            return None
    except Exception:
        return None
    # 直接覆盖闹钟包对应弹点文案（精准替换，不污染其他弹点）
    try:
        prefs = memory.get_preferences(user_id)
        pack = prefs.get("alarm_pack") or {}
        items = pack.get("items") or []
        changed = False
        for it in items:
            if it.get("event") == event:
                it["body"] = text
                it["src"] = "ai"
                changed = True
        if changed:
            pack["version"] = int(datetime.now().timestamp())
            memory.update_preferences_shallow({"alarm_pack": pack}, user_id)
            # ★ 2026-10-15 修复：生成完 AI 文案【必须通知手机拉包】——
            #   旧代码只改包不 push pack_update，手机快线(20s)不知道包变了、
            #   不拉新包重定闹钟 → 响铃时用的还是旧模板包（兜底文案），
            #   响铃后 refreshLatestPack 补救才发现 AI 文案（"先兜底、10秒后变AI"）。
            #   现在 push 后手机 20 秒内拉包重定 → 响铃直接用 AI 文案。
            try:
                push({"type": "pack_update",
                      "payload": {"version": pack["version"],
                                  "bedtime": bedtime_str,
                                  "reason": "ai_copy_ready"}}, user_id)
                print(f"[PreGenNext][{user_id or 'root'}] 📦 已通知手机拉包(version={pack['version']})")
            except Exception as e:
                print(f"[PreGenNext][{user_id or 'root'}] 通知手机拉包失败: {e!r}")
    except Exception:
        pass
    # 同时入池（下次同事件弹点还能复用）。带 ts 时间戳：区分"本次入睡周期
    # 刚生成"与"几小时前的旧文案"，供 pregen_due 判断是否已生成过。
    today = datetime.now().strftime("%Y-%m-%d")
    now_ts = int(datetime.now().timestamp())
    pool = _pool_of(user_id)
    with _LOCK:
        pool[event] = [
            x for x in pool.get(event, [])
            if isinstance(x, dict) and x.get("date") == today
        ] + [{"text": text, "date": today, "ts": now_ts}]
        _save_pool(user_id)
    # 直观反馈（用户对照用）：生成完成
    print(f"[PreGenNext][{user_id or 'root'}] ✅ 生成完成 {event} @ {datetime.now().strftime('%H:%M:%S')}")
    return {"event": event, "text": text}


def _json_fresh_secs(user_id: str = "") -> float | None:
    """最新导出 JSON 的年龄（秒）。None = 没有 JSON。按当前用户（data_source.set_user）。"""
    try:
        import data_source
        path = data_source._latest_export(user_id)
        if not path:
            return None
        return datetime.now().astimezone().timestamp() - os.path.getmtime(path)
    except Exception:
        return None


def _trigger_pull(user_id: str = "") -> None:
    """触发电脑端拉云端（异步，不等待）。"""
    try:
        import urllib.request
        import urllib.parse
        url = "http://127.0.0.1:8000/api/pull"
        if user_id:
            url += "?user_id=" + urllib.parse.quote(user_id)
        urllib.request.urlopen(url, timeout=5).read()
    except Exception:
        pass


def pregen_due(user_id: str = "") -> dict | None:
    """服务器调度器每拍调用：弹前 5 分钟自动生成"即将弹点"的实时文案。

    用户定版（替代"响铃后立即生成"）：
      · 生成时机 = 弹点前 5 分钟（不是响铃后立即）——P40 在 16:25 生成，
        而不是 16:10 响铃后马上生成（那时数据没同步完，文案没实时性）
      · ★ 2026-10-15 从"弹前 2 分钟"放宽到"弹前 5 分钟"：完整链路
        LLM 10-30s + push pack_update + 手机快线(20s)拉包重定 ≈ 1 分钟，
        弹前 2 分钟生成根本来不及 → 响铃时手机还是旧模板包（兜底文案）。
        提前到 5 分钟给足余量，响铃时 AI 文案已就位。
      · 生成前保证数据新鲜：JSON 太旧（>3 分钟）→ 先触发电脑端 pull，
        等它拉完（最多等 90 秒）再生成 → 文案带刚同步的实时心率
      · 防重入：_REFILLING 标记，避免调度器多拍重复生成同一条
      · 距睡眠不足 10 分钟：不生成（固定弹点全已过），由手机端在设置时
        弹提示"距睡眠不足 10 分钟将不会有提示"——不强行弹闹钟。

    返回 {event, text} 或 None（没到时机/已生成/失败）。
    """
    import planner
    prefs = memory.get_preferences(user_id)
    bed, _src = planner.tonight_bedtime(prefs, user_id)
    if bed is None:
        return None
    bedtime_str = pat_mod.SleepPattern.fmt(bed)
    pending = _pending_events(bed, user_id)
    if not pending:
        return None
    # 下一个弹点（就近优先 = 即将弹的）
    event, lead = pending[0]
    # 计算距弹点剩余分钟（用绝对时间戳，跨午夜安全）
    import planner as _pl
    now_dt = datetime.now().astimezone()
    now_abs = int(now_dt.timestamp() // 60)
    at_min = (bed - lead) % 1440
    at_abs = _pl._next_occurrence(at_min, now_dt)
    mins_to_ring = at_abs - now_abs
    # 还没到弹前 5 分钟 → 不生成（等下一拍）
    if mins_to_ring > 5:
        return None
    # 已过点 → 不生成（LLM 来不及，白烧 token）
    if mins_to_ring <= 0:
        return None
    # 防重入：正在生成同一条就不重复起
    ref = _refilling_of(user_id)
    pool = _pool_of(user_id)
    with _LOCK:
        if "PREGEN" in ref:
            return None
        # 该弹点【本次入睡周期】已生成过 AI 文案 → 跳过（不重复生成）。
        # 周期锚点 = alarm_pack.version（包重建时间）：设入睡时间(bed_min变)会
        # 重建包 → version 刷新 → 旧文案(ts < version)作废，重新生成；
        # 本次周期内 pregen_next 生成的新文案(ts >= version)不重复。
        # 若只看 date(今晚)，池子里几小时前的旧文案会被误当"已生成"而跳过
        # （用户观察到的"P30 弹出来是旧 AI 文案、服务器没生成 P30"）。
        today = datetime.now().strftime("%Y-%m-%d")
        pack_ver = (memory.get_preferences(user_id).get("alarm_pack") or {}).get("version", 0)
        if any(isinstance(x, dict) and x.get("date") == today
               and (x.get("ts") or 0) >= pack_ver
               for x in pool.get(event, [])):
            return None
        ref.add("PREGEN")

    def _run():
        try:
            # 数据新鲜度闸门：JSON 太旧 → 先触发电脑端拉云端，等它拉完
            age = _json_fresh_secs(user_id)
            if age is None or age > 180:
                print(f"[PreGenDue][{user_id or 'root'}] JSON 年龄 {age} 秒，先触发云端拉取...")
                _trigger_pull(user_id)
                # 等 pull 完成（增量追拉约 2 分钟，最多等 90 秒）
                waited = 0
                while waited < 90:
                    import time
                    time.sleep(5)
                    waited += 5
                    age2 = _json_fresh_secs(user_id)
                    if age2 is not None and age2 <= 180:
                        break
            pregen_next(bed, bedtime_str, user_id)
        finally:
            with _LOCK:
                ref.discard("PREGEN")

    threading.Thread(target=_run, daemon=True).start()
    return None


def pregenerate(bed_min: int, bedtime_str: str, events: tuple | None = None,
                user_id: str = "") -> int:
    """为弹点预生成 AI 文案入池，完成后重建闹钟包（手机侧系统闹钟的弹药）。

    动态数量：events=None 时按 _pending_events 过滤——只生成"还没过点"的弹点，
    已过点的不生成（不为白烧 token）。剩 10 分钟就只生成 P10 一个。
    并行生成：弹点互相独立，同时发请求（urllib 无全局锁，可并发），
    总耗时 ≈ 单条 LLM 耗时（10-30s），而不是串行的 N 倍。
    就近优先：距现在最近的弹点排最前（并行下几乎同时返回，但最近的先入池）。
    并发上限 4：避免一次打爆百炼并发限制导致部分失败。
    """
    import coach
    import threading
    from concurrent.futures import ThreadPoolExecutor

    if events is None:
        events = _pending_events(bed_min, user_id)
    if not events:
        return 0

    # 直观反馈（用户对照用）：开始生成
    print(f"[PreGen][{user_id or 'root'}] 🚀 开始生成 {len(events)} 条文案 (bedtime={bedtime_str}) "
          f"events={[e for e, _ in events]} @ {datetime.now().strftime('%H:%M:%S')}")

    results = [None] * len(events)
    def _gen(i: int, event: str, lead: int) -> None:
        ev = {
            "event": event, "lead_min": lead, "mins_to_bed": lead,
            "hr_now": None, "hr_base": None, "trend": None,
            "energy": coach.current_energy(user_id), "urgency": "normal",
        }
        try:
            import brain
            r = brain.ask(_build_prompt(ev, bedtime_str, [], user_id),
                          snapshot=False, temperature=1.0, timeout=60,
                          use_history=False, tag="pregen", lean=True,
                          user_id=user_id)
            if r["ok"]:
                text = r["reply"].strip().strip('"“”')
                if 8 <= len(text) <= 140:
                    today = datetime.now().strftime("%Y-%m-%d")
                    now_ts = int(datetime.now().timestamp())
                    with _LOCK:
                        pool = _pool_of(user_id)
                        pool[event] = [
                            x for x in pool.get(event, [])
                            if isinstance(x, dict) and x.get("date") == today
                        ] + [{"text": text, "date": today, "ts": now_ts}]
                        _save_pool(user_id)
                    results[i] = True
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=min(4, len(events))) as ex:
        futs = [ex.submit(_gen, i, e, lead) for i, (e, lead) in enumerate(events)]
        for f in futs:
            try:
                f.result()
            except Exception:
                pass

    ok = sum(1 for r in results if r)
    if ok:
        rebuild_alarm_pack(bed_min, bedtime_str, user_id)
    # 直观反馈（用户对照用）：生成完成
    print(f"[PreGen][{user_id or 'root'}] ✅ 生成完成 {ok}/{len(events)} 条 @ {datetime.now().strftime('%H:%M:%S')}")
    return ok


# ---------------- 闹钟包：今晚五弹一次性打包给手机系统闹钟 ----------------

def _items_for(bed_min: int, bedtime_str: str, user_id: str = "") -> list:
    """按当前模式拼五弹条目：文案=今晚池中最新AI文案，没有就模板兜底。"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 心率广播已与睡前提醒解耦（2026-10-16）：固定五弹始终保留。
    events = (("P60", 60), ("P40", 40), ("P30", 30), ("P20", 20), ("P10", 10))
    items = []
    pool = _pool_of(user_id)
    pack_ver = (memory.get_preferences(user_id).get("alarm_pack") or {}).get("version", 0)
    for event, lead in events:
        text = None
        src = "template"
        with _LOCK:
            # 优先取"本次入睡周期"的新鲜文案(ts >= pack_ver)；没有才退今晚旧文案
            fresh = [x for x in reversed(pool.get(event, []))
                     if isinstance(x, dict) and x.get("date") == today
                     and (x.get("ts") or 0) >= pack_ver]
            for x in (fresh or reversed(pool.get(event, []))):
                if isinstance(x, dict) and x.get("date") == today:
                    text = x["text"]
                    src = "ai"
                    break
        if not text:
            text = _EVENT_FALLBACK.get(event, ["该休息啦。"])[0]
        at = (bed_min - lead) % 1440
        # 绝对时间戳（epoch秒）：手机直接照它排闹钟，跨午夜/凌晨锚点不会错一天
        try:
            import planner
            at_ts = int(planner._next_occurrence(
                at, __import__("datetime").datetime.now().astimezone()) * 60)
        except Exception:
            at_ts = 0
        items.append({"event": event, "lead_min": lead, "title": "😴 小眠提醒",
                      "body": text, "src": src, "at_min": at, "at_ts": at_ts,
                      "at": "%02d:%02d" % (at // 60, at % 60)})
    return items


def get_or_build_pack(user_id: str = "") -> dict:
    """给 GET 端点用：返回今晚闹钟包。
    锚点变了（改过目标/新计划）或还没有包 → 现拼模板版落盘（version=当下时间戳）；
    AI 文案就绪后 rebuild 会用更大 version 覆盖 → 手机自动升级重定闹钟。

    ★ 授权闸门（2026-10-13 修复）：Agent 睡前提醒开关（agent_auth.notify）关闭时，
      不生成/不返回闹钟包——用户关了就不该再有任何提醒。返回空包（items=[]），
      手机端检测到空包自动清空已定闹钟。

    关键：重建包时【同步触发 AI 生成】（后台线程）——否则池子空、
    窗口未激活时 maybe_prewarm 不跑，闹钟包一直是模板版，第一次响铃就显示
    "AI 生成失败，使用兜底文案"。触发后 LLM 10-30s 完成，rebuild 覆盖升级。

    只生成【最近的 1 条】（就近优先第一个弹点），不批量烧 token：
    用户定版 = 设置时间→只生成第一次文案(通用话术)+立即排闹钟；响铃时
    pregen_next 逐个补下一条。批量生成 5 条是旧逻辑，白烧 4 条 token。"""
    import planner
    prefs = memory.get_preferences(user_id)
    # ★ 授权闸门：睡前提醒开关关闭 → 返回空包（手机端清空闹钟）
    auth = prefs.get("agent_auth") or {}
    if auth.get("notify") is False:
        empty = {"date": datetime.now().strftime("%Y-%m-%d"),
                 "bedtime": None, "bed_min": None,
                 "version": int(datetime.now().timestamp()),
                 "items": []}
        # 若之前有包，清掉（version 刷新让手机端检测到变化）
        old = prefs.get("alarm_pack") or {}
        if old.get("items"):
            memory.update_preferences_shallow({"alarm_pack": empty}, user_id)
        return empty
    bed, _src = planner.tonight_bedtime(prefs, user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    # ★ 2026-10-17 修复：AI 计划刚过点、同晚未结束 → 不回落学习基线重建，
    #   同时清掉旧闹钟包（否则会残留 02:46 这类历史基线排的闹钟）。
    if _src == "expired":
        empty = {"date": today, "bedtime": None, "bed_min": None,
                 "version": int(datetime.now().timestamp()), "items": []}
        old = prefs.get("alarm_pack") or {}
        if old.get("items") or old.get("bed_min") is not None:
            memory.update_preferences_shallow(
                {"alarm_pack": empty, "alarm_ack_events": []}, user_id)
            try:
                push({"type": "pack_update",
                      "payload": {"version": empty["version"],
                                  "bedtime": None,
                                  "reason": "plan_expired_clear"}}, user_id)
                print(f"[Pack][{user_id or 'root'}] 计划已过期，清空闹钟包 "
                      f"ver={empty['version']}")
            except Exception as e:
                print(f"[Pack][{user_id or 'root'}] 清空闹钟包通知失败: {e!r}")
        return empty
    pack = prefs.get("alarm_pack") or {}
    if bed is not None and (pack.get("date") != today
                            or pack.get("bed_min") != bed):
        pack = {"date": today, "bedtime": pat_mod.SleepPattern.fmt(bed),
                "bed_min": bed,
                "version": int(datetime.now().timestamp()),
                "items": _items_for(bed, pat_mod.SleepPattern.fmt(bed), user_id)}
        memory.update_preferences_shallow({"alarm_pack": pack}, user_id)
        # 包重建（锚点/时间变了）→ 旧 ack 作废，清掉防误吞新弹点
        memory.update_preferences_shallow({"alarm_ack_events": []}, user_id)
        print(f"[Pack][{user_id or 'root'}] 重建闹钟包: bed={pat_mod.SleepPattern.fmt(bed)} "
              f"ver={pack['version']} items={[i['event'] for i in pack['items']]}")
        # ★ AI 规划/手动改时间重建模板包后 → 立即通知手机拉包（不等 5 分钟慢线）。
        #   手机快线发现 pack_update 标记就 syncFromServer(force=true) 拉包排闹钟，
        #   与手动模式同链路"瞬间同步 + 生成闹钟 + 推送"。
        #   只在真正重建时写（bed_min 变化），手机拉包时 bed_min 不变不重建 → 不循环。
        try:
            push({"type": "pack_update",
                  "payload": {"version": pack["version"],
                              "bedtime": pat_mod.SleepPattern.fmt(bed),
                              "reason": "plan_or_manual_ready"}}, user_id)
        except Exception as e:
            print(f"[Pack][{user_id or 'root'}] 通知手机拉包失败: {e!r}")
        # 同步触发 AI 生成（后台线程，不阻塞返回）：只生成最近的 1 条。
        # 判定：距第一个弹点 ≤5 分钟才立即生成（设置时间临近弹点，如 16:05 设
        # 16:10 弹）；否则不生成，等调度器 pregen_due 到弹前 5 分钟再生成
        # （避免 15:00 设置就提前生成 16:10 的文案，数据不新鲜）。
        # ★ 2026-10-15 从 ≤2 放宽到 ≤5（与 pregen_due 同步）。
        bedtime_str = pat_mod.SleepPattern.fmt(bed)
        try:
            now_dt = datetime.now().astimezone()
            now_abs = int(now_dt.timestamp() // 60)
            pending = _pending_events(bed, user_id)
            if pending:
                ev0, lead0 = pending[0]
                at_min0 = (bed - lead0) % 1440
                at_abs0 = planner._next_occurrence(at_min0, now_dt)
                if 0 < at_abs0 - now_abs <= 5:
                    import threading
                    ref = _refilling_of(user_id)
                    with _LOCK:
                        if "PREGEN" not in ref:
                            ref.add("PREGEN")
                    def _run():
                        try:
                            pregen_next(bed, bedtime_str, user_id)
                        finally:
                            with _LOCK:
                                ref.discard("PREGEN")
                    threading.Thread(target=_run, daemon=True).start()
        except Exception:
            pass
    return pack


def rebuild_alarm_pack(bed_min: int, bedtime_str: str, user_id: str = "") -> None:
    """AI 文案就绪后重建闹钟包（version 刷新，手机下一拍检测到自动重定）。"""
    pack = {"date": datetime.now().strftime("%Y-%m-%d"),
            "bedtime": bedtime_str, "bed_min": bed_min,
            "version": int(datetime.now().timestamp()),
            "items": _items_for(bed_min, bedtime_str, user_id)}
    memory.update_preferences_shallow({"alarm_pack": pack}, user_id)
    # 包重建 → 旧 ack 作废（version 已变，防旧抑制清单误吞新弹点）
    memory.update_preferences_shallow({"alarm_ack_events": []}, user_id)
    # ★ 生成完 AI 文案 → 立即通知手机拉包（不等 5 分钟慢线）：
    #   手机快线每 20 秒查 pending，发现 pack_update 标记就立刻 syncFromServer(force=true)
    #   拉最新包重定闹钟 → 响铃直接用 AI 文案，不再"先兜底、10秒后变 AI"。
    #   不提前生成（保持弹前 2 分钟，保证文案用最新同步数据）。
    try:
        push({"type": "pack_update",
              "payload": {"version": pack["version"],
                          "bedtime": bedtime_str,
                          "reason": "ai_copy_ready"}}, user_id)
        print(f"[Pack][{user_id or 'root'}] 📦 闹钟包已重建(version={pack['version']}) "
              f"→ 已通知手机拉包排闹钟")
    except Exception as e:
        print(f"[Pack][{user_id or 'root'}] 通知手机拉包失败: {e!r}")


def maybe_prewarm(bed_min: int, bedtime_str: str, user_id: str = "") -> None:
    """窗口激活时每拍检查：池子里缺今晚弹点文案 → 后台补热。
    覆盖手动模式（不触发计划生成，也就没有计划侧的预生成）。
    只补【弹前 5 分钟内】的最近 1 条——与 pregen_due 同一判定：
    没到 5 分钟窗口不生成（避免提前生成、数据不新鲜）；已过点不补（白烧 token）。
    ★ 2026-10-15 从 2 分钟放宽到 5 分钟（与 pregen_due 同步，给 LLM+拉包留余量）。"""
    import planner as _pl
    now_dt = datetime.now().astimezone()
    now_abs = int(now_dt.timestamp() // 60)
    want = _pending_events(bed_min, user_id)
    if not want:
        return
    event, lead = want[0]  # 就近优先 = 即将弹的第一个
    # 距弹点剩余分钟（绝对时间戳，跨午夜安全）
    at_min = (bed_min - lead) % 1440
    at_abs = _pl._next_occurrence(at_min, now_dt)
    mins_to_ring = at_abs - now_abs
    # 弹前 5 分钟窗口外 → 不生成（等 pregen_due 到点触发）
    if mins_to_ring > 5 or mins_to_ring <= 0:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    ref = _refilling_of(user_id)
    pool = _pool_of(user_id)
    with _LOCK:
        if "PREGEN" in ref:
            return
        # 该弹点【本次入睡周期】已生成过 AI 文案则跳过（不重复生成）。
        # 与 pregen_due 同一判定：只看 date 会被几小时前的旧文案骗过。
        pack_ver = (memory.get_preferences(user_id).get("alarm_pack") or {}).get("version", 0)
        if any(isinstance(x, dict) and x.get("date") == today
               and (x.get("ts") or 0) >= pack_ver
               for x in pool.get(event, [])):
            return
        ref.add("PREGEN")

    def _run():
        try:
            pregen_next(bed_min, bedtime_str, user_id)
        finally:
            with _LOCK:
                ref.discard("PREGEN")

    threading.Thread(target=_run, daemon=True).start()


def _build_prompt(ev: dict, bedtime_str: str, avoid: list, user_id: str = "") -> str:
    event = ev["event"]
    # 时间词：只允许 lead_min，lead=0（HR/OD/测试）严禁提时间
    lead = ev.get("lead_min") or 0
    if lead:
        time_rule = f"- 时间词：距入睡还有 {lead} 分钟。文案里如果要提时间，只能用 {lead} 分钟/约{('半小时' if lead <= 30 else ('四十分钟' if lead <= 40 else '一小时'))}，禁止出现其他任何时间数字。\n"
    else:
        time_rule = "- 时间词：本次禁止出现任何'还有X分钟/小时'的说法（时间未到固定弹点或已过点）。\n"
    hr_line = ""
    if ev.get("hr_now") and ev.get("hr_base"):
        hr_line = f"- 当前心率 {ev['hr_now']}，你的睡前基线 {ev['hr_base']}\n"
    # 无实时心率时严禁编造心率数值/趋势（闹钟包文案是提前生成的，拿不到实时数据）
    else:
        hr_line = ("- 无实时心率数据：文案禁止出现任何具体心率数字或'心率已降到X'的说法；"
                   "心率只能以期许式提及，如'让心率慢慢降到安睡水位''心脏该下班了'。\n")
    trend = ev.get("trend") or {}
    trend_line = ""
    d = trend.get("direction")
    if d == "falling":
        trend_line = f"- 心率趋势：最近从 {trend.get('first')} 慢慢降到 {trend.get('last')}（在放松，值得夸一句、让它保持）\n"
    elif d == "rising":
        trend_line = f"- 心率趋势：最近从 {trend.get('first')} 升到 {trend.get('last')}（有点兴奋/紧张，可以轻轻问一句是不是在想事）\n"
    elif d == "steady":
        trend_line = f"- 心率趋势：平稳在 {trend.get('last')} 左右\n"
    en = ev.get("energy")
    en_line = ""
    if en is not None:
        word = ("几乎见底" if en <= 10 else "很低" if en <= 25
                else "还剩一半左右" if en <= 55 else "还不错")
        en_line = f"- 今日能量 {en:.0f}%（{word}）。引用就说\"{en:.0f}%\"或\"{word}\"，不许放大或改写数字。\n"
    day_line = ""
    try:
        import planner
        prefs = memory.get_preferences(user_id)
        plan = planner.fresh_plan(prefs, user_id) or {}
        bits = []
        if plan.get("note"):
            bits.append(f"今晚计划备注：{plan['note']}")
        day = planner._context_text(prefs, user_id)
        for ln in day.splitlines():
            if "今天" in ln:
                bits.append(ln.lstrip("- "))
        if bits:
            day_line = "- 今日素材（可挑一条揉进文案，如'今天走了XX步累了吧'）：\n" + \
                "".join(f"  · {b}\n" for b in bits[:4])
    except Exception:
        pass
    avoid_line = ""
    if avoid:
        avoid_line = "- 以下说法最近用过，别再重复或近似：\n" + \
            "".join(f"  · {a}\n" for a in avoid[-6:])
    return (
        f"你是睡眠助手小眠，正在哄用户去睡觉。事件：{coach.EVENT_TEXT[event]}（今晚建议 {bedtime_str} 入睡）。\n"
        + time_rule + hr_line + trend_line + en_line + day_line + avoid_line
        + f"语气：{random.choice(_TONES)}。\n"
        "创作要求：把上面的心率/能量/今日素材揉进话里（可以打趣'心跳还这么快，是在想谁呢'，"
        "也可以夸'心率慢慢降下来了，保持住'，也可以心疼'今天走这么远，腿不酸吗'），"
        "而不是干报数据。绝对禁止命令句（不许'立刻执行/别磨蹭/该躺了/必须'），先共情再轻声劝。"
        "25-50 字，口语化，不列表，最多一个emoji，直接输出文案本身。"
    )


def _refill(ev: dict, bedtime_str: str, user_id: str = "") -> None:
    """后台线程：为当前事件生成 3 条不同文案入池（LLM 慢不怕，弹不等待）。"""
    import brain
    event = ev["event"]
    try:
        _refill_inner(brain, ev, bedtime_str, event, user_id)
    finally:
        with _LOCK:
            _refilling_of(user_id).discard(event)


def _refill_inner(brain, ev: dict, bedtime_str: str, event: str, user_id: str = "") -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    pool = _pool_of(user_id)
    recent = _recent_of(user_id)
    for _ in range(3):
        with _LOCK:
            avoid = list(recent)
        r = brain.ask(_build_prompt(ev, bedtime_str, avoid, user_id),
                      snapshot=False, temperature=1.0, timeout=60,
                      use_history=False, tag="copy", lean=True,
                      user_id=user_id)
        if not r["ok"]:
            break
        text = r["reply"].strip().strip('"“”')
        if not (8 <= len(text) <= 140):
            continue
        with _LOCK:
            plist = [x for x in pool.get(event, [])
                     if isinstance(x, dict) and x.get("date") == today]
            if text in recent or any(x["text"] == text for x in plist):
                continue
            pool[event] = plist + [{"text": text, "date": today}]
            _save_pool(user_id)
            recent.append(text)
            if len(recent) > 12:
                recent.pop(0)


def _append_live_event(out: dict, user_id: str = "") -> None:
    """把 STAY/HR/OD 实时事件动态追加到闹钟包（全闹钟模式）。

    实时事件没有固定弹点（屏幕还亮着/心率高/过点没睡时触发），
    触发即追加一条带 at_ts 的闹钟条目（now + 1 分钟，给手机排闹钟留时间），
    version 变 → 手机下一拍检测到重定 → 到点全屏响铃。
    同事件已有未过点条目则不重复追加（coach 的 fired/冷却已控频）。
    """
    ev = out["event"]
    now_abs = int(datetime.now().timestamp() // 60)
    at_abs = now_abs + 1          # 1 分钟后响（实时事件要"尽快"，但给排闹钟留时间）
    try:
        prefs = memory.get_preferences(user_id)
        pack = prefs.get("alarm_pack") or {}
        items = pack.get("items") or []
        # 同事件已有未过点条目 → 不重复追加
        for it in items:
            if it.get("event") == ev and it.get("at_ts", 0) > now_abs * 60:
                return
        # 清掉已过点的旧条目（只留未来要响的）
        items = [it for it in items if it.get("at_ts", 0) > now_abs * 60]
        items.append({
            "event": ev, "lead_min": 0, "title": "😴 小眠提醒",
            "body": out["body"], "src": out["src"],
            "at_min": (at_abs % 1440),
            "at_ts": at_abs * 60,
            "at": "%02d:%02d" % ((at_abs % 1440) // 60, (at_abs % 1440) % 60),
        })
        pack["items"] = items
        pack["version"] = int(datetime.now().timestamp())
        memory.update_preferences_shallow({"alarm_pack": pack}, user_id)
    except Exception:
        pass


def run_pre_sleep_check(uid: str = "", force: bool = False) -> dict | None:
    """后台同步节拍每 5 分钟调一次；force=测试模式。uid=user_id（多用户隔离）。"""
    prefs = memory.get_preferences(uid)
    test_mode = ((prefs.get("test_mode") or {}).get("notify")) is True
    force = force or test_mode
    # 授权闸门（测试模式也尊重）
    auth = prefs.get("agent_auth") or {}
    if auth.get("notify") is False:
        return None
    if not force and not windows.active("pre_sleep", uid):
        return None                                   # 窗口外零成本
    import planner
    bed, _src = planner.tonight_bedtime(prefs, uid)
    if bed is None:
        return None
    # 窗口已激活：确保今晚 P60/P40/P30 文案已预热（手动模式无计划侧预热的兜底）
    if not force:
        maybe_prewarm(bed, pat_mod.SleepPattern.fmt(bed), uid)
    ev = coach.decide(bed, force=force, user_id=uid)
    if not ev:
        return None
    out = {
        "kind": "pre_sleep",
        "event": ev["event"],
        "bedtime": pat_mod.SleepPattern.fmt(bed),
        "bed_src": _src,
        "hr_now": ev.get("hr_now"),
        "hr_base": ev.get("hr_base"),
        "mins_to_bed": ev.get("mins_to_bed"),
        "lead_min": ev.get("lead_min"),
        "energy": ev.get("energy"),
        "urgency": ev.get("urgency", "normal"),
        "title": "⚠️ 睡前提醒" if ev.get("urgency") == "high" else "😴 睡前提醒",
        "test": bool(force),
    }
    # 闹钟模式抑制：今晚该事件已由系统闹钟响铃兜底（手机 ack 过），
    # 不再推通知（避免"响铃+通知"双重轰炸）；STAY/HR/OD 属实时驱动，照推
    # 关键：ack 必须对应当前闹钟包 version 才有效——若用户刚改过入睡时间，
    # 包已重建（version 变），旧 ack 的抑制清单作废，否则新弹点会被误吞
    # 注意：抑制只针对 P 系列固定弹点；STAY/HR/OD 实时事件不受 ack 抑制
    #（它们动态追加闹钟，若被 ack 抑制会丢失——实时提醒必须每次都响）
    if not force and out["event"] in ("P60", "P40", "P30", "P20", "P10"):
        prefs = memory.get_preferences(uid)
        acked = prefs.get("alarm_ack_events")
        ack_ver = prefs.get("alarm_ack_version")
        pack_ver = (prefs.get("alarm_pack") or {}).get("version")
        if (isinstance(acked, list) and out["event"] in acked
                and ack_ver is not None and ack_ver == pack_ver):
            return None
        # 全闹钟模式：P 系列已由固定闹钟处理（_items_for 生成固定弹点条目），
        # run_pre_sleep_check 的 P 分支【不 push 不追加】——纯属"确认窗口命中"。
        # 关键：P 系列【不调 _polish】——_polish 是消费式的（从池子弹出文案），
        # 弹出后闹钟包没同步，打印的文案手机根本不会用（手机响铃用闹钟包 items），
        # 白白消耗池子 + 打印一条误导用户的文案（用户看到"能量39%"以为手机弹这个）。
        # 所以 P 系列直接返回 None（不打印"已生成睡前提醒"）。
        # STAY/HR/OD 实时事件才真正 _polish + 追加闹钟（下面才调用）。
        if not force:
            return None
    out["body"], out["src"] = _polish(ev, out["bedtime"], uid)
    # 全闹钟模式（用户定版）：所有睡前提醒都走系统闹钟，通知弹窗彻底下线。
    # STAY/HR/OD 实时事件【动态追加】到闹钟包（带 at_ts 绝对时间戳），
    # 手机照常排 setAlarmClock → 到点全屏响铃。不再 push 通知。
    if force:
        push({"type": "notification", "payload": out}, uid)
    elif ev["event"] in ("STAY", "HR", "OD"):
        _append_live_event(out, uid)
    # 续弹类事件（STAY/HR/OD）弹后补热一条，池子常备
    if ev["event"] in ("STAY", "HR", "OD"):
        threading.Thread(target=_refill, args=(ev, out["bedtime"], uid),
                         daemon=True).start()
    return out
