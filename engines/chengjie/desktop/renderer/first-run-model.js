"use strict";

// 首启向导纯函数模型（P0-1 A2/A5）：步骤编排 / AI 状态预填 / 测试与保存结果视图。
// 双模式：浏览器经 <script> 加载后挂全局（first-run.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/first-run-model.test.js 单测）。
//
// 设计：不碰 DOM / IPC——所有 I/O 由 first-run.js 完成，这里只做「响应 → 展示模型」映射，
// 保证向导流程可在无 Electron 环境下回归。

var FR_STRINGS = {
  zh: {
    // 欢迎步（原先硬编码在 first-run.js::renderBasic，英文用户看不到翻译，故收进这里）
    welcome_title: "欢迎使用 智聊",
    // 白标部署（cfg.brand.product 非空）时的欢迎标题。此前是 first-run.js 里
    // `state.lang === "en" ? "Welcome to " : "欢迎使用 "` 的内联三元——白标客户改一次
    // 品牌名就要改代码，且中文串烙在逻辑层。占位符由调用方 replace。
    welcome_to_brand: "欢迎使用 {brand}",
    welcome_sub: "多平台 AI 客服，装好即用——不用注册账号、也不用自己配 AI 模型。首次启动会自动准备本地服务，稍等片刻即可进入。",
    welcome_managed_note: "AI 引擎与额度我们已为你配好，直接用就行。",
    welcome_value_hook: "装好就能聊——不用注册、不用自己配 AI",
    lang_label: "界面语言",
    lang_follow: "跟随系统",
    token_label: "后台访问令牌（默认 admin）",
    btn_next: "下一步",
    btn_claim_start: "免费领 100 万字符",
    btn_enter: "进入工作台",
    celebrate_title: "可以开始了",
    celebrate_sub_claimed: "免费 100 万字符已激活。剩余额度随时可在顶栏徽章查看；用完可在「会员中心」邀请好友、联系客服或购买。",
    celebrate_sub_skipped: "体验额度已就绪。随时可在「会员中心」领取免费 100 万字符或加客服加量。",
    celebrate_sub_gift: "绑定码已就绪——发给客服核销后 10 万字符自动到账。",
    step_welcome: "欢迎",
    step_claim: "领取",
    step_celebrate: "就绪",
    ai_title: "配置 AI 大模型（翻译 / 智能回复引擎）",
    ai_sub: "只需一个 OpenAI 兼容 API Key（如 DeepSeek）。跳过则翻译暂不可用，之后可在后台「接入向导」补配。",
    ai_key_label: "AI API Key",
    ai_base_label: "接口地址（base_url）",
    ai_model_label: "模型",
    btn_test: "测试连接",
    btn_save: "保存并继续",
    btn_skip: "跳过",
    btn_back: "上一步",
    btn_finish: "完成并进入",
    testing: "正在连接 AI 服务…",
    test_ok: "连接成功，Key 有效",
    test_fail: "连接失败",
    saving: "正在保存…",
    save_fail: "保存失败",
    ready: "翻译就绪 ✓ 已保存并即时生效",
    saved_not_ready: "已保存，但连接自检未通过：请核对 Key / 接口地址 / 网络，稍后可在后台「接入向导」重试",
    key_required: "API Key 不能为空",
    backend_wait: "本地后台服务还在启动，请稍候几秒再试",
    result_ok_title: "一切就绪",
    result_ok_sub: "AI 已连通，翻译与智能回复可直接使用。",
    result_warn_title: "还差一步",
    result_warn_sub: "AI Key 已保存但未验证通过。进入后可在后台「接入向导 → AI 大模型」重测。",
    already_configured: "已检测到 AI 配置，无需重复填写。",
    trial_title: "已为你开启体验额度",
    trial_sub: "装完即可直接用，无需注册。剩余额度随时可在顶栏徽章和「会员中心」查看。",
    trial_chars: "可用字符",
    trial_hours: "有效时长",
    trial_hours_unit: "小时",
    btn_start: "开始使用",
    // ── 注册领免费额度（P2；2026-08-11 起 100 万字符 · 无期限）──
    claim_title: "留个联系方式，免费领 100 万字符",
    // 「100 万」而非「1000000」：上方额度格用 frFormatChars 出的是「10 万」，
    // 同一屏两种数字写法看着像两个人写的（视觉验收所见）。
    claim_sub: "100 万字符 · 不限渠道 · 无期限，绑定本机使用。填 Telegram 用户名或手机号即可，不用注册账号、不填邮箱。",
    claim_ph: "@你的Telegram用户名 或 手机号",
    invite_ph: "邀请码（选填，好友给你的 ZL-XXXXXX，双方各得 10 万）",
    btn_claim: "免费领取",
    btn_skip_claim: "先不领，直接开始",
    claim_need_contact: "填一下联系方式，客服才能找到你",
    claim_sending: "正在提交…",
    claim_waiting: "已提交，正在签发…（通常 1 分钟内完成，好了自动生效，可先开始使用）",
    claim_slow: "已提交。签发通常 1 分钟内，好了会自动生效——你可以先开始使用，稍后在「会员中心」查看额度。",
    claim_ok: "已激活：免费 100 万字符已到账",
    claim_fail: "领取失败",
    claim_no_fp: "读不到本机标识，无法绑定授权。可先直接开始使用，稍后在「会员中心」重试。",
    claim_network: "连不上服务器。可先直接开始使用，稍后在「会员中心」重试。",
    claim_exhausted: "这台机器的免费额度已经领过了。可在「会员中心」邀请好友、联系客服或购买。",
    // ── 加客服领 10 万字符 ──
    gift_title: "再领 10 万字符",
    gift_sub: "把下面这串码发给客服，核销后额度自动到账——不用你再回来操作。",
    btn_gift: "领 10 万字符",
    btn_open_tg: "打开 Telegram 发给客服",
    btn_open_wa: "用 WhatsApp 发给客服",
    btn_copy: "复制",
    copied: "已复制",
    gift_fail: "取码失败，稍后可在「会员中心」再试",
    gift_done: "已到账 10 万字符",
    // ── 首屏能力三点式（P0-A 2026-08-21：让新用户 5 秒内明白产品是什么，
    //    先建立价值认知再要联系方式）──
    feat1_t: "多平台消息，一个收件箱",
    feat1_d: "Telegram / WhatsApp / LINE 统一接待",
    feat2_t: "AI 替你回复",
    feat2_d: "自动拟好草稿，你只管把关",
    feat3_t: "聊天实时互译",
    feat3_d: "客户什么语言都能接",
    // ── 额度对比（P0-B：尝鲜 vs 注册领取，10 倍差距从文案数字升为视觉对比）──
    cmp_now_t: "现在就能用",
    cmp_now_note: "已激活",
    cmp_claim_t: "留联系方式再领",
    cmp_claim_note: "免费 · 无期限",
    // ── 领取步信任说明 + 字符换算锚点（P0-C：要联系方式必须给理由）──
    claim_trust: "联系方式仅用于发放额度与售后服务，不会向你推销，也不会提供给第三方。",
    claim_calc: "参考：一条普通回复约消耗 50–150 字符。",
    invite_toggle: "有邀请码？（选填）",
    // ── 就绪屏「接下来三步」（P0-D：修「进入工作台后不知道干嘛」的落地断层；
    //    纯信息地图不做深链——finish 后落点由壳决定，假深链失效比没有更糟）──
    next_title: "接下来 3 步",
    next1_t: "绑定第一个聊天渠道",
    next1_d: "左侧「渠道」入口，扫码即可接入",
    next2_t: "看 AI 替你接待",
    next2_d: "客户消息进来自动拟稿，你确认后发出",
    next3_t: "额度随时可查",
    next3_d: "顶栏紫色徽章可进「会员中心」查余量、加量",
    btn_back_claim: "← 免费领 100 万字符",
  },
  en: {
    welcome_title: "Welcome to ChatX",
    welcome_to_brand: "Welcome to {brand}",
    welcome_sub: "Multi-platform AI customer service, ready right after install — no account, no AI setup on your side. The local service is starting up; you'll be in shortly.",
    welcome_managed_note: "The AI engine and your quota are already set up for you — just start.",
    welcome_value_hook: "Ready to chat — no signup, no AI setup on your side",
    lang_label: "Language",
    lang_follow: "System default",
    token_label: "Backend token (default admin)",
    btn_next: "Next",
    btn_claim_start: "Claim 1,000,000 free characters",
    btn_enter: "Enter workspace",
    celebrate_title: "You're ready",
    celebrate_sub_claimed: "Your 1,000,000 free characters are active. Check the remaining quota anytime from the top-bar badge; when it runs out, invite friends, ask support, or purchase from Membership.",
    celebrate_sub_skipped: "Your starter allowance is ready. Claim 1,000,000 free characters or top up via support anytime from Membership.",
    celebrate_sub_gift: "Your bind code is ready — send it to support and 100,000 characters credit automatically.",
    step_welcome: "Welcome",
    step_claim: "Claim",
    step_celebrate: "Ready",
    ai_title: "Set up the AI model (translation / smart replies)",
    ai_sub: "One OpenAI-compatible API key (e.g. DeepSeek) is enough. Skip for now and translation stays off until configured in the admin setup wizard.",
    ai_key_label: "AI API Key",
    ai_base_label: "Endpoint (base_url)",
    ai_model_label: "Model",
    btn_test: "Test connection",
    btn_save: "Save & continue",
    btn_skip: "Skip",
    btn_back: "Back",
    btn_finish: "Finish",
    testing: "Connecting to AI service…",
    test_ok: "Connected — key works",
    test_fail: "Connection failed",
    saving: "Saving…",
    save_fail: "Save failed",
    ready: "Translation ready ✓ — saved and live",
    saved_not_ready: "Saved, but the connection check failed: verify key / endpoint / network. You can retest in the admin setup wizard.",
    key_required: "API Key is required",
    backend_wait: "Local backend is still starting — try again in a few seconds",
    result_ok_title: "All set",
    result_ok_sub: "AI is connected. Translation and smart replies work out of the box.",
    result_warn_title: "One more step",
    result_warn_sub: "The AI key is saved but not verified. Retest later in admin → setup wizard → AI model.",
    already_configured: "An AI configuration was detected — nothing to fill in.",
    trial_title: "Your starter allowance is ready",
    trial_sub: "Works right after install — no signup needed. You can check the remaining "
      + "quota any time from the top-bar badge or the membership center.",
    trial_chars: "Characters",
    trial_hours: "Valid for",
    trial_hours_unit: "hours",
    btn_start: "Start using",
    claim_title: "Leave a contact, claim 1,000,000 free characters",
    claim_sub: "1,000,000 characters, all channels, no expiry — bound to this machine. "
      + "A Telegram handle or phone number is enough; no account, no email.",
    claim_ph: "@your_telegram or phone number",
    invite_ph: "Invite code (optional, ZL-XXXXXX from a friend — you both get 100,000)",
    btn_claim: "Claim for free",
    btn_skip_claim: "Skip, just start",
    claim_need_contact: "Add a contact so support can reach you",
    claim_sending: "Submitting…",
    claim_waiting: "Submitted, issuing… (usually within a minute, activates itself — you can start now)",
    claim_slow: "Submitted. Issuing usually finishes within a minute and activates itself — "
      + "start using the app now; check your quota later in Membership.",
    claim_ok: "Activated: 1,000,000 free characters credited",
    claim_fail: "Claim failed",
    claim_no_fp: "Can't read this machine's ID, so the licence can't be bound. "
      + "Start using the app for now and retry from the membership center.",
    claim_network: "Can't reach the server. Start using the app for now and retry "
      + "from the membership center.",
    claim_exhausted: "This machine has already claimed its free quota. Invite friends, "
      + "contact support, or purchase from the membership center.",
    gift_title: "Get 100,000 more characters",
    gift_sub: "Send the code below to support. Once they redeem it the quota lands "
      + "automatically — you don't have to come back.",
    btn_gift: "Get 100,000 characters",
    btn_open_tg: "Open Telegram and message support",
    btn_open_wa: "Message support on WhatsApp",
    btn_copy: "Copy",
    copied: "Copied",
    gift_fail: "Couldn't get a code — retry later from the membership center",
    gift_done: "100,000 characters credited",
    feat1_t: "Every platform, one inbox",
    feat1_d: "Telegram, WhatsApp and LINE in one place",
    feat2_t: "AI replies for you",
    feat2_d: "Drafts are written automatically — you just approve",
    feat3_t: "Live two-way translation",
    feat3_d: "Serve customers in any language",
    cmp_now_t: "Ready now",
    cmp_now_note: "Active",
    cmp_claim_t: "After you claim",
    cmp_claim_note: "Free · no expiry",
    claim_trust: "Your contact is used only to issue the quota and for support — "
      + "no marketing, never shared with anyone.",
    claim_calc: "For reference: a typical reply uses ~50–150 characters.",
    invite_toggle: "Have an invite code? (optional)",
    next_title: "Your next 3 steps",
    next1_t: "Connect your first channel",
    next1_d: "Open “Channels” in the sidebar and scan to connect",
    next2_t: "Watch AI handle chats",
    next2_d: "Incoming messages get drafts automatically — approve and send",
    next3_t: "Check your quota anytime",
    next3_d: "The purple badge in the top bar opens Membership",
    btn_back_claim: "← Claim 1,000,000 free characters",
  },
};

