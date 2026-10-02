// /console/telegram：Telegram 运营配置 —— 管理的 bot（名称 / token）、群与频道（用途、启停、功能开关）、
// 客服工作时间、带来源码的邀请链接、工单概览。与 /console/channels（渠道账号台账）区分：这里的配置直接驱动 bot 行为。
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { isOverdue, listTickets, searchTickets, ticketStats } from "@/lib/chatx-tickets";
import { loadHub } from "@/lib/tg-hub-store";
import { publicHub, setupChecklist } from "@/lib/tg-hub-admin";
import { Card, DataTable, EmptyState, PageHeader, SectionTitle, Td, fmtDateTime } from "../parts";
import { listKnownIssueRows } from "@/lib/chatx-known-issues";
import { listPosts, seriesKeyFn, summarizeSeries } from "@/lib/tg-posts";
import { buildPostFunnel, readEvents } from "@/lib/chatx-report";
import { AddBotForm, AddChatForm, BotActions, CancelPostButton, ChatControls, InviteForm, KnownIssueEditor, PostForm, SupportForm } from "./ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ROLE_LABEL: Record<string, string> = { community: "社群", support: "客服群", channel: "频道" };
const POST_LABEL: Record<string, string> = { scheduled: "待发", sending: "发送中", sent: "已发出", failed: "失败", canceled: "已取消" };
const STATUS_LABEL: Record<string, string> = { open: "待接", claimed: "处理中", resolved: "已结案" };

