"use client";

import { useEffect, useRef } from "react";

/**
 * 蝙蝠群消散→群飞→定点重聚 的 canvas 覆盖层（仅恶魔彩蛋态挂载）。
 * 纯 canvas 2D + 单 rAF，transform/opacity 级开销；reduced/低配不挂载。
 *
 * 编排（每只蝙蝠独立错峰，形成“随机排队成流”的观感）：
 *  1) 起点：蝙蝠铺满恶魔轮廓（密集）——与 DOM 本体“碎裂溶解”同步；
 *  2) 爆散：按随机 departDelay 逐批向外迸发（不是一次全散）；
 *  3) 群飞：沿各自带弧度的路径飞向落点，带扰动；
 *  4) 回聚：按随机 gather 时序逐批收拢回落点处的恶魔轮廓 → onArrive → 本体重现。
 * fromX≈toX 时表现为“原地爆散再聚合”（用于变身揭示）。
 */
export type BatFlight = { id: number; fromX: number; fromY: number; toX: number; toY: number };

type Bat = {
  ox: number; // 轮廓内相对偏移（起终点共用 → 回聚成同一形状）
  oy: number;
  bx: number; // 迸发散点相对偏移
  by: number;
  arc: number; // 群飞路径的横向弧度
  size: number;
  flap: number;
  flapSpeed: number;
  red: boolean;
  wobble: number;
  dd: number; // 起飞错峰延迟 [0, 0.24]
  gd: number; // 回聚起点抖动 [0, 0.14]
};

const DURATION = 1250; // 一次飞行总时长(ms)
const BURST_END = 0.18; // 爆散阶段（局部进度）
const ARRIVE_AT = 0.86; // 本体在此进度开始重现，与最后一批回聚重叠，衔接更顺

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);
const easeInOut = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const lerp = (a: number, b: number, k: number) => a + (b - a) * k;