// 向导词典只有 zh/en。扩展语白名单（vi/th/id，与 main.js::SHELL_EXT_LANGS /
// 后端 i18n_packs.UI_LANGS 对齐）→ en 底：扩展语坐席英文向导远比中文可用。
// 其余（空=跟随系统 / 真未知语）维持 zh——保守口径，只对白名单语言改变行为。
var FR_EXT_LANGS = { vi: 1, th: 1, id: 1 };
function frT(lang, key) {
  var d = FR_STRINGS[lang] || (FR_EXT_LANGS[lang] ? FR_STRINGS.en : FR_STRINGS.zh);
  return d[key] != null ? d[key] : key;
}

// 系统语言 → 壳语言码（「跟随系统」真跟随，2026-08-27）。与 main.js::shellLangFromTag
// 同一家族映射（zh 系按 TW/HK/MO/Hant 细分繁体；en 家族→en；扩展语取主子标签）；
// 认不出返回 ""（调用方回落中文）。输入=navigator.language / app.getLocale()。
function frSysLang(raw) {
  var l = String(raw || "").trim().toLowerCase().replace(/-/g, "_");
  if (!l) return "";
  if (l === "en" || l.indexOf("en_") === 0) return "en";
  var p = l.split("_");
  if (p[0] === "zh") {
    return (p.indexOf("tw") > 0 || p.indexOf("hk") > 0 || p.indexOf("mo") > 0
      || p.indexOf("hant") > 0) ? "zh_hant" : "zh";
  }
  if (p[0] === "zh_hant") return "zh_hant";
  if (FR_EXT_LANGS[p[0]]) return p[0];
  return "";
}

