/**
 * npx tsx lib/compute-host-tag.test.ts
 */
import assert from "assert";
import { hostTag } from "./compute-host-tag";

assert.equal(hostTag("http://192.168.0.198:8765", "@176"), "@198:8765");
assert.equal(hostTag("http://192.168.0.104:7865", "@104"), "@104:7865");
assert.equal(hostTag(undefined, "@176"), "@176");
assert.equal(hostTag("", "@104"), "@104");
console.log("compute-host-tag.test.ts ok");
