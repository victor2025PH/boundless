// Q-24（#298）：Messenger 代发链止血纯函数门禁 + DXAPAX 四连败回放（零静默失败）。
// 运行：cd services/messenger-web && node --test
import test from "node:test";
import assert from "node:assert/strict";
import {
  SEND_FAIL_CODES, isDetachedError, isCallOverlayText, normalizeSendFailCode, sendFailBody,
  sendFailHttpStatus, inputWithRebind, bumpJidFailStreak, shouldQueueRetry, retryQueueUpsert,
  retryQueueDue, retryQueueSettle, RETRY_QUEUE_THRESHOLD, RETRY_QUEUE_MAX_TRIES,
  REACTION_ARIA_RE, parseReactionPill,
} from "./send_chain.js";

const DETACHED = "elementHandle.click: Element is not attached to the DOM";

test("七码契约冻结：数量 + 命名与指令 B 段一致", () => {
  assert.deepEqual([...SEND_FAIL_CODES].sort(), [
    "call_overlay", "composer_detached", "e2ee_pin_pending", "login_expired",
    "send_backoff", "thread_not_found", "upload_failed",
  ]);
});

test("isDetachedError：Playwright 失联族判定", () => {
  assert.equal(isDetachedError(new Error(DETACHED)), true);
  assert.equal(isDetachedError("Execution context was destroyed, most likely because of a navigation"), true);
  assert.equal(isDetachedError("elementHandle.type: Element is not stable"), true);
  assert.equal(isDetachedError("Timeout 30000ms exceeded"), false);
  assert.equal(isDetachedError(null), false);
});

test("normalizeSendFailCode：legacy reason / 异常文本 → 七码", () => {
  assert.equal(normalizeSendFailCode({ reason: "not_logged_in" }), "login_expired");
  assert.equal(normalizeSendFailCode({ status: 404 }), "login_expired");
  assert.equal(normalizeSendFailCode({ reason: "e2ee_pin_pending" }), "e2ee_pin_pending");
  assert.equal(normalizeSendFailCode({ message: "e2ee recovery pin prompt on screen" }), "e2ee_pin_pending");
  assert.equal(normalizeSendFailCode({ reason: "account_blocked" }), "send_backoff");
  assert.equal(normalizeSendFailCode({ reason: "send_backoff" }), "send_backoff");
  assert.equal(normalizeSendFailCode({ message: "Ongoing call" }), "call_overlay");
  assert.equal(normalizeSendFailCode({ reason: "file_chooser_timeout" }), "upload_failed");
  assert.equal(normalizeSendFailCode({ reason: "page_not_rendered" }), "thread_not_found");
  assert.equal(normalizeSendFailCode({ message: "page.goto: net::ERR_CONNECTION_RESET" }), "thread_not_found");
  // DXAPAX 实锤：exception 分支的 detached 文本必须落 composer_detached，而不是泛 500
  assert.equal(normalizeSendFailCode({ reason: "exception", message: DETACHED }), "composer_detached");
  assert.equal(normalizeSendFailCode({}), "composer_detached");
  assert.equal(isCallOverlayText("张三 正在通话"), true);
  assert.equal(isCallOverlayText("hello"), false);
});

test("sendFailBody：形状固定 + reason_code 兼容老 Python + retry_after_ms 数值化", () => {
  const b = sendFailBody({ code: "send_backoff", reason: "backoff_window", detail: "40s left",
    retryAfterMs: 40000.7, retries: 4, extra: { manual: true } });
  assert.equal(b.ok, false);
  assert.equal(b.delivered, false);
  assert.equal(b.code, "send_backoff");
  assert.equal(b.reason, "backoff_window");
  assert.equal(b.reason_code, "backoff_window");
  assert.equal(b.retry_after_ms, 40000);
  assert.equal(b.retries, 4);
  assert.equal(b.manual, true);
  assert.equal(typeof b.error, "string");
  // 认不出的 code 走归一，不会漏出契约外的码
  assert.equal(sendFailBody({ code: "weird", detail: DETACHED }).code, "composer_detached");
  assert.equal(sendFailHttpStatus("login_expired"), 404);
  assert.equal(sendFailHttpStatus("send_backoff"), 429);
  assert.equal(sendFailHttpStatus("e2ee_pin_pending"), 503);
  assert.equal(sendFailHttpStatus("composer_detached"), 500);
});

