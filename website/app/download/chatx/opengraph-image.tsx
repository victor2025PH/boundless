import { downloadOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "下载智聊 ChatX 客户端 · 六平台统一 AI 收件箱 · 无界科技 BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return downloadOgImage("zh");
}
