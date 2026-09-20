"use strict";

/* 多平台内容注入核心（单一事实来源）——平台无关业务逻辑：
 *   点击翻译 / 媒体翻译 / 智能回复浮钮 / 同步桥回流 / 注入健康上报 / 当前会话上报。
 *
 * 历史上这段逻辑内联在 desktop/inject/tg-inject.js 并直接耦合 Electron 的 ipcRenderer。
 * 现抽成 createInject(host, deps)：把所有「与宿主对话」的动作收口到 host 适配层，
 * 让桌面（Electron preload，host=ipcRenderer 实现）与浏览器扩展（content script，
 * host=chrome.runtime 实现）复用同一份核心，零重复。
 *
 * host 契约（全部可返回 Promise；不需要的能力可省略，core 会安全降级）：
 *   translate({text,target_lang})            -> {ok,text}
 *   translateMedia({kind,b64,target_lang})   -> 后端媒体翻译原始响应
 *   smartReply({messages,platform,persona_id,target_lang}) -> {ok,reply,translated}
 *   ingest(payload)                          -> 回流一条消息到统一收件箱
 *   getConfig()                              -> 运行配置 {translate,sync,debug,...}
 *   getSelectorProfiles()                    -> {ok,profiles} 远程选择器覆写
 *   diag(msg)                                -> 诊断日志
 *   injectHealth(payload)                    -> 注入健康信标（后端看板）
 *   reportInjectStatus(payload)              -> 注入状态上报给宿主 UI（桌面 sendToHost）
 *   reportActiveChat(payload)                -> 当前会话上报给宿主右栏
 *   reportFillResult({token,ok,reason})      -> 受控出站「填入/发送」回执（可选；缺则宿主拿不到
 *                                               送达证据，只能乐观 ack —— 桌面壳必须实现）
 *   onSetPersona(cb)/onSetReplyLang(cb)/onSetAccount(cb)/onFillComposer(cb) -> 宿主下行事件
 *
 * deps：{ profiles, mediaFormat, translateScheduler?, bubbleModel? }（即同目录下 profiles.js /
 *   media-format.js / translate-scheduler.js / bubble-model.js 的导出）。后两个是可选依赖：
 *   缺 translateScheduler → 逐条翻译；缺 bubbleModel → 单块译文旧渲染。两者都零回归降级，
 *   浏览器扩展包可以只带前两个。
 */

// ── P0 性能：突变批分析（纯函数，Node 可测）────────────────────────────────────
// 载入慢的三个根因之一是「自触发循环」：我们往气泡里 append 翻译按钮/译文盒 → 触发
// MutationObserver → 全量重扫 → 又 append……官方页流式载入 50 条消息时，实测会放大成
// 上百轮全量扫描。本函数把一批突变记录判成 {relevant, dirty}：
//   · 全部源于我们注入的节点（.aitr-*）→ 不相关，直接不扫（掐断自触发）；
//   · composer 打字这类「气泡外 characterData」→ 不相关（否则每个键击都排一轮扫描）；
//   · 落在某个气泡子树内的变化 → 该气泡进 dirty（取文缓存失效 + 陈旧比对只做这几条）。
// DOM 依赖全部经 adapters 注入（isOurs/bubbleOf），故可用普通对象在 Node 里单测。
function analyzeMutations(records, adapters) {
  const a = adapters || {};
  const isOurs = typeof a.isOurs === "function" ? a.isOurs : function () { return false; };
  const bubbleOf = typeof a.bubbleOf === "function" ? a.bubbleOf : function () { return null; };
  let relevant = false;
  const dirty = new Set();
  const list = records || [];
  for (let i = 0; i < list.length; i++) {
    const r = list[i];
    if (!r) continue;
    const target = r.target || null;
    if (isOurs(target)) continue; // 变化发生在我们注入的节点内部
    if (r.type === "childList") {
      const added = r.addedNodes ? Array.prototype.slice.call(r.addedNodes) : [];
      const removed = r.removedNodes ? Array.prototype.slice.call(r.removedNodes) : [];
      // 只是我们自己在挂/摘注入控件（target 是官方节点、增删的却全是 .aitr-*）
      if ((added.length || removed.length)
          && added.every(isOurs) && removed.every(isOurs)) continue;
    }
    const b = bubbleOf(target);
    if (r.type === "characterData") {
      // 文本节点原地改写：只有发生在气泡内才关我们的事（消息被编辑/虚拟列表复用改文）；
      // 气泡外（composer 打字、计时器跳动）一律忽略——那是键击级噪音源。
      if (!b) continue;
      relevant = true;
      dirty.add(b);
      continue;
    }
    relevant = true;
    if (b) dirty.add(b);
  }
  return { relevant: relevant, dirty: dirty };
}

