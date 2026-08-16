"use client";

// /console/channels 客户端交互组件：登记新账号、行内编辑（含软状态变更）。
// 写操作 POST/PATCH /api/console/channels（admin+），成功后 router.refresh()；
// toast 复用 layout 挂载的 ConsoleToaster（同名 window 事件）。

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Pencil, Plus, X } from "lucide-react";

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

// 枚举选项（值与 lib/channels.ts 的 CHECK 约束一致）
const PLATFORM_OPTIONS = [
  { value: "telegram", label: "Telegram（MTProto 协议号）" },
  { value: "whatsapp", label: "WhatsApp（baileys 侧车）" },
  { value: "messenger", label: "Messenger（网页会话侧车）" },
  { value: "line", label: "LINE（RPA 桌面登录）" },
  { value: "web", label: "web 官网客服 widget" },
  { value: "other", label: "其他平台" },
] as const;

const INSTANCE_OPTIONS = [
  { value: "zhiliao", label: "zhiliao 智聊实例" },
  { value: "tongyi", label: "tongyi 通译实例" },
  { value: "avatarhub", label: "avatarhub 幻境系引擎" },
  { value: "huoke", label: "huoke 获客引擎" },
  { value: "website", label: "website 官网" },
  { value: "none", label: "none 暂未挂载" },
] as const;

const PURPOSE_OPTIONS = ["总机接待", "交付服务", "测试", "投放专号", "其他"] as const;

const STATUS_OPTIONS = [
  { value: "active", label: "active 在用" },
  { value: "pending", label: "pending 待启用" },
  { value: "paused", label: "paused 已暂停" },
  { value: "revoked", label: "revoked 已弃用" },
] as const;

interface ChannelFormValue {
  platform: string;
  label: string;
  handle: string;
  instance: string;
  purpose: string;
  holder: string;
  status: string;
  session_ref: string;
  notes: string;
}

const EMPTY_FORM: ChannelFormValue = {
  platform: "telegram",
  label: "",
  handle: "",
  instance: "none",
  purpose: "其他",
  holder: "",
  status: "active",
  session_ref: "",
  notes: "",
};

/** 新建/编辑共用的字段区（受控表单）。 */
function ChannelFields({ value, onChange }: { value: ChannelFormValue; onChange: (v: ChannelFormValue) => void }) {
  const set = (k: keyof ChannelFormValue) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    onChange({ ...value, [k]: e.target.value });
  return (
    <div className="space-y-2.5">
      <div className="flex gap-2">
        <select value={value.platform} onChange={set("platform")} className={`${inputCls} w-56`}>
          {PLATFORM_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select value={value.status} onChange={set("status")} className={inputCls}>
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>
      <input value={value.label} onChange={set("label")} placeholder="显示名 *（如：官方总机）" className={inputCls} />
      <input value={value.handle} onChange={set("handle")} placeholder="号码/用户名（如 +639757135247、@boundless_hq）" className={inputCls} />
      <div className="flex gap-2">
        <select value={value.instance} onChange={set("instance")} className={inputCls}>
          {INSTANCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        <select value={value.purpose} onChange={set("purpose")} className={inputCls}>
          {PURPOSE_OPTIONS.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </div>
      <input value={value.holder} onChange={set("holder")} placeholder="保管人（谁拿着手机号/收验证码）" className={inputCls} />
      <input
        value={value.session_ref}
        onChange={set("session_ref")}
        placeholder="登录态位置备注（如 智聊实例 sessions/639952947442.session）"
        className={inputCls}
      />
      <input value={value.notes} onChange={set("notes")} placeholder="备注（可空）" className={inputCls} />
      <p className="text-[11px] leading-relaxed text-slate-500">
        登录态位置只写「文件在哪」的纯文本备注，任何密钥/密码/session 文件本体都不进台账。
      </p>
    </div>
  );
}

// ── 登记新账号（admin+ 可见；viewer 由服务端页面隐藏入口）──────────
export function NewChannelAccountForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<ChannelFormValue>(EMPTY_FORM);

  async function submit() {
    if (!form.label.trim() || busy) return;
    setBusy(true);
    try {
      await api("/api/console/channels", {
        method: "POST",
        json: {
          platform: form.platform,
          label: form.label.trim(),
          handle: form.handle.trim() || undefined,
          instance: form.instance,
          purpose: form.purpose,
          holder: form.holder.trim() || undefined,
          status: form.status,
          session_ref: form.session_ref.trim() || undefined,
          notes: form.notes.trim() || undefined,
        },
      });
      emitToast(`已登记渠道账号「${form.label.trim()}」`);
      setOpen(false);
      setForm(EMPTY_FORM);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className={`${btnPrimary} flex items-center gap-1.5`}>
        <Plus className="h-4 w-4" />
        登记账号
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-amber-500/25 bg-slate-900/70 p-4 sm:max-w-md">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-amber-300">登记渠道账号</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>
      <ChannelFields value={form} onChange={setForm} />
      <button onClick={submit} disabled={busy || !form.label.trim()} className={`${btnPrimary} mt-2.5 w-full`}>
        {busy ? "登记中…" : "登记账号"}
      </button>
    </div>
  );
}

// ── 行内编辑（含状态变更，一次 PATCH 提交）─────────────────────────
export interface ChannelAccountItem {
  id: string;
  platform: string;
  label: string;
  handle: string | null;
  instance: string;
  purpose: string;
  holder: string | null;
  status: string;
  session_ref: string | null;
  notes: string | null;
}

export function EditChannelAccountControl({ account }: { account: ChannelAccountItem }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<ChannelFormValue>(() => ({
    platform: account.platform,
    label: account.label,
    handle: account.handle ?? "",
    instance: account.instance,
    purpose: account.purpose,
    holder: account.holder ?? "",
    status: account.status,
    session_ref: account.session_ref ?? "",
    notes: account.notes ?? "",
  }));

  async function submit() {
    if (!form.label.trim() || busy) return;
    setBusy(true);
    try {
      await api("/api/console/channels", {
        method: "PATCH",
        json: {
          id: account.id,
          platform: form.platform,
          label: form.label.trim(),
          handle: form.handle.trim() || null,
          instance: form.instance,
          purpose: form.purpose,
          holder: form.holder.trim() || null,
          status: form.status,
          session_ref: form.session_ref.trim() || null,
          notes: form.notes.trim() || null,
        },
      });
      emitToast(`已更新「${form.label.trim()}」`);
      setOpen(false);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-amber-500/60 hover:text-amber-300"
      >
        <Pencil className="h-3 w-3" />
        编辑
      </button>
    );
  }
  return (
    <div className="w-80 rounded-xl border border-amber-500/25 bg-slate-900 p-3 text-left shadow-xl">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-semibold text-amber-300">编辑「{account.label}」</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <ChannelFields value={form} onChange={setForm} />
      <button onClick={submit} disabled={busy || !form.label.trim()} className={`${btnPrimary} mt-2.5 w-full px-2 py-1.5 text-xs`}>
        {busy ? "保存中…" : "保存变更"}
      </button>
    </div>
  );
}
