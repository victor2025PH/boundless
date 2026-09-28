/** AI 回复里的裸链接 → 单独渲染成按钮（小程序 / 官网 AiChat 共用）。 */
export type ChatLinkAction = { label: string; onClick: () => void };

const URL_SRC = "https?:\\/\\/[^\\s<>()（）「」“”\"'，。；！？]+";
const URL_RE = new RegExp(URL_SRC, "g");
// 「下载安装：URL」这类短引导标签（≤14 字、位于句段开头）连同链接一起去掉——按钮本身就是标签；长句只去链接保留文字
// （不用 lookbehind：旧 iOS WebView 不支持会在模块加载时直接报错）
const LABEL_URL_RE = new RegExp(`(^|[。！？.!?\\n,，;；])([^。！？.!?\\n,，;；]{0,14}?)\\s*[：:]?\\s*(${URL_SRC})`, "gm");

function collect(urls: string[], raw: string) {
  const url = raw.replace(/[.,:;!?]+$/, "");
  if (!urls.includes(url)) urls.push(url);
}

/** 把助手回复里的裸 URL 抽出来：正文去掉链接（及其短引导标签），链接去重后单独渲染成按钮 */
export function splitChatLinks(content: string): { text: string; urls: string[] } {
  const urls: string[] = [];
  const text = content
    .replace(LABEL_URL_RE, (_m, lead: string, _label: string, raw: string) => {
      collect(urls, raw);
      return lead;
    })
    .replace(URL_RE, (raw) => {
      collect(urls, raw);
      return "";
    })
    .replace(/[：:]\s*(?=[。！？.!?,，;；]|\n|$)/gm, "")
    .replace(/([。！？，；])\1+/g, "$1")
    .replace(/^[\s。！？.!?,，;；]+/gm, "")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{2,}/g, "\n")
    .trim();
  return { text, urls };
}
