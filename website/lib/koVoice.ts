import type { Metadata } from "next";
import type { LandingDict } from "./landingContent";
import type { LandingUi } from "@/components/ProductLanding";
import { SITE_URL } from "./site";
import { landingLanguages } from "./seo";

/** 韩语版语音克隆落地页（/ko/voice）自包含内容包。
 *  接入方式：LandingDict 的 zh/en 槽位都放韩语文案（SSR 与客户端任一语言态渲染结果一致，
 *  无水合错位），界面固定文案由 LandingUi 覆盖。不引入全站第三语言重构。 */

const k = (s: string) => ({ zh: s, en: s });

export const KO_VOICE: LandingDict = {
  slug: "/ko/voice",
  productLine: k("VoiceX · AI 음성 클로닝"),
  seo: {
    title: k("AI 음성 클로닝 · 몇십 초 샘플로 목소리 복제 | VoiceX — BOUNDLESS"),
    description: k(
      "몇십 초의 참조 음성만으로 제로샷 음성 클로닝: 3개 엔진 자동 선택, 10개 언어를 하나의 목소리로, 자연스러운 감정 표현, 상업용 48kHz. 온프레미스 배포로 음성 데이터가 외부로 나가지 않으며, 산출물에는 C2PA 검증 워터마크가 포함됩니다."
    ),
    keywords: ["AI 음성 클로닝", "음성 복제", "보이스 클로닝", "AI 더빙", "TTS", "음성 합성", "다국어 더빙", "voice cloning"],
  },
  hero: {
    title: k("몇십 초의 음성 샘플로,"),
    accent: k("똑같은 목소리를 복제합니다"),
    subtitle: k(
      "3개 엔진(Fish / Qwen3 / VoxCPM)이 작업마다 자동으로 최적을 선택합니다: 실시간 대화, 초고속 첫 패킷, 상업용 48kHz. 10개 언어를 같은 목소리로, 감정과 어조까지 자연스럽게 — 전부 온프레미스로 배포되어 음성 데이터가 회사 밖으로 나가지 않습니다."
    ),
    points: [
      k("Qwen3 첫 패킷 ≈97ms · 3초 클로닝 · 실시간 대화급"),
      k("한국어 / 영어 / 중국어 / 일본어 등 10개 언어 · 같은 목소리"),
      k("C2PA 검증 워터마크 · 클로닝 윤리 검증"),
    ],
  },
  demo: {
    title: k("먼저 들어보세요"),
    subtitle: k("같은 클론 목소리가 4개 언어를 읽습니다 — 탭해서 재생하세요."),
    realNote: k(
      "위 샘플은 엔진이 실제로 생성한 무편집 결과물입니다. 본인 목소리로 듣고 싶다면 10~40초 샘플을 보내주세요. 그 자리에서 클로닝해 들려드립니다."
    ),
  },
  caps: [
    {
      title: k("3개 엔진 클로닝 · 자동 선택"),
      desc: k(
        "몇십 초 샘플로 제로샷 클로닝. Fish 실시간 / Qwen3 초고속 10개 언어 / VoxCPM 상업용 48kHz — 클로닝이 끝나면 용도에 맞는 엔진을 자동 추천합니다."
      ),
      proof: k("Qwen3 첫 패킷 ≈97ms · 3초 클로닝 · 10개 언어"),
    },
    {
      title: k("사람처럼 말하는 감정과 어조"),
      desc: k(
        "감정 태그 + 자연어 스타일 지시 두 가지 모드: 기쁨, 위로, 흥분, 속삭임을 원하는 대로 — 긴 글을 읽어도 어조가 끊기지 않습니다."
      ),
      proof: k("감정 엔진 + 지시 모드 · 긴 글 어조 일관"),
    },
    {
      title: k("라이브 방송 · 전화 · AI 대화 두뇌 연결"),
      desc: k(
        "클론 음성이 디지털 휴먼 방송, 전화 브리지, AI 대화 두뇌로 바로 연결됩니다 — 기억력과 감정 인식을 갖춰 진짜 사람처럼 응대합니다."
      ),
      proof: k("방송 / 전화 / 채팅 단일 파이프라인"),
    },
    {
      title: k("규정 준수 · 원클릭 검증"),
      desc: k(
        "산출물에 C2PA 콘텐츠 자격 증명 + Ed25519 서명 + 비가시 워터마크가 기본 포함되어 제3자가 오프라인으로 검증할 수 있습니다. 승인되지 않은 목소리는 클로닝 자체를 거부합니다."
      ),
      proof: k("C2PA + Ed25519 · 클로닝 윤리 검증"),
    },
  ],
  steps: [
    {
      title: k("10~40초의 깨끗한 음성을 보내주세요"),
      desc: k("휴대폰 녹음이면 충분합니다. 깨끗할수록 더 비슷해지고, 여러 클립을 융합해 유사도를 높일 수 있습니다."),
    },
    {
      title: k("엔진이 클로닝하고 자동 선택"),
      desc: k("약 3초면 클로닝이 끝나고, 사용 시나리오에 맞는 합성 엔진을 자동으로 추천합니다."),
    },
    {
      title: k("텍스트만 입력하면 어디서든"),
      desc: k("더빙, 라이브 방송, 전화 상담, 다국어 콘텐츠 — 같은 목소리를 모든 곳에서 재사용하세요."),
    },
  ],
  faq: [
    {
      q: k("음성 샘플은 얼마나 필요한가요?"),
      a: k("10~40초의 깨끗한 음성이면 충분합니다. 샘플이 깨끗할수록 유사도가 높아지고, 여러 클립을 융합하면 더 끌어올릴 수 있습니다."),
    },
    {
      q: k("어떤 언어를 지원하나요? 한국어로 클로닝한 목소리가 영어도 하나요?"),
      a: k(
        "한국어, 영어, 중국어, 일본어 등 10개 언어를 지원하며, 같은 목소리가 언어를 넘나듭니다 — 한국어로 클로닝하면 그 목소리 그대로 영어와 일본어를 말합니다."
      ),
    },
    {
      q: k("음성 데이터는 안전한가요?"),
      a: k(
        "전부 온프레미스로 배포되어 샘플과 산출물이 회사 장비 밖으로 나가지 않습니다. 산출물에는 C2PA 검증 워터마크가 기본 포함되며, 승인되지 않은 목소리는 엔진이 클로닝을 거부합니다."
      ),
    },
  ],
  finalCta: {
    title: k("당신의 목소리, 그 자리에서 클로닝해 들려드립니다"),
    desc: k(
      "30분 라이브 데모: 원격으로 고객 장비에 연결하거나 저희 데모 장비로 진행합니다. 샘플을 보내주시면 그 자리에서 클로닝하고, 여러 언어로 읽어 직접 확인시켜 드립니다."
    ),
  },
};