// 字符数展示。原实现一律缩成「1w / 2.5w」——那是中文口语写法，**英文界面照样显示
// `1w`**，对英文用户就是个不明所以的字符串（视觉验收时抓到）。改为跟语言走：
// 简体「万」/ 繁体「萬」，en 与扩展语（vi/th/id）用千分位。语言判定与 frT 同口径。
function frFormatChars(n, lang) {
  var v = Math.max(0, Math.round(Number(n) || 0));
  if (lang === "en" || FR_EXT_LANGS[lang]) return v.toLocaleString("en-US");
  if (v >= 10000) {
    var w = v / 10000;
    return (w >= 100 ? w.toFixed(0) : w.toFixed(1).replace(/\.0$/, ""))
      + (lang === "zh_hant" ? " 萬" : " 万");
  }
  return v.toLocaleString("zh-CN");
}

// 步骤编排。
//
// 托管版（客户成品）：固定三屏「欢迎 → 领取 → 就绪」。AI/令牌绝不出场——不靠
// 「此刻 AI 有没有配好」猜（打包漏 key 就会突然弹配置页）。领取步失败也可跳过，
// 所以即便 trialStatus 探不到，仍把 claim 放进漏斗（价值钩子 + 转化入口）。
//
// 自建/开发态：AI 已配置 → 只走基础步；未配置 → 基础 + AI + 结果。
// trialStatus 确认体验档可见时再追加旧版 trial 收尾（自建路径保留，托管已并入 welcome/claim）。
function frBuildSteps(aiStatus, trialStatus, opts) {
  var managed = !!(opts && opts.managed);
  if (managed) return ["welcome", "claim", "celebrate"];
  var configured = !!(aiStatus && aiStatus.ok && aiStatus.configured);
  var steps = configured ? ["basic"] : ["basic", "ai", "result"];
  if (frTrialView(trialStatus).show) steps.push("trial");
  return steps;
}

