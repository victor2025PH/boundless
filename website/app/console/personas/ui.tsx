"use client";

// /console/personas 客户端交互组件：授权矩阵（grant/revoke）、归属客户、全域清除。
// 全部写操作 POST /api/console/personas/[id]（admin+），成功后 router.refresh()；
// toast 复用 layout 挂载的 ConsoleToaster（同名 window 事件）。

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Flame, Link2, ShieldAlert, X } from "lucide-react";

function emitToast(msg: string, ok = true) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("console-toast", { detail: { msg, ok } }));
  }
}

async function api<T = Record<string, unknown>>(url: string, init?: RequestInit & { json?: unknown }): Promise<T> {
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
  if (!res.ok) throw new Error(String(data?.error ?? `请求失败 (${res.status})`));
  return data as T;
}

const inputCls =
  "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-amber-500";
const btnPrimary =
  "rounded-lg bg-amber-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-amber-400 disabled:opacity-50";

// ── 授权矩阵：7 产品 × 授权/未授权切换 ─────────────────────────────
export interface GrantMatrixItem {
  productId: string;
  /** 产品中文名。 */
  label: string;
  /** 承载引擎（purge 指令目标）。 */
  engine: string;
  granted: boolean;
}

export function GrantToggle({
  personaId,
  item,
  canWrite,
  frozen,
}: {
  personaId: string;
  item: GrantMatrixItem;
  canWrite: boolean;
  /** purge_pending / purged 时授权冻结。 */
  frozen: boolean;
}) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  async function toggle() {
    if (busy || !canWrite || frozen) return;
    setBusy(true);
    const action = item.granted ? "revoke" : "grant";
    try {
      await api(`/api/console/personas/${personaId}`, {
        method: "POST",
        json: { action, product_id: item.productId },
      });
      emitToast(action === "grant" ? `已授权 ${item.label}` : `已撤销 ${item.label} 的授权`);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  const disabled = busy || !canWrite || frozen;
  return (
    <button
      onClick={toggle}
      disabled={disabled}
      title={
        frozen
          ? "清除中/已清除的人设授权冻结"
          : !canWrite
            ? "viewer 只读：切换授权需 admin 及以上角色"
            : item.granted
              ? `撤销 ${item.label} 的授权`
              : `授权给 ${item.label}`
      }
      className={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${
        item.granted
          ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
          : "border-slate-700 bg-slate-900/60 text-slate-500 hover:border-slate-500 hover:text-slate-300"
      }`}
    >
      {busy ? "…" : item.granted ? "已授权" : "未授权"}
    </button>
  );
}

// ── 归属客户（复用订单/授权行内控件的「选已有 / 快捷新建」模式）────
export interface PersonaCustomerOption {
  id: string;
  label: string;
}

export function AssignPersonaCustomerControl({
  personaId,
  customers,
}: {
  personaId: string;
  customers: PersonaCustomerOption[];
}) {
  const router = useRouter();
  const [mode, setMode] = useState<"closed" | "pick" | "create">("closed");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");
  const [newName, setNewName] = useState("");
  const [newContact, setNewContact] = useState("");

  async function assignTo(customerId: string) {
    await api(`/api/console/personas/${personaId}`, {
      method: "POST",
      json: { action: "assign_customer", customer_id: customerId },
    });
  }

  async function submitPick() {
    if (!selected || busy) return;
    setBusy(true);
    try {
      await assignTo(selected);
      emitToast("已归属客户");
      setMode("closed");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  async function submitCreate() {
    if (!newName.trim() || busy) return;
    setBusy(true);
    try {
      const data = await api<{ customer: { id: string; display_name: string | null } }>(
        "/api/console/customers",
        {
          method: "POST",
          json: { display_name: newName.trim(), primary_contact: newContact.trim() || undefined },
        }
      );
      await assignTo(data.customer.id);
      emitToast(`已创建并归属 ${data.customer.display_name ?? data.customer.id}`);
      setMode("closed");
      setNewName("");
      setNewContact("");
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (mode === "closed") {
    return (
      <button
        onClick={() => setMode("pick")}
        className="inline-flex items-center gap-1 rounded-lg border border-amber-500/40 px-2 py-1 text-xs font-medium text-amber-300 hover:bg-amber-500/10"
      >
        <Link2 className="h-3 w-3" />
        归属客户
      </button>
    );
  }

  return (
    <div className="w-56 rounded-lg border border-slate-700 bg-slate-900 p-2 text-left shadow-xl">
      <div className="mb-1.5 flex items-center justify-between">
        <div className="flex gap-1 text-[11px]">
          <button
            onClick={() => setMode("pick")}
            className={`rounded px-1.5 py-0.5 ${mode === "pick" ? "bg-amber-500/20 text-amber-300" : "text-slate-400 hover:text-slate-200"}`}
          >
            选已有
          </button>
          <button
            onClick={() => setMode("create")}
            className={`rounded px-1.5 py-0.5 ${mode === "create" ? "bg-amber-500/20 text-amber-300" : "text-slate-400 hover:text-slate-200"}`}
          >
            快捷新建
          </button>
        </div>
        <button onClick={() => setMode("closed")} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {mode === "pick" ? (
        customers.length ? (
          <div className="space-y-1.5">
            <select value={selected} onChange={(e) => setSelected(e.target.value)} className={`${inputCls} px-2 py-1.5 text-xs`}>
              <option value="">— 选择客户 —</option>
              {customers.map((c) => (
                <option key={c.id} value={c.id}>{c.label}</option>
              ))}
            </select>
            <button onClick={submitPick} disabled={busy || !selected} className={`${btnPrimary} w-full px-2 py-1.5 text-xs`}>
              {busy ? "提交中…" : "确认归属"}
            </button>
          </div>
        ) : (
          <p className="p-1 text-[11px] leading-relaxed text-slate-500">还没有客户，切到「快捷新建」直接建一位。</p>
        )
      ) : (
        <div className="space-y-1.5">
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="客户显示名 *" className={`${inputCls} px-2 py-1.5 text-xs`} />
          <input value={newContact} onChange={(e) => setNewContact(e.target.value)} placeholder="联系方式（可空）" className={`${inputCls} px-2 py-1.5 text-xs`} />
          <button onClick={submitCreate} disabled={busy || !newName.trim()} className={`${btnPrimary} w-full px-2 py-1.5 text-xs`}>
            {busy ? "提交中…" : "创建并归属"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── 全域清除（危险区，二次确认展示影响范围）────────────────────────
export function PersonaPurgeButton({
  personaId,
  displayName,
  /** 将收到清除指令的引擎（source_system + grants 推导，服务端已算好）。 */
  targets,
  /** 当前持有授权的产品（中文名）。 */
  grantedProducts,
}: {
  personaId: string;
  displayName: string;
  targets: string[];
  grantedProducts: string[];
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  async function doPurge() {
    if (busy) return;
    setBusy(true);
    try {
      const data = await api<{ targets?: string[] }>(`/api/console/personas/${personaId}`, {
        method: "POST",
        json: { action: "purge" },
      });
      emitToast(`已发起全域清除，指令已下发 ${data.targets?.length ?? 0} 个引擎`);
      setOpen(false);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-rose-500/50 px-3 py-2 text-sm font-semibold text-rose-300 hover:bg-rose-500/10"
      >
        <Flame className="h-4 w-4" />
        全域清除
      </button>
    );
  }

  return (
    <div className="rounded-xl border border-rose-500/40 bg-rose-950/30 p-4">
      <div className="mb-2 flex items-start justify-between gap-2">
        <p className="flex items-center gap-1.5 text-sm font-semibold text-rose-300">
          <ShieldAlert className="h-4 w-4 shrink-0" />
          确认对「{displayName}」发起全域清除？
        </p>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="取消">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-1.5 text-xs leading-relaxed text-rose-200/80">
        <p>
          影响范围：该人设当前在{" "}
          {grantedProducts.length ? (
            <b className="text-rose-200">{grantedProducts.join("、")}</b>
          ) : (
            <b className="text-rose-200">（无产品）</b>
          )}{" "}
          持有授权；将向{" "}
          <b className="text-rose-200">{targets.join("、")}</b>{" "}
          共 {targets.length} 个引擎下发清除指令。
        </p>
        <p>
          状态先置 <b>清除中（purge_pending）</b>，各引擎删除本地资产（脸模/声纹/话术/知识库）并回执后自动置{" "}
          <b>已清除（purged）</b>。已清除的人设不会被后续同步复活，此操作不可逆。
        </p>
      </div>
      <div className="mt-3 flex gap-2">
        <button
          onClick={doPurge}
          disabled={busy}
          className="rounded-lg bg-rose-500 px-3 py-1.5 text-xs font-semibold text-white hover:bg-rose-400 disabled:opacity-50"
        >
          {busy ? "下发中…" : "确认清除（不可逆）"}
        </button>
        <button
          onClick={() => setOpen(false)}
          disabled={busy}
          className="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500"
        >
          取消
        </button>
      </div>
    </div>
  );
}
