#!/usr/bin/env node
/**
 * qa-trial-abuse.mjs —— 试用领取接口的滥用面压测。
 *
 * 放量前要能回答三个问题，而且要有数字，不能靠读代码猜：
 *   ① 同一出口 IP 批量伪造指纹刷 claim，第几发被挡住？
 *   ② 同一台机器反复来，第几发被挡住？
 *   ③ **限流键取自客户端可控的 x-forwarded-for**——伪造这个头能不能绕过 IP 限流？
 *      （能绕的话，IP 那一轴等于不存在，只剩「按机器指纹幂等」这一道真闸门。）
 *
 * 刻意只打本地 dev server：打生产会往真台账灌垃圾行，还会把真限流窗自己占满。
 *
 * 用法：
 *   cd website && node scripts/qa-trial-abuse.mjs [--url http://localhost:3573]
 */
const args = process.argv.slice(2);
const argOf = (n, d) => {
  const i = args.indexOf(n);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};
const BASE = argOf("--url", "http://localhost:3573").replace(/\/$/, "");

let fpSeq = 0;
function freshFp() {
  fpSeq += 1;
  const hex = fpSeq.toString(16).toUpperCase().padStart(16, "A");
  return hex.slice(0, 16).replace(/(.{4})(?=.)/g, "$1-");
}

async function claim({ fp, ip, contact = "@abuse_probe" }) {
  const headers = { "Content-Type": "application/json" };
  if (ip) headers["x-forwarded-for"] = ip;
  const r = await fetch(`${BASE}/api/trial/claim`, {
    method: "POST",
    headers,
    body: JSON.stringify({ fingerprint: fp, contact, source: "abuse-probe" }),
  });
  let body = {};
  try {
    body = await r.json();
  } catch { /* 429 可能无体 */ }
  return { status: r.status, error: body?.error || "", id: body?.claim_id || "", deduped: !!body?.deduped };
}

const findings = [];
function report(name, detail, ok) {
  console.log(`  ${ok ? "OK  " : "!!  "}${name} —— ${detail}`);
  if (!ok) findings.push(`${name}: ${detail}`);
}

async function main() {
  // 预检：服务在不在
  try {
    await fetch(`${BASE}/api/trial/claim-status`);
  } catch {
    console.error(`起不来或连不上 ${BASE}（先跑 next dev）`);
    process.exit(2);
  }

  console.log("① 同一 IP 批量伪造指纹（每发一个新指纹，只有 IP 这一轴能拦）");
  const ipA = "203.0.113.7";
  let firstBlock = 0;
  for (let i = 1; i <= 20; i++) {
    const { status } = await claim({ fp: freshFp(), ip: ipA });
    if (status === 429 && !firstBlock) firstBlock = i;
  }
  report("IP 轴限流生效", `第 ${firstBlock || "从未"} 发被 429（代码 MAX_PER_IP=12）`,
    firstBlock > 0 && firstBlock <= 14);

  console.log("② 同一指纹反复来（指纹轴）");
  const ipB = "203.0.113.8";
  const sameFp = freshFp();
  let fpBlock = 0;
  for (let i = 1; i <= 10; i++) {
    const { status } = await claim({ fp: sameFp, ip: ipB });
    if (status === 429 && !fpBlock) fpBlock = i;
  }
  report("指纹轴限流生效", `第 ${fpBlock || "从未"} 发被 429（代码 MAX_PER_FP=5）`,
    fpBlock > 0 && fpBlock <= 7);

  // ③ XFF 伪造。两种场景要分开测，否则测不出修复到底管不管用：
  //   经反代：nginx 用 $proxy_add_x_forwarded_for「追加」，头长成 `伪造值, 真实IP`。
  //           限流必须按**最后一跳**取值，取第一段就等于让客户端自己说自己是谁。
  //   直连  ：没有反代追加，头完全由客户端写死 —— 代码层无解，只能靠部署面把
  //           应用端口挡住（绑 127.0.0.1 或防火墙）。
  console.log("③ 经反代时伪造 XFF 能否绕过 IP 限流（真实 IP 在最后一跳）");
  const realIp = "203.0.113.9";
  const proxied = (spoof) => ({ ip: `${spoof}, ${realIp}` });
  for (let i = 0; i < 14; i++) {
    await claim({ fp: freshFp(), ...proxied(`198.51.100.${i + 1}`) });
  }
  const afterCap = await claim({ fp: freshFp(), ...proxied("198.51.100.250") });
  report("经反代时 XFF 伪造无效",
    afterCap.status === 429
      ? `同一真实 IP 打满后，换伪造前缀仍被 429（限流按最后一跳取值）`
      : `换个伪造前缀就放行(${afterCap.status}) → 取了第一段，IP 轴由客户端说了算`,
    afterCap.status === 429);

  console.log("③b 直连（无反代）场景：任何请求头都不可信 —— 这一项只做记录");
  const d1 = "203.0.113.77";
  for (let i = 0; i < 14; i++) await claim({ fp: freshFp(), ip: d1 });
  const direct = await claim({ fp: freshFp(), ip: "198.51.100.9" });
  console.log(`      直连伪造后 HTTP ${direct.status}` +
    (direct.status === 429
      ? "（本轮恰好被别的键挡住）"
      : "（放行——预期如此：直连时代码层无解，须把应用端口绑 127.0.0.1 / 防火墙挡掉）"));

  console.log("④ 指纹幂等：同机重复领取只有一条单（唯一真闸门）");
  const idemFp = freshFp();
  const a = await claim({ fp: idemFp, ip: "203.0.113.20" });
  const b = await claim({ fp: idemFp, ip: "203.0.113.21" });   // 换 IP 也不该建第二条
  report("按机器指纹幂等", 
    a.id && b.id === a.id ? `两次同 id（${b.deduped ? "deduped 标记正确" : "缺 deduped 标记"}）`
      : `第一次 ${a.id || "无 id"} / 第二次 ${b.id || "无 id"}`,
    !!a.id && b.id === a.id);

  console.log("⑤ 蜜罐字段 hp 有值时静默丢弃（不建单、不报错）");
  const hp = await (async () => {
    const r = await fetch(`${BASE}/api/trial/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-forwarded-for": "203.0.113.30" },
      body: JSON.stringify({ fingerprint: freshFp(), contact: "@bot", hp: "i-am-a-bot" }),
    });
    return r.json().catch(() => ({}));
  })();
  report("蜜罐生效", hp?.ok === true && !hp?.claim_id ? "回 ok 但不建单（不给爬虫反馈）"
    : `回 ${JSON.stringify(hp).slice(0, 60)}`, hp?.ok === true && !hp?.claim_id);

  console.log("");
  if (findings.length) {
    console.log(`发现 ${findings.length} 项需要决策：`);
    for (const f of findings) console.log("  - " + f);
    process.exit(1);
  }
  console.log("滥用面压测全部通过");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
