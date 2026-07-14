import { landingOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "Real-time AI Interpreting · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return landingOgImage("interpreting", "en");
}
