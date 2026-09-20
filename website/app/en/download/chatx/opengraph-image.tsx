import { downloadOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "Download ChatX · one AI inbox for six chat platforms · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return downloadOgImage("en");
}
