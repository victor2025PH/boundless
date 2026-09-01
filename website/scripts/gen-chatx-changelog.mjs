/**
 * 智聊 ChatX 版本更新记录生成器 —— 发版时把一条双语条目写进 lib/chatx-release-notes.json。
 *
 * 「每次发包都生成 changelog」的执行工具：网站发布页与 announcements.json 同步长出新版本。
 * 与 announcements.json 分工：announcements 是客户端内横幅（中文单段，publish_chatx.ps1 维护），
 * 本文件是官网结构化双语时间线（新增条目走这里）；gate:content 检查 6 强制两者版本对齐。
 *
 * 用法（在 website/ 下）：
 *   node scripts/gen-chatx-changelog.mjs add \
 *     --version 1.0.62 [--date 2026-09-01] --tags fix,improve \
 *     --title-zh "一句话中文主题" --title-en "one-line English title" \
 *     --zh path/to/_notes_1062.txt   --en path/to/_notes_1062_en.txt
 *
 *   要点来源二选一：--zh/--en 传文本文件（按行拆，自动去掉行首 ·•-* 项目符号与空行），
 *   或 --hl-zh/--hl-en 可重复传单条要点。zh 与 en 必须都给（官网是双语站，不做静默回落）。
 *   同版本号已存在 → 覆盖该条（幂等）；写入后按语义化版本从新到旧排序。
 *
 * -n / --dry-run 只打印将写入的条目，不落盘。
 */
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, isAbsolute, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const websiteRoot = join(here, "..");
const dataPath = join(websiteRoot, "lib", "chatx-release-notes.json");
const VALID_TAGS = new Set(["feature", "fix", "improve", "security"]);

function fail(m) { console.error(`[changelog] ✗ ${m}`); process.exit(1); }

/** 解析 `--key value` / `--flag` / 可重复 `--hl-zh a --hl-zh b` 形式的参数。 */
function parseArgs(argv) {
  const out = { _: [], "hl-zh": [], "hl-en": [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "-n") { out["dry-run"] = true; continue; }
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      const takesValue = next !== undefined && !next.startsWith("--");
      if (key === "hl-zh" || key === "hl-en") { out[key].push(next); i++; }
      else if (key === "dry-run" || key === "n") { out["dry-run"] = true; }
      else if (takesValue) { out[key] = next; i++; }
      else { out[key] = true; }
    } else out._.push(a);
  }
  if (out.n) out["dry-run"] = true;
  return out;
}

/** 从文本文件读要点：按行拆，去行首项目符号（· • - * 数字.）与首尾空白，丢弃空行。 */
function bulletsFromFile(p) {
  const abs = isAbsolute(p) ? p : resolve(process.cwd(), p);
  if (!existsSync(abs)) fail(`要点文件不存在：${abs}`);
  return readFileSync(abs, "utf8")
    .split(/\r?\n/)
    .map((l) => l.replace(/^\s*(?:[·•*\-]|\d+[.)、])\s*/, "").trim())
    .filter((l) => l.length > 0);
}

function cmdAdd(args) {
  const version = String(args.version ?? "").trim();
  if (!/^\d+\.\d+\.\d+$/.test(version)) fail(`--version 必须是语义化版本 x.y.z（收到 "${version}"）`);

  const date = String(args.date ?? new Date().toISOString().slice(0, 10)).trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) fail(`--date 必须是 YYYY-MM-DD（收到 "${date}"）`);

  const tags = String(args.tags ?? "").split(",").map((t) => t.trim()).filter(Boolean);
  if (tags.length === 0) fail("--tags 至少给一个（feature|fix|improve|security，逗号分隔）");
  for (const t of tags) if (!VALID_TAGS.has(t)) fail(`未知 tag "${t}"（可选：feature|fix|improve|security）`);

  const titleZh = String(args["title-zh"] ?? "").trim();
  const titleEn = String(args["title-en"] ?? "").trim();
  if (!titleZh || !titleEn) fail("必须同时给 --title-zh 与 --title-en（官网是双语站，不做静默回落）");

  const hlZh = args["hl-zh"].length ? args["hl-zh"].map((s) => s.trim()).filter(Boolean) : (args.zh ? bulletsFromFile(args.zh) : []);
  const hlEn = args["hl-en"].length ? args["hl-en"].map((s) => s.trim()).filter(Boolean) : (args.en ? bulletsFromFile(args.en) : []);
  if (hlZh.length === 0) fail("中文要点为空：给 --zh <文件> 或重复 --hl-zh <要点>");
  if (hlEn.length === 0) fail("英文要点为空：给 --en <文件> 或重复 --hl-en <要点>");

  const entry = { version, date, title: { zh: titleZh, en: titleEn }, tags, highlights: { zh: hlZh, en: hlEn } };

  const notes = JSON.parse(readFileSync(dataPath, "utf8"));
  const kept = notes.filter((n) => n.version !== version); // 幂等：同版本号覆盖
  const replaced = kept.length !== notes.length;
  kept.push(entry);
  kept.sort((a, b) => cmpSemverDesc(a.version, b.version));

  if (args["dry-run"]) {
    console.log(`[changelog] DRYRUN 将${replaced ? "覆盖" : "新增"} v${version}：`);
    console.log(JSON.stringify(entry, null, 2));
    return;
  }
  writeFileSync(dataPath, JSON.stringify(kept, null, 2) + "\n", "utf8");
  console.log(`[changelog] ✓ 已${replaced ? "覆盖" : "新增"} v${version}（${hlZh.length} 条要点）→ lib/chatx-release-notes.json`);
  console.log(`[changelog]   记得同步更新下载事实源（publish_chatx.ps1 会更新 manifest / announcements），并跑 npm run gate:content`);
}

/** 语义化版本从新到旧比较。 */
function cmpSemverDesc(a, b) {
  const pa = a.split(".").map(Number), pb = b.split(".").map(Number);
  for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pb[i] - pa[i];
  return 0;
}

// -- 入口 --------------------------------------------------------------------
const argv = process.argv.slice(2);
const sub = argv[0];
const args = parseArgs(argv.slice(1));
if (sub === "add") cmdAdd(args);
else {
  console.log("用法: node scripts/gen-chatx-changelog.mjs add --version X.Y.Z --tags fix,improve --title-zh ... --title-en ... --zh notes.txt --en notes_en.txt [--date YYYY-MM-DD] [-n]");
  process.exit(sub ? 1 : 0);
}
