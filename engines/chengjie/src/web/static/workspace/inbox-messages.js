/* 收件箱消息区辅助（四/五期薄拆）
 *
 * 从 unified_inbox.html 抽出：
 *   - DOM reconcile（sig / buildNode / adoptPlayingMedia / reconcile / txPatch）
 *   - 纯文本与回执 helper（normTxt / meaningfulXlate / deliveryTick）
 * 气泡 HTML 组装仍留在宿主（会话态 / 翻译 / WA / 内联 onclick 门禁）。
 *
 * 导出：window.InboxMessages
 */
(function () {
  'use strict';
  if (window.InboxMessages && window.InboxMessages.reconcile) return;

  function sig(s) {
    s = String(s == null ? '' : s);
    var h = 5381;
    for (var i = 0; i < s.length; i++) { h = ((h << 5) + h + s.charCodeAt(i)) | 0; }
    return h.toString(36) + '_' + s.length;
  }

  var _tpl = document.createElement('template');
  function buildNode(u) {
    _tpl.innerHTML = String(u.html || '').trim();
    var n = _tpl.content.firstElementChild || document.createElement('div');
    n.dataset.ukey = u.key;
    n.dataset.usig = u.sig;
    return n;
  }

  function adoptPlayingMedia(oldNode, newNode) {
    try {
      oldNode.querySelectorAll('audio,video').forEach(function (om) {
        if (om.ended || (om.paused && !(om.currentTime > 0))) return;
        var src = om.getAttribute('src') || '';
        var list = newNode.querySelectorAll(om.tagName.toLowerCase());
        for (var i = 0; i < list.length; i++) {
          var nm = list[i];
          if ((nm.getAttribute('src') || '') === src) {
            nm.replaceWith(om);
            if (om.tagName === 'AUDIO' && typeof window._voiceDur === 'function') {
              try { window._voiceDur(om); } catch (_) {}
            }
            break;
          }
        }
      });
    } catch (_) {}
  }

  function txPatch(tx, html) {
    var a = document.getElementById('msg-area');
    var nb = a && (a.scrollHeight - a.scrollTop - a.clientHeight < 60);
    tx.innerHTML = html;
    if (nb && a) a.scrollTop = a.scrollHeight;
  }

  function reconcile(el, units) {
    var changed = false;
    var byKey = new Map();
    var children = Array.prototype.slice.call(el.children);
    for (var ci = 0; ci < children.length; ci++) {
      var n = children[ci];
      var k = (n.dataset && n.dataset.ukey) || '';
      if (!k || byKey.has(k)) { n.remove(); changed = true; continue; }
      byKey.set(k, n);
    }
    var want = new Set(units.map(function (u) { return u.key; }));
    byKey.forEach(function (node, k) {
      if (!want.has(k)) { node.remove(); byKey.delete(k); changed = true; }
    });
    var prev = null;
    for (var ui = 0; ui < units.length; ui++) {
      var u = units[ui];
      var node = byKey.get(u.key);
      if (node && node.dataset.usig !== u.sig) {
        var nn = buildNode(u);
        adoptPlayingMedia(node, nn);
        node.replaceWith(nn); node = nn; changed = true;
      } else if (!node) {
        node = buildNode(u); changed = true;
      }
      var expected = prev ? prev.nextElementSibling : el.firstElementChild;
      if (node !== expected) el.insertBefore(node, expected);
      prev = node;
    }
    var tail = prev ? prev.nextElementSibling : null;
    while (tail) {
      var nx = tail.nextElementSibling;
      tail.remove();
      changed = true;
      tail = nx;
    }
    return changed;
  }

  function normTxt(s) {
    return String(s == null ? '' : s).replace(/\s+/g, '').toLowerCase();
  }

  function meaningfulXlate(orig, xl) {
    if (!xl) return false;
    return normTxt(orig) !== normTxt(xl);
  }

  /* status → 勾 HTML；文案经 T(key) 注入，缺省回落键名 */
  function deliveryTick(status, T) {
    var t = typeof T === 'function' ? T : function (k) { return k; };
    var s = String(status || '');
    if (s === 'read') {
      return '<span class="msg-tick read" title="' + t('inbox.msg.receipt_read') + '">\u2713\u2713</span>';
    }
    if (s === 'delivered') {
      return '<span class="msg-tick" title="' + t('inbox.msg.receipt_delivered') + '">\u2713\u2713</span>';
    }
    if (s === 'sent') {
      return '<span class="msg-tick" title="' + t('inbox.msg.receipt_sent') + '">\u2713</span>';
    }
    return '';
  }

  window.InboxMessages = {
    sig: sig,
    buildNode: buildNode,
    adoptPlayingMedia: adoptPlayingMedia,
    reconcile: reconcile,
    txPatch: txPatch,
    normTxt: normTxt,
    meaningfulXlate: meaningfulXlate,
    deliveryTick: deliveryTick
  };
})();
