"use strict";

// 注入诊断纯函数单测（无框架，node 直跑）：node test/inject-status.test.js
const assert = require("assert");
const { deriveInjectState } = require("../renderer/inject-status.js");

// 本层只出 { cls, code, vars }（i18n 收口：文案在 renderer/shell-i18n.js 的 inject.* 段，
// 由展示层按 code 取词）。故断言口径＝cls/code/vars + 「两语词典都有这个 code」。
const SHI = require("../renderer/shell-i18n.js");

let pass = 0;
function check(name, got, expectCls) {
  assert.strictEqual(got.cls, expectCls, `${name}: 期望 cls=${expectCls}，实际 ${got.cls}`);
  assert.ok(got.code, `${name}: code 不应为空`);
  assert.ok(got.vars && typeof got.vars === "object", `${name}: vars 应为对象（可为空）`);
  // 每个 code 必须在两语词典里都有标题与 tooltip——漏一条线上就显裸键 `inject.xxx`
  for (const lang of ["zh", "en"]) {
    for (const suffix of ["", ".d"]) {
      const key = "inject." + got.code + suffix;
      assert.notStrictEqual(SHI.tIn(lang, key), key,
        `${name}: shell-i18n.js 缺 ${lang} 词条 ${key}（线上会显裸键）`);
    }
  }
  pass++;
}

// 无上报 → 等待
check("无上报", deriveInjectState(null), "wait");

// 平台无档案 → bad
check("无档案", deriveInjectState({ supported: false }), "bad");

// 已支持但未登录/未开会话（无输入框无会话）→ warn(未登录)
check("未登录", deriveInjectState({ supported: true, composer: false, bubbles: 0, chatOpen: false }), "warn");

// 会话开了但找不到输入框 → warn(输入框失配)
check("输入框失配", deriveInjectState({ supported: true, composer: false, bubbles: 5, chatOpen: true }), "warn");

// 输入框在但会话开着却抓不到消息 → warn(消息失配)
check("消息失配", deriveInjectState({ supported: true, composer: true, bubbles: 0, chatOpen: true }), "warn");

// 输入框 + 有消息 → ok
const ok = deriveInjectState({ supported: true, composer: true, bubbles: 8, chatOpen: true });
check("正常", ok, "ok");
// 消息数是**数据**（经 vars 插值），不是文案：两语 tooltip 渲染出来都得带上那个 8，
// 否则「注入正常但一条都没抓到」和「抓到 8 条」在坐席眼里没区别。
assert.strictEqual(ok.vars.bubbles, 8, "正常态 vars 应带气泡数");
for (const lang of ["zh", "en"]) {
  assert.ok(SHI.tIn(lang, "inject.ok.d", ok.vars).indexOf("8") >= 0,
    `${lang} 正常态 tooltip 应插值出消息数`);
}
pass += 3;

// 仅输入框在、无会话（刚登录未点会话）→ ok（composer 在即视为注入可用）
check("仅输入框", deriveInjectState({ supported: true, composer: true, bubbles: 0, chatOpen: false }), "ok");

// ── 提取器失效（元素在、内容抓不出）─────────────────────────────────────────────
// 官方改版常见形态：bubble 照样命中而 bubbleText/mid 提取全空 → 翻译按钮一个都不出现、
// 消息一条都不回流，只数元素存在性的旧判据会报「注入正常」。判定顺序须与后端
// desktop_inject_health.classify_inject_health 逐档一致（两侧同口径）。
function live(extra) {
  return Object.assign(
    { supported: true, composer: true, bubbles: 5, chatOpen: true }, extra || {});
}

const exText = deriveInjectState(live({ extract: { decorated: 0, unresolved: 5 } }));
check("正文提取失配", exText, "warn");
assert.strictEqual(exText.code, "mismatch_text", "正文提取失配应报 mismatch_text");
// 两档失配的区分度靠**文案**说清（都是 warn/都是「选择器失配」），故词典里两语都得
// 指路各自该校准的字段——否则运维看到黄灯不知道去改 bubbleText 还是 mid。
for (const lang of ["zh", "en"]) {
  assert.ok(SHI.tIn(lang, "inject.mismatch_text.d", exText.vars).indexOf("bubbleText") >= 0,
    `${lang} 正文失配 tooltip 应指路 PROFILES.bubbleText`);
}
pass += 3;

const exIngest = deriveInjectState(live({
  extract: { decorated: 5, ingestTried: 4, ingestKeyed: 0 },
}));
check("消息标识失配", exIngest, "warn");
assert.strictEqual(exIngest.code, "mismatch_ingest", "标识失配须与正文失配区分（不同 code）");
for (const lang of ["zh", "en"]) {
  const d = SHI.tIn(lang, "inject.mismatch_ingest.d", exIngest.vars);
  assert.ok(d.indexOf("mid") >= 0 || d.indexOf("peerId") >= 0,
    `${lang} 标识失配 tooltip 应指路 mid/peerId`);
  assert.notStrictEqual(SHI.tIn(lang, "inject.mismatch_ingest"),
    SHI.tIn(lang, "inject.mismatch_text"),
    `${lang} 两档失配的标题不得同文（坐席分不出该修哪）`);
}
pass += 3;

// 正文塌了必然连带 ingest 拿不到内容 → 报根因，别把运营引去校准 mid
const both = deriveInjectState(live({
  extract: { decorated: 0, unresolved: 5, ingestTried: 5, ingestKeyed: 0 },
}));
assert.strictEqual(both.code, "mismatch_text", "正文失配须优先于标识失配");
pass++;

