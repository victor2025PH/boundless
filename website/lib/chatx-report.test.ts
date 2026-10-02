/**
 * ChatX 广告日报聚合冒烟：npx tsx lib/chatx-report.test.ts
 * 人按 uid 去重、无 src 事件归到人所属来源、30 分钟切会话、IP 只来自 HTTP 事件、推送回话归到推送日、诊断规则。
 */
import assert from "assert";
import { buildDailyReport, buildPostFunnel, dayKey, fmtDur, formatDailyDigest, recentDays, type Ev } from "./chatx-report";

const TZ = 8 * 3600_000;
const D1 = "2026-09-22";
const D2 = "2026-09-23";
const at = (day: string, hm: string) => new Date(`${day}T${hm}:00+08:00`).toISOString();
const ev = (t: string, event: string, props: Record<string, unknown> | null = null, extra: Partial<Ev> = {}): Ev => ({ t, event, props, ...extra });

const events: Ev[] = [
  // 来源 A：3 人 start（u1 两次），u1 落地+点击+下载（同人 3 次点击算 1），u2 落地无下载，u3 只 start
  ev(at(D1, "09:00"), "chatx_bot_start", { src: "ad_a", uid: 1, first: true }),
  ev(at(D1, "09:01"), "chatx_bot_start", { src: "ad_a", uid: 1, first: false }),
  ev(at(D1, "09:05"), "chatx_bot_start", { src: "ad_a", uid: 2, first: true }),
  ev(at(D1, "09:06"), "chatx_bot_start", { src: "ad_a", uid: 3, first: true }),
  ev(at(D1, "09:10"), "chatx_landing_view", { src: "ad_a", tg: "1" }, { sid: "s1", ip: "1.1.1.1" }),
  ev(at(D1, "09:11"), "chatx_download_click", { src: "ad_a", tg: "1" }, { sid: "s1", ip: "1.1.1.1" }),
  ev(at(D1, "09:12"), "chatx_download_click", { src: "ad_a", tg: "1" }, { sid: "s1", ip: "1.1.1.1" }),
  ev(at(D1, "09:12"), "chatx_download_click", { src: "ad_a", tg: "1" }, { sid: "s1", ip: "1.1.1.1" }),
  ev(at(D1, "09:13"), "download_redirect", { src: "ad_a", uid: 1, installer: true }, { ip: "1.1.1.1" }),
  ev(at(D1, "09:20"), "chatx_landing_view", { src: "ad_a" }, { sid: "s2", ip: "2.2.2.2" }),
  // u1 聊了 2 句（其中一句 src 缺失，应按 uid 归到 ad_a），u2 发了一条语音
  ev(at(D1, "09:14"), "chatx_bot_ai", { src: "ad_a", uid: 1, mode: "ai" }),
  ev(at(D1, "09:16"), "chatx_bot_ai", { uid: 1, mode: "kb" }),
  ev(at(D1, "09:30"), "chatx_bot_voice_in", { src: "ad_a", uid: 2, ok: true, sec: 4 }),
  ev(at(D1, "09:30"), "chatx_bot_voice_out", { src: "ad_a", uid: 2, ok: true, sec: 6.4, persona: "xiaojie" }),
  // u1 切到恋爱人设聊了一句，语音因 TTS 挂了没发出；只看菜单（无 from）不算切换，重选同一人设也不算
  ev(at(D1, "09:40"), "chatx_bot_cmd", { src: "ad_a", uid: 1, cmd: "persona", persona: "xiaojie" }),
  ev(at(D1, "09:41"), "chatx_bot_cmd", { src: "ad_a", uid: 1, cmd: "persona", persona: "lover", from: "xiaojie" }),
  ev(at(D1, "09:42"), "chatx_bot_cmd", { src: "ad_a", uid: 1, cmd: "persona", persona: "lover", from: "lover" }),
  ev(at(D1, "09:43"), "chatx_bot_ai", { src: "ad_a", uid: 1, mode: "ai", persona: "lover" }),
  ev(at(D1, "09:43"), "chatx_bot_voice_out", { src: "ad_a", uid: 1, ok: false, why: "tts_failed", persona: "lover" }),
  // 按钮 / 命令不算对话，但算活动
  ev(at(D1, "09:31"), "chatx_bot_cmd", { src: "ad_a", uid: 2, cmd: "features" }),
  // u1 两小时后又来聊：第二段会话
  ev(at(D1, "11:30"), "chatx_bot_ai", { src: "ad_a", uid: 1, mode: "ai" }),
  ev(at(D1, "11:40"), "chatx_bot_ai", { src: "ad_a", uid: 1, mode: "ai" }),
  // 来源 B：1 人，落地没 tg（按 sid 去重），同 IP 刷 6 次
  ev(at(D1, "10:00"), "chatx_bot_start", { src: "ad_b", uid: 9, first: true }),
  ...Array.from({ length: 6 }, (_, i) => ev(at(D1, `10:0${i + 1}`), "chatx_landing_view", { src: "ad_b" }, { sid: `b${i}`, ip: "9.9.9.9" })),
  // 提醒 / 推送是 bot 主动发的：不算活动；D1 推送给 u3，u3 在 D2 凌晨回话 → 归到 D1 的推送回话
  ev(at(D1, "20:00"), "chatx_bot_remind", { src: "ad_a", uid: 3, ok: true }),
  ev(at(D1, "21:00"), "chatx_bot_push", { src: "ad_a", uid: 3, ok: true }),
  ev(at(D1, "21:00"), "chatx_bot_push", { src: "ad_a", uid: 2, ok: false }),
  ev(at(D2, "01:00"), "chatx_bot_ai", { uid: 3, mode: "ai" }),
  // 小程序 chatx 场景对话也算一句（无 uid，按 sid）
  ev(at(D2, "12:00"), "miniapp_chat", { scene: "chatx", src: "ad_a" }, { sid: "m1" }),
  ev(at(D2, "12:01"), "miniapp_chat", { scene: "other" }, { sid: "m2" }),
  // 窗口外的日子不计
  ev(at("2026-09-01", "12:00"), "chatx_bot_start", { src: "ad_a", uid: 77, first: true }),
];

