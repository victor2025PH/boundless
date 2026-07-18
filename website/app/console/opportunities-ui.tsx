"use client";

// 商机跟进交互件（schema v4）：状态小徽章 + 「跟进/赢单/忽略」行尾操作（带确认
// 面板与备注小输入）。admin+ 才由服务端页面渲染操作件（与 AttachIdentityForm 的
// canWrite 模式一致）；提交走 POST /api/console/opportunities → markOpportunity，
// 成功后 router.refresh() 让服务端直出的清单自然更新。
// 备注隐私纪律：note 只写运营自己的跟进话术/结果，绝不粘贴客户聊天原文。
// toast 走 app/console/ui.tsx 的 console-toast 事件总线（ConsoleToaster 挂在 layout，
// 此处只发事件不改那份文件）。

import { useState } from "react";
import { useRouter } from "next/navigation";
import { X } from "lucide-react";

export type OppLogStatus = "open" | "contacted" | "won" | "dismissed";

export interface OppLogInfo {
  status: OppLogStatus;
  note: string | null;
  acted_by: string | null;
  acted_at: string | null;
}

function emitToast(msg: string, ok = true) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("console-toast", { detail: { msg, ok } }));
  }
}

async function api<T = Record<string, unknown>>(
  url: string,
  init?: RequestInit & { json?: unknown }
): Promise<T> {
  const { json, ...rest } = init ?? {};
  const res = await fetch(url, {
    ...rest,
    headers: { "Content-Type": "application/json", ...(rest.headers ?? {}) },
    ...(json !== undefined ? { body: JSON.stringify(json) } : {}),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应按状态码处理 */
  }
  if (!res.ok) {
    throw new Error(String(data?.error ?? `请求失败 (${res.status})`));
  }
  return data as T;
}

// ── 状态小徽章（viewer 也可见；open=重新打开的行）──────────────────
const LOG_STATUS_STYLE: Record<OppLogStatus, string> = {
  open: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  contacted: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  won: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  dismissed: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};
const LOG_STATUS_LABEL: Record<OppLogStatus, string> = {
  open: "待跟进",
  contacted: "已联系",
  won: "已赢单",
  dismissed: "已忽略",
};

/** 商机行的跟进状态徽章：未标记（log=null）不占位；title 带经手人/时间/备注。 */
export function OpportunityLogBadge({ log }: { log: OppLogInfo | null }) {
  if (!log) return null;
  const title = [
    log.acted_by ? `经手：${log.acted_by}` : null,
    log.acted_at ? `时间：${log.acted_at.slice(0, 16).replace("T", " ")}` : null,
    log.note ? `备注：${log.note}` : null,
  ]
    .filter(Boolean)
    .join("\n");
  return (
    <span
      title={title || undefined}
      className={`inline-block shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${LOG_STATUS_STYLE[log.status]}`}
    >
      {LOG_STATUS_LABEL[log.status]}
    </span>
  );
}

// ── 行尾操作：跟进 / 赢单 / 忽略（点开确认面板，可附备注）──────────
const ACTIONS: { status: OppLogStatus; label: string; cls: string; confirmLabel: string }[] = [
  {
    status: "contacted",
    label: "跟进",
    cls: "border-sky-500/40 text-sky-300 hover:bg-sky-500/10",
    confirmLabel: "标记为「已联系」（保留在清单，信号值 −20）",
  },
  {
    status: "won",
    label: "赢单",
    cls: "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10",
    confirmLabel: "标记为「已赢单」（默认从清单隐藏）",
  },
  {
    status: "dismissed",
    label: "忽略",
    cls: "border-slate-600 text-slate-400 hover:border-rose-500/50 hover:text-rose-300",
    confirmLabel: "标记为「已忽略」（默认从清单隐藏）",
  },
];

export function OpportunityActions({
  oppKey,
  kind,
  customerId,
  toProduct,
  log,
}: {
  oppKey: string;
  kind: string;
  customerId: string;
  toProduct?: string | null;
  log: OppLogInfo | null;
}) {
  const router = useRouter();
  const [pending, setPending] = useState<OppLogStatus | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(status: OppLogStatus) {
    if (busy) return;
    setBusy(true);
    try {
      await api("/api/console/opportunities", {
        method: "POST",
        json: {
          opp_key: oppKey,
          kind,
          customer_id: customerId,
          to_product: toProduct ?? undefined,
          status,
          // 空备注不传 → 保留已有备注（重复标记不冲掉上次跟进语）
          ...(note.trim() ? { note: note.trim() } : {}),
        },
      });
      emitToast(`已标记：${LOG_STATUS_LABEL[status]}`);
      setPending(null);
      setNote("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (pending) {
    const action = ACTIONS.find((a) => a.status === pending)!;
    return (
      <div className="w-60 rounded-lg border border-slate-700 bg-slate-900 p-2 text-left shadow-xl">
        <div className="mb-1.5 flex items-start justify-between gap-2">
          <p className="text-[11px] leading-relaxed text-slate-300">{action.confirmLabel}</p>
          <button
            onClick={() => {
              setPending(null);
              setNote("");
            }}
            className="shrink-0 text-slate-500 hover:text-slate-300"
            aria-label="取消"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="备注（可空；只写跟进结果，勿贴聊天原文）"
          className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 outline-none placeholder:text-slate-600 focus:border-amber-500"
          onKeyDown={(e) => e.key === "Enter" && submit(pending)}
          autoFocus
        />
        <button
          onClick={() => submit(pending)}
          disabled={busy}
          className="mt-1.5 w-full rounded-lg bg-amber-500 px-2 py-1.5 text-xs font-semibold text-slate-950 hover:bg-amber-400 disabled:opacity-50"
        >
          {busy ? "提交中…" : "确认"}
        </button>
      </div>
    );
  }

  return (
    <span className="inline-flex gap-1">
      {ACTIONS.map((a) => (
        <button
          key={a.status}
          onClick={() => setPending(a.status)}
          disabled={busy || log?.status === a.status}
          title={log?.status === a.status ? `当前已是「${LOG_STATUS_LABEL[a.status]}」` : a.confirmLabel}
          className={`rounded-md border px-1.5 py-0.5 text-[11px] font-medium disabled:cursor-not-allowed disabled:opacity-40 ${a.cls}`}
        >
          {a.label}
        </button>
      ))}
    </span>
  );
}
