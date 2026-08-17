/*!
 * ui_icons.js — 工作台通用线性 SVG 图标库（单一事实来源 / SSOT）。
 *
 * 与 platform_icons.js 同构：既提供命令式 `window.uiIcon(name, size|opts)` 供 JS 拼接，
 * 又提供声明式 `[data-ui-icon]` + `enhance()` 供静态 HTML（DOMContentLoaded 自动填充）。
 *
 * 所有图标：24x24 viewBox、fill=none、stroke=currentColor、round 线帽/连接 —— 单色、
 * 继承文字颜色、明暗两态自适应。图标路径纯 ASCII（无 CJK），不受模板 i18n / CJK 门禁影响。
 *
 * ⚠ SSOT 铁律（2026-08-08 起，tests/test_ui_icon_registry.py 门禁钉住）：
 *   1. 全站只允许本文件定义 `uiIcon`。unified_inbox.html 曾内联同名 `_UIIC` 覆盖全局，
 *      导致共享顶栏在收件箱页图标集缺斤短两（shield/pin/bell… 静默变空）——已合并进本表。
 *   2. 查不到的图标名不再无声返回空串：上报 icon_miss 遥测（复用 frontend-error 通道，
 *      按名去重、每页上限 8 条），线上「哑图标」从此可观测。
 *   3. 生成的 <svg> 带 pointer-events:none —— 图标是装饰物，点击一律落到宿主元素，
 *      防「点中 SVG 本体导致 e.target.id 判断失灵」一类死点击。
 */
