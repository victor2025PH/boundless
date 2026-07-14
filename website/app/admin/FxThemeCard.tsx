"use client";

import { useEffect, useState } from "react";

/** 活动皮肤切换卡(system tab):即时切换全站背景氛围色,免重新部署。
 *  写入 /api/fx-theme(admin session cookie 鉴权),前台 ThemeLoader 拉取生效。 */

const THEMES = [
  { id: "", label: "默认 · 青紫", from: "#22d3ee", to: "#8b5cf6" },
  { id: "gold", label: "鎏金 · 节庆", from: "#fb7185", to: "#f59e0b" },
  { id: "emerald", label: "翠绿 · 春季", from: "#a3e635", to: "#34d399" },
  { id: "crimson", label: "绯红 · 年末", from: "#d946ef", to: "#ef4444" },
];

export default function FxThemeCard() {
  const [cur, setCur] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/fx-theme", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setCur(String(d?.theme ?? "")))
      .catch(() => setCur(""));
  }, []);

  async function apply(theme: string) {
    if (saving || theme === cur) return;
    setSaving(true);
    setMsg("");
    try {
      const res = await fetch("/api/fx-theme", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme }),
      });
      const d = await res.json().catch(() => null);
      if (!res.ok || !d?.ok) throw new Error(String(d?.error ?? res.status));
      setCur(theme);
      setMsg("已生效 · 访客最多 2 分钟后看到新皮肤(浏览器缓存)");
    } catch {
      setMsg("保存失败:请确认已登录且服务正常");
    }
    setSaving(false);
  }

  return (
    <div className="mt-4 rounded-xl border border-slate-700/60 bg-slate-900/40 p-4">
      <div className="mb-1 text-sm font-semibold text-slate-200">活动皮肤 · 全站氛围色</div>
      <p className="mb-3 text-[11px] text-slate-500">
        切换首页背景极光/流星/光点的整体色相,适合节庆与大促;不影响正文与品牌色。
      </p>
      <div className="flex flex-wrap gap-2">
        {THEMES.map((th) => (
          <button
            key={th.id}
            onClick={() => apply(th.id)}
            disabled={saving || cur === null}
            className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 text-xs transition disabled:opacity-50 ${
              cur === th.id
                ? "border-sky-500 bg-sky-950/40 text-white"
                : "border-slate-700 text-slate-300 hover:border-slate-500"
            }`}
          >
            <span
              className="h-3.5 w-6 rounded-full"
              style={{ background: `linear-gradient(90deg, ${th.from}, ${th.to})` }}
            />
            {th.label}
            {cur === th.id && <span className="text-sky-300">✓</span>}
          </button>
        ))}
      </div>
      {msg && <p className="mt-2 text-[11px] text-slate-400">{msg}</p>}
    </div>
  );
}
