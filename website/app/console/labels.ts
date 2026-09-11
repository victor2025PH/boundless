// /console 展示标签单源（2026-09-10 中文化）。
//
// 纪律：
//   · 所有「枚举值 → 界面文字」只在这里定义；页面 / parts.tsx / ui.tsx 一律 import，不再各写一套。
//   · 枚举**取值**（active / trial / basic / viewer / tg …）是引擎、ledger schema、API 的契约，
//     绝不改；这里只管显示。筛选表单的 <option value> 仍传原值。
//   · 无 "use client"、不 import JSON：纯常量 + 纯函数，服务端组件与客户端组件都能直接用，
//     且不会把产品事实 JSON 打进浏览器包。SKU 名称查表见 ./sku-names.ts（仅服务端）。
//   · 品牌名（ChatX / Telegram / WhatsApp / LINE / Stripe）、编号格式、IP、URL 保留原文；
//     英文枚举值只允许出现在 title / tooltip。

export type LabelMap = Record<string, string>;

/** 查表：未知键回退到 fallback（默认原值），空值回退到 "—"。 */
export function lbl(map: LabelMap, key: string | null | undefined, fallback?: string): string {
  if (key === null || key === undefined || key === "") return fallback ?? "—";
  return map[key] ?? fallback ?? key;
}

/** 下拉选项形状：value 传原枚举，label 显示中文。 */
export interface LabeledOption {
  value: string;
  label: string;
}
export function toOptions(map: LabelMap, order?: readonly string[]): LabeledOption[] {
  const keys = order ?? Object.keys(map);
  return keys.map((value) => ({ value, label: map[value] ?? value }));
}

// ── 产品线（与 lib/ledger.ts PRODUCT_IDS / EVENT_CONTRACT product_id 对齐）─────
/** 中文 + 英文品牌名：列表、徽章、下拉主用。 */
export const PRODUCT_LABEL: LabelMap = {
  zhituo: "智拓 ReachX",
  zhiliao: "智聊 ChatX",
  tongyi: "通译 LingoX",
  tongchuan: "通传 VoxX",
  huansheng: "幻声 VoiceX",
  huanying: "幻影 LiveX",
  huanyan: "幻颜 FaceX",
  zhikong: "智控 MatrixX",
  website: "官网服务",
  platform: "平台层",
};
/** 只留中文：窄列 / 图例 / 数字旁。 */
export const PRODUCT_SHORT: LabelMap = {
  zhituo: "智拓",
  zhiliao: "智聊",
  tongyi: "通译",
  tongchuan: "通传",
  huansheng: "幻声",
  huanying: "幻影",
  huanyan: "幻颜",
  zhikong: "智控",
  website: "官网",
  platform: "平台",
};
/** 安装包下载台账的产品键（lib/download-ledger.ts classifyPath）。 */
export const DOWNLOAD_PRODUCT_LABEL: LabelMap = {
  chatx: "智聊 ChatX",
  avatarhub: "幻境 STUDIO",
  matrixx: "智控 MatrixX",
};

// ── 授权来源系统 / 承载引擎 ────────────────────────────────────────────
/** ledger_import.schema.json 的 source_system 枚举——只有这两个有导出器。 */
export const LEDGER_SOURCE_SYSTEMS = ["avatarhub", "chengjie"] as const;
export const SYSTEM_LABEL: LabelMap = {
  avatarhub: "幻境引擎",
  chengjie: "成杰引擎（智聊 · 通译）",
  huoke: "获客引擎",
  tgkz2026: "智控引擎",
  website: "官网",
};
export const SYSTEM_SHORT: LabelMap = {
  avatarhub: "幻境引擎",
  chengjie: "成杰引擎",
  huoke: "获客引擎",
  tgkz2026: "智控引擎",
  website: "官网",
};

