"use strict";
/**
 * ChatX 热补丁决策核（零 Electron 依赖，Node 直跑可测）。
 *
 * 和 update-notify.js 同一约束：main.js 负责 IO（拉 hotpatch.json、下 zip、
 * 校验、退出后换文件），本模块只回答「能不能套、套哪份、路径安不安全」。
 *
 * 与全量 electron-updater 的关系：
 *   - latest.yml 上有更新的 semver → 走整包，热补丁让路
 *   - 同一 baseAppVersion 且远端 patch > 本地 → 套热补丁
 *   - base 对不上 → 拒绝（防止把 1.0.56 的 asar 套到 1.0.55 上）
 */

const ALLOWED = [
  /^app\.asar$/,
  /^app\.asar\.unpacked\//,
  /^build-info\.json$/,
  /^hotpatch\.json$/,
  /^shared\//,
  /^services\//,
  /^seed-data\//,
  /^backend\//,
];

function normalizeRel(rel) {
  return String(rel || "").replace(/\\/g, "/").replace(/^\/+/, "").trim();
}

function isSafeRelPath(rel) {
  const p = normalizeRel(rel);
  if (!p) return false;
  if (p.includes("..") || p.includes(":") || p.startsWith("/")) return false;
  if (/ffmpeg/i.test(p)) return false;
  return ALLOWED.some((rx) => rx.test(p));
}

function toIntPatch(v) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function normalizeManifest(raw) {
  const o = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const filesIn = Array.isArray(o.files) ? o.files : [];
  const files = [];
  const seen = new Set();
  for (const f of filesIn) {
    if (!f || typeof f !== "object") continue;
    const path = normalizeRel(f.path);
    if (!isSafeRelPath(path) || seen.has(path)) continue;
    seen.add(path);
    files.push({
      path,
      sha256: String(f.sha256 || "").toLowerCase(),
      bytes: Number(f.bytes) || 0,
    });
  }
  return {
    schema: Number(o.schema) || 1,
    kind: String(o.kind || ""),
    id: String(o.id || "").trim(),
    baseAppVersion: String(o.baseAppVersion || "").trim(),
    patch: toIntPatch(o.patch),
    fixes: Array.isArray(o.fixes) ? o.fixes.map(String) : [],
    notes: String(o.notes || "").trim(),
    zip: String(o.zip || "").trim(),
    sha256: String(o.sha256 || "").toLowerCase(),
    size_bytes: Number(o.size_bytes) || 0,
    files,
  };
}

function readLocalPatch(raw) {
  const o = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    baseAppVersion: String(o.baseAppVersion || "").trim(),
    patch: toIntPatch(o.patch),
  };
}

/**
 * @param {object} args
 *   appVersion: 当前安装包 semver（app.getVersion()）
 *   local: {baseAppVersion, patch} 本机 resources/hotpatch.json
 *   remote: normalizeManifest 产物
 *   latestFullVersion?: latest.yml 上的全量版本；比 appVersion 新则让路
 * @returns {{action:"skip"|"apply"|"mismatch"|"defer_full", reason:string, remote?:object}}
 */
function decide(args) {
  const a = args || {};
  const appVersion = String(a.appVersion || "").trim();
  const local = readLocalPatch(a.local);
  const remote = a.remote && a.remote.kind ? a.remote : normalizeManifest(a.remote);
  const latestFull = String(a.latestFullVersion || "").trim();

  if (latestFull && appVersion && latestFull !== appVersion) {
    const cmp = _cmpSemver(latestFull, appVersion);
    if (cmp > 0) return { action: "defer_full", reason: "full_update_available", remote };
  }
  if (!remote || remote.kind !== "chatx_hotpatch") {
    return { action: "skip", reason: "not_a_hotpatch" };
  }
  if (!remote.baseAppVersion || remote.patch < 1) {
    return { action: "skip", reason: "incomplete_manifest" };
  }
  if (!remote.files.length) return { action: "skip", reason: "empty_files" };
  if (appVersion && remote.baseAppVersion !== appVersion) {
    return { action: "mismatch", reason: "base_mismatch", remote };
  }
  if (local.baseAppVersion && local.baseAppVersion !== remote.baseAppVersion) {
    // 本机热补丁记录是上一档安装包留下的，按 0 视作未打过
  }
  const localPatch = local.baseAppVersion === remote.baseAppVersion ? local.patch : 0;
  if (localPatch > remote.patch) return { action: "skip", reason: "local_newer", remote };
  if (localPatch === remote.patch) return { action: "skip", reason: "already_applied", remote };
  return { action: "apply", reason: "newer_patch", remote };
}

function displayLabel(appVersion, patch) {
  const v = String(appVersion || "").trim() || "?";
  const p = toIntPatch(patch);
  return p > 0 ? `${v}+p${p}` : v;
}

function _cmpSemver(a, b) {
  const pa = String(a || "").split(".");
  const pb = String(b || "").split(".");
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = parseInt(pa[i], 10) || 0;
    const y = parseInt(pb[i], 10) || 0;
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

function hotpatchUrlFromPublish(publishUrl) {
  const base = String(publishUrl || "").replace(/\/+$/, "");
  return base ? base + "/hotpatch.json" : "";
}

module.exports = {
  isSafeRelPath,
  normalizeRel,
  normalizeManifest,
  readLocalPatch,
  decide,
  displayLabel,
  hotpatchUrlFromPublish,
};
