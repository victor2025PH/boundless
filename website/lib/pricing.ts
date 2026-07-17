// Canonical price figures, single source of truth.
//
// These are the headline SKU numbers surfaced in structured data (schema.org
// JSON-LD in app/layout.tsx) and in headline marketing copy. The localized
// display strings in lib/content.ts (e.g. "980 起", "198 / 月") are presentation
// formats of these same numbers — when a price changes, update it HERE and keep
// the content.ts display strings in sync.

export type PriceUnit = "one-time" | "month";

export interface PriceOffer {
  id: string;
  name: string;
  /** Numeric string (no currency / suffix) so it is valid for schema.org Offer.price. */
  price: string;
  /** ISO 4217 for compliant SKUs (USD). USDT is retained only for legacy custom-deploy
   *  offers whose payment rail is still crypto; new SKUs should use USD. */
  currency: "USD" | "USDT";
  unit: PriceUnit;
  description: string;
}

/** Real-time face & voice swap — private deployment. */
export const realtimeOffers: PriceOffer[] = [
  {
    id: "realtime-basic",
    name: "Basic deployment",
    price: "980",
    currency: "USDT",
    unit: "one-time",
    description:
      "One-time; real-time face swap OR voice clone, remote deploy + tuning + training + support.",
  },
  {
    id: "realtime-creator",
    name: "Creator all-in deployment",
    price: "2580",
    currency: "USDT",
    unit: "one-time",
    description:
      "One-time; face swap + voice + digital human, multi-scenario deep tuning, 30-day support.",
  },
];

/** AI auto-closing chat system — subscription. */
export const autochatOffers: PriceOffer[] = [
  {
    id: "autochat-team",
    name: "Team",
    price: "198",
    currency: "USDT",
    unit: "month",
    description: "Per month; 10 chat accounts, all platforms, AI auto-closing replies.",
  },
  {
    id: "autochat-flagship",
    name: "Flagship",
    price: "598",
    currency: "USDT",
    unit: "month",
    description: "Per month; 50 accounts, human handoff, dashboard, persona voice.",
  },
];

/** Real-time cross-border translation SCRM (通译 LingoX) — flagship, low-risk cash flow.
 *  USD, self-serve. Differentiator vs. plain translation add-ons: term-lock glossary,
 *  translation memory, and customer-asset SCRM (unified inbox + journey + funnel). */
export const translateOffers: PriceOffer[] = [
  {
    id: "translate-charpack",
    name: "Char pack",
    price: "39",
    currency: "USD",
    unit: "one-time",
    description:
      "One-time; 1.5M translation chars, term-lock glossary + translation memory.",
  },
  {
    id: "translate-team",
    name: "Team",
    price: "59",
    currency: "USD",
    unit: "month",
    description:
      "Per month; multi-seat unified inbox, customer journey, conversion funnel counter.",
  },
  {
    id: "translate-pro",
    name: "Pro",
    price: "99",
    currency: "USD",
    unit: "month",
    description:
      "Per month; unlimited chars, multimodal (image/voice) translate, confidence badge + engine health.",
  },
];

/** Map a PriceOffer to a schema.org Offer node. */
export function toSchemaOffer(o: PriceOffer) {
  return {
    "@type": "Offer",
    name: o.name,
    price: o.price,
    priceCurrency: o.currency,
    description: o.description,
  };
}