// ── 授权（licenses 表；状态枚举与 schema / export_chengjie / ledger_outbox 一致）──
export const LICENSE_STATUS_LABEL: LabelMap = {
  active: "生效中",
  trial: "试用",
  expired: "已过期",
  revoked: "已吊销",
  unknown: "未判定",
};
export const LICENSE_STATUS_ORDER = ["active", "trial", "expired", "revoked", "unknown"] as const;
/** chengjie plan（license_manager.py：community | basic | pro | flagship）。 */
export const LICENSE_PLAN_LABEL: LabelMap = {
  community: "社区版",
  basic: "基础版",
  pro: "专业版",
  flagship: "旗舰版",
};
/** avatarhub edition（ledger schema：trial | standard | pro；enterprise 引擎侧按 pro 签发）。 */
export const LICENSE_EDITION_LABEL: LabelMap = {
  trial: "试用",
  standard: "标准版",
  pro: "专业版",
  enterprise: "企业版",
};

/** 席位：chengjie seats=0 = 不限（schema 与 license_manager 同义）；null = 未知。 */
export function seatsLabel(seats: number | null | undefined, sourceSystem?: string | null): string {
  if (seats === null || seats === undefined) return "—";
  if (seats === 0 && sourceSystem === "chengjie") return "不限";
  return String(seats);
}

/** 绑定机器：'*' = 站点授权；长指纹缩短。返回 { text, title }。 */
export function fingerprintLabel(fp: string | null | undefined): { text: string; title?: string } {
  if (!fp) return { text: "—" };
  if (fp === "*") return { text: "站点授权（不限机器）" };
  return fp.length > 14 ? { text: `${fp.slice(0, 9)}…`, title: fp } : { text: fp };
}

// ── 订单 / 留资 ─────────────────────────────────────────────────────────
export const ORDER_STATUS_LABEL: LabelMap = {
  pending: "待支付",
  paid: "已支付",
  activated: "已开通",
  cancelled: "已取消",
  refunded: "已退款",
};
export const ORDER_STATUS_ORDER = ["pending", "paid", "activated", "cancelled"] as const;
export const LEAD_STATUS_LABEL: LabelMap = {
  new: "新留资",
  contacted: "已联系",
  won: "已成交",
  lost: "已流失",
};
export const LEAD_STATUS_ORDER = ["new", "contacted", "won", "lost"] as const;

// ── 控制台账号角色（lib/console-users.ts CONSOLE_ROLES）──────────────────
export const ROLE_LABEL: LabelMap = {
  master: "主账号",
  admin: "运营",
  viewer: "只读",
};
export const ROLE_ORDER = ["viewer", "admin", "master"] as const;
/** 角色一句话说明（用户页 / 下拉 title）。 */
export const ROLE_DESC: LabelMap = {
  viewer: "只能查看，不能做任何写操作",
  admin: "可做客户归属、建档、核销等日常写操作",
  master: "运营权限之外，另可管理控制台账号",
};

// ── 身份标识类型（lib/ledger.ts IDENTITY_KINDS）──────────────────────────
export const IDENTITY_KIND_LABEL: LabelMap = {
  contact: "联系方式",
  tg: "Telegram",
  email: "邮箱",
  phone: "电话",
  fingerprint: "设备指纹",
};
export const IDENTITY_KIND_ORDER = ["contact", "tg", "email", "phone", "fingerprint"] as const;

// ── 渠道账号台账（lib/channels.ts CHECK 约束）────────────────────────────
export const CHANNEL_PLATFORM_LABEL: LabelMap = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  messenger: "Messenger",
  line: "LINE",
  web: "官网客服",
  other: "其他",
};
/** 登记表单里带接入方式的长标签（技术细节进 title，不进正文）。 */
export const CHANNEL_PLATFORM_FORM_LABEL: LabelMap = {
  telegram: "Telegram（协议登录）",
  whatsapp: "WhatsApp（网页登录）",
  messenger: "Messenger（网页登录）",
  line: "LINE（桌面登录）",
  web: "官网在线客服",
  other: "其他平台",
};
export const CHANNEL_PLATFORM_ORDER = ["telegram", "whatsapp", "messenger", "line", "web", "other"] as const;
export const CHANNEL_INSTANCE_LABEL: LabelMap = {
  zhiliao: "智聊实例",
  tongyi: "通译实例",
  avatarhub: "幻境引擎",
  huoke: "获客引擎",
  website: "官网",
  none: "暂未挂载",
};
export const CHANNEL_INSTANCE_ORDER = ["zhiliao", "tongyi", "avatarhub", "huoke", "website", "none"] as const;
export const CHANNEL_STATUS_LABEL: LabelMap = {
  active: "在用",
  pending: "待启用",
  paused: "已暂停",
  revoked: "已弃用",
};
export const CHANNEL_STATUS_ORDER = ["active", "pending", "paused", "revoked"] as const;

