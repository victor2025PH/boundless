"use client";

// /console/telegram 客户端交互：登记 bot、设 webhook / 查状态、登记群与频道、改用途与功能开关、
// 生成来源邀请链接、客服工作时间。写操作 POST /api/console/telegram（admin+），成功后 router.refresh()。

import { useState } from "react";
import { useRouter } from "next/navigation";

type PublicBot = { id: string; name: string; username?: string; tokenMasked: string; enabled: boolean; builtin: boolean };
type Features = { ai: boolean; welcome: boolean; verify: boolean; antispam: boolean; privacyGuard: boolean; joinApprove: boolean };
type Chat = { chatId: string; botId: string; title: string; type: string; isForum?: boolean; role: string; enabled: boolean; langs?: string[]; features: Features };

const FEATURE_LABEL: Record<keyof Features, string> = {
  ai: "AI 答疑（@/回复）",
  welcome: "新人欢迎",
  verify: "入群验证",
  antispam: "反广告",
  privacyGuard: "隐私保护（删机器码/手机号）",
  joinApprove: "自动批准申请",
};

function emitToast(msg: string, ok = true) {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent("console-toast", { detail: { msg, ok } }));
}

async function act<T = Record<string, unknown>>(json: Record<string, unknown>): Promise<T> {
  const res = await fetch("/api/console/telegram", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(json) });
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON */
  }
  if (!res.ok || data.ok === false) throw new Error(String(data?.error ?? `请求失败 (${res.status})`));
  return data as T;
}

const inputCls =
  "rounded-lg border border-slate-700 bg-ink-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-crown-500";
const btnPrimary = "rounded-lg bg-crown-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-crown-400 disabled:opacity-50";
const btnGhost = "rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500 disabled:opacity-50";

function useAction() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const run = async (json: Record<string, unknown>, okMsg: string, after?: (d: Record<string, unknown>) => void) => {
    setBusy(true);
    try {
      const d = await act(json);
      emitToast(okMsg);
      after?.(d);
      router.refresh();
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setBusy(false);
    }
  };
  return { busy, run };
}

export function AddBotForm() {
  const { busy, run } = useAction();
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  return (
    <form
      className="mt-4 flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        void run({ action: "add_bot", name, token }, "已登记，下一步点「设置 webhook」", () => {
          setName("");
          setToken("");
        });
      }}
    >
      <input className={`${inputCls} w-44`} placeholder="名称，如 客服 1 号" value={name} onChange={(e) => setName(e.target.value)} />
      <input className={`${inputCls} w-96 font-mono`} placeholder="BotFather 给的 token（123456:AA…）" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" type="password" />
      <button className={btnPrimary} disabled={busy || !name || !token}>
        {busy ? "校验中…" : "登记 bot"}
      </button>
    </form>
  );
}

