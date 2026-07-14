import type { Metadata } from "next";
import type { LandingDict } from "./landingContent";
import type { LandingUi } from "@/components/ProductLanding";
import { SITE_URL } from "./site";
import { landingLanguages } from "./seo";

/** 日本語版音声クローンLP（/ja/voice）自包含内容包。
 *  与 koVoice.ts 同构：LandingDict 双槽位放日语文案，LandingUi 覆盖界面固定文案。 */

const j = (s: string) => ({ zh: s, en: s });

export const JA_VOICE: LandingDict = {
  slug: "/ja/voice",
  productLine: j("VoiceX · AI音声クローン"),
  seo: {
    title: j("AI音声クローン · 数十秒のサンプルで声を複製 | VoiceX — BOUNDLESS"),
    description: j(
      "数十秒の参照音声だけでゼロショット音声クローン：3エンジン自動選択、10言語を同じ声で、自然な感情表現、商用グレード48kHz。オンプレミス導入で音声データは社外に出ず、出力にはC2PA検証ウォーターマークを標準搭載。"
    ),
    keywords: ["AI音声クローン", "ボイスクローン", "音声合成", "AI吹き替え", "TTS", "多言語ナレーション", "voice cloning"],
  },
  hero: {
    title: j("数十秒の音声サンプルで、"),
    accent: j("あなたと同じ声を複製"),
    subtitle: j(
      "3つのエンジン（Fish / Qwen3 / VoxCPM）がジョブごとに最適を自動選択：リアルタイム会話、超高速ファーストパケット、商用48kHz。10言語を同じ声で、感情やトーンまで自然に——すべてオンプレミス導入で、音声データは社外に出ません。"
    ),
    points: [
      j("Qwen3 ファーストパケット ≈97ms · 3秒クローン · リアルタイム会話級"),
      j("日本語 / 英語 / 中国語 / 韓国語など10言語 · 同じ声"),
      j("C2PA検証ウォーターマーク · クローン倫理チェック"),
    ],
  },
  demo: {
    title: j("まず、聴いてください"),
    subtitle: j("同じクローンボイスが4つの言語を読み上げます——タップで再生。"),
    realNote: j(
      "上記はエンジンが実際に生成した未編集の出力です。ご自身の声で聴きたい方は、10〜40秒のサンプルをお送りください。その場でクローンしてお聴かせします。"
    ),
  },
  caps: [
    {
      title: j("3エンジンクローン · 自動選択"),
      desc: j(
        "数十秒のサンプルでゼロショットクローン。Fishはリアルタイム / Qwen3は超高速10言語 / VoxCPMは商用48kHz——クローン完了後、用途に合うエンジンを自動推薦します。"
      ),
      proof: j("Qwen3 ファーストパケット ≈97ms · 3秒クローン · 10言語"),
    },
    {
      title: j("人間らしい感情とトーン"),
      desc: j(
        "感情タグ＋自然言語スタイル指示の2モード：喜び、癒やし、興奮、ささやきを自在に切り替え——長文の読み上げでもトーンが崩れません。"
      ),
      proof: j("感情エンジン + 指示モード · 長文でも一貫"),
    },
    {
      title: j("ライブ配信 · 電話 · AI会話ブレインに接続"),
      desc: j(
        "クローンボイスがデジタルヒューマン配信、電話ブリッジ、AI会話ブレインへ直結——記憶力と感情認識を備え、本物の人間のように応対します。"
      ),
      proof: j("配信 / 電話 / チャット 単一パイプライン"),
    },
    {
      title: j("コンプライアンス · ワンクリック検証"),
      desc: j(
        "出力にはC2PAコンテンツクレデンシャル + Ed25519署名 + 不可視ウォーターマークを標準搭載し、第三者がオフラインで検証可能。無許可の声はクローン自体を拒否します。"
      ),
      proof: j("C2PA + Ed25519 · クローン倫理チェック"),
    },
  ],
  steps: [
    {
      title: j("10〜40秒のクリアな音声を送る"),
      desc: j("スマホ録音で十分です。クリアなほど似ます。複数クリップの融合で類似度をさらに向上できます。"),
    },
    {
      title: j("エンジンがクローンし自動選択"),
      desc: j("約3秒でクローン完了。利用シーンに合わせて最適な合成エンジンを自動推薦します。"),
    },
    {
      title: j("テキストを入力するだけ、どこでも"),
      desc: j("吹き替え、ライブ配信、電話サポート、多言語コンテンツ——同じ声をあらゆる場面で再利用。"),
    },
  ],
  faq: [
    {
      q: j("音声サンプルはどのくらい必要ですか？"),
      a: j("10〜40秒のクリアな音声で十分です。サンプルがクリアなほど類似度が上がり、複数クリップの融合でさらに向上します。"),
    },
    {
      q: j("対応言語は？日本語でクローンした声が英語も話せますか？"),
      a: j(
        "日本語、英語、中国語、韓国語など10言語に対応し、同じ声が言語をまたぎます——日本語でクローンすれば、その声のまま英語や中国語を話します。"
      ),
    },
    {
      q: j("音声データは安全ですか？"),
      a: j(
        "すべてオンプレミス導入で、サンプルも出力も社内の機材から外に出ません。出力にはC2PA検証ウォーターマークが標準搭載され、無許可の声はエンジンがクローンを拒否します。"
      ),
    },
  ],
  finalCta: {
    title: j("あなたの声を、その場でクローンしてお聴かせします"),
    desc: j(
      "30分のライブデモ：リモートで御社の機材に接続、または弊社のデモ機で実施。サンプルをお送りいただければその場でクローンし、多言語で読み上げてご確認いただけます。"
    ),
  },
};

