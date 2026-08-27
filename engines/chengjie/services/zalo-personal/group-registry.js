/**
 * Zalo 群会话注册表（纯模块，node --test 可测）。
 *
 * 为什么存在：zca-js 的 sendMessage 必须显式区分 ThreadType.User / ThreadType.Group，
 * 而 Zalo 的 threadId 本身**不自描述**（不像 WhatsApp 的 @g.us 后缀）——上游调用方
 * （Python 编排器 send / autosend / 主动触达）大多不知道也不该知道这个事实。
 * 「这个 thread 是不是群」的权威知识在 Zalo 域内：本边车见过每条入站消息的
 * ThreadType，登录后还能 getAllGroups 全量拉取。故把该知识收拢在这里：
 *
 *   - 入站学习：ingest 见到 ThreadType.Group → remember()
 *   - 登录预热：attachAccount 后 getAllGroups() → primeFromResult()
 *   - 落盘持久：sessions/<account_id>/groups.json（边车重启不失忆）
 *   - 出站解析：resolveThreadType()——显式 chat_type 参数永远最高优先，
 *     未显式时按注册表命中判群，未命中回落 User（与修复前行为一致）。
 *
 * 修复的事故面（2026-08-19 P0）：Python 侧 ZaloPersonalWorker.send 从不传
 * chat_type → 群会话的回复全部按 ThreadType.User 发出（错目标/失败）。
 * 在最低层修（本模块），所有上游路径零改动同时受益。
 */

import fs from "fs";
import path from "path";

/** groups.json 的落点：与 context.json 同目录（一号一目录）。 */
export function groupsFile(sessionsDir, accountId) {
  return path.join(String(sessionsDir), String(accountId), "groups.json");
}

/** 从磁盘读某账号的已知群集合（坏文件/缺文件 → 空集，绝不抛）。 */
export function loadGroups(sessionsDir, accountId) {
  try {
    const raw = fs.readFileSync(groupsFile(sessionsDir, accountId), "utf8");
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr.map((x) => String(x)).filter(Boolean) : []);
  } catch {
    return new Set();
  }
}

/** 把集合写回磁盘（best-effort；失败静默——注册表只是加速层，不是账本）。 */
export function saveGroups(sessionsDir, accountId, set) {
  try {
    const dir = path.dirname(groupsFile(sessionsDir, accountId));
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(
      groupsFile(sessionsDir, accountId),
      JSON.stringify([...set].sort()),
      "utf8"
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * 从 zca-js getAllGroups() 的返回里抽群 id 列表（防御式：跨版本形状不稳）。
 * 已知/可能的形状：
 *   { gridVerMap: { "<groupId>": <ver>, ... } }   // v2 主流形状
 *   { groups: [ { groupId | id }, ... ] }
 *   [ "<groupId>", ... ] / [ { groupId | id }, ... ]
 * 抽不出来 → 空数组（宁可少记，入站学习会补）。
 */
export function extractGroupIds(res) {
  const out = [];
  const push = (v) => {
    const s = String(v == null ? "" : v).trim();
    if (s && !out.includes(s)) out.push(s);
  };
  const fromItem = (it) => {
    if (it == null) return;
    if (typeof it === "string" || typeof it === "number") return push(it);
    if (typeof it === "object") push(it.groupId ?? it.id ?? "");
  };
  if (res == null) return out;
  if (Array.isArray(res)) {
    res.forEach(fromItem);
    return out;
  }
  if (typeof res === "object") {
    const m = res.gridVerMap;
    if (m && typeof m === "object" && !Array.isArray(m)) {
      Object.keys(m).forEach(push);
      return out;
    }
    if (Array.isArray(res.groups)) {
      res.groups.forEach(fromItem);
      return out;
    }
  }
  return out;
}

/**
 * 出站线程类型解析（纯函数）。返回 "group" | "user"。
 * 优先级：显式 chat_type 参数（调用方最懂）→ 注册表命中 → User（保守回落）。
 */
export function resolveThreadType({ explicit, isKnownGroup }) {
  const s = String(explicit || "").trim().toLowerCase();
  if (s === "group") return "group";
  if (s === "user" || s === "private") return "user";
  return isKnownGroup ? "group" : "user";
}

/** 进程级注册表：内存缓存 + 懒加载磁盘 + 变更即持久。 */
export function createGroupRegistry({ sessionsDir }) {
  const cache = new Map(); // account_id -> Set(threadId)

  const setOf = (accountId) => {
    const key = String(accountId);
    let s = cache.get(key);
    if (!s) {
      s = loadGroups(sessionsDir, key);
      cache.set(key, s);
    }
    return s;
  };

  return {
    /** 入站见到群消息 → 记住（新条目才落盘）。 */
    remember(accountId, threadId) {
      const t = String(threadId || "").trim();
      if (!t) return false;
      const s = setOf(accountId);
      if (s.has(t)) return false;
      s.add(t);
      saveGroups(sessionsDir, accountId, s);
      return true;
    },
    /** 登录预热：getAllGroups 结果整批并入（有新条目才落盘一次）。 */
    primeFromResult(accountId, res) {
      const ids = extractGroupIds(res);
      if (!ids.length) return 0;
      const s = setOf(accountId);
      let added = 0;
      for (const id of ids) {
        if (!s.has(id)) {
          s.add(id);
          added += 1;
        }
      }
      if (added) saveGroups(sessionsDir, accountId, s);
      return added;
    },
    isGroup(accountId, threadId) {
      return setOf(accountId).has(String(threadId || "").trim());
    },
    sizeOf(accountId) {
      return setOf(accountId).size;
    },
  };
}
