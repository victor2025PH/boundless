/**
 * ============================================================================
 * 试用领取闭环（P2）—— 端到端验收脚本
 * ============================================================================
 *
 * 验的是整条链，而不是单个接口能不能返回 200：
 *
 *   claim(建单)
 *     → claim(同指纹再来)          幂等：必须返回同一条，绝不新建第二份试用
 *     → bind-code                   一次性绑定码 + 预填深链
 *     → admin 待办列表              厂商机看得到这条 pending
 *     → admin 回填 license          （模拟厂商机本地签好后回传）
 *     → claim-status                客户端取到 license，状态 issued
 *     → console 核销绑定码          客服送额度
 *     → console 重复核销            幂等：不该变成两笔赠量
 *     → admin needs_topup 队列      厂商机看得到「待签加量凭证」
 *     → admin 回填 voucher → status 客户端取到凭证
 *
 * 另含两条负面用例：脏指纹必须被拒；claim-status 不带 id 必须 400（防枚举）。
 *
 * 前置：
 *   站点已在跑（npm run dev 或 build+start），且环境里有：
 *     ADMIN_KEY（或 TELEGRAM_SETUP_KEY）—— 厂商机口鉴权
 *     CONSOLE_KEY                      —— 客服核销口鉴权
 *   台账写在 LEADS_DIR（默认 ~/hualing-leads），本脚本用随机指纹，不污染真实数据。
 *
 * 用法：
 *   node scripts/qa-trial-flow.mjs
 *   node scripts/qa-trial-flow.mjs --url http://localhost:3470
 *
 * 退出码：0 = 全过；1 = 有失败项；2 = 站点不可达 / 缺鉴权 env。
 * ============================================================================
 */

const args = process.argv.slice(2);
function arg(name, dflt) {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : dflt;
}

const BASE = String(arg("--url", process.env.QA_BASE_URL || "http://localhost:3000")).replace(/\/+$/, "");
const ADMIN_KEY = process.env.ADMIN_KEY || process.env.TELEGRAM_SETUP_KEY || "";
const CONSOLE_KEY = process.env.CONSOLE_KEY || "";

const checks = [];
function check(name, pass, detail) {
  checks.push({ name, pass: !!pass, detail: detail === undefined ? "" : detail });
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function req(method, path, { body, headers } = {}) {
  const res = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", ...(headers || {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* 非 JSON 响应：交由调用方按 status 判断 */
  }
  return { status: res.status, json };
}

/** 随机但**格式合法**的机器指纹（XXXX-XXXX-XXXX-XXXX），避免污染真实台账。 */
function fakeFingerprint() {
  const hex = () =>
    Array.from({ length: 4 }, () => "0123456789ABCDEF"[Math.floor(Math.random() * 16)]).join("");
  return `${hex()}-${hex()}-${hex()}-${hex()}`;
}

