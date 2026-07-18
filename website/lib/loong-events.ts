/**
 * 祥龙 / 星珠玩法跨组件事件契约（DragonQuest ↔ AISprite）。
 * 自定义事件名保持稳定；payload 在此集中类型化，避免魔法字符串漂移。
 */

export const LOONG_APPLY_SKIN = "bl:apply-skin";
export const LOONG_TEASER = "bl:dragon-teaser";
/** 召唤仪式开始/结束：AISprite 可暂停游动 rAF，省帧预算 */
export const LOONG_CEREMONY = "bl:loong-ceremony";

export type LoongSkinId = "normal" | "demon" | "loong";

export type LoongApplySkinDetail = { skin: LoongSkinId };
export type LoongTeaserDetail = {
  collected: number;
  /** 全息文案；缺省由 AISprite 按语言回退 */
  text?: string;
};
export type LoongCeremonyDetail = { active: boolean };

export function dispatchApplySkin(skin: LoongSkinId) {
  window.dispatchEvent(new CustomEvent(LOONG_APPLY_SKIN, { detail: { skin } satisfies LoongApplySkinDetail }));
}

export function dispatchLoongTeaser(detail: LoongTeaserDetail) {
  window.dispatchEvent(new CustomEvent(LOONG_TEASER, { detail }));
}

export function dispatchLoongCeremony(active: boolean) {
  window.dispatchEvent(new CustomEvent(LOONG_CEREMONY, { detail: { active } satisfies LoongCeremonyDetail }));
}
