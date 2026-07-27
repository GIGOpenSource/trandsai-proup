# 问题优化记录（2026-07-24）

范围：`trandsai/server` + `new-AI-trandsai`  
依据：前后端问题排查报告中的 P0 / P1 / P2 / P3 项（不含报告中的 #4 朋友圈空列表，该项未列入本次修复清单）。

---

## 体感目标


| 优先级   | 目标                                        |
| ----- | ----------------------------------------- |
| P0    | 上传/重绘不能匿名滥用；登出真正失效；Token 不可猜、旧 Token 作废   |
| P1 安全 | 不能靠昵称串看别人伴侣；「我的」统计是自己的；多用户记忆/会话不抢同一个脑     |
| P1 创建 | 一键填充芯片亮得对、年龄/性别锁得对、人设轴能存能回传、国外城市不再被当成中文语境 |
| P2/P3 | 地区必填、取向中文、切语言不乱改城、扩展有反馈、性别契约统一等           |


---

## 逐项变更

### P0


| #   | 问题                      | 改动                                                                                  | 预期体感                    |
| --- | ----------------------- | ----------------------------------------------------------------------------------- | ----------------------- |
| 1   | 图片上传无鉴权                 | `api/posts.py`：`POST /api/upload/image` 需 `x-token` 用户或 Admin Bearer                | 未登录无法上传；滥用面收窄           |
| 2   | Token 可预测 + 旧 Token 未作废 | `api/auth.py`：`secrets.token_urlsafe(32)`；发新 Token 前 `invalidate` 旧 Token（内存+Redis） | 重登后旧登录态失效；难以按时间窗猜 Token |
| 3   | 前端登出不调服务端               | `pages/profile/index.vue`：登出先 `POST /api/auth/logout` 再清本地                          | 点退出后服务端会话作废             |
| 5   | 朋友圈重绘图无鉴权               | `api/moments.py`：需 Admin Bearer 或用户 `x-token`                                       | 匿名无法刷重绘费用               |




### P1 · 安全与隔离


| #   | 问题                      | 改动                                                                                                         | 预期体感                |
| --- | ----------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------- |
| 6   | ACL 过严（误当成只能看自己的）     | **已纠正**：登录可读/可聊全部智能体；仅删除/重生头像校验创建者（user_id/username，不用 nickname）。`list_all` 返回全量并附带当前用户态 | 发现/消息能看到别人创建的 AI；不能删别人的；昵称撞车不会误获管理权 |
| 7   | stats 全局统计              | `_compute_user_stats`：按当前用户归属伴侣 + 用户态 turns；未登录 401                                                        | 「我的」数字贴近本人          |
| 8   | 长期记忆未按用户隔离              | `facts.user_id`；新表 `user_relation_summaries`；Chroma collection `companion_{id}_u{uid}`；`bind_user` 同步短/长记忆 | 多用户同伴不串长期记忆（新会话起生效） |
| 9   | WS session 仅按 companion | `session_store` / `state`：键改为 `session:{user_id}:{companion_id}`；WS 读写均带 `user_id`                         | 双端/两人不再互相覆盖时区与会话元数据 |




### P1 · 人设创建


| #   | 问题                | 改动                                                             | 预期体感                  |
| --- | ----------------- | -------------------------------------------------------------- | --------------------- |
| 10  | 性格别名反查错误          | `personalityTags.js`：`oneesan→commanding` 等别名归一                | 气场强等芯片能亮，互斥规则生效       |
| 11  | 年龄无 touch 锁       | `ageTouched`；仅手拖/确认后锁定；空白填充用随机年龄                               | 不会「只拖年龄就被冲掉」或「死卡 22」  |
| 12  | 默认性别/取向看起来已选      | 初始 `gender`/`sexualOrientation` 为空；按钮仅 `*Touched` 后高亮          | 空白一键填充改性别不再像「擅自改我的选择」 |
| 13  | update 丢新区段       | `updatable` 含 `persona_axes`/`country`/`region_key`；落库规范化      | 后台改轴/地区能保存            |
| 14  | Create 去重回退丢字段    | 回退 `CompanionProfile` 补齐 axes/country/region_key/language      | 偶发冲突路径不再「像没存全」        |
| 15  | generate 不回传 axes | 响应附带本次 `persona_axes` / labels / region；前端写入                   | 简介与存档轴一致              |
| 16  | 城市→语言推断脆弱         | 城市别名表 + 弱匹配改为前缀且 ≥6；`Mexico City`/`Sao Paulo`/`Hong Kong` 等可命中 | 国外城人设不再默认中文味          |




### P2


