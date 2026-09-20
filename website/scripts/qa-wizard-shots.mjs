#!/usr/bin/env node
/**
 * qa-wizard-shots.mjs —— 桌面首启向导的视觉验收（逐状态截图）。
 *
 * 为什么要这个：向导只在「首次运行」弹一次，而它的关键状态（等签发 / 已激活 /
 * 试用已用尽 / 领赠量）各自依赖后端与官网的不同返回，靠人在真机上凑齐这些状态
 * 极其别扭——有些状态（如「本机试用已用尽」）要等 7 天才自然出现。
 *
 * 向导本身是自包含的 DOM + inline style，唯一外部依赖是 `window.shell` 这层 IPC 桥。
 * 于是把 shell 打桩成各状态的真实返回，就能在浏览器里逐个渲染并截图，不需要 Electron、
 * 不需要真机、不需要等待。产物用于人眼过一遍文案与版式。
 *
 * 住在 website/scripts 是沿用本仓惯例：Playwright 只装在 website 包下，
 * qa-intro-* / robot-* 等截图脚本都在这里；本脚本按路径读 desktop 的 renderer 文件。
 *
 * 用法：
 *   cd website && node scripts/qa-wizard-shots.mjs [--out <目录>] [--lang zh|en]
 */
import { chromium } from "playwright";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const RENDERER = resolve(HERE, "../../engines/chengjie/desktop/renderer");

const args = process.argv.slice(2);
const argOf = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
};
const OUT = resolve(argOf("--out", join(HERE, "../.wizard-shots")));
const LANG = argOf("--lang", "zh");

/** 每个场景 = 一套 shell 打桩 + 一串「渲染后做什么」的点击脚本。 */
// 体验档状态必须照 /api/workspace/quota 的真实返回形状打桩：frTrialView 要求
// ok + visible + source==="local_trial" + !exceeded 才显示这一步，形状不对
// 这一步会静默不出现（第一版就是这么踩的）。
const TRIAL_ON = {
  ok: true, visible: true, source: "local_trial", included: 10000,
  used: 0, remaining: 10000, exceeded: false, hours_left: 48, expired: false, level: "ok",
};

const SCENES = [
  {
    id: "01-basic",
    title: "步骤① 界面语言 + 后台令牌（AI 已配好时向导就这一步）",
    shell: { aiConfigured: true },
    steps: [],
  },
  {
    id: "02-trial-fresh",
    title: "步骤② 体验档 + 注册领 7 天（刚装机，未领过）",
    shell: { aiConfigured: true, trial: TRIAL_ON },
    steps: [{ click: "#fr-ok" }],
  },
  {
    id: "03-claim-pending",
    title: "点了领取，等厂商机签发中",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: true, claim_id: "tc_demo", status: "pending", claimed: true },
      claimStatus: { ok: true, status: "pending", claimed: true },
    },
    steps: [{ click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] }, { click: "#fr-claim" }],
  },
  {
    id: "04-activated",
    title: "签发到账，7 天完整版已激活（可继续领赠量）",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: true, claim_id: "tc_demo", status: "pending", claimed: true },
      claimStatus: {
        ok: true, status: "issued", claimed: true, activated: true,
        plan: "pro", quota: { included_chars: 25000, remaining_chars: 25000 },
      },
    },
    steps: [{ click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] }, { click: "#fr-claim" }],
  },
  {
    id: "05-gift-code",
    title: "加客服领 10 万字符（一次性绑定码 + 深链）",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: true, claim_id: "tc_demo", status: "pending", claimed: true },
      claimStatus: {
        ok: true, status: "issued", claimed: true, activated: true, plan: "pro",
        quota: { included_chars: 25000, remaining_chars: 25000 },
      },
      bindCode: {
        ok: true, bind_code: "BC-7K2M-9QXP",
        telegram_url: "https://t.me/WJKJ2026?text=BC-7K2M-9QXP",
        whatsapp_url: "https://wa.me/0000?text=BC-7K2M-9QXP",
      },
    },
    steps: [
      { click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] },
      { click: "#fr-claim" }, { click: "#fr-gift" },
    ],
  },
  {
    id: "06-exhausted",
    title: "这台机器的试用已用过（同机重领拿回过期授权）",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: true, claim_id: "tc_old", status: "issued", claimed: true },
      claimStatus: {
        ok: true, status: "expired", claimed: true, exhausted: true,
        trial_exhausted: true, activate_error: "expired",
      },
    },
    steps: [{ click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] }, { click: "#fr-claim" }],
  },
  {
    id: "07-offline",
    title: "领取时断网（必须给出路，不能是死路）",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: false, error: "network" },
    },
    steps: [{ click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] }, { click: "#fr-claim" }],
  },
  {
    id: "08-no-fingerprint",
    title: "取不到机器指纹（绑不了机，当场说清而不是静默建单）",
    shell: {
      aiConfigured: true,
      trial: TRIAL_ON,
      claim: { ok: false, error: "no_fingerprint" },
    },
    steps: [{ click: "#fr-ok" }, { fill: ["#fr-contact", "@your_handle"] }, { click: "#fr-claim" }],
  },
];

