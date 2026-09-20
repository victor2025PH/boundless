import { compareOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "ChatX vs WATI · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return compareOgImage("wati", "zh");
}
