import { access } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import { DATA_DIR } from "./data-dir";
import type { ChatxPersona, ChatxPrefs } from "./chatx-prefs";

/**
 * @ChatX_bot 音色库：取自智聊 / AvatarHub 真人音色库（voice_pack_aishell3，AISHELL-3 棚录真人，Apache 2.0），
 * 名字沿用库里的精选名（角色库命名方案 2026-07-09 §2.3）；没有精选名的按旧版「清晰女声」规则标注。
 * 参考音由 scripts/chatx-voicepack-fetch.ts 按智聊同一流程（抓样本 → 去静音拼接 → 体检）落到 VOICEPACK_DIR，
 * 文件在服务器数据目录、不进 public，不对外暴露。
 *
 * 选择顺序（resolveVoiceChoice）：用户对该人设的显式选择 → 运维环境变量 CHATX_BOT_VOICE_REF_<P>（chatx-voice 处理）
 * → 默认参考音（SSB0139 醇厚书卷 clone-original.mp3）。没选过的用户行为与之前完全一致；人设推荐音色只在菜单里标 ⭐。
 */

export type VoiceGender = "male" | "female";
export interface VoiceEntry {
  spk: string;
  zh: string;
  en: string;
  gender: VoiceGender;
  tagZh: string;
  tagEn: string;
}

export const VOICEPACK: readonly VoiceEntry[] = [
  { spk: "SSB0139", zh: "醇厚书卷", en: "Mellow Scholar", gender: "male", tagZh: "中低音男声，耐听木质共鸣（小界默认）", tagEn: "warm low male (Xiaojie default)" },
  { spk: "SSB0016", zh: "录音棚女声", en: "Studio Female", gender: "female", tagZh: "清亮女声，字字透亮", tagEn: "clear bright female" },
  { spk: "SSB0534", zh: "清晰女声B", en: "Clear Female B", gender: "female", tagZh: "成熟女声，北方口音", tagEn: "mature female" },
  { spk: "SSB1328", zh: "深夜电台", en: "Late-night Radio", gender: "male", tagZh: "磁性低音，晚间陪伴感", tagEn: "magnetic low male" },
  { spk: "SSB0710", zh: "爽朗掌柜", en: "Cheerful Host", gender: "male", tagZh: "明亮快语速，热络招呼感", tagEn: "bright, lively male" },
  { spk: "SSB0966", zh: "静水流深", en: "Still Waters", gender: "male", tagZh: "沉稳低音，可靠讲述者", tagEn: "calm, steady male" },
  { spk: "SSB1136", zh: "深海低音", en: "Deep Bass", gender: "male", tagZh: "全库最低音，品牌口播", tagEn: "deepest male" },
];

/** 人设推荐音色（菜单 ⭐ 标注，不自动套用）：小界就是默认参考音；恋爱陪聊推女声、销售跟进推热络男声。 */
export const PERSONA_DEFAULT_VOICE: Record<ChatxPersona, string | null> = { xiaojie: null, lover: "SSB0016", sales: "SSB0710" };

/** 用户选择值：库里的 spk / 我的克隆音 / 默认参考音。 */
export const MY_VOICE = "mine";
export const DEFAULT_VOICE = "default";

export const VOICEPACK_DIR = process.env.CHATX_VOICEPACK_DIR || path.join(DATA_DIR, "chatx_voicepack");
export const USER_VOICE_DIR = process.env.CHATX_USER_VOICE_DIR || path.join(DATA_DIR, "chatx_voices");

export function voiceEntry(spk: string): VoiceEntry | undefined {
  return VOICEPACK.find((v) => v.spk === spk);
}

export function voicepackPath(spk: string): string {
  return path.join(VOICEPACK_DIR, `${spk}_ref.wav`);
}

/** 按钮 / 参数值校验：库 spk、mine、default；其它一律 null（防注入路径）。 */
export function parseVoiceChoice(v: string | undefined): string | null {
  const s = (v ?? "").trim();
  if (s === MY_VOICE || s === DEFAULT_VOICE) return s;
  return voiceEntry(s) ? s : null;
}

export function hasMyVoice(prefs: ChatxPrefs): boolean {
  return Boolean(prefs.myVoice?.owner_consent && prefs.myVoice.path);
}

/** 该人设实际要用的选择：显式选择（mine 需仍有克隆音）→ default（再由 chatx-voice 走环境变量 / 默认参考音）。 */
export function resolveVoiceChoice(prefs: ChatxPrefs, persona: ChatxPersona): { choice: string; explicit: boolean } {
  const own = prefs.voices?.[persona];
  if (own && parseVoiceChoice(own) && (own !== MY_VOICE || hasMyVoice(prefs))) return { choice: own, explicit: true };
  return { choice: DEFAULT_VOICE, explicit: false };
}

export function voiceLabel(choice: string, lang: BotLang): string {
  const zh = lang === "zh";
  if (choice === MY_VOICE) return zh ? "🧬 我的声音" : "🧬 My voice";
  if (choice === DEFAULT_VOICE) return zh ? "醇厚书卷（默认）" : "Mellow Scholar (default)";
  const e = voiceEntry(choice);
  if (!e) return choice;
  const icon = e.gender === "female" ? "👩" : "👨";
  return `${icon} ${zh ? e.zh : e.en}`;
}

let availCache: { at: number; set: Set<string> } | null = null;
/** 已落盘的库音色（60s 缓存）；抓取脚本没跑过就是空集，菜单只显示可用的。 */
export async function availableVoices(now = Date.now()): Promise<Set<string>> {
  if (availCache && now - availCache.at < 60_000) return availCache.set;
  const set = new Set<string>();
  await Promise.all(
    VOICEPACK.map(async (v) => {
      try {
        await access(voicepackPath(v.spk));
        set.add(v.spk);
      } catch {
        /* not fetched */
      }
    })
  );
  availCache = { at: now, set };
  return set;
}

export function resetVoicepackCache(): void {
  availCache = null;
}
