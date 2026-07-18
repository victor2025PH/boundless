"use client";

// /console 客户端交互组件集：登录卡（账号 + 初始化引导）、导航高亮、toast、
// 新建客户、归属客户、挂身份。服务端页面只读账本直出，所有写操作经这里
// fetch /api/console/**，成功后 router.refresh()。

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Activity,
  BarChart3,
  Crown,
  Inbox,
  KeyRound,
  LayoutDashboard,
  Link2,
  Lock,
  LogOut,
  Plus,
  ReceiptText,
  ScrollText,
  ShieldAlert,
  Sparkles,
  UserCog,
  UserRound,
  Users,
  VenetianMask,
  X,
} from "lucide-react";

// ── toast（模块级事件总线，ConsoleToaster 挂在 layout）─────────────
type ToastMsg = { msg: string; ok: boolean };

function emitToast(msg: string, ok = true) {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent<ToastMsg>("console-toast", { detail: { msg, ok } }));
  }
}

export function ConsoleToaster() {
  const [toast, setToast] = useState<ToastMsg | null>(null);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onToast = (e: Event) => {
      setToast((e as CustomEvent<ToastMsg>).detail);
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => setToast(null), 3200);
    };
    window.addEventListener("console-toast", onToast);
    return () => {
      window.removeEventListener("console-toast", onToast);
      if (timer) clearTimeout(timer);
    };
  }, []);
  if (!toast) return null;
  return (
    <div
      className={`fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-2xl ${
        toast.ok ? "bg-amber-400 text-slate-950" : "bg-rose-500 text-white"
      }`}
    >
      {toast.msg}
    </div>
  );
}

// ── fetch 封装：非 2xx 抛服务端 error 文案，调用方统一 toast ───────
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

const inputCls =
  "w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-amber-500";
const btnPrimary =
  "rounded-lg bg-amber-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-amber-400 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-amber-500/60 hover:text-amber-300 disabled:opacity-50";

