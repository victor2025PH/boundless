// 统一「中文表单卡片」渲染器（与承接引擎 webhook_notifier 的卡片同款）。
// 目的：每条推送都自解释——顶部一眼看级别+类别，底部标明来源，纯中文、可扫读。
// 纯文本（不含 HTML 标签）：在 parse_mode:HTML 或纯文本两种发送方式下都正常显示。

export type Sev = "critical" | "warn" | "info" | "recover" | "biz" | "digest";

const SEV_LABEL: Record<Sev, string> = {
  critical: "🔴 严重",
  warn: "🟠 警告",
  info: "🔵 提示",
  recover: "✅ 恢复",
  biz: "💰 业务",
  digest: "📊 日报",
};

const RULE = "━━━━━━━━━━━━━━";

function cnTime(): string {
  // UTC+8，形如 08-10 10:30
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(5, 16).replace("T", " ");
}

/**
 * 渲染统一卡片。
 * @param sev   级别（六档）
 * @param cat   类别（服务器/算力/运营/业务/机器运维…）
 * @param title 一句话中文标题
 * @param body  正文（已是中文说明；可多行）
 * @param source 来源系统（默认「官网(bd2026)」）
 * @param rawTag 原始事件名（排查用，可选）
 */
export function card(opts: {
  sev: Sev;
  cat: string;
  title: string;
  body: string;
  source?: string;
  rawTag?: string;
}): string {
  const source = opts.source || "官网(bd2026)";
  const parts = [
    `${SEV_LABEL[opts.sev]} · ${opts.cat}`,
    RULE,
    `📌 ${opts.title}`,
    (opts.body || "").trim(),
    RULE,
    `🏷️ 来源：${source}　🕒 ${cnTime()}`,
    opts.rawTag ? `🏳️ 原始事件：${opts.rawTag}（仅排查用）` : "",
  ];
  return parts.filter((p) => p !== "").join("\n").trim();
}
