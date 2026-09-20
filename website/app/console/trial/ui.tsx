"use client";

// 客服核销面板：贴码 → 核销 → 厂商机自动签凭证 → 用户端后台轮询自动入账。
// 客服全程不接触密钥，也不需要告诉用户「去哪粘贴」——这是这条链最省事的地方。
import { useState } from "react";
import { useRouter } from "next/navigation";
import { CheckCircle2, Gift } from "lucide-react";

const inputCls =
  "rounded-lg border border-slate-700 bg-ink-950 px-3 py-2 text-sm text-slate-200 outline-none placeholder:text-slate-600 focus:border-crown-500";
const btnPrimary =
  "rounded-lg bg-crown-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-crown-400 disabled:opacity-50";

type Result = { ok: boolean; msg: string };

export function TrialRedeemPanel({ defaultChars }: { defaultChars: number }) {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [chars, setChars] = useState(String(defaultChars));
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Result | null>(null);

  // 客服是照着聊天记录手打的：小写、丢横杠、前后空格都很常见，全在前端补齐，
  // 别让「BC-」这种格式细节变成一次失败的核销和一轮来回。
  function normalize(raw: string): string {
    const s = raw.trim().toUpperCase().replace(/\s+/g, "").replace(/^BC-?/, "");
    const body = s.replace(/-/g, "");
    if (body.length !== 8) return raw.trim().toUpperCase();
    return `BC-${body.slice(0, 4)}-${body.slice(4)}`;
  }

  async function submit() {
    const norm = normalize(code);
    if (!norm) return;
    setBusy(true);
    setRes(null);
    try {
      const r = await fetch("/api/console/trial-redeem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: norm, chars: Number(chars) || defaultChars }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d?.ok) {
        const why =
          d?.error === "bad_code"
            ? "码的格式不对（应形如 BC-XXXX-XXXX）"
            : d?.error === "not_found"
              ? "查无此码——确认用户报的是绑定码而不是别的编号"
              : String(d?.error || `失败 (${r.status})`);
        setRes({ ok: false, msg: why });
        return;
      }
      setRes({
        ok: true,
        msg: d.already_redeemed
          ? `这个码之前已经核销过了（${d.contact || "—"}），没有重复赠送。`
          : `已核销：${d.contact || "—"} 将获得 ${Number(d.chars || 0).toLocaleString()} 字符，`
            + `厂商机签发后自动到账，不用让用户做任何操作。`,
      });
      setCode("");
      router.refresh();
    } catch (e) {
      setRes({ ok: false, msg: "网络错误，请重试" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-5 rounded-xl border border-crown-500/25 bg-crown-500/[0.04] p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-crown-300">
        <Gift className="h-4 w-4" />
        核销绑定码
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={code}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !busy) submit();
          }}
          placeholder="BC-XXXX-XXXX"
          className={`${inputCls} w-48 font-mono tracking-wider`}
          autoFocus
        />
        <label className="flex items-center gap-1.5 text-xs text-slate-400">
          赠送字符
          <input
            value={chars}
            onChange={(e) => setChars(e.target.value.replace(/[^0-9]/g, ""))}
            className={`${inputCls} w-28`}
          />
        </label>
        <button onClick={submit} disabled={busy || !code.trim()} className={btnPrimary}>
          {busy ? "核销中…" : "核销"}
        </button>
      </div>
      {res && (
        <div
          className={`mt-3 flex items-start gap-1.5 text-xs ${
            res.ok ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {res.ok && <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
          <span>{res.msg}</span>
        </div>
      )}
      <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
        用户会在客户端「联系客服申请更多字符」拿到这串码。核销只是登记「该赠多少」
        （数额可按用户情况调整），真凭证由厂商机用离线私钥签发后回填，客户端自动入账。
        重复核销不会重复赠送。
      </p>
    </div>
  );
}

/** 邀请人审：放行一条被防刷启发标 flagged 的邀请（宿舍/公司同网段是真实场景，
 *  启发式必然有误伤，所以出口是人审不是直接拒绝）。 */
export function ReferralApproveButton({ id }: { id: string }) {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function approve() {
    setBusy(true);
    setErr("");
    try {
      const r = await fetch("/api/console/referral-approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d?.ok) {
        setErr(String(d?.error || `失败 (${r.status})`));
        return;
      }
      router.refresh();
    } catch {
      setErr("网络错误");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-1.5">
      <button
        onClick={approve}
        disabled={busy}
        className="rounded bg-emerald-500/15 px-2 py-0.5 text-[11px] font-medium text-emerald-300 hover:bg-emerald-500/25 disabled:opacity-50"
      >
        {busy ? "放行中…" : "人审放行"}
      </button>
      {err && <span className="text-[10px] text-rose-400">{err}</span>}
    </span>
  );
}
