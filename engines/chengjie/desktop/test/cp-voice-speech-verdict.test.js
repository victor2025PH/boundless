"use strict";

/* cp-voice「有声裁决 / 选声失配」纯核心常驻门禁：node test/cp-voice-speech-verdict.test.js
 *
 * 背景（#121 三进宫 + #149，2026-09-02 KKXSTU）：
 *  · 「音频异常（疑似无声）」红条两轮改阈值仍误报——1.0.70 上被 Whisper 全文转录
 *    成功的预览 wav 照亮红条。根因是让浏览器端解码启发式当终审。本轮裁决改为
 *    服务端下发 voice_meta.speech（voiced/silent/garbled/unknown），客户端只在
 *    unknown 时兜底，且加解码合理性闸。裁决核心 CpVoice.speechDecision 是纯静态
 *    方法。#161（1.0.71 日语克隆乱音）补第三档 garbled＝有声但念错，与无声分文案
 *    分出路（CpVoice.blockNoteKey）。
 *  · 「选 X 出 Y」（下拉美月、实际使用张景光）——CpVoice.selectionMismatch 复核
 *    服务端 reason=voice_selection:* 与 voice_meta.requested_persona_id。
 *
 * 源码以 repo 根 shared/copilot 为准（桌面份由 copy-shared 镜像，双树字节一致由
 * tests/test_copilot_shared_sync.py 保证）。vm 沙箱塞最小 stub（HTMLElement /
 * customElements / window），组件 IIFE 挂 customElements 后取类，静态方法直接调用。
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.resolve(
  __dirname, "..", "..", "shared", "copilot", "components", "cp-voice.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }
function eq(name, a, b) { assert.strictEqual(a, b, `${name}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); pass++; }

function loadCpVoice() {
  const sandbox = {
    console,
    HTMLElement: class {},
    CustomEvent: class {},
    customElements: {
      _m: {},
      get(n) { return this._m[n]; },
      define(n, c) { this._m[n] = c; },
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox);
  const C = sandbox.customElements.get("cp-voice");
  assert.ok(C, "cp-voice 类应已定义（沙箱 stub 生效）");
  return C;
}

const Cls = loadCpVoice();
ok("speechDecision 挂在类上", typeof Cls.speechDecision === "function");
ok("selectionMismatch 挂在类上", typeof Cls.selectionMismatch === "function");
ok("blockNoteKey 挂在类上", typeof Cls.blockNoteKey === "function");

// ── speechDecision：服务端裁决优先 ──────────────────────────────────────────
const dead = { peak: 0.0001, rms: 0.00001, decodedSec: 9.2 };   // 客户端解码出「近零」
const live = { peak: 0.71, rms: 0.13, decodedSec: 2.7 };

// KKXSTU 形态：服务端 voiced（Whisper 全文转录）+ 客户端解码近零 → 绝不判无声
let v = Cls.speechDecision({ speech: "voiced", speech_basis: "transcript+energy" }, dead, 9.2);
eq("server voiced 压过客户端近零", v.silent, false);
eq("basis=server-voiced", v.basis, "server-voiced");
v = Cls.speechDecision({ speech: "VOICED" }, dead, 0);
eq("speech 大小写不敏感", v.silent, false);

// 服务端 silent → 判无声，客户端说有声也不翻案（字节为准）
v = Cls.speechDecision({ speech: "silent" }, live, 2.7);
eq("server silent 判无声", v.silent, true);
eq("basis=server-silent", v.basis, "server-silent");
v = Cls.speechDecision({ speech: "silent" }, null, 0);
eq("server silent 且解码失败仍判无声", v.silent, true);

// 服务端 garbled（#161 日语「ゾオパパパ」）→ 禁发，且原因与无声分开
v = Cls.speechDecision({ speech: "garbled", speech_basis: "transcript_cer:0.85" }, live, 2.7);
eq("server garbled 禁发", v.silent, true);
eq("basis=server-garbled", v.basis, "server-garbled");
eq("kind=garbled（文案分流依据）", v.kind, "garbled");
eq("silent 的 kind 是 silent",
   Cls.speechDecision({ speech: "silent" }, live, 2.7).kind, "silent");
eq("voiced 无 kind", Cls.speechDecision({ speech: "voiced" }, dead, 9.2).kind, "");
eq("客户端兜底判无声时 kind=silent",
   Cls.speechDecision({}, dead, 0).kind, "silent");
// 文案键：念错/无声/客户端兜底三条各归各的，绝不混成一句
eq("garbled 用念错文案", Cls.blockNoteKey("garbled"), "cp.voice.garbled_note");
eq("silent 用服务端无声文案", Cls.blockNoteKey("silent"), "cp.voice.silent_note_server");
eq("unknown 用旧疑似无声文案", Cls.blockNoteKey("unknown"), "cp.voice.silent_note");
eq("缺省（旧后端）用旧疑似无声文案", Cls.blockNoteKey(undefined), "cp.voice.silent_note");
eq("大小写不敏感", Cls.blockNoteKey("GARBLED"), "cp.voice.garbled_note");

// 服务端 unknown / 旧后端缺键 → 客户端双信号兜底
v = Cls.speechDecision({ speech: "unknown" }, dead, 0);
eq("unknown+近零 → 客户端判无声", v.silent, true);
eq("basis=client-dual", v.basis, "client-dual");
v = Cls.speechDecision({}, dead, 0);
eq("旧后端缺键 → 客户端判无声", v.silent, true);
v = Cls.speechDecision(null, live, 0);
eq("旧后端+正常音频 → 不判", v.silent, false);
eq("basis=client-ok", v.basis, "client-ok");
v = Cls.speechDecision(undefined, { peak: 0.0015, rms: 0.01, decodedSec: 3 }, 0);
eq("双信号必须同时成立（峰值低但 RMS 高）→ 不判", v.silent, false);

// 解码合理性闸：解码时长与服务端时长相差过半＝解码器读错容器 → 不冤枉
v = Cls.speechDecision({}, { peak: 0.0001, rms: 0.00001, decodedSec: 0.3 }, 9.2);
eq("解码时长 0.3s vs 服务端 9.2s → 不判", v.silent, false);
eq("basis=decode-implausible", v.basis, "decode-implausible");
v = Cls.speechDecision({}, { peak: 0.0001, rms: 0.00001, decodedSec: 8.9 }, 9.2);
eq("解码时长吻合 → 正常判无声", v.silent, true);
v = Cls.speechDecision({}, { peak: 0.0001, rms: 0.00001, decodedSec: 0.4 }, 0.4);
eq("短音频（≤0.5s）不启用合理性闸", v.silent, true);

// 解码失败 / 形状坏 → 不判（检测是增益不是闸门）
v = Cls.speechDecision({}, null, 0);
eq("解码失败不判", v.silent, false);
eq("basis=no-decode", v.basis, "no-decode");
v = Cls.speechDecision({}, { peak: "x" }, 0);
eq("坏形状不判", v.silent, false);

// ── selectionMismatch：选 X 必须出 X ────────────────────────────────────────
eq("服务端拒绝 persona_not_found",
   Cls.selectionMismatch("mizuki", { ok: false, reason: "voice_selection:persona_not_found" }),
   "persona_not_found");
eq("服务端拒绝 source_mismatch → persona_mismatch",
   Cls.selectionMismatch("mizuki", { ok: false, reason: "voice_selection:source_mismatch" }),
   "persona_mismatch");
eq("服务端成功且 requested==resolved==所选 → 一致",
   Cls.selectionMismatch("mizuki", { ok: true,
     voice_meta: { requested_persona_id: "mizuki", persona_id: "mizuki" } }), "");
eq("KKXSTU 截图形态：所选 mizuki、实际 zhang_jingguang → 失配",
   Cls.selectionMismatch("mizuki", { ok: true,
     voice_meta: { requested_persona_id: "mizuki", persona_id: "zhang_jingguang" } }),
   "persona_mismatch");
eq("服务端收到的 requested 与本地所选不同（请求被改写）→ 失配",
   Cls.selectionMismatch("mizuki", { ok: true,
     voice_meta: { requested_persona_id: "zhang_jingguang", persona_id: "zhang_jingguang" } }),
   "persona_mismatch");
eq("旧后端无 requested_persona_id → 不判（无可核对真值）",
   Cls.selectionMismatch("mizuki", { ok: true, voice_meta: { persona_id: "zhang_jingguang" } }), "");
eq("未显式选人设（跟随会话）→ 不判",
   Cls.selectionMismatch("", { ok: true,
     voice_meta: { requested_persona_id: "", persona_id: "zhang_jingguang" } }), "");
eq("系统通用音色哨兵 → 不判",
   Cls.selectionMismatch("__system__", { ok: true,
     voice_meta: { requested_persona_id: "__system__", persona_id: "" } }), "");
eq("普通失败响应（非选声原因）→ 不当失配（走既有失败文案）",
   Cls.selectionMismatch("mizuki", { ok: false, error: "boom-503" }), "");
eq("空响应不炸", Cls.selectionMismatch("mizuki", null), "");
eq("resolved 为空（服务端未回填）不误判", Cls.selectionMismatch("mizuki", { ok: true,
  voice_meta: { requested_persona_id: "mizuki", persona_id: "" } }), "");

// ── panelVerdict（#250 N-4 B）：结果区唯一结论，四态互斥 ─────────────────────────
ok("panelVerdict 挂在类上", typeof Cls.panelVerdict === "function");
let pv = Cls.panelVerdict({ speech: "voiced", silent: false, kind: "", stale: false });
eq("钧机形态：服务端 voiced、未过期 → ok", pv.state, "ok");
eq("ok 用「已核有声，可发送」文案", pv.key, "cp.voice.v_ok");
eq("ok 可发送", pv.canSend, true);
pv = Cls.panelVerdict({ speech: "voiced", silent: false, stale: true });
eq("过期压过一切（含 voiced）→ stale", pv.state, "stale");
eq("stale 用过期文案", pv.key, "cp.voice.stale_note");
eq("stale 不可发送（出路=重新生成）", pv.canSend, false);
pv = Cls.panelVerdict({ speech: "silent", silent: true, kind: "silent", stale: false });
eq("服务端 silent → blocked", pv.state, "blocked");
eq("blocked/silent 用服务端确认文案", pv.key, "cp.voice.silent_note_server");
pv = Cls.panelVerdict({ speech: "garbled", silent: true, kind: "garbled", stale: false });
eq("服务端 garbled → blocked", pv.state, "blocked");
eq("blocked/garbled 用念错文案", pv.key, "cp.voice.garbled_note");
pv = Cls.panelVerdict({ speech: "unknown", silent: true, kind: "silent", stale: false });
eq("客户端兜底判无声（服务端 unknown）→ blocked", pv.state, "blocked");
eq("客户端兜底用「疑似无声」文案", pv.key, "cp.voice.silent_note");
pv = Cls.panelVerdict({ speech: "unknown", silent: false, stale: false });
eq("服务端 unknown 且客户端无异常 → unverified（可发但提醒先听）", pv.state, "unverified");
eq("unverified 可发送", pv.canSend, true);
pv = Cls.panelVerdict({ speech: "", silent: false, stale: false });
eq("旧后端缺键 → unverified", pv.state, "unverified");
pv = Cls.panelVerdict({ speech: "silent", silent: true, kind: "silent", stale: true });
eq("过期 + 无声 → 只报过期（旧产物的无声结论不再有意义）", pv.state, "stale");
pv = Cls.panelVerdict(null);
eq("空入参不炸 → unverified", pv.state, "unverified");
// 四态穷举：任意组合恒返回四态之一，且 canSend 只在 ok/unverified 为真
const STATES = ["stale", "blocked", "ok", "unverified"];
[["voiced", false], ["silent", true], ["garbled", true], ["unknown", false], ["unknown", true], ["", false]]
  .forEach(([sp, si]) => [false, true].forEach((st) => {
    const r = Cls.panelVerdict({ speech: sp, silent: si, kind: sp === "garbled" ? "garbled" : "silent", stale: st });
    ok(`四态穷举 ${sp}/${si}/${st} → ${r.state}`, STATES.indexOf(r.state) >= 0);
    eq(`canSend 与 state 一致 ${sp}/${si}/${st}`, r.canSend, r.state === "ok" || r.state === "unverified");
  }));

// ── xlBaseline（#250 N-4 C）：译声过期基准 = 服务端有效目标语，不是字面 'auto' ────
ok("xlBaseline 挂在类上", typeof Cls.xlBaseline === "function");
// 钧机形态：跟随翻译开（auto）、中文客户中文文本 → identity 不译；新后端回 target_lang_resolved=zh
eq("钧机形态：identity 不译、服务端解析 zh → 基准 zh（与会话语言同值，不过期）",
   Cls.xlBaseline({ voice_translated: false, target_lang: "", target_lang_resolved: "zh" }, "auto", "zh"), "zh");
eq("旧后端缺 target_lang_resolved → 用客户端已知会话语言",
   Cls.xlBaseline({ voice_translated: false, target_lang: "" }, "auto", "zh"), "zh");
eq("旧后端 + 会话语言未知 → ''（_isStale 回落 'auto'，双侧未知视为未变）",
   Cls.xlBaseline({ voice_translated: false }, "auto", null), "");
eq("服务端解析不出（no_target）→ ''", Cls.xlBaseline({ target_lang_resolved: "" }, "auto", "zh"), "");
eq("确已翻译 → 真实译向优先", Cls.xlBaseline({ voice_translated: true, target_lang: "ja", target_lang_resolved: "ja" }, "auto", "zh"), "ja");
eq("显式目标语 + identity 不译（旧后端）→ 该语种", Cls.xlBaseline({ voice_translated: false }, "en", "zh"), "en");
eq("显式目标语 + 新后端 → 服务端真值", Cls.xlBaseline({ voice_translated: false, target_lang_resolved: "en" }, "en", "zh"), "en");
eq("开关关 → ''（发送不译，与任何会话语言都无关）", Cls.xlBaseline({ target_lang_resolved: "zh" }, "", "zh"), "");
eq("大小写归一", Cls.xlBaseline({ voice_translated: false, target_lang_resolved: "ZH" }, "AUTO", "zh"), "zh");
eq("空回包不炸", Cls.xlBaseline(null, "auto", "zh"), "zh");

console.log(`cp-voice-speech-verdict.test.js: ${pass} passed`);