function pageHtml(model, wizard, stub) {
  // 轮询间隔在打桩里压到 1ms：真向导每 5s 一轮，截图不该为此干等。
  return `<!DOCTYPE html><html lang="${LANG}"><head><meta charset="utf-8">
<style>html,body{margin:0;height:100%;background:#0b0f17}</style></head><body>
<script>${model}</script>
<script>
  window.frClaimPollPlan = function (attempt) {
    return { again: attempt < 3, delayMs: 1, giveUpKey: "claim_slow" };
  };
  ${stub}
</script>
<script>${wizard}</script>
</body></html>`;
}

function stubFor(scene) {
  const s = scene.shell || {};
  const ai = s.aiConfigured
    ? { ok: true, configured: true, ai_ready: true }
    : { ok: true, configured: false };
  return `window.shell = {
    forceFirstRun: true,
    getConfig: () => Promise.resolve({ backend: { token: "admin" }, unified_inbox: { lang: "${LANG}" } }),
    setupAiStatus: () => Promise.resolve(${JSON.stringify(ai)}),
    trialStatus: () => Promise.resolve(${JSON.stringify(s.trial || null)}),
    trialClaim: () => Promise.resolve(${JSON.stringify(s.claim || { ok: false, error: "unsupported" })}),
    trialClaimStatus: () => Promise.resolve(${JSON.stringify(s.claimStatus || { ok: true, status: "pending", claimed: true })}),
    trialBindCode: () => Promise.resolve(${JSON.stringify(s.bindCode || { ok: false })}),
    saveConfig: () => Promise.resolve({ ok: true }),
    openExternal: () => Promise.resolve({ ok: true }),
  };`;
}

async function main() {
  const model = await readFile(join(RENDERER, "first-run-model.js"), "utf8");
  const wizard = await readFile(join(RENDERER, "first-run.js"), "utf8");
  await mkdir(OUT, { recursive: true });

  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1000, height: 760 }, deviceScaleFactor: 2 });
  const problems = [];
  const done = [];

  for (const scene of SCENES) {
    await page.setContent(pageHtml(model, wizard, stubFor(scene)), { waitUntil: "load" });
    // 向导是异步起的（Promise.all 三个 shell 调用），等卡片真出现再动手
    try {
      await page.waitForSelector("div[style*='z-index:99999']", { timeout: 5000 });
    } catch {
      problems.push(`${scene.id}: 向导没渲染出来`);
      continue;
    }
    let failed = "";
    for (const step of scene.steps) {
      try {
        if (step.click) await page.click(step.click, { timeout: 4000 });
        if (step.fill) await page.fill(step.fill[0], step.fill[1], { timeout: 4000 });
        await page.waitForTimeout(120);
      } catch (e) {
        failed = `${step.click || step.fill?.[0]} —— ${String(e).split("\n")[0]}`;
        break;
      }
    }
    if (failed) problems.push(`${scene.id}: ${failed}`);
    await page.waitForTimeout(260);
    const file = join(OUT, `${scene.id}.png`);
    await page.screenshot({ path: file });
    // 同时把卡片纯文本存一份：文案审阅读文本比数像素快
    const text = await page.evaluate(() => {
      const card = document.querySelector("div[style*='z-index:99999'] > div");
      return card ? card.innerText : "";
    });
    await writeFile(join(OUT, `${scene.id}.txt`), `${scene.title}\n${"─".repeat(48)}\n${text}\n`, "utf8");
    done.push({ id: scene.id, title: scene.title, ok: !failed });
    console.log(`  ${failed ? "✗" : "✓"} ${scene.id}  ${scene.title}`);
  }

  await browser.close();
  console.log(`\n产物目录：${OUT}`);
  if (problems.length) {
    console.error(`\n${problems.length} 个场景没走通：`);
    for (const p of problems) console.error("  - " + p);
    process.exit(1);
  }
  console.log(`${done.length} 个状态全部渲染成功`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
