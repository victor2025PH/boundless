import ProductLanding from "@/components/ProductLanding";
import { JA_VOICE, JA_VOICE_UI, jaVoiceMetadata, jaVoiceJsonLd, jaVoiceFaqJsonLd } from "@/lib/jaVoice";

export const metadata = jaVoiceMetadata();

export default function VoiceLandingJa() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jaVoiceJsonLd()) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jaVoiceFaqJsonLd()) }}
      />
      <ProductLanding product="voice" content={JA_VOICE} ui={JA_VOICE_UI} />
    </>
  );
}
