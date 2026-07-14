import ProductLanding from "@/components/ProductLanding";
import { KO_VOICE, KO_VOICE_UI, koVoiceMetadata, koVoiceJsonLd, koVoiceFaqJsonLd } from "@/lib/koVoice";

export const metadata = koVoiceMetadata();

export default function VoiceLandingKo() {
  return (
    <>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(koVoiceJsonLd()) }}
      />
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(koVoiceFaqJsonLd()) }}
      />
      <ProductLanding product="voice" content={KO_VOICE} ui={KO_VOICE_UI} />
    </>
  );
}
