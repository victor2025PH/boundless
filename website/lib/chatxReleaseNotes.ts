/**
 * 智聊 ChatX 客户端版本更新记录（官网「版本更新记录」发布页的单一数据源）。
 *
 * 数据本体在 lib/chatx-release-notes.json（机器可写：由 scripts/gen-chatx-changelog.mjs
 * 在发版时 add 一条，保证「每次发包都生成 changelog 条目」；本文件只做类型标注与派生导出）。
 * 内容整理自发版事实源 public/downloads/announcements.json 与 desktop/build/_notes_*.txt。
 *
 * 版本号规则：语义化版本 1.0.NN（客户端关于页展示为 1.0NN，内部构建简写 10NN）。
 * ⚠ 日期：1.0.58 及以后取 announcements.json 的发布时刻；1.0.58 之前（1.0.40/46/56）为
 *   依据发版说明与 git 版本号推定的发布日（1.0.46 由说明正文明确为 2026-08-21）。
 *
 * 发布新版本：跑 `node scripts/gen-chatx-changelog.mjs add --version X.Y.Z ...`（勿手改 JSON），
 * gate:content 检查 6 会强制 changelog 覆盖 manifest.json 的已发布版本。
 */

import rawNotes from "@/lib/chatx-release-notes.json";

/** 版本条目的主要变更类型（决定 UI 徽章配色与文案） */
export type ChatxReleaseTag = "feature" | "fix" | "improve" | "security";

/** 单个版本的更新记录（zh/en 双语，与站点语言切换联动） */
export interface ChatxReleaseNote {
  /** 版本号，如 "1.0.61"（不带 v 前缀） */
  version: string;
  /** 发布日期 YYYY-MM-DD */
  date: string;
  /** 一句话主题 */
  title: { zh: string; en: string };
  /** 变更类型标签（第一个为主标签） */
  tags: ChatxReleaseTag[];
  /** 更新要点列表 */
  highlights: { zh: string[]; en: string[] };
}

/** 按版本号从新到旧排列（JSON 已按此顺序维护） */
export const CHATX_RELEASE_NOTES: ChatxReleaseNote[] = rawNotes as ChatxReleaseNote[];

/** 最新对外发布版本号（下载卡片兜底与 JSON-LD softwareVersion 共用） */
export const CHATX_LATEST_VERSION = CHATX_RELEASE_NOTES[0]?.version ?? "";