/** 界面固定文案（默认版里是 lang 三元表达式的那些字符串）。 */
export const KO_VOICE_UI: LandingUi = {
  homeLabel: "홈",
  homeHref: "/en",
  chatNow: "지금 상담하기",
  bookDemo: "라이브 데모 예약",
  seeSamples: "실제 샘플 먼저 듣기",
  trustLine: "온프레미스 배포 · 데이터 외부 유출 없음 · USDT 결제 · 검증 가능한 산출물",
  capsTitle: "핵심 성능 · 모두 현장에서 검증 가능",
  stepsTitle: "3단계면 시작",
  faqTitle: "자주 묻는 질문",
  moreFaqLabel: "더 많은 질문 → 홈페이지 전체 FAQ",
  moreFaqHref: "/en#faq",
  tgCta: "Telegram 1:1 상담",
  pricingLabel: "요금제 보기",
  pricingHref: "/en#pricing",
  langLabel: "EN",
  langHref: "/en/voice",
  clipLabels: ["중국어", "영어", "일본어", "한국어"],
};

export function koVoiceMetadata(): Metadata {
  return {
    title: KO_VOICE.seo.title.zh,
    description: KO_VOICE.seo.description.zh,
    keywords: KO_VOICE.seo.keywords,
    alternates: { canonical: "/ko/voice", languages: landingLanguages("/voice") },
    openGraph: {
      type: "website",
      url: "/ko/voice",
      title: KO_VOICE.seo.title.zh,
      description: KO_VOICE.seo.description.zh,
      siteName: "BOUNDLESS",
      locale: "ko_KR",
    },
    twitter: {
      card: "summary_large_image",
      title: KO_VOICE.seo.title.zh,
      description: KO_VOICE.seo.description.zh,
    },
  };
}

export function koVoiceJsonLd() {
  return {
    "@context": "https://schema.org",
    "@type": "Service",
    name: KO_VOICE.seo.title.zh,
    serviceType: KO_VOICE.productLine.zh,
    description: KO_VOICE.seo.description.zh,
    provider: { "@type": "Organization", name: "무계 테크놀로지 BOUNDLESS", url: SITE_URL },
    areaServed: "KR",
    inLanguage: "ko",
    url: `${SITE_URL}/ko/voice`,
  };
}

export function koVoiceFaqJsonLd() {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    inLanguage: "ko",
    mainEntity: KO_VOICE.faq.map((f) => ({
      "@type": "Question",
      name: f.q.zh,
      acceptedAnswer: { "@type": "Answer", text: f.a.zh },
    })),
  };
}
