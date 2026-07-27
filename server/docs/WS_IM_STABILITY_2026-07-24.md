# WebSocket IM 稳定性更新说明

| 项 | 内容 |
|----|------|
| **日期** | 2026-07-24 |
| **问题** | 进入对话页后 IM 链路反复断开 / 重连；后续发现 **3600s 代理超时 + 无 pong 清扫** 易堆积僵死 WS，占满连接槽 |
| **根因** | 空闲无应用层心跳 + Nginx `/ws/` 超时不当；Proactive 长 sleep；前端仅被动重连；僵死连接未主动回收 |
| **范围** | 后端 WS、Nginx、前端 `chat` store |
| **目标** | 空闲会话可长期保持；断线后当前聊天页更快恢复；**僵死连接及时释放，避免新用户无法建连** |

---

## 1. 变更文件

| 文件 | 变更类型 |
|------|----------|
| `trandsai/server/api/companions.py` | 空闲 ping、收客户端 ping/pong、Proactive 切段保活、**无 pong 僵死清扫** |
| `trandsai/nginx.conf` | `/ws/` 读写超时：**3600s → 300s**（配合应用层心跳） |
| `new-AI-trandsai/stores/chat.js` | 应答服务端 ping、当前页更快退避重连 |

---

## 2. 根因简述

线上 Nginx 对 `/ws/` 曾配置过短的 `proxy_read_timeout 180s`：上游若连续约 180 秒无下行，代理掐断。后又曾放到 **3600s**，应用层 ping 虽能保活，但**半开/僵死连接**可挂很久，占满 `worker_connections` 与 Uvicorn 并发槽，导致新用户无法建立 WS。

原链路在以下阶段几乎不发帧：

1. 用户坐在聊天页等待输入（主循环阻塞在 `message_queue.get()`）
2. AI 回复后的 Proactive 等待：静默可达数分钟
3. 前端无定时 ping，只能等对端/代理先断再指数退避重连

Agent 推理阶段已有 `_keepalive_during_agent`（约每 2s `typing`）；不稳定主要在**空闲等待**；连接泄漏主要在**客户端已死但服务端未感知**。

本地 Vite 开发代理 `/ws` timeout 为 `0`，本地不易复现，线上更明显。

---

## 3. 后端改动（`companions.py`）

### 3.1 常量与工具

| 符号 | 默认 | 环境变量 | 说明 |
|------|------|----------|------|
| `_WS_IDLE_HEARTBEAT_MIN_S` | 25 | `WS_IDLE_HEARTBEAT_MIN_S` | 空闲心跳下限（秒） |
| `_WS_IDLE_HEARTBEAT_MAX_S` | 45 | `WS_IDLE_HEARTBEAT_MAX_S` | 空闲心跳上限（秒） |
| `_WS_PROACTIVE_SLEEP_CHUNK_S` | 30 | `WS_PROACTIVE_SLEEP_CHUNK_S` | Proactive 切段长度（秒） |
| `_WS_PONG_TIMEOUT_S` | 90 | `WS_PONG_TIMEOUT_S` | 发出 ping 后等待 pong 上限；超时判僵死 |
| `_WS_ZOMBIE_SWEEP_S` | 30 | `WS_ZOMBIE_SWEEP_S` | 后台清扫扫描间隔 |

| 函数 | 作用 |
|------|------|
| `_ws_heartbeat_interval()` | 在 [min, max] 内随机取等待超时 |
| `_ws_send_json(ws, payload)` | 安全发送 JSON |
| `_ws_send_ping_or_close(ws)` | 空闲下发 ping；上一轮无 pong 超时则 force-close |
| `_ws_mark_alive` / `_ws_note_ping_sent` / `_ws_is_zombie` | 存活标记与僵死判定 |
| `_ws_register` / `_ws_unregister` / `_ws_janitor_loop` | 连接注册表 + 定时清扫 |
| `_sleep_with_ws_keepalive` | 长 sleep 切段，并检测僵死 |

### 3.2 正常心跳（必须保持）

```
服务端 ──ping──▶ 客户端   （约每 25–45s，空闲主循环 Timeout）
服务端 ◀──pong── 客户端   （刷新存活；刷新 Nginx 读超时计时）
```

```python
payload = await asyncio.wait_for(
    message_queue.get(), timeout=_ws_heartbeat_interval()
)
# TimeoutError → _ws_send_ping_or_close()：发 ping 或关闭僵死连接
```

### 3.3 接收环

- `ping` → 回 `pong`，`_ws_mark_alive`，不入业务队列  
- `pong` → `_ws_mark_alive`，解除 `awaiting_pong`  
- 业务 `text` 入队前同样 `_ws_mark_alive`

### 3.4 僵死连接清扫

