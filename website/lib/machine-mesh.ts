import net from "net";

// 五机台账静态副本 —— website 部署通常不含 deploy/machines.json，故摘关键字段内置于此。
// 单一源仍是仓库根 deploy/machines.json；改机器时请同步本文件关键字段。
// 可选：环境变量 MACHINE_MESH_JSON 覆盖整表（JSON 数组，形状同 MachineMeshEntry）。
//
// 本模块不做真 SSH / 内网探测：192.168.x 仅内网可见，运维探活见 docs/MACHINE_SSH.md
// 与 tools/cluster_ping.ps1。可选 probeLocalEndpoints 只打本机 localhost 已知端口。

export interface MachineService {
  name: string;
  /** 服务声明端口；无端口声明时为 null。 */
  port: number | null;
}

export interface MachineMeshEntry {
  id: string;
  zh: string;
  ip: string;
  role: "dev" | "compute" | string;
  products: string[];
  primaryBrand: string;
  services: MachineService[];
  note?: string;
}

/** 从 "avatar_hub:9000" / "fish_tts" 解析主服务名与端口。 */
export function parseServiceSpec(spec: string): MachineService {
  const i = spec.lastIndexOf(":");
  if (i > 0) {
    const port = Number(spec.slice(i + 1));
    if (Number.isFinite(port) && port > 0) {
      return { name: spec.slice(0, i), port };
    }
  }
  return { name: spec, port: null };
}

/** 内置六机关键台账（与 deploy/machines.json v2 对齐，2026-08-05 功能命名改版）。 */
export const MACHINE_MESH_BUILTIN: MachineMeshEntry[] = [
  {
    id: "zhongshu",
    zh: "中枢",
    ip: "192.168.0.176",
    role: "hub",
    products: ["幻声", "幻影", "幻颜", "通传"],
    primaryBrand: "voicex",
    services: [
      "avatar_hub:9000", "faceswap:8000", "live_interpreter:7900",
      "fish_tts:7855", "sbv2_tts:7861", "ditto:8096", "musetalk_lipsync:8090",
    ].map(parseServiceSpec),
    note: "集群大脑：Hub + 换脸主引擎 + 同传 + 渲染链（旧名 幻声/huansheng）",
  },
  {
    id: "yunsheng",
    zh: "韵声",
    ip: "192.168.0.173",
    role: "compute",
    products: ["幻声"],
    primaryBrand: "voicex",
    services: ["index_tts:7865", "emotion_tts:7852", "ollama_llm:11434"].map(parseServiceSpec),
    note: "质量轨 TTS + LLM 容灾（旧名 幻影/huanying）",
  },
  {
    id: "shengbei",
    zh: "声备",
    ip: "192.168.0.117",
    role: "dev",
    products: ["通译", "智聊"],
    primaryBrand: "lingox",
    services: ["qwen3_tts:7858", "emotion_tts_standby:7852"].map(parseServiceSpec),
    note: "声音热备 + 智聊坐席开发（旧名 通译/tongyi）",
  },
  {
    id: "lianbei",
    zh: "脸备",
    ip: "192.168.0.104",
    role: "compute",
    products: ["幻颜"],
    primaryBrand: "facex",
    services: ["faceswap_standby:8000"].map(parseServiceSpec),
    note: "换脸热备（2026-08-02 主引擎收编中枢；旧名 幻颜节点/huanyan-node）",
  },
  {
    id: "tingxie",
    zh: "听写",
    ip: "192.168.0.140",
    role: "compute",
    products: ["通传"],
    primaryBrand: "voxx",
    services: ["stt:7854", "nemo_stt:7857"].map(parseServiceSpec),
    note: "STT 听写专机（旧名 通传节点/tongchuan-node）",
  },
  {
    id: "kouxing",
    zh: "口型",
    ip: "192.168.0.198",
    role: "dev",
    products: ["智拓"],
    primaryBrand: "reachx",
    services: ["lipsync_standby:8090"].map(parseServiceSpec),
    note: "口型热备 + 获客开发（旧名 智拓/zhituo）",
  },
];