| #   | 问题                    | 改动                                                  | 预期体感                    |
| --- | --------------------- | --------------------------------------------------- | ----------------------- |
| 18  | 地区 * 不校验              | 创建校验 `regionKey`；错误态挂在地区选择器                         | 不选地区无法提交                |
| 19  | 取向滚轮英文                | `orientationOptions` + `range-key="label"`          | 滚轮显示本地化文案               |
| 20  | 切语言改城市 / 竞态           | locale 请求代次；语言切换不强制改已选城市                            | 换语言城市不莫名跳变              |
| 21  | 锁城不传 random-profile   | API 支持 `city=`；前端传锁定城                               | 锁城后简介与城市一致              |
| 22  | applyLong 静默失败        | 放宽扩展接受条件；成功 toast `autofillExpanded`                | 扩展有反馈                   |
| 23  | expand_user_input 空操作 | generate 在扩展模式追加强制「保留草稿痕迹」指令                        | 扩展模式真正约束模型              |
| 24  | axes JSON 字符串被拒       | `resolve_axes_from_payload` 解析 JSON 字符串 / `keys` 包装 | 再生成不会无故换一套轴             |
| 25  | 性别英/中混用               | `_normalize_gender` + create/update/clamp 统一为 男/女   | Admin/英文 payload 不再写坏性别 |
| 26  | city max 20 静默截断      | Profile/ORM `city` → 64；`_alter_column_length`      | 较长城市名可完整保存              |
| 27  | Batch 未透传地区           | Admin batch 传 `region`/`region_key`/`country`       | 可按地理批量生成                |
| 28  | Autofill 出互斥性格        | 合并后过滤 mutex 并补足 ≥2                                  | 填充完可直接创建，少一轮冲突          |
| 29  | 性格一律用 、               | 按 companion 语种选用 `、` 或 `,`                          | 非中文标签分隔自然               |




### P3


| 项                       | 改动                                          | 预期体感        |
| ----------------------- | ------------------------------------------- | ----------- |
| Loading 写死中文            | i18n `autofillGenerating`                   | 非中文界面显示对应文案 |
| personaAxesSummary 未展示  | 创建页 axes 预览区展示 summary                      | 能看到维度摘要     |
| AI 填过再填充重抽              | 填充后将 gender/orientation/city/age 标为 touched | 再点填充保留已确认字段 |
| 显式 zh + 非中城市被覆盖         | create：仅未显式传 `language` 时按城推断               | 刻意设 zh 可保留  |
| 缺 Anthropic/Volcano Key | **未改密钥**；见下方运维说明                            | 需在部署环境补齐    |


---



## 主要涉及文件

**后端：** `api/auth.py`, `api/posts.py`, `api/moments.py`, `api/companions.py`, `api/culture.py`, `api/admin.py`, `core/session_store.py`, `core/state.py`, `core/database.py`, `services/memory.py`, `services/companion_manager.py`, `services/persona_axes.py`, `services/region_catalog.py`, `services/culture_data.py`

**前端：** `pages/profile/index.vue`, `pages-sub/create/index.vue`, `utils/personalityTags.js`, `i18n/locales/*.json`

---



## 运维注意

1. **Token：** 已登录用户在下次登录前仍持有旧格式 Token（若有）；重登后一律换新高熵 Token。
2. **DB：** `facts.user_id`、`user_relation_summaries`、`companions.city VARCHAR(64)` 依赖启动时 `_ensure_column` / `create_all`。
3. **记忆：** 历史无 `user_id` 的 facts / 旧 Chroma collection 不会自动迁移；绑定用户后的新对话写入隔离存储。
4. **环境变量：** 确认 `ANTHROPIC_API_KEY` / `VOLCANO_API_KEY`（或管理后台模型配置）已按部署填写，否则生图/部分 LLM 路径仍可能失败。
5. **未纳入本次：** 报告 #4 首页朋友圈 `user_id` 未传导致列表恒空——若仍复现，需单独接线 `api/moments` → `get_moments_feed(user_id=…)`。

---



## 建议验证清单

- [ ] 未登录调用上传图片 → 401  
- [ ] 登录 A → 登出 → 旧 token 调 `/api/auth/me` → 401  
- [ ] 两账号昵称相同、不同 username → 看不到对方伴侣  
- [ ] 「我的」统计 ≈ 本人伴侣数  
- [ ] 创建页：空白填充随机性别；拖年龄后再填充年龄保留；`气场强` 芯片选中  
- [ ] 锁「México City / Sao Paulo」填充 → 非中文文化语境  
- [ ] Admin 改 `persona_axes`/`country` 后刷新仍在  
- [ ] 同伴侣双账号 WS：互不覆盖 session 元数据  