function drawBat(ctx: CanvasRenderingContext2D, x: number, y: number, b: Bat, alpha: number) {
  if (alpha <= 0.01) return;
  const s = b.size;
  const wy = -s * 0.12 - Math.abs(Math.sin(b.flap)) * s * 0.6; // 翅尖扇动
  ctx.save();
  ctx.translate(x, y);
  ctx.globalAlpha = alpha;
  ctx.fillStyle = b.red ? "#c01f38" : "#331018";
  ctx.shadowColor = b.red ? "#fb7185" : "#e11d48";
  ctx.shadowBlur = b.red ? 6 : 3.5;
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.quadraticCurveTo(-s * 0.5, wy, -s, wy * 0.5);
  ctx.quadraticCurveTo(-s * 0.55, s * 0.22, -s * 0.18, s * 0.16);
  ctx.quadraticCurveTo(0, s * 0.34, s * 0.18, s * 0.16);
  ctx.quadraticCurveTo(s * 0.55, s * 0.22, s, wy * 0.5);
  ctx.quadraticCurveTo(s * 0.5, wy, 0, 0);
  ctx.closePath();
  ctx.fill();
  ctx.beginPath();
  ctx.ellipse(0, s * 0.04, s * 0.14, s * 0.28, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.shadowBlur = 0;
  ctx.restore();
}

export default function BatSwarm({ flight, count = 48, onArrive }: { flight: BatFlight | null; count?: number; onArrive?: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rafRef = useRef(0);
  const onArriveRef = useRef(onArrive);
  onArriveRef.current = onArrive;

  useEffect(() => {
    if (!flight) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    canvas.width = vw * dpr;
    canvas.height = vh * dpr;
    canvas.style.width = `${vw}px`;
    canvas.style.height = `${vh}px`;
    ctx.scale(dpr, dpr);

    const { fromX, fromY, toX, toY } = flight;
    const dirX = toX - fromX;
    const dirY = toY - fromY;
    const dist = Math.hypot(dirX, dirY);
    // 群飞方向的单位法向量（给路径加横向弧度用）；原地变身时无方向→弧度取竖直
    const nx = dist > 1 ? -dirY / dist : 1;
    const ny = dist > 1 ? dirX / dist : 0;

    const bats: Bat[] = [];
    for (let i = 0; i < count; i++) {
      // 恶魔轮廓采样（竖长椭圆 + 轻微上偏，贴合兜帽斗篷剪影）
      const a = Math.random() * Math.PI * 2;
      const r = Math.sqrt(Math.random());
      const ox = Math.cos(a) * 24 * r;
      const oy = Math.sin(a) * 50 * r - 6;
      // 迸发散点：大致沿轮廓法向朝外
      const ba = Math.atan2(oy + 6, ox) + (Math.random() - 0.5) * 0.9;
      const bmag = 46 + Math.random() * 88;
      bats.push({
        ox,
        oy,
        bx: ox + Math.cos(ba) * bmag,
        by: oy + Math.sin(ba) * bmag - 12,
        arc: (Math.random() - 0.5) * 70,
        size: 5.5 + Math.random() * 7,
        flap: Math.random() * Math.PI * 2,
        flapSpeed: 0.4 + Math.random() * 0.35,
        red: Math.random() < 0.16,
        wobble: Math.random() * Math.PI * 2,
        dd: Math.random() * 0.24,
        gd: Math.random() * 0.14,
      });
    }

    const start = performance.now();
    let arrived = false;
    let cleared = false;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      ctx.clearRect(0, 0, vw, vh);
      for (const b of bats) {
        const lp = Math.min(1, Math.max(0, (t - b.dd) / (1 - b.dd))); // 该蝙蝠的局部进度
        const gatherStart = 0.6 + b.gd;
        let px: number;
        let py: number;
        if (lp < BURST_END) {
          // 爆散：从轮廓点冲到散点
          const k = easeOut(lp / BURST_END);
          px = fromX + lerp(b.ox, b.bx, k);
          py = fromY + lerp(b.oy, b.by, k);
        } else if (lp < gatherStart) {
          // 群飞：起点散点 → 落点散点，二次贝塞尔加弧度 + 逐渐收敛的扰动
          const k = easeInOut((lp - BURST_END) / (gatherStart - BURST_END));
          const p0x = fromX + b.bx;
          const p0y = fromY + b.by;
          const p2x = toX + b.bx;
          const p2y = toY + b.by;
          const midx = (p0x + p2x) / 2 + nx * b.arc;
          const midy = (p0y + p2y) / 2 + ny * b.arc;
          const mk = 1 - k;
          px = mk * mk * p0x + 2 * mk * k * midx + k * k * p2x;
          py = mk * mk * p0y + 2 * mk * k * midy + k * k * p2y;
          const flutter = (1 - k) * 20;
          px += Math.sin(now * 0.006 + b.wobble) * flutter;
          py += Math.cos(now * 0.007 + b.wobble) * flutter * 0.7;
        } else {
          // 回聚：落点散点 → 落点轮廓点
          const k = easeInOut((lp - gatherStart) / (1 - gatherStart));
          px = lerp(toX + b.bx, toX + b.ox, k);
          py = lerp(toY + b.by, toY + b.oy, k);
        }
        // 逐只淡入淡出：起飞时冒出、落位时融入重现的本体
        const alpha = lp < 0.09 ? lp / 0.09 : lp > 0.9 ? Math.max(0, (1 - lp) / 0.1) : 1;
        b.flap += b.flapSpeed;
        drawBat(ctx, px, py, b, alpha);
      }
      if (t >= ARRIVE_AT && !arrived) {
        arrived = true;
        onArriveRef.current?.();
      }
      if (t >= 1) {
        if (!cleared) {
          cleared = true;
          ctx.clearRect(0, 0, vw, vh);
        }
        return;
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(rafRef.current);
      ctx.clearRect(0, 0, vw, vh);
    };
  }, [flight, count]);

  return <canvas ref={canvasRef} className="pointer-events-none fixed inset-0 z-[60]" aria-hidden />;
}
