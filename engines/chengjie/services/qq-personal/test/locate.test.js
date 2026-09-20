// QQ 定位 / 版本锁定 / 下载状态机的纯逻辑测试（不打网络、不装 QQ）。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { isSupportedBuild, locateQQ, PINNED_QQ_BUILD, SUPPORTED_QQ_BUILDS } from "../ntq/locate.js";
import { downloadState, startDownload } from "../ntq/qq-download.js";

function fakeQQ(dir, version) {
  fs.mkdirSync(path.join(dir, "versions", version, "resources", "app"), { recursive: true });
  fs.writeFileSync(path.join(dir, "QQ.exe"), "MZ");
  fs.writeFileSync(path.join(dir, "versions", "config.json"), JSON.stringify({ curVersion: version, baseVersion: version }));
  fs.writeFileSync(path.join(dir, "versions", version, "resources", "app", "wrapper.node"), "node");
}

test("支持表：锁定版在表里，未知 build 不在", () => {
  assert.ok(isSupportedBuild(PINNED_QQ_BUILD));
  assert.ok(SUPPORTED_QQ_BUILDS[PINNED_QQ_BUILD].url.startsWith("https://dldir1"));
  assert.equal(isSupportedBuild("1"), false);
  assert.equal(isSupportedBuild(""), false);
});

test("runtimeDir 优先于注册表/默认路径，并解析版本与 wrapper.node", { skip: process.platform !== "win32" }, () => {
  const rt = fs.mkdtempSync(path.join(os.tmpdir(), "qqrt-"));
  fakeQQ(rt, "9.9.26-44343");
  const r = locateQQ({ runtimeDir: rt });
  assert.equal(r.installed, true);
  assert.equal(r.source, "runtime");
  assert.equal(r.version, "9.9.26-44343");
  assert.equal(r.build, "44343");
  assert.ok(r.wrapper.endsWith(path.join("resources", "app", "wrapper.node")));
  fs.rmSync(rt, { recursive: true, force: true });
});

test("下载状态机：缺 runtimeDir → error；已装同版本 → done 且不下载", () => {
  const e = startDownload({ runtimeDir: "" });
  assert.equal(e.phase, "error");
  const rt = fs.mkdtempSync(path.join(os.tmpdir(), "qqrt2-"));
  fakeQQ(rt, SUPPORTED_QQ_BUILDS[PINNED_QQ_BUILD].version);
  const d = startDownload({ runtimeDir: rt, logger: { info() {}, warn() {} } });
  assert.equal(d.phase, "done");
  assert.equal(d.percent, 100);
  assert.equal(downloadState().build, PINNED_QQ_BUILD);
  fs.rmSync(rt, { recursive: true, force: true });
});

test("未知 build → error（不会去下载未验证版本）", () => {
  const rt = fs.mkdtempSync(path.join(os.tmpdir(), "qqrt3-"));
  const d = startDownload({ runtimeDir: rt, build: "999999" });
  assert.equal(d.phase, "error");
  assert.match(d.error, /unsupported build/);
  fs.rmSync(rt, { recursive: true, force: true });
});