export function BotActions({ bot, canWrite }: { bot: PublicBot; canWrite: boolean }) {
  const { busy, run } = useAction();
  const [info, setInfo] = useState<string | null>(null);
  const [newToken, setNewToken] = useState("");
  const [editing, setEditing] = useState(false);
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap gap-1.5">
        <button
          className={btnGhost}
          disabled={busy}
          onClick={() =>
            void run({ action: "webhook_info", botId: bot.id }, "已刷新", (d) => {
              const i = d.info as { url?: string; pending: number; allowedUpdates?: string[]; lastError?: string };
              setInfo(`${i.url ? "已连接" : "未设置"} · 积压 ${i.pending} · 类型 ${(i.allowedUpdates ?? ["默认"]).join(",")}${i.lastError ? ` · 最近错误：${i.lastError}` : ""}`);
            })
          }
        >
          查状态
        </button>
        {canWrite && (
          <button className={btnGhost} disabled={busy} onClick={() => void run({ action: "apply_webhook", botId: bot.id }, "webhook 与命令菜单已设置")}>
            设置 webhook
          </button>
        )}
        {canWrite && !bot.builtin && (
          <>
            <button className={btnGhost} disabled={busy} onClick={() => void run({ action: "update_bot", id: bot.id, enabled: !bot.enabled }, bot.enabled ? "已停用" : "已启用")}>
              {bot.enabled ? "停用" : "启用"}
            </button>
            <button className={btnGhost} onClick={() => setEditing((v) => !v)}>
              换 token
            </button>
          </>
        )}
      </div>
      {editing && (
        <div className="flex gap-1.5">
          <input className={`${inputCls} w-72 py-1 font-mono text-xs`} type="password" autoComplete="off" placeholder="新 token（同一个 bot 重新生成的）" value={newToken} onChange={(e) => setNewToken(e.target.value)} />
          <button
            className={btnGhost}
            disabled={busy || !newToken}
            onClick={() =>
              void run({ action: "update_bot", id: bot.id, token: newToken }, "token 已更新，请重新设置 webhook", () => {
                setNewToken("");
                setEditing(false);
              })
            }
          >
            保存
          </button>
        </div>
      )}
      {info && <div className="text-[11px] text-slate-400">{info}</div>}
    </div>
  );
}

export function AddChatForm({ bots }: { bots: { id: string; label: string }[] }) {
  const { busy, run } = useAction();
  const [botId, setBotId] = useState(bots[0]?.id ?? "");
  const [chat, setChat] = useState("");
  const [role, setRole] = useState("support");
  return (
    <form
      className="mt-4 flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        void run({ action: "verify_chat", botId, chat, role, enable: true }, "已登记并启用", (d) => {
          const miss = (d.missing as string[]) ?? [];
          if (miss.length) emitToast(`还缺权限：${miss.join("、")}`, false);
          setChat("");
        });
      }}
    >
      <select className={inputCls} value={botId} onChange={(e) => setBotId(e.target.value)}>
        {bots.map((b) => (
          <option key={b.id} value={b.id}>
            {b.label}
          </option>
        ))}
      </select>
      <input className={`${inputCls} w-64 font-mono`} placeholder="群/频道 id（-100…）或 @用户名" value={chat} onChange={(e) => setChat(e.target.value)} />
      <select className={inputCls} value={role} onChange={(e) => setRole(e.target.value)}>
        <option value="support">客服群</option>
        <option value="community">社群</option>
        <option value="channel">频道</option>
      </select>
      <button className={btnPrimary} disabled={busy || !chat}>
        {busy ? "验证中…" : "验证并添加"}
      </button>
    </form>
  );
}