async function main() {
  assert.strictEqual(dayKey(at(D2, "01:00")), D2, "日键按 UTC+8");
  assert.deepStrictEqual(recentDays(2, Date.parse(at(D2, "10:00")), TZ), [D1, D2]);

  const r = buildDailyReport(events, [D1, D2], Date.parse(at(D2, "20:00")));
  const row = (day: string, src: string) => {
    const x = r.rows.find((q) => q.day === day && q.src === src);
    assert.ok(x, `缺行 ${day}/${src}`);
    return x!;
  };

  const a1 = row(D1, "ad_a");
  assert.strictEqual(a1.starts, 4);
  assert.strictEqual(a1.users, 3, "人按 uid 去重");
  assert.strictEqual(a1.newUsers, 3);
  assert.strictEqual(a1.landUsers, 2, "u1 + 匿名 s2");
  assert.strictEqual(a1.clickUsers, 1, "同人点三次算 1");
  assert.strictEqual(a1.dlUsers, 1);
  assert.strictEqual(a1.msgs, 6, "u1 5 句 + u2 语音 1 句；命令 / 语音回复不算");
  assert.strictEqual(a1.voiceMsgs, 1);
  assert.strictEqual(a1.chatUsers, 2);
  assert.strictEqual(a1.ips, 2, "IP 只来自 HTTP 事件");
  assert.deepStrictEqual(a1.topIps[0], { ip: "1.1.1.1", n: 2 }, "IP 只数落地 + 安装包请求，点击不算");
  assert.strictEqual(a1.pushSent, 1, "失败的推送不计");
  assert.strictEqual(a1.pushReplied, 1, "u3 次日凌晨回话归到推送日");
  // 会话：u1 09:00–09:43（43m）+ 11:30–11:40（10m）；u2 09:05–09:31（26m）；u3 09:06 单事件（0）
  assert.strictEqual(a1.sessions, 4);
  assert.strictEqual(a1.durSec, (43 + 10 + 26) * 60);
  assert.strictEqual(a1.avgDurSec, Math.round(((43 + 10 + 26) * 60) / 4));

  // 功能面：语音进/出、失败原因、人设分布与切换、推送失败
  assert.strictEqual(r.features[0].day, D2, "features 新→旧");
  const f1 = r.features.find((f) => f.day === D1)!;
  assert.strictEqual(f1.voiceIn, 1);
  assert.strictEqual(f1.voiceInOk, 1);
  assert.strictEqual(f1.voiceInUsers, 1);
  assert.strictEqual(f1.voiceOut, 2);
  assert.strictEqual(f1.voiceOutOk, 1);
  assert.strictEqual(f1.voiceOutSec, 6.4, "只累加成功的时长");
  assert.strictEqual(f1.voiceOutUsers, 2);
  assert.deepStrictEqual(f1.voiceOutFail, { tts_failed: 1 });
  assert.deepStrictEqual(f1.voiceInFail, {});
  assert.deepStrictEqual(f1.personaMsgs, { xiaojie: 4, lover: 1 }, "无 persona 字段算小界");
  assert.deepStrictEqual(f1.personaUsers, { xiaojie: 1, lover: 1 });
  assert.deepStrictEqual(f1.personaSwitches, { lover: 1 }, "看菜单 / 重选同人设不算切换");
  assert.strictEqual(f1.pushFail, 1);
  const f2 = r.features.find((f) => f.day === D2)!;
  assert.strictEqual(f2.voiceIn + f2.voiceOut, 0);
  assert.deepStrictEqual(f2.personaMsgs, { xiaojie: 1 });

  const b1 = row(D1, "ad_b");
  assert.strictEqual(b1.landUsers, 6, "无 tg 时按 sid 去重");
  assert.strictEqual(b1.ips, 1);
  assert.strictEqual(b1.topIps[0].n, 6);

  const a2 = row(D2, "ad_a");
  assert.strictEqual(a2.msgs, 2, "u3 无 src 的回话按 uid 归到 ad_a + 小程序 chatx 一句");
  assert.strictEqual(a2.chatUsers, 2);
  assert.ok(!r.rows.some((q) => q.day === "2026-09-01"), "窗口外不计");
  assert.ok(!r.rows.some((q) => q.src === "other"), "非 chatx 小程序对话不计");

  const t1 = r.totals.find((t) => t.day === D1)!;
  assert.strictEqual(t1.users, 4, "日合计按人去重");
  assert.strictEqual(t1.landUsers, 8);
  assert.strictEqual(r.totals[0].day, D2, "totals 新→旧");
  const sa = r.bySrc.find((s) => s.src === "ad_a")!;
  assert.strictEqual(sa.users, 3, "来源合计跨天按人去重（u1/u2/u3）");
  assert.strictEqual(sa.days, 2);

  // 诊断：ad_b 同 IP 刷量应被点名；样本 <5 不下转化结论
  assert.ok(r.insights.some((i) => i.src === "ad_b" && /9\.9\.9\.9/.test(i.text)), "同 IP 刷量诊断");
  assert.ok(!r.insights.some((i) => /最优来源/.test(i.text)), "小样本不下最优结论");
  assert.ok(!r.insights.some((i) => /语音回复/.test(i.text)), "语音样本 <5 不下结论");

  // 功能面诊断：TTS 失败率高 → bad 点名原因；配额挡人 → warn 提示环境变量；非默认人设占比
  const vf: Ev[] = [];
  for (let i = 0; i < 10; i++) {
    vf.push(ev(at(D1, "09:00"), "chatx_bot_voice_out", { uid: 500 + i, ok: i >= 4, why: i < 4 ? "tts_failed" : undefined, sec: 5 }));
    vf.push(ev(at(D1, "09:00"), "chatx_bot_ai", { uid: 500 + i, persona: i < 6 ? "lover" : undefined }));
  }
  const rv = buildDailyReport(vf, [D1], Date.parse(at(D2, "20:00")));
  assert.ok(rv.insights.some((i) => i.level === "bad" && /40%/.test(i.text) && /TTS 中继/.test(i.text)), rv.insights.map((i) => i.text).join("\n"));
  assert.ok(rv.insights.some((i) => /恋爱陪聊 60%/.test(i.text)), "人设占比");
  const vq: Ev[] = Array.from({ length: 10 }, (_, i) => ev(at(D1, "09:00"), "chatx_bot_voice_out", { uid: 600 + i, ok: i >= 3, why: i < 3 ? "user_quota" : undefined, sec: 5 }));
  const rq = buildDailyReport(vq, [D1], Date.parse(at(D2, "20:00")));
  assert.ok(rq.insights.some((i) => i.level === "bad" && /每人每日上限/.test(i.text)), "30% 失败主因是配额也点名");
  const vq2: Ev[] = Array.from({ length: 20 }, (_, i) => ev(at(D1, "09:00"), "chatx_bot_voice_out", { uid: 700 + i, ok: i >= 3, why: i < 3 ? "cooldown" : undefined, sec: 5 }));
  const rq2 = buildDailyReport(vq2, [D1], Date.parse(at(D2, "20:00")));
  assert.ok(rq2.insights.some((i) => i.level === "warn" && /CHATX_VOICE_USER_DAILY_MAX/.test(i.text)), "15% 被配额挡 → warn");
  const vok: Ev[] = Array.from({ length: 6 }, (_, i) => ev(at(D1, "09:00"), "chatx_bot_voice_out", { uid: 800 + i, ok: true, sec: 10 }));
  const rok = buildDailyReport(vok, [D1], Date.parse(at(D2, "20:00")));
  assert.ok(rok.insights.some((i) => i.level === "info" && /6\/6 成功/.test(i.text) && /1m/.test(i.text)), "全成功→info 带累计时长");

  // 样本够时：最优 / 最差 / 落地低 / 聊了不下载
  const big: Ev[] = [];
  for (let u = 1; u <= 20; u++) {
    big.push(ev(at(D1, "09:00"), "chatx_bot_start", { src: "ad_good", uid: 100 + u, first: true }));
    big.push(ev(at(D1, "09:05"), "chatx_landing_view", { src: "ad_good", tg: String(100 + u) }, { ip: `10.0.0.${u}` }));
    if (u <= 10) big.push(ev(at(D1, "09:06"), "download_redirect", { src: "ad_good", uid: 100 + u, installer: true }, { ip: `10.0.0.${u}` }));
    big.push(ev(at(D1, "09:00"), "chatx_bot_start", { src: "ad_bad", uid: 200 + u, first: true }));
    if (u <= 2) big.push(ev(at(D1, "09:05"), "chatx_landing_view", { src: "ad_bad", tg: String(200 + u) }, { ip: `10.0.1.${u}` }));
    big.push(ev(at(D1, "09:00"), "chatx_bot_start", { src: "ad_talk", uid: 300 + u, first: true }));
    big.push(ev(at(D1, "09:05"), "chatx_landing_view", { src: "ad_talk", tg: String(300 + u) }, { ip: `10.0.2.${u}` }));
    big.push(ev(at(D1, "09:07"), "chatx_bot_ai", { src: "ad_talk", uid: 300 + u, mode: "ai" }));
  }
  const rb = buildDailyReport(big, [D1, D2], Date.parse(at(D2, "20:00")));
  const texts = rb.insights.map((i) => `${i.level}|${i.src ?? ""}|${i.text}`).join("\n");
  assert.ok(/good\|ad_good\|.*50%/.test(texts), "最优来源 50%");
  assert.ok(/^bad\|ad_(bad|talk)\|.*0%/m.test(texts), "最差来源点名");
  assert.ok(/warn\|ad_bad\|.*落地页/.test(texts), "start 多落地少");
  assert.ok(/warn\|ad_talk\|.*0 人下载/.test(texts), "聊了不下载");
  assert.ok(/warn\|ad_good\|.*安装包/.test(texts) === false, "ad_good 落地→下载 50% 不告警");

  // 日报文案
  const digest = formatDailyDigest(r, D1);
  assert.ok(digest.includes(`ChatX 广告日报 · ${D1}`));
  assert.ok(digest.includes("进 bot <b>4</b> 人"));
  assert.ok(digest.includes("<code>ad_a</code>  3 → 2 → <b>1</b>"), digest);
  assert.ok(digest.includes("9.9.9.9"), "诊断进日报");
  assert.ok(digest.includes("🎙 语音：收到 1 条 · 回出 1 条，失败 1（tts_failed 1）"), digest);
  assert.ok(digest.includes("🎭 人设：小界 4 句/1 人 · 恋爱陪聊 1 句/1 人（切换 1 次）"), digest);
  const digest2 = formatDailyDigest(r, D2);
  assert.ok(!digest2.includes("🎙") && !digest2.includes("🎭"), "没语音 / 只有默认人设的日子不加这两行");
  const empty = formatDailyDigest(buildDailyReport([], [D2]), D2);
  assert.ok(empty.includes("没有任何"));
  assert.strictEqual(fmtDur(0), "0s");
  assert.strictEqual(fmtDur(125), "2m5s");

  // 帖子漏斗：点击 → 带帖子号的 /start → 同一 uid 的安装包请求；系列内按人去重
  const pe: Ev[] = [
    ev(at(D1, "10:00"), "tg_post_click", { post: 7, src: "ad_p" }),
    ev(at(D1, "10:00"), "tg_post_click", { post: 7, src: "ad_p" }),
    ev(at(D1, "10:01"), "tg_post_click", { post: 8 }),
    ev(at(D1, "10:02"), "chatx_bot_start", { src: "ad_p", uid: 71, first: true, post: 7 }),
    ev(at(D1, "10:03"), "chatx_bot_start", { src: "ad_p", uid: 71, first: false, post: 7 }),
    ev(at(D1, "10:04"), "chatx_bot_start", { src: "ad_p", uid: 72, first: true, post: 7 }),
    ev(at(D1, "10:05"), "chatx_bot_start", { src: "organic", uid: 73, first: true }),
    ev(at(D1, "10:10"), "download_redirect", { src: "ad_p", uid: 71, installer: true }),
    ev(at(D1, "10:11"), "download_redirect", { src: "ad_p", uid: 71, installer: false }),
    ev(at(D1, "10:12"), "download_redirect", { src: "organic", uid: 73, installer: true }),
    ev(at(D2, "10:00"), "chatx_bot_start", { src: "ad_p", uid: 72, first: false, post: 8 }),
    ev(at(D2, "10:01"), "chatx_bot_start", { src: "ad_p", uid: 74, first: true, post: 8 }),
    ev(at(D2, "10:05"), "download_redirect", { src: "ad_p", uid: 72, installer: true }),
    ev(at(D2, "10:06"), "chatx_bot_start", { src: "ad_p", uid: 71, first: false }),
    ev(at(D2, "10:07"), "download_redirect", { src: "ad_p", uid: 71, installer: true }),
  ];
  const pf = buildPostFunnel(pe);
  assert.deepStrictEqual(pf.get(7), { post: 7, clicks: 2, users: 2, dlUsers: 1 }, "u71 两次 start 算 1 人；非安装包请求不算");
  assert.deepStrictEqual(pf.get(8), { post: 8, clicks: 1, users: 2, dlUsers: 1 }, "u72 后来从 #8 进，下载归 #8；u71 D2 不带帖子号的 start 不覆盖，但 #7 已算过该人");
  assert.strictEqual(pf.size, 2, "不带帖子号的 organic 不进帖子漏斗");
  const sf = buildPostFunnel(pe, (p) => (p === 8 ? 7 : p));
  assert.deepStrictEqual(sf.get(7), { post: 7, clicks: 3, users: 3, dlUsers: 2 }, "系列合计：u72 两期只算 1 人，u71 下载只算 1 人");
  const pr = buildDailyReport(pe, [D1, D2]);
  const pf1 = pr.features.find((x) => x.day === D1)!;
  assert.deepStrictEqual(pf1.postClicks, { "7": 2, "8": 1 });
  assert.deepStrictEqual(pf1.postUsers, { "7": 2 });
  assert.deepStrictEqual(pf1.postDl, { "7": 1 });
  const pf2 = pr.features.find((x) => x.day === D2)!;
  assert.deepStrictEqual(pf2.postUsers, { "8": 2 });
  assert.deepStrictEqual(pf2.postDl, { "8": 1, "7": 1 }, "u71 隔天下载仍归 #7");
  const pd = formatDailyDigest(pr, D1);
  assert.ok(pd.includes("📣 帖子（点击→进 bot→下载人）：3→2→1（#7 2→2→1 · #8 1→0→0）"), pd);

  console.log("chatx-report smoke OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