// 托管欢迎屏：价值钩子前置 + 体验额度（有则展示，无则只讲卖点，绝不凭空承诺数字）。
function frWelcomeView(trialStatus, lang) {
  var tv = frTrialView(trialStatus, lang);
  return {
    title: frT(lang, "welcome_title"),
    sub: frT(lang, "welcome_value_hook"),
    managedNote: frT(lang, "welcome_managed_note"),
    showQuota: !!tv.show,
    chars: tv.chars,
    hours: tv.hours,
    charsLabel: frT(lang, "trial_chars"),
    hoursLabel: frT(lang, "trial_hours"),
    hoursUnit: frT(lang, "trial_hours_unit"),
    cta: frT(lang, "btn_claim_start"),
  };
}

// 就绪庆祝屏：按用户是否领取 / 是否看过赠量码给出不同收尾文案。
// P0-D（2026-08-21）：附「接下来三步」信息地图（修落地断层）+ 未领取用户的
// 「回去领取」次入口——bind-code 赠量要求先有 claim_id（trial_claim_client.bind_code
// 无单直接 not_claimed），给 skip 用户摆绑定码入口＝必然失败，改成送回领取步。
function frCelebrateView(opts, lang) {
  var o = opts || {};
  var subKey = o.claimed ? "celebrate_sub_claimed"
    : (o.giftShown ? "celebrate_sub_gift" : "celebrate_sub_skipped");
  return {
    title: frT(lang, "celebrate_title"),
    sub: frT(lang, subKey),
    cta: frT(lang, "btn_enter"),
    showClaimBack: !o.claimed,
    backCta: frT(lang, "btn_back_claim"),
    stepsTitle: frT(lang, "next_title"),
    steps: frNextSteps(lang),
  };
}