(function () {
  "use strict";

  var P = {
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18z"/>',
    alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12" y2="17"/>',
    shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    chart: '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
    "trend-up": '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    "trend-down": '<polyline points="23 18 13.5 8.5 8.5 13.5 1 6"/><polyline points="17 18 23 18 23 12"/>',
    trophy: '<path d="M8 21h8"/><path d="M12 17v4"/><path d="M7 4h10v5a5 5 0 0 1-10 0V4z"/><path d="M17 5h2.5a1.5 1.5 0 0 1 0 4H18"/><path d="M7 5H4.5a1.5 1.5 0 0 0 0 4H6"/>',
    clipboard: '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
    building: '<rect x="5" y="3" width="14" height="18" rx="1.5"/><path d="M9 21v-4h6v4"/><line x1="9" y1="7" x2="9" y2="7"/><line x1="15" y1="7" x2="15" y2="7"/><line x1="9" y1="11" x2="9" y2="11"/><line x1="15" y1="11" x2="15" y2="11"/>',
    scale: '<line x1="12" y1="3" x2="12" y2="21"/><line x1="7" y1="21" x2="17" y2="21"/><line x1="4" y1="7" x2="20" y2="7"/><path d="M4 7 1.5 12a2.5 2.5 0 0 0 5 0z"/><path d="M20 7l-2.5 5a2.5 2.5 0 0 0 5 0z"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    zap: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>',
    mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/>',
    inbox: '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/>',
    rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
    book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
    star: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>',
    pin: '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
    chat: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
    bot: '<rect x="4" y="8" width="16" height="11" rx="2.5"/><path d="M12 8V5"/><circle cx="12" cy="3.6" r="1.2"/><line x1="9" y1="13.5" x2="9" y2="13.5"/><line x1="15" y1="13.5" x2="15" y2="13.5"/>',
    ban: '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
    check: '<polyline points="20 6 9 17 4 12"/>',
    x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    phone: '<rect x="5" y="2" width="14" height="20" rx="2.5"/><line x1="12" y1="18" x2="12" y2="18"/>',
    send: '<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    scroll: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    clock: '<circle cx="12" cy="12" r="9"/><polyline points="12 7.5 12 12 15 13.8"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 8-3 8h18s-3-1-3-8"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    heart: '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
    search: '<circle cx="11" cy="11" r="7"/><line x1="20.5" y1="20.5" x2="16.6" y2="16.6"/>',
    folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    /* —— 2026-08-08 自 unified_inbox.html 页内 _UIIC 合并（SSOT 收敛，页内副本已删除）—— */
    msg: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
    lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    tag: '<path d="M20.6 13.4 12 22l-9-9V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.6" cy="7.6" r="1.3"/>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><line x1="10" y1="12" x2="14" y2="12"/>',
    claim: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><polyline points="16 11 18 13 22 9"/>',
    clip: '<path d="M21 12.5 12.5 21a4.5 4.5 0 0 1-6.4-6.4L14 6.7a3 3 0 0 1 4.3 4.3l-7.8 7.8a1.5 1.5 0 0 1-2.1-2.1l7.1-7.1"/>',
    spark: '<path d="M11 3l1.7 4.6L17 9l-4.3 1.4L11 15l-1.7-4.6L5 9l4.3-1.4L11 3z"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.5 0l2-2a5 5 0 0 0-7.1-7.1l-1.3 1.3"/><path d="M14 11a5 5 0 0 0-7.5 0l-2 2a5 5 0 0 0 7.1 7.1l1.3-1.3"/>',
    brain: '<path d="M9.5 2A5.5 5.5 0 0 0 4 7.5c0 1.9.9 3.6 2.3 4.7L6 22h12l-.3-9.8A5.5 5.5 0 0 0 14.5 2 5.5 5.5 0 0 0 9.5 2z"/>',
    film: '<rect x="2" y="2" width="20" height="20" rx="2.5"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/>',
    edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
    route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7H11a3.5 3.5 0 0 1 0-7H4"/>',
    sync: '<path d="M8 17.5 12 21l4-3.5"/><path d="M12 21v-8"/><path d="M20.9 18.4A5 5 0 0 0 18 9.5h-1.3A8 8 0 1 0 4 16.9"/>',
    logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    trash: '<polyline points="3 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    quote: '<path d="M10 7c-2.8.7-4.5 2.7-4.5 5.6V17H10v-4.5H7.6c.1-1.8 1.2-3.2 3-3.9z"/><path d="M18.5 7c-2.8.7-4.5 2.7-4.5 5.6V17h4.5v-4.5h-2.4c.1-1.8 1.2-3.2 3-3.9z"/>',
    speed: '<path d="M5 14a7 7 0 1 1 14 0"/><path d="m12 14 3.2-3.6"/><path d="M5 18h14"/>',
    smile: '<circle cx="12" cy="12" r="9"/><path d="M8.5 14a4.5 4.5 0 0 0 7 0"/><line x1="9" y1="9.5" x2="9.01" y2="9.5"/><line x1="15" y1="9.5" x2="15.01" y2="9.5"/>',
    target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
    /* —— 2026-08-08 顶栏字符图标 SVG 化新增（✕/▾/↗/🔁/🎨/🛠/🔑/👑 的线性替身）—— */
    /* —— 2026-08-08 P2A 收件箱控件 emoji 治理新增（⏸❓🧪🎭🔕📤🎵📄↩💡🌱🔌🧬⏳ 的线性替身）—— */
    pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4"/><line x1="12" y1="17.5" x2="12.01" y2="17.5"/>',
    flask: '<path d="M10 2v6L4.6 17.1A2.5 2.5 0 0 0 6.8 21h10.4a2.5 2.5 0 0 0 2.2-3.9L14 8V2"/><line x1="8" y1="2" x2="16" y2="2"/><line x1="7" y1="14" x2="17" y2="14"/>',
    mask: '<path d="M12 3C7.5 3 4 4.8 4 8.5c0 5 3.6 9.6 8 12.5 4.4-2.9 8-7.5 8-12.5C20 4.8 16.5 3 12 3z"/><path d="M8 10c.7.8 1.8.8 2.5 0"/><path d="M13.5 10c.7.8 1.8.8 2.5 0"/><path d="M9.5 14.5c1.5 1 3.5 1 5 0"/>',
    "bell-off": '<path d="M8.7 3A6 6 0 0 1 18 8c0 3 .6 4.7 1.2 5.7"/><path d="M17 17H3s3-1 3-9a6 6 0 0 1 .3-1.9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/><line x1="2" y1="2" x2="22" y2="22"/>',
    unarchive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8"/><path d="m9.5 14.5 2.5-2.5 2.5 2.5"/><line x1="12" y1="12.5" x2="12" y2="18"/>',
    music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
    reply: '<polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/>',
    bulb: '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4.9 12c.9.9 1.4 1.9 1.6 3h6.6c.2-1.1.7-2.1 1.6-3A7 7 0 0 0 12 2z"/>',
    leaf: '<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/>',
    plug: '<path d="M12 22v-3"/><path d="M9 8V2"/><path d="M15 8V2"/><path d="M6 8h12v4a6 6 0 0 1-12 0z"/>',
    dna: '<path d="M5 3c0 4.5 3.5 4.5 3.5 9S5 16.5 5 21"/><path d="M19 3c0 4.5-3.5 4.5-3.5 9s3.5 4.5 3.5 9"/><line x1="7" y1="7" x2="17" y2="7"/><line x1="8.5" y1="12" x2="15.5" y2="12"/><line x1="7" y1="17" x2="17" y2="17"/>',
    hourglass: '<path d="M6 2h12"/><path d="M6 22h12"/><path d="M7 2v3.5L12 11l5-5.5V2"/><path d="M7 22v-3.5L12 13l5 5.5V22"/>',
    cloud: '<path d="M17.5 19a4.5 4.5 0 0 0 0-9 6 6 0 0 0-11.7 1.7A3.8 3.8 0 0 0 7 19z"/>',
    headphones: '<path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/>',
    volume: '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/>',
    "phone-off": '<path d="M19 12V4.5A2.5 2.5 0 0 0 16.5 2h-9A2.5 2.5 0 0 0 5 4.5V7"/><path d="M5 11v8.5A2.5 2.5 0 0 0 7.5 22h9a2.5 2.5 0 0 0 2.5-2.5V16"/><line x1="2" y1="2" x2="22" y2="22"/>',
    "chevron-down": '<polyline points="6 9 12 15 18 9"/>',
    "chevron-right": '<polyline points="9 5 16 12 9 19"/>',
    "arrow-up-right": '<line x1="7" y1="17" x2="17" y2="7"/><polyline points="8 7 17 7 17 16"/>',
    flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>',
    crown: '<path d="m2 5 3 11h14l3-11-6 6.5L12 4 8 11.5z"/><path d="M5 20h14"/>',
    key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 8.8-8.8"/><path d="m15 4 3 3"/><path d="m18.5 7.5 2 2"/>',
    contrast: '<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z"/>',
    wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'
  };

  // 历史短名兼容（原 unified_inbox 页内命名）：一个语义一份美术，别名只做转发。
  var ALIAS = { chev: "chevron-right", dl: "download" };

  // 「哑图标」可观测化：查不到名 → 上报 icon_miss（同名每页一次、每页上限 8 条；
  // 复用 dead-click 守卫同一后端通道 /api/telemetry/frontend-error，绝不抛错影响渲染）。
  var _missed = {};
  var _missedCount = 0;
  function _reportMiss(name) {
    try {
      if (_missed[name] || _missedCount >= 8) return;
      _missed[name] = 1; _missedCount++;
      if (typeof navigator === "undefined" || !navigator.sendBeacon || typeof Blob === "undefined") return;
      var body = JSON.stringify({
        page: (typeof location !== "undefined" ? location.pathname : ""),
        fn: "uiIcon:" + String(name).slice(0, 40),
        type: "icon_miss"
      });
      navigator.sendBeacon("/api/telemetry/frontend-error", new Blob([body], { type: "application/json" }));
    } catch (_) { /* 遥测永不干扰渲染 */ }
  }

  // uiIcon(name, size) 或 uiIcon(name, {size, cls, sw, style}) 或 uiIcon(name, size, cls)
  function svg(name, opts, cls3) {
    if (typeof opts === "number") opts = { size: opts, cls: cls3 };
    opts = opts || {};
    var key = ALIAS[name] || name;
    var p = P[key];
    if (!p) { _reportMiss(name); return ""; }
    var s = opts.size || 16;
    var sw = opts.sw || 2;
    var cls = "ui-ic" + (opts.cls ? " " + opts.cls : "");
    var st = "vertical-align:-0.15em;flex:none" + (opts.style ? ";" + opts.style : "");
    return (
      '<svg class="' + cls + '" style="' + st + '" viewBox="0 0 24 24" width="' + s +
      '" height="' + s + '" fill="none" stroke="currentColor" stroke-width="' + sw +
      '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + p + "</svg>"
    );
  }

  // 图标一律不吃鼠标事件：点击落宿主元素，防 e.target 命中 <svg>/<path> 绕过 id/class 判断。
  function _injectBaseCss() {
    if (typeof document === "undefined" || !document.head) return;
    if (document.getElementById("ui-ic-base-style")) return;
    var st = document.createElement("style");
    st.id = "ui-ic-base-style";
    st.textContent = ".ui-ic{pointer-events:none}";
    document.head.appendChild(st);
  }

  // 声明式：把 <span data-ui-icon="shield" data-size="15"></span> 就地填成 SVG（幂等）。
  function enhance(root) {
    if (typeof document === "undefined") return;
    var scope = root || document;
    var els = scope.querySelectorAll("[data-ui-icon]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.getAttribute("data-ui-done") === "1") continue;
      var sz = parseInt(el.getAttribute("data-size") || "16", 10);
      el.innerHTML = svg(el.getAttribute("data-ui-icon"), { size: sz });
      el.setAttribute("data-ui-done", "1");
    }
  }

  var api = {
    svg: svg,
    enhance: enhance,
    has: function (n) { return !!P[ALIAS[n] || n]; },
    names: function () { return Object.keys(P); }
  };
  var root = (typeof window !== "undefined") ? window :
    (typeof globalThis !== "undefined" ? globalThis : this);
  if (root) {
    root.UiIcons = api;
    // SSOT：全站唯一 uiIcon 定义点（门禁 tests/test_ui_icon_registry.py 禁止模板再声明同名全局）。
    if (typeof root.uiIcon !== "function") root.uiIcon = svg;
    root.uiIconEnhance = enhance;
  }
  if (typeof document !== "undefined") {
    _injectBaseCss();
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { _injectBaseCss(); enhance(); });
    } else {
      enhance();
    }
  }
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
})();