// ── 人设总线 ────────────────────────────────────────────────────────────
export const PERSONA_STATUS_LABEL: LabelMap = {
  active: "在用",
  archived: "已归档",
  purge_pending: "清除中",
  purged: "已清除",
};
export const PERSONA_STATUS_ORDER = ["active", "archived", "purge_pending", "purged"] as const;
/** 四槽位：短名（列表/统计）与说明（title）。 */
export const PERSONA_SLOT_LABEL: LabelMap = {
  face: "形象",
  voice: "声纹",
  prompt: "话术",
  knowledge: "知识库",
};
export const PERSONA_SLOT_DESC: LabelMap = {
  face: "形象 / 脸模",
  voice: "声纹克隆",
  prompt: "语言人格 / 话术",
  knowledge: "术语库 / 知识库",
};
export const PURGE_STATE_LABEL: LabelMap = {
  pending: "待回执",
  acked: "已回执",
};

// ── 跨售商机（lib/opportunities.ts）─────────────────────────────────────
export const OPP_KIND_LABEL: LabelMap = {
  persona_cross_sell: "数字分身可复用",
  product_gap_cross_sell: "同系互补品未购",
  expiring_renewal: "续费在即",
};
export const OPP_KIND_DESC: LabelMap = {
  persona_cross_sell: "客户已有形象 / 声纹 / 话术资产，可直接开通同资产能支撑的其他产品",
  product_gap_cross_sell: "买了同系产品 A，还没买互补品 B",
  expiring_renewal: "30 天内到期的授权，建议续费",
};
export const OPP_STATUS_LABEL: LabelMap = {
  open: "待跟进",
  contacted: "已联系",
  won: "已赢单",
  dismissed: "已忽略",
};
/** 信号值 → 优先级（数值只进 title）。阈值：≥85 高 / ≥65 中 / 其余 低。 */
export function opportunityPriority(signal: number): { label: "高" | "中" | "低"; tone: "danger" | "warning" | "neutral" } {
  if (signal >= 85) return { label: "高", tone: "danger" };
  if (signal >= 65) return { label: "中", tone: "warning" };
  return { label: "低", tone: "neutral" };
}

// ── 审计流水（audit 表 action / entity；键来自 lib/ledger.ts / console-users.ts / opportunities.ts）──
export const AUDIT_ACTION_LABEL: LabelMap = {
  "customer.create": "新建客户",
  "customer.update": "更新客户主档",
  "identity.attach": "挂接身份标识",
  "identity.detach": "解绑身份标识",
  assign_customer: "归属客户",
  unassign_customer: "解除归属",
  auto_link: "自动归属（命中已有身份）",
  auto_create: "自动建档（强信号）",
  "opportunity.mark": "商机跟进",
  "user.create": "新建控制台用户",
  "user.bootstrap": "初始化主账号",
  "user.set_role": "修改角色",
  "user.set_enabled": "启用 / 禁用用户",
  "user.reset_password": "重置密码",
  "session.revoke": "撤销会话",
  "persona.grant": "人设授权产品",
  "persona.revoke": "人设撤销产品",
  "persona.assign_customer": "人设归属客户",
  "persona.purge": "人设全域清除",
  "channel.create": "登记渠道账号",
  "channel.update": "更新渠道账号",
  "trial.redeem": "核销绑定码",
};
export const AUDIT_ENTITY_LABEL: LabelMap = {
  customer: "客户",
  order: "订单",
  lead: "留资",
  license: "授权",
  user: "控制台用户",
  opportunity: "商机",
  persona: "人设",
  channel: "渠道账号",
  trial: "试用领取",
};
/** 审计常用动作筛选（值传原 key）。 */
export const AUDIT_ACTION_QUICK = [
  "customer.create",
  "customer.update",
  "identity.attach",
  "identity.detach",
  "assign_customer",
  "opportunity.mark",
] as const;

