import { compareHubOgImage, OG_SIZE } from "@/lib/ogTemplate";

export const runtime = "edge";
export const alt = "How to choose an AI customer-chat tool (2026) · BOUNDLESS";
export const size = OG_SIZE;
export const contentType = "image/png";

export default function OgImage() {
  return compareHubOgImage("en");
}
