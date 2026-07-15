"use client";

import { usePathname } from "next/navigation";
import TechBackground from "./fx/TechBackground";
import ParticleField from "./fx/ParticleField";
import CalmZones from "./fx/CalmZones";
import VideoSpotlight from "./fx/VideoSpotlight";
import WarpNav from "./fx/WarpNav";
import RumProbe from "./fx/RumProbe";
import ThemeLoader from "./fx/ThemeLoader";
import Spotlight from "./fx/Spotlight";
import ScrollProgress from "./fx/ScrollProgress";
import BackToTop from "./fx/BackToTop";
import StickyCTA from "./StickyCTA";
import AIChat from "./AIChat";
import MiniAppBridge from "./MiniAppBridge";
import Analytics from "./Analytics";
import CookieConsent from "./CookieConsent";

export default function GlobalChrome() {
  const pathname = usePathname();
  if (pathname?.startsWith("/app")) return <Analytics />;
  /* IP 素材舞台页：纯净画布，不挂任何全站装饰/客服/统计 */
  if (pathname?.startsWith("/robot-stage")) return null;
  return (
    <>
      <TechBackground />
      <ParticleField />
      <CalmZones />
      <VideoSpotlight />
      <WarpNav />
      <RumProbe />
      <ThemeLoader />
      <Spotlight />
      <ScrollProgress />
      <BackToTop />
      <StickyCTA />
      <AIChat />
      <MiniAppBridge />
      <CookieConsent />
      <Analytics />
    </>
  );
}
