/**
 * npx tsx lib/guide-trial-ingress.test.ts
 */
import assert from "assert";
import { guideTrialIngress } from "./guide-trial-ingress";

assert.deepEqual(guideTrialIngress({ telegram: "@Bob_1", feature: "matrixx" }), {
  telegram: "Bob_1",
  feature: "matrixx",
});
assert.equal(guideTrialIngress({ telegram: "Bob", feature: "chatx" }), null);
assert.equal(guideTrialIngress({ telegram: "Bob", feature: "one" }), null);
assert.equal(guideTrialIngress({ telegram: "Bob", feature: "matrixx!" }), null);
assert.equal(guideTrialIngress({ telegram: "1bob", feature: "matrixx" }), null);
assert.equal(guideTrialIngress({ telegram: "ab", feature: "matrixx" }), null);
assert.equal(guideTrialIngress({ telegram: "", feature: "matrixx" }), null);
assert.equal(guideTrialIngress(null), null);

console.log("guide-trial-ingress ok");
