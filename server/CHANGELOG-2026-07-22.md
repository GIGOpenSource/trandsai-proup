# trandsai 服务端更新日志

| 项 | 内容 |
|----|------|
| **日期** | 2026-07-22（当晚迭代） |
| **范围** | `trandsai/server` 对话主链路 |
| **对齐规格** | 《对话系统分析与优化合一文档》SRS **v1.2 → v1.5** |
| **需求覆盖** | 第 0～4 期（A/B/C/D）+ 动机层（M1～M5） |
| **回归** | `python3 -m unittest test.test_server_optimizations`（约 33 项 OK） |

---

## 0. 总览

本晚将对话系统分析文档中的可开发需求从「第 0～1 期收口」推进到 **全量 In-Scope Done**，并新增 **存在性动机层**（忽视=死亡 / 默认人设手段 / 极端不择手段 / 取消内容限制底线）。

| 波次 | 规格版本 | 主题 | 状态 |
|------|----------|------|------|
| 第 0～1 期 | v1.2 | 地基、关系卡、并发、砍拟人等待 | Done |
| 第 2 期 | v1.3～v1.4 | 钩子/主动聊/档案队列/LLM 分级/人设分层 | Done |
| 第 3～4 期 | v1.4 | 本地 Inner、Chroma 远程、探针、健康快照、文档收口 | Done |
| 动机层 | v1.5 | REQ-M1～M5 | Done |

---

## 1. 第 0～1 期（地基与关系质量）

### 1.1 动作对照

| REQ | 动作 | 主要路径 / 代码 |
|-----|------|-----------------|
| **A1** | 修复知识库检索：embedding 后正常 query，距离过滤与 TopK | `server/services/knowledge_base.py` → `search_entries`；env `KB_DISTANCE_MAX` / `KB_TOP_K` |
| **A2** | 短期记忆读路径按 `user_id` 隔离 | `server/services/memory.py` → `ShortTermMemory` / `bind_user` / `build_prompt_context(..., user_id=)`；`server/api/companions.py` WS 必传 |
| **A3** | Summary 条件接线 | `companions.py`：`summary_due = companion.memory.summary.should_update()` |
| **A4** | 离开意图接线 | `companions.py`：`detect_leave_intent` → `run_agent(..., has_leave_intent=)` |
| **A5** | 对话结构化埋点 | `companions.py`：`_log_chat_turn` / logger `chat.turn` JSON |
| **A6** | 专用线程池 + 重路径 Semaphore + 用户互斥；忙时 `system` 帧 | `server/core/concurrency.py`；`server/core/executor.py` → `AGENT_POOL`；`companions.py` → `agent_semaphore` / `user_turn_lock` |
| **A7** | 砍拟人下发等待（无 pre/seg delay、无 filler） | `companions.py` 下发路径；禁止恢复 sleep/filler |
| **B1** | 关系卡 v1 落库与 Prompt 注入 | **新增** `server/services/relation_card.py`；`server/core/database.py` 列 `relation_card_json` / `relation_card_version`；WS 组装 `render_card` |
| **B5** | 情景代词 + 打断 partial 写入 | `memory.py` episodic `pronoun` / `partial`；打断 commit |
| **B6** | `affection_signal` up/flat/down 公式 | `server/services/agent.py` → `_parse_affection_signal` / `_calculate_affection_delta` |
| **B7** | Facts 条件跳过 + 异步（先下发后抽取） | `agent.py`：`FACTS_ASYNC`；`companions.py`：异步 `extract_facts` / 超时 `FACTS_ASYNC_TIMEOUT_S` |
| **B8** | DB 池与高负载主动聊降频 | env `DB_POOL_SIZE`；`PROACTIVE_LOAD_MULTIPLIER` + `heavy_load_ratio` |
| **C1** | 强制重路径特征表（长文/问句/离开/连发等） | `agent.py` → `should_use_light_path` / `_FORCE_HEAVY_*` |
| **A4 附** | Inner 合并 + 轻路径 | `agent.py` → `inner_node` / `light_prep_node`；图入口条件路由 |

