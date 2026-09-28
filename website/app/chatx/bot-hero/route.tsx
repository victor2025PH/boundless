import { NextRequest } from "next/server";
import { chatxBotHeroImage } from "@/lib/ogTemplate";

export const runtime = "edge";

/** @ChatX_bot /start 首条海报（PNG 1280×720），?lang=en 出英文版；bot 侧 sendPhoto by URL。
 *  底图 prod-chatx.jpg 构建期内联为 data URL（同根 opengraph-image 的做法，不走网络）。 */
const bgPromise = fetch(new URL("../../../public/products/prod-chatx.jpg", import.meta.url))
  .then((res) => res.arrayBuffer())
  .catch(() => null);

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

export async function GET(req: NextRequest) {
  const lang = req.nextUrl.searchParams.get("lang") === "en" ? "en" : "zh";
  const buf = await bgPromise;
  const bg = buf ? `data:image/jpeg;base64,${toBase64(buf)}` : "";
  const res = chatxBotHeroImage(lang, bg);
  res.headers.set("Cache-Control", "public, max-age=86400, s-maxage=86400");
  return res;
}
