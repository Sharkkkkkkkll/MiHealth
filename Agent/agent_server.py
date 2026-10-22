 # -*- coding: utf-8 -*-
r"""MiHealth 睡眠 Agent 服务（架构 v0：能跑的对话大脑）。

与仪表盘 server.py 同风格：纯标准库 http.server，端口 8001。

接口：
  GET  /api/agent/ping                连通性自检（百炼 API 是否可达）
  GET  /api/agent/chat?q=你好          对话（自动注入健康快照+三层记忆）
  POST /api/agent/chat   {"q":"..."}  同上，POST 版
  GET  /api/agent/memory              查看三层记忆现状
  POST /api/agent/preferences {"sleep_goal":{...}}   手机推送作息目标
  POST /api/agent/feedback  {"kind":"wake","rating":4,"note":"..."}  回传反馈

运行：
    python D:\MIwatchAI\Agent\agent_server.py
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import config
import memory

_PLAN_NOW_TS = 0.0    # 上次"立即规划"触发时间
_PLAN_RUNNING = False # 是否正在后台生成（防连点重复触发）
import brain
import notifier
import windows
import pattern as pat_mod


def _scheduler_loop():
    """后台线程：窗口外每 5 分钟一拍（省资源），窗口内每 60 秒一拍（弹点准时）。
    多用户：遍历所有用户（含根目录单用户），每个用户独立跑 planner/notifier。"""
    import planner
    while True:
        for uid in config.list_users():
            try:
                import data_source as _ds
                _ds.set_user(uid)
                p = planner.maybe_generate(uid)
                if p:
                    print(f"[Planner][{uid or 'root'}] 今晚计划已生成: {p['bedtime']} ({p['reason'][:30]})")
            except Exception as e:
                print(f"[Planner][{uid or 'root'}] 异常: {e!r}")
            try:
                out = notifier.run_pre_sleep_check(uid=uid)
                if out:
                    print(f"[Notifier][{uid or 'root'}] 已生成睡前提醒[{out['event']}/{out.get('src')}]: "
                          f"{out['body'][:30]}…")
            except Exception as e:
                print(f"[Notifier][{uid or 'root'}] 异常: {e!r}")
            try:
                # 弹前 2 分钟定时生成实时文案（服务器端，替代"响铃后立即生成"）
                notifier.pregen_due(uid)
            except Exception as e:
                print(f"[PreGenDue][{uid or 'root'}] 异常: {e!r}")
        try:
            import windows as _w
            interval = 20 if any(_w.active(user_id=uid) for uid in config.list_users()) else 300
        except Exception:
            interval = 300
        time.sleep(interval)


def _startup_recovery():
    """Agent 启动恢复：不等调度器第一拍，立即恢复今晚计划/闹钟包/即将弹点文案。

    原则（2026-10-16 用户强调）：不破坏原有数据/生态，不能引入新 bug。
    依赖各函数自带的幂等保护：
      · maybe_generate：计划存在且参数没变 → 返回 None（零成本）
      · get_or_build_pack：bed_min 没变 → 不重建（不刷 version）
      · pregen_due：_REFILLING 防重入 + copy_pool 去重，已生成过 → 跳过
    只做"恢复"，不做"重规划/重建"，避免覆盖用户已确认的状态。
    """
    import planner
    import notifier as _n
    for uid in config.list_users():
        try:
            import data_source as _ds
            _ds.set_user(uid)
            # 1. 恢复今晚计划（若新的一天/参数变/没计划才生成，否则零成本）
            try:
                p = planner.maybe_generate(uid)
                if p:
                    print(f"[Startup][{uid or 'root'}] 恢复今晚计划: {p['bedtime']} ({p['reason'][:30]})")
            except Exception as e:
                print(f"[Startup][{uid or 'root'}] maybe_generate 异常: {e!r}")
            # 2. 恢复闹钟包（bed_min 没变不重建，手机拉包能拿到现有包）
            try:
                _n.get_or_build_pack(uid)
            except Exception as e:
                print(f"[Startup][{uid or 'root'}] get_or_build_pack 异常: {e!r}")
            # 3. 恢复即将弹点文案（弹前 5 分钟内才生成，已生成过跳过）
            try:
                _n.pregen_due(uid)
            except Exception as e:
                print(f"[Startup][{uid or 'root'}] pregen_due 异常: {e!r}")
        except Exception as e:
            print(f"[Startup][{uid or 'root'}] 恢复异常: {e!r}")


def _j(handler, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler) -> dict:
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _get_user_id(qs: dict, body: dict = None) -> str:
    """从 GET query 或 POST body 解析 user_id（小米账号 ID）。

    多用户方案 A：user_id 空 = 单用户（读根目录，兼容旧数据）。
    解析后同时设置 data_source 当前用户上下文（load() 按它找数据目录）。
    """
    uid = ""
    if qs:
        uid = (qs.get("user_id") or [""])[0]
    if not uid and body:
        uid = str(body.get("user_id") or "")
    uid = (uid or "").strip()
    import data_source as _ds
    _ds.set_user(uid)
    return uid


# ---------- 二维码登录（米家 APP 扫码，拿小米账号 user_id 当多用户 ID） ----------

_QR = {"state": "idle", "qr_url": "", "user_id": "", "ts": 0.0, "err": ""}


def qr_login(force: bool = False) -> dict:
    """GET /api/agent/qr_login：电脑端准备二维码（米家 APP 扫码），返回二维码图片链接。

    手机 App 显示二维码图片 → 用户用米家 APP 扫码 → 电脑端长轮询拿到 user_id
    → 手机轮询 /api/agent/qr_status 拿到 user_id 存本地。
    同一时刻只允许一次扫码会话（_QR 全局单会话）。
    """
    global _QR
    import threading
    # 已在扫码中且未过期（3 分钟内）→ 直接返回当前二维码，不重复弹
    if _QR["state"] == "waiting" and time.time() - _QR["ts"] < 180:
        return {"ok": True, "state": _QR["state"], "qr_url": _QR["qr_url"]}
    _QR = {"state": "waiting", "qr_url": "", "user_id": "", "ts": time.time(), "err": ""}

    def _do():
        global _QR
        try:
            # 清代理走直连（与 pull_to_json.qr_login_flow 一致）
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                        "http_proxy", "https_proxy", "all_proxy"):
                os.environ.pop(key, None)
            from mijiaAPI import mijiaAPI
            if force:
                # 强制出新二维码：用临时空认证文件，绕开默认账号的有效 token
                import tempfile
                tmp = os.path.join(tempfile.gettempdir(), "mijia_qr_force.json")
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                api = mijiaAPI(auth_data_path=tmp)
            else:
                api = mijiaAPI()
            login_data = api._get_qr_login_data()
            if not force and login_data.get("refreshed"):
                # token 有效，无需扫码，直接用已有 userId
                uid = str(api.auth_data.get("userId", ""))
                _QR["state"] = "done"
                _QR["user_id"] = uid
                _QR["ts"] = time.time()
                return
            _QR["qr_url"] = str(login_data.get("qr") or "")
            # 长轮询等扫码（阻塞，最多 120s）
            auth = api._complete_qr_login(login_data)
            uid = str(auth.get("userId", ""))
            pass_token = str(auth.get("passToken", ""))
            if uid and pass_token:
                try:
                    import pull_to_json
                    pull_to_json.save_token_for(uid, pass_token)
                    print(f"[QR] 已保存账号 {uid[-4:].rjust(len(uid), '*')} 的 token")
                except Exception as e:
                    print(f"[QR] 保存账号 token 失败: {e!r}")
            _QR["state"] = "done"
            _QR["user_id"] = uid
            _QR["ts"] = time.time()
        except Exception as e:
            _QR["state"] = "error"
            _QR["err"] = repr(e)
            _QR["ts"] = time.time()

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "state": "waiting", "qr_url": _QR["qr_url"]}


def qr_status() -> dict:
    """GET /api/agent/qr_status：手机轮询扫码结果。"""
    return {"ok": True, "state": _QR["state"],
            "qr_url": _QR["qr_url"],
            "user_id": _QR["user_id"], "err": _QR["err"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        uid = _get_user_id(qs)
        if path == "/api/agent/ping":
            _j(self, brain.ping())
        elif path == "/api/agent/chat":
            q = unquote((qs.get("q") or [""])[0]).strip()
            if not q:
                _j(self, {"ok": False, "err": "缺少参数 q"}, 400)
            else:
                _j(self, brain.ask(q, user_id=uid))
        elif path == "/api/agent/qr_login":
            force = (qs.get("force") or ["0"])[0] in ("1", "true")
            _j(self, qr_login(force=force))
        elif path == "/api/agent/qr_status":
            _j(self, qr_status())
        elif path == "/api/agent/memory":
            _j(self, {
                "preferences": memory.get_preferences(uid),
                "history_tail": memory.recent_history(20, uid),
                "feedback_tail": memory.all_feedback(uid)[-20:],
            })
        elif path == "/api/agent/pattern":
            pat = pat_mod.effective_bedtime(memory.get_preferences(uid), user_id=uid)
            _j(self, {"ok": True, "pattern": pat.to_dict()})
        elif path == "/api/agent/windows":
            ws = []
            now = windows.now_min()
            for w in windows.windows(uid):
                st, en = w["start"], w["end"]
                in_now = (now - st) % 1440 <= (en - st) % 1440
                ws.append({"kind": w["kind"], "center": w["center"],
                           "start": pat_mod.SleepPattern.fmt(st),
                           "end": pat_mod.SleepPattern.fmt(en),
                           "active_now": in_now})
            _j(self, {"ok": True, "windows": ws,
                      "server_time": datetime.now().strftime("%H:%M")})
        elif path == "/api/agent/usage":
            import os
            logp = os.path.join(config.data_dir_for(uid), "usage_log.jsonl")
            rows = []
            if os.path.exists(logp):
                with open(logp, "r", encoding="utf-8") as f:
                    rows = [json.loads(l) for l in f if l.strip()][-500:]
            by_tag, by_day = {}, {}
            total_in = total_out = 0
            for r in rows:
                t = r.get("tag") or "?"
                d = (r.get("ts") or "")[:10]
                i, o = r.get("in") or 0, r.get("out") or 0
                total_in += i; total_out += o
                bt = by_tag.setdefault(t, {"n": 0, "in": 0, "out": 0})
                bt["n"] += 1; bt["in"] += i; bt["out"] += o
                bd = by_day.setdefault(d, {"n": 0, "in": 0, "out": 0})
                bd["n"] += 1; bd["in"] += i; bd["out"] += o
            _j(self, {"ok": True, "calls": len(rows),
                      "total_in": total_in, "total_out": total_out,
                      "by_tag": by_tag, "by_day": by_day})
        elif path == "/api/agent/alarm_pack":
            # 今晚闹钟包（手机系统闹钟的弹药）：锚点变化自动重算
            import notifier as _n
            _j(self, {"ok": True, "pack": _n.get_or_build_pack(uid)})
        elif path == "/api/agent/plan":
            import planner
            prefs = memory.get_preferences(uid)
            tonight = planner.fresh_plan(prefs, uid)
            # ★ 正在规划中判定：plan_now 触发过（60秒内）且新计划还没生成出来
            #   （tonight_plan.generated_at 不晚于触发时刻）→ 手机端显示"正在规划中"
            planning = False
            plan_reason = None
            if tonight:
                plan_reason = tonight.get("reason")
            if _PLAN_RUNNING or (_PLAN_NOW_TS > 0 and time.time() - _PLAN_NOW_TS < 120):
                gen_at = (tonight or {}).get("generated_at") or ""
                try:
                    from datetime import datetime as _dt
                    gen_dt = _dt.fromisoformat(gen_at)
                    # 计划是触发之前生成的 → 还在等新计划
                    if gen_at and gen_dt.timestamp() <= _PLAN_NOW_TS:
                        planning = True
                    elif not gen_at:
                        planning = True
                except Exception:
                    planning = True
            _j(self, {"ok": True,
                      "ai_enabled": planner.ai_enabled(prefs),
                      "manual": planner.manual_bedtime(prefs) is not None,
                      "tonight": tonight,
                      "planning": planning,
                      "plan_reason": plan_reason,
                      "tonight_min_src": planner.tonight_bedtime(prefs, uid)[1]})
        elif path == "/api/agent/pending":
            _j(self, {"ok": True, "items": notifier.pop_all(uid)})
        elif path == "/api/agent/check_now":
            force = (qs.get("force") or ["0"])[0] in ("1", "true")
            out = notifier.run_pre_sleep_check(uid=uid, force=force)
            _j(self, {"ok": True, "generated": out})
        elif path == "/api/agent/test_notify":
            # 测试手机弹链路：强制生成一条提醒并立即返回（消费式）
            notifier.run_pre_sleep_check(uid=uid, force=True)
            _j(self, {"ok": True, "items": notifier.pop_all(uid)})
        elif path == "/":
            _j(self, {"service": "MiHealth Agent", "model": config.MODEL,
                      "endpoints": ["/api/agent/ping", "/api/agent/chat?q=",
                                    "/api/agent/pattern", "/api/agent/pending",
                                    "/api/agent/memory", "/api/agent/preferences",
                                    "/api/agent/feedback"]})
        else:
            _j(self, {"ok": False, "err": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        qs = parse_qs(u.query)
        body = _read_body(self)
        # ★ 2026-10-20 修复：App 把 user_id 放在 URL query 里，
        #   这里必须同时读 query 和 body，否则所有 POST 都串到根目录。
        uid = _get_user_id(qs, body)
        if path == "/api/agent/chat":
            q = str(body.get("q") or "").strip()
            if not q:
                _j(self, {"ok": False, "err": "缺少字段 q"}, 400)
            else:
                _j(self, brain.ask(q, user_id=uid))
        elif path == "/api/agent/preferences":
            saved = memory.save_preferences(body, uid)
            _j(self, {"ok": True, "preferences": saved})
        elif path == "/api/agent/feedback":
            memory.add_feedback(body, uid)
            _j(self, {"ok": True})
        elif path == "/api/agent/screen":
            # 手机上报屏幕熄灭时长（亮=醒信号；灭≠睡，仅参考）：{"off_min": 27}
            try:
                off = max(0, min(600, int(body.get("off_min") or 0)))
            except Exception:
                off = 0
            memory.update_preferences_shallow({
                "last_screen_off_min": off,
                "last_screen_report": datetime.now().isoformat(timespec="seconds"),
            }, uid)
            _j(self, {"ok": True})
        elif path == "/api/agent/hr":
            # 手机上报手环BLE广播的实时心率：{"bpm": 58.0}
            try:
                bpm = float(body.get("bpm") or 0)
            except Exception:
                bpm = 0
            if 25 <= bpm <= 220:
                prefs = memory.get_preferences(uid)
                pts = (prefs.get("realtime_hr") or [])[-120:]
                pts.append({"ts": datetime.now().isoformat(timespec="seconds"),
                            "bpm": bpm})
                memory.update_preferences_shallow({"realtime_hr": pts[-160:]}, uid)
            _j(self, {"ok": True})
        elif path == "/api/agent/alarm_ack":
            # 手机确认系统闹钟已排好：{version|items:[{event,at_ts}]}
            # → 服务器停推同版本/同事件 P 系列通知，避免"响铃+通知"双轰炸
            if body.get("version"):
                memory.update_preferences_shallow(
                    {"alarm_ack_version": int(body["version"])}, uid)
            evs = [str(e.get("event") or "") for e in (body.get("items") or [])
                   if isinstance(e, dict) and e.get("at_ts", 0) * 1000 > time.time() * 1000 - 60_000]
            memory.update_preferences_shallow({"alarm_ack_events": evs}, uid)
            _j(self, {"ok": True})
        elif path == "/api/agent/plan_now":
            # 立即规划今晚（手机打开AI开关时调用）。
            # ★ 2026-10-13 方案C：打开 AI 一律强制重规划（防抖60秒防连点）。
            #   不再走"fresh"分支——否则"关过再开"会闪一下旧计划（params_changed
            #   检测不到开关翻转，plan_age_min 又发现刚生成过 → 不重规划）。
            #   副作用：每次开 AI 多调一次 LLM（约30秒+几分钱），可接受。
            import planner as _pl
            global _PLAN_NOW_TS, _PLAN_RUNNING
            now = time.time()
            if now - _PLAN_NOW_TS < 60 or _PLAN_RUNNING:
                _j(self, {"ok": True, "status": "planning"})
                return
            _PLAN_NOW_TS = now
            _PLAN_RUNNING = True
            def _run():
                global _PLAN_RUNNING
                try:
                    p = _pl.generate(uid)
                    print(f"[PlanNow] {'已生成 ' + p['bedtime'] if p else '失败(无基线/LLM)'}")
                except Exception as e:
                    print(f"[PlanNow] 异常: {e!r}")
                finally:
                    _PLAN_RUNNING = False
            threading.Thread(target=_run, daemon=True).start()
            _j(self, {"ok": True, "status": "started"})
        elif path == "/api/agent/pregen_next":
            # 弹前2分钟：只生成即将弹点的1条文案，注入最新实时数据（弹小米同步+拉云端后调）
            import planner as _pl
            import notifier as _n
            prefs = memory.get_preferences(uid)
            bed, src = _pl.tonight_bedtime(prefs, uid)
            if bed is None:
                _j(self, {"ok": True, "generated": None})
            else:
                out = _n.pregen_next(bed, pat_mod.SleepPattern.fmt(bed), uid)
                _j(self, {"ok": True, "generated": out})
        elif path == "/api/agent/wake_plan":
            # AI 智能闹钟：整晚心率曲线 → 预测起床窗口 → 生成唤醒策略
            import wake_engine
            curve = body.get("curve") or []
            ws = str(body.get("wake_start") or "08:00")
            we = str(body.get("wake_end") or "08:30")
            print(f"[WakePlan][{uid or 'root'}] 🚀 收到唤醒策略请求 @ {datetime.now().strftime('%H:%M:%S')} "
                  f"window={ws}~{we} curve_points={len(curve)}")
            pack = wake_engine.generate_wake_plan(
                user_id=uid, curve=curve, wake_start=ws, wake_end=we)
            print(f"[WakePlan][{uid or 'root'}] ✅ 生成策略: {pack.get('strategy')} "
                  f"({pack.get('condition_label')}) confidence={((pack.get('params') or {}).get('confidence'))} "
                  f"@ {datetime.now().strftime('%H:%M:%S')}")
            _j(self, {"ok": True, "pack": pack})
        elif path == "/api/agent/wake_review":
            # 唤醒后复盘：比对预测 vs 实际，更新 memory 供下次进化
            import wake_engine
            out = wake_engine.review_wake(
                user_id=uid,
                curve_full=body.get("curve_full") or [],
                wake_pack=body.get("wake_pack"),
                actual_wake_time=str(body.get("actual_wake_time") or ""),
                outcome=str(body.get("outcome") or ""),
                concurrent_strategies=body.get("concurrent_strategies") or [])
            print(f"[WakeReview][{uid or 'root'}] 📊 收到唤醒复盘 @ {datetime.now().strftime('%H:%M:%S')} "
                  f"wake_time={body.get('actual_wake_time')} outcome={body.get('outcome')} "
                  f"concurrent={body.get('concurrent_strategies')} "
                  f"curve_points={len(body.get('curve_full') or [])}")
            _j(self, out)
        else:
            _j(self, {"ok": False, "err": "not found"}, 404)


def main():
    print(f"[Agent] 模型 {config.MODEL} @ {config.BASE_URL}")
    print(f"[Agent] 记忆目录 {config.DATA_DIR}")
    print(f"[Agent] 数据源   {config.EXPORT_DIR}")
    # ★ 启动恢复：不等调度器第一拍，立即恢复今晚计划/闹钟包/即将弹点文案
    _startup_recovery()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    print("[Agent] 睡前提醒调度线程已启动（每 5 分钟体检一次）")
    srv = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    print(f"[Agent] 服务已启动：http://127.0.0.1:{config.PORT}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[Agent] 已退出")


if __name__ == "__main__":
    main()
