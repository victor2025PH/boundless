// /growth（智连系落地页）FAQ 单一数据源：GrowthLanding 渲染 + page.tsx JSON-LD 共用，
// 避免「页面上一套、结构化数据另一套」的口径漂移。
export const GROWTH_FAQ = {
  zh: [
    {
      q: "智拓需要准备多少台真机？",
      a: "按目标获客量配置：主控 + Worker 集群可从几台扩展到上百台，配套防封风控与 VPN 池，规模与预算按需报价。",
    },
    {
      q: "智聊会被客户看出是 AI 吗？",
      a: "拟人多语种翻译 + 人设话术，多数场景对方难以察觉；关键节点支持人工一键接管，成交节奏始终可控。",
    },
    {
      q: "智拓和智聊必须一起买吗？",
      a: "可以单选。智拓单独做获客引流进私域，智聊单独承接你现有流量做 AI 成交；组合使用时获客到成交全链路自动化。",
    },
  ],
  en: [
    {
      q: "How many real devices does ReachX need?",
      a: "Sized to your lead-gen target: the controller + worker cluster scales from a few devices to hundreds, with anti-ban controls and a VPN pool. Quoted by scale.",
    },
    {
      q: "Will customers notice ChatX is AI?",
      a: "Human-like multilingual replies with persona scripts are hard to spot, and you can take over manually at key moments — the close stays under your control.",
    },
    {
      q: "Do I have to buy ReachX and ChatX together?",
      a: "No. ReachX alone feeds your private funnel; ChatX alone closes the traffic you already have. Combined, the whole reach-to-close loop runs automatically.",
    },
  ],
} as const;
