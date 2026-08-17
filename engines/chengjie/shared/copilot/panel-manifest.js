"use strict";
/* 业务助手「装配清单」——两端右栏（网页 unified_inbox 原生面板 / 统一 App app.html）
   卡片装配的**单一事实源**（P1-2，2026-08-12）。

   为什么存在：组件层早已单源（shared/copilot + 双树镜像门禁），但「哪张卡、进哪个
   tab、什么顺序、叫什么名」从没有单源——三套装配各自演化出「SOP 两端不同 tab、
   同组件三个名字」的漂移（老板实录反馈）。本表把装配层钉成数据：
   - pytest 门禁 tests/test_copilot_panel_manifest.py 逐条对照两端 HTML 实际装配，
     漂移即红（新增卡片必须先登记本表——这里就是装配层的变更控制点）；
   - 渲染层后续（P2+）直接消费本表生成卡片骨架，届时 HTML 手写装配退役。

   字段：
   - tab: reply|customer|tools|pinned（pinned=跨 tab 常驻，如英雄卡）；
   - surfaces: 当前真实存在于哪些端（web=unified_inbox 原生 / app=统一 App）——
     缺口是**登记在案的债**（note/pending 写明去向），不是「大家都对」；
   - keyWeb/keyApp: 两端词典键（web=服务端 Jinja 词典 / app=cp-i18n.js）；
   - appCardId: app 端 data-cp-card 与清单 id 不同名时的别名（如 custinfo↔customer）；
   - converged: true=两端标题 zh+en 显示值必须逐字相等（门禁校验）；false=登记的
     待收敛项（pending 写明前置条件，防「同名不同物」的假收敛）；
   - hiddenBoth: 两端都必须带 hidden（如 2026-08-01 运营决策下线的 AI 对话分析）。

   ⚠ 下方字面量必须保持**严格 JSON**（双引号/无尾逗号/无注释）——pytest 门禁
   直接切片 json.loads；注释一律写在字面量外。 */
(function (root) {
  var MANIFEST =
{
  "version": 1,
  "tabs": [
    { "id": "reply",    "keyWeb": "inbox.cp.tab.reply",    "keyApp": "cp.app.tab_reply" },
    { "id": "customer", "keyWeb": "inbox.cp.tab.customer", "keyApp": "cp.app.tab_customer" },
    { "id": "tools",    "keyWeb": "inbox.cp.tab.tools",    "keyApp": "cp.app.tab_tools" }
  ],
  "cards": [
    { "id": "hero", "tab": "pinned", "surfaces": ["web"],
      "keyWeb": "inbox.hero.title",
      "note": "英雄卡＝关系阶段一句话 + 工作目标行（宿主逻辑）。原「AI 下一步」建议面板（cp-next-actions）已于 2026-08-14 按运营决策整体下线，app 端英雄卡随之移除（关系/目标信息由客户关系 tab 对应卡承担）" },
    { "id": "draft", "tab": "reply", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.ai_reply", "keyApp": "cp.app.h_draft", "converged": true,
      "note": "2026-08-17 工具箱重组后回复台唯一卡：回复台=生成动作区，语音/人设/知识库低频配置与工具全部迁 tools（老板需求）；卡内联人设选择器保留" },
    { "id": "custinfo", "tab": "customer", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.cust_info", "keyApp": "cp.app.h_customer",
      "appCardId": "customer", "converged": true },
    { "id": "relstage", "tab": "customer", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.rel_progress", "keyApp": "cp.app.h_rel", "converged": true,
      "note": "P1-5 已收敛：conv 级 relationship-stage 响应在 handler 层附 journey{funnel_stage,label}（unified_inbox_relationship_routes，软失败绝不伤主体；.py 改动随下次重启点亮），cp-rel-stage 带 journey 才渲染旅程子分区（老后端/contacts 关=缺省无痕）；「关系进展」=陪聊进展(关系阶段)+生意进展(旅程)双维语义" },
    { "id": "goal", "tab": "customer", "surfaces": ["web", "app"],
      "keyWeb": "inbox.goal.title", "keyApp": "cp.app.h_goal", "converged": true },
    { "id": "chain", "tab": "customer", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.chain", "keyApp": "cp.app.h_chain", "converged": true },
    { "id": "collab", "tab": "customer", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.collab_notes", "keyApp": "cp.app.h_collab", "converged": true,
      "note": "P1-4 第二刀已收敛：app 侧 <cp-collab notes> 内建注解子分区（列表走 collab-context 自带 recent_notes 零新读、写走 POST notes；@ 选人/编辑/删除留网页端，卡内小字指路）；web 侧继续宿主内联注解，组件不带 notes 属性零重复" },
    { "id": "convops", "tab": "tools", "surfaces": ["web", "app"],
      "keyWeb": "inbox.convops.title", "keyApp": "cp.app.h_convops", "converged": true,
      "note": "会话运维。app 已落地 v1（P1-4 2026-08-12，cp-conv-ops.js：档位状态+暂停全自动+搁置1h/4h+归档两步确认）；标签/自动发记录/参数仍 web 独有（卡底小字指路），升档全自动刻意只留网页（群聊确认等护栏在那边）" },
    { "id": "xlate", "tab": "tools", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.xlate", "keyApp": "cp.app.h_xlate", "converged": true,
      "note": "2026-08-17 新增：单次翻译四件套（图片/语音/多线路对照/文档）共享组件 cp-xlate-tools，直连收件箱同一批 translate-* 端点；收件箱「对话翻译」弹层底部的四按钮保留为第二入口（同端点单源逻辑，未来按用量退役）。弱会话依赖：无会话也可用（no-ctx 豁免），auto 目标语仅在有会话时走服务端解析" },
    { "id": "voice", "tab": "tools", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.voice", "keyApp": "cp.app.h_voice", "converged": true,
      "note": "2026-08-17 自 reply 迁入 tools（工具箱重组）：发送级低频工具；主输入框语音链路不受影响" },
    { "id": "kb", "tab": "tools", "surfaces": ["app"],
      "keyApp": "cp.app.h_kb",
      "note": "知识库/快捷回复（P1-4 第二刀收编桌面原生 aside 的 kb/tpl 两卡，退役前置；2026-08-17 随工具箱重组自 reply 迁入 tools）；web 刻意不挂——composer 的 / 指令面板与 KB 自动推荐浮层已承担同职能，右栏再放=双入口" },
    { "id": "persona", "tab": "tools", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.persona", "keyApp": "cp.app.h_persona", "converged": true,
      "note": "2026-08-17 自 reply 迁入 tools（工具箱重组）：设一次即忘的配置卡；回复工坊卡内联人设选择器仍在 reply" },
    { "id": "analysis", "tab": "tools", "surfaces": ["web", "app"],
      "keyWeb": "inbox.cp.h.ai_analysis", "keyApp": "cp.app.h_analyze",
      "hiddenBoth": true, "converged": true,
      "note": "2026-08-01 运营决策下线（每展开烧一次 LLM 且与回复工坊重复）；保 DOM 防接线断裂" }
  ]
}
;
  root.CP_PANEL_MANIFEST = MANIFEST;
})(typeof window !== "undefined" ? window : this);
