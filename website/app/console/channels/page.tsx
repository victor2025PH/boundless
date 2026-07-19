// /console/channels：渠道账号台账 —— 多平台对外账号登记（哪个号 · 哪个平台 · 挂哪个
// 实例 · 什么用途 · 谁保管）。落地渠道账号架构 2026-07「纪律三条」之 3：新号必须登记。
// 统计卡（按平台/状态计数）+ 搜索筛选（GET 表单）+ 列表 + 登记/编辑（admin+）。
import Link from "next/link";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  CHANNEL_PLATFORMS,
  CHANNEL_STATUSES,
  getChannelAccountStats,
  listChannelAccounts,
} from "@/lib/channels";
import { EditChannelAccountControl, NewChannelAccountForm } from "./ui";
import {
  Card,
  ChannelPlatformBadge,
  ChannelStatusBadge,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  SectionTitle,
  ShortId,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;

const PLATFORM_LABEL: Record<string, string> = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  messenger: "Messenger",
  line: "LINE",
  web: "web 客服",
  other: "其他",
};
const STATUS_LABEL: Record<string, string> = {
  active: "在用",
  pending: "待启用",
  paused: "已暂停",
  revoked: "已弃用",
};
/** 状态展示顺序（在用优先，弃用沉底）。 */
const STATUS_ORDER = ["active", "pending", "paused", "revoked"] as const;

export default function ChannelsPage({
  searchParams,
}: {
  searchParams: { q?: string; platform?: string; status?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const q = searchParams.q?.trim() || undefined;
  const platform = searchParams.platform?.trim() || undefined;
  const status = searchParams.status?.trim() || undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  const { rows, total } = listChannelAccounts({ q, platform, status, limit: LIMIT, offset });
  const stats = getChannelAccountStats();
  const hasFilter = !!(q || platform || status);

  return (
    <div>
      <PageHeader
        title="渠道账号"
        desc={
          <>
            多平台对外账号台账：哪个号 · 哪个平台 · 挂哪个实例 · 什么用途 · 谁保管。纪律：
            <b className="text-slate-300">一号一实例</b>（同一登录态双进程互踢+风控）、登录态跟实例数据根走
            （备份数据根即备份号）、<b className="text-slate-300">新号必须先登记再上线</b>。
            台账只存登录态位置备注，不存任何密钥。
          </>
        }
        actions={canWrite ? <NewChannelAccountForm /> : undefined}
      />

      <div className="mb-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <SectionTitle count={stats.total}>按平台</SectionTitle>
          {stats.total === 0 ? (
            <p className="text-xs text-slate-500">尚无登记 —— 平台分布在登记第一个号后出现。</p>
          ) : (
            <ul className="space-y-2.5">
              {CHANNEL_PLATFORMS.filter((p) => (stats.byPlatform[p] ?? 0) > 0).map((p) => (
                <li key={p} className="flex items-center justify-between gap-2">
                  <Link href={`/console/channels?platform=${p}`} className="hover:opacity-80">
                    <ChannelPlatformBadge platform={p} />
                  </Link>
                  <span className="text-sm font-semibold tabular-nums text-slate-200">{stats.byPlatform[p]}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card>
          <SectionTitle>按状态</SectionTitle>
          {stats.total === 0 ? (
            <p className="text-xs text-slate-500">尚无登记 —— 状态分布在登记第一个号后出现。</p>
          ) : (
            <ul className="space-y-2.5">
              {STATUS_ORDER.filter((st) => (stats.byStatus[st] ?? 0) > 0).map((st) => (
                <li key={st} className="flex items-center justify-between gap-2">
                  <Link href={`/console/channels?status=${st}`} className="hover:opacity-80">
                    <ChannelStatusBadge status={st} />
                  </Link>
                  <span className="text-sm font-semibold tabular-nums text-slate-200">{stats.byStatus[st]}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索显示名 / 号码 / 保管人 / 登录态位置"
          className={`${filterInputCls} w-64`}
        />
        <select name="platform" defaultValue={platform ?? ""} className={filterInputCls}>
          <option value="">全部平台</option>
          {CHANNEL_PLATFORMS.map((p) => (
            <option key={p} value={p}>
              {PLATFORM_LABEL[p] ?? p}
            </option>
          ))}
        </select>
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          <option value="">全部状态</option>
          {CHANNEL_STATUSES.map((st) => (
            <option key={st} value={st}>
              {STATUS_LABEL[st] ?? st}
            </option>
          ))}
        </select>
        <FilterSubmit />
        {hasFilter && (
          <Link href="/console/channels" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={hasFilter ? "没有匹配的渠道账号" : "渠道账号台账还是空的"}
          hints={
            hasFilter
              ? ["调整搜索或平台/状态筛选试试。"]
              : [
                  <span key="first">
                    第一个应登记的号：<b className="text-amber-300">官方总机 +639757135247</b>
                    （telegram · zhiliao 实例 · 总机接待）—— session 已找回，登录态在智聊实例数据根。
                  </span>,
                  canWrite
                    ? "点右上角「登记账号」录入；平台/实例/用途/保管人填全，后续封号换号有账可查。"
                    : "登记需要 admin 及以上角色 —— 请联系管理员录入。",
                  "web 官网客服 widget 无账号概念，也建议登记一条（platform=web），保持台账全貌。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["账号", "平台", "实例", "用途", "状态", "保管人", "登录态位置", "更新时间", ""]}>
            {rows.map((a) => (
              <tr key={a.id} className="hover:bg-slate-800/40">
                <Td>
                  <span className="font-medium text-slate-100" title={a.notes ?? undefined}>
                    {a.label}
                  </span>
                  {a.handle && <div className="mt-0.5 font-mono text-xs text-slate-400">{a.handle}</div>}
                  <div className="mt-0.5">
                    <ShortId id={a.id} />
                  </div>
                </Td>
                <Td>
                  <ChannelPlatformBadge platform={a.platform} />
                </Td>
                <Td>
                  {a.instance === "none" ? (
                    <span className="text-xs text-slate-600">未挂载</span>
                  ) : (
                    <span className="font-mono text-xs text-slate-300">{a.instance}</span>
                  )}
                </Td>
                <Td className="text-xs text-slate-300">{a.purpose}</Td>
                <Td>
                  <ChannelStatusBadge status={a.status} />
                </Td>
                <Td className="text-xs text-slate-300">{a.holder || "—"}</Td>
                <Td className="max-w-[220px] truncate font-mono text-[11px] text-slate-400">
                  <span title={a.session_ref ?? undefined}>{a.session_ref || "—"}</span>
                </Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(a.updated_at)}</Td>
                <Td>{canWrite ? <EditChannelAccountControl account={a} /> : null}</Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/channels" params={{ q, platform, status }} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
