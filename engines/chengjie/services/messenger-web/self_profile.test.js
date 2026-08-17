/**
 * self_profile.js 门禁（`node --test`，零依赖直跑）。
 *
 * 金标来自 2026-08-15 真实事故：Messenger 账号 615***403（自身昵称 Calixa Lopez）的
 * `self_avatar` 存的是**会话对端 Micah Bindo** 的头像——根因是旧实现「取页面第一个
 * img[alt]」，而 DOM 顺序里排在前面的往往是会话行里对端的小圆头。这批用例的核心
 * 就是把「宁可交空也不许挑错脸」钉死：任何一条 fail-closed 断言变绿→变红，都意味着
 * 有人又给它加回了「任意图」兜底。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeName, parseCurrentUserInitialData, isFetchableAvatarUrl,
  pickSelfAvatar, summarizeCandidates, nextSelfProfileRecaptureMs,
  SELF_AVATAR_STRATEGY,
} from "./self_profile.js";

// 事故现场形状：对端头像在前、自身头像在后，两者都是合法 fbcdn 直链。
const PEER_AVATAR = "https://scontent-hkt1-1.xx.fbcdn.net/v/t1.6435-1/peer_micah.jpg?_nc_ohc=AAA&oh=1&oe=2";
const SELF_AVATAR = "https://scontent-hkt1-1.xx.fbcdn.net/v/t1.6435-1/self_calixa.jpg?_nc_ohc=BBB&oh=3&oe=4";
const INCIDENT_CANDIDATES = [
  { src: PEER_AVATAR, alt: "Micah Bindo", w: 40, h: 40, hrefs: ["/t/8901234567/"] },
  { src: SELF_AVATAR, alt: "Calixa Lopez", w: 60, h: 60, hrefs: ["/me/"] },
];

test("normalizeName 折叠空白 + 去零宽字符", () => {
  assert.equal(normalizeName("  Calixa\u200b   Lopez \n"), "Calixa Lopez");
  assert.equal(normalizeName(null), "");
  assert.equal(normalizeName(undefined), "");
});

test("parseCurrentUserInitialData 取昵称与 uid（含 JSON 转义）", () => {
  const slice = 'CurrentUserInitialData",{"ACCOUNT_ID":"1","USER_ID":"61511122233",'
    + '"NAME":"Calixa Lopez","SHORT_NAME":"Calixa"';
  const got = parseCurrentUserInitialData([slice]);
  assert.equal(got.name, "Calixa Lopez");
  assert.equal(got.userId, "61511122233");
  // Unicode 转义（中文昵称在页面里就是这种形态）
  assert.equal(
    parseCurrentUserInitialData(['CurrentUserInitialData {"NAME":"\\u674e\\u5c0f\\u82b1"}']).name,
    "李小花",
  );
  // 多片段：任一片段有值即可；都没有 → 空（绝不编）
  assert.equal(parseCurrentUserInitialData(["noise", slice]).name, "Calixa Lopez");
  assert.deepEqual(parseCurrentUserInitialData([]), { name: "", userId: "" });
  assert.deepEqual(parseCurrentUserInitialData("garbage"), { name: "", userId: "" });
});

test("isFetchableAvatarUrl 只认 http(s)——其余存进去就是账号卡上一张裂图", () => {
  assert.equal(isFetchableAvatarUrl(SELF_AVATAR), true);
  assert.equal(isFetchableAvatarUrl("http://x/y.jpg"), true);
  assert.equal(isFetchableAvatarUrl("blob:https://www.messenger.com/abc"), false);
  assert.equal(isFetchableAvatarUrl("data:image/png;base64,AAAA"), false);
  assert.equal(isFetchableAvatarUrl(""), false);
  assert.equal(isFetchableAvatarUrl(null), false);
});

test("事故金标：对端头像排在前面，也必须选中自身那张", () => {
  // 前提复现：旧实现「第一个 img」拿到的就是对端——这条断言在提醒别退回那个口径。
  assert.equal(INCIDENT_CANDIDATES[0].src, PEER_AVATAR);
  const got = pickSelfAvatar(INCIDENT_CANDIDATES, {
    selfName: "Calixa Lopez", selfId: "61511122233",
  });
  assert.equal(got.url, SELF_AVATAR);
  assert.equal(got.strategy, SELF_AVATAR_STRATEGY.NAME);
  assert.equal(got.reason, "");
});

test("认不出就交空（fail-closed）：不许拿 fbcdn 图顶包", () => {
  // 页面上只有对端的图 → 一张都不能选
  const onlyPeer = [INCIDENT_CANDIDATES[0]];
  const got = pickSelfAvatar(onlyPeer, { selfName: "Calixa Lopez", selfId: "61511122233" });
  assert.equal(got.url, "");
  assert.equal(got.strategy, "");
  assert.equal(got.reason, "no_match");
});

test("拿不到自身昵称、也没有自己主页链接 → 不猜（reason=no_self_name）", () => {
  const noSelfEvidence = [
    { src: PEER_AVATAR, alt: "Micah Bindo", w: 40, h: 40, hrefs: ["/t/890/"] },
    { src: SELF_AVATAR, alt: "Calixa Lopez", w: 60, h: 60, hrefs: [] },
  ];
  const got = pickSelfAvatar(noSelfEvidence, { selfName: "", selfId: "" });
  assert.equal(got.url, "");
  assert.equal(got.reason, "no_self_name");
});

test("昵称读空但有 /me/ 链接 → 仍可选中（PROFILE_LINK 不依赖昵称）", () => {
  // 登录瞬间脚本片段没进文档流时会走到这一档，比整轮交空更有用。
  const got = pickSelfAvatar(INCIDENT_CANDIDATES, { selfName: "", selfId: "" });
  assert.equal(got.url, SELF_AVATAR);
  assert.equal(got.strategy, SELF_AVATAR_STRATEGY.PROFILE_LINK);
});

test("无候选 → no_candidates；不可下载的直链不算候选", () => {
  assert.equal(pickSelfAvatar([], { selfName: "A" }).reason, "no_candidates");
  assert.equal(pickSelfAvatar(null, { selfName: "A" }).reason, "no_candidates");
  const blobOnly = [{ src: "blob:https://x/1", alt: "Calixa Lopez", w: 60, h: 60, hrefs: [] }];
  const got = pickSelfAvatar(blobOnly, { selfName: "Calixa Lopez" });
  assert.equal(got.url, "");
  assert.equal(got.reason, "no_candidates");
});

test("次级判据 profile_link：alt 为空/被本地化，靠自己主页链接兜住", () => {
  const cands = [
    { src: PEER_AVATAR, alt: "Micah Bindo", w: 40, h: 40, hrefs: ["/t/890/"] },
    { src: SELF_AVATAR, alt: "", w: 36, h: 36, hrefs: ["/me/"] },
  ];
  const byMe = pickSelfAvatar(cands, { selfName: "Calixa Lopez", selfId: "61511122233" });
  assert.equal(byMe.url, SELF_AVATAR);
  assert.equal(byMe.strategy, SELF_AVATAR_STRATEGY.PROFILE_LINK);

  // uid 形态的两种真实 href
  for (const href of ["/profile.php?id=61511122233", "/61511122233/"]) {
    const got = pickSelfAvatar(
      [{ src: SELF_AVATAR, alt: "你的个人资料", w: 36, h: 36, hrefs: [href] }],
      { selfName: "Calixa Lopez", selfId: "61511122233" },
    );
    assert.equal(got.url, SELF_AVATAR, `href=${href} 应判为自己主页`);
    assert.equal(got.strategy, SELF_AVATAR_STRATEGY.PROFILE_LINK);
  }
  // 别人的 uid 链接不得误判
  assert.equal(pickSelfAvatar(
    [{ src: PEER_AVATAR, alt: "Micah Bindo", w: 40, h: 40, hrefs: ["/profile.php?id=8901234567"] }],
    { selfName: "Calixa Lopez", selfId: "61511122233" },
  ).url, "");
});

test("NAME 判据优先于 profile_link（前者更强）", () => {
  const cands = [
    { src: PEER_AVATAR, alt: "", w: 200, h: 200, hrefs: ["/me/"] },      // 更大且带 /me/
    { src: SELF_AVATAR, alt: "Calixa Lopez", w: 36, h: 36, hrefs: [] },
  ];
  const got = pickSelfAvatar(cands, { selfName: "Calixa Lopez", selfId: "1" });
  assert.equal(got.url, SELF_AVATAR);
  assert.equal(got.strategy, SELF_AVATAR_STRATEGY.NAME);
});

test("同层多张取面积最大（头像位通常大于列表小圆头）", () => {
  const small = "https://scontent.xx.fbcdn.net/small.jpg";
  const big = "https://scontent.xx.fbcdn.net/big.jpg";
  const got = pickSelfAvatar([
    { src: small, alt: "Calixa Lopez", w: 24, h: 24, hrefs: [] },
    { src: big, alt: "calixa   lopez", w: 168, h: 168, hrefs: [] },  // 大小写/空白不敏感
  ], { selfName: " Calixa Lopez " });
  assert.equal(got.url, big);
});

// ---- NAV_LABEL：alt 全空的真实 DOM 形态（2026-08-15 实测这版 messenger.com）----
// 自身头像嵌在左栏账号菜单按钮里（aria-label 带本名 + 功能词），且只有 32×32；
// 线程里对端那张 100×100 却挂在 role=img[aria-label=对端名] 上。
const NAV_SELF = {
  src: SELF_AVATAR, alt: "", w: 32, h: 32,
  chain: [
    { tag: "IMG", role: "", aria: "", href: "" },
    { tag: "SVG", role: "none", aria: "", href: "" },
    { tag: "DIV", role: "button", aria: "Calixa Settings, help and more", href: "" },
  ],
};
const NAV_PEER = {
  src: PEER_AVATAR, alt: "", w: 100, h: 100,
  chain: [
    { tag: "IMG", role: "", aria: "", href: "" },
    { tag: "SVG", role: "img", aria: "Micah Bindo", href: "" },
    { tag: "DIV", role: "", aria: "", href: "/t/8901234567/" },
  ],
};

test("NAV_LABEL：alt 全空时，按「带本人名字的交互控件」认出自己（且不被大图带偏）", () => {
  const got = pickSelfAvatar([NAV_PEER, NAV_SELF], {
    selfName: "Calixa Lopez", selfId: "61511122233",
  });
  assert.equal(got.url, SELF_AVATAR);
  assert.equal(got.strategy, SELF_AVATAR_STRATEGY.NAV_LABEL);
  // 面积一旦被当判据就会挑中 100×100 的对端 —— 这条断言就是防那个退化。
  assert.ok(NAV_PEER.w * NAV_PEER.h > NAV_SELF.w * NAV_SELF.h);
});

test("被标成别人的图先剔除：整页只剩他人 → all_labeled_others", () => {
  const seen = {   // 「已读」指示器也是 role=img，同样是别人的脸
    src: PEER_AVATAR.replace("peer_micah", "seen_micah"), alt: "", w: 12, h: 12,
    chain: [{ tag: "IMG", role: "", aria: "", href: "" },
      { tag: "SVG", role: "img", aria: "Seen by Micah Bindo", href: "" }],
  };
  const got = pickSelfAvatar([NAV_PEER, seen], {
    selfName: "Calixa Lopez", selfId: "61511122233",
  });
  assert.equal(got.url, "");
  assert.equal(got.reason, "all_labeled_others");
});

test("他人标签的排除优先于其他判据：对端图哪怕挂着 /me/ 也不许选", () => {
  const trap = {
    ...NAV_PEER,
    chain: [...NAV_PEER.chain, { tag: "A", role: "link", aria: "", href: "/me/" }],
  };
  assert.equal(pickSelfAvatar([trap], { selfName: "Calixa Lopez" }).url, "");
});

test("交互控件的标签里没有本人名字 → 不算自己（fail-closed）", () => {
  const chats = {
    src: SELF_AVATAR, alt: "", w: 32, h: 32,
    chain: [{ tag: "IMG", role: "", aria: "", href: "" },
      { tag: "DIV", role: "button", aria: "Chats", href: "" }],
  };
  const got = pickSelfAvatar([chats], { selfName: "Calixa Lopez", selfId: "1" });
  assert.equal(got.url, "");
  assert.equal(got.reason, "no_match");
});

test("名字 token 要词边界，且单字符 token 不参与匹配", () => {
  // "Ana" 不得命中 "Manager"（子串匹配会误判成自己的按钮）
  const mgr = {
    src: SELF_AVATAR, alt: "", w: 32, h: 32,
    chain: [{ tag: "DIV", role: "button", aria: "Manager tools", href: "" }],
  };
  assert.equal(pickSelfAvatar([mgr], { selfName: "Ana" }).url, "");
  // 单字符名（"A Lopez" 的 "A"）不得让 "Account settings" 命中；"Lopez" 才算
  const acct = {
    src: SELF_AVATAR, alt: "", w: 32, h: 32,
    chain: [{ tag: "DIV", role: "button", aria: "Account settings", href: "" }],
  };
  assert.equal(pickSelfAvatar([acct], { selfName: "A Lopez" }).url, "");
  const mine = {
    src: SELF_AVATAR, alt: "", w: 32, h: 32,
    chain: [{ tag: "DIV", role: "button", aria: "Lopez, settings and more", href: "" }],
  };
  assert.equal(pickSelfAvatar([mine], { selfName: "A Lopez" }).strategy,
    SELF_AVATAR_STRATEGY.NAV_LABEL);
});

test("CJK 昵称：整名当一个 token 走子串（页面无空格分词）", () => {
  const cjk = {
    src: SELF_AVATAR, alt: "", w: 32, h: 32,
    chain: [{ tag: "DIV", role: "button", aria: "李小花，设置、帮助和更多", href: "" }],
  };
  const got = pickSelfAvatar([cjk], { selfName: "李小花" });
  assert.equal(got.strategy, SELF_AVATAR_STRATEGY.NAV_LABEL);
});

test("summarizeCandidates 带上祖先标签（miss 归因靠它）", () => {
  const got = summarizeCandidates([NAV_PEER, NAV_SELF]);
  assert.ok(got[0].labels.some((s) => s.includes("Micah Bindo")), "对端标签应可见");
  assert.ok(got[1].labels.some((s) => s.includes("Settings")), "自身按钮标签应可见");
});

test("summarizeCandidates 有界且不泄完整签名 URL", () => {
  const many = Array.from({ length: 20 }, (_, i) => ({
    src: `https://scontent.xx.fbcdn.net/${i}.jpg?oh=secret${i}`,
    alt: `n${i}`, w: i, h: i, hrefs: ["/a/", "/b/", "/c/"],
  }));
  const got = summarizeCandidates(many);
  assert.equal(got.length, 6);
  assert.equal(got[0].host, "scontent.xx.fbcdn.net");
  assert.equal(got[0].hrefs.length, 2);
  assert.equal(JSON.stringify(got).includes("secret"), false);
  assert.deepEqual(summarizeCandidates(null), []);
});

test("重采节奏：没头像先短周期补，有了转长周期，everyMs=0 整体关", () => {
  const cfg = { everyMs: 6 * 3600 * 1000, retryMs: 180 * 1000 };
  assert.equal(nextSelfProfileRecaptureMs({ hasAvatar: false }, cfg), 180 * 1000);
  assert.equal(nextSelfProfileRecaptureMs({ hasAvatar: true }, cfg), 6 * 3600 * 1000);
  assert.equal(nextSelfProfileRecaptureMs({ hasAvatar: false }, { everyMs: 0, retryMs: 1000 }), 0);
  // retryMs=0（只想要长周期）→ 无头像也走长周期，绝不返 0 把重采整个关掉
  assert.equal(nextSelfProfileRecaptureMs({ hasAvatar: false }, { everyMs: 5000, retryMs: 0 }), 5000);
});
