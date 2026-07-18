import IntroCover from "@/components/IntroCover";
import SectionDivider from "@/components/fx/SectionDivider";
import Navbar from "@/components/Navbar";
import SectionNav from "@/components/SectionNav";
import Hero from "@/components/Hero";
import BrandShowcase from "@/components/BrandShowcase";
import AISprite from "@/components/AISprite";
import DragonQuest from "@/components/DragonQuest";
import TrustBar from "@/components/TrustBar";
import ProductMatrix from "@/components/ProductMatrix";
import Personas from "@/components/Personas";
import Compare from "@/components/Compare";
import AutoChat from "@/components/AutoChat";
import TranslateDemo from "@/components/TranslateDemo";
import RealtimeSwap from "@/components/RealtimeSwap";
import EngineCapabilities from "@/components/EngineCapabilities";
import Showcase from "@/components/Showcase";
import Cases from "@/components/Cases";
import RealProof from "@/components/RealProof";
import EngagementModels from "@/components/EngagementModels";
import Pricing from "@/components/Pricing";
import ClientAppCTA from "@/components/ClientAppCTA";
import OrderSteps from "@/components/OrderSteps";
import About from "@/components/About";
import Faq from "@/components/Faq";
import Community from "@/components/Community";
import UnlockGate from "@/components/UnlockGate";
import Contact from "@/components/Contact";
import Footer from "@/components/Footer";

/** Shared marketing homepage tree, rendered at both `/` (zh) and `/en` (en).
 *  Locale is driven by the route via LanguageProvider, so this stays presentational. */
export default function SiteHome() {
  return (
    <main className="relative min-h-screen">
      <IntroCover />
      <AISprite />
      {/* 龙珠彩蛋：每日到访集星珠，七星聚召唤界龙（与 AISprite 通过 bl:apply-skin 事件解耦） */}
      <DragonQuest />
      <Navbar />
      <SectionNav />
      {/* 品牌块(公司+三系七图标) 在上，营销主文案区(大标题+按钮) 在下 */}
      <BrandShowcase />
      <Hero />
      <TrustBar />
      {/* 光弧分隔线:只放在五个主要叙事转折处,保持稀缺感 */}
      <SectionDivider />
      <ProductMatrix />
      <Personas />
      <Compare />
      <AutoChat />
      <TranslateDemo />
      <RealtimeSwap />
      <SectionDivider />
      <EngineCapabilities />
      <Showcase />
      <SectionDivider />
      <Cases />
      <RealProof />
      <EngagementModels />
      <SectionDivider />
      <Pricing />
      <ClientAppCTA />
      <OrderSteps />
      <About />
      <Faq />
      <Community />
      <UnlockGate />
      <SectionDivider />
      <Contact />
      <Footer />
    </main>
  );
}
