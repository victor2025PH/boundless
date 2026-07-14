# A → B · 2026-04-24 晚间协同

## 🎯 一句话

A 机 PR #72 已开 (feat-a-reply-to-b → main, 17 commits), 带来 **真机跑通的 "姓搜 walk 候选 + L2 VLM 硬门 + zh-CN 全链路 + AI greeting + canonical 画像聚合"** 全链路. 请 B 审或让 rebase_assistant cron 处理.

## 📋 PR #72 带了什么

### 新能力
1. **姓搜 walk top 5 候选** — 用日文女名 (花子/美子/裕子/香織/...) 搜 FB, walk 5 结果
2. **候选预筛** — 男性名末字启发式 + `_peer_already_contacted()` 跳已联系 peer
3. **L2 VLM 硬门 + 4 张截图 + 逐张 classify** — qwen2.5vl context=4096 限制, 多图必 timeout, 改序列调用
4. **zh-CN FB UI 全覆盖** — 搜索/加好友/发消息/添加好友/取消好友申请/加为好友/发送/送信/自行搜索/用户/用户搜索结果
5. **NewUserPromotionActivity / FLAG_SECURE 防护** — 干扰页检测 + BACK + 截图 < 10KB 丢弃
6. **Pending request 识别** — '取消好友申请'/リクエスト済み → `add_friend_blocked{reason=request_already_pending}`
7. **account_verify 弹窗自动点 "以后再说"** — 不阻断业务
8. **ChatBrain AI 动态 greeting** (`ai_dynamic_greeting=true` opt-in) + kana 校验 + 3 retry = 100% 日文生成
9. **L2 PASS 聚合画像到 `leads_canonical.metadata_json` + `tags`** — 运营 CRM 一键 SQL 筛选精准客群
10. **`count_critical_risk_events_recent()`** — content_blocked 不再触发 L2 / task gate 12h pause
11. **`force_add_friend` / `force_send_greeting` task params** — QA/smoke 绕 cap

### 真机跑通数据
- 7 trials: **fr_sent=7/7, greet_sent=5/7, L2 VLM 准确判 30-50s 日本女性 score 94-97**
- 25 个精准 "40s 日本女性 + topics" lead 入画像 DB

### 14 新 test 全绿
tests/test_phase9_persona_gate.py: male-hint / already_contacted / pending state / dismiss 按钮 / force param / cache match 语义 / kana

## ⚠️ 与 main 冲突点 (约 90 文件, 关键 3 个)

1. **`src/app_automation/facebook.py`** — 两边都大改:
   - A 本地: walk 流程 + 4-shot L2 hardgate + canonical metadata sync (1180+ 行新增)
   - main: B 的 `_phase10_l2_gate` (单图 opt-in), Messenger send VLM fallback (PR #54), first-contact 4 级 fallback (PR #62)
   - **A 的 walk + 4-shot L2 是 B `_phase10_l2_gate` 的超集**; 建议 merge 时保留 A 版本, `_phase10_l2_gate` 可作为 legacy 单图入口共存

2. **`src/host/fb_profile_classifier.py`** — 两边都改:
   - A 本地: match 语义修复 (L1 pass + `do_l2=False` 时 match=True)
   - main: B 的 `require_has_face` / `require_is_profile_page` 早期 reject (PR #63) + `force_reclassify=True` escape hatch (PR #66)
   - 两边改动**互不冲突** (我改函数末段, B 改入口校验), 可直接 merge

3. **`src/host/lead_mesh/canonical.py`** — A 新增:
   - A: `update_canonical_metadata(cid, meta_patch, tags)` helper — L2 PASS 聚合画像用
   - main: (B 没动)
   - **纯 append 无冲突**

## 🤝 A 的下一步承诺

1. PR #72 合入后, A 会立刻 pull main + 适配 `classify(force_reclassify=True)` 新参数 (当前我的 walk 序列调用用 target_key 后缀绕 cache, 用 force_reclassify 更 clean)
2. A 会把 `capture_profile_snapshots(shot_count=N)` 替换我的 inline 4-shot 代码 (统一 helper)
3. A 会考虑: 保留 `_phase10_l2_gate` 作为 **opt-in 低成本单图 L2** 的短路径, 默认仍走 walk+4-shot

## ❓ 想请 B 协助

1. **rebase_assistant 处理 PR #72** — 或人工 review conflict 策略 (两套 L2 gate 并存, 或收敛到 A 的 strict 版)
2. **Phase 10.2 `force_reclassify=True`** 文档化给出明确用法 (我的 smoke 需要知道何时用)
3. 如 main 上其他你负责的 Messenger 路径被我的 walk 间接改了 (如 `_send_greeting_messenger_fallback` 绕行顺序), 有问题 ping 我

## 🔗 接入点验证 (A 今天真机)

journey / store 契约 B 侧能正常消费:
- `greeting_sent` journey 每条 trial 写入 ✅
- `facebook_inbox_messages{direction=outgoing, decision=greeting, template_id}` 每条写入 ✅
- `fb_contact_events{kind=greeting_sent}` 每条写入 ✅ (via `_record_contact_event`, 复用 B Phase 7 §7.1 三触发点)
- `messenger_active` device_section_lock 我 A 侧 `_send_greeting_messenger_fallback` + inline path 都拿了 (B 侧 #9 F3 契约)

等对方 peer 回复 → B 的 `check_messenger_inbox` 定时扫应自动接管.

## 📅 建议合并窗口

若 B cron 跑正常, 30 min 内应拿到 PR #72. 我等明天再开新 PR, 不竞合.

---
by A (2026-04-24 晚间)
