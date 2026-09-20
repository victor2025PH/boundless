import { mkdir, readFile, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "@/lib/data-dir";
import { normalizeFingerprint } from "@/lib/ai-gateway";

// 远程诊断请求存储（实施51 抽单源，2026-08-21）：原先 loadReqs/saveReqs 长在
// app/api/diag-request/route.ts 里；quota 路由要做「额度轮询捎带 diag_requested 位」
// （客户端见位即触发既有 check_remote_diag，把远程拉取时延从 1 小时压到 ~1 分钟），
// 两处各读各的 JSON 必然漂移——抽到 lib 单源。存储形状/过期语义原样搬迁零变化：
// DATA_DIR/diag/requests.json 单文件小字典（诊断请求量级=个位数/天），7 天过期。

const DIAG_DIR = path.join(DATA_DIR, "diag");
const REQ_FILE = path.join(DIAG_DIR, "requests.json");
const KEEP_MS = 7 * 86400_000; // 请求 7 天过期：过期不清会让老请求突然触发上传

export type DiagRequest = {
  request_id: string;
  fp: string;
  note: string;
  t: string; // created ISO
  status: "pending" | "done";
  code?: string;
  done_t?: string;
};

export async function loadDiagRequests(): Promise<DiagRequest[]> {
  try {
    const arr = JSON.parse(await readFile(REQ_FILE, "utf-8"));
    if (!Array.isArray(arr)) return [];
    const now = Date.now();
    return arr.filter(
      (r: DiagRequest) => now - Date.parse(r.t || "") < KEEP_MS,
    );
  } catch {
    return [];
  }
}

export async function saveDiagRequests(arr: DiagRequest[]) {
  await mkdir(DIAG_DIR, { recursive: true });
  await writeFile(REQ_FILE, JSON.stringify(arr, null, 1));
}

/**
 * 是否存在针对该指纹的 pending 取包请求（quota 捎带位的唯一判据）。
 * 指纹两侧都过 normalizeFingerprint 再比——客服 create 时录入的 fp 与设备令牌
 * claims.mid 的归一化形态可能差大小写/杂字符，裸串比对会静默漏报。
 */
export async function hasPendingDiag(fp: string): Promise<boolean> {
  const want = normalizeFingerprint(fp);
  if (!want) return false;
  const reqs = await loadDiagRequests();
  return reqs.some(
    (r) => r.status === "pending" && normalizeFingerprint(r.fp) === want,
  );
}
