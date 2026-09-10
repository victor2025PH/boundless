/**
 * 下载台账/闸门纯冒烟：npx tsx lib/download-ledger.test.ts（不依赖 Next runtime）。
 * env 必须在动态 import 前设好——lib 在 import 时解析台账路径。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "dl-ledger-"));
process.env.LEADS_DIR = TMP;
process.env.ANALYTICS_DIR = TMP;
process.env.DL_TRUSTED_IPS = "27.126.158.220, 112.198.239.199";
process.env.DL_RATE_MAX_FILES_PER_HOUR = "3";

async function main() {
  const L = await import("./download-ledger");

  // 分类
  assert.deepStrictEqual(L.classifyPath("downloads/internal/ChatX-Setup-1.0.79.exe"), {
    product: "chatx", version: "1.0.79", channel: "internal",
  });
  assert.deepStrictEqual(L.classifyPath("releases/AvatarHub-Setup-1.5.3.exe"), {
    product: "avatarhub", version: "1.5.3", channel: "public",
  });
  assert.strictEqual(L.classifyPath("releases/matrixx/MatrixX-2.1.1-Setup.exe").product, "matrixx");
  assert.strictEqual(L.classifyPath("downloads/lite/ChatX-Setup-1.0.78.exe").channel, "lite");
  assert.ok(L.isInstallerPath("downloads/ChatX-Setup-1.0.79.exe"));
  assert.ok(!L.isInstallerPath("downloads/latest.yml"));
  assert.ok(!L.isInstallerPath("downloads/ChatX-Setup-1.0.79.exe.blockmap"));

  // 爬虫 UA：nginx 里实锤出现过的全部非人类都得中；真人/更新器/curl 不能中
  for (const ua of [
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
    "Mozilla/5.0 (compatible; OAI-SearchBot/1.3; +https://openai.com/searchbot)",
    "TelegramBot (like TwitterBot)",
    "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
    "Mozilla/5.0 (compatible; jscrawler/0.1; +https://github.com/)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Windows; U;XMPP Tiscali Communicator v.10.0.1; Windows NT 5.1; en-US)",
    "Mozilla/5.0 (Windows; U; Windows NT 5.1; en-US; rv:1.8.0.5) Gecko/20060719",
    "python-requests/2.31",
    "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
  ]) assert.ok(L.isBotUa(ua), `should be bot: ${ua}`);
  for (const ua of [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "electron-builder",
    "curl/8.13.0",
    "AvatarHub-Installer/1.4.6",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
  ]) assert.ok(!L.isBotUa(ua), `should NOT be bot: ${ua}`);

  const CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0.0.0 Safari/537.36";
  const exe = (v: string) => `downloads/ChatX-Setup-1.0.${v}.exe`;
  let now = Date.parse("2026-09-10T10:00:00Z");

  // 办公室：免闸、标 office、照常 first 去重
  L._resetDlState();
  let d = L.gate({ ip: "27.126.158.220", ua: "TelegramBot (like TwitterBot)", path: exe("79"), hasRange: false, now });
  assert.ok(d.allow && d.flags.includes("office") && d.first);
  d = L.gate({ ip: "27.126.158.220", ua: CHROME, path: exe("79"), hasRange: true, now: now + 1000 });
  assert.ok(d.allow && !d.first, "同文件 30 分钟内第二次不算 first");

  // 爬虫 403、空 UA 403；yml 不设闸
  L._resetDlState();
  d = L.gate({ ip: "74.7.227.24", ua: "GPTBot/1.2", path: exe("79"), hasRange: false, now });
  assert.strictEqual(d.status, 403); assert.ok(!d.allow && d.flags.includes("bot"));
  d = L.gate({ ip: "74.7.227.24", ua: "", path: exe("79"), hasRange: false, now });
  assert.strictEqual(d.reason, "empty-ua");
  d = L.gate({ ip: "74.7.227.24", ua: "GPTBot/1.2", path: "downloads/latest.yml", hasRange: false, now });
  assert.ok(d.allow, "非安装包不设闸");

  // 限流：按不同文件数（上限 3），同一文件的 Range 续传不计
  L._resetDlState();
  const ip = "1.2.3.4";
  for (const v of ["71", "72", "73"]) {
    d = L.gate({ ip, ua: CHROME, path: exe(v), hasRange: false, now });
    assert.ok(d.allow && d.first, `第 ${v} 个文件应放行`);
  }
  for (let i = 0; i < 50; i++) {
    d = L.gate({ ip, ua: CHROME, path: exe("73"), hasRange: true, now: now + i * 1000 });
    assert.ok(d.allow && !d.first && d.flags.includes("range"), "差量 Range 请求不占额度、不重复记账");
  }
  d = L.gate({ ip, ua: CHROME, path: exe("74"), hasRange: false, now });
  assert.strictEqual(d.status, 429); assert.ok(d.flags.includes("ratelimit"));
  // 一小时后窗口滑走，恢复
  d = L.gate({ ip, ua: CHROME, path: exe("74"), hasRange: false, now: now + 61 * 60_000 });
  assert.ok(d.allow, "窗口过后应恢复");

  // sweep：一小时内三个产品家族
  L._resetDlState();
  d = L.gate({ ip: "5.6.7.8", ua: CHROME, path: exe("79"), hasRange: false, now });
  assert.ok(!d.flags.includes("sweep"));
  d = L.gate({ ip: "5.6.7.8", ua: CHROME, path: "releases/AvatarHub-Setup-1.5.3.exe", hasRange: false, now });
  assert.ok(!d.flags.includes("sweep"));
  d = L.gate({ ip: "5.6.7.8", ua: CHROME, path: "releases/matrixx/MatrixX-2.1.1-Setup.exe", hasRange: false, now });
  assert.ok(d.allow && d.flags.includes("sweep"), "第三个产品应打 sweep 标（不拦）");

  // 落盘 + 读聚合 + 装机交叉
  fs.writeFileSync(path.join(TMP, "client-logs.jsonl"), [
    JSON.stringify({ t: new Date().toISOString(), ip: "9.9.9.9", fp: "AAAA-BBBB-CCCC-0001", logger: "beacon", msg: "boot" }),
    JSON.stringify({ t: new Date().toISOString(), ip: "27.126.158.220", fp: "TEST-BEAC-ON00-0001", logger: "beacon", msg: "boot" }),
  ].join("\n") + "\n");
  type Flag = import("./download-ledger").DlFlag;
  const rec = (ip: string, ua: string, via: "r2" | "local" | "blocked", status: number, flags: Flag[], p = exe("79"), reason?: string) =>
    L.appendDownload(L.buildRecord({ ip, ua, ref: "", path: p }, via, status, flags, reason));
  await rec("9.9.9.9", CHROME, "r2", 302, []);
  await rec("9.9.9.10", CHROME, "local", 200, [], "releases/AvatarHub-Setup-1.5.3.exe");
  await rec("27.126.158.220", "curl/8.13.0", "r2", 302, ["office"]);
  await rec("74.7.227.24", "GPTBot/1.2", "blocked", 403, ["bot"], exe("79"), "bot-ua");
  await rec("1.2.3.4", CHROME, "blocked", 429, ["ratelimit"], exe("74"), "too-many-files");
  assert.ok(fs.existsSync(L.DOWNLOAD_LOG), "台账文件应落在 ANALYTICS_DIR");

  const s = await L.readLedger({ days: 7 });
  assert.strictEqual(s.total, 5);
  assert.strictEqual(s.human, 2);
  assert.strictEqual(s.office, 1);
  assert.strictEqual(s.bot, 1);
  assert.strictEqual(s.blocked, 1);
  assert.strictEqual(s.human_ips, 2);
  assert.strictEqual(s.human_ips_installed, 1, "9.9.9.9 有信标 → 已装机");
  assert.deepStrictEqual(s.by_product.map((p) => p.product).sort(), ["avatarhub", "chatx"]);
  assert.strictEqual(s.rows.length, 2, "默认只列外部");
  assert.deepStrictEqual(s.rows.find((r) => r.ip === "9.9.9.9")?.fps, ["AAAA-BBBB-CCCC-0001"]);
  const all = await L.readLedger({ days: 7, includeNoise: true });
  assert.strictEqual(all.rows.length, 5);
  assert.strictEqual(all.rows.find((r) => r.ip === "74.7.227.24")?.kind, "bot");
  assert.strictEqual(all.rows.find((r) => r.ip === "1.2.3.4")?.kind, "blocked");
  const only = await L.readLedger({ days: 7, product: "avatarhub" });
  assert.strictEqual(only.total, 1);

  console.log("download-ledger smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
}).finally(() => {
  fs.rmSync(TMP, { recursive: true, force: true });
});
