"use strict";
// 个人微信 PC 副驾托盘：状态 → 菜单项 纯函数门禁（零 Electron）。

const assert = require("assert");
const tray = require("../wechat-pc-tray.js");

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
  console.log("  ok - " + msg);
}

ok(tray.shouldAttach("win32") && !tray.shouldAttach("darwin") && !tray.shouldAttach("linux"),
  "只在 Windows 挂托盘（微信 PC 副驾是 Win 专属）");
ok(tray.normalizeState({ state: "online" }) === "online" && tray.normalizeState({}) === "idle"
  && tray.normalizeState({ state: "nope" }) === "unknown", "状态归一");

const ids = (state) => tray.menuSpec(state, "zh").filter((i) => i.id).map((i) => i.id);
ok(ids("idle").join(",") === "status,start,guide,show", "未启动：启动 + 引导 + 打开");
ok(ids("offline").includes("start") && !ids("offline").includes("stop"), "离线只有启动");
ok(ids("online").includes("stop") && !ids("online").includes("start"), "在线只有停止");
ok(ids("blind").includes("focus") && ids("blind").includes("stop"), "看不到窗口：还原 + 停止");
ok(ids("starting").includes("stop") && !ids("starting").includes("start"), "启动中可停");
ok(ids("error").includes("start"), "异常可重试启动");

ok(tray.t("zh", "start") === "启动副驾" && tray.t("en", "start") === "Start copilot", "双语词条");
const zhKeys = Object.keys(tray.LABELS.zh).sort();
const enKeys = Object.keys(tray.LABELS.en).sort();
ok(zhKeys.join() === enKeys.join(), "zh/en 键对齐");
const CJK = /[\u3400-\u9fff]/;
ok(enKeys.every((k) => !CJK.test(tray.LABELS.en[k])), "en 侧零 CJK");

// 语音就绪后缀（2026-09-19）：只认心跳里的布尔；老驱动不报 → 无后缀；离线态不显示
ok(tray.normalizeVoice({ heartbeat: { voice_ready: true } }) === true
  && tray.normalizeVoice({ heartbeat: { voice_ready: false } }) === false
  && tray.normalizeVoice({ heartbeat: {} }) === null && tray.normalizeVoice({}) === null
  && tray.normalizeVoice({ heartbeat: { voice_ready: "yes" } }) === null, "voice_ready 归一只认布尔");
ok(tray.labelFor("online", true, "zh") === "副驾在线 · 语音就绪"
  && tray.labelFor("online", false, "en") === "Copilot online · voice not ready · text fallback"
  && tray.labelFor("online", null, "zh") === "副驾在线"
  && tray.labelFor("offline", true, "zh") === "副驾离线", "在线才带语音后缀，老驱动/离线无后缀");
// 麦克风占用（P2-3）：就绪 + voice_mic_busy → "busy"，在线态提示「语音暂改文字」；未就绪时占用位无意义
ok(tray.normalizeVoice({ heartbeat: { voice_ready: true, voice_mic_busy: true } }) === "busy"
  && tray.normalizeVoice({ heartbeat: { voice_ready: false, voice_mic_busy: true } }) === false
  && tray.labelFor("online", "busy", "zh") === "副驾在线 · 麦克风占用中 · 语音暂改文字"
  && tray.labelFor("blind", "busy", "en") === "Copilot online · window hidden · mic in use · voice paused"
  && tray.labelFor("offline", "busy", "zh") === "副驾离线", "麦克风占用后缀");
ok(tray.menuSpec("online", "zh", false)[0].label.indexOf("语音未就绪") > 0
  && tray.menuSpec("online", "zh")[0].label === "副驾在线", "菜单状态项同样带后缀，缺参兼容");

(async () => {
  const calls = [];
  let online = false;
  const fakeTray = {
    setToolTip(s) { calls.push(["tip", s]); },
    setContextMenu() { calls.push(["menu"]); },
    on(ev, fn) { calls.push(["on", ev]); this._click = fn; },
    destroy() { calls.push(["destroy"]); },
  };
  const ctl = tray.start({
    platform: "win32",
    Tray: function FakeTray() { return fakeTray; },
    Menu: { buildFromTemplate(t) { return t; } },
    nativeImage: { createFromPath() { return { isEmpty: () => true }; } },
    iconPath: "x.png",
    getLang: () => "zh",
    intervalMs: 999999,
    setInterval: () => 1,
    clearInterval() {},
    fetchJson: async (method, path) => {
      calls.push([method, path]);
      return online ? { state: "online", heartbeat: { voice_ready: true } } : { state: "offline" };
    },
  });
  ok(ctl.attached, "Win 下挂上");
  await ctl.refresh();
  ok(ctl.state() === "offline", "首轮 poll 落到 offline");
  ok(calls.some((c) => c[0] === "GET"), "首轮打了 status");
  online = true;
  await ctl.refresh();
  ok(ctl.state() === "online" && ctl.voice() === true, "第二轮在线 + 语音就绪");
  ok(calls.some((c) => c[0] === "tip" && c[1] === "副驾在线 · 语音就绪"), "托盘提示带语音就绪");
  ctl.destroy();
  ok(calls.some((c) => c[0] === "destroy"), "destroy 回收托盘");
  const none = tray.start({ platform: "darwin", Tray: function () {}, Menu: {} });
  ok(!none.attached, "非 Windows 不挂");
  console.log("wechat-pc-tray: " + passed + " passed");
})().catch((e) => { console.error(e); process.exit(1); });