// ── 首屏能力三点式（P0-A）：静态价值主张，刻意不探后端能力——首启时后端可能
// 还没起，且欢迎屏要回答「这是什么」，不是「此刻什么能用」。
function frFeatureList(lang) {
  return [
    { icon: "💬", t: frT(lang, "feat1_t"), d: frT(lang, "feat1_d") },
    { icon: "🤖", t: frT(lang, "feat2_t"), d: frT(lang, "feat2_d") },
    { icon: "🌐", t: frT(lang, "feat3_t"), d: frT(lang, "feat3_d") },
  ];
}

//: 注册领取的营销额度数字（与 claim_title / btn_claim_start 文案同源）。
//: 真值以官网台账签发为准——这里只做展示对比，不构成额度的第二事实源。
var FR_CLAIM_CHARS = 1000000;

// ── 额度对比视图（P0-B）：尝鲜（本机真值）vs 注册领取（营销数字）。
// 只在体验档确认可见时 show——额度状态探不到就不摆数字（绝不凭空承诺）；
// 隐藏时欢迎屏仍有 CTA 文案里的「100 万」，语义完整。
function frQuotaCompareView(trialStatus, lang) {
  var tv = frTrialView(trialStatus, lang);
  return {
    show: tv.show,
    now: {
      label: frT(lang, "cmp_now_t"),
      chars: frFormatChars(tv.chars, lang),
      note: frT(lang, "cmp_now_note"),
    },
    claim: {
      label: frT(lang, "cmp_claim_t"),
      chars: frFormatChars(FR_CLAIM_CHARS, lang),
      note: frT(lang, "cmp_claim_note"),
    },
  };
}

// ── 就绪屏「接下来三步」（P0-D）──
function frNextSteps(lang) {
  return [
    { n: "1", t: frT(lang, "next1_t"), d: frT(lang, "next1_d") },
    { n: "2", t: frT(lang, "next2_t"), d: frT(lang, "next2_d") },
    { n: "3", t: frT(lang, "next3_t"), d: frT(lang, "next3_d") },
  ];
}

// ── 步骤指示器状态（P1-I）：done/current/todo 三态——已完成的步骤打 ✓，
// 给「走到哪了」以进度感。activeId 不在 steps 里（异常态）＝全 todo 不装完成。
function frStepDotsView(steps, activeId) {
  var ids = Array.isArray(steps) ? steps : [];
  var cur = ids.indexOf(activeId);
  var out = [];
  for (var i = 0; i < ids.length; i++) {
    out.push({
      id: ids[i],
      state: cur < 0 ? "todo" : (i < cur ? "done" : (i === cur ? "current" : "todo")),
    });
  }
  return out;
}

