"use strict";
// 桌面壳 · 个人微信 PC 副驾托盘（2026-09-19）：轮询 /copilot/status，托盘提示 + 右键启停。
// 纯函数（labelFor / menuSpec / shouldAttach）零 Electron 依赖，门禁直跑；start() 才碰 Tray。
// 不改关窗退出语义——托盘只在壳还活着时存在，window-all-closed 仍会 quit。

const LABELS = {
  zh: {
    online: "副驾在线",
    blind: "副驾在线 · 窗口不可见",
    starting: "副驾启动中",
    offline: "副驾离线",
    idle: "副驾未启动",
    error: "副驾异常",
    unknown: "副驾状态未知",
    start: "启动副驾",
    stop: "停止副驾",
    focus: "还原微信",
    guide: "打开接入流程",
    show: "打开智聊",
    voice_ok: "语音就绪",
    voice_off: "语音未就绪 · 改发文字",
    voice_busy: "麦克风占用中 · 语音暂改文字",
  },
  en: {
    online: "Copilot online",
    blind: "Copilot online · window hidden",
    starting: "Copilot starting",
    offline: "Copilot offline",
    idle: "Copilot idle",
    error: "Copilot error",
    unknown: "Copilot status unknown",
    start: "Start copilot",
    stop: "Stop copilot",
    focus: "Restore WeChat",
    guide: "Open setup guide",
    show: "Show ChatX",
    voice_ok: "voice ready",
    voice_off: "voice not ready · text fallback",
    voice_busy: "mic in use · voice paused",
  },
};

function t(lang, key) {
  const d = LABELS[lang] || LABELS.zh;
  return d[key] || LABELS.zh[key] || key;
}

function normalizeState(raw) {
  const s = String((raw && raw.state) || "idle");
  return ({ online: 1, blind: 1, starting: 1, offline: 1, idle: 1, error: 1 }[s]) ? s : "unknown";
}

function shouldAttach(platform) {
  return String(platform || "") === "win32";
}

// 心跳 voice_ready（2026-09-19 语音）：true/false 来自驱动实测；老驱动不报（undefined/null）→ 不加后缀。
// 只在副驾真在线时显示——离线/未启动时「语音就绪」没有意义。
// → true（就绪）/ false（未就绪）/ "busy"（就绪但坐席正用麦，语音暂改文字）/ null（老驱动不报）
function normalizeVoice(raw) {
  const hb = raw && raw.heartbeat;
  if (!hb || typeof hb !== "object") return null;
  const v = hb.voice_ready;
  if (v === true) return hb.voice_mic_busy === true ? "busy" : true;
  return v === false ? false : null;
}

function labelFor(state, voice, lang) {
  const base = t(lang, state);
  if (voice === null || voice === undefined) return base;
  if (state !== "online" && state !== "blind") return base;
  const key = voice === "busy" ? "voice_busy" : voice ? "voice_ok" : "voice_off";
  return base + " · " + t(lang, key);
}

function menuSpec(state, lang, voice) {
  const items = [{ id: "status", label: labelFor(state, voice, lang), enabled: false }];
  if (state === "offline" || state === "idle" || state === "error" || state === "unknown") {
    items.push({ id: "start", label: t(lang, "start"), enabled: true });
  }
  if (state === "blind") {
    items.push({ id: "focus", label: t(lang, "focus"), enabled: true });
  }
  if (state === "online" || state === "blind" || state === "starting") {
    items.push({ id: "stop", label: t(lang, "stop"), enabled: true });
  }
  items.push({ type: "separator" });
  items.push({ id: "guide", label: t(lang, "guide"), enabled: true });
  items.push({ id: "show", label: t(lang, "show"), enabled: true });
  return items;
}

function start(deps) {
  const d = deps || {};
  if (!shouldAttach(d.platform) || !d.Tray || !d.Menu) {
    return { attached: false, destroy() {}, refresh() {}, act() {} };
  }
  let tray = null;
  let lastState = "unknown";
  let lastVoice = null;
  let timer = null;
  const interval = Number(d.intervalMs) || 8000;

  function lang() { try { return d.getLang() || "zh"; } catch (e) { return "zh"; } }

  function apply(state, voice) {
    lastState = normalizeState({ state });
    lastVoice = (voice === true || voice === false || voice === "busy") ? voice : null;
    if (!tray) return;
    try { tray.setToolTip(labelFor(lastState, lastVoice, lang())); } catch (e) { /* 托盘提示失败不阻断 */ }
    const spec = menuSpec(lastState, lang(), lastVoice);
    const template = spec.map((it) => {
      if (it.type === "separator") return { type: "separator" };
      return {
        label: it.label,
        enabled: it.enabled !== false,
        click: it.enabled === false ? undefined : () => act(it.id),
      };
    });
    try { tray.setContextMenu(d.Menu.buildFromTemplate(template)); } catch (e) { /* 菜单失败不阻断 */ }
  }

  async function poll() {
    if (typeof d.fetchJson !== "function") return;
    try {
      const st = await d.fetchJson("GET", "/api/setup/wechat_pc/copilot/status");
      apply((st && st.state) || "unknown", normalizeVoice(st));
    } catch (e) {
      apply("unknown", null);
    }
  }

  async function act(id) {
    if (id === "show" || id === "guide") {
      try { if (typeof d.onAction === "function") d.onAction(id); } catch (e) { /* 打开窗口失败不阻断 */ }
      return;
    }
    const path = { start: "/api/setup/wechat_pc/copilot/start", stop: "/api/setup/wechat_pc/copilot/stop",
      focus: "/api/setup/wechat_pc/launch-wechat" }[id];
    if (!path || typeof d.fetchJson !== "function") return;
    try { await d.fetchJson("POST", path, {}); } catch (e) { /* 启停失败下一轮 poll 会反映 */ }
    poll();
  }

  try {
    const img = (d.nativeImage && d.iconPath) ? d.nativeImage.createFromPath(d.iconPath) : null;
    tray = (img && !img.isEmpty()) ? new d.Tray(img) : new d.Tray(d.iconPath);
    tray.on("click", () => act("show"));
    apply("idle");
  } catch (e) {
    return { attached: false, destroy() {}, refresh() {}, act() {} };
  }

  poll();
  const iv = d.setInterval || setInterval;
  const cv = d.clearInterval || clearInterval;
  timer = iv(poll, interval);
  return {
    attached: true,
    destroy() {
      if (timer) { try { cv(timer); } catch (e) { /* 停轮询失败 */ } timer = null; }
      if (tray) { try { tray.destroy(); } catch (e) { /* 销毁失败 */ } tray = null; }
    },
    refresh: poll,
    act,
    state() { return lastState; },
    voice() { return lastVoice; },
  };
}

module.exports = { LABELS, t, normalizeState, normalizeVoice, labelFor, shouldAttach, menuSpec, start };