// ── A 段：输入重绑编排 ─────────────────────────────────────────────────────────

function makeHarness(inputScript, { requeryOk = true, renav = { box: {}, reason: "" } } = {}) {
  const calls = { input: 0, clear: 0, requery: 0, renavigate: 0, logs: [] };
  const script = [...inputScript];
  return {
    calls,
    opts: {
      input: async () => {
        calls.input += 1;
        const step = script.shift();
        if (step === "detached") throw new Error(DETACHED);
        if (step === "other") throw new Error("Timeout 30000ms exceeded");
      },
      clear: async () => { calls.clear += 1; },
      requery: async () => { calls.requery += 1; return requeryOk; },
      renavigate: async () => { calls.renavigate += 1; return renav; },
      log: (stage) => calls.logs.push(stage),
    },
  };
}

test("inputWithRebind：首击成功 → 不重查不导航", async () => {
  const h = makeHarness(["ok"]);
  const r = await inputWithRebind(h.opts);
  assert.deepEqual(r, { attempts: 1, renavigated: false, accepted: false });
  assert.equal(h.calls.requery, 0);
  assert.equal(h.calls.renavigate, 0);
});

test("inputWithRebind：detached 一次 → 同线程重查重试成功（不 re-navigate）", async () => {
  const h = makeHarness(["detached", "ok"]);
  const r = await inputWithRebind(h.opts);
  assert.equal(r.attempts, 2);
  assert.equal(r.renavigated, false);
  assert.equal(h.calls.clear, 1, "重试前必清残留输入（防叠字双发）");
  assert.equal(h.calls.requery, 1);
  assert.equal(h.calls.renavigate, 0);
  assert.deepEqual(h.calls.logs, ["detached_retry_inthread"]);
});

test("inputWithRebind：detached 两次 → re-navigate 一次后成功", async () => {
  const h = makeHarness(["detached", "detached", "ok"], { renav: { box: {}, reason: "", accepted: true } });
  const r = await inputWithRebind(h.opts);
  assert.equal(r.attempts, 3);
  assert.equal(r.renavigated, true);
  assert.equal(r.accepted, true);
  assert.equal(h.calls.renavigate, 1);
});

test("inputWithRebind：三连 detached → 抛 sendCode=composer_detached（不再撞第四次）", async () => {
  const h = makeHarness(["detached", "detached", "detached", "ok"]);
  await assert.rejects(inputWithRebind(h.opts), (e) => {
    assert.equal(e.sendCode, "composer_detached");
    assert.equal(e.sendReason, "composer_detached");
    return true;
  });
  assert.equal(h.calls.input, 3);
  assert.equal(h.calls.renavigate, 1);
});

test("inputWithRebind：重导航后 composer 仍没有 → code 归一自 recover reason", async () => {
  const h = makeHarness(["detached", "detached"], { renav: { box: null, reason: "page_not_rendered" } });
  await assert.rejects(inputWithRebind(h.opts), (e) => {
    assert.equal(e.sendCode, "thread_not_found");
    return true;
  });
});

test("inputWithRebind：非 detached 错误原样上抛，不触发重绑", async () => {
  const h = makeHarness(["other"]);
  await assert.rejects(inputWithRebind(h.opts), /Timeout 30000ms/);
  assert.equal(h.calls.requery, 0);
  assert.equal(h.calls.renavigate, 0);
});

// ── 连败 + 待重试队列 ─────────────────────────────────────────────────────────

