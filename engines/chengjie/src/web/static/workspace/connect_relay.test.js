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

test('B65：占位/检查点按平台分键（LINE 弹窗不再露 Facebook 文案）', () => {
  const line = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] }, { platform: 'line' });
  assert.equal(line.fields[0].phKey, 'inbox.connect.relay_email_ph_line');
  const ms = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] }, { platform: 'messenger' });
  assert.equal(ms.fields[0].phKey, 'inbox.connect.relay_email_ph_messenger');
  // 未知平台走中性基键（绝不吐没有词条的裸后缀键）
  const other = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] }, { platform: 'telegram' });
  assert.equal(other.fields[0].phKey, 'inbox.connect.relay_email_ph');
  // 不带 platform 的旧调用 = 基键（向后兼容）
  const legacy = relayFormView({ step: 'credentials', fields: ['email', 'password'] });
  assert.equal(legacy.fields[0].phKey, 'inbox.connect.relay_email_ph');
  // 检查点提示同分键
  const ck = relayFormView({ step: 'checkpoint' }, { platform: 'line' });
  assert.equal(ck.hintKey, 'inbox.connect.relay_checkpoint_line');
  assert.equal(relayFormView({ step: 'checkpoint' }).hintKey,
    'inbox.connect.relay_checkpoint');
  // 密码等其它字段不受平台影响
  assert.equal(line.fields[1].phKey, 'inbox.connect.relay_password_ph');
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

// ── B64 续：提交回执化 + verifying 超时升级（2026-08-23 23:55「点登录没反应」实录）──
const NOW = 1_800_000_000_000;

test('verifying：提交已送达、step 未前进 → 官方式「正在验证」而非静默表单', () => {
  const v = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'verifying', ts: NOW - 3000, step: 'credentials' }, now: NOW });
  assert.equal(v.mode, 'verifying');
  assert.equal(v.headKey, 'inbox.connect.relay_verifying_title');
});

test('verify_stuck：verifying 超过升级阈值 → 截图 + 出路 + 重试（绝不无限转圈）', () => {
  const v = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'verifying', ts: NOW - 11000, step: 'credentials' },
      qrImage: 'data:img', now: NOW });
  assert.equal(v.mode, 'verify_stuck');
  assert.equal(v.showScreenshot, true);
  assert.equal(v.submitKey, 'inbox.connect.relay_retry');
  const html = buildFormHtml(v, { t: _t });
  assert.match(html, /data-relay-mode="verify_stuck"/);
  assert.match(html, /data-relay-retry/);
  assert.match(html, /data-relay-zoom/);
  assert.match(html, /T:inbox\.connect\.relay_stuck_ways/);
});

test('step 前进后本地相位失效：verifying(credentials) + 服务端已到 twofactor → 渲 twofactor 表单', () => {
  const v = relayFormView(
    { step: 'twofactor', fields: ['code'] },
    { local: { phase: 'verifying', ts: NOW - 3000, step: 'credentials' }, now: NOW });
  assert.equal(v.mode, 'form');
  assert.equal(v.submitStep, 'twofactor');
});

test('submitting：按钮忙态（disabled + 提交中文案），点击必有回响', () => {
  const v = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'submitting', ts: NOW, step: 'credentials' }, now: NOW });
  assert.equal(v.mode, 'form');
  assert.equal(v.submitBusy, true);
  const html = buildFormHtml(v, { t: _t });
  assert.match(html, /data-relay-submit disabled/);
  assert.match(html, /T:inbox\.connect\.relay_submitting/);
});

test('submit_failed / submit_missing：就地红字、输入保留（fire-and-forget 之死）', () => {
  const f = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'submit_failed', ts: NOW, step: 'credentials' }, now: NOW });
  assert.equal(f.mode, 'form');
  assert.equal(f.errorKey, 'inbox.connect.relay_err_network');
  const m = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'submit_missing', ts: NOW, step: 'credentials' }, now: NOW });
  assert.equal(m.errorKey, 'inbox.connect.relay_err_missing');
});