// ── 登录卡（未登录时由 layout 渲染）────────────────────────────────
// usersEmpty=true → 「初始化主账号」引导表单（CONSOLE_KEY + 用户名 + 密码，建首个 master）；
// 否则 → 用户名 + 密码登录。旧共享口令登录已下线。
export function LoginCard({ configured, usersEmpty }: { configured: boolean; usersEmpty: boolean }) {
  const [key, setKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const canSubmit = usersEmpty ? !!(key && username && password) : !!(username && password);

  async function doLogin() {
    if (!canSubmit || busy) return;
    setBusy(true);
    setErr("");
    try {
      await api("/api/console/login", {
        method: "POST",
        json: usersEmpty ? { bootstrap: true, key, username, password } : { username, password },
      });
      location.reload();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(
        msg.includes("invalid key")
          ? "CONSOLE_KEY 不正确"
          : msg.includes("invalid credentials")
            ? "用户名或密码错误"
            : msg.includes("account disabled")
              ? "该账号已被禁用，请联系主账号"
              : msg.includes("too many")
                ? "尝试过多，请 10 分钟后再试"
                : msg.includes("password must be")
                  ? "密码至少 8 位"
                  : msg.includes("username must be")
                    ? "用户名格式：2–32 位小写字母/数字/_.-"
                    : msg
      );
      setBusy(false);
    }
  }

  const onEnter = (e: React.KeyboardEvent) => e.key === "Enter" && doLogin();

  return (
    <div className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-6">
      <div className="rounded-2xl border border-amber-500/25 bg-slate-900/70 p-7 shadow-[0_0_60px_rgba(245,158,11,0.06)]">
        <div className="mb-1 flex items-center gap-2">
          <Crown className="h-6 w-6 text-amber-400" />
          <span className="text-lg font-bold text-white">无界 · 集团控制台</span>
        </div>
        <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-amber-500/80">
          Boundless Console
        </p>
        <div className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-[11px] font-medium text-amber-300">
          <ShieldAlert className="h-3.5 w-3.5" />
          皇冠资产 · 最小暴露
        </div>
        {usersEmpty ? (
          <>
            <p className="mb-4 mt-4 text-xs leading-relaxed text-slate-500">
              <span className="font-semibold text-amber-300">初始化主账号</span>
              ：控制台尚无任何账号。请用服务端 CONSOLE_KEY 验证身份，创建首个 master 账号并直接登录。
            </p>
            {configured ? (
              <div className="space-y-2.5">
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <input
                    type="password"
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                    placeholder="CONSOLE_KEY（服务端口令）"
                    className={`${inputCls} py-2.5 pl-9`}
                    onKeyDown={onEnter}
                    autoFocus
                  />
                </div>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="主账号用户名（小写字母/数字/_.-）"
                  className={`${inputCls} py-2.5`}
                  onKeyDown={onEnter}
                  autoComplete="username"
                />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="密码（至少 8 位）"
                  className={`${inputCls} py-2.5`}
                  onKeyDown={onEnter}
                  autoComplete="new-password"
                />
                <button onClick={doLogin} disabled={busy || !canSubmit} className={`${btnPrimary} w-full py-2.5`}>
                  {busy ? "创建中…" : "创建主账号并进入"}
                </button>
              </div>
            ) : (
              <p className="rounded-lg border border-rose-500/30 bg-rose-950/30 p-3 text-xs leading-relaxed text-rose-300">
                服务端未配置口令：请在 .env.local 设置 CONSOLE_KEY（生产必须独立设置，勿与
                ADMIN_KEY 共用），重启后再来初始化主账号。
              </p>
            )}
          </>
        ) : (
          <>
            <p className="mb-4 mt-4 text-xs leading-relaxed text-slate-500">
              管理集团客户、订单与授权台账。请用控制台账号登录。
            </p>
            <div className="space-y-2.5">
              <div className="relative">
                <UserRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="用户名"
                  className={`${inputCls} py-2.5 pl-9`}
                  onKeyDown={onEnter}
                  autoComplete="username"
                  autoFocus
                />
              </div>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="密码"
                  className={`${inputCls} py-2.5 pl-9`}
                  onKeyDown={onEnter}
                  autoComplete="current-password"
                />
              </div>
              <button onClick={doLogin} disabled={busy || !canSubmit} className={`${btnPrimary} w-full py-2.5`}>
                {busy ? "登录中…" : "进入控制台"}
              </button>
            </div>
          </>
        )}
        {err && <p className="mt-3 text-center text-sm text-rose-400">{err}</p>}
      </div>
      <p className="mt-4 text-center text-[11px] text-slate-600">
        实名账号 + RBAC（viewer / admin / master）；生产请配合 IP 白名单使用。
      </p>
    </div>
  );
}

export function LogoutButton() {
  const [busy, setBusy] = useState(false);
  return (
    <button
      onClick={async () => {
        setBusy(true);
        try {
          await api("/api/console/logout", { method: "POST" });
        } catch {
          /* 清 cookie 失败也照样刷新回登录页 */
        }
        location.reload();
      }}
      disabled={busy}
      className="flex items-center gap-1 rounded-lg border border-slate-700 px-2.5 py-1.5 text-xs text-slate-400 hover:border-rose-500 hover:text-rose-300 disabled:opacity-50"
    >
      <LogOut className="h-3.5 w-3.5" />
      登出
    </button>
  );
}

// ── 顶部导航（当前路由高亮；用户管理入口仅 master 可见）────────────
const NAV = [
  { href: "/console", label: "总览", Icon: LayoutDashboard },
  { href: "/console/customers", label: "客户", Icon: Users },
  { href: "/console/personas", label: "人设", Icon: VenetianMask },
  { href: "/console/opportunities", label: "商机", Icon: Sparkles },
  { href: "/console/orders", label: "订单", Icon: ReceiptText },
  { href: "/console/licenses", label: "授权", Icon: KeyRound },
  { href: "/console/leads", label: "留资", Icon: Inbox },
  { href: "/console/kpi", label: "KPI", Icon: BarChart3 },
  { href: "/console/audit", label: "审计", Icon: ScrollText },
  { href: "/console/health", label: "健康", Icon: Activity },
] as const;

const NAV_USERS = { href: "/console/users", label: "用户", Icon: UserCog } as const;

export function ConsoleNav({ showUsers = false }: { showUsers?: boolean }) {
  const pathname = usePathname();
  const items = showUsers ? [...NAV, NAV_USERS] : [...NAV];
  return (
    <nav className="flex gap-1 overflow-x-auto">
      {items.map(({ href, label, Icon }) => {
        const active = href === "/console" ? pathname === "/console" : pathname?.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className={`flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium transition ${
              active
                ? "border-amber-400 text-amber-300"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

// ── 新建客户（客户页 + 归属控件内复用的提交逻辑）───────────────────
interface CreatedCustomer {
  id: string;
  display_name: string | null;
}

async function createCustomerReq(input: {
  display_name: string;
  primary_contact?: string;
  notes?: string;
  identity?: { kind: string; value: string };
}): Promise<CreatedCustomer> {
  const data = await api<{ customer: CreatedCustomer }>("/api/console/customers", {
    method: "POST",
    json: input,
  });
  return data.customer;
}

const IDENTITY_KIND_OPTIONS = [
  { value: "contact", label: "contact 通用联系" },
  { value: "tg", label: "tg Telegram" },
  { value: "email", label: "email 邮箱" },
  { value: "phone", label: "phone 电话" },
  { value: "fingerprint", label: "fingerprint 设备指纹" },
] as const;

export function NewCustomerForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [contact, setContact] = useState("");
  const [notes, setNotes] = useState("");
  const [identKind, setIdentKind] = useState("contact");
  const [identValue, setIdentValue] = useState("");

  async function submit() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const customer = await createCustomerReq({
        display_name: name.trim(),
        primary_contact: contact.trim() || undefined,
        notes: notes.trim() || undefined,
        identity: identValue.trim() ? { kind: identKind, value: identValue.trim() } : undefined,
      });
      emitToast(`已创建客户 ${customer.display_name ?? customer.id}`);
      setOpen(false);
      setName("");
      setContact("");
      setNotes("");
      setIdentValue("");
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
        新建客户
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-amber-500/25 bg-slate-900/70 p-4 sm:max-w-md">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-amber-300">新建客户</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-2.5">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="显示名 *（如：张总 / ACME Ltd.）" className={inputCls} />
        <input value={contact} onChange={(e) => setContact(e.target.value)} placeholder="主联系方式（微信 / TG / 邮箱，可空）" className={inputCls} />
        <input value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="备注（可空）" className={inputCls} />
        <div className="flex gap-2">
          <select value={identKind} onChange={(e) => setIdentKind(e.target.value)} className={`${inputCls} w-40`}>
            {IDENTITY_KIND_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <input
            value={identValue}
            onChange={(e) => setIdentValue(e.target.value)}
            placeholder="初始身份标识值（可空，用于自动归属）"
            className={inputCls}
          />
        </div>
        <button onClick={submit} disabled={busy || !name.trim()} className={`${btnPrimary} w-full`}>
          {busy ? "创建中…" : "创建客户"}
        </button>
      </div>
    </div>
  );
}

// ── 归属客户（订单/授权/留资行内控件）──────────────────────────────
export interface CustomerOption {
  id: string;
  label: string;
}

export function AssignCustomerControl({
  entity,
  entityKey,
  customers,
}: {
  entity: "order" | "license" | "lead";
  entityKey: string;
  customers: CustomerOption[];
}) {
  const router = useRouter();
  const [mode, setMode] = useState<"closed" | "pick" | "create">("closed");
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState("");
  const [newName, setNewName] = useState("");
  const [newContact, setNewContact] = useState("");

  const endpoint =
    entity === "order" ? "/api/console/orders" : entity === "license" ? "/api/console/licenses" : "/api/console/leads";

  async function assignTo(customerId: string) {
    const body = entity === "lead" ? { source_key: entityKey, customer_id: customerId } : { id: entityKey, customer_id: customerId };
    await api(endpoint, { method: "PATCH", json: body });
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
      const customer = await createCustomerReq({
        display_name: newName.trim(),
        primary_contact: newContact.trim() || undefined,
      });
      await assignTo(customer.id);
      emitToast(`已创建并归属 ${customer.display_name ?? customer.id}`);
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

// ── 用户管理（/console/users；API 侧全部动作仅 master）─────────────
export interface ConsoleUserItem {
  id: string;
  username: string;
  role: "master" | "admin" | "viewer";
  display_name: string | null;
  enabled: boolean;
  created_at: string | null;
  last_login: string | null;
}

const ROLE_OPTIONS = [
  { value: "viewer", label: "viewer 只读" },
  { value: "admin", label: "admin 运营" },
  { value: "master", label: "master 主账号" },
] as const;

export function NewUserForm() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("viewer");
  const [displayName, setDisplayName] = useState("");

  async function submit() {
    if (!username.trim() || !password || busy) return;
    setBusy(true);
    try {
      await api("/api/console/users", {
        method: "POST",
        json: {
          username: username.trim(),
          password,
          role,
          display_name: displayName.trim() || undefined,
        },
      });
      emitToast(`已创建用户 ${username.trim()}`);
      setOpen(false);
      setUsername("");
      setPassword("");
      setRole("viewer");
      setDisplayName("");
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
        新建用户
      </button>
    );
  }
  return (
    <div className="w-full rounded-xl border border-amber-500/25 bg-slate-900/70 p-4 sm:max-w-md">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-amber-300">新建用户</span>
        <button onClick={() => setOpen(false)} className="text-slate-500 hover:text-slate-300" aria-label="关闭">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="space-y-2.5">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="用户名 *（2–32 位小写字母/数字/_.-）"
          className={inputCls}
          autoComplete="off"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="初始密码 *（至少 8 位）"
          className={inputCls}
          autoComplete="new-password"
        />
        <div className="flex gap-2">
          <select value={role} onChange={(e) => setRole(e.target.value)} className={`${inputCls} w-44`}>
            {ROLE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="显示名（可空）"
            className={inputCls}
          />
        </div>
        <button onClick={submit} disabled={busy || !username.trim() || !password} className={`${btnPrimary} w-full`}>
          {busy ? "创建中…" : "创建用户"}
        </button>
      </div>
    </div>
  );
}

/** 用户行操作：改角色 / 禁用启用 / 重置密码。isLastEnabledMaster 时 UI 直接禁用
 *  降级与禁用入口（API 侧仍有硬校验兜底）。 */
export function UserActions({ user, isLastEnabledMaster }: { user: ConsoleUserItem; isLastEnabledMaster: boolean }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");

  async function patch(payload: Record<string, unknown>, okMsg: string) {
    if (busy) return;
    setBusy(true);
    try {
      await api("/api/console/users", { method: "PATCH", json: { id: user.id, ...payload } });
      emitToast(okMsg);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <select
        value={user.role}
        disabled={busy || isLastEnabledMaster}
        onChange={(e) => patch({ action: "set_role", role: e.target.value }, `已把 ${user.username} 改为 ${e.target.value}`)}
        className={`${inputCls} w-auto px-2 py-1 text-xs`}
        title={isLastEnabledMaster ? "最后一个启用的 master 不能降级" : "改角色"}
      >
        {ROLE_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.value}</option>
        ))}
      </select>
      <button
        disabled={busy || (user.enabled && isLastEnabledMaster)}
        onClick={() =>
          patch({ action: "set_enabled", enabled: !user.enabled }, user.enabled ? `已禁用 ${user.username}（会话已撤销）` : `已启用 ${user.username}`)
        }
        className={`rounded-lg border px-2 py-1 text-xs disabled:opacity-40 ${
          user.enabled
            ? "border-rose-500/40 text-rose-300 hover:bg-rose-500/10"
            : "border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10"
        }`}
        title={user.enabled && isLastEnabledMaster ? "最后一个启用的 master 不能禁用" : undefined}
      >
        {user.enabled ? "禁用" : "启用"}
      </button>
      {pwOpen ? (
        <span className="flex items-center gap-1.5">
          <input
            type="password"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder="新密码（≥8 位）"
            className={`${inputCls} w-36 px-2 py-1 text-xs`}
            autoComplete="new-password"
            onKeyDown={(e) => {
              if (e.key === "Enter" && pw.length >= 8) {
                patch({ action: "reset_password", password: pw }, `已重置 ${user.username} 的密码（旧会话已撤销）`);
                setPwOpen(false);
                setPw("");
              }
            }}
          />
          <button
            disabled={busy || pw.length < 8}
            onClick={() => {
              patch({ action: "reset_password", password: pw }, `已重置 ${user.username} 的密码（旧会话已撤销）`);
              setPwOpen(false);
              setPw("");
            }}
            className={`${btnGhost} px-2 py-1 text-xs`}
          >
            确认
          </button>
          <button
            onClick={() => {
              setPwOpen(false);
              setPw("");
            }}
            className="text-slate-500 hover:text-slate-300"
            aria-label="取消重置"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </span>
      ) : (
        <button disabled={busy} onClick={() => setPwOpen(true)} className={`${btnGhost} px-2 py-1 text-xs`}>
          重置密码
        </button>
      )}
    </div>
  );
}

// ── 客户 360：挂身份标识 ───────────────────────────────────────────
export function AttachIdentityForm({ customerId }: { customerId: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState("contact");
  const [value, setValue] = useState("");

  async function submit() {
    if (!value.trim() || busy) return;
    setBusy(true);
    try {
      await api(`/api/console/customers/${customerId}`, {
        method: "POST",
        json: { action: "attach_identity", kind, value: value.trim() },
      });
      emitToast("身份标识已挂接");
      setValue("");
      router.refresh();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      emitToast(msg.includes("another customer") ? "该身份已属于另一位客户，未抢占" : msg, false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap gap-2">
      <select value={kind} onChange={(e) => setKind(e.target.value)} className={`${inputCls} w-44`}>
        {IDENTITY_KIND_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="标识值（如 tg 用户 ID / 邮箱）"
        className={`${inputCls} min-w-[180px] flex-1`}
        onKeyDown={(e) => e.key === "Enter" && submit()}
      />
      <button onClick={submit} disabled={busy || !value.trim()} className={btnGhost}>
        {busy ? "挂接中…" : "＋ 挂身份"}
      </button>
    </div>
  );
}
