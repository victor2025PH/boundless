/**
 * group-registry 纯模块门禁（node --test）。
 *
 * 守住 2026-08-19 P0 修复的三个不变量：
 *   1. 出站 ThreadType 解析优先级：显式参数 > 注册表 > User 回落；
 *   2. getAllGroups 跨版本形状都能抽出群 id（抽不出=空数组，绝不抛）；
 *   3. 注册表持久化：remember/prime 落盘、重建实例后仍认得（边车重启不失忆）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "fs";
import os from "os";
import path from "path";

import {
  createGroupRegistry,
  extractGroupIds,
  groupsFile,
  loadGroups,
  resolveThreadType,
  saveGroups,
} from "../group-registry.js";

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "zalo-groups-"));
}

test("resolveThreadType: 显式参数最高优先", () => {
  assert.equal(resolveThreadType({ explicit: "group", isKnownGroup: false }), "group");
  assert.equal(resolveThreadType({ explicit: "GROUP", isKnownGroup: false }), "group");
  assert.equal(resolveThreadType({ explicit: "user", isKnownGroup: true }), "user");
  assert.equal(resolveThreadType({ explicit: "private", isKnownGroup: true }), "user");
});

test("resolveThreadType: 未显式时按注册表，未命中回落 User", () => {
  assert.equal(resolveThreadType({ explicit: "", isKnownGroup: true }), "group");
  assert.equal(resolveThreadType({ explicit: null, isKnownGroup: false }), "user");
  // 未知字符串不当 group（宁可回落 User 也不误发群语义）
  assert.equal(resolveThreadType({ explicit: "channel", isKnownGroup: false }), "user");
});

test("extractGroupIds: gridVerMap / groups 数组 / 裸数组 / 对象数组 / 垃圾输入", () => {
  assert.deepEqual(
    extractGroupIds({ gridVerMap: { g1: 3, g2: 7 } }).sort(),
    ["g1", "g2"]
  );
  assert.deepEqual(
    extractGroupIds({ groups: [{ groupId: "a" }, { id: "b" }, "c"] }),
    ["a", "b", "c"]
  );
  assert.deepEqual(extractGroupIds(["x", 123, "x"]), ["x", "123"]);
  assert.deepEqual(extractGroupIds(null), []);
  assert.deepEqual(extractGroupIds("nonsense"), []);
  assert.deepEqual(extractGroupIds({ whatever: true }), []);
});

test("registry: remember 新条目落盘，重建实例仍认得", () => {
  const dir = tmpDir();
  const reg = createGroupRegistry({ sessionsDir: dir });
  assert.equal(reg.isGroup("acct1", "111"), false);
  assert.equal(reg.remember("acct1", "111"), true);
  assert.equal(reg.remember("acct1", "111"), false); // 已知条目不重复落盘
  assert.equal(reg.isGroup("acct1", "111"), true);
  // 磁盘上真的有
  assert.ok(fs.existsSync(groupsFile(dir, "acct1")));
  // 全新实例（模拟边车重启）→ 懒加载磁盘 → 仍认得
  const reg2 = createGroupRegistry({ sessionsDir: dir });
  assert.equal(reg2.isGroup("acct1", "111"), true);
  assert.equal(reg2.isGroup("acct1", "222"), false);
  assert.equal(reg2.isGroup("acct2", "111"), false); // 账号间隔离
});

test("registry: primeFromResult 整批并入且只算新增", () => {
  const dir = tmpDir();
  const reg = createGroupRegistry({ sessionsDir: dir });
  reg.remember("a", "g1");
  const added = reg.primeFromResult("a", { gridVerMap: { g1: 1, g2: 2, g3: 3 } });
  assert.equal(added, 2);
  assert.equal(reg.sizeOf("a"), 3);
  assert.equal(reg.primeFromResult("a", { gridVerMap: { g2: 9 } }), 0);
});

test("loadGroups/saveGroups: 坏文件回空集，写读往返一致", () => {
  const dir = tmpDir();
  fs.mkdirSync(path.join(dir, "bad"), { recursive: true });
  fs.writeFileSync(groupsFile(dir, "bad"), "{not json", "utf8");
  assert.equal(loadGroups(dir, "bad").size, 0);
  const s = new Set(["z2", "z1"]);
  assert.equal(saveGroups(dir, "ok", s), true);
  assert.deepEqual([...loadGroups(dir, "ok")].sort(), ["z1", "z2"]);
});
