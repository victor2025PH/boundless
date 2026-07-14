/* canvas_brand.js — 画布品牌基元（单源）。
   P10-3：hub.js 战报卡 与 phone.html 分享海报 各自 copy-paste 了一份
   roundRect/loadImg/accRgb/金标(阈值+金渐变+字色)——阈值或品牌色一改就漂移。
   收敛到本模块：两页 <script> 引入后经 window.BD_CANVAS 取用；
   金标语义（相似度 >= 0.75 = 金标）与海报金渐变自此只有一处真相。
   注意：仅放「画布绘制」共用基元；DOM/Alpine 相关的助手不进来（保持零依赖可独测）。 */
(function () {
  'use strict';

  var GOLD_MIN = 0.75;                       // 金标阈值：音色相似度(cosine) >= 0.75
  var GOLD_GRAD = ['#f59e0b', '#fbbf24'];    // 金标渐变（与 phone .pq.gold / hero-gold CSS 同色）
  var GOLD_TEXT = '#3a2400';                 // 金底上的深棕字
  var OK_BG = 'rgba(34,197,94,.18)';         // 非金标的绿底
  var OK_TEXT = '#4ade80';

  /* 品牌点缀色 "R,G,B"（白标跟随 --acc-rgb；取不到回退无界蓝） */
  function accRgb() {
    try {
      return (getComputedStyle(document.documentElement).getPropertyValue('--acc-rgb').trim() || '79 122 255')
        .replace(/\s+/g, ',');
    } catch (_) { return '79,122,255'; }
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  /* 加载失败 reject（调用方按需 .catch(()=>null) 降级——显式可见，杜绝静默吞错） */
  function loadImg(src) {
    return new Promise(function (res, rej) {
      var img = new Image(); img.crossOrigin = 'anonymous';
      img.onload = function () { res(img); };
      img.onerror = function () { rej(new Error('load ' + src)); };
      img.src = src;
    });
  }

  function isGold(cos) { return (cos || 0) >= GOLD_MIN; }

  /* 金标 pill 的水平金渐变填充（x0→x1 与 pill 左右缘对齐） */
  function goldFill(ctx, x0, x1) {
    var g = ctx.createLinearGradient(x0, 0, x1, 0);
    g.addColorStop(0, GOLD_GRAD[0]); g.addColorStop(1, GOLD_GRAD[1]);
    return g;
  }

  /* P11 二期：文本基元。
     wrapText —— 逐字换行（中文无空格断词，按字符测宽），最多 maxLines 行，超出截断。
     ellipsize —— 单行截断加省略号（超长角色名/URL 防溢出画布安全区）。 */
  function wrapText(ctx, text, cx, y, maxW, lh, maxLines) {
    var chars = Array.from(String(text || ''));
    var line = '', lines = [];
    for (var i = 0; i < chars.length; i++) {
      var ch = chars[i];
      if (ctx.measureText(line + ch).width > maxW && line) {
        lines.push(line); line = ch;
        if (lines.length >= maxLines) { line = ''; break; }
      } else line += ch;
    }
    if (line) lines.push(line);
    lines.slice(0, maxLines).forEach(function (l, i2) { ctx.fillText(l, cx, y + i2 * lh); });
  }

  function ellipsize(ctx, text, maxW) {
    var s = String(text || '');
    if (ctx.measureText(s).width <= maxW) return s;
    var chars = Array.from(s);
    while (chars.length && ctx.measureText(chars.join('') + '…').width > maxW) chars.pop();
    return chars.join('') + '…';
  }

  window.BD_CANVAS = {
    GOLD_MIN: GOLD_MIN, GOLD_TEXT: GOLD_TEXT, OK_BG: OK_BG, OK_TEXT: OK_TEXT,
    accRgb: accRgb, roundRect: roundRect, loadImg: loadImg,
    isGold: isGold, goldFill: goldFill,
    wrapText: wrapText, ellipsize: ellipsize,
  };
})();
