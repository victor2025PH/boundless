import { NEWBIE_PACK } from "./chatx-pricing";

/** /pricing 页 FAQ 单一数据源（实施77 GEO 批次2，2026-08-27 从 PricingPage.tsx 抽出）：
 *  可见 FAQ（PricingPage 客户端渲染）与 FAQPage JSON-LD（app/pricing/page.tsx 服务端
 *  注入）同源消费——schema 与页面内容绝不分叉。数字纪律照旧：能派生的从
 *  chatx-pricing 常量取，其余口径与费率表/加赠阶梯保持一致（改价同批改这里）。 */
export function faqItems(zh: boolean): { q: string; a: string }[] {
  return zh
    ? [
        {
          q: "Token 是什么？会不会像话费一样偷偷扣光？",
          a: "Token 是全站统一的 AI 用量单位，每个动作的消耗全部公示在上方费率表（如 AI 回复 10 Token/条）；后台会员中心实时显示余额与流水，余额低于 20% 会主动提醒。充值实付 12 个月有效（500U 及以上档 24 个月）、赠送部分 6 个月且先扣。",
        },
        {
          q: "Token 用完了会断线吗？",
          a: "不会。用尽后 AI 回复自动切换到本地免费模型、专业翻译降级为标准翻译（仍然免费不限量），会话永不中断；任意充值一笔立即恢复完整能力。",
        },
        {
          q: "为什么不做订阅了？我买过订阅怎么办？",
          a: "2026-08-21 起订阅档（基础 39 / 专业 99 / 旗舰 598）全部停售，计费收敛为一句话：免费开始，充多少用多少。已购订阅按原价服务到期，期内每月 Token 照常到账、扣减顺序不变；到期后直接充值即可，功能完全一致、无需迁移。有疑问随时找客服核对。",
        },
        {
          q: "首充加赠怎么算？充 99U 有加赠吗？",
          a: "首笔充值按档位一次性加赠：100U +5%、200U +10%、500U +20%、1000U +30%、5000U +35%、10000U +40%，每人仅一次，按到账金额向下取档——充 99U 落在 50U 档没有加赠，差 1U 就到 +5% 档，页面会提示。到账时按 账号 + 支付指纹 核验资格；退款会回收加赠部分。新人 6U 大礼包不占用首充资格。",
        },
        {
          q: "复充有优惠吗？VIP 累充等级是什么？",
          a: "有。累计已付充值达到门槛后，之后每一笔复充自动加赠：累计 ≥500U 复充 +3%、≥2000U +5%、≥10000U +8%——按台账自动生效，无需申请，长期有效。与首充加赠不叠加（你的第一笔走首充阶梯，之后每笔走 VIP 档）。",
        },
        {
          q: "新人大礼包是什么？",
          a: `注册 ${NEWBIE_PACK.windowHours} 小时内可用 ${NEWBIE_PACK.price}U 购买 ${NEWBIE_PACK.tokens.toLocaleString("en-US")} Token（2 倍率，约 $0.33/千，全场最低单价），每账号仅一次，且不占用首充加赠资格——之后的首笔正常充值仍可享受阶梯加赠。桌面端新用户启动时会弹出同款海报（带真实 72 小时倒计时）。`,
        },
        {
          q: "5000U / 10000U 大额档比小档多什么？",
          a: "除了更高加赠（+35% / +40%，折合单价低至 $0.48/千），大额档带服务权益：5000U 起配专属客户经理、发票/合同/对公通道、优先支持；10000U 另含团队用量分账报表与 API 对接支持。实付 Token 24 个月有效。",
        },
        {
          q: "企业合作和企业级部署有什么区别？",
          a: "企业合作＝还是用云端服务，只是量大：年用量超过 10000U 建议直接谈年框协议价（月结、对公、发票、专属 SLA）。企业级部署＝整套引擎装进你自己的服务器/内网 GPU：本地模型 Token 不限量、数据不出网，按「一次性实施 + 年授权维保」计价。两者都是联系商务面议，可先约演示或小规模试点。",
        },
        {
          q: "我是更早的老客户（入门版 / 团队版 / Token 包 / 字符包），怎么办？",
          a: "全部「只多不少」承接：存量订阅按原价服务到期；已购 Token 包照常有效，后续加购走充值档；字符包未用完的字符按 150 万字符 = 60,000 Token 免费换发。老的下单链接会自动跳到就近充值档，绝不落空。",
        },
        {
          q: "支持哪些支付方式？发票 / 对公怎么办？",
          a: "自助下单支持 USDT（TRC20）与银行卡（Stripe）；到账自动开通。企业对公、单笔 2000U 以上或定制方案请联系官方 Telegram 客服，有专属通道；5000U 及以上档自带发票与合同支持。",
        },
      ]
    : [
        {
          q: "What exactly is a token? Will it drain silently?",
          a: "Tokens are the single usage unit across the product. Every action's cost is published in the rate table above (e.g. an AI reply costs 10). The membership center shows your live balance and ledger, and we alert you below 20%. Paid top-ups stay valid 12 months (24 for 500U+); bonus tokens 6 months and spend first.",
        },
        {
          q: "What happens when tokens run out?",
          a: "Nothing breaks. AI replies fall back to the free local model and pro translation degrades to standard translation (still free and unlimited). Any top-up restores full capability instantly.",
        },
        {
          q: "Why no subscriptions anymore? I bought one — what now?",
          a: "As of 2026-08-21 the subscription tiers (Basic 39 / Pro 99 / Max 598) are discontinued. Billing is now one sentence: start free, top up as you go. Active subscriptions run to term at the old price with monthly tokens landing as usual; after expiry, just top up — identical features, nothing to migrate. Ping support with any question.",
        },
        {
          q: "How does the first-top-up bonus work? Does 99U earn one?",
          a: "Your first top-up earns a one-time tiered bonus: 100U +5%, 200U +10%, 500U +20%, 1000U +30%, 5000U +35%, 10000U +40% — once per person, tiered by the amount floor. 99U lands in the 50U tier with no bonus; 1U more reaches +5%, and the page tells you so. Eligibility is verified at credit time by account + payment fingerprint; refunds reclaim the bonus. The 6U newcomer pack does not consume this.",
        },
        {
          q: "Do repeat top-ups earn anything? What are VIP loyalty tiers?",
          a: "Yes. Once your lifetime paid top-ups pass a threshold, every later top-up earns a bonus automatically: ≥500U lifetime → +3%, ≥2000U → +5%, ≥10000U → +8% — applied from the ledger, no application needed, permanent. It doesn't stack with the first-top-up bonus (your first order uses the first-charge ladder; every order after uses your VIP tier).",
        },
        {
          q: "What's the newcomer pack?",
          a: `Within ${NEWBIE_PACK.windowHours}h of signup you can buy ${NEWBIE_PACK.tokens.toLocaleString("en-US")} tokens for ${NEWBIE_PACK.price}U (double rate, ~$0.33/1k — the lowest unit price here), once per account — and it doesn't consume your first-top-up bonus. New desktop users see the same offer as a launch popup with a live 72-hour countdown.`,
        },
        {
          q: "What do the 5000U / 10000U tiers add?",
          a: "Beyond the bigger bonus (+35% / +40%, effective rate down to ~$0.48/1k), large tiers carry service perks: from 5000U you get a dedicated account manager, invoice/contract/corporate billing and priority support; 10000U adds team usage split reports and API integration support. Paid tokens stay valid 24 months.",
        },
        {
          q: "Enterprise partnership vs. private deployment?",
          a: "Partnership = you still use the cloud service, just at volume: past ~10000U a year it pays to lock an annual frame (monthly settlement, corporate billing, invoices, dedicated SLA). Private deployment = the full engine installed on your own servers / on-prem GPUs: unlimited local-model tokens, data never leaves your network, priced as one-time setup + annual license & care. Both are quoted by sales — book a demo or a small pilot first.",
        },
        {
          q: "I'm on an older legacy plan (Entry / Team / token packs / char packs) — what now?",
          a: "Everything converts in your favor: active subscriptions run to term; purchased token packs stay valid; unused char-pack balances convert free at 1.5M chars = 60,000 tokens. Old order links redirect to the nearest top-up tier — nothing dead-ends.",
        },
        {
          q: "Payment methods? Invoices?",
          a: "Self-serve checkout takes USDT (TRC20) and cards (Stripe), with automatic activation. For corporate invoicing, single top-ups above 2000U, or custom deals, contact official Telegram support; tiers from 5000U include invoice & contract support.",
        },
      ];
}

/** faqItems → schema.org FAQPage（服务端页面注入用）。 */
export function pricingFaqJsonLd(zh: boolean): object {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faqItems(zh).map((f) => ({
      "@type": "Question",
      name: f.q,
      acceptedAnswer: { "@type": "Answer", text: f.a },
    })),
  };
}
