/**
 * 品牌 preset 同步脚本：platform/brand → website/vendor/brand（vendored 副本）。
 *
 * 为什么需要它：
 *   单一真相是 monorepo 的 `platform/brand/{tailwind-preset.cjs,tokens.json,optical-scale.json}`，
 *   但官网部署到只包含 `website/` 的服务器时无法引用 website 之外的路径
 *   （曾导致 next build 失败：Cannot find module '../platform/brand/tailwind-preset.cjs'）。
 *   因此 website 保留一份 vendored 副本自包含；本脚本把上游机械同步过来，避免两处手改漂移。
 *
 * 用法（在 website/ 下）：
 *   npm run sync:brand          # 同步并写入 vendor/brand
 *   npm run sync:brand -- --check   # 只校验是否已最新（CI/预部署用，不一致则非零退出）
 *
 * 改了品牌 token / 光学系数？改 platform/brand/ 对应文件 → 跑 npm run sync:brand → 提交 vendor/brand。
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const websiteRoot = join(here, "..");
const srcDir = join(websiteRoot, "..", "platform", "brand");
const dstDir = join(websiteRoot, "vendor", "brand");
const FILES = ["tailwind-preset.cjs", "tokens.json", "optical-scale.json"];
const checkOnly = process.argv.includes("--check");

const HEADER = {
  ".cjs": (name) =>
    `/* AUTO-VENDORED from platform/brand/${name} — 请勿手改；改上游后跑 npm run sync:brand。*/\n`,
  ".json": null, // JSON 不能有注释，保持原样
};

if (!existsSync(srcDir)) {
  // 无上游（如只检出/部署 website/ 的服务器）：vendored 副本已随包存在就优雅降级用现有的，
  // 让 prebuild 钩子在服务器 next build 时不报错；仅当连 vendored 都缺时才判定为真缺失。
  const haveVendored = FILES.every((f) => existsSync(join(dstDir, f)));
  if (haveVendored) {
    console.log(`[sync:brand] 上游不存在 (${srcDir})，使用已 vendored 的副本（跳过同步）`);
    process.exit(0);
  }
  console.error(`[sync:brand] 上游目录不存在且无 vendored 副本: ${srcDir}`);
  console.error("  （完整 monorepo 下运行本脚本可生成 vendored；仅拿到 website/ 时应确保 vendor/brand 已随包。）");
  process.exit(1);
}

mkdirSync(dstDir, { recursive: true });
let changed = 0;
for (const f of FILES) {
  const src = join(srcDir, f);
  const dst = join(dstDir, f);
  if (!existsSync(src)) {
    // 服务器上可能有残缺的 platform/brand（只含旧文件）：缺的项保留 vendored，不拖垮 prebuild。
    if (existsSync(dst)) {
      console.log(`[sync:brand] 上游缺 ${f}，保留已 vendored 副本`);
      continue;
    }
    console.error(`[sync:brand] 缺上游文件且无 vendored 副本: ${src}`);
    process.exit(1);
  }
  let content = readFileSync(src, "utf8");
  const ext = f.slice(f.lastIndexOf("."));
  const hdr = HEADER[ext];
  const out = hdr ? hdr(f) + content : content;
  const prev = existsSync(dst) ? readFileSync(dst, "utf8") : null;
  if (prev !== out) {
    changed++;
    if (checkOnly) {
      console.error(`[sync:brand] 过期: vendor/brand/${f} 与 platform/brand/${f} 不一致，请跑 npm run sync:brand`);
    } else {
      writeFileSync(dst, out, "utf8");
      console.log(`[sync:brand] 已更新 vendor/brand/${f}`);
    }
  } else {
    console.log(`[sync:brand] 最新 vendor/brand/${f}`);
  }
}

if (checkOnly && changed > 0) process.exit(2);
console.log(`[sync:brand] 完成（${changed} 个变更）`);
