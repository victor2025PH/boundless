import { SITE_URL } from "./site";
import {
  CHATX_TUTORIALS,
  CHATX_TUTORIALS_OG,
  CHATX_TUTORIALS_PATH,
  CHATX_TUTORIALS_UPLOAD_DATE,
  isoDuration,
  type TutorialLang,
} from "./chatx-tutorials";

/** 教程合集结构化数据：ItemList → VideoObject × N（与页面可见列表同源）。
 *  措辞沿用 2026-07-26 合规收口：只描述客服工作台能力，不出现换脸类目词。 */
export function chatxTutorialsJsonLd(lang: TutorialLang) {
  const pagePath = lang === "en" ? `/en${CHATX_TUTORIALS_PATH}` : CHATX_TUTORIALS_PATH;
  const pageUrl = `${SITE_URL}${pagePath}`;
  return {
    "@context": "https://schema.org",
    "@type": "ItemList",
    name: lang === "zh" ? "智聊 ChatX 视频教程" : "ChatX video tutorials",
    url: pageUrl,
    numberOfItems: CHATX_TUTORIALS.length,
    itemListOrder: "https://schema.org/ItemListOrderAscending",
    itemListElement: CHATX_TUTORIALS.map((e, i) => ({
      "@type": "ListItem",
      position: i + 1,
      url: `${pageUrl}?ep=${e.id}`,
      item: {
        "@type": "VideoObject",
        name: `${e.id} · ${e.title[lang]}`,
        description: e.desc[lang],
        thumbnailUrl: `${SITE_URL}${e.poster}`,
        uploadDate: CHATX_TUTORIALS_UPLOAD_DATE,
        duration: isoDuration(e.durationSec),
        contentUrl: `${SITE_URL}${e.src}`,
        embedUrl: `${pageUrl}?ep=${e.id}`,
        // 说唱与字幕是中文；英文页仅界面英文
        inLanguage: "zh-CN",
        isFamilyFriendly: true,
        publisher: {
          "@type": "Organization",
          name: "BOUNDLESS 无界科技",
          url: SITE_URL,
        },
      },
    })),
    image: `${SITE_URL}${CHATX_TUTORIALS_OG}`,
  };
}