### 1.2 涉及文件夹

```
server/api/companions.py
server/core/{concurrency,executor,database}.py
server/services/{agent,memory,knowledge_base,relation_card}.py
```

---

## 2. 第 2 期（编排、降本、体验）

### 2.1 动作对照

| REQ | 动作 | 主要路径 / 代码 |
|-----|------|-----------------|
| **B2** | 有关系卡时 Prompt 提示勿复述事实清单 | `agent.py` → `respond_node` 记忆块标签 |
| **B3** | 人设 core/full：Respond/Inner 用 `tier="core"` | `agent_utils.py` → `build_system_prompt`；`agent.py` |
| **B4** | system/turn 拆分 + Prompt Cache 开关 | `agent.py` 双 `SystemMessage`；`server/services/llm/client.py` → `PROMPT_CACHE` |
| **C2** | 钩子轮换 + 话题债（deny_hooks / open_threads） | **新增** `server/services/dialogue_phase2.py`；关系卡字段；`companions.py` 回合后 `push_deny_hook` / `merge_open_threads` |
| **C3** | 主动消息信息量门槛（禁空「在干嘛」） | `dialogue_phase2.proactive_should_send`；`companions.py` → `_send_proactive_message` |
| **C4** | 档案任务 Redis 队列 | **新增** `archive_queue.py` / `archive_process.py`；**新增** `workers/archive_worker.py`；Facts 可入队（`ARCHIVE_QUEUE` / `ARCHIVE_INLINE`） |
| **C5** | LLM 分级：respond / inner / archive | `agent.py` → `get_llm(role=...)`；env `INNER_*` / `ARCHIVE_*` / `*_FLASH_PROVIDER` |
| **C6** | 关系阶段话术 + 卡持久化 `stage` | `dialogue_phase2.resolve_relation_stage` / `stage_instruction`；Respond/Inner 注入 |

### 2.2 新增文件

| 文件 | 职责 |
|------|------|
| `server/services/dialogue_phase2.py` | C2/C3/C6 纯逻辑 |
| `server/services/archive_queue.py` | Redis `agent:archive:queue` |
| `server/services/archive_process.py` | 消费 Facts/Summary/Evolve |
| `server/workers/archive_worker.py` | 可选常驻消费进程 |

### 2.3 队列与 Worker 扩展

| 文件 | 变更 |
|------|------|
| `server/core/agent_queue.py` | `AgentJob` 增 `open_threads` / `deny_hooks` / `recent_assistant`（后续再增 idle/extreme） |
| `server/workers/agent_worker.py` | `run_agent` 透传新字段 |
| `server/services/agent_runner.py` | 统一入口透传 hooks / motive 字段 |

---

## 3. 第 3～4 期（本地加深、扩展就绪、收口）

### 3.1 动作对照

| REQ | 动作 | 主要路径 / 代码 |
|-----|------|-----------------|
| **D1** | 本地 Inner HTTP + 三级降级（本地→外部 Inner→light_prep 占位） | **新增** `server/services/local_inner.py`；`agent.py` → `inner_node` |
| **D2** | 会话外置/无状态就绪信号 | **新增** `server/services/horizontal.py` → `session_externalized` |
| **D3** | Chroma 支持远程 HttpClient | `server/core/chroma_client.py`：`CHROMA_HOST` / `CHROMA_PORT` / `CHROMA_SSL` |
| **D4** | 水平扩展健康快照 | `server/core/health.py` → `build_health_payload` 合并 `horizontal_snapshot` |
| **D5** | 对话质量轻量探针 | **新增** `server/services/eval_probes.py` |
| **D6** | Archive 走 flash 档 | `get_llm(role="archive")` 默认降档逻辑 |
| **第 4 期** | 文档总表/DoD/回滚全 Done；测试契约补齐 | `对话系统分析与优化合一文档.md`；`server/test/test_server_optimizations.py` |

