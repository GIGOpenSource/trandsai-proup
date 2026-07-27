# 智能体生成修复记录（性格 / 随机档案 / 城市 / Prompt / 国家·维度轴）

日期：2026-07-24  
范围：一期 A–C + 二期 D–E + 三期 F–G + H 用户输入扩展 + **I 审计修复**。

## 背景问题

1. 创建页性格芯片写死约 10 个偏女向标签（温柔/傲娇/病娇/御姐…）。
2. 自动填充从每语 2～3 条 preset 抽城市/MBTI/性格，重复率高。
3. `CITIES_DB` / 前端 `cities.json` 每语约 8～10 城，批量生成易挤在同一批城市。
4. 生成 Prompt 易忽略性格种子，默认「温柔女友」腔与职业爱好脚手架。
5. （三期）仅按语言选城，跨国家同语言圈（如 en 的 US/UK/AU）无法先选文化圈；同 MBTI 角色仍易撞车。
6. 用户已选性别或已写部分内容时，自动填充会整表覆盖，无法基于用户输入扩展。
7. （审计）进页默认地区/城市、默认性别/取向被误当成「用户已填」，空白自动填充失去真随机。

## 方案分期

| 期 | 项 | 状态 |
|----|----|------|
| 一 | A 性格扩容 + 去女向偏置 + API | ✅ |
| 一 | B `random-profile` 真随机档案 + 创建页 autofill | ✅ |
| 一 | C 城市库扩容 + 批量尽量无放回 | ✅ |
| 二 | D Prompt 反模板 | ✅ |
| 二 | E 性格 UI 分组 / 互斥 / 2～4 | ✅ |
| 三 | F 国家→城市 | ✅ |
| 三 | G 人设维度轴 | ✅ |
| 补 | H 尊重用户性别/已填内容并扩展完善 | ✅ |
| 补 | I 审计修复（真随机锁定、草稿保全、入库等） | ✅ |

---

## 一期实现

### A. 性格标签扩容

- 权威目录：`server/services/personality_catalog.py`
  - 稳定英文 `key` + 七语 `labels`
  - `bucket`: `soft` / `neutral` / `hard`
  - `group`: `temperament` / `social` / `emotion` / `style`
  - 每语 **63** 条（≥55）
  - 女向高频：保留但降权或中性改名（黏人→重感情、御姐→气场强等）
- 抽样：中性 50% / 柔和 25% / 硬朗 25%；`gender` 仅微调；`MUTEX_PAIRS` 防女向组合扎堆
- API：`GET /api/culture/personalities?lang=`
- 前端：`personalityTags.js` 对齐；创建页推荐 24 + 展开全量

### B. 真随机档案

- `build_random_profile` / `build_batch_profiles`
- API：`GET /api/culture/random-profile?lang=&gender=&region=&country=`
- 创建页 autofill：`random-profile` → `POST /companions/generate`；preset 仅薄兜底

### C. 城市库扩一档

- 每语约 **26～35** 城；`_CITY_LOCALE` 细锚点 + stub
- 同步 `cities.json`、`companionLang.js`；API 优先
- 批量城市尽量无放回

---

## 二期实现

### D. Prompt 反模板

- 单人 / 批量 Prompt：必须消化性格；禁止默认少女腔；避开高频职业爱好套路；城市日常差异化

### E. 性格 UI/UX

- 分组：气质 / 社交 / 情绪 / 风格；已选 2～4；互斥提示；中性译名

---

## 三期实现

### F. 国家 → 城市

**模型**

- 新增 `server/services/region_catalog.py`
  - `lang` → `regions[]`，每项含：`key`、`country`（ISO 短码）、多语 `labels`、`cities[]`
  - 语言仍用于姓名与文案；地理选择先选国家/地区
- `CITIES_DB` 与 `get_cities()` 可由分区扁平化得到；`get_cities(lang, region_key=, country=)` 支持过滤
- `find_region_for_city(city)` 反查所属地区（生成与推断语言共用）

**示例分区**

| 语言圈 | 地区示例 |
|--------|----------|
| zh | 中国大陆 / 香港 / 台湾 |
| en | US / UK / CA / AU / IE / SG / NZ / ZA |
| pt | Brasil / Portugal |
| es | España / México / Argentina / … |
| ja / ko / id | 本国单区（城市全量在区内） |