function normalizeEntry(raw: Record<string, unknown>): MachineMeshEntry | null {
  const id = String(raw.id ?? "").trim();
  const zh = String(raw.zh ?? "").trim();
  const ip = String(raw.ip ?? "").trim();
  if (!id || !ip) return null;
  const servicesRaw = raw.services;
  let services: MachineService[] = [];
  if (Array.isArray(servicesRaw)) {
    services = servicesRaw.map((s) => {
      if (typeof s === "string") return parseServiceSpec(s);
      if (s && typeof s === "object") {
        const o = s as Record<string, unknown>;
        const name = String(o.name ?? "");
        const port = o.port == null ? null : Number(o.port);
        return { name, port: Number.isFinite(port) && (port as number) > 0 ? (port as number) : null };
      }
      return { name: String(s), port: null };
    });
  }
  return {
    id,
    zh: zh || id,
    ip,
    role: String(raw.role ?? "dev"),
    products: Array.isArray(raw.products) ? raw.products.map(String) : [],
    primaryBrand: String(raw.primaryBrand ?? raw.primary_brand ?? ""),
    services,
    note: raw.note != null ? String(raw.note) : undefined,
  };
}

/** 读取五机台账：MACHINE_MESH_JSON 覆盖 → 否则内置副本。 */
export function getMachineMesh(): MachineMeshEntry[] {
  const raw = process.env.MACHINE_MESH_JSON?.trim();
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as unknown;
      const arr = Array.isArray(parsed)
        ? parsed
        : parsed && typeof parsed === "object" && Array.isArray((parsed as { machines?: unknown }).machines)
          ? (parsed as { machines: unknown[] }).machines
          : null;
      if (arr) {
        const rows = arr
          .map((x) => (x && typeof x === "object" ? normalizeEntry(x as Record<string, unknown>) : null))
          .filter((x): x is MachineMeshEntry => !!x);
        if (rows.length) return rows;
      }
    } catch {
      /* 坏 JSON 回退内置 */
    }
  }
  return MACHINE_MESH_BUILTIN;
}

/** 私网 RFC1918：控制台若部署在公网 VPS，无法直连这些 IP。 */
export function isPrivateLanIp(ip: string): boolean {
  const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(ip.trim());
  if (!m) return false;
  const a = Number(m[1]);
  const b = Number(m[2]);
  if (a === 10) return true;
  if (a === 192 && b === 168) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  return false;
}

export interface LocalProbeResult {
  target: string;
  ok: boolean;
  ms: number;
  detail: string;
}

/** TCP 探活 127.0.0.1:port（比 fetch 更稳，Windows 上不会拖死事件循环）。 */
function probeTcpLocal(port: number, timeoutMs: number): Promise<{ ok: boolean; detail: string; ms: number }> {
  return new Promise((resolve) => {
    const started = Date.now();
    const socket = new net.Socket();
    let settled = false;
    const finish = (ok: boolean, detail: string) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve({ ok, detail, ms: Date.now() - started });
    };
    socket.setTimeout(timeoutMs);
    socket.once("connect", () => finish(true, "tcp open"));
    socket.once("timeout", () => finish(false, "timeout"));
    socket.once("error", (e) => finish(false, (e.message || "error").slice(0, 80)));
    socket.connect(port, "127.0.0.1");
  });
}

/** 对本机 localhost 已知端口做短超时探测（不扫内网 IP）。 */
export async function probeLocalEndpoints(
  mesh: MachineMeshEntry[] = getMachineMesh(),
  timeoutMs = 400
): Promise<LocalProbeResult[]> {
  const ports = new Map<number, string>();
  // 本站默认开发端口
  ports.set(3000, "website");
  for (const m of mesh) {
    for (const s of m.services) {
      if (s.port != null && !ports.has(s.port)) {
        ports.set(s.port, `${m.zh}/${s.name}`);
      }
    }
  }

  const out: LocalProbeResult[] = [];
  for (const [port, label] of ports) {
    const r = await probeTcpLocal(port, timeoutMs);
    out.push({
      target: `localhost:${port} (${label})`,
      ok: r.ok,
      ms: r.ms,
      detail: r.detail,
    });
  }
  return out;
}
