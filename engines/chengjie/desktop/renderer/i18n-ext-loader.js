/* 扩展语 overlay 条件装载器（index.html 专用；CSP script-src 'self' 禁内联，
   故用外部文件 + document.write 同步注入保时序——写入点即当前解析位置，
   紧跟宿主 <script> 之后执行，先于任何组件渲染与 DOMContentLoaded。
   用法：<script src="i18n-ext-loader.js" data-kind="shell|cp"></script>
   语言源=?lang=（main.js loadFile 注入，与壳菜单/webview 同一权威源）。
   非扩展语（zh/en/未知）零动作零开销；文件缺失=该语言维持既有回落。 */
(function () {
  'use strict';
  var s = document.currentScript;
  if (!s) return;
  var kind = s.getAttribute('data-kind') || '';
  var m = '';
  try { m = new URLSearchParams(location.search).get('lang') || ''; } catch (e) { m = ''; }
  m = m.trim().toLowerCase().replace(/-/g, '_');
  if (m === 'zh_tw' || m === 'zh_hk') m = 'zh_hant';
  if (m !== 'zh_hant' && m !== 'vi' && m !== 'th' && m !== 'id') return;
  var src = kind === 'cp'
    ? 'shared/copilot/i18n/cp-i18n-ext.' + m + '.js'
    : 'shell-i18n-ext.' + m + '.js';
  /* eslint-disable-next-line no-document-write -- 同步注入是时序契约的一部分 */
  document.write('<script src="' + src + '"><\/script>');
})();
