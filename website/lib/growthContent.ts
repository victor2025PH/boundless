// /growth（智连系落地页）FAQ 单一数据源：GrowthLanding 渲染 + page.tsx JSON-LD 共用，
// 避免「页面上一套、结构化数据另一套」的口径漂移。
export const GROWTH_FAQ = {
  zh: [
    {
      q: "智拓需要准备多少台真机？",
      a: "智拓走邀请制：先评估你的获客场景与合规边界，再按目标规模规划主控 + Worker 真机集群，从几台起步逐步扩展，私有化部署交付，触达节奏按平台规则配置，规模与预算评估后确定。",
    },
    {
      q: "智聊会被客户看出是 AI 吗？",
      a: "拟人多语种翻译 + 人设话术，多数场景对方难以察觉；关键节点支持人工一键接管，成交节奏始终可控。",
    },
    {
      q: "智拓和智聊必须一起买吗？",
      a: "可以单选。智聊单独承接你现有流量做 AI 成交；智拓单独做获客引流进私域（邀请制评估）。组合使用时，从触达到成交串成一条可人工把关的闭环。",
    },
  ],
  en: [
    {
      q: "How many real devices does ReachX need?",
      a: "ReachX is invite-only: we assess your lead-gen scenario and compliance boundaries first, then plan the controller + worker device cluster for your target scale — starting small and growing — delivered as a private deployment, with pacing configured to platform rules. Scale and budget are set after the assessment.",
    },
    {
      q: "Will customers notice ChatX is AI?",
      a: "Human-like multilingual replies with persona scripts are hard to spot, and you can take over manually at key moments — the close stays under your control.",
    },
    {
      q: "Do I have to buy ReachX and ChatX together?",
      a: "No. ChatX alone closes the traffic you already have; ReachX alone feeds your private funnel (invite-only assessment). Combined, reach-to-close runs as one loop with human gating.",
    },
  ],
} as const;
