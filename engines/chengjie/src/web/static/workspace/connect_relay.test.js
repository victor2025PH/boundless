/* connect_relay 视图模型门禁（node --test，零依赖 CJS）。
 *
 * 钉住：① 各 /relay-step 响应 → 正确渲染模式；② 字段规格与后端 login_relay 契约对齐
 * （credentials=email+password / twofactor=code / e2ee_pin=pin）；③ 终态与回落优先级；
 * ④ submitPayload 只挑合法字段。视图模型是前后端之间的前端侧契约，漂移即前端错渲。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const { relayFormView, submitPayload, specFor, buildFormHtml } = require('./connect_relay.js');

test('已授权 / done → success（终态优先，即便还带着 step）', () => {
  assert.equal(relayFormView({ status: 'authorized', step: 'credentials' }).mode, 'success');
  assert.equal(relayFormView({ step: 'done' }).mode, 'success');
});

test('expired → expired 终态', () => {
  assert.equal(relayFormView({ status: 'expired' }).mode, 'expired');
});

test('booting / wait / 空 → preparing（有截图则安抚显示）', () => {
  assert.equal(relayFormView({ booting: true, qr_image: 'data:x' }).mode, 'preparing');
  const w = relayFormView({ step: 'wait', qr_image: 'data:x' });
  assert.equal(w.mode, 'preparing');
  assert.equal(w.showScreenshot, true);
  assert.equal(relayFormView({}).mode, 'preparing');
});

test('checkpoint / escalate → 截图回落 + checkpoint 提示码（应用内驱不动）', () => {
  const c = relayFormView({ step: 'checkpoint', escalate: true, qr_image: 'data:x' });
  assert.equal(c.mode, 'screenshot');
  assert.equal(c.noticeCode, 'checkpoint');
  assert.equal(c.showScreenshot, true);
});

test('credentials → form：email+password 字段规格 + 提交步 + 引导键', () => {
  const v = relayFormView({ step: 'credentials', fields: ['email', 'password'] });
  assert.equal(v.mode, 'form');
  assert.equal(v.submitStep, 'credentials');
  assert.deepEqual(v.fields.map((f) => f.name), ['email', 'password']);
  assert.equal(v.fields[0].autocomplete, 'username');
  assert.equal(v.fields[1].type, 'password');
  assert.equal(v.hintKey, 'inbox.connect.relay_hint_credentials');
  assert.equal(v.errorKey, '');
});

test('credentials + error → 账密表单带密码错误横幅（就地重填，不另起步）', () => {
  const v = relayFormView({ step: 'credentials', fields: ['email', 'password'], error: true, code: 'password_error' });
  assert.equal(v.mode, 'form');
  assert.equal(v.errorKey, 'inbox.connect.relay_err_password');
  assert.equal(v.noticeCode, 'password_error');
});

test('twofactor → 单 code 字段（numeric + one-time-code）', () => {
  const v = relayFormView({ step: 'twofactor', fields: ['code'] });
  assert.equal(v.mode, 'form');
  assert.deepEqual(v.fields.map((f) => f.name), ['code']);
  assert.equal(v.fields[0].inputmode, 'numeric');
  assert.equal(v.fields[0].autocomplete, 'one-time-code');
  assert.equal(v.hintKey, 'inbox.connect.relay_hint_twofactor');
});

test('e2ee_pin → 单 pin 字段（password 型 numeric）', () => {
  const v = relayFormView({ step: 'e2ee_pin', fields: ['pin'] });
  assert.deepEqual(v.fields.map((f) => f.name), ['pin']);
  assert.equal(v.fields[0].type, 'password');
});

test('fields 缺省时按 step 兜底（后端未下发 fields 也能渲对表单）', () => {
  assert.deepEqual(relayFormView({ step: 'credentials' }).fields.map((f) => f.name), ['email', 'password']);
  assert.deepEqual(relayFormView({ step: 'twofactor' }).fields.map((f) => f.name), ['code']);
});

test('specFor：返回副本 + 未知字段安全兜底（不崩）', () => {
  const a = specFor('email');
  a.type = 'hacked';
  assert.equal(specFor('email').type, 'text');
  const u = specFor('mystery');
  assert.equal(u.type, 'text');
  assert.equal(u.name, 'mystery');
});

test('submitPayload：只挑该步合法字段、缺字段留空串', () => {
  assert.deepEqual(
    submitPayload('credentials', { email: ' a@b.com ', password: 'pw', junk: 'x' }),
    { step: 'credentials', values: { email: ' a@b.com ', password: 'pw' } });
  assert.deepEqual(submitPayload('twofactor', {}),
    { step: 'twofactor', values: { code: '' } });
});

test('脏输入不抛异常（前端渲染绝不能因脏响应崩掉整个向导）', () => {
  assert.doesNotThrow(() => relayFormView(null));
  assert.doesNotThrow(() => relayFormView(42));
  assert.doesNotThrow(() => submitPayload('credentials', null));
  assert.equal(relayFormView(null).mode, 'preparing');
});

// ── buildFormHtml：视图规格 → HTML 字符串（纯，可测；render 薄壳由浏览器门禁验证）──
const _t = (k) => 'T:' + k;  // 可辨识的 i18n stub

test('buildFormHtml credentials：email/password 输入 + 属性 + 提交钮 + data 钩子', () => {
  const html = buildFormHtml(relayFormView({ step: 'credentials', fields: ['email', 'password'] }), { t: _t });
  assert.match(html, /data-relay-mode="form"/);
  assert.match(html, /data-relay-step="credentials"/);
  assert.match(html, /data-relay-field="email"/);
  assert.match(html, /data-relay-field="password"/);
  assert.match(html, /type="password"/);
  assert.match(html, /autocomplete="username"/);
  assert.match(html, /data-relay-submit/);
  assert.match(html, /T:inbox\.connect\.relay_submit/);
});

test('buildFormHtml credentials+error：出错误横幅（role=alert）', () => {
  const html = buildFormHtml(relayFormView({ step: 'credentials', error: true }), { t: _t });
  assert.match(html, /connect-relay-error/);
  assert.match(html, /role="alert"/);
  assert.match(html, /T:inbox\.connect\.relay_err_password/);
});

test('buildFormHtml twofactor：单 code 输入（numeric/one-time-code）', () => {
  const html = buildFormHtml(relayFormView({ step: 'twofactor' }), { t: _t });
  assert.match(html, /data-relay-field="code"/);
  assert.match(html, /inputmode="numeric"/);
  assert.match(html, /autocomplete="one-time-code"/);
  assert.doesNotMatch(html, /data-relay-field="email"/);
});

test('buildFormHtml preparing：spinner + 截图（有 qr_image 时）', () => {
  const html = buildFormHtml(relayFormView({ booting: true, qr_image: 'data:image/png;base64,AA' }), { t: _t });
  assert.match(html, /data-relay-mode="preparing"/);
  assert.match(html, /connect-relay-spin/);
  assert.match(html, /connect-relay-shot/);
});

test('buildFormHtml screenshot(checkpoint)：截图 + checkpoint 提示', () => {
  const html = buildFormHtml(relayFormView({ step: 'checkpoint', escalate: true, qr_image: 'data:x' }), { t: _t });
  assert.match(html, /data-relay-mode="screenshot"/);
  assert.match(html, /T:inbox\.connect\.relay_checkpoint/);
});

test('buildFormHtml success/expired：终态消息', () => {
  assert.match(buildFormHtml(relayFormView({ status: 'authorized' }), { t: _t }), /data-relay-mode="success"/);
  assert.match(buildFormHtml(relayFormView({ status: 'expired' }), { t: _t }), /data-relay-mode="expired"/);
});

test('buildFormHtml：属性值消毒（防属性逃逸；无 t 时回落键名）', () => {
  // qr_image 带引号必须被转义，不能逃出 src 属性
  const html = buildFormHtml({ mode: 'screenshot', qrImage: 'data:x" onerror="alert(1)', hintKey: 'k' });
  assert.doesNotMatch(html, /onerror="alert/);
  assert.match(html, /&quot;/);
  // 无 t：回落键名（不崩）
  assert.match(buildFormHtml(relayFormView({ step: 'twofactor' })), /inbox\.connect\.relay_code_label/);
});

test('buildFormHtml：脏输入不抛（渲染绝不能崩向导）', () => {
  assert.doesNotThrow(() => buildFormHtml(null));
  assert.doesNotThrow(() => buildFormHtml(42, { t: _t }));
  assert.match(buildFormHtml(null), /data-relay-mode="preparing"/);
});
