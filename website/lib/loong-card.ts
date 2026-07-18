/**
 * 星图分享卡 PNG 绘制（1080×1350，4:5 社交竖版）。
 * 纯 canvas 2D，无新依赖；QR 由调用方传入已渲染的 <canvas>（qrcode.react）。
 */

const GOLD = "#f5c542";
const GOLD_HI = "#ffe9a8";

/** 北斗七星坐标（与 LoongCodex/DragonQuest 同源，viewBox 100×64） */
const DIPPER: Array<[number, number]> = [
  [14, 14], [10, 36], [28, 46], [36, 22], [56, 30], [73, 40], [90, 52],
];
const DIPPER_LINKS: Array<[number, number]> = [
  [3, 0], [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6],
];

export interface LoongCardOpts {
  lang: "zh" | "en";
  collected: number;
  summons: number;
  scales: number;
  loongUnlocked: boolean;
  siteOrigin: string;
  stars: string[];
  qrCanvas?: HTMLCanvasElement | null;
}

export function drawLoongCard(canvas: HTMLCanvasElement, o: LoongCardOpts): void {
  const W = 1080;
  const H = 1350;
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const zh = o.lang === "zh";

  /* 背景：墨底金调渐变 + 伪随机星点 */
  const bg = ctx.createLinearGradient(0, 0, W, H);
  bg.addColorStop(0, "#0a0b14");
  bg.addColorStop(0.55, "#0d1018");
  bg.addColorStop(1, "#171208");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
  let seed = 42;
  const rnd = () => {
    seed = (seed * 9301 + 49297) % 233280;
    return seed / 233280;
  };
  for (let i = 0; i < 90; i++) {
    const x = rnd() * W;
    const y = rnd() * H;
    const r = rnd() * 1.6 + 0.3;
    ctx.fillStyle = `rgba(255, 240, 200, ${0.06 + rnd() * 0.22})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }

  /* 内边框 */
  ctx.strokeStyle = "rgba(245,197,66,0.35)";
  ctx.lineWidth = 3;
  roundRect(ctx, 36, 36, W - 72, H - 72, 28);
  ctx.stroke();

  /* 顶部品牌行 */
  ctx.textAlign = "center";
  ctx.fillStyle = "rgba(245,197,66,0.65)";
  ctx.font = "600 30px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText("BOUNDLESS · LOONG CODEX", W / 2, 118);

  /* 标题 */
  ctx.fillStyle = GOLD_HI;
  ctx.font = `900 ${zh ? 88 : 66}px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif`;
  ctx.shadowColor = "rgba(245,197,66,0.5)";
  ctx.shadowBlur = 26;
  ctx.fillText(zh ? "七星聚 · 龙行无界" : "Seven Stars · Boundless Loong", W / 2, 226);
  ctx.shadowBlur = 0;

  /* 北斗：DIPPER(100×64) → 缩放居中 */
  const scale = 7.6;
  const dw = 100 * scale;
  const dx = (W - dw) / 2;
  const dy = 320;
  const P = DIPPER.map(([x, y]) => [dx + x * scale, dy + y * scale] as const);

  ctx.strokeStyle = "rgba(245,197,66,0.4)";
  ctx.lineWidth = 3;
  ctx.setLineDash([10, 14]);
  ctx.beginPath();
  for (const [a, b] of DIPPER_LINKS) {
    ctx.moveTo(P[a][0], P[a][1]);
    ctx.lineTo(P[b][0], P[b][1]);
  }
  ctx.stroke();
  ctx.setLineDash([]);

  for (let i = 0; i < 7; i++) {
    const [x, y] = P[i];
    const on = i < o.collected;
    if (on) {
      const glow = ctx.createRadialGradient(x, y, 2, x, y, 34);
      glow.addColorStop(0, "rgba(255,233,168,0.95)");
      glow.addColorStop(0.4, "rgba(245,197,66,0.5)");
      glow.addColorStop(1, "rgba(245,197,66,0)");
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(x, y, 34, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = on ? GOLD : "rgba(255,255,255,0.2)";
    ctx.beginPath();
    ctx.arc(x, y, on ? 13 : 9, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = on ? "#f3dfa4" : "rgba(255,255,255,0.4)";
    ctx.font = "500 27px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif";
    ctx.fillText(o.stars[i] ?? "", x, y + 58);
  }

  /* 进度大字 */
  ctx.fillStyle = GOLD_HI;
  ctx.font = "800 78px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(`${o.collected} / 7`, W / 2, dy + 64 * scale + 140);

  /* 统计行 */
  ctx.fillStyle = "rgba(255,246,223,0.85)";
  ctx.font = "500 42px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif";
  const statsLine = zh
    ? `召唤 ${o.summons} 次 · 月鳞 ${o.scales}/3`
    : `Summons ${o.summons} · Scales ${o.scales}/3`;
  ctx.fillText(statsLine, W / 2, dy + 64 * scale + 220);

  ctx.fillStyle = o.loongUnlocked ? GOLD : "rgba(255,255,255,0.55)";
  ctx.font = "600 40px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif";
  ctx.fillText(
    o.loongUnlocked
      ? zh ? "🐉 祥龙金鳞 · 已解锁" : "🐉 Golden Loong · Unlocked"
      : zh ? "✦ 司星者守北斗" : "✦ Star Keeper on duty",
    W / 2,
    dy + 64 * scale + 296
  );

  /* 底部：邀约 + 链接 + QR */
  const footY = H - 210;
  ctx.strokeStyle = "rgba(255,255,255,0.12)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(90, footY - 60);
  ctx.lineTo(W - 90, footY - 60);
  ctx.stroke();

  ctx.textAlign = "left";
  ctx.fillStyle = "rgba(255,246,223,0.9)";
  ctx.font = "600 40px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif";
  ctx.fillText(zh ? "每日一珠 · 七星唤龙" : "One pearl a day · seven summon the Loong", 96, footY + 10);
  ctx.fillStyle = "rgba(245,197,66,0.9)";
  ctx.font = "500 34px 'Segoe UI', system-ui, sans-serif";
  ctx.fillText(`${o.siteOrigin.replace(/^https?:\/\//, "")}/loong`, 96, footY + 68);
  ctx.fillStyle = "rgba(255,255,255,0.4)";
  ctx.font = "400 28px 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif";
  ctx.fillText(zh ? "无界科技 · 限定玩法" : "Boundless Tech · Limited quest", 96, footY + 118);

  if (o.qrCanvas) {
    const qs = 176;
    const qx = W - 96 - qs;
    const qy = footY - 24;
    ctx.fillStyle = "#fffdf4";
    roundRect(ctx, qx - 12, qy - 12, qs + 24, qs + 24, 18);
    ctx.fill();
    ctx.drawImage(o.qrCanvas, qx, qy, qs, qs);
  }
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
