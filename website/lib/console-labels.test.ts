// /console 标签单源冒烟。node --experimental-strip-types lib/console-labels.test.ts
import assert from "node:assert/strict";
import {
  LEDGER_SOURCE_SYSTEMS,
  LICENSE_PLAN_LABEL,
  LICENSE_STATUS_LABEL,
  PRODUCT_LABEL,
  ROLE_LABEL,
  fingerprintLabel,
  lbl,
  licenseQuotaLabel,
  seatsLabel,
} from "../app/console/labels";

assert.equal(lbl(ROLE_LABEL, "master"), "主账号");
assert.equal(lbl(ROLE_LABEL, "admin"), "运营");
assert.equal(lbl(ROLE_LABEL, "viewer"), "只读");
assert.equal(lbl(LICENSE_STATUS_LABEL, "trial"), "试用");
assert.equal(lbl(LICENSE_PLAN_LABEL, "pro"), "专业版");
assert.equal(lbl(PRODUCT_LABEL, "zhiliao"), "智聊 ChatX");
assert.equal(lbl(PRODUCT_LABEL, null), "—");
assert.ok(!(LEDGER_SOURCE_SYSTEMS as readonly string[]).includes("huoke"));
assert.equal(seatsLabel(0, "chengjie"), "不限");
assert.equal(seatsLabel(0, "avatarhub"), "0");
assert.equal(seatsLabel(null), "—");
assert.equal(fingerprintLabel("*").text, "站点授权（不限机器）");
assert.equal(fingerprintLabel(null).text, "—");
assert.equal(
  licenseQuotaLabel(JSON.stringify({ payload: { included_chars: 1_000_000 } })).text,
  "1,000,000 字符"
);
assert.equal(licenseQuotaLabel("{}").text, "—");

console.log("console-labels.test.ts OK");