test("bumpJidFailStreak / shouldQueueRetry：阈值 2，仅瞬态码排队", () => {
  const m = new Map();
  assert.equal(bumpJidFailStreak(m, "j1", false), 1);
  assert.equal(shouldQueueRetry({ streak: 1, code: "composer_detached" }), false);
  assert.equal(bumpJidFailStreak(m, "j1", false), 2);
  assert.equal(shouldQueueRetry({ streak: 2, code: "composer_detached" }), true);
  assert.equal(shouldQueueRetry({ streak: 5, code: "login_expired" }), false);
  assert.equal(shouldQueueRetry({ streak: 5, code: "e2ee_pin_pending" }), false);
  assert.equal(shouldQueueRetry({ streak: 5, code: "send_backoff" }), false);
  assert.equal(shouldQueueRetry({ streak: 2, code: "thread_not_found" }), true);
  assert.equal(bumpJidFailStreak(m, "j1", true), 0);
  assert.equal(m.has("j1"), false);
  assert.equal(RETRY_QUEUE_THRESHOLD, 2);
});

test("retryQueue：upsert 同会话覆盖、到点筛选、settle 三次终局", () => {
  const q = new Map();
  const t0 = 1_000_000;
  const a = retryQueueUpsert(q, "j1", { text: "old", code: "composer_detached", now: t0, intervalMs: 60000 });
  assert.equal(a.queued, true);
  assert.equal(a.replaced, false);
  const b = retryQueueUpsert(q, "j1", { text: "new", code: "composer_detached", now: t0, intervalMs: 60000 });
  assert.equal(b.replaced, true);
  assert.equal(q.get("j1").text, "new", "改稿后旧稿不再补发");
  assert.equal(retryQueueUpsert(q, "j2", { text: "", now: t0 }).queued, false, "空文案不排");
  assert.equal(retryQueueDue(q, t0 + 59_999).length, 0);
  assert.equal(retryQueueDue(q, t0 + 60_000).length, 1);

  let s = retryQueueSettle(q, "j1", { ok: false, error: DETACHED, now: t0 + 60_000, intervalMs: 60000 });
  assert.deepEqual([s.done, s.final, s.item.tries], [false, false, 1]);
  assert.equal(s.item.nextAt, t0 + 120_000);
  s = retryQueueSettle(q, "j1", { ok: false, error: DETACHED, now: t0 + 120_000 });
  assert.deepEqual([s.done, s.final, s.item.tries], [false, false, 2]);
  s = retryQueueSettle(q, "j1", { ok: false, error: DETACHED, now: t0 + 180_000 });
  assert.deepEqual([s.done, s.final, s.item.tries], [true, true, RETRY_QUEUE_MAX_TRIES]);
  assert.equal(q.has("j1"), false);

  retryQueueUpsert(q, "j3", { text: "x", now: t0 });
  s = retryQueueSettle(q, "j3", { ok: true });
  assert.deepEqual([s.done, s.final], [true, false]);
  assert.equal(q.has("j3"), false);
});

// ── G 段：DXAPAX 四连败回放 ─────────────────────────────────────────────────────
// 2026-09-12 07:20–07:22 同一会话 4 次 /send 全部 500「Element is not attached to the DOM」，
// 边车侧只有 500，Python 侧只有「发送失败」，坐席端零提示、40s backoff 静默。
// 回放要求：四次里每一次都有结构化 code；第 2 次起进待重试队列 + 铃铛事件；零静默路径。

