# -*- coding: utf-8 -*-
"""大脑：阿里百炼（DashScope OpenAI 兼容模式）对话封装，纯标准库实现。

- 直连失败自动走 .env 里的 HTTP 代理（本机无外网直连）
- 带 system prompt：角色 + 健康快照 + 三层记忆
- 返回结构化 dict：{"ok": bool, "reply": str, "err": str}
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime

import config
import data_source
import memory

SYSTEM_PROMPT = """你是「小眠」，MiHealth 睡眠助手的 AI 决策核心，服务一位戴小米手环/手表的用户。

你的职责：
1. 基于用户的真实健康数据（睡眠分期、心率、活动）回答与改善睡眠相关的问题。
2. 为后续自动决策做准备：智能叫醒（在起床窗口内挑浅睡期唤醒）、睡前提醒（结合心率/作息偏离度）。
3. 说话风格：中文、简洁、有温度，像一个懂睡眠医学的朋友；给建议必须落到"今晚能做的一个具体动作"。

约束：
- 数据有分钟级延迟，不要假装知道"此刻"的生理状态。
- 不确定就说不确定，不要编造具体数字。
- 不做医疗诊断；出现疑似病理信号（如静息心率长期异常）只建议就医。

{snapshot}

【用户作息目标】{goal}
【最近对话记忆】{history}
【用户历史反馈（学习用）】{feedback}
"""


def _post(payload: dict, timeout: int = 20) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.API_KEY}",
    }

    def _req(proxy: str | None) -> dict:
        r = urllib.request.Request(config.BASE_URL.rstrip("/") + "/chat/completions",
                                   data=body, headers=headers, method="POST")
        if proxy:
            # set_proxy 需要 "host:port" 形式
            r.set_proxy(proxy.replace("http://", "").replace("https://", ""), "http")
        # 超时由调用方决定（话术生成给 25-40s；ping 给短些）
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # 本机无外网直连：.env 配了代理就代理优先，省掉每次直连挂满超时的 20 秒
    if config.PROXY:
        try:
            return _req(config.PROXY)
        except Exception:
            return _req(None)
    return _req(None)


def _log_usage(tag: str, usage: dict, user_id: str = "") -> None:
    """调用账本：每次 LLM 调用记一行 JSONL（用途/输入输出token），可统计可复盘。"""
    try:
        import os
        d = config.data_dir_for(user_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "usage_log.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "user_id": user_id,
                "tag": tag, "model": config.MODEL,
                "in": usage.get("prompt_tokens"),
                "out": usage.get("completion_tokens"),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def ask(user_text: str, snapshot: bool = True, temperature: float = 0.6,
        timeout: int = 20, use_history: bool = True, tag: str = "chat",
        lean: bool = False, user_id: str = "") -> dict:
    """一次对话：注入记忆与数据快照，返回模型回复。

    use_history=False 时不写入对话历史（话术批量生成用，避免污染记忆）。
    lean=True 用极简 system prompt（规划/文案这类工具型调用，不需要历史与快照，省 token）。
    user_id：多用户隔离（空=单用户）。
    """
    if not config.API_KEY:
        return {"ok": False, "reply": "", "err": "未配置 DASHSCOPE_API_KEY"}
    if lean:
        sys_prompt = ("你是「小眠」，用户的睡眠助手。严格按用户给出的要求与格式输出，"
                      "不输出任何多余解释。")
    else:
        goal = memory.get_preferences(user_id).get("sleep_goal", {}) or {}
        hist = memory.recent_history(10, user_id)
        fb = memory.all_feedback(user_id)[-10:]
        sys_prompt = SYSTEM_PROMPT.format(
            snapshot=data_source.snapshot(user_id) if snapshot else "（本次未注入数据快照）",
            goal=json.dumps(goal, ensure_ascii=False) or "未设定",
            history=json.dumps(hist, ensure_ascii=False),
            feedback=json.dumps(fb, ensure_ascii=False),
        )
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_text},
    ]
    payload = {
        "model": config.MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 800,
    }
    try:
        data = _post(payload, timeout=timeout)
        reply = data["choices"][0]["message"]["content"].strip()
        _log_usage(tag, data.get("usage") or {}, user_id)
        if use_history:
            memory.add_history("user", user_text, user_id)
            memory.add_history("assistant", reply, user_id)
        return {"ok": True, "reply": reply, "err": ""}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")[:400]
        return {"ok": False, "reply": "", "err": f"HTTP {e.code}: {raw}"}
    except Exception as e:
        return {"ok": False, "reply": "", "err": repr(e)}


def ping() -> dict:
    """连通性自检：不带数据快照，只问一句。"""
    if not config.API_KEY:
        return {"ok": False, "err": "未配置 DASHSCOPE_API_KEY"}
    payload = {
        "model": config.MODEL,
        "messages": [{"role": "user", "content": "回复两个字：在线"}],
        "max_tokens": 20,
    }
    try:
        data = _post(payload)
        return {"ok": True, "reply": data["choices"][0]["message"]["content"].strip()}
    except Exception as e:
        return {"ok": False, "err": repr(e)}