test('prefill：email 回填、密码绝不回填', () => {
  const v = relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'submit_failed', ts: NOW, step: 'credentials',
               prefill: { email: 'a@b.com', password: 'NEVER' } }, now: NOW });
  assert.deepEqual(v.prefill, { email: 'a@b.com', password: 'NEVER' });
  const html = buildFormHtml(v, { t: _t });
  assert.match(html, /value="a@b\.com"/);
  assert.doesNotMatch(html, /value="NEVER"/);
});

test('终态压过本地相位：authorized + verifying → success', () => {
  const v = relayFormView(
    { status: 'authorized' },
    { local: { phase: 'verifying', ts: NOW - 3000, step: 'credentials' }, now: NOW });
  assert.equal(v.mode, 'success');
});

test('官方式分步叙事：credentials 带步骤标题与「接下来」预告', () => {
  const v = relayFormView({ step: 'credentials', fields: ['email', 'password'] });
  assert.equal(v.headKey, 'inbox.connect.relay_head_credentials');
  assert.equal(v.nextKey, 'inbox.connect.relay_next_credentials');
  const html = buildFormHtml(v, { t: _t });
  assert.match(html, /T:inbox\.connect\.relay_head_credentials/);
  assert.match(html, /T:inbox\.connect\.relay_next_credentials/);
});

test('checkpoint 视图带出路清单（官方式「你现在可以怎么办」）', () => {
  const v = relayFormView({ step: 'checkpoint', qr_image: 'data:x' }, { platform: 'messenger' });
  assert.equal(v.nextKey, 'inbox.connect.relay_stuck_ways');
  assert.equal(v.headKey, 'inbox.connect.relay_stuck_title');
});

test('B64 检查点「继续」钮：checkpoint 出、account_locked/verify_stuck 不出', () => {
  // 手机确认后卡死的直接出路：替用户点离屏登录页的「继续/This was me」
  const ck = buildFormHtml(relayFormView({ step: 'checkpoint', qr_image: 'data:x' },
    { platform: 'messenger' }), { t: _t });
  assert.match(ck, /data-relay-continue/);
  assert.match(ck, /T:inbox\.connect\.relay_continue/);
  // account_locked 要等冷却，没有「继续」可点 → 不给该钮（免误导硬试）
  const lk = buildFormHtml(relayFormView({ step: 'checkpoint', escalate: true,
    code: 'account_locked', qr_image: 'data:x' }, { platform: 'messenger' }), { t: _t });
  assert.doesNotMatch(lk, /data-relay-continue/);
  // B64 三期收窄：verify_stuck 时服务端 step 仍在可填步，relay-submit 的 step 守卫
  // 必拒 checkpoint 提交——「继续」在此=必失败的假出口，只留「重试」。
  const vs = buildFormHtml(relayFormView(
    { step: 'credentials', fields: ['email', 'password'] },
    { local: { phase: 'verifying', ts: NOW - 11000, step: 'credentials' },
      qrImage: 'data:img', now: NOW }), { t: _t });
  assert.doesNotMatch(vs, /data-relay-continue/);
  assert.match(vs, /data-relay-retry/);
});

// ── B64 三期：继续回执分道 / device_confirm 手机主视觉 / 等待生命感 / 解卡正反馈 ──

test('B64三期 继续回执：continuing/continued → 忙态钮 + 跟进提示；超窗自动回常态', () => {
  const base = { step: 'checkpoint', escalate: true, qr_image: 'data:x' };
  // continuing：在途忙态（点击立即有回响，且忙态在整块重渲间由相位携带存活）
  const ing = relayFormView(base, { platform: 'messenger',
    local: { phase: 'continuing', ts: NOW, step: 'checkpoint' }, now: NOW });
  assert.equal(ing.contPhase, 'continuing');
  const ingHtml = buildFormHtml(ing, { t: _t });
  assert.match(ingHtml, /data-relay-continue disabled/);
  assert.match(ingHtml, /T:inbox\.connect\.relay_continuing/);
  // continued：已替点 → 提示切「正在跟进页面」，按钮保持忙态
  const done = relayFormView(base, { platform: 'messenger',
    local: { phase: 'continued', ts: NOW - 5000, step: 'checkpoint' }, now: NOW });
  assert.equal(done.contPhase, 'continued');
  assert.equal(done.hintKey, 'inbox.connect.relay_continued_hint');
  assert.match(buildFormHtml(done, { t: _t }), /data-relay-continue disabled/);
  // 超过驻留窗（CONTINUE_WAIT_MS）：自动回常态，按钮可再点
  const stale = relayFormView(base, { platform: 'messenger',
    local: { phase: 'continued', ts: NOW - 61000, step: 'checkpoint' }, now: NOW });
  assert.equal(stale.contPhase, '');
  assert.doesNotMatch(buildFormHtml(stale, { t: _t }), /data-relay-continue disabled/);
});

