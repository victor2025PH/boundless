/**
 * 第一次用量才回传。npx tsx lib/guide-trial-notify.test.ts
 */
import assert from "assert";
import {
  GUIDE_TRIAL_TUNNEL_URL,
  guideTrialBody,
  guideTrialTarget,
} from "./guide-trial-notify";

const used = {
  contact: "@Bob",
  contactKind: "telegram" as const,
  product: "matrixx",
  usedChars: 1200,
};

assert.deepEqual(guideTrialBody(used, 0), {
  telegram: "@Bob",
  feature: "matrixx",
});
assert.equal(guideTrialBody(used, 1200), null);
assert.equal(guideTrialBody({ ...used, usedChars: 0 }, 0), null);
assert.equal(guideTrialBody({ ...used, contactKind: "email" }, 0), null);
assert.equal(guideTrialBody({ ...used, product: "" }, 0), null);

assert.equal(guideTrialTarget({}), null);
assert.equal(guideTrialTarget({ GUIDE_TRIAL_SECRET: "   " }), null);
assert.deepEqual(guideTrialTarget({ GUIDE_TRIAL_SECRET: "same-as-engine" }), {
  url: GUIDE_TRIAL_TUNNEL_URL,
  secret: "same-as-engine",
});
assert.deepEqual(
  guideTrialTarget({
    GUIDE_TRIAL_SECRET: "same-as-engine",
    GUIDE_TRIAL_URL: "http://127.0.0.1:18799/api/cta/convert",
  }),
  {
    url: "http://127.0.0.1:18799/api/cta/convert",
    secret: "same-as-engine",
  },
);

console.log("guide-trial-notify ok");