export const JA_VOICE_UI: LandingUi = {
  homeLabel: "ホーム",
  homeHref: "/en",
  chatNow: "今すぐ相談",
  bookDemo: "ライブデモを予約",
  seeSamples: "まず実サンプルを聴く",
  trustLine: "オンプレミス導入 · データ社外流出なし · USDT決済 · 検証可能な出力",
  capsTitle: "コア性能 · すべてその場で検証可能",
  stepsTitle: "3ステップで開始",
  faqTitle: "よくある質問",
  moreFaqLabel: "その他の質問 → ホームページの全FAQ",
  moreFaqHref: "/en#faq",
  tgCta: "Telegramで1対1相談",
  pricingLabel: "料金プランを見る",
  pricingHref: "/en#pricing",
  langLabel: "EN",
  langHref: "/en/voice",
  clipLabels: ["中国語", "英語", "日本語", "韓国語"],
};

export function jaVoiceMetadata(): Metadata {
  return {
    title: JA_VOICE.seo.title.zh,
    description: JA_VOICE.seo.description.zh,
    keywords: JA_VOICE.seo.keywords,
    alternates: { canonical: "/ja/voice", languages: landingLanguages("/voice") },
    openGraph: {
      type: "website",
      url: "/ja/voice",
      title: JA_VOICE.seo.title.zh,
      description: JA_VOICE.seo.description.zh,
      siteName: "BOUNDLESS",
      locale: "ja_JP",
    },
    twitter: {
      card: "summary_large_image",
      title: JA_VOICE.seo.title.zh,
      description: JA_VOICE.seo.description.zh,
    },
  };
}

export function jaVoiceJsonLd() {
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: JA_VOICE.seo.title.zh,
    serviceType: JA_VOICE.productLine.zh,
    description: JA_VOICE.seo.description.zh,
    provider: { "@type": "Organization", name: "BOUNDLESS", url: SITE_URL },
    areaServed: "JP",
    inLanguage: "ja",
    url: `${SITE_URL}/ja/voice`,
  };
}

export function jaVoiceFaqJsonLd() {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    inLanguage: "ja",
    mainEntity: JA_VOICE.faq.map((f) => ({
      "@type": "Question",
      name: f.q.zh,
      acceptedAnswer: { "@type": "Answer", text: f.a.zh },
    })),
  };
}