test('B64三期 继续回执：miss/fail 分道 notice（点了没点到必须让用户知道）', () => {
  const base = { step: 'checkpoint', escalate: true, qr_image: 'data:x' };
  const miss = relayFormView(base, { platform: 'messenger',
    local: { phase: 'continue_miss', ts: NOW, step: 'checkpoint' }, now: NOW });
  assert.equal(miss.contNoticeKey, 'inbox.connect.relay_continue_miss');
  const missHtml = buildFormHtml(miss, { t: _t });
  assert.match(missHtml, /connect-relay-notice warn/);
  assert.match(missHtml, /T:inbox\.connect\.relay_continue_miss/);
  assert.doesNotMatch(missHtml, /data-relay-continue disabled/);   // 可立即再点
  const fail = relayFormView(base, { platform: 'messenger',
    local: { phase: 'continue_fail', ts: NOW, step: 'checkpoint' }, now: NOW });
  assert.equal(fail.contNoticeKey, 'inbox.connect.relay_continue_fail');
  assert.match(buildFormHtml(fail, { t: _t }), /connect-relay-notice err/);
});

test('B64三期 device_confirm 子味：手机主视觉 + 三步清单 + 截图折叠（默认收起）', () => {
  const v = relayFormView({ step: 'checkpoint', escalate: true, code: 'device_confirm',
    qr_image: 'data:x' }, { platform: 'messenger' });
  assert.equal(v.mode, 'screenshot');
  assert.equal(v.devConfirm, true);
  assert.equal(v.noticeCode, 'device_confirm');
  assert.equal(v.headKey, 'inbox.connect.relay_devconf_title');
  assert.equal(v.hintKey, 'inbox.connect.relay_devconf_hint');
  assert.equal(v.nextKey, '');   // 三步清单替代泛化 ways 文字墙
  const html = buildFormHtml(v, { t: _t });
  assert.match(html, /data-relay-flavor="device_confirm"/);
  assert.match(html, /connect-relay-hero/);
  assert.match(html, /connect-relay-steps/);
  assert.match(html, /T:inbox\.connect\.relay_devconf_s1/);
  assert.match(html, /T:inbox\.connect\.relay_devconf_s3/);
  // 截图进折叠区（主角是手机动作），默认收起、shotOpen=true 时保持展开
  assert.match(html, /data-relay-shotbox/);
  assert.doesNotMatch(html, /data-relay-shotbox open/);
  v.shotOpen = true;
  assert.match(buildFormHtml(v, { t: _t }), /data-relay-shotbox open/);
  // 继续钮照常（device_confirm 是 checkpoint 家族）
  assert.match(html, /data-relay-continue/);
});

test('B64三期 泛检查点：截图直出带窗体 chrome + LIVE 徽标（不折叠）', () => {
  const html = buildFormHtml(relayFormView({ step: 'checkpoint', escalate: true,
    qr_image: 'data:x' }, { platform: 'messenger' }), { t: _t });
  assert.match(html, /connect-relay-shotwrap/);
  assert.match(html, /crs-live/);
  assert.doesNotMatch(html, /data-relay-shotbox/);
});