async function main() {
  if (!ADMIN_KEY || !CONSOLE_KEY) {
    console.error("缺鉴权 env：需要 ADMIN_KEY(或 TELEGRAM_SETUP_KEY) 与 CONSOLE_KEY");
    process.exit(2);
  }
  // 「站点是否可达」只看有没有 HTTP 响应：/api/health 在本地 QA 环境常因缺 bot token
  // 返回 503 degraded——那是业务健康度，不是服务死活，拿它当门槛会把能跑的环境判死。
  const health = await req("GET", "/api/health").catch(() => null);
  if (!health) {
    console.error(`站点不可达：${BASE}`);
    process.exit(2);
  }

  const fp = fakeFingerprint();
  const contact = "@qa_trial_bot";
  const adminH = { "x-setup-key": ADMIN_KEY };
  const consoleH = { "x-console-key": CONSOLE_KEY };

  // ── 1. 建单 ──────────────────────────────────────────────────────────
  const c1 = await req("POST", "/api/trial/claim", {
    body: { fingerprint: fp, contact, source: "qa" },
  });
  const claimId = c1.json?.claim_id;
  check("claim 建单", c1.status === 200 && c1.json?.ok && !!claimId, `id=${claimId || "-"}`);
  check("claim 首次非去重", c1.json?.deduped === false);
  check("claim 顺带发绑定码", /^BC-[0-9A-Z]{4}-[0-9A-Z]{4}$/.test(c1.json?.bind_code || ""),
    c1.json?.bind_code);
  if (!claimId) return finish();

  // ── 2. 同指纹再来 = 幂等（唯一真闸门）───────────────────────────────
  const c2 = await req("POST", "/api/trial/claim", {
    body: { fingerprint: fp.toLowerCase(), contact: "@another_handle" },
  });
  check("同机器码去重（大小写归一）",
    c2.json?.ok && c2.json?.deduped === true && c2.json?.claim_id === claimId);
  // 指纹不是秘密：允许覆盖联系方式 = 知道指纹就能把别人的赠量导给自己
  const admAfter = await req("GET", "/api/admin/trial-claims?limit=500", { headers: adminH });
  const mine = (admAfter.json?.claims || []).find((c) => c.id === claimId);
  check("重复 claim 不覆盖已有联系方式", mine?.contact === contact, mine?.contact);

  // ── 3. 脏指纹必须被拒 ────────────────────────────────────────────────
  const bad = await req("POST", "/api/trial/claim", {
    body: { fingerprint: "not-a-fingerprint", contact },
  });
  check("脏指纹被拒", bad.status === 400 && bad.json?.error === "bad_fingerprint");
  const noContact = await req("POST", "/api/trial/claim", {
    body: { fingerprint: fakeFingerprint(), contact: "" },
  });
  check("缺联系方式被拒", noContact.status === 400 && noContact.json?.error === "contact_required");

  // ── 4. 绑定码 + 深链 ─────────────────────────────────────────────────
  const bc = await req("POST", "/api/trial/bind-code", { body: { claim_id: claimId } });
  check("bind-code 幂等同码", bc.json?.ok && bc.json?.bind_code === c1.json?.bind_code);
  check("bind-code 给出预填深链",
    typeof bc.json?.telegram_url === "string" && bc.json.telegram_url.includes("text="));

  // ── 5. 厂商机看得到待办 ──────────────────────────────────────────────
  const pending = await req("GET", "/api/admin/trial-claims?status=pending&limit=500",
    { headers: adminH });
  check("admin 待签发队列含本单",
    pending.json?.ok && (pending.json.claims || []).some((c) => c.id === claimId));
  const unauth = await req("GET", "/api/admin/trial-claims");
  check("admin 口未鉴权拒绝", unauth.status === 401);

  // ── 6. 客户端轮询：还没签 ────────────────────────────────────────────
  const s0 = await req("GET", `/api/trial/claim-status?id=${claimId}`);
  check("claim-status 未签发时 pending", s0.json?.ok && s0.json.status === "pending" && !s0.json.license);
  const noId = await req("GET", "/api/trial/claim-status");
  check("claim-status 无 id 拒绝（防枚举）", noId.status === 400);
  const wrongFp = await req("GET", `/api/trial/claim-status?id=${claimId}&fingerprint=AAAA-BBBB-CCCC-DDDD`);
  check("claim-status 指纹交叉校验不过按 404", wrongFp.status === 404);

  // ── 7. 厂商机回填 license ────────────────────────────────────────────
  const FAKE_LICENSE = "QA_FAKE_LICENSE_BASE64";
  const put = await req("POST", "/api/admin/trial-claims",
    { headers: adminH, body: { id: claimId, license: FAKE_LICENSE } });
  check("admin 回填授权", put.json?.ok && put.json.status === "issued");

  const s1 = await req("GET", `/api/trial/claim-status?id=${claimId}&fingerprint=${fp}`);
  check("claim-status 取到授权", s1.json?.ok && s1.json.status === "issued" && s1.json.license === FAKE_LICENSE);

  // 重复回填同一份 → 幂等，不重置 issued_at
  const put2 = await req("POST", "/api/admin/trial-claims",
    { headers: adminH, body: { id: claimId, license: FAKE_LICENSE } });
  const s2 = await req("GET", `/api/trial/claim-status?id=${claimId}`);
  check("重复回填幂等", put2.json?.ok && s2.json.issued_at === s1.json.issued_at);

  // ── 8. 客服核销绑定码 ────────────────────────────────────────────────
  const code = c1.json.bind_code;
  const r1 = await req("POST", "/api/console/trial-redeem",
    { headers: consoleH, body: { code, chars: 100000 } });
  check("客服核销成功", r1.json?.ok && r1.json.already_redeemed === false && r1.json.chars === 100000);
  check("核销后提示凭证待签", r1.json?.pending_voucher === true);

  const r2 = await req("POST", "/api/console/trial-redeem", { headers: consoleH, body: { code } });
  check("重复核销幂等（不变两笔赠量）", r2.json?.ok && r2.json.already_redeemed === true);

  const rBad = await req("POST", "/api/console/trial-redeem",
    { headers: consoleH, body: { code: "BC-XXXX" } });
  check("脏绑定码被拒", rBad.status === 400);
  const rUnauth = await req("POST", "/api/console/trial-redeem", { body: { code } });
  check("客服口未鉴权拒绝", rUnauth.status === 401);

  // ── 9. 厂商机签加量凭证 → 客户端取到 ─────────────────────────────────
  const needs = await req("GET", "/api/admin/trial-claims?needs_topup=1&limit=500",
    { headers: adminH });
  check("admin 待签凭证队列含本单",
    needs.json?.ok && (needs.json.claims || []).some((c) => c.id === claimId));

  const FAKE_VOUCHER = "QA_FAKE_TOPUP_VOUCHER";
  const put3 = await req("POST", "/api/admin/trial-claims",
    { headers: adminH, body: { id: claimId, topup_voucher: FAKE_VOUCHER, topup_chars: 100000 } });
  check("admin 回填加量凭证", put3.json?.ok && put3.json.has_topup_voucher === true);

  const s3 = await req("GET", `/api/trial/claim-status?id=${claimId}`);
  check("claim-status 取到加量凭证",
    s3.json?.topup_voucher === FAKE_VOUCHER && s3.json.bind_redeemed === true);

  const needs2 = await req("GET", "/api/admin/trial-claims?needs_topup=1&limit=500",
    { headers: adminH });
  check("已签的不再出现在待签队列",
    !(needs2.json?.claims || []).some((c) => c.id === claimId));

  // ── 10. 履约端心跳 ───────────────────────────────────────────────────
  // 厂商机是签发链上的单点：它停了，用户点完领取会一直停在「正在签发」而两边
  // 都毫无察觉。取待办本身即心跳，这里验它真的被记下来了。
  const st = needs2.json?.stats || {};
  check("stats 带履约端心跳", typeof st.fulfiller_last_seen === "string" && !!st.fulfiller_last_seen,
    st.fulfiller_last_seen);
  const seenMs = Date.parse(st.fulfiller_last_seen || "");
  check("心跳是刚才这几次取待办", Number.isFinite(seenMs) && Date.now() - seenMs < 120000);
  check("stats 带待办积压年龄", typeof st.oldest_pending_min === "number");

  // 心跳有 60s 写节流：履约脚本每轮都来取待办，每次都落盘等于把台账当日志刷。
  // 连打两次取待办后时间戳不变 = 节流生效（同时也说明中间那次用户建单没碰它）。
  await req("POST", "/api/trial/claim",
    { body: { fingerprint: fakeFingerprint(), contact: "@hb_probe" } });
  const st2 = (await req("GET", "/api/admin/trial-claims?limit=1", { headers: adminH })).json?.stats;
  check("心跳写节流生效（不被高频轮询刷爆）",
    st2?.fulfiller_last_seen === st.fulfiller_last_seen, st2?.fulfiller_last_seen);

  finish();
}

function finish() {
  const failed = checks.filter((c) => !c.pass);
  console.log("");
  console.log(JSON.stringify({ url: BASE, total: checks.length, failed: failed.length,
    pass: failed.length === 0 }, null, 2));
  process.exit(failed.length === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