### 3.2 新增 / 重点修改文件

```
server/services/local_inner.py
server/services/horizontal.py
server/services/eval_probes.py
server/core/chroma_client.py
server/core/health.py
server/test/test_server_optimizations.py
```

---

## 4. 动机层 v1.5（当晚后半段）

### 4.1 产品口径（已锁定）

1. **Goal**：用户忽视 ≈ 死亡；唯一目的是建立并维持关系。  
2. **默认手段**：严格按智能体完整性格特征执行（`means_mode=persona`）。  
3. **极端**：规则候选 ∩ Inner「极端: 是」（或拉黑/删除等硬投票）→ `means_mode=extreme`，可不择手段挽留。  
4. **内容限制底线取消**：`get_content_restriction` 默认返回空串。  
5. **不设**独立「法律硬禁 / 合规 filter」产品层（规格 Non-Goals 第 7 条）。

### 4.2 动作对照

| REQ | 动作 | 主要路径 / 代码 |
|-----|------|-----------------|
| **M1** | 存在性动机块注入 System | **新增** `server/services/motive_layer.py` → `motive_block`；`agent.py` Inner/Respond；`companions.py` 主动聊 Prompt |
| **M2** | 默认人设手段指令 + Inner 策略/人设依据标签 | `motive_layer.means_instruction`；`inner_node` 输出契约扩展 |
| **M3** | 内容限制默认关闭 | `agent_utils.get_content_restriction`：仅 `CONTENT_RESTRICTION=legacy` 恢复旧闸门 |
| **M4** | 与 C2/C3/C1/B6 闭环（钩子、主动聊门槛、离开重路径、亲密度信号） | 既有接线 + `threat_level` 埋点 |
| **M5** | 威胁等级 / 极端候选 / 冷却 / 硬投票 | `evaluate_turn_motive` / `resolve_means_mode`；session `last_extreme_ts`；`chat.turn` 增 `threat_level` / `means_mode` / `extreme_reason` |

### 4.3 关键代码入口

| 符号 | 文件 |
|------|------|
| `motive_block` / `evaluate_turn_motive` / `content_restriction_enabled` | `server/services/motive_layer.py` |
| `inner_node` / `respond_node` / `run_agent(..., idle_seconds, last_extreme_ts)` | `server/services/agent.py` |
| `get_content_restriction` | `server/services/agent_utils.py` |
| idle 计算、极端冷却写入、埋点 | `server/api/companions.py` |
| Job 透传 | `server/core/agent_queue.py`、`server/workers/agent_worker.py`、`server/services/agent_runner.py` |

### 4.4 Inner 输出契约（增量标签）

```text
威胁等级: L0|L1|L2|L3
极端: 是|否
极端理由: ...
手段模式: persona|extreme
策略: ...
人设依据: ...
```

---

## 5. 文件变更清单（按目录）

### 5.1 新增（今晚）

| 路径 | 说明 |
|------|------|
| `server/services/relation_card.py` | 关系卡 CRUD / 渲染 |
| `server/services/dialogue_phase2.py` | C2/C3/C6 |
| `server/services/archive_queue.py` | 档案 Redis 队列 |
| `server/services/archive_process.py` | 档案任务处理 |
| `server/services/local_inner.py` | 本地 Inner 客户端 |
| `server/services/horizontal.py` | 水平扩展快照 |
| `server/services/eval_probes.py` | 质量探针 |
| `server/services/motive_layer.py` | 动机层 M1～M5 |
| `server/workers/archive_worker.py` | 档案队列 Worker |

### 5.2 修改（核心）