test('B64三期 等待生命感：已等待/截图新鲜度/会话剩余 <5min 才渲染', () => {
  const v = relayFormView({ step: 'checkpoint', escalate: true, qr_image: 'data:x' },
    { platform: 'messenger' });
  v.waitedSec = 75; v.shotAgeSec = 8; v.ttlSec = 240;
  // 无引号 stub（meta 行经 _esc，引号会转义成 &quot; 干扰断言）
  const tf = (k, p) => 'F:' + k + ':' + Object.keys(p).map((kk) => kk + '=' + p[kk]).join(',');
  const html = buildFormHtml(v, { t: _t, tf });
  assert.match(html, /connect-relay-meta/);
  assert.match(html, /F:inbox\.connect\.relay_waited:t=1:15/);
  assert.match(html, /F:inbox\.connect\.relay_ttl_left:t=4:00/);
  assert.match(html, /F:inbox\.connect\.relay_shot_ts:sec=8/);
  // 剩余 ≥5min 不渲染倒计时（30min 会话前 25 分钟保持安静）；刚进入也不渲「已等待」
  const q = relayFormView({ step: 'checkpoint', escalate: true, qr_image: 'data:x' },
    { platform: 'messenger' });
  q.waitedSec = 2; q.ttlSec = 1500; q.shotAgeSec = -1;
  const qh = buildFormHtml(q, { t: _t, tf });
  assert.doesNotMatch(qh, /connect-relay-meta/);
  assert.doesNotMatch(qh, /connect-relay-shot-ts/);
});

test('B64三期 解卡正反馈：unstuck 只进无输入视图（verifying/preparing）', () => {
  const prep = relayFormView({ booting: true, qr_image: 'data:x' });
  prep.unstuck = true;
  assert.match(buildFormHtml(prep, { t: _t }), /connect-relay-unstuck/);
  // form 视图有渲染去重键保输入，闪现条刻意不进（进了会逼重渲清输入）
  const form = relayFormView({ step: 'twofactor', fields: ['code'] });
  form.unstuck = true;   // 即便被误置，builder 的 form 分支也不消费该字段
  assert.doesNotMatch(buildFormHtml(form, { t: _t }), /connect-relay-unstuck/);
});

test('submitPayload(checkpoint)：无字段但 step 保留（继续推进的请求体）', () => {
  assert.deepEqual(submitPayload('checkpoint', {}), { step: 'checkpoint', values: {} });
});

// ── P1：错误语义分道 / 前置清单 / 密码眼睛 / 本地缺填 ──

test('P1 account_locked：锁定专属文案（等冷却/申诉），不与泛检查点混谈', () => {
  const v = relayFormView({ step: 'checkpoint', escalate: true, code: 'account_locked',
    qr_image: 'data:x' }, { platform: 'messenger' });
  assert.equal(v.mode, 'screenshot');
  assert.equal(v.headKey, 'inbox.connect.relay_locked_title');
  assert.equal(v.hintKey, 'inbox.connect.relay_locked_hint');
  assert.equal(v.nextKey, 'inbox.connect.relay_locked_ways');
  assert.equal(v.noticeCode, 'account_locked');
});

test('P1 前置清单：干净账密步带 prep 卡；报错重填时收起（不占版面）', () => {
  const clean = relayFormView({ step: 'credentials', fields: ['email', 'password'] });
  assert.equal(clean.prepKey, 'inbox.connect.relay_prep_credentials');
  assert.match(buildFormHtml(clean, { t: _t }), /T:inbox\.connect\.relay_prep_credentials/);
  const err = relayFormView({ step: 'credentials', error: true });
  assert.equal(err.prepKey, '');
  const tf = relayFormView({ step: 'twofactor' });
  assert.equal(tf.prepKey, '');
});

test('P1 密码眼睛：password 族字段渲显隐钮，text 字段不渲', () => {
  const html = buildFormHtml(relayFormView({ step: 'credentials', fields: ['email', 'password'] }), { t: _t });
  assert.match(html, /data-relay-eye="password"/);
  assert.doesNotMatch(html, /data-relay-eye="email"/);
  assert.match(html, /T:inbox\.connect\.relay_eye_show/);
  const pin = buildFormHtml(relayFormView({ step: 'e2ee_pin' }), { t: _t });
  assert.match(pin, /data-relay-eye="pin"/);
});
