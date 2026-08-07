/** 品牌片单一真相：/film 页、首页影院区、/videos 精选卡、/order 横幅共用一份数据。
 *  视频本体走 nginx /media/feed（与日更 feed 同通道，部署不覆盖）；海报/og 图随仓部署。
 *  章节时点由片源工程 `_chapters_calc.py` 按组装公式算出（2026-08-07，v1.1 版）。 */

export type FilmLang = "zh" | "en";

export const BRAND_FILM = {
  src: { zh: "/media/feed/brand-film-zh.mp4", en: "/media/feed/brand-film-en.mp4" },
  poster: { zh: "/videos/film/poster-zh.jpg", en: "/videos/film/poster-en.jpg" },
  og: { zh: "/brand/campaign/og-film.jpg", en: "/brand/campaign/og-film-en.jpg" },
  durationSec: { zh: 143, en: 184 },
  durationLabel: { zh: "2:23", en: "3:04" },
  uploadDate: "2026-08-07",
  title: {
    zh: "这部片子，没有摄影师、配音员和翻译",
    en: "No camera crew. No voice actor. No translator.",
  },
  tagline: {
    zh: "3 分钟看完六个功能的真机实测——包括正在介绍它的主持人，也是它做的。",
    en: "Six features, three minutes, all running live on one machine — including the host presenting them.",
  },
  badgeNote: {
    zh: "右上角绿标 = 真实引擎输出，全程不摘。",
    en: "The green badge (top right) = actual engine output, never removed.",
  },
  /** 产品页 → 品牌片章节深链（/film?t=…）：每条产品线指到片中对应的那一幕 */
  productChapter: {
    voice: { t: { zh: 93.5, en: 130.1 }, label: { zh: "声音克隆 · 还能唱", en: "Voice cloning · it sings" } },
    face: { t: { zh: 54.2, en: 75 }, label: { zh: "换脸直播", en: "Face-swap live" } },
    interpreting: { t: { zh: 22.9, en: 32.8 }, label: { zh: "同传 · 真实通话实拍", en: "Interpreting · real call footage" } },
  } as Record<string, { t: { zh: number; en: number }; label: { zh: string; en: string } }>,
  chapters: [
    { label: { zh: "开场 · 合成主持人", en: "Opening · synthetic host" }, t: { zh: 1.5, en: 1.5 } },
    { label: { zh: "宣言", en: "Manifesto" }, t: { zh: 9.9, en: 12.9 } },
    { label: { zh: "同传 · 真实通话实拍", en: "Interpreting · real call footage" }, t: { zh: 22.9, en: 32.8 } },
    { label: { zh: "换脸直播", en: "Face-swap live" }, t: { zh: 54.2, en: 75 } },
    { label: { zh: "照片开口 · 多语成片", en: "Talking photo · multilingual" }, t: { zh: 76.7, en: 108.4 } },
    { label: { zh: "声音克隆 · 还能唱", en: "Voice cloning · it sings" }, t: { zh: 93.5, en: 130.1 } },
    { label: { zh: "收尾 · 3 天试用", en: "Wrap-up · 3-day trial" }, t: { zh: 131.4, en: 171.3 } },
  ],
} as const;