export function ChatControls({ chat, canWrite, roleLabel }: { chat: Chat; canWrite: boolean; roleLabel: Record<string, string> }) {
  const { busy, run } = useAction();
  const upd = (patch: Record<string, unknown>, msg: string) => void run({ action: "update_chat", botId: chat.botId, chatId: chat.chatId, ...patch }, msg);
  const feats = (chat.type === "channel" ? ["joinApprove"] : chat.role === "support" ? [] : ["ai", "welcome", "verify", "antispam", "privacyGuard", "joinApprove"]) as (keyof Features)[];
  return (
    <div className="space-y-1.5 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <select className={`${inputCls} py-1 text-xs`} disabled={!canWrite || busy} value={chat.role} onChange={(e) => upd({ role: e.target.value }, "用途已更新")}>
          {Object.entries(roleLabel).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1">
          <input type="checkbox" disabled={!canWrite || busy} checked={chat.enabled} onChange={(e) => upd({ enabled: e.target.checked }, e.target.checked ? "已启用" : "已停用")} />
          <span className={chat.enabled ? "text-emerald-400" : "text-slate-500"}>{chat.enabled ? "启用" : "停用"}</span>
        </label>
        {canWrite && (
          <button className={btnGhost} disabled={busy} onClick={() => void run({ action: "verify_chat", botId: chat.botId, chat: chat.chatId }, "已重新验证", (d) => {
            const miss = (d.missing as string[]) ?? [];
            if (miss.length) emitToast(`还缺权限：${miss.join("、")}`, false);
          })}>
            重新验证
          </button>
        )}
      </div>
      {chat.role === "support" && <LangsInput chat={chat} canWrite={canWrite} busy={busy} onSave={(langs) => upd({ langs }, "接待语言已保存")} />}
      {feats.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {feats.map((k) => (
            <label key={k} className="flex items-center gap-1 text-slate-300">
              <input type="checkbox" disabled={!canWrite || busy} checked={chat.features[k]} onChange={(e) => upd({ features: { [k]: e.target.checked } }, "已保存")} />
              {FEATURE_LABEL[k]}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

function LangsInput({ chat, canWrite, busy, onSave }: { chat: Chat; canWrite: boolean; busy: boolean; onSave: (langs: string) => void }) {
  const [v, setV] = useState((chat.langs ?? []).join(","));
  return (
    <div className="flex items-center gap-1.5 text-slate-300">
      <span>接待语言</span>
      <input className={`${inputCls} w-28 py-1 font-mono text-xs`} disabled={!canWrite} placeholder="全部" value={v} onChange={(e) => setV(e.target.value)} />
      {canWrite && (
        <button className={btnGhost} disabled={busy || v === (chat.langs ?? []).join(",")} onClick={() => onSave(v)}>
          保存
        </button>
      )}
      <span className="text-slate-500">如 zh 或 en，留空 = 全部；多个客服群时按语言分流，再按未结工单少的优先</span>
    </div>
  );
}

export function InviteForm({ chats }: { chats: { key: string; label: string }[] }) {
  const { busy, run } = useAction();
  const [key, setKey] = useState(chats[0]?.key ?? "");
  const [src, setSrc] = useState("");
  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const [botId, chatId] = key.split("|");
        void run({ action: "create_invite", botId, chatId, src }, "邀请链接已生成", () => setSrc(""));
      }}
    >
      <select className={inputCls} value={key} onChange={(e) => setKey(e.target.value)}>
        {chats.map((c) => (
          <option key={c.key} value={c.key}>
            {c.label}
          </option>
        ))}
      </select>
      <input className={`${inputCls} w-44 font-mono`} placeholder="来源码 ad_biz_voice01" value={src} onChange={(e) => setSrc(e.target.value)} />
      <button className={btnPrimary} disabled={busy || !src}>
        生成
      </button>
    </form>
  );
}

export function SupportForm({ support, canWrite }: { support: { hours: string; tzOffset: number; slaMin: number; escalateMin: number; duty: string; followupMin: number }; canWrite: boolean }) {
  const { busy, run } = useAction();
  const [hours, setHours] = useState(support.hours);
  const [tz, setTz] = useState(String(support.tzOffset));
  const [sla, setSla] = useState(String(support.slaMin));
  const [esc, setEsc] = useState(String(support.escalateMin));
  const [fu, setFu] = useState(String(support.followupMin));
  const [duty, setDuty] = useState(support.duty);
  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        void run({ action: "set_support", hours, tzOffset: Number(tz), slaMin: Number(sla), escalateMin: Number(esc), followupMin: Number(fu), duty }, "已保存");
      }}
    >
      <input className={`${inputCls} w-36 font-mono`} disabled={!canWrite} value={hours} onChange={(e) => setHours(e.target.value)} />
      <span className="text-xs text-slate-400">UTC</span>
      <input className={`${inputCls} w-16 font-mono`} disabled={!canWrite} value={tz} onChange={(e) => setTz(e.target.value)} />
      <span className="text-xs text-slate-400">超时提醒</span>
      <input className={`${inputCls} w-16 font-mono`} disabled={!canWrite} value={sla} onChange={(e) => setSla(e.target.value)} />
      <span className="text-xs text-slate-400">分钟 · 升级管理员</span>
      <input className={`${inputCls} w-16 font-mono`} disabled={!canWrite} value={esc} onChange={(e) => setEsc(e.target.value)} />
      <span className="text-xs text-slate-400">分钟（0 不升级）· 已知问题追问</span>
      <input className={`${inputCls} w-16 font-mono`} disabled={!canWrite} value={fu} onChange={(e) => setFu(e.target.value)} />
      <span className="text-xs text-slate-400">分钟（0 不追问）</span>
      <textarea
        className={`${inputCls} h-20 w-full font-mono text-xs`}
        disabled={!canWrite}
        placeholder={"值班表（可空），每行一条：\n1-5 09:00-18:00 @alice @bob\n6,7 @carol"}
        value={duty}
        onChange={(e) => setDuty(e.target.value)}
      />
      {canWrite && (
        <button className={btnPrimary} disabled={busy}>
          保存
        </button>
      )}
    </form>
  );
}

type KnownIssueRow = { id: string; builtin: boolean; pattern: string; keywords: string[]; zh: string; en: string; enabled: boolean; overridden: boolean };

export function KnownIssueEditor({ row, canWrite }: { row?: KnownIssueRow; canWrite: boolean }) {
  const { busy, run } = useAction();
  const [open, setOpen] = useState(false);
  const [id, setId] = useState(row?.id ?? "");
  const [keywords, setKeywords] = useState((row?.keywords ?? []).join(", "));
  const [zh, setZh] = useState(row?.zh ?? "");
  const [en, setEn] = useState(row?.en ?? "");
  const [enabled, setEnabled] = useState(row?.enabled ?? true);
  if (!canWrite) return null;
  if (!open)
    return (
      <div className="flex flex-wrap gap-1.5">
        <button className={row ? btnGhost : btnPrimary} onClick={() => setOpen(true)}>
          {row ? "编辑" : "新增已知问题"}
        </button>
        {row && (row.overridden || !row.builtin) && (
          <button
            className={btnGhost}
            disabled={busy}
            onClick={() => {
              if (window.confirm(row.builtin ? `把「${row.id}」恢复成内置默认？` : `删除「${row.id}」？`)) void run({ action: "known_issue_delete", id: row.id }, row.builtin ? "已恢复默认" : "已删除");
            }}
          >
            {row.builtin ? "恢复默认" : "删除"}
          </button>
        )}
      </div>
    );
  return (
    <form
      className="mt-2 grid gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        void run({ action: "known_issue_save", id, keywords, zh, en, enabled }, "已保存，bot 立即生效", () => setOpen(false));
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <input className={`${inputCls} w-40 font-mono`} placeholder="id，如 dll-missing" value={id} disabled={!!row} onChange={(e) => setId(e.target.value)} />
        <label className="flex items-center gap-1 text-xs text-slate-300">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> 启用
        </label>
      </div>
      <input className={inputCls} placeholder={row?.builtin ? "追加关键词（可空；内置规则保留），逗号分隔" : "关键词，逗号分隔，如 msvcp140, 找不到 dll"} value={keywords} onChange={(e) => setKeywords(e.target.value)} />
      <textarea className={`${inputCls} h-16`} placeholder="中文解决步骤" value={zh} onChange={(e) => setZh(e.target.value)} />
      <textarea className={`${inputCls} h-16`} placeholder="English steps（可空，空则用中文）" value={en} onChange={(e) => setEn(e.target.value)} />
      <div className="flex gap-2">
        <button className={btnPrimary} disabled={busy || !id}>
          {busy ? "保存中…" : "保存"}
        </button>
        <button type="button" className={btnGhost} onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </form>
  );
}

export function PostForm({ chats }: { chats: { key: string; label: string }[] }) {
  const { busy, run } = useAction();
  const [key, setKey] = useState(chats[0]?.key ?? "");
  const [when, setWhen] = useState("");
  const [text, setText] = useState("");
  const [bt, setBt] = useState("");
  const [bu, setBu] = useState("");
  const [photo, setPhoto] = useState("");
  const [repeat, setRepeat] = useState("");
  const [uploading, setUploading] = useState(false);
  const uploaded = photo.startsWith("media:");
  const upload = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) return emitToast("图片最大 5MB", false);
    setUploading(true);
    try {
      const res = await fetch("/api/console/telegram/upload", { method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
      const data = (await res.json().catch(() => ({}))) as { ok?: boolean; photo?: string; error?: string };
      if (!res.ok || !data.photo) throw new Error(data.error ?? `上传失败 (${res.status})`);
      setPhoto(data.photo);
      emitToast(`已上传 ${file.name}`);
    } catch (e) {
      emitToast(e instanceof Error ? e.message : String(e), false);
    } finally {
      setUploading(false);
    }
  };
  return (
    <form
      className="grid gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const [botId, chatId] = key.split("|");
        const sendAt = when ? new Date(when).toISOString() : "";
        void run({ action: "post_schedule", botId, chatId, text, sendAt, buttonText: bt, buttonUrl: bu, photo, repeat }, when ? "已排期" : "已加入发送队列（10 分钟内发出）", () => {
          setText("");
          setBt("");
          setBu("");
          setPhoto("");
          setRepeat("");
        });
      }}
    >
      <div className="flex flex-wrap items-center gap-2">
        <select className={inputCls} value={key} onChange={(e) => setKey(e.target.value)}>
          {chats.map((c) => (
            <option key={c.key} value={c.key}>
              {c.label}
            </option>
          ))}
        </select>
        <input type="datetime-local" className={inputCls} value={when} onChange={(e) => setWhen(e.target.value)} />
        <span className="text-xs text-slate-500">不填 = 尽快发（按你电脑的时区）</span>
        <select className={inputCls} value={repeat} onChange={(e) => setRepeat(e.target.value)}>
          <option value="">只发一次</option>
          <option value="daily">每天同一时间</option>
          <option value="weekly">每周同一时间</option>
        </select>
      </div>
      <textarea className={`${inputCls} h-24`} placeholder={photo ? "图片说明（纯文本，最多 1024 字）" : "帖子内容（纯文本，最多 4000 字）"} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="flex flex-wrap items-center gap-2">
        {uploaded ? (
          <span className="text-xs text-emerald-400">已上传配图</span>
        ) : (
          <input className={`${inputCls} w-96 font-mono`} placeholder="配图地址（可空）https://…jpg / png" value={photo} onChange={(e) => setPhoto(e.target.value)} />
        )}
        <label className={`${btnGhost} cursor-pointer`}>
          {uploading ? "上传中…" : uploaded ? "换一张" : "或上传图片"}
          <input type="file" accept="image/jpeg,image/png,image/webp" className="hidden" disabled={uploading} onChange={(e) => void upload(e.target.files?.[0])} />
        </label>
        {photo && (
          <button type="button" className={btnGhost} onClick={() => setPhoto("")}>
            去掉配图
          </button>
        )}
        <span className="text-xs text-slate-500">JPG / PNG / WebP，≤ 5MB</span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input className={`${inputCls} w-40`} placeholder="按钮文字（可空）" value={bt} onChange={(e) => setBt(e.target.value)} />
        <input className={`${inputCls} w-96 font-mono`} placeholder="按钮链接 https://t.me/ctx2026_bot?start=ad_…" value={bu} onChange={(e) => setBu(e.target.value)} />
        <button className={btnPrimary} disabled={busy || uploading || !key || !text.trim()}>
          {busy ? "提交中…" : "排期发帖"}
        </button>
      </div>
    </form>
  );
}

export function CancelPostButton({ id }: { id: number }) {
  const { busy, run } = useAction();
  return (
    <button className={btnGhost} disabled={busy} onClick={() => window.confirm(`取消帖子 #${id}？`) && void run({ action: "post_cancel", id }, "已取消")}>
      取消
    </button>
  );
}