function createInject(host, deps) {
  host = host || {};
  deps = deps || {};
  const profiles = deps.profiles || {};
  const mediaFormat = deps.mediaFormat || null;
  const formatMediaResult = mediaFormat && mediaFormat.formatMediaResult;

  const detectPlatform = profiles.detectPlatform || function () { return "unknown"; };
  const BUILTIN_PROFILES = profiles.BUILTIN_PROFILES || {};
  const applySelectorOverlay = profiles.applySelectorOverlay || function (p) { return p; };
  const selectorHealth = profiles.selectorHealth || null;
  // 翻译调度器（可选依赖）：桌面 preload 注入 shared/inject/translate-scheduler.js;
  // 浏览器扩展未注入时 scheduler 保持 null → 自动回落逐条 translate（行为不变）。
  const TranslateScheduler = deps.translateScheduler
    || (typeof globalThis !== "undefined" ? globalThis.ATranslateScheduler : null);
  // 双语气泡渲染模型（可选依赖）：缺失时回落成「一块译文 div」的旧渲染,行为不变。
  const BubbleModel = deps.bubbleModel
    || (typeof globalThis !== "undefined" ? globalThis.ABubbleModel : null);

  // host 方法安全包装：缺失能力 → no-op / 安全默认，绝不抛错中断扫描循环。
  function call(name, args, fallback) {
    const fn = host[name];
    if (typeof fn !== "function") return Promise.resolve(fallback);
    try {
      return Promise.resolve(fn(args));
    } catch (e) {
      return Promise.resolve(fallback);
    }
  }
  function fire(name, payload) {
    const fn = host[name];
    if (typeof fn !== "function") return;
    try { fn(payload); } catch (e) { /* 宿主缺失/异常：忽略 */ }
  }
  function on(name, cb) {
    const fn = host[name];
    if (typeof fn === "function") {
      try { fn(cb); } catch (e) { /* ignore */ }
    }
  }
  function diag(msg) { fire("diag", msg); }

  const PLATFORM = detectPlatform();
  let PROFILE = BUILTIN_PROFILES[PLATFORM] || { platform: PLATFORM, supported: false };

  // 远程选择器覆写：非阻塞，成功则就地热更新 PROFILE，失败静默用内置档。
  (async function bootstrapOverlay() {
    if (!BUILTIN_PROFILES[PLATFORM]) return;
    try {
      const res = await call("getSelectorProfiles", undefined, null);
      const remote = res && res.ok && res.profiles;
      if (remote && remote[PLATFORM]) {
        PROFILE = applySelectorOverlay(BUILTIN_PROFILES[PLATFORM], remote[PLATFORM]);
        diag(`[selector-overlay:${PLATFORM}] applied`);
      }
    } catch (e) { /* 静默用内置档 */ }
  })();

  const PUSHED = new Set();
  let CONFIG = { translate: { target_lang: "zh", auto: false } };
  let CURRENT_PERSONA = "";
  let REPLY_LANG = "";
  let ACCOUNT_ID = "";
  let scheduler = null; // 翻译调度器实例（start() 里按能力构建;null=逐条回落）

  const PROCESSED = "data-aitr";
  const SRC_FP = "data-aitr-src"; // 出过译文时记下当时原文的指纹（陈旧检测判据）

  // ── P0 性能：取文缓存 + 脏气泡集 ─────────────────────────────────────────────
  // Telegram 档的 text() 每次调用都 cloneNode(true) 整个气泡子树再删注入物取文——
  // refreshStale 曾对每条已译气泡每轮都取文比指纹，载入期＝数千次子树克隆（主因之二）。
  // 缓存以气泡节点为键（WeakMap，节点卸载即自动回收）；失效只有一个入口：观察器判定
  // 该气泡子树真的变了（analyzeMutations 的 dirty）→ 删缓存 + 标脏。我们自己往气泡里
  // append .aitr-* 不会走到失效（被判 ours），而 PROFILE.text 本来就剥 .aitr-*，缓存值仍正确。
  const TEXT_CACHE = typeof WeakMap !== "undefined" ? new WeakMap() : null;
  const DIRTY = typeof WeakSet !== "undefined" ? new WeakSet() : null;
  const OUR_SELECTOR = ".aitr-box,.aitr-btn,#aitr-smart,#aitr-style";

  function _elOf(n) {
    if (!n) return null;
    return n.nodeType === 1 ? n : (n.parentElement || null);
  }
  function _isOurNode(n) {
    const el = _elOf(n);
    if (!el || !el.closest) return false;
    try { return !!el.closest(OUR_SELECTOR); } catch (e) { return false; }
  }
  function _bubbleOf(n) {
    const el = _elOf(n);
    if (!el || !el.closest || !PROFILE.bubble) return null;
    try { return el.closest(PROFILE.bubble); } catch (e) { return null; }
  }

  function targetLang() {
    return (CONFIG.translate && CONFIG.translate.target_lang) || "zh";
  }

  function bubbleVisibleText(bubble) {
    if (TEXT_CACHE) {
      const hit = TEXT_CACHE.get(bubble);
      if (hit !== undefined) return hit;
    }
    let t = "";
    try { t = PROFILE.text ? PROFILE.text(bubble) : ""; } catch (e) { t = ""; }
    if (TEXT_CACHE) TEXT_CACHE.set(bubble, t);
    return t;
  }

  // ── 样式：优先注入一张样式表（明暗 token 集中在 bubble-model）；被 CSP 拦下则回落内联 ──
  // 官方页普遍带 CSP。appendChild 一个 <style> 被拦时**不会抛错**，样式只是静默不生效
  // （坐席看到无样式裸文本）→ 用 `el.sheet.cssRules` 有没有真的解析出规则来判定,
  // 拦下就给每个元素补内联样式（内联属性与 <style> 同受 style-src 'unsafe-inline' 管，
  // 但历史实现一直用内联且工作正常，故内联是可靠回落）。
  // 三态：null=未定论（可重试）;true=样式表生效;false=永久回落内联。
  // 「head 还没解析出来」（document-start 注入）必须留在未定论,否则一次过早探测就把整个
  // 会话钉死在内联样式上（丢 hover / 暗色 / 状态色）。
  let _styleOk = null;
  function ensureStyle() {
    if (_styleOk !== null) return _styleOk;
    try {
      if (typeof document === "undefined") { _styleOk = false; return false; }
      if (!document.head) return false; // 不定论,下次再探
      if (!BubbleModel || !BubbleModel.bubbleStyleCss) { _styleOk = false; return false; }
      let el = document.getElementById("aitr-style");
      if (!el) {
        el = document.createElement("style");
        el.id = "aitr-style";
        el.textContent = BubbleModel.bubbleStyleCss();
        document.head.appendChild(el);
      }
      _styleOk = !!(el.sheet && el.sheet.cssRules && el.sheet.cssRules.length);
    } catch (e) { _styleOk = false; }
    return _styleOk === true;
  }

  // CSP 回落内联样式（样式表被拦时才用）：与 bubble-model 的原生化样式同口径——
  // ghost 文字链按钮 + 发丝线分隔的透明译文块（继承气泡文字色），别再是外挂蓝框。
  const _BTN_INLINE =
    "display:inline-block;margin-top:4px;font-size:11px;cursor:pointer;" +
    "color:#3aa0ff;opacity:.7;user-select:none;background:none;border:0;padding:0;";
  const _BOX_INLINE =
    "margin-top:4px;padding-top:4px;font-size:13px;line-height:1.45;white-space:pre-wrap;" +
    "border-top:1px solid rgba(128,128,128,.28);color:inherit;";

  function makeBtn(label) {
    const b = document.createElement("span");
    b.className = "aitr-btn";
    b.textContent = label;
    // 内联样式优先级高于样式表 → 样式表生效时**不能**再写内联,否则 hover/忙态/暗色全被压死。
    if (!ensureStyle()) b.style.cssText = _BTN_INLINE;
    return b;
  }

  function bubbleDirection(bubble) {
    try { return PROFILE.isOut && PROFILE.isOut(bubble) ? "out" : "in"; } catch (e) { return "in"; }
  }

  function ensureBox(bubble) {
    let box = bubble.querySelector(".aitr-box");
    if (!box) {
      box = document.createElement("div");
      box.className = "aitr-box";
      bubble.appendChild(box);
    }
    if (!ensureStyle()) box.style.cssText = _BOX_INLINE;
    return box;
  }

  function _appendLine(box, cls, text) {
    const el = document.createElement("span");
    el.className = cls;
    el.textContent = text; // 原文/译文是不可信内容,只走 textContent —— 拼 innerHTML 等于开 XSS
    box.appendChild(el);
  }

  // 渲染唯一入口：状态 → bubbleRenderModel → DOM。渲染层零业务判断,状态语义全在模型里。
  function renderBubbleState(bubble, st) {
    const model = BubbleModel && BubbleModel.bubbleRenderModel
      ? BubbleModel.bubbleRenderModel({
        origText: st.origText,
        translated: st.translated,
        state: st.state,
        direction: st.direction || bubbleDirection(bubble),
      })
      : null;
    if (!model) { // 模型缺失（未注入依赖的旧扩展包）：退回单块译文
      if (st.translated) renderTranslation(bubble, st.translated);
      return;
    }
    if (!model.show) {
      const old = bubble.querySelector(".aitr-box");
      if (old && old.parentNode) old.parentNode.removeChild(old);
      return;
    }
    const box = ensureBox(bubble);
    box.className = "aitr-box " + model.cls;
    while (box.firstChild) box.removeChild(box.firstChild);
    model.lines.forEach((ln) => {
      _appendLine(box, "aitr-line " + (ln.role === "orig" ? "aitr-orig" : "aitr-trans"), ln.text);
    });
    if (model.badge) _appendLine(box, "aitr-badge", model.badge);
  }

  // 已渲染的译文（重译时先留着弱化展示 / 陈旧时保留旧译）。
  function existingTranslation(bubble) {
    const box = bubble.querySelector(".aitr-box");
    if (!box) return "";
    try {
      return Array.from(box.querySelectorAll(".aitr-trans")).map((n) => n.textContent).join("\n");
    } catch (e) { return ""; }
  }

  // 媒体译文（OCR/转写）仍是一整块文本：走同一个盒子拿到同一套明暗主题,不进状态机。
  function renderTranslation(bubble, text) {
    const box = ensureBox(bubble);
    box.className = "aitr-box aitr-done";
    while (box.firstChild) box.removeChild(box.firstChild);
    _appendLine(box, "aitr-line aitr-trans", text);
  }

  // 媒体译文（OCR/转写）＝单块文本,不进双语状态机;但**复用同一套盒子状态样式**
  // （aitr-loading/aitr-error/aitr-done）,让它与文本翻译视觉一致——此前媒体的「加载中/
  // 失败」只落在按钮文字上,没有盒子、没有错误色,与文本翻译两套观感。
  function renderMediaBox(bubble, state, opts) {
    const o = opts || {};
    const box = ensureBox(bubble);
    box.className = "aitr-box aitr-" + state;
    while (box.firstChild) box.removeChild(box.firstChild);
    if (o.original) _appendLine(box, "aitr-line aitr-orig", o.original);
    if (o.translated) _appendLine(box, "aitr-line aitr-trans", o.translated);
    if (o.badge) _appendLine(box, "aitr-badge", o.badge);
  }

  function bubbleMedia(bubble) {
    try { return PROFILE.media ? PROFILE.media(bubble) : null; } catch (e) { return null; }
  }

  async function mediaToBase64(url) {
    const resp = await fetch(url);
    const blob = await resp.blob();
    return await new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || "").split(",").pop() || "");
      fr.onerror = reject;
      fr.readAsDataURL(blob);
    });
  }

  async function translateMediaBubble(bubble, btn, m) {
    // 连点防护：一次识别/转写在途不重复触发——媒体路径此前没有,点两下"翻译图片"＝烧两次
    // 后端 OCR（与文本按钮忙态、智能回复浮钮同一类,唯独这里漏了）。
    if (btn.getAttribute("data-aitr-busy")) return;
    btn.setAttribute("data-aitr-busy", "1");
    const label = m.kind === "image" ? "识别中" : "转写中";
    btn.textContent = label + "…";
    renderMediaBox(bubble, "loading", { badge: label + "…" }); // 加载进盒子（与文本一致）
    try {
      const b64 = await mediaToBase64(m.url);
      if (!b64) {
        renderMediaBox(bubble, "error", { badge: "读取媒体失败" });
        btn.textContent = "重试"; return;
      }
      const res = await call("translateMedia", { kind: m.kind, b64, target_lang: targetLang() }, null);
      const f = formatMediaResult(m.kind, res);
      if (!f.ok) {
        // 失败进错误盒子（错误色 + 徽标）,不再只把错误藏在按钮文字里
        renderMediaBox(bubble, "error", { badge: f.note || "翻译失败" });
        btn.textContent = "重试"; return;
      }
      renderMediaBox(bubble, "done", {
        original: f.original ? "[" + f.label + "原文] " + f.original : "",
        translated: f.translated || "",
      });
      btn.textContent = m.kind === "image" ? "重新识别" : "重新转写";
    } catch (e) {
      renderMediaBox(bubble, "error", { badge: "读取媒体失败" });
      btn.textContent = "重试";
    } finally {
      btn.removeAttribute("data-aitr-busy");
    }
  }

  function _aitrNorm(s) { return String(s == null ? "" : s).replace(/\s+/g, "").toLowerCase(); }
  function aitrMeaningful(orig, xl) { return !!xl && _aitrNorm(orig) !== _aitrNorm(xl); }

  function setSrcFingerprint(bubble, text) {
    try {
      bubble.setAttribute(SRC_FP, BubbleModel && BubbleModel.srcFingerprint
        ? BubbleModel.srcFingerprint(text) : _aitrNorm(text));
    } catch (e) { /* 节点已卸载：陈旧检测降级为不检测,不影响译文本身 */ }
  }

  // opts.immediate=false → 只入队,靠调度器去抖窗合批（自动全局翻译档,一次扫描多气泡合成一批）；
  // 缺省（点击单条）→ 入队后立即 flush,不让坐席等满去抖窗。scheduler 为 null 时回落逐条 translate。
  async function translateBubble(bubble, btn, opts) {
    const text = bubbleVisibleText(bubble);
    if (!text) return;
    // 忙态闸门：坐席连点、或「陈旧自动重译」与手动点击撞车时,不重复烧一次后端往返。
    if (btn.getAttribute("data-aitr-busy")) return;
    const idle = btn.textContent;
    btn.setAttribute("data-aitr-busy", "1");
    btn.textContent = "翻译中…";
    const dir = bubbleDirection(bubble);
    const immediate = !opts || opts.immediate !== false;
    // 自动档（批量）**不铺 loading 框**：首屏几十条同时挂"翻译中"虚线框是纯视觉噪音,
    // 还要白付两轮 DOM 重建（建框 → 换译文）。手动点击是单条,即时反馈才有价值。
    if (immediate) {
      renderBubbleState(bubble, {
        origText: text, translated: existingTranslation(bubble), state: "loading", direction: dir,
      });
    }
    try {
      let res;
      if (scheduler) {
        const p = scheduler.request(text);
        if (immediate) scheduler.flush();
        res = await p;
      } else {
        const r = await call("translate", { text, target_lang: targetLang() }, null);
        res = r && r.ok && r.text
          ? { ok: true, translated: r.text, meaningful: aitrMeaningful(text, r.text) }
          : { ok: false };
      }
      const state = BubbleModel && BubbleModel.stateFromResult
        ? BubbleModel.stateFromResult(res)
        : (res && res.ok ? (res.translated && res.meaningful ? "done" : "same") : "error");
      renderBubbleState(bubble, {
        origText: text, translated: (res && res.translated) || "", state, direction: dir,
      });
      // 指纹只在「已按当前原文出过结论」时落（done/same）。error/budget 不落：否则一次失败
      // 会被固化——原文没变永远不再译,原文真变了也判不出来。
      if (state === "done" || state === "same") setSrcFingerprint(bubble, text);
      btn.textContent = state === "done" ? "重新翻译" : (state === "error" ? "重试" : idle);
    } finally {
      btn.removeAttribute("data-aitr-busy");
    }
  }

  // 已装饰过的气泡：对方编辑了消息、或虚拟列表复用同一 DOM 节点换了内容时,旧译文会静默
  // 张冠李戴（比「没译」更糟）。P0 起不再「每轮全体比对」——只比**观察器判定真变过**的
  // 气泡（DIRTY），10s 自愈全量轮兜底（full=true 时不看脏标记,防脏信号丢失后永不比对）。
  function refreshStale(bubble, full) {
    if (!BubbleModel || !BubbleModel.isStale) return;
    // 脏标记闸门：子树没变过（且非自愈全量轮）→ 指纹必然没变，别白克隆一次取文。
    // 标记的**消费**刻意放在 busy 短路之后——「翻译在途时消息被编辑」这个竞态里，
    // 此刻消费掉脏标记会让陈旧检测只能等 10s 自愈轮；留着它，翻译一落地下一轮就补判。
    if (DIRTY && !full && !DIRTY.has(bubble)) return;
    let fp = "";
    try { fp = bubble.getAttribute(SRC_FP) || ""; } catch (e) { return; }
    if (!fp) return;
    const box = bubble.querySelector(".aitr-box");
    if (!box) return;
    // 便宜的判据全部前置,再碰贵的取文+指纹（长会话里每轮几百条已译气泡都要过这里）：
    //   已标陈旧 → 结论没变,别每轮重排 DOM；
    //   翻译在途 → 指纹还没落,此刻必判"变了",标了又马上被 done 覆盖＝白闪一次。
    if (box.className.indexOf("aitr-stale") >= 0) return;
    const btn = bubble.querySelector(".aitr-btn");
    if (btn && btn.getAttribute("data-aitr-busy")) return;
    if (DIRTY) DIRTY.delete(bubble); // 真要比对了才消费脏标记（busy/已标陈旧不消费）
    const cur = bubbleVisibleText(bubble);
    if (!BubbleModel.isStale(fp, cur)) return;
    renderBubbleState(bubble, {
      origText: cur,
      translated: existingTranslation(bubble),
      state: "stale",
      direction: bubbleDirection(bubble),
    });
    if (btn) btn.textContent = "重译";
    // 自动全局翻译档:陈旧就该自己追上去。重译成功即更新指纹 → 天然收敛不会每轮重烧;
    // 原文来回抖动的极端情况也有调度器缓存兜着（第二次起零成本）。
    if (CONFIG.translate && CONFIG.translate.auto && btn) {
      translateBubble(bubble, btn, { immediate: false });
    }
  }

  function appendInjectControl(bubble, el) {
    const anchor =
      bubble.querySelector(".copyable-text, .copyable-area") ||
      bubble.querySelector("[data-testid='msg-container']") ||
      bubble;
    anchor.appendChild(el);
  }

  // ── 提取器健康计数（每轮 scanAll 重置）───────────────────────────────────────
  // 官方改版最常见的形态不是「气泡容器消失」,而是**内层结构微调**:PROFILE.bubble 照样命中,
  // 但 bubbleText / mid / peerId 提取全空 → 翻译按钮一个都不出现、消息一条都不回流,
  // 而只数元素存在性的健康灯全程绿着。这里在**已有的遍历里顺手计数**（零额外 DOM 开销）,
  // 只上报事实,不在前端下判定——分类仍由后端 classify_inject_health 单点决定（避免双源真相）。
  let _ex = { decorated: 0, unresolved: 0, ingestTried: 0, ingestKeyed: 0 };
  function _exReset() {
    _ex = { decorated: 0, unresolved: 0, ingestTried: 0, ingestKeyed: 0 };
  }

  function decorateBubble(bubble) {
    if (bubble.getAttribute(PROCESSED)) { _ex.decorated++; return; } // 曾成功提取过
    const text = bubbleVisibleText(bubble);
    if (text) {
      _ex.decorated++;
      bubble.setAttribute(PROCESSED, "1");
      const btn = makeBtn("点击翻译");
      btn.addEventListener("click", (e) => { e.stopPropagation(); translateBubble(bubble, btn); });
      appendInjectControl(bubble, btn);
      // 自动全局翻译:走批处理档（immediate:false）,一次扫描的多条气泡合并成一次后端往返。
      if (CONFIG.translate && CONFIG.translate.auto) translateBubble(bubble, btn, { immediate: false });
      return;
    }
    // 媒体探测先于 formatMediaResult 判空:认出「这是张图/段语音」就说明提取器活着,
    // 缺格式化器只是没有按钮可加（扩展环境可能不注入 mediaFormat）,不该算成提取失效。
    const m = bubbleMedia(bubble);
    if (m) {
      _ex.decorated++;
      if (!formatMediaResult) return;
      bubble.setAttribute(PROCESSED, "1");
      const btn = makeBtn(m.kind === "image" ? "🖼 翻译图片" : "🎤 翻译语音");
      btn.addEventListener("click", (e) => { e.stopPropagation(); translateMediaBubble(bubble, btn, m); });
      appendInjectControl(bubble, btn);
      return;
    }
    _ex.unresolved++; // 既提不出文本、也认不出媒体 = 这一条彻底没抓住
  }

  function currentPeerName() {
    try { return PROFILE.peerName ? PROFILE.peerName() : ""; } catch (e) { return ""; }
  }
  function bubbleMid(bubble) {
    try { return PROFILE.mid ? PROFILE.mid(bubble) : ""; } catch (e) { return ""; }
  }
  function bubblePeerId(bubble) {
    try { return PROFILE.peerId ? PROFILE.peerId(bubble) : ""; } catch (e) { return ""; }
  }
  // 气泡真实时间（P0 时间推理）：档案无 ts 提取器/解析不确定 → 0（宁缺勿错，
  // 后端对无 ts 行走结构判定，绝不猜时间）。
  function bubbleTs(bubble) {
    try { return (PROFILE.ts && PROFILE.ts(bubble)) || 0; } catch (e) { return 0; }
  }

  let _diagAt = 0;
  function ingestDiag(bubbles) {
    const now = Date.now();
    if (now - _diagAt < 5000) return;
    _diagAt = now;
    let withMid = 0;
    let withPeer = 0;
    bubbles.forEach((b) => {
      if (bubbleMid(b)) withMid++;
      if (bubblePeerId(b)) withPeer++;
    });
    diag(
      `[sync:${PLATFORM}] bubbles=${bubbles.length} mid=${withMid} peer=${withPeer} hash=${(
        (typeof location !== "undefined" && location.hash) || ""
      ).slice(0, 24)} pushed=${PUSHED.size}`
    );
  }

  function ingestBubble(bubble) {
    if (!(CONFIG.sync && CONFIG.sync.enabled)) return;
    if (!PROFILE.canIngest) return;
    const mid = bubbleMid(bubble);
    const peerId = bubblePeerId(bubble);
    // 计数口径:分母只含「本轮真正要推的」——已在 PUSHED 里的不计,否则长会话稳定态下
    // tried 会恒等于 keyed=0（全推过了）,把「一切正常」误判成「键提取全废」。
    if (!mid || !peerId) { _ex.ingestTried++; return; }
    const key = peerId + ":" + mid;
    if (PUSHED.has(key)) return;
    _ex.ingestTried++;
    _ex.ingestKeyed++;
    const text = bubbleVisibleText(bubble);
    if (!text) return;
    PUSHED.add(key);
    fire("ingest", {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      chat_key: String(peerId),
      name: currentPeerName(),
      text,
      direction: PROFILE.isOut(bubble) ? "out" : "in",
      msg_id: String(mid),
      // 优先气泡真实时间——旧实现恒用抓取时刻，历史消息回流后在库里被抹平成
      // 「刚刚」（实录：8/8 的消息落库成 8/12 21:15），时间推理/回访判定全失真。
      ts: bubbleTs(bubble) || Math.floor(Date.now() / 1000),
    });
  }

  function isContentBubble(b) {
    try { return PROFILE.isContent ? PROFILE.isContent(b) : !!b; } catch (e) { return false; }
  }

  function scanAll(opts) {
    const full = !!(opts && opts.full); // full=自愈全量轮：陈旧比对不看脏标记
    let bubbles = Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble);
    if (PLATFORM === "whatsapp") {
      const roots = bubbles.filter(
        (b) => b.classList && (b.classList.contains("message-in") || b.classList.contains("message-out"))
      );
      if (roots.length) bubbles = roots;
    }
    _exReset();
    bubbles.forEach((b) => { decorateBubble(b); refreshStale(b, full); ingestBubble(b); });
    if (CONFIG.sync && CONFIG.sync.enabled && bubbles.length) ingestDiag(bubbles);
    maybeReportActiveChat(bubbles);
    reportInjectStatus(bubbles);
    mountSmartReplyButton(); // 幂等且首查 getElementById 极廉价；折掉原 3s 专用轮询
  }

  function findComposer() {
    try { return PROFILE.composer ? document.querySelector(PROFILE.composer) : null; } catch (e) { return null; }
  }
  let _statusAt = 0;
  let _lastStatusSig = "";
  let _healthBeaconAt = 0;
  const _HEALTH_HEARTBEAT_MS = 30000;
  function reportInjectStatus(bubbles) {
    const now = Date.now();
    if (now - _statusAt < 2500) return;
    _statusAt = now;
    let count = 0;
    try {
      count = (bubbles || Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble)).length;
    } catch (e) { count = 0; }
    const composer = !!findComposer();
    const peer = currentPeerName();
    const chatOpen = !!peer || count > 0;
    let selectors = { bubble: count > 0, composer, sendBtn: false, peerTitle: !!peer };
    try {
      if (selectorHealth) selectors = selectorHealth(PROFILE);
    } catch (e) { /* 探针异常：用粗粒度兜底 */ }
    const payload = {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      supported: !!PROFILE.supported,
      generic: !!PROFILE.generic,
      can_ingest: !!PROFILE.canIngest,
      composer,
      bubbles: count,
      chatOpen,
      selectors,
      // 提取器健康事实（不含判定）:后端据此区分「元素在但抓不出内容」这类静默失效。
      extract: {
        decorated: _ex.decorated,
        unresolved: _ex.unresolved,
        ingestTried: _ex.ingestTried,
        ingestKeyed: _ex.ingestKeyed,
      },
    };
    // 上报节流的触发指纹:只由**事实位**组成（刻意不放「是否失效」的判断,那是后端的活）,
    // 这样提取器全绿→全废的跃迁能即时上报,而不用等 30s 心跳。
    const sig = [
      composer ? 1 : 0, count > 0 ? 1 : 0, chatOpen ? 1 : 0,
      _ex.decorated > 0 ? 1 : 0, _ex.unresolved > 0 ? 1 : 0,
      _ex.ingestTried > 0 ? 1 : 0, _ex.ingestKeyed > 0 ? 1 : 0,
    ].join("|");
    const changed = sig !== _lastStatusSig;
    if (changed) {
      _lastStatusSig = sig;
      fire("reportInjectStatus", payload);
    }
    if (changed || now - _healthBeaconAt >= _HEALTH_HEARTBEAT_MS) {
      _healthBeaconAt = now;
      fire("injectHealth", payload);
    }
  }

  let _lastChatPeer = "";
  let _lastTopMid = "";
  let _lastReportAt = 0;
  function maybeReportActiveChat(bubbles) {
    let peer = "";
    let topMid = "";
    for (let i = bubbles.length - 1; i >= 0; i--) {
      if (!peer) peer = bubblePeerId(bubbles[i]);
      if (!topMid) topMid = bubbleMid(bubbles[i]);
      if (peer && topMid) break;
    }
    if (!peer && PROFILE.conversationPeerId) {
      try { peer = PROFILE.conversationPeerId(); } catch (e) { /* ignore */ }
    }
    if (!peer) return;
    const now = Date.now();
    const switched = peer !== _lastChatPeer;
    const grew = topMid && topMid !== _lastTopMid && now - _lastReportAt > 2000;
    if (!switched && !grew) return;
    _lastChatPeer = peer;
    _lastTopMid = topMid;
    _lastReportAt = now;
    fire("reportActiveChat", {
      platform: PLATFORM,
      account_id: ACCOUNT_ID,
      chat_key: String(peer),
      name: currentPeerName(),
      switched,
      messages: collectRecentMessages(12),
    });
    diag(`[panel:${PLATFORM}] active-chat → ${peer} (${currentPeerName()})`);
  }

  function collectRecentMessages(limit) {
    const out = [];
    const bubbles = Array.from(document.querySelectorAll(PROFILE.bubble))
      .filter(isContentBubble)
      .slice(-(limit || 12));
    for (const b of bubbles) {
      const text = bubbleVisibleText(b);
      if (!text) continue;
      const row = { direction: PROFILE.isOut(b) ? "out" : "in", text };
      // 带上气泡真实时间（抓不到不带=旧行为）：后端时间推理靠它区分
      // 「刚收到的消息」和「四天前已答过的旧消息」。
      const ts = bubbleTs(b);
      if (ts > 0) row.ts = ts;
      out.push(row);
    }
    return out;
  }

  // 当前会话 peer（智能回复浮钮用）：与 maybeReportActiveChat 同一套判定，
  // 点击时现算（活跃聊天可能刚切换，_lastChatPeer 只作兜底）。
  function currentPeerKey() {
    try {
      const bs = Array.from(document.querySelectorAll(PROFILE.bubble)).filter(isContentBubble);
      for (let i = bs.length - 1; i >= 0; i--) {
        const p = bubblePeerId(bs[i]);
        if (p) return String(p);
      }
      if (PROFILE.conversationPeerId) {
        const p2 = PROFILE.conversationPeerId();
        if (p2) return String(p2);
      }
    } catch (e) { /* 判定失败走兜底 */ }
    return _lastChatPeer || "";
  }

  function fillComposer(text) {
    const el = document.querySelector(PROFILE.composer);
    if (!el) return false;
    el.focus();
    if (PROFILE.richInput) {
      try {
        document.execCommand("selectAll", false, null);
        if (document.execCommand("insertText", false, text)) {
          el.dispatchEvent(new InputEvent("input", { bubbles: true }));
          return true;
        }
      } catch (e) { /* 回落到 textContent */ }
    }
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true }));
    return true;
  }

  function sendComposer() {
    const btn = document.querySelector(PROFILE.sendBtn);
    if (btn) {
      (btn.closest("button") || btn).click();
      return true;
    }
    const el = document.querySelector(PROFILE.composer);
    if (!el) return false;
    el.focus();
    ["keydown", "keypress", "keyup"].forEach((type) =>
      el.dispatchEvent(
        new KeyboardEvent(type, {
          key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true,
        })
      )
    );
    return true;
  }

  // composer 当前文本（用于「发出去了没」的回读判定）
  function composerText() {
    const el = document.querySelector(PROFILE.composer);
    if (!el) return null;
    const v = typeof el.value === "string" ? el.value : el.textContent;
    return String(v || "").trim();
  }

  /** 点发送 → 回读 composer 是否被清空，得出「真的发出去了吗」。
   *
   * 为什么必须回读：宿主此前把 `webview.send("fill-composer")` 之后**无条件**当成
   * 发送成功回执给后端，于是选择器失配/注入整体没装载时，每条受控出站都被记成「已发送」
   * 而客户什么也没收到（1.016/1.017 的 shared/inject 漏包让这不是假设而是已发生）。
   * 官方页把消息真正提交后一定会清空输入框，故「清空」是本地可得的最强送达证据。
   *
   * 三态刻意不对称（与 messenger-web 的既定口径一致：**不定态按已送达**）：
   *   · 清空了            → sent（可 ack 成功）
   *   · 控件压根不在      → 明确没发出（ack 失败，交人审 + 由注入健康遥测告警）
   *   · 点了但没清空      → 不定态 → 仍报 sent，只记 note——重发一条客户会收到两遍，
   *                        比「漏一条」更伤（漏的那条服务端另有积压告警兜住）。
   */
  function sendAndConfirm(cb) {
    const before = composerText();
    if (before === null) { cb({ ok: false, reason: "composer_missing" }); return; }
    if (!sendComposer()) { cb({ ok: false, reason: "send_control_missing" }); return; }
    const deadline = Date.now() + 1500;
    (function poll() {
      const now = composerText();
      if (now === null || now === "" || now !== before) { cb({ ok: true, reason: "" }); return; }
      if (Date.now() >= deadline) { cb({ ok: true, reason: "composer_not_cleared" }); return; }
      setTimeout(poll, 120);
    })();
  }

  // 智能回复浮钮：安静的主题化 ghost chip（样式在 bubble-model 的 #aitr-smart 规则,CSP 被拦
  // 回落 _FAB_INLINE）。刻意去掉 🤖 ——既是「外挂感」大头,也会向坐席暴露「这是机器人」；
  // 换成 SVG spark 图标（currentColor 自动跟主题色）。图标+文字拆成子节点,忙态只换文字span
  // 不动图标；连点防护（data-aitr-busy）防重复烧 LLM/重复填入。
  const _FAB_INLINE =
    "position:fixed;right:16px;bottom:88px;z-index:99999;display:inline-flex;" +
    "align-items:center;gap:5px;padding:6px 12px;border-radius:16px;font-size:12px;" +
    "cursor:pointer;user-select:none;color:#1c7ed6;background:rgba(255,255,255,.92);" +
    "border:1px solid rgba(28,126,214,.35);box-shadow:0 1px 3px rgba(0,0,0,.14);";
  const _SVG_NS = "http://www.w3.org/2000/svg";

  function _fabSparkIcon() {
    const svg = document.createElementNS(_SVG_NS, "svg");
    svg.setAttribute("class", "aitr-fab-ic");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "13");   // 显式尺寸:CSP 回落无样式表时也不至于撑成默认 300×150
    svg.setAttribute("height", "13");
    svg.setAttribute("fill", "currentColor");
    svg.setAttribute("aria-hidden", "true");
    const p = document.createElementNS(_SVG_NS, "path");
    p.setAttribute("d", "M12 2l1.8 5.2L19 9l-5.2 1.8L12 16l-1.8-5.2L5 9l5.2-1.8L12 2z");
    svg.appendChild(p);
    return svg;
  }

  function mountSmartReplyButton() {
    if (!document.body || document.getElementById("aitr-smart")) return;
    const fab = document.createElement("div");
    fab.id = "aitr-smart";
    if (!ensureStyle()) fab.style.cssText = _FAB_INLINE;
    fab.appendChild(_fabSparkIcon());
    const tx = document.createElement("span");
    tx.className = "aitr-fab-tx";
    tx.textContent = "智能回复";
    fab.appendChild(tx);
    fab.addEventListener("click", async () => {
      if (fab.getAttribute("data-aitr-busy")) return; // 连点防护:一次生成在途不重复触发
      const old = tx.textContent;
      fab.setAttribute("data-aitr-busy", "1");
      tx.textContent = "生成中…";
      try {
        const messages = collectRecentMessages(12);
        // chat_key/account_id 必带（2026-08-12 实锤：不带 → 后端统一规则引擎
        // 因 chat_key 为空整体弃用、退化 direct 路径，时间推理/记忆/规则栈全丢，
        // 日志侧 conv=telegram:: 无法归因）。
        const res = await call("smartReply", {
          messages, platform: PLATFORM, persona_id: CURRENT_PERSONA, target_lang: REPLY_LANG,
          chat_key: currentPeerKey(), account_id: ACCOUNT_ID,
        }, null);
        if (res && res.ok && res.reply) fillComposer(res.translated || res.reply);
      } finally {
        fab.removeAttribute("data-aitr-busy");
        tx.textContent = old; // 只换文字,图标不动
      }
    });
    document.body.appendChild(fab);
  }

  async function selfTest() {
    try {
      diag(`inject loaded (${PLATFORM}); running self-test…`);
      const res = await call("translate", { text: "Hello, how can we cooperate?", target_lang: "zh" }, null);
      diag("self-test translate => " + JSON.stringify(res));
    } catch (e) {
      diag("self-test ERROR " + String(e));
    }
  }

  // 按能力构建翻译调度器:优先批处理 host（desktop:translate-batch）,缺失回落逐条 translate;
  // 命中缓存/inflight 去重/语言守卫（已是目标语跳过）/可选字符预算,让「自动全局翻译」省字符抗刷屏。
  function buildScheduler() {
    if (!(TranslateScheduler && typeof TranslateScheduler.createTranslateScheduler === "function")) return;
    const tc = CONFIG.translate || {};
    const budgetChars = Number(tc.budget_chars) > 0 ? Number(tc.budget_chars) : 0;
    scheduler = TranslateScheduler.createTranslateScheduler({
      translateBatch: async (texts, target) => {
        if (typeof host.translateBatch === "function") {
          const res = await call("translateBatch", { texts, target_lang: target }, null);
          if (res && Array.isArray(res.texts)) return res.texts;
          // 批处理 host 在但返回异常 → 落逐条兜底,不整批判失败
        }
        const rs = await Promise.all(
          texts.map((t) => call("translate", { text: t, target_lang: target }, null))
        );
        return rs.map((r) => (r && r.ok && r.text) ? r.text : "");
      },
      targetLang: () => targetLang(),
      cacheMax: 800,
      maxBatch: 24,
      autoFlushMs: 350,
      budget: budgetChars && typeof TranslateScheduler.createBudget === "function"
        ? TranslateScheduler.createBudget({ limit: budgetChars, windowMs: 60000 })
        : null,
    });
  }

  async function start() {
    try {
      const c = await call("getConfig", undefined, null);
      if (c) CONFIG = c;
    } catch (e) { /* 用默认 CONFIG */ }
    try { buildScheduler(); } catch (e) { scheduler = null; /* 调度器构建失败:逐条回落 */ }

    on("onSetPersona", (payload) => { CURRENT_PERSONA = (payload && payload.persona_id) || ""; });
    on("onSetReplyLang", (payload) => { REPLY_LANG = (payload && payload.target_lang) || ""; });
    on("onSetAccount", (payload) => { ACCOUNT_ID = (payload && payload.account_id) || ""; });

    if (!PROFILE.supported) {
      diag(`[inject] 平台「${PLATFORM}」暂无选择器档案，已跳过注入。`);
      return;
    }

    if (CONFIG.debug) selfTest();

    on("onFillComposer", (payload) => {
      const text = typeof payload === "string" ? payload : (payload && payload.text) || "";
      const send = typeof payload === "object" && payload && payload.send;
      // token 由宿主带下来：有 token 才回执（受控出站要凭它决定 ack 成功/失败/不 ack）。
      // 人工点「填入」不带 token，行为与旧版逐字节一致。
      const token = (typeof payload === "object" && payload && payload.token) || "";
      const done = (ok, reason) => {
        if (!token || typeof host.reportFillResult !== "function") return;
        try { host.reportFillResult({ token, ok: !!ok, reason: reason || "" }); } catch (e) { /* 非 webview 宿主 */ }
      };
      if (!text) { done(false, "empty_text"); return; }
      if (!fillComposer(String(text))) { done(false, "composer_missing"); return; }
      if (!send) { done(true, ""); return; }
      // 150ms：给富文本编辑器把 input 事件消费完（过早点发送会发出空消息）
      setTimeout(() => sendAndConfirm((r) => done(r.ok, r.reason)), 150);
    });

    // ── P0 性能：去抖合并 + 自体突变过滤（载入慢的主因之一）────────────────────
    // 旧实现＝回调直接 scanAll()：官方页流式插入一条消息就全量扫一轮，我们自己 append
    // 按钮/译文盒又反过来触发观察器 → 一次开会话放大成上百轮全量扫描。现在：
    //   ① 一批突变先过 analyzeMutations——全是我们自己的注入写入/气泡外键击噪音 → 不扫；
    //   ② 真变过的气泡标脏（取文缓存失效，陈旧比对只做这几条）；
    //   ③ 60ms 尾沿合并：突变风暴期间至多 ~16 轮/秒 → 1 轮/60ms，且每轮都因缓存变得极廉。
    // 刻意用 setTimeout 而非 rAF：隐藏账号的 webview 里 rAF 可能整体停摆（后台节流），
    // 消息回流/装饰会饿死到 10s 自愈轮；setTimeout 后台最多被钳到 ~1s，与旧 2s 轮询等价。
    let _scanPending = false;
    function scheduleScan() {
      if (_scanPending) return;
      _scanPending = true;
      setTimeout(function () { _scanPending = false; scanAll(); }, 60);
    }
    const obs = new MutationObserver(function (records) {
      const v = analyzeMutations(records, { isOurs: _isOurNode, bubbleOf: _bubbleOf });
      if (!v.relevant) return;
      v.dirty.forEach(function (b) {
        if (TEXT_CACHE) TEXT_CACHE.delete(b);
        if (DIRTY) DIRTY.add(b);
      });
      scheduleScan();
    });
    obs.observe(document.documentElement, {
      childList: true,
      subtree: true,
      attributes: true,
      // characterData：为取文缓存补的窄口——文本节点原地改写（消息被编辑）不发 childList，
      // 不订阅就会漏失效。analyzeMutations 已把气泡外的 characterData（composer 打字）全滤掉。
      characterData: true,
      attributeFilter: ["data-mid", "data-peer-id", "data-id"],
    });
    scanAll({ full: true });
    // 自愈全量轮：观察器覆盖不到的边角（脏信号丢失/极端 SPA 重挂）由它兜底。
    // 旧 2s 全量轮 + 3s 挂钮轮已废——增量路径接管后它们只剩纯浪费。
    setInterval(function () { scanAll({ full: true }); }, 10000);
  }

  function autostart() {
    if (typeof document === "undefined") return;
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", start);
    } else {
      start();
    }
  }

  return { start, autostart, get platform() { return PLATFORM; } };
}

// 双模式导出（单一源，桌面 preload 与浏览器扩展 content script 共用）。
// analyzeMutations 一并导出：纯函数，Node 单测直接喂假记录对象验证过滤/标脏语义。
(function (api) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.AInjectCore = api;
  }
})({ createInject, analyzeMutations });