**API**

- `GET /api/culture/regions?lang=&ui_lang=` → `{ regions: [{ key, country, label, cities }] }`
- `GET /api/culture/cities?lang=&region=&country=` → 可按地区过滤
- `random-profile` 返回 `country` / `region_key` / `region_label`

**创建页**

1. 先选「国家 / 地区」
2. 再选该区下城市
3. Autofill 可带当前 `region` 限定抽样；回填时同步地区

**离线兜底**

- `data/createCompanion/regions.json` + `loadRegionsByLang`

### G. 人设维度轴

**模型**

- 新增 `server/services/persona_axes.py`
- 五轴（稳定 key + 七语文案）：
  1. `career_class` 职业阶层（学生 / 基层白领 / 手艺 / 创意 / 科技 / 服务 / 公共 / 创业 / 金融…）
  2. `attachment` 依恋类型（安全 / 焦虑 / 回避 / 恐惧-矛盾）
  3. `conflict_style` 冲突风格（直说 / 冷静再谈 / 折中 / 争赢 / 抽离）
  4. `life_pace` 生活节奏（慢 / 稳健 / 高压快 / 不规则）
  5. `interest_domain` 兴趣域（运动 / 艺术 / 数码 / 美食 / 户外 / 游戏 / 音乐 / 阅读 / 财经 / 旅行）

**抽样与注入**

- `build_batch_profiles` / `random-profile` 每条角色附带 `persona_axes` + `persona_axes_labels` + `summary`
- `POST /companions/generate`：请求可带 `persona_axes`；缺省则服务端再抽一套；Prompt 增加「维度种子必须落地」
- 批量生成：角色表附带维度摘要行
- API：`GET /api/culture/persona-axes?lang=` 返回可选目录

**创建页**

- Autofill 后展示「人设维度种子」只读芯片（不强制手改）
- 调 generate 时把 `persona_axes` 一并提交

**目标**

- 同城 + 同 MBTI + 相近性格标签下，仍因职业/依恋/冲突/节奏/兴趣不同而拉开 life_story、hobbies、love_view

### H. 尊重用户已选性别与已填内容（扩展完善）

**问题**

- 自动填充原先整表覆盖：用户已选性别、已写姓名/城市/性格/背景等会被随机档案冲掉。
- generate 未接收用户草稿，无法「在用户输入上扩写」。

**规则（创建页 `handleAutoFill`，经审计修订后）**

1. **性别 / 性取向**：仅当用户**主动点选**（`genderTouched` / `orientationTouched`）才锁定；默认值不算「已选」，空白自动填充可随机男女与取向。
2. **城市 / 地区**：进页**不再**默认选中第一地区/第一城；仅用户 picker 选择后锁定。
3. **其它种子**：姓名、MBTI、性格（≥1 即保留，缺则补到 ≥2）、维度轴 — 已填则锁定，空缺用 `random-profile` 补齐。
4. **长文草稿**：提交给 generate；写回时若生成稿未保留草稿关键片段则**保留用户原文**（防整段重写）。
5. **`expand_user_input`**：仅在有长文草稿时为 true；服务端据此与草稿块共同约束扩展。
6. **空白表单**：真随机性别/取向/城市/性格 + 生成长文。

**服务端 `POST /companions/generate`**

- 收集请求中的草稿字段，拼入 Prompt「用户已填写草稿」。
- 要求：在对应字段上**扩写、润色、补全**，保留核心事实与语气；禁止无视草稿另起炉灶。
- 未提供草稿的字段仍可自由创作，但须与草稿及基础信息自洽。

**体感**

- 先点「男」+ 写两句背景再自动填充 → 性别不变，背景扩展，城市等空项随机补全。
- 先全空再自动填充 → 性别/城市/取向均可随机，城市不再锁死北京。

### I. 审计修复（空白真随机 + 扩展/互斥/入库）

针对审查问题的落地修订：