// ── 试用台账 / 联系方式类型 ─────────────────────────────────────────────
export const TRIAL_CLAIM_STATUS_LABEL: LabelMap = {
  pending: "待签发",
  issued: "已激活",
  rejected: "已驳回",
};
/** 试用 / 挽回名单的联系方式类型（trial-claim-store contactKind）。 */
export const CONTACT_KIND_LABEL: LabelMap = {
  tg: "Telegram",
  telegram: "Telegram",
  email: "邮箱",
  phone: "电话",
  contact: "联系方式",
  fingerprint: "设备指纹",
  other: "其他",
};
export const REFERRAL_FLAG_LABEL: LabelMap = {
  ip_cluster: "同 IP 聚集",
};

// ── 客户端错误回传 ─────────────────────────────────────────────────────
export const LOG_LEVEL_LABEL: LabelMap = {
  CRITICAL: "严重",
  ERROR: "错误",
  WARNING: "警告",
  WARN: "警告",
  INFO: "信息",
  DEBUG: "调试",
};

// ── 安装包下载台账（lib/download-ledger.ts）─────────────────────────────
export const DOWNLOAD_KIND_LABEL: LabelMap = {
  human: "外部",
  office: "办公室",
  bot: "爬虫",
  blocked: "已拦",
};
export const DOWNLOAD_FLAG_LABEL: LabelMap = {
  sweep: "横扫多产品",
  ratelimit: "限流",
  bot: "爬虫特征",
  noua: "无浏览器标识",
  range: "续传",
  office: "办公室",
};
export const DOWNLOAD_VIA_LABEL: LabelMap = {
  r2: "云端镜像",
  local: "本站",
  blocked: "拦截",
};
export const DOWNLOAD_CHANNEL_LABEL: LabelMap = {
  internal: "内部",
  public: "公开",
};

// ── 服务器台账（deploy/machines.json role）─────────────────────────────
export const MACHINE_ROLE_LABEL: LabelMap = {
  dev: "开发机",
  compute: "算力节点",
};

// ── 授权 raw 解析（额度列；无用量字段时只显示签发额度）────────────────
function parseJsonObject(raw: string | null | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    const obj = typeof raw === "string" ? JSON.parse(raw) : raw;
    return obj && typeof obj === "object" && !Array.isArray(obj) ? (obj as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function asPositiveInt(v: unknown): number | null {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
}

/** 授权 raw.payload 的额度展示。无剩余用量时不做「<10%」预警（台账目前不存消耗）。 */
export function licenseQuotaLabel(raw: string | null | undefined): { text: string; title?: string } {
  const obj = parseJsonObject(raw);
  const payload =
    obj.payload && typeof obj.payload === "object" && !Array.isArray(obj.payload)
      ? (obj.payload as Record<string, unknown>)
      : obj;
  const chars = asPositiveInt(payload.included_chars);
  const monthly = asPositiveInt(payload.included_tokens_monthly);
  const bits: string[] = [];
  if (chars) bits.push(`${chars.toLocaleString("zh-CN")} 字符`);
  if (monthly) bits.push(`每月 ${monthly.toLocaleString("zh-CN")} Token`);
  if (!bits.length) return { text: "—" };
  const title = chars === 1_000_000 ? "约 10,000 Token（官网口径，按 100 字符 ≈ 1 Token）" : undefined;
  return { text: bits.join(" · "), title };
}
