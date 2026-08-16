import { useEffect, useSyncExternalStore } from "react";

/**
 * 右下角「互动坞」契约：聊天 FAB / 聊天面板 / EveBot / 龙珠簇共用一个空间，
 * 这里是唯一房东——层级 token + 占用广播，住户之间不再互相盲叠。
 *
 * 层级表（fixed 元素统一从这里对表，勿再各写各的）：
 *   40  移动端底部 CTA 条
 *   50  聊天 FAB / EveBot 常驻态
 *   55  龙影 ghost / 精灵 hover 态
 *   60  龙珠簇（气泡/托盘/引导）
 *   70  龙珠托盘面板
 *   80  聊天面板
 *   95  全局 toast（底部居中）
 *   300 召唤仪式全屏
 */

export const DOCK_EVENT = "bl:dock";

export type DockSource = "chat" | "dragon-panel";
export type DockDetail = { source: DockSource; active: boolean };

export function dispatchDock(source: DockSource, active: boolean) {
  window.dispatchEvent(new CustomEvent(DOCK_EVENT, { detail: { source, active } satisfies DockDetail }));
}

/* ── 龙珠状态外发（聊天面板迷你星珠条渲染用；无隐私字段） ── */
export const DRAGON_STATE_EVENT = "bl:dragon-state";
export type DragonStateDetail = {
  collected: number;
  todayCollected: boolean;
  canSummon: boolean;
};
export function dispatchDragonState(detail: DragonStateDetail) {
  window.dispatchEvent(new CustomEvent(DRAGON_STATE_EVENT, { detail }));
}

/* ── 外部请求收珠（迷你星珠条点击 → DragonQuest 执行，结果仍走服务端记账） ── */
export const DRAGON_COLLECT_EVENT = "bl:dragon-collect";
export function dispatchDragonCollect() {
  window.dispatchEvent(new CustomEvent(DRAGON_COLLECT_EVENT));
}

/* ══════════════ 注意力叫号机（自动浮层排队，同屏最多一个「主动打扰」） ══════════════
 *
 * 上一轮互斥管住了「用户主动打开」的浮层；自动弹出物（星珠气泡 / AI 招呼）各自决定
 * 何时出现导致同屏撞车。这里统一叫号：想弹先领号，被叫到才渲染。
 *
 * 轮转规则（避免弹跳）：持有者到期（HOLD_MS）不立刻下台——只有队列里有人等时才让位、
 * 自己回队尾；队列空则一直续期。让位对 caller 可见（granted 下降沿），招呼气泡以此
 * 判断「已充分曝光」自行收场，星珠气泡则排队等回归。
 */

export type AttentionId = "pearl-offer" | "chat-teaser";

const ATTN_HOLD_MS: Record<AttentionId, number> = {
  /* 星珠 15s 无人理会即可让位（收下后组件自会 release） */
  "pearl-offer": 15000,
  /* 招呼 22s 完整阅读窗口 */
  "chat-teaser": 22000,
};
const ATTN_PRIO: Record<AttentionId, number> = { "pearl-offer": 2, "chat-teaser": 1 };

type AttnState = {
  current: AttentionId | null;
  expired: boolean;
  queue: AttentionId[];
  timer: ReturnType<typeof setTimeout> | null;
};
const attn: AttnState = { current: null, expired: false, queue: [], timer: null };
const attnSubs = new Set<() => void>();

function attnNotify() {
  attnSubs.forEach((f) => f());
}

function attnGrant(id: AttentionId) {
  attn.current = id;
  attn.expired = false;
  if (attn.timer) clearTimeout(attn.timer);
  attn.timer = setTimeout(() => {
    attn.expired = true;
    if (attn.queue.length > 0) attnYield();
  }, ATTN_HOLD_MS[id]);
  attnNotify();
}

/** 持有者让位：回队尾，放行队首 */
function attnYield() {
  const cur = attn.current;
  if (attn.timer) clearTimeout(attn.timer);
  attn.timer = null;
  attn.current = null;
  if (cur) attn.queue.push(cur);
  const next = attn.queue.shift();
  if (next) attnGrant(next);
  else attnNotify();
}

export function attnRequest(id: AttentionId) {
  if (attn.current === id || attn.queue.includes(id)) return;
  attn.queue.push(id);
  attn.queue.sort((a, b) => ATTN_PRIO[b] - ATTN_PRIO[a]);
  if (!attn.current) {
    const next = attn.queue.shift();
    if (next) attnGrant(next);
  } else if (attn.expired) {
    attnYield();
  } else {
    attnNotify();
  }
}

export function attnRelease(id: AttentionId) {
  attn.queue = attn.queue.filter((x) => x !== id);
  if (attn.current === id) {
    if (attn.timer) clearTimeout(attn.timer);
    attn.timer = null;
    attn.current = null;
    const next = attn.queue.shift();
    if (next) attnGrant(next);
    else attnNotify();
  } else {
    attnNotify();
  }
}

export function attnGranted(id: AttentionId): boolean {
  return attn.current === id;
}

/** 当前是否有任何自动浮层在台上（全息播报等「只避让不占号」的场景用） */
export function attnActive(): AttentionId | null {
  return attn.current;
}

function attnSubscribe(fn: () => void): () => void {
  attnSubs.add(fn);
  return () => attnSubs.delete(fn);
}

/** want=true 入队等叫号；返回「此刻是否轮到我」。卸载或 want=false 自动释放。 */
export function useAttention(id: AttentionId, want: boolean): boolean {
  const granted = useSyncExternalStore(
    attnSubscribe,
    () => attnGranted(id),
    () => false
  );
  useEffect(() => {
    if (!want) return;
    attnRequest(id);
    return () => attnRelease(id);
  }, [id, want]);
  return want && granted;
}