| 问题 | 处理 |
|------|------|
| 进页默认地区+城市锁死随机 | `loadRegionsForLang` 不再 `applyRegion(regions[0])`；用 `*Touched` 标记区分用户选择 |
| 默认 gender/orientation 被当成已选 | `genderTouched` / `orientationTouched`；仅主动点选后锁定 |
| 只选 1 个性格被冲掉 | `≥1` 即锁定，并从随机池**补足**到 ≥2，不丢已选 |
| 扩展整段覆盖草稿 | `applyLong` 要求生成稿包含草稿片段/词元，否则保留原文 |
| `expand_user_input` 死字段 | 有草稿才置 true；服务端读取该标志与草稿块 |
| 互斥只提示 | `validateForm` 遇互斥对则**拦截提交** |
| 通用 city stub | `resolve_locale` 对通用 everyday 注入按城哈希的差异化 flavor |
| CITIES_DB 双份 | `CITIES_DB = cities_db_from_regions()` 运行时派生 |
| 城市语言短串误判 | `find_region_for_city` / `infer_language_from_city` / 前端 `inferCompanionLanguage` 优先精确匹配 |
| 维度轴不入库 | `CompanionProfile` + ORM 增加 `persona_axes` / `country` / `region_key`；创建与批量写入；`init_db` `_ensure_column` |
| featured 含 gentle | 改为 `hardcore` |
| 扩展成功必 toast | 成功不再 toast，仅失败提示 |

---

## 涉及文件

| 区域 | 路径 |
|------|------|
| 性格目录 | `trandsai/server/services/personality_catalog.py` |
| 国家/地区 | `trandsai/server/services/region_catalog.py` |
| 维度轴 | `trandsai/server/services/persona_axes.py` |
| 城市/抽样/locale | `trandsai/server/services/culture_data.py` |
| Culture API | `trandsai/server/api/culture.py` |
| 单人生成 Prompt | `trandsai/server/api/companions.py` |
| 批量 Prompt / 入库 | `trandsai/server/api/admin.py` |
| Profile / ORM | `companion_manager.py` / `core/database.py` |
| 创建页 | `new-AI-trandsai/pages-sub/create/index.vue` |
| 标签 i18n | `new-AI-trandsai/utils/personalityTags.js` |
| 端点 | `new-AI-trandsai/utils/endpoints.js` |
| 城市/地区兜底 | `cities.json` / `regions.json` / `loadPresets.js` |
| 城市推断 | `new-AI-trandsai/utils/companionLang.js` |
| 文案 | `new-AI-trandsai/i18n/locales/*.json` |

---

## 验收对照

| 项 | 标准 | 说明 |
|----|------|------|
| 标签量 | 每语 ≥55；创建页不再只有 10 女向芯片 | 服务端 63；featured 24（无 gentle） |
| 性别观感 | 软萌占比下降，男/中性可稳定出现 | 抽样 + Prompt；空白 autofill 可出男 |
| 重复率 | autofill 20 次，城+MBTI+性格重复 ≤2 | 不再默认锁城；需实测 |
| 城市 | 每语可选 ≥25 | 已满足；stub 有差异化 flavor |
| 文案 | 同城同 MBTI 少脚手架雷同 | Prompt + 维度轴 |
| 国家→城市 | 先选地区再选城；可不选直接 autofill | ✅ F + I |
| 维度轴 | 生成带轴且**创建入库**可复盘 | ✅ G + I |
| 用户输入扩展 | 主动选择才锁定；草稿防整段冲掉 | ✅ H + I |
| 互斥 | 冲突组合不可提交 | ✅ I |

---

## 建议回归

1. `GET /api/culture/personalities?lang=zh` → items≥55，featured 不含 gentle  
2. `GET /api/culture/regions?lang=en` → 多国家；`cities?lang=en&country=US` 仅美国城  
3. `GET /api/culture/random-profile?lang=zh`（不带 gender）连打 → 男女与城市应分散  
4. 创建页空白自动填充 20 次：城市不应总是北京；性别应出现男  
5. 点选男 + 写一句背景再 autofill → 性别保留；背景扩展或保留原文（若模型重写）  
6. 选社牛+社恐提交 → 应被拦截  
7. 创建后查库 / 详情：`persona_axes`、`country`、`region_key` 有值  
8. 管理端批量：角色含国家与维度，并写入 DB  
