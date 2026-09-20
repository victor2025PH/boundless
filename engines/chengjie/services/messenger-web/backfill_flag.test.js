// Q-31 D（#317 PUUWJB）：Messenger 边车 backfill 推送体必须带 backfill 标。
// 运行：cd services/messenger-web && node --test backfill_flag.test.js
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const src = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "server.js"),
  "utf8",
);

test("backfillStep push body marks backfill + msg_backfill source", () => {
  const i = src.indexOf("async function backfillStep");
  assert.ok(i >= 0, "backfillStep missing");
  const chunk = src.slice(i, i + 2800);
  assert.match(chunk, /PY_THREAD_HISTORY_URL/);
  assert.match(chunk, /backfill\s*:\s*true/);
  assert.match(chunk, /backfill_source\s*:\s*["']msg_backfill["']/);
});
