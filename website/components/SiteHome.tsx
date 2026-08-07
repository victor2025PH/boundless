import IntroCover from "@/components/IntroCover";
import SectionDivider from "@/components/fx/SectionDivider";
import Navbar from "@/components/Navbar";
import SectionNav from "@/components/SectionNav";
import Hero from "@/components/Hero";
import FilmSection from "@/components/FilmSection";
import BrandShowcase from "@/components/BrandShowcase";
import { LazyAISprite, LazyDragonQuest } from "@/components/fx/LazyFx";
import TrustBar from "@/components/TrustBar";
import SolutionPicker from "@/components/SolutionPicker";
import ProductMatrix from "@/components/ProductMatrix";
import AutoChat from "@/components/AutoChat";
import TranslateDemo from "@/components/TranslateDemo";
import RealProof from "@/components/RealProof";
import ClientAppCTA from "@/components/ClientAppCTA";
import Faq from "@/components/Faq";
import Contact from "@/components/Contact";
import Footer from "@/components/Footer";

/** Shared marketing homepage tree, rendered at both `/` (zh) and `/en` (en).
 *  Locale is driven by the route via LanguageProvider, so this stays presentational. */
export default function SiteHome() {
  return (
    <main className="relative min-h-screen">
      <IntroCover />
      {/* 两个纯装饰彩蛋走 LazyFx（dynamic ssr:false）懒加载，不占首屏关键路径。
          <768px 整体隐藏（hidden md:contents）：390px 屏上机器人+星珠徽章与聊天气泡/
          粘性条五层浮动互相叠压、遮挡菜单（2026-08-07 实录截图），彩蛋定位为桌面体验；
          md+ 用 display:contents 让包装层对布局零影响。 */}
      <div className="hidden md:contents">
        <LazyAISprite />
        {/* 龙珠彩蛋：每日到访集星珠，七星聚召唤界龙（与 AISprite 通过 bl:apply-skin 事件解耦） */}
        <LazyDragonQuest />
      </div>
      <Navbar />
      <SectionNav />
      {/* 品牌块(公司+三系七图标) 在上，营销主文案区(大标题+按钮) 在下 */}
      <BrandShowcase />
      <Hero />
      {/* 影院区：主张(Hero)之后立刻上证据(品牌片)，第二屏叙事位 */}
      <FilmSection />
      <TrustBar />
      {/* 光弧分隔线:只放在四个主要叙事转折处,保持稀缺感 */}
      <SectionDivider />
      {/* 30 秒选型器：两个问题直接给产品+套餐推荐 */}
      <SolutionPicker />
      <ProductMatrix />
      {/* AutoChat 内部已含 Plans（AI 成交聊天三档套餐 + 自助下单）与 ROI 计算器 */}
      <AutoChat />
      <TranslateDemo />
      <SectionDivider />
      <RealProof />
      {/* 「私有定制 · 一切皆可实现」报价大表已于 2026-08-04 下线：与产品卡 / AutoChat 套餐 /
          /order 下单页三处报价冲突，报价动线收敛为「产品卡一句话 → /order 自助下单」。 */}
      <SectionDivider />
      <ClientAppCTA />
      <Faq />
      <SectionDivider />
      <Contact />
      <Footer />
    </main>
  );
}