// 体验额度展示模型。只在「后端确认可见 + 确实是体验档 + 还没用尽」时 show——
// 付费额度不该出现在首启欢迎流里，用尽/过期更不该拿来当欢迎语。
function frTrialView(trialStatus, lang) {
  var d = trialStatus || {};
  var show = !!(d.ok && d.visible && d.source === "local_trial" && !d.exceeded);
  return {
    show: show,
    title: frT(lang, "trial_title"),
    sub: frT(lang, "trial_sub"),
    chars: Number(d.remaining != null ? d.remaining : (d.included || 0)) || 0,
    hours: (d.hours_left == null ? null : Number(d.hours_left)),
  };
}

// ── 注册领 7 天（P2）纯模型 ────────────────────────────────────────────────
//
// 一条硬约束贯穿下面几个函数：**领取失败绝不能把人卡在开机第一屏**。体验档已经
// 在跑，用户此刻什么都不做也能用；所以任何错误都渲染成「可跳过的提示」而不是拦路弹窗。

// 联系方式校验。刻意宽松——这不是注册表单，只是「客服怎么找到你」：
// @handle / handle / 手机号都收，只挡空值和明显不是联系方式的一两个字符。
function frClaimValidate(contact) {
  var s = String(contact == null ? "" : contact).trim();
  if (!s) return { ok: false, err: "claim_need_contact" };
  var core = s.replace(/^@+/, "").replace(/[\s\-()]/g, "");
  if (core.replace(/^\+/, "").length < 3) return { ok: false, err: "claim_need_contact" };
  return { ok: true, err: "" };
}

// claim / poll 响应 → 展示模型。
// phase: sending | waiting | ok | exhausted | fail —— UI 据此决定按钮组与配色。
// 「已激活」与「已用尽」是两种终态：前者继续引导领赠量，后者引导购买，绝不混为一谈。
function frClaimResultView(resp, lang) {
  var d = resp || {};
  if (d.activated) {
    return { phase: "ok", cls: "ok", text: frT(lang, "claim_ok"), done: true, canGift: true };
  }
  if (d.trial_exhausted || d.exhausted || d.activate_error === "expired") {
    return { phase: "exhausted", cls: "warn", text: frT(lang, "claim_exhausted"),
      done: true, canGift: false };
  }
  if (d.ok) {
    // 建单/轮询成功但还没签发下来——这是最常见的中间态，不是错误。
    return { phase: "waiting", cls: "info", text: frT(lang, "claim_waiting"),
      done: false, canGift: false };
  }
  var err = String(d.error || d.activate_error || "");
  var key = err === "no_fingerprint" ? "claim_no_fp"
    : (err === "network" || err.indexOf("http_") === 0) ? "claim_network"
      : "claim_fail";
  var extra = (key === "claim_fail" && err) ? "：" + err : "";
  return { phase: "fail", cls: "err", text: frT(lang, key) + extra, done: true, canGift: false };
}

// 轮询编排：给定已轮询次数，回答「还要不要再轮、下次隔多久」。
// 前 6 次 5 秒一轮（覆盖厂商机 cron 的常见延迟），之后就交给**后端后台轮询**兜底
// 并让用户走人——把开机第一屏变成进度条守望是最差的体验。
function frClaimPollPlan(attempt, maxAttempts) {
  var max = maxAttempts == null ? 6 : maxAttempts;
  var n = Number(attempt) || 0;
  if (n >= max) return { again: false, delayMs: 0, giveUpKey: "claim_slow" };
  return { again: true, delayMs: 5000, giveUpKey: "" };
}

// bind-code 响应 → 展示模型。深链拿不到也不算失败（码本身就能手发给客服）。
function frGiftView(resp, lang) {
  var d = resp || {};
  if (!d.ok || !d.bind_code) {
    return { ok: false, code: "", tgUrl: "", text: frT(lang, "gift_fail") };
  }
  return {
    ok: true,
    code: String(d.bind_code),
    tgUrl: String(d.telegram_url || ""),
    waUrl: String(d.whatsapp_url || ""),
    text: frT(lang, "gift_sub"),
  };
}

// AI 步预填：后端不可达/未配置时给桌面种子同款默认（deepseek），保证表单可直接填。
function frAiPrefill(aiStatus) {
  var st = (aiStatus && aiStatus.ok) ? aiStatus : {};
  return {
    base_url: st.base_url || "https://api.deepseek.com",
    model: st.model || "deepseek-chat",
    configured: !!st.configured,
    api_key_masked: st.api_key_masked || "",
  };
}

