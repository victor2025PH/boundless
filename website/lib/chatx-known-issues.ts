/**
 * 报障自助：按用户描述 / 客户端错误摘要匹配常见问题，先给自助步骤，解决不了再转人工。
 * 只收录通用、确定有效的处理办法；新问题由客服在工单里总结后补进来。
 */
import { mkdir, readFile, rename, stat, writeFile } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import { DATA_DIR } from "./data-dir";

export type KnownIssue = { id: string; match: RegExp; zh: string; en: string };

export const KNOWN_ISSUES: KnownIssue[] = [
  {
    id: "smartscreen",
    match: /smartscreen|保护了你的电脑|protected your pc|无法验证发布者|unknown publisher/i,
    zh: "Windows 弹出「已保护你的电脑」时：点「更多信息」→「仍要运行」即可继续安装。",
    en: "If Windows shows “Windows protected your PC”: click “More info” → “Run anyway”.",
  },
  {
    id: "antivirus",
    match: /杀毒|杀软|360|火绒|defender|antivirus|病毒|隔离|quarantin|被删/i,
    zh: "安装包或程序被杀毒软件拦截 / 删除时：在杀毒软件里把 ChatX 安装目录加入信任（白名单），再重新下载安装。",
    en: "If your antivirus blocks or removes ChatX: add the ChatX install folder to its allow-list, then download and install again.",
  },
  {
    id: "permission",
    match: /permission ?denied|access is denied|拒绝访问|权限|winerror 5\b/i,
    zh: "提示「拒绝访问 / 权限不足」时：右键 ChatX 图标 →「以管理员身份运行」，并确认没有装在受保护的系统目录里。",
    en: "For “Access is denied” errors: right-click ChatX → “Run as administrator”, and avoid installing into protected system folders.",
  },
  {
    id: "disk",
    match: /no space|disk full|磁盘.*(满|不足)|空间不足|errno 28/i,
    zh: "磁盘空间不足：安装包约 450 MB，安装后需要更多空间，请先清理出至少 2 GB 再安装。",
    en: "Low disk space: the installer is ~450 MB and needs more once installed — free up at least 2 GB first.",
  },
  {
    id: "network",
    match: /timeout|timed out|connection(error| reset| refused)|econn|ssl|proxy|网络|连不上|超时|代理/i,
    zh: "网络 / 连接超时：检查网络是否正常；开了代理或 VPN 的，换个节点或暂时关掉后重试。",
    en: "Network timeouts: check your connection; if you use a proxy or VPN, switch nodes or turn it off and retry.",
  },
];

/** 用最近一次 refreshKnownIssues() 的结果匹配（未刷新时即内置默认）；list 可显式传入。 */
export function matchKnownIssue(text: string, list: KnownIssue[] = active): KnownIssue | null {
  const t = text.slice(0, 2000);
  return list.find((k) => k.match.test(t)) ?? null;
}

export function knownIssueText(k: KnownIssue, lang: BotLang): string {
  return lang === "zh" ? k.zh : k.en;
}

// ---- 控制台可维护：DATA_DIR/chatx_known_issues.json ----
// 内置条目是默认值；同 id 的记录可覆盖文案、追加关键词或停用。自定义条目只用关键词（按字面匹配，不接受正则）。

export type KnownIssueRec = { id: string; keywords: string[]; zh: string; en: string; enabled: boolean; updatedAt: string };
type KiStore = { items: KnownIssueRec[] };

const KI_FILE = process.env.CHATX_KNOWN_ISSUES_FILE || path.join(DATA_DIR, "chatx_known_issues.json");
const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const kwRe = (kws: string[]) => (kws.length ? new RegExp(kws.map(escRe).join("|"), "i") : null);

export class KnownIssueError extends Error {}

async function loadKi(): Promise<KiStore> {
  try {
    const raw = JSON.parse(await readFile(KI_FILE, "utf-8")) as Partial<KiStore>;
    return { items: Array.isArray(raw.items) ? raw.items : [] };
  } catch {
    return { items: [] };
  }
}

