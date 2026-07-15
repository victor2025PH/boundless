/* 全站统一错误文案 —— 把原始异常翻成"可执行人话"，挂到 window.friendlyErr。
 * 覆盖：getUserMedia 设备错误(类型在 e.name) / fetch 网络 / 超时 / HTTP 4xx·5xx / 取消。
 * 纯函数、无依赖。各页面在 <head> 引入（与 _clientlog.js 同级），避免多副本各自漂移。
 * 原始报错只进 console 便于排障，界面只显示友好文案。 */
(function () {
  function friendlyErr(e) {
    var raw = (e && (e.message || e)) ? String(e.message || e) : '';
    var name = (e && e.name) ? String(e.name) : '';
    try { if (raw || name) console.error('[friendlyErr]', name, raw); } catch (_) {}
    var s = (raw + ' ' + name).toLowerCase();
    if (!raw && !name) return '服务异常，请稍后重试';
    if (s.indexOf('abort') >= 0) return '已取消';
    // ── 麦克风 / 录音设备（语音类页面最常见的拦路虎）──
    if (s.indexOf('notallowed') >= 0 || s.indexOf('permission') >= 0 || s.indexOf('denied') >= 0)
      return '麦克风被拒绝：点地址栏🔒/ⓘ→网站设置→麦克风→改“允许”，然后刷新；微信里请点右上···→在浏览器打开';
    if (s.indexOf('notfound') >= 0 || s.indexOf('device not found') >= 0 || s.indexOf('devices not found') >= 0)
      return '未检测到麦克风：请插入或启用麦克风后重试';
    if (s.indexOf('notreadable') >= 0 || s.indexOf('could not start audio') >= 0 || s.indexOf('device in use') >= 0)
      return '麦克风被占用：请关闭其他正在使用麦克风的程序后重试';
    if (s.indexOf('overconstrained') >= 0) return '麦克风不支持所需参数，请更换设备后重试';
    if (s.indexOf('secure') >= 0) return '需在 HTTPS 或 localhost 环境下才能使用麦克风';
    // ── 网络 / 超时 / HTTP ──
    if (s.indexOf('failed to fetch') >= 0 || s.indexOf('networkerror') >= 0 ||
        s.indexOf('network request') >= 0 || s.indexOf('load failed') >= 0)
      return '网络连接异常，请检查网络后重试';
    if (s.indexOf('timeout') >= 0 || s.indexOf('timed out') >= 0) return '响应超时，请稍后重试';
    if (/\b5\d{2}\b/.test(s) || s.indexOf('offer') >= 0) return '服务暂时繁忙，请稍后重试';
    if (/\b(40[0-9]|41\d|42\d|43\d)\b/.test(s)) return '请求未被接受，请刷新页面后重试';
    return '服务异常，请稍后重试';
  }
  window.friendlyErr = friendlyErr;
})();
