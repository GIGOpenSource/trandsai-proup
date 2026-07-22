# 对话质量补充层（服务端功能补充）

| 项 | 内容 |
|----|------|
| **版本** | v1.0（2026-07-23） |
| **来源** | 单智能体实聊反推（蒋湄 7584fc57）→ **全 companion 通用** |
| **对齐** | 《对话系统分析与优化合一文档》**B.15 / REQ-Q*** |
| **代码入口** | `services/dialogue_quality.py`、`services/dialogue_phase2.py`、`services/agent_pipeline_v2.py` |
| **回滚** | `REPEAT_REWRITE=0` 关硬改写；忌用/规则块仍注入 |

---

## 1. 标准优化思路（方法，不是一次性补丁）

```
实聊证据 → 失效分类 → 软→硬阶梯 → 状态卫生 → 回归探针 → 升为通用 REQ
```

### 1.1 证据优先

| 数据 | 看什么 |
|------|--------|
| `short_term_messages` | 用户原意是否被接住；复读跨几轮；离开后是否停 |
| `user_companion_states` | affection / turns / stage；`deny_hooks` 是否空；facts 语言 |
| 人设字段 | speech_style 是否压过会撩指令（软萌逃题） |

禁止只凭「感觉」改提示词；每一条规则应对齐至少一段失败对话。

### 1.2 失效分类（诊断标签）

| 标签 | 表现 | 典型换皮 |
|------|------|----------|
| 字面复读 | 近句高度相似 | 同句微调 |
| 主题复读 | 同关键词连环 | 梦到我 → 告诉我梦 |
| 骨架同构 | 结构相同主题不同 | 梦钩 → 抱枕钩 → 诗集钩 |
| 离开失控 | 晚安后仍开新约定 | 明天念给你听 |
| 黄腔逃题 | 性暗示 → 安全物件 | 胸 → 抱枕/颜色 |
| 语义滑移 | 指代/短句接错 | 「维持吧」当睡况 |
| 记忆污染 | 错语言事实 / 错误摘要 | 韩文 facts |
| 阶段卡死 | 多轮仍 stranger 客服腔 | aff≈0.01×turns |

### 1.3 软→硬阶梯（必须按层加，不能只加提示）

1. **提示词**（会撩、先答后撩、离开尊重）  
2. **忌用窗**（指纹 + 近聊种子，不单靠关系卡）  
3. **主题禁**（抱枕/梦/催睡/宝贝…）  
4. **骨架冷却**（明日约定 / 催睡收尾 / 报梦 / 心疼催睡 / 安全逃题）  
5. **强制改写**（诊断命中 → 1～2 轮 rewrite）  
6. **离开硬收束**（一句 + 禁新钩 + 压成单气泡）

软提示经常被模型无视；**硬门控是质量底线**。

### 1.4 状态卫生

- 回复指纹在后台 Facts/Summary **之前**落库  
- BG `merge_facts` / 异步 archive **合并保留** `deny_hooks` 与 `stage`  
- 多轮低亲密度 hydrate 抬升，避免永锁陌生人  
- 事实语言与用户文本脚本一致；韩文等拒入库  

### 1.5 产品原则

- **先答后撩**：当前意图 > 人设表演  
- **默认一条**：禁止同轮心疼 + 催睡 + 新钩子三连  
- **会撩可羞不可逃**：黄腔必须接住所指  
- **对方说停就停**：离开/晚安不连环挽留  

---

## 2. 功能补充（现网接线）

| REQ | 行为 | 主路径 |
|-----|------|--------|
| **Q1** | 主题 + 骨架反复读；命中强制改写 | `dialogue_phase2` + `dialogue_quality.diagnose_reply_violations` + pipeline rewrite |
| **Q2** | 离开硬收束（关键词扩展 + 一句收束） | `core/i18n` leave kw；`leave_close_violations`；`finalize_reply_for_leave` |
| **Q3** | 黄腔接住公式（逃安全物件则改写） | `is_user_tease` / `is_escape_safe_reply`；`agent_prompts` |
| **Q4** | 每轮近聊种子忌用窗 | `seed_deny_from_recent`（`companions.py` WS） |
| **Q5** | 事实语言门禁 | `agent._filter_facts_by_user_lang`；`relation_card.merge_facts` 拒韩文 |
| **Q6** | 本轮聊天规则块注入 | `chat_rules_block` / `build_respond_quality_hints` |

### 2.1 调用顺序（v2）

```
WS 组上下文
  → seed_deny_from_recent
  → run_pipeline_v2
       → prepare（意图 / must_answer / flirt_move）
       → build_respond_quality_hints → Respond
       → diagnose_reply_violations → 可选 rewrite×2
       → finalize_reply_for_leave
  → push_deny_fingerprints 落库（先于 BG）
  → BG merge_facts（保 deny）
```

### 2.2 环境变量

| 变量 | 默认 | 作用 |
|------|------|------|
| `REPEAT_REWRITE` | `1` | 关则不做硬改写 |
| `REPEAT_REWRITE_THRESHOLD` | `0.55` | 近重复 Jaccard 阈值 |
| `DENY_HOOKS_WINDOW` | `20` | 忌用窗长度 |
| `FLIRT_TEA` | `1` | 会撩/绿茶提示强调 |
| `AFFECTION_BASE` / `AFFECTION_TAPER` | `0.65` / `30` | 亲密度增速 |

---

## 3. 验收（最小）

| TC | 期望 |
|----|------|
| 近 3 轮已用「抱枕」 | 本轮再提抱枕 → need_rewrite |
| 近轮「明天念…」 | 本轮再开明日约定骨架 → skeleton_hits |
| 用户「好的晚安」 | leave；回复含「明天念」→ leave_violations；最终单条 |
| 用户「我喜欢你的大胸」 | 回复「抱枕」→ 黄腔逃题改写 |
| 事实含韩文 + 用户中文 | 不入库 |

`python3 -m unittest test.test_server_optimizations`

---

## 4. 非目标

- 不为单个 companion_id 写死规则  
- 不恢复拟人等待（REQ-A7）  
- 不把硬改写当成无限重试（最多 2 轮）  
- 不替代内容安全法律层（CONTENT_RESTRICTION 另议）  