export function buildKnownIssues(items: KnownIssueRec[]): KnownIssue[] {
  const byId = new Map(items.map((r) => [r.id, r]));
  const out: KnownIssue[] = [];
  for (const b of KNOWN_ISSUES) {
    const r = byId.get(b.id);
    if (!r) {
      out.push(b);
      continue;
    }
    if (!r.enabled) continue;
    const extra = kwRe(r.keywords);
    out.push({ id: b.id, match: extra ? new RegExp(`${b.match.source}|${extra.source}`, "i") : b.match, zh: r.zh || b.zh, en: r.en || b.en });
  }
  for (const r of items) {
    if (!r.enabled || KNOWN_ISSUES.some((b) => b.id === r.id)) continue;
    const m = kwRe(r.keywords);
    if (m) out.push({ id: r.id, match: m, zh: r.zh, en: r.en || r.zh });
  }
  return out;
}

let active: KnownIssue[] = KNOWN_ISSUES;
let activeMtime = -1;

/** 按文件 mtime 刷新匹配用的缓存；读失败时保持内置默认。 */
export async function refreshKnownIssues(): Promise<KnownIssue[]> {
  try {
    const m = (await stat(KI_FILE)).mtimeMs;
    if (m !== activeMtime) {
      active = buildKnownIssues((await loadKi()).items);
      activeMtime = m;
    }
  } catch {
    active = KNOWN_ISSUES;
    activeMtime = -1;
  }
  return active;
}

/** 控制台展示：内置 + 自定义合并后的全部条目（含已停用）。 */
export async function listKnownIssueRows() {
  const items = (await loadKi()).items;
  const rows = KNOWN_ISSUES.map((b) => {
    const r = items.find((x) => x.id === b.id);
    return { id: b.id, builtin: true, pattern: b.match.source, keywords: r?.keywords ?? [], zh: r?.zh || b.zh, en: r?.en || b.en, enabled: r ? r.enabled : true, overridden: !!r, updatedAt: r?.updatedAt };
  });
  for (const r of items) if (!KNOWN_ISSUES.some((b) => b.id === r.id)) rows.push({ id: r.id, builtin: false, pattern: "", keywords: r.keywords, zh: r.zh, en: r.en, enabled: r.enabled, overridden: false, updatedAt: r.updatedAt });
  return rows;
}

let kiChain: Promise<unknown> = Promise.resolve();
function mutateKi<T>(fn: (s: KiStore) => T): Promise<T> {
  const run = kiChain.then(async () => {
    const s = await loadKi();
    const out = fn(s);
    await mkdir(path.dirname(KI_FILE), { recursive: true });
    const tmp = `${KI_FILE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(s, null, 1), "utf-8");
    await rename(tmp, KI_FILE);
    activeMtime = -1;
    return out;
  });
  kiChain = run.catch(() => undefined);
  return run;
}

export function parseKeywords(v: string): string[] {
  const out = Array.from(new Set(v.split(/[\n,，、]+/).map((x) => x.trim()).filter(Boolean)));
  if (out.length > 30) throw new KnownIssueError("关键词最多 30 个");
  if (out.some((k) => k.length < 2 || k.length > 60)) throw new KnownIssueError("每个关键词 2–60 个字符");
  return out;
}

export async function saveKnownIssue(input: { id: string; keywords: string; zh: string; en: string; enabled: boolean }): Promise<KnownIssueRec> {
  const id = input.id.trim().toLowerCase();
  if (!/^[a-z0-9_-]{2,32}$/.test(id)) throw new KnownIssueError("id 只能是 2–32 位小写字母、数字、- 或 _");
  const builtin = KNOWN_ISSUES.some((b) => b.id === id);
  const keywords = parseKeywords(input.keywords);
  const zh = input.zh.trim().slice(0, 600);
  const en = input.en.trim().slice(0, 600);
  if (!builtin && !keywords.length) throw new KnownIssueError("自定义条目至少要一个关键词");
  if (!builtin && !zh) throw new KnownIssueError("请填写中文解决步骤");
  const rec: KnownIssueRec = { id, keywords, zh, en, enabled: input.enabled, updatedAt: new Date().toISOString() };
  return mutateKi((s) => {
    s.items = s.items.filter((x) => x.id !== id).concat(rec);
    return rec;
  });
}

/** 删除自定义条目；对内置条目 = 恢复默认。 */
export async function removeKnownIssue(id: string): Promise<boolean> {
  return mutateKi((s) => {
    const n = s.items.length;
    s.items = s.items.filter((x) => x.id !== id);
    return s.items.length !== n;
  });
}