// snake_case（后端归一后的记录）同样认
check("标识失配 snake", deriveInjectState(live({
  extract: { decorated: 5, ingest_tried: 3, ingest_keyed: 0 },
})), "warn");

// ── 不该误报的边界（比该报的更要紧：误报一次运维就不再信这盏灯）──────────────────
// 旧版桌面壳不上报 extract → 绝不能因「老客户端没这字段」变黄
check("无 extract 字段仍正常", deriveInjectState(live()), "ok");
check("extract 为 null 仍正常", deriveInjectState(live({ extract: null })), "ok");
// 长会话稳定态：全推过了 → tried=keyed=0，分母为 0 必须判正常
check("稳定态无待回流", deriveInjectState(live({
  extract: { decorated: 200, unresolved: 0, ingestTried: 0, ingestKeyed: 0 },
})), "ok");
// 混合会话（贴纸/通话记录本就提不出内容）：有一条成功就说明提取器活着
check("部分提不出仍正常", deriveInjectState(live({
  extract: { decorated: 4, unresolved: 1 },
})), "ok");
check("部分拿到键仍正常", deriveInjectState(live({
  extract: { decorated: 5, ingestTried: 4, ingestKeyed: 1 },
})), "ok");

// ── core.js 计数接线的静态门禁 ──────────────────────────────────────────────────
// 计数搭便车在既有遍历里（零额外 DOM 开销），有两条顺序不变量一旦被挪就会静默误报：
//   ① _exReset() 必须在 forEach 之前——漏了就累加成单调递增，判据永久失真；
//   ② PUSHED 命中**不得**计入 ingestTried 分母——否则长会话稳定态（全推过了）
//      会被判成「键提取全废」，把一切正常报成故障。
{
  const fs = require("fs");
  const path = require("path");
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "shared", "inject", "core.js"), "utf8");

  const scan = src.slice(src.indexOf("function scanAll"));
  const iReset = scan.indexOf("_exReset()");
  const iEach = scan.indexOf("bubbles.forEach");
  assert.ok(iReset > 0 && iEach > 0 && iReset < iEach, "_exReset 必须在扫描遍历之前");
  pass++;

  const ing = src.slice(src.indexOf("function ingestBubble"));
  const body = ing.slice(0, ing.indexOf('fire("ingest"'));
  const iPushed = body.indexOf("PUSHED.has(key)");
  const iTried = body.indexOf("_ex.ingestTried++", iPushed);
  assert.ok(iPushed > 0 && iTried > iPushed, "已推过的气泡不得计入 ingestTried 分母");
  pass++;

  // 上报载荷必须带 extract 段，且节流指纹要含提取器事实位（否则全绿→全废要等 30s 心跳）
  const rep = src.slice(src.indexOf("function reportInjectStatus"));
  assert.ok(/extract:\s*\{/.test(rep), "健康载荷须含 extract 段");
  assert.ok(rep.indexOf("_ex.unresolved > 0 ? 1 : 0") > 0, "节流指纹须含提取器事实位");
  pass += 2;

  // 已装饰的气泡（早退路径）也要计入 decorated，否则稳定态会被判成「一条都没装饰上」
  const dec = src.slice(src.indexOf("function decorateBubble"));
  assert.ok(/getAttribute\(PROCESSED\)\)\s*\{\s*_ex\.decorated\+\+/.test(dec),
    "PROCESSED 早退路径须计入 decorated");
  pass++;
}

// ── code 字：与后端状态字同名的稳定标识 ──────────────────────────────────────
// 受控出站按 code 判「这账号压根没档案就别拉命令」，账号栏也要按症状分流；只有 cls
// 四档太粗，而 text 是会被产品改写的中文文案——拿文案 match 迟早悄悄失效。
{
  const cases = [
    [null, "wait"],
    [{ supported: false }, "unsupported"],
    [{ supported: true, composer: false, bubbles: 0, chatOpen: false }, "no_chat"],
    [{ supported: true, composer: false, bubbles: 5, chatOpen: true }, "mismatch_composer"],
    [{ supported: true, composer: true, bubbles: 0, chatOpen: true }, "mismatch_bubble"],
    [{ supported: true, composer: true, bubbles: 4, chatOpen: true,
       extract: { decorated: 0, unresolved: 4 } }, "mismatch_text"],
    [{ supported: true, composer: true, bubbles: 4, chatOpen: true,
       extract: { decorated: 4, ingestTried: 3, ingestKeyed: 0 } }, "mismatch_ingest"],
    [{ supported: true, composer: true, bubbles: 8, chatOpen: true }, "ok"],
  ];
  for (const [input, code] of cases) {
    assert.strictEqual(deriveInjectState(input).code, code,
      `code 漂移：${JSON.stringify(input)} 应为 ${code}`);
    pass++;
  }
  // 与后端 classify_inject_health 的状态字逐字对齐（两侧同口径，别各说一套）
  const fs = require("fs");
  const path = require("path");
  const py = fs.readFileSync(
    path.join(__dirname, "..", "..", "src", "web", "desktop_inject_health.py"), "utf8");
  for (const code of ["unsupported", "no_chat", "mismatch_composer", "mismatch_bubble",
                      "mismatch_text", "mismatch_ingest"]) {
    assert.ok(py.includes(`return "${code}"`),
      `后端 classify_inject_health 没有状态字 ${code} —— 壳层 code 与后端分类口径已分叉`);
    pass++;
  }
}

console.log(`inject-status.test.js: ${pass} passed`);
