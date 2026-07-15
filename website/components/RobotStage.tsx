"use client";

import { useEffect, useState } from "react";
import { useMotionValue } from "framer-motion";
import { EveBot, type BotMode, type Skin } from "./AISprite";

/**
 * IP 素材舞台：把 EveBot 单独摆上纯净舞台，供截图/录屏导出品牌素材
 * （TG 广告、开屏、频道贴图等），与站内形象同一份实现，永不走样。
 *
 * URL 参数：
 * - mode:  idle_base | idle_wave | idle_dance | idle_scan | idle_news | idle_spin | flying | falling
 * - scale: 渲染倍数（默认 2，导出 4x 高清用 4）
 * - bg:    transparent（默认，配合截图 omitBackground 出透明 PNG）| ink（品牌深色，录 webm 用）
 * - pool:  1 显示悬浮光池（默认）| 0 隐藏（透明素材叠加到任意底图时更干净）
 * - skin:  normal（默认）| demon（恶魔彩蛋形态素材）
 */
const STAGE_MODES: BotMode[] = ["idle_base", "idle_wave", "idle_dance", "idle_scan", "idle_news", "idle_spin", "flying", "falling"];

export default function RobotStage() {
  const [params, setParams] = useState<{ mode: BotMode; scale: number; bg: string; pool: boolean; skin: Skin } | null>(null);
  const zero = useMotionValue(0);
  const one = useMotionValue(1);
  const poolOpacity = useMotionValue(0.5);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const m = q.get("mode") as BotMode | null;
    const mode: BotMode = m && STAGE_MODES.includes(m) ? m : "idle_base";
    const scale = Math.min(6, Math.max(0.5, Number(q.get("scale") ?? 2) || 2));
    const bg = q.get("bg") === "ink" ? "ink" : "transparent";
    const pool = q.get("pool") !== "0";
    const skin: Skin = q.get("skin") === "demon" ? "demon" : "normal";
    setParams({ mode, scale, bg, pool, skin });
    /* 透明导出：全链路清掉底色（globals 给 body 铺了品牌深色） */
    if (bg === "transparent") {
      document.documentElement.style.background = "transparent";
      document.body.style.background = "transparent";
    }
  }, []);

  useEffect(() => {
    if (params) poolOpacity.set(params.pool ? 0.5 : 0);
  }, [params, poolOpacity]);

  if (!params) return null;

  return (
    <main
      data-stage-ready="1"
      className="flex min-h-screen items-center justify-center overflow-hidden"
      style={{ background: params.bg === "ink" ? "#05060f" : "transparent" }}
    >
      <div style={{ transform: `scale(${params.scale})` }}>
        <EveBot
          mode={params.mode}
          isHovered={false}
          newsText="AI 拟人翻译已就绪…"
          newsCta="BOUNDLESS · AI"
          scrollTilt={zero}
          flightRotate={zero}
          gazeX={zero}
          gazeY={zero}
          squashY={one}
          shadowOpacity={poolOpacity}
          onNewsCta={() => {}}
          reduced={false}
          skin={params.skin}
        />
      </div>
    </main>
  );
}