1. **主循环**：准备发下一轮 ping 前，若上一轮超过 `WS_PONG_TIMEOUT_S`（默认 90s）仍无 pong → `close(1001)` 并结束协程。  
2. **后台 janitor**：`_ws_live_registry` 登记；约每 30s 扫描并 force-close。  
3. **finally**：unregister + cancel receive/proactive + 必要时再 `close()`。

### 3.5 Proactive

切段 sleep，片间**不再额外 ping**；会检测僵死并主动关闭。

### 3.6 帧约定

| `type` | 方向 | 语义 |
|--------|------|------|
| `ping` | 双向 | 应用层心跳 |
| `pong` | 应答 | 刷新存活；不展示 |
| `typing` / `message` / … | 业务 | 不变 |

---

## 4. Nginx 改动（`nginx.conf`）

`location /ws/`：

| 指令 | 历史值 | **当前值** |
|------|--------|------------|
| `proxy_connect_timeout` | 60s | 60s |
| `proxy_send_timeout` | 曾 3600s | **300s** |
| `proxy_read_timeout` | 曾 3600s / 更早 180s | **300s** |

- **不要用 3600s**：僵死连接长期占槽。  
- **300s（5 分钟）** 作代理兜底；正常 25–45s ping+pong 远小于此。  
- 修改后需 **reload Nginx**。

---

## 5. 前端改动（`stores/chat.js`）

- 客户端**不**定时主动 `ping`；收到服务端 `ping` → 回 `pong`。  
- 当前聊天页更快、更持久重连；`retiringSockets` 隔离旧回调。  

详见二次修复 §9。

---

## 6. 部署与验证

1. 发布后端并重启 Uvicorn  
2. **reload Nginx**（确认 `/ws/` 为 **300s**，不是 3600s）  
3. 前端须能应答 `pong`（旧前端不回 pong 约 90s 会被清掉——预期）

| 步骤 | 预期 |
|------|------|
| 空闲 5–10 分钟 | 约 25–45s 服务端 `ping`，客户端 `pong` |
| 模拟僵死（停回 pong） | ≤约 90s 服务端 force-close；连接数下降 |
| 高并发后杀客户端 | worker 连接回落，新用户可连 |

```bash
WS_IDLE_HEARTBEAT_MIN_S=25
WS_IDLE_HEARTBEAT_MAX_S=45
WS_PROACTIVE_SLEEP_CHUNK_S=30
WS_PONG_TIMEOUT_S=90
WS_ZOMBIE_SWEEP_S=30
```

---

## 7. 非本次范围 / 已知边界

- Agent 保活仍走 `_keepalive_during_agent`。  
- 多 worker 粘滞不在范围。  
- Session 按 `user_id:companion_id` 隔离。  
- CDN 空闲超时需边缘侧确认。

---

## 8. 回滚要点

还原 `companions.py` 心跳/清扫、`nginx.conf` 超时（**勿长期 3600s**）、`chat.js` 重连策略。

---

## 9. 2026-07-24 二次修复

- 旧 socket 回调隔离（`retiringSockets`）  
- 连接级 send lock，统一 `_ws_send_json`  
- 取消客户端定时 ping；Proactive 去片间 ping  
- 当前聊天页超过 12 次仍持续重试  

---

## 10. 2026-07-24 三次修复：僵死 WS 回收 + Nginx 300s

### 10.1 问题

`proxy_read_timeout 3600s` 下，客户端已死或不回 pong 的连接可长期占用 Nginx `worker_connections` 与 Uvicorn 并发上限 → **新用户无法建立 WS**。

### 10.2 策略（与需求一一对应）

| # | 需求 | 落地 |
|---|------|------|
| 1 | 正常：25–45s 服务端 ping，客户端 pong，刷新 Nginx 计时 | 保持；pong / 业务入站均 `_ws_mark_alive` |
| 2 | Nginx `/ws/` 超时改为 **300s**，不用 3600s | `proxy_send_timeout` / `proxy_read_timeout` = **300s** |
| 3 | 定时清理无 pong 的失效 WS | 主循环检测 + janitor 约 30s 扫描；默认 90s 无 pong → `close(1001)` |

### 10.3 时序关系

```
心跳周期 ≈ 25–45s  ≪  pong 超时 90s  ≪  Nginx 读超时 300s
```

正常会话到不了 Nginx 超时；僵死先被应用层清掉。

### 10.4 变更文件（三次）

| 文件 | 说明 |
|------|------|
| `trandsai/server/api/companions.py` | 存活标记、ping 或 close、注册表、janitor |
| `trandsai/nginx.conf` | `/ws/` 3600s → **300s** |
| `trandsai/server/docs/WS_IM_STABILITY_2026-07-24.md` | 本节 |

### 10.5 运维注意

- 必须 **reload Nginx**。  
- 确认客户端回 `pong`。  
- 建议观察：活跃 WS 数、janitor 关闭次数、Nginx active connections。
