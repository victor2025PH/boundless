import { compareOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "ChatX vs SaleSmartly · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return compareOgImage("salesmartly", "en");
}