test("DXAPAX 四连败回放：每次失败结构化 + 第2次起排队/铃铛 + 重试成功出队", async () => {
  const jid = "100012345678901";
  const streak = new Map();
  const queue = new Map();
  const bells = [];
  const responses = [];
  let now = 1_757_600_000_000;

  // 边车 /send 失败分支的编排（与 server.failSend 同序：归码 → 连败 → 排队 → 铃铛 → 结构化体）
  const failSend = (err) => {
    const detail = String(err.message || err);
    const code = err.sendCode || normalizeSendFailCode({ reason: "exception", message: detail });
    const n = bumpJidFailStreak(streak, jid, false);
    let queued = false;
    if (shouldQueueRetry({ streak: n, code })) {
      const up = retryQueueUpsert(queue, jid, { text: "hello", manual: true, code, now });
      queued = up.queued;
      bells.push({ status: "send_stuck", code, streak: n });
    }
    const body = sendFailBody({ code, reason: err.sendReason || "exception", detail,
      retries: n, extra: { queued, streak: n } });
    responses.push({ status: sendFailHttpStatus(code), body });
    return body;
  };

  for (let i = 0; i < 4; i++) {
    // 每次坐席点发送：三连 detached（重查 + 重导航都没救回）→ composer_detached 终局
    const h = makeHarness(["detached", "detached", "detached"]);
    try {
      await inputWithRebind(h.opts);
      assert.fail("should have thrown");
    } catch (e) {
      failSend(e);
    }
    now += 30_000;
  }

  assert.equal(responses.length, 4);
  for (const r of responses) {
    assert.equal(r.body.ok, false);
    assert.equal(r.body.code, "composer_detached", "零静默：每次失败都有七码之一");
    assert.equal(typeof r.body.detail, "string");
    assert.ok(r.body.detail.length > 0);
    assert.equal(r.status, 500);
  }
  assert.deepEqual(responses.map((r) => r.body.queued), [false, true, true, true]);
  assert.deepEqual(responses.map((r) => r.body.streak), [1, 2, 3, 4]);
  assert.equal(bells.length, 3, "第 2/3/4 次连败各响一次铃铛（send_stuck）");
  assert.equal(queue.size, 1, "同会话只留一条待重试");
  assert.equal(queue.get(jid).text, "hello");

  // 60s 后后台重试：这次 composer 稳定 → 成功 → 出队 + 连败清零
  const due = retryQueueDue(queue, now + 60_000);
  assert.equal(due.length, 1);
  const h = makeHarness(["ok"]);
  const r = await inputWithRebind(h.opts);
  assert.equal(r.attempts, 1);
  const s = retryQueueSettle(queue, jid, { ok: true });
  assert.equal(s.done, true);
  assert.equal(bumpJidFailStreak(streak, jid, true), 0);
  assert.equal(queue.size, 0);
});

test("DXAPAX 变体：退避窗内直接 send_backoff 也必须结构化（不再 500 无码）", () => {
  const body = sendFailBody({ code: "send_backoff", reason: "backoff", detail: "consecutive failures",
    retryAfterMs: 40000, retries: 4 });
  assert.equal(body.code, "send_backoff");
  assert.equal(body.retry_after_ms, 40000);
  assert.equal(shouldQueueRetry({ streak: 4, code: body.code }), false, "退避码不入队：等窗到期");
});

// ── D 段：反应胶囊 ─────────────────────────────────────────────────────────────

test("REACTION_ARIA_RE：覆盖 messenger.com 当前反应胶囊 aria（P7P8FY 丢失原因）", () => {
  assert.equal(REACTION_ARIA_RE.test("See who reacted to this message"), true);
  assert.equal(REACTION_ARIA_RE.test("查看回应"), true);
  assert.equal(REACTION_ARIA_RE.test("张三用👍回应了"), true);
  assert.equal(REACTION_ARIA_RE.test("Double tap to like"), true);
  assert.equal(REACTION_ARIA_RE.test("Send a like"), false);
});

test("parseReactionPill：emoji 抽取 + 发送者推断 + 关键词回退", () => {
  assert.deepEqual(parseReactionPill("See who reacted to this message", "👍 1", "peer"),
    { emoji: "👍", sender: "peer" });
  assert.deepEqual(parseReactionPill("你用❤️回应了", "", "peer"), { emoji: "❤️", sender: "me" });
  assert.deepEqual(parseReactionPill("Alice reacted with 😆", "", "me"), { emoji: "😆", sender: "peer" });
  assert.deepEqual(parseReactionPill("Double tap to like", "", "peer"), { emoji: "👍", sender: "peer" });
  assert.equal(parseReactionPill("See who reacted to this message", "", "peer"), null, "没 emoji 不造假");
  assert.equal(parseReactionPill("", "", "peer"), null);
});