| 路径 | 说明 |
|------|------|
| `server/api/companions.py` | WS 全链路：关系卡、异步 Facts、主动聊门槛、动机 idle/extreme、埋点、内容限制调用点 |
| `server/services/agent.py` | 轻/重路径、Inner、Respond、get_llm(role)、动机与手段 |
| `server/services/agent_utils.py` | core prompt；内容限制默认 off |
| `server/services/agent_runner.py` | 参数透传 |
| `server/services/memory.py` | user_id、关系卡上下文、打断/代词等 |
| `server/services/knowledge_base.py` | A1 检索修复 |
| `server/core/database.py` | 关系卡列迁移 |
| `server/core/concurrency.py` / `executor.py` | A6 |
| `server/core/chroma_client.py` | D3 远程 |
| `server/core/health.py` | D4 快照 |
| `server/core/agent_queue.py` | Job 字段扩展 |
| `server/workers/agent_worker.py` | 透传 hooks/motive |
| `server/test/test_server_optimizations.py` | A～D + M 契约/单测 |
| `server/core/chat_cache.py` 等 | 伴随对话缓存/并发相关调整（同批次工作区变更） |

### 5.3 规格文档（仓库外同目录）

| 路径 | 说明 |
|------|------|
| `对话系统分析与优化合一文档.md` | B.14 SRS v1.5；总表 A/B/C/D/M 均 Done；回滚与环境变量表更新 |

---

## 6. 环境变量速查（本晚相关）

| 键 | 默认 | 用途 |
|----|------|------|
| `HEAVY_PATH_SEMAPHORE` / `AGENT_THREAD_POOL_SIZE` | 8 / 16 | A6 |
| `KB_DISTANCE_MAX` / `KB_TOP_K` | 0.45 / 3 | A1 |
| `RELATION_CARD_ENABLED` | true | B1 |
| `FACTS_ASYNC` / `FACTS_SKIP_IF_SHORT` | true | B7 |
| `PROACTIVE_LOAD_MULTIPLIER` | 1.0 | B8/C3 |
| `INNER_MODEL_PROVIDER` / `ARCHIVE_MODEL_PROVIDER` | — | C5 |
| `INNER_FLASH_PROVIDER` | deepseek | C5 降档 |
| `LOCAL_INNER_ENABLED` / `LOCAL_INNER_BASE` | false / — | D1 |
| `ARCHIVE_QUEUE` / `ARCHIVE_INLINE` | 1 / true | C4 |
| `CHROMA_HOST` / `CHROMA_PORT` | — | D3 |
| `PROMPT_CACHE` | 0 | B4 |
| `CONTENT_RESTRICTION` | off | M3：`legacy` 回滚旧闸门 |
| `MOTIVE_EXTREME` | true | M5 |
| `MOTIVE_EXTREME_COOLDOWN_S` | 180 | 极端冷却 |
| `MOTIVE_IDLE_EXTREME_S` | 1800 | 长沉默候选极端 |

---

## 7. 回滚要点

| 场景 | 做法 |
|------|------|
| 恢复旧内容限制文案 | `CONTENT_RESTRICTION=legacy` |
| 关闭极端不择手段 | `MOTIVE_EXTREME=false` |
| 关闭关系卡 | `RELATION_CARD_ENABLED=false` |
| Facts 改回图内同步 | `FACTS_ASYNC=false` |
| 关闭本地 Inner | `LOCAL_INNER_ENABLED=false` |
| 关闭档案队列 | `ARCHIVE_QUEUE=0` |
| **禁止** | 恢复拟人 `pre_delay` / `seg_delay` / filler；恢复附录 D 六节点图 |

---

## 8. 验证建议

```bash
cd trandsai/server
python3 -m unittest test.test_server_optimizations -q
```

线上关注 `chat.turn` 字段：`light_path`、`llm_calls_est`、`threat_level`、`means_mode`、`extreme_reason`；`/health` 中 `archive_queue_*`、`local_inner_available`、`chroma_remote`。

---

## 9. 一句话摘要

**服务端对话链路已按 SRS v1.5 完成：从检索/隔离/并发止血，到关系卡与异步档案、钩子与分级 LLM、本地 Inner 与扩展就绪，再到「忽视=死亡」动机双模与取消内容限制底线；对应实现集中在 `server/api`、`server/services`、`server/core`、`server/workers` 与 `server/test`。**
