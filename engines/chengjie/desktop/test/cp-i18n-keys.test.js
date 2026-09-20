/* cp-i18n 键完整性门禁（#116，2026-08-31）。
 *
 * 实锤：cp-voice.js 引导行引用 cp.voice.tip_got，而 cp-i18n.js 从未定义该键
 * ——组件 _t() 缺键回落显示裸键名，客户界面直接出现「cp.voice.tip_got」
 * （钧 0831 原图 909）。组件迭代加键、词典忘记跟 ＝ 静态可查的一类回归，
 * 本门禁在打包链（npm test → predist）把它拦在出包之前：
 *   renderer/shared/copilot 下所有组件的 _t("cp.…") 字面量键，必须在
 *   cp-i18n.js 的 zh 与 en 两个词典块里都有定义（zh_hant 走 ext 覆盖 +
 *   回落 zh，不作硬要求）。动态键（_t(variable)）不在本门禁范围。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..', 'renderer', 'shared', 'copilot');
const I18N = path.join(ROOT, 'i18n', 'cp-i18n.js');

let failures = 0;
let checks = 0;
function ok(cond, msg) {
  checks += 1;
  if (!cond) {
    failures += 1;
    console.error('  [FAIL] ' + msg);
  }
}

function listComponentFiles() {
  const dirs = [path.join(ROOT, 'components')];
  const out = [];
  for (const d of dirs) {
    if (!fs.existsSync(d)) continue;
    for (const f of fs.readdirSync(d)) {
      if (f.endsWith('.js')) out.push(path.join(d, f));
    }
  }
  return out;
}

function definedKeys(src) {
  // 词典定义形态："cp.voice.tip_got": "…"（zh/en 两块都在同一文件里；
  // 这里收全量定义集合——同键双语齐备由 zh/en 分块计数断言兜）。
  const keys = new Set();
  const re = /"(cp\.[a-z0-9_.]+)"\s*:/gi;
  let m;
  while ((m = re.exec(src)) !== null) keys.add(m[1]);
  return keys;
}

function keyCountIn(src, key) {
  // 同一键应至少出现 2 次定义（zh 块 + en 块）；ext 包不算（另一文件）。
  const re = new RegExp('"' + key.replace(/\./g, '\\.') + '"\\s*:', 'g');
  const m = src.match(re);
  return m ? m.length : 0;
}

function usedLiteralKeys(src) {
  const keys = new Set();
  // _t("cp.xxx") / _t('cp.xxx')，含 this._t / 换行后首参。变量键刻意不收。
  const re = /_t\(\s*["'](cp\.[a-z0-9_.]+)["']/gi;
  let m;
  while ((m = re.exec(src)) !== null) keys.add(m[1]);
  return keys;
}

const i18nSrc = fs.readFileSync(I18N, 'utf8');
const defined = definedKeys(i18nSrc);
ok(defined.size > 200, 'cp-i18n.js 词典解析异常（仅 ' + defined.size + ' 键）');

const files = listComponentFiles();
ok(files.length >= 3, 'components 目录扫描异常（仅 ' + files.length + ' 文件）');

const missing = [];
const monolingual = [];
for (const f of files) {
  const src = fs.readFileSync(f, 'utf8');
  for (const k of usedLiteralKeys(src)) {
    if (!defined.has(k)) {
      missing.push(path.basename(f) + ' -> ' + k);
    } else if (keyCountIn(i18nSrc, k) < 2) {
      monolingual.push(path.basename(f) + ' -> ' + k);
    }
  }
}
ok(missing.length === 0,
   '组件引用了 cp-i18n.js 未定义的键（缺键=界面直显裸键名）：\n    '
   + missing.join('\n    '));
ok(monolingual.length === 0,
   '以下键只有单语定义（zh/en 必须双语齐备）：\n    ' + monolingual.join('\n    '));

// #116 回归钉：事故键本体必须存在且双语。
ok(defined.has('cp.voice.tip_got'), '事故键 cp.voice.tip_got 未定义（#116 回归）');
ok(keyCountIn(i18nSrc, 'cp.voice.tip_got') >= 2, 'cp.voice.tip_got 缺双语定义');

if (failures) {
  console.error('cp-i18n-keys: ' + failures + '/' + checks + ' FAILED');
  process.exit(1);
}
console.log('cp-i18n-keys: ' + checks + ' checks OK'
  + '（组件文件 ' + files.length + '，词典键 ' + defined.size + '）');
