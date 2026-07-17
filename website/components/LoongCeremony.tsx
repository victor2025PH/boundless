"use client";

import { useEffect, useRef } from "react";

/**
 * 界龙现世：一次性 canvas 长龙演出（约 4.2s），与 BatSwarm 同架构（单 rAF、自清理）。
 *
 * 编排：左下入场 S 形掠升 → 绕屏心一整圈（衔珠盘旋）→ 右上破界离场。
 * 龙体：头部沿参数路径运动，34 节鳞段按弧长间距沿"历史轨迹"拖尾（蛇形跟随），
 * 金鳞渐变 + 背脊鳍 + 鹿角/龙须/青鬃 + 龙首前方追逐的辉光宝珠（龙戏珠）。
 * 性能：只有头部/宝珠开 shadowBlur；reduced/lowFx 由父组件决定不挂载。
 */

const DURATION = 4200;

type Pt = { x: number; y: number };

export default function LoongCeremony({ onDone }: { onDone: () => void }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = vw * dpr;
    canvas.height = vh * dpr;
    ctx.scale(dpr, dpr);

    const small = Math.min(vw, vh);
    const SEGS = vw < 640 ? 30 : 42;
    const maxR = Math.max(8, Math.min(19, small * 0.02 + 6));
    /* 间距 ≈ 0.72 倍头径：相邻鳞段重叠 ~1/4，身体连贯不成"串珠" */
    const SPACING = Math.max(7, maxR * 0.72);

    /* ---- 路径：三段 C0 连续（S 入场 → 椭圆环绕 → 出场） ---- */
    const cx = vw * 0.5;
    const cy = vh * 0.46;
    const rx = vw * 0.3;
    const ry = vh * 0.26;
    const th0 = -0.35; // 环绕起点角（弧度，右上方）
    const loopAt = (th: number): Pt => ({ x: cx + Math.cos(th) * rx, y: cy + Math.sin(th) * ry });
    const pInEnd = loopAt(th0);
    /* 环绕段在 th0 处的切向：入/出场贝塞尔沿此切线衔接，转折处不折角 */
    const tanX = -Math.sin(th0) * rx;
    const tanY = Math.cos(th0) * ry;
    const pIn0: Pt = { x: -vw * 0.1, y: vh * 0.82 };
    const pIn1: Pt = { x: vw * 0.16, y: vh * 0.24 };
    const pIn2: Pt = { x: pInEnd.x - tanX * 0.45, y: pInEnd.y - tanY * 0.45 };
    const pOut0: Pt = { x: pInEnd.x + tanX * 0.4, y: pInEnd.y + tanY * 0.4 };
    const pOut1: Pt = { x: vw * 0.98, y: vh * 0.02 };
    const pOutEnd: Pt = { x: vw * 1.18, y: -vh * 0.16 };

    const bez = (a: Pt, b: Pt, c: Pt, d: Pt, t: number): Pt => {
      const u = 1 - t;
      return {
        x: u * u * u * a.x + 3 * u * u * t * b.x + 3 * u * t * t * c.x + t * t * t * d.x,
        y: u * u * u * a.y + 3 * u * u * t * b.y + 3 * u * t * t * c.y + t * t * t * d.y,
      };
    };
    /* 入场用 easeOut（破空而入后匀速），环绕匀速，出场 easeIn（加速离场）——
       段间速度连续，不再有"顿一下再走"的机械感 */
    const easeOutQ = (t: number) => 1 - (1 - t) * (1 - t);
    const easeInQ = (t: number) => t * t;

    /** 头部位置：t ∈ [0,1] */
    const headAt = (t: number): Pt => {
      if (t < 0.3) {
        return bez(pIn0, pIn1, pIn2, pInEnd, easeOutQ(t / 0.3));
      }
      if (t < 0.78) {
        const th = th0 + ((t - 0.3) / 0.48) * Math.PI * 2;
        return loopAt(th);
      }
      const k = easeInQ((t - 0.78) / 0.22);
      return bez(loopAt(th0), pOut0, pOut1, pOutEnd, k);
    };

    /* ---- 蛇形拖尾：记录头部轨迹，按弧长回溯放置鳞段 ---- */
    const history: Pt[] = [];
    const start = performance.now();
    let raf = 0;
    let finished = false;
    let fadeNow = 1;

    const gold = (l: number) => `hsl(43, 80%, ${l}%)`;

    /** 脊柱底层：相邻鳞段连线（粗圆头线段），把圆点缝成连贯蛇躯 */
    const drawSpine = (a: Pt, b: Pt, r: number, i: number) => {
      ctx.globalAlpha = fadeNow * (1 - (i / SEGS) * 0.3);
      ctx.strokeStyle = gold(50 - i * 0.5);
      ctx.lineWidth = r * 1.9;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    };

    const drawSeg = (p: Pt, r: number, i: number, time: number) => {
      const shimmer = 1 + 0.06 * Math.sin(i * 0.85 + time * 5);
      const rr = r * shimmer;
      ctx.globalAlpha = fadeNow * (1 - (i / SEGS) * 0.3);
      ctx.fillStyle = gold(54 - i * 0.55);
      ctx.beginPath();
      ctx.arc(p.x, p.y, rr, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(130,85,12,0.35)";
      ctx.lineWidth = 1;
      ctx.stroke();
    };

    const drawRidge = (p: Pt, prev: Pt, r: number) => {
      const dx = p.x - prev.x;
      const dy = p.y - prev.y;
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len;
      const ny = dx / len;
      /* 背脊小鳍：垂直于行进方向、朝"上侧"（y 分量小的一侧） */
      const s = ny < 0 ? 1 : -1;
      ctx.globalAlpha = fadeNow * 0.85;
      ctx.fillStyle = gold(38);
      ctx.beginPath();
      ctx.moveTo(p.x + nx * s * r * 0.7, p.y + ny * s * r * 0.7);
      ctx.lineTo(p.x + nx * s * r * 1.5 + (dx / len) * r * 0.28, p.y + ny * s * r * 1.5 + (dy / len) * r * 0.28);
      ctx.lineTo(p.x + nx * s * r * 0.7 + (dx / len) * r * 0.6, p.y + ny * s * r * 0.7 + (dy / len) * r * 0.6);
      ctx.closePath();
      ctx.fill();
    };

    const drawHead = (p: Pt, ang: number, time: number) => {
      ctx.save();
      ctx.translate(p.x, p.y);
      ctx.rotate(ang);
      const r = maxR * 1.12;

      /* 青鬃：脑后 4 缕飘带 */
      ctx.strokeStyle = "rgba(56,189,248,0.65)";
      ctx.lineWidth = 2.2;
      for (let i = 0; i < 4; i++) {
        const off = (i - 1.5) * (r * 0.5);
        const sway = Math.sin(time * 4 + i) * r * 0.5;
        ctx.beginPath();
        ctx.moveTo(-r * 0.6, off * 0.62);
        ctx.quadraticCurveTo(-r * 2.1, off + sway * 0.4, -r * 3.3, off * 1.35 + sway);
        ctx.stroke();
      }

      /* 头 + 吻部（发光核心） */
      ctx.shadowColor = "#f5c542";
      ctx.shadowBlur = 16;
      ctx.fillStyle = gold(58);
      ctx.beginPath();
      ctx.arc(0, 0, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = gold(63);
      ctx.beginPath();
      ctx.arc(r * 0.82, -r * 0.05, r * 0.66, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(r * 1.4, r * 0.02, r * 0.42, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;

      /* 鹿角：双叉后掠 */
      ctx.strokeStyle = "#caa14e";
      ctx.lineWidth = 2.4;
      ctx.lineCap = "round";
      for (const s of [-1, 1]) {
        ctx.beginPath();
        ctx.moveTo(-r * 0.15, s * r * 0.42);
        ctx.quadraticCurveTo(-r * 1.1, s * r * 1.15, -r * 1.75, s * r * 0.9);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(-r * 0.85, s * r * 0.86);
        ctx.quadraticCurveTo(-r * 1.35, s * r * 1.6, -r * 1.9, s * r * 1.62);
        ctx.stroke();
      }

      /* 龙须：吻侧两根长须随时间摆动 */
      ctx.strokeStyle = "#ffd75e";
      ctx.lineWidth = 1.4;
      for (const s of [-1, 1]) {
        const sway = Math.sin(time * 3.2 + s) * r * 0.5;
        ctx.beginPath();
        ctx.moveTo(r * 1.28, s * r * 0.3);
        ctx.quadraticCurveTo(r * 0.4, s * r * 1.5 + sway * 0.4, -r * 1.2, s * r * 1.8 + sway);
        ctx.stroke();
      }

      /* 眼：琥珀底 + 墨瞳 + 高光 */
      ctx.fillStyle = "#7c2d12";
      ctx.beginPath();
      ctx.arc(r * 0.42, -r * 0.34, r * 0.17, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.9)";
      ctx.beginPath();
      ctx.arc(r * 0.47, -r * 0.4, r * 0.055, 0, Math.PI * 2);
      ctx.fill();

      ctx.restore();
    };

    const drawPearl = (head: Pt, ang: number, time: number) => {
      const dist = maxR * 3.1;
      const wob = Math.sin(time * 4.2) * 5;
      const px = head.x + Math.cos(ang) * dist - Math.sin(ang) * wob;
      const py = head.y + Math.sin(ang) * dist + Math.cos(ang) * wob;
      const pr = maxR * 0.58;
      ctx.save();
      ctx.shadowColor = "#f5c542";
      ctx.shadowBlur = 22;
      const g = ctx.createRadialGradient(px - pr * 0.3, py - pr * 0.3, pr * 0.1, px, py, pr);
      g.addColorStop(0, "#fffbe8");
      g.addColorStop(0.55, "#f5c542");
      g.addColorStop(1, "#b8860b");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(px, py, pr, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    };

    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      const time = (now - start) / 1000;
      ctx.clearRect(0, 0, vw, vh);

      const head = headAt(t);
      /* 轨迹历史：头进一步，尾随其后 */
      if (!history.length || Math.hypot(head.x - history[0].x, head.y - history[0].y) > 1.2) {
        history.unshift({ x: head.x, y: head.y });
        if (history.length > 700) history.pop();
      }

      /* 按弧长采样鳞段位置 */
      const pts: Pt[] = [head];
      let need = SPACING;
      let acc = 0;
      for (let h = 1; h < history.length && pts.length < SEGS; h++) {
        const a = history[h - 1];
        const b = history[h];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        acc += d;
        while (acc >= need && pts.length < SEGS) {
          const back = (acc - need) / (d || 1);
          pts.push({ x: b.x + (a.x - b.x) * back, y: b.y + (a.y - b.y) * back });
          need += SPACING;
        }
      }

      /* 入场淡入 / 离场整体淡出 */
      fadeNow = t < 0.06 ? t / 0.06 : t > 0.9 ? 1 - (t - 0.9) / 0.1 : 1;
      ctx.save();

      /* 尾→头绘制：先缝脊柱（连贯躯干），再叠鳞段与背鳍 */
      for (let i = pts.length - 1; i >= 1; i--) {
        const taper = maxR * (0.32 + 0.68 * (1 - i / SEGS));
        drawSpine(pts[i], pts[i - 1], taper, i);
      }
      for (let i = pts.length - 1; i >= 1; i--) {
        const taper = maxR * (0.32 + 0.68 * (1 - i / SEGS));
        drawSeg(pts[i], taper, i, time);
        if (i % 2 === 0 && i < pts.length - 2) drawRidge(pts[i], pts[i + 1], taper);
      }
      const ang = pts.length > 1 ? Math.atan2(pts[0].y - pts[1].y, pts[0].x - pts[1].x) : 0;
      ctx.globalAlpha = fadeNow;
      drawHead(head, ang, time);
      drawPearl(head, ang, time);
      ctx.restore();

      if (t >= 1) {
        if (!finished) {
          finished = true;
          ctx.clearRect(0, 0, vw, vh);
          doneRef.current();
        }
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
    };
  }, []);

  return <canvas ref={canvasRef} className="pointer-events-none absolute inset-0" aria-hidden />;
}