export default async function TelegramPage({ searchParams = {} }: { searchParams?: { q?: string; status?: string; overdue?: string } }) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const hub = publicHub(await loadHub());
  const tickets = await listTickets();
  const knownIssues = await listKnownIssueRows();
  const allPosts = await listPosts();
  const posts = allPosts.slice(-20).reverse();
  const series = summarizeSeries(allPosts);
  const events = allPosts.some((p) => p.button) ? await readEvents(undefined, Date.now() - 90 * 86400_000) : [];
  const postFunnel = buildPostFunnel(events);
  const seriesFunnel = buildPostFunnel(events, seriesKeyFn(allPosts));
  const funnelCells = (f: { users: number; dlUsers: number } | undefined, hasButton: boolean) =>
    hasButton ? [<Td key="u" className="text-xs">{f?.users ?? 0}</Td>, <Td key="d" className="text-xs">{f?.dlUsers ?? 0}</Td>] : [<Td key="u">{""}</Td>, <Td key="d">{""}</Td>];
  const postChats = hub.chats.filter((c) => c.role !== "support" && c.enabled && c.botStatus === "administrator");
  const d7 = ticketStats(tickets, Date.now() - 7 * 86400_000);
  const q = (searchParams.q ?? "").trim().slice(0, 100);
  const status = ["open", "claimed", "resolved"].includes(searchParams.status ?? "") ? searchParams.status : undefined;
  const onlyOverdue = searchParams.overdue === "1";
  const filtering = !!(q || status || onlyOverdue);
  const slaMin = hub.support.slaMin;
  const overdueCount = tickets.filter((t) => isOverdue(t, slaMin)).length;
  const recent = searchTickets(tickets, q, { status, overdueMin: onlyOverdue ? slaMin : undefined, limit: filtering ? 100 : 20 });
  const botName = (id: string) => hub.bots.find((b) => b.id === id)?.name ?? id;
  const steps = setupChecklist(hub, knownIssues.filter((k) => k.enabled).length);
  const stepsDone = steps.filter((s) => s.done).length;
  const botOptions = hub.bots.map((b) => ({ id: b.id, label: `${b.name}${b.username ? ` @${b.username}` : ""}` }));

  return (
    <div className="space-y-5">
      <PageHeader
        title="Telegram 运营"
        desc="管理的 bot、客服群、社群和频道。这里的配置直接决定 bot 在哪些群里说话、转人工转到哪个客服群。bot 被拉进新群后会自动出现在下面（默认停用），设好用途再启用。"
        techNote={
          <>
            token 只保存在服务器（DATA_DIR/tg_hub.json，权限 600），页面只显示脱敏值；每个 bot 有独立 webhook 地址和 secret。
            内置推广 bot 的 token 在 .env.local 管理。群管理需要 bot 是管理员（或在 BotFather 里关闭 Privacy Mode）。
          </>
        }
      />

      <Card>
        <SectionTitle>
          配置清单 · 已完成 {stepsDone}/{steps.length}
        </SectionTitle>
        <ol className="grid gap-2 text-sm md:grid-cols-2">
          {steps.map((s, i) => (
            <li key={s.key} className={`rounded-xl border px-3 py-2 ${s.done ? "border-emerald-500/20 bg-emerald-500/5" : "border-amber-500/30 bg-amber-500/5"}`}>
              <div className="flex items-center gap-2">
                <span className={s.done ? "text-emerald-400" : "text-amber-400"}>{s.done ? "✓" : `${i + 1}.`}</span>
                <span className={s.done ? "text-slate-300" : "text-slate-100"}>{s.label}</span>
                {!s.done && (
                  <a href={s.href} className="ml-auto text-xs text-crown-400 underline">
                    去设置
                  </a>
                )}
              </div>
              {!s.done && <div className="mt-1 pl-5 text-xs text-slate-400">{s.hint}</div>}
            </li>
          ))}
        </ol>
      </Card>

      <Card id="bots">
        <SectionTitle count={hub.bots.length}>Bot</SectionTitle>
        {hub.bots.length === 0 ? (
          <EmptyState title="还没有 bot" hints={["在 BotFather 创建 bot 后，把名称和 token 填到下面"]} />
        ) : (
          <DataTable head={["名称", "用户名", "Token", "状态", "Webhook", "操作"]}>
            {hub.bots.map((b) => (
              <tr key={b.id}>
                <Td>
                  {b.name}
                  {b.builtin && <span className="ml-1.5 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">内置</span>}
                </Td>
                <Td>{b.username ? `@${b.username}` : "—"}</Td>
                <Td className="font-mono text-xs">{b.tokenMasked}</Td>
                <Td>{b.enabled ? <span className="text-emerald-400">启用</span> : <span className="text-slate-500">停用</span>}</Td>
                <Td className="text-xs text-slate-400">{b.builtin ? "由部署脚本设置" : b.webhookSetAt ? fmtDateTime(b.webhookSetAt) : "未设置"}</Td>
                <Td>
                  <BotActions bot={b} canWrite={canWrite} />
                </Td>
              </tr>
            ))}
          </DataTable>
        )}
        {canWrite && <AddBotForm />}
      </Card>

      <Card id="chats">
        <SectionTitle count={hub.chats.length}>群与频道</SectionTitle>
        {hub.chats.length === 0 ? (
          <EmptyState title="还没有群或频道" hints={["把 bot 拉进群 / 频道并设为管理员，会自动登记到这里", "或在下面按 id / @用户名 手动添加"]} />
        ) : (
          <DataTable head={["名称", "类型", "所属 bot", "bot 身份", "用途 / 启用 / 功能"]}>
            {hub.chats.map((c) => (
              <tr key={`${c.botId}:${c.chatId}`}>
                <Td>
                  <div>{c.title}</div>
                  <div className="font-mono text-[11px] text-slate-500">
                    {c.chatId}
                    {c.username ? ` · @${c.username}` : ""}
                  </div>
                </Td>
                <Td>
                  {c.type === "channel" ? "频道" : "群"}
                  {c.isForum ? " · 话题" : ""}
                </Td>
                <Td>{botName(c.botId)}</Td>
                <Td>{c.botStatus === "administrator" ? <span className="text-emerald-400">管理员</span> : <span className="text-amber-400">{c.botStatus ?? "未知"}</span>}</Td>
                <Td>
                  <ChatControls chat={c} canWrite={canWrite} roleLabel={ROLE_LABEL} />
                </Td>
              </tr>
            ))}
          </DataTable>
        )}
        {canWrite && botOptions.length > 0 && <AddChatForm bots={botOptions} />}
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card id="support">
          <SectionTitle>客服工作时间</SectionTitle>
          <p className="mb-3 text-xs text-slate-400">非工作时间转人工时，bot 会告诉用户消息已留好、上班后回复。工作时间内，待接工单超过「超时提醒」分钟没人回复，会在该工单话题里提醒一次；等到「升级管理员」分钟仍没人回复，再私信通知管理员一次（每 10 分钟巡检）。填了值班表，提醒和升级消息里会 @ 当时当班的客服（星期 1–7 = 周一到周日，时段可省略）。「已知问题追问」是 bot 给出解决步骤后，用户多久没点按钮就追问一次。</p>
          <SupportForm support={hub.support} canWrite={canWrite} />
        </Card>
        <Card id="invites">
          <SectionTitle count={hub.invites.length}>来源邀请链接</SectionTitle>
          <p className="mb-3 text-xs text-slate-400">生成「需申请」的邀请链接，链接名就是来源码；用户申请后 bot 自动批准、私聊欢迎，并按来源码统计。</p>
          {canWrite && hub.chats.length > 0 && <InviteForm chats={hub.chats.map((c) => ({ key: `${c.botId}|${c.chatId}`, label: `${c.title}（${ROLE_LABEL[c.role]}）` }))} />}
          <ul className="mt-3 space-y-1 text-xs">
            {hub.invites
              .slice(-10)
              .reverse()
              .map((i) => (
                <li key={i.link} className="flex flex-wrap gap-2 text-slate-300">
                  <span className="font-mono text-crown-400">{i.src}</span>
                  <span className="text-slate-500">{hub.chats.find((c) => c.chatId === i.chatId)?.title ?? i.chatId}</span>
                  <span className="select-all font-mono text-slate-400">{i.link}</span>
                </li>
              ))}
          </ul>
        </Card>
      </div>

      <Card>
        <SectionTitle count={posts.filter((p) => p.status === "scheduled").length}>定时发帖</SectionTitle>
        <p className="mb-3 text-xs text-slate-400">给已启用、bot 是管理员的频道 / 社群排期发帖，到点由对应 bot 发出（每 10 分钟巡检一次）。可以配一张图（直接上传，或填 https 图片地址；内容变成图片说明）和一个链接按钮，比如带来源码的 bot 链接；按钮点击次数会统计在「点击」列和日报里；t.me 的 bot 链接还会带上帖子号，能看到每条帖带进 bot 多少人、其中多少人请求了安装包（按人去重，近 90 天）。选「每天 / 每周」会在发出后自动排下一期，取消待发的那一期就停止；重复帖各期在下方「重复系列」里合计。</p>
        {canWrite &&
          (postChats.length ? (
            <PostForm chats={postChats.map((c) => ({ key: `${c.botId}|${c.chatId}`, label: `${c.title}（${ROLE_LABEL[c.role]}）` }))} />
          ) : (
            <EmptyState title="还没有可发帖的频道 / 社群" hints={["在上面把频道或社群设为启用，并确认 bot 是管理员"]} />
          ))}
        {posts.length > 0 && (
          <div className="mt-3">
            <DataTable head={["#", "发送时间", "发到", "内容", "状态", "点击", "进 bot", "下载人", "操作"]}>
              {posts.map((p) => (
                <tr key={p.id}>
                  <Td>
                    #{p.id}
                    {p.seriesId !== undefined && <div className="text-[11px] text-slate-500">系列 #{p.seriesId}</div>}
                  </Td>
                  <Td className="text-xs">{fmtDateTime(p.sentAt ?? p.sendAt)}</Td>
                  <Td className="text-xs">{hub.chats.find((c) => c.botId === p.botId && c.chatId === p.chatId)?.title ?? p.chatId}</Td>
                  <Td className="max-w-md whitespace-pre-wrap text-xs">
                    {p.text.length > 120 ? `${p.text.slice(0, 120)}…` : p.text}
                    {(p.photo || p.repeat) && (
                      <div className="mt-1 text-[11px] text-slate-500">
                        {p.photo ? (p.photo.startsWith("media:") ? "🖼 上传的配图" : "🖼 配图链接") : ""}
                        {p.photo && p.repeat ? " · " : ""}
                        {p.repeat ? (p.repeat === "daily" ? "🔁 每天" : "🔁 每周") : ""}
                      </div>
                    )}
                    {p.button && <div className="mt-1 text-[11px] text-crown-400">[{p.button.text}] {p.button.url}</div>}
                  </Td>
                  <Td className="text-xs">
                    {POST_LABEL[p.status] ?? p.status}
                    {p.error && <div className="text-[11px] text-rose-400">{p.error}</div>}
                  </Td>
                  <Td className="text-xs">{p.button ? (p.button.url.startsWith("https://") ? (p.clicks ?? 0) : "—") : ""}</Td>
                  {funnelCells(postFunnel.get(p.id), !!p.button)}
                  <Td>{canWrite && p.status === "scheduled" && <CancelPostButton id={p.id} />}</Td>
                </tr>
              ))}
            </DataTable>
          </div>
        )}
        {series.length > 0 && (
          <div className="mt-4">
            <SectionTitle count={series.filter((s) => s.active).length}>重复系列合计</SectionTitle>
            <DataTable head={["系列", "周期", "发到", "内容", "期数", "状态", "点击", "进 bot", "下载人"]}>
              {series.map((s) => {
                const f = seriesFunnel.get(s.seriesId);
                return (
                  <tr key={s.seriesId}>
                    <Td>#{s.seriesId}</Td>
                    <Td className="text-xs">{s.repeat === "daily" ? "每天" : "每周"}</Td>
                    <Td className="text-xs">{hub.chats.find((c) => c.botId === s.botId && c.chatId === s.chatId)?.title ?? s.chatId}</Td>
                    <Td className="max-w-md whitespace-pre-wrap text-xs">{s.text.length > 80 ? `${s.text.slice(0, 80)}…` : s.text}</Td>
                    <Td className="text-xs">
                      {s.issues} 期（已发 {s.sent}
                      {s.failed ? `，失败 ${s.failed}` : ""}）
                      <div className="text-[11px] text-slate-500">
                        {fmtDateTime(s.firstAt)} → {fmtDateTime(s.lastAt)}
                      </div>
                    </Td>
                    <Td className="text-xs">{s.active ? "在跑" : "已停"}</Td>
                    <Td className="text-xs">{s.clicks}</Td>
                    <Td className="text-xs">{f?.users ?? 0}</Td>
                    <Td className="text-xs">{f?.dlUsers ?? 0}</Td>
                  </tr>
                );
              })}
            </DataTable>
          </div>
        )}
      </Card>

      <Card id="known">
        <SectionTitle count={knownIssues.filter((k) => k.enabled).length}>已知问题库</SectionTitle>
        <p className="mb-3 text-xs text-slate-400">用户报障描述或设备最近的错误里出现关键词时，bot 直接给出解决步骤。内置条目可改文案、追加关键词或停用，「恢复默认」即回到内置；关键词按字面匹配，不区分大小写。</p>
        <DataTable head={["id", "匹配", "中文解决步骤", "状态", "操作"]}>
          {knownIssues.map((k) => (
            <tr key={k.id}>
              <Td className="font-mono text-xs">
                {k.id}
                {k.builtin && <span className="ml-1.5 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">{k.overridden ? "内置·已改" : "内置"}</span>}
              </Td>
              <Td className="max-w-[16rem] break-all text-[11px] text-slate-400">
                {k.builtin && <div className="font-mono">{k.pattern}</div>}
                {k.keywords.length > 0 && <div className="text-slate-300">{k.keywords.join("、")}</div>}
              </Td>
              <Td className="max-w-md text-xs">{k.zh}</Td>
              <Td>{k.enabled ? <span className="text-emerald-400">启用</span> : <span className="text-slate-500">停用</span>}</Td>
              <Td>
                <KnownIssueEditor row={k} canWrite={canWrite} />
              </Td>
            </tr>
          ))}
        </DataTable>
        <div className="mt-3">
          <KnownIssueEditor canWrite={canWrite} />
        </div>
      </Card>

      <Card>
        <SectionTitle count={d7.total}>客服工单（近 7 天）</SectionTitle>
        <div className="mb-3 flex flex-wrap gap-4 text-xs text-slate-300">
          <span>待接 {d7.open}</span>
          <span>处理中 {d7.claimed}</span>
          <span>已结案 {d7.resolved}</span>
          <span>首次响应中位数 {d7.medianFirstReplyMin === null ? "—" : `${d7.medianFirstReplyMin} 分钟`}</span>
          <span>
            满意 👍 {d7.good} / 👎 {d7.bad}
          </span>
          <span className={overdueCount ? "text-rose-400" : ""}>
            超时未回复 {overdueCount}（&gt;{slaMin} 分钟）
          </span>
        </div>
        <form method="get" className="mb-3 flex flex-wrap items-center gap-2">
          <input name="q" defaultValue={q} placeholder="搜 #工单号 / @用户名 / 昵称 / uid / 来源码 / 机器码 / 回执号" className="w-96 rounded-lg border border-slate-700 bg-ink-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-crown-500" />
          <select name="status" defaultValue={status ?? ""} className="rounded-lg border border-slate-700 bg-ink-950 px-2 py-2 text-sm text-slate-200">
            <option value="">全部状态</option>
            <option value="open">待接</option>
            <option value="claimed">处理中</option>
            <option value="resolved">已结案</option>
          </select>
          <label className="flex items-center gap-1 text-xs text-slate-300">
            <input type="checkbox" name="overdue" value="1" defaultChecked={onlyOverdue} /> 只看超时
          </label>
          <button className="rounded-lg bg-crown-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-crown-400">搜索</button>
          {filtering && (
            <a href="/console/telegram" className="text-xs text-slate-400 underline">
              清除（共 {recent.length} 条{recent.length >= 100 ? "，仅显示最近 100" : ""}）
            </a>
          )}
        </form>
        {recent.length === 0 ? (
          filtering ? (
            <EmptyState title="没有匹配的工单" hints={["换个关键词，或清除筛选"]} />
          ) : (
            <EmptyState title="还没有工单" hints={["启用一个客服群（bot 需为管理员）后，用户点「人工客服」或报障就会在这里出现"]} />
          )
        ) : (
          <DataTable head={["#", "时间", "用户", "类型", "来源", "机器码 / 回执", "状态", "客服"]}>
            {recent.map((t) => (
              <tr key={t.id}>
                <Td>#{t.id}</Td>
                <Td className="text-xs">{fmtDateTime(t.createdAt)}</Td>
                <Td>
                  {t.name}
                  {t.username ? ` @${t.username}` : ""}
                </Td>
                <Td>{t.kind === "human" ? "转人工" : `报障 · ${t.kind}`}</Td>
                <Td className="font-mono text-xs">{t.src}</Td>
                <Td className="font-mono text-[11px] text-slate-400">{[t.fp, t.diag].filter(Boolean).join(" · ") || "—"}</Td>
                <Td>
                  {STATUS_LABEL[t.status] ?? t.status}
                  {isOverdue(t, slaMin) && <span className="ml-1.5 rounded bg-rose-500/15 px-1.5 py-0.5 text-[10px] text-rose-400">超时</span>}
                </Td>
                <Td>{t.agent ?? "—"}</Td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>
    </div>
  );
}
