# MiHealth 睡眠 Agent（v0：能跑的对话大脑）

服务器端大脑，调用阿里百炼 qwen3.7-flash。手机 App 只负责"弹消息/定闹钟"的手，
后续通过 HTTP 接口与本服务对接。

## 启动

**必须用 Python 3.14 启动**（`C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe`）——
Windows 防火墙是按"程序"放行的，当初允许的是这个 python.exe；换别的解释器起服务，手机会被防火墙静默拦截（表现为 App 报"Agent 未启动"）。

```
# 推荐：PyCharm 里 Run start_agent.py（与仪表盘 start_server.py 同体验）
# 或命令行：
C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe D:\MIwatchAI\Agent\start_agent.py
# 监听 http://127.0.0.1:8001 （8000 是仪表盘）
```

手机 App 只填一个地址（仪表盘 `http://IP:8000`），Agent 地址自动=同 IP 的 8001；
换 WiFi 后电脑 IP 变了，手机改仪表盘地址即可，Agent 跟着变。

配置都在 `.env`（API key / 模型 / 代理 / 端口），代码不含密钥。
本机无外网直连，请求先试直连、失败自动走 Clash HTTP 代理 127.0.0.1:7897。

## 文件结构

| 文件 | 职责 |
|---|---|
| `config.py` | .env 读取（含不可见字符清洗） |
| `data_source.py` | 读 `D:\MIwatchAI\JSON` 最新导出，产出 LLM 友好的文字摘要（64MB 按 mtime 缓存） |
| `memory.py` | 三层记忆 JSON：preferences 偏好 / history 对话 / feedback 反馈 |
| `brain.py` | 百炼 OpenAI 兼容接口封装（纯标准库 urllib），system prompt 注入快照+记忆 |
| `agent_server.py` | HTTP 服务（与仪表盘 server.py 同风格，零第三方依赖） |
| `memory_data/` | 记忆落盘目录（自动生成） |

## 接口

```
GET  /api/agent/ping                      连通性自检
GET  /api/agent/chat?q=...                对话（自动带健康数据+记忆）
POST /api/agent/chat        {"q":"..."}   同上
POST /api/agent/preferences {"sleep_goal":{"bedtime":"23:30","wake":"06:30-07:30"}}
                                          手机推送作息目标（偏好记忆）
POST /api/agent/feedback    {"kind":"wake","rating":4,"note":"..."}
                                          叫醒/建议反馈（反馈记忆）
GET  /api/agent/memory                    查看三层记忆现状
```

## 路线图

- [x] v0 对话大脑 + 三层记忆 + 数据快照（本次）
- [ ] 决策 A：睡前生成"明晨唤醒策略"下发手机，手机按浅睡期落闹钟
- [ ] 决策 B：APScheduler 定时（22:30）体检当日数据 → 生成提醒 → 手机轮询 /pending 弹通知
- [ ] 反馈闭环：叫醒评分回流 feedback 记忆，影响后续策略
- [ ] 数据侧：睡眠记录去重（云端有同晚多条）、醒后 1-5 星自评 UI
