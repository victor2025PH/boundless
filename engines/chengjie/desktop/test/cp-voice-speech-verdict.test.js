"use strict";

/* cp-voice「有声裁决 / 选声失配」纯核心常驻门禁：node test/cp-voice-speech-verdict.test.js
 *
 * 背景（#121 三进宫 + #149，2026-09-02 KKXSTU）：
 *  · 「音频异常（疑似无声）」红条两轮改阈值仍误报——1.0.70 上被 Whisper 全文转录
 *    成功的预览 wav 照亮红条。根因是让浏览器端解码启发式当终审。本轮裁决改为
 *    服务端下发 voice_meta.speech（voiced/silent/unknown），客户端只在 unknown 时
 *    兜底，且加解码合理性闸。裁决核心 CpVoice.speechDecision 是纯静态方法。
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

console.log(`cp-voice-speech-verdict.test.js: ${pass} passed`);