// 输入校验（提交前）：key 必填；base_url/model 可空（后端回落已存值/默认）。
function frValidateAiInput(vals) {
  var key = String((vals && vals.api_key) || "").trim();
  if (!key) return { ok: false, err: "key_required" };
  return { ok: true, err: "" };
}

// POST /api/setup/test-ai 响应 → 展示模型 {cls, text}
function frAiTestView(resp, lang) {
  if (resp && resp.ok) return { cls: "ok", text: frT(lang, "test_ok") };
  var extra = resp && (resp.msg || resp.detail) ? String(resp.msg || resp.detail) : "";
  return { cls: "err", text: frT(lang, "test_fail") + (extra ? ": " + extra : "") };
}

// POST /api/setup/ai-key 响应 → 展示模型 {cls, ready, text}
// ready=true 才算「翻译就绪」绿灯（后端已用新 key 实连自检）。
function frAiSaveView(resp, lang) {
  if (!resp || !resp.ok) {
    var extra = resp && resp.detail ? String(resp.detail) : "";
    return { cls: "err", ready: false, text: frT(lang, "save_fail") + (extra ? ": " + extra : "") };
  }
  if (resp.ai_ready) return { cls: "ok", ready: true, text: frT(lang, "ready") };
  return { cls: "warn", ready: false, text: frT(lang, "saved_not_ready") };
}

// 结果步（A5 绿灯）：保存视图 → 终屏标题/副文案
function frResultView(saveView, lang) {
  if (saveView && saveView.ready) {
    return { cls: "ok", title: frT(lang, "result_ok_title"), sub: frT(lang, "result_ok_sub") };
  }
  return { cls: "warn", title: frT(lang, "result_warn_title"), sub: frT(lang, "result_warn_sub") };
}

// 首启漏斗事件白名单（与后端 trial_claim_client.FUNNEL_EVENTS 同口径）。
// 单列成表：first-run.js 只从这里取名，拼错事件名在单测就会红，而不是数据悄悄丢。
// invite_share / claim_banner 属会员页（web 侧），列在此处只为与后端保持同一张表。
// invite_open=领取屏邀请码折叠展开（P0 收进折叠后的需求读数）；claim_back=就绪屏
// 「回去领取」回门点击（跳过者挽回入口的效果读数）。
var FR_FUNNEL_EVENTS = ["welcome", "claim_submit", "claim_ok", "claim_skip", "gift_open",
  "done", "invite_share", "invite_open", "claim_back", "claim_banner"];

// 是否弹向导。默认「只弹一次」。权威完成态有两处：
//   ① localStorage 旗标（快路径，同步可读）
//   ② desktop config.json 的 onboarding.completed（卸载重装/清缓存仍在 userData）
// 任一已完成 → 不弹；`--first-run` 强制重看时两者都无视。
// 强制态下**仍然照常写标记**：强制是显式的一次性动作，不该顺手改掉常态。
function frShouldShowWizard(flagPresent, forced, configCompleted) {
  if (forced) return true;
  if (flagPresent || configCompleted) return false;
  return true;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    FR_STRINGS: FR_STRINGS,
    FR_FUNNEL_EVENTS: FR_FUNNEL_EVENTS,
    FR_CLAIM_CHARS: FR_CLAIM_CHARS,
    frT: frT,
    frSysLang: frSysLang,
    frFormatChars: frFormatChars,
    frShouldShowWizard: frShouldShowWizard,
    frBuildSteps: frBuildSteps,
    frWelcomeView: frWelcomeView,
    frCelebrateView: frCelebrateView,
    frFeatureList: frFeatureList,
    frQuotaCompareView: frQuotaCompareView,
    frNextSteps: frNextSteps,
    frStepDotsView: frStepDotsView,
    frAiPrefill: frAiPrefill,
    frValidateAiInput: frValidateAiInput,
    frAiTestView: frAiTestView,
    frAiSaveView: frAiSaveView,
    frResultView: frResultView,
    frTrialView: frTrialView,
    frClaimValidate: frClaimValidate,
    frClaimResultView: frClaimResultView,
    frClaimPollPlan: frClaimPollPlan,
    frGiftView: frGiftView,
  };
}
