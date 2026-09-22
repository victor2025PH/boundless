/* 教程录屏指针：大号光标 + 大红圈闪光 + 底部说明条。由 record_chatx 注入。 */
(function () {
  if (window.__chatxPointer) return;
  const root = document.createElement("div");
  root.id = "chatx-tutorial-pointer-root";
  root.innerHTML = `
    <style>
      #chatx-tutorial-pointer-root{pointer-events:none;position:fixed;inset:0;z-index:2147483646;font-family:Microsoft YaHei,sans-serif}
      #ctp-cursor{position:fixed;left:0;top:0;width:56px;height:56px;margin:-6px 0 0 -6px;transition:left .16s ease,top .16s ease;filter:drop-shadow(0 3px 8px rgba(0,0,0,.65));z-index:3}
      #ctp-cursor svg{display:block;width:56px;height:56px}
      #ctp-ring{position:fixed;width:160px;height:160px;margin:-80px 0 0 -80px;border:7px solid #ff2d1a;border-radius:50%;box-shadow:0 0 0 10px rgba(255,45,26,.35),0 0 36px rgba(255,45,26,.85),inset 0 0 24px rgba(255,80,40,.25);opacity:0;transform:scale(.4);transition:opacity .12s,transform .22s;z-index:2}
      #ctp-ring.on{opacity:1;transform:scale(1);animation:ctp-pulse 0.85s ease-out infinite}
      #ctp-flash{position:fixed;width:220px;height:220px;margin:-110px 0 0 -110px;border-radius:50%;background:radial-gradient(circle,rgba(255,220,80,.55) 0%,rgba(255,60,30,.35) 35%,rgba(255,40,20,0) 70%);opacity:0;transform:scale(.3);z-index:1;pointer-events:none}
      #ctp-flash.on{animation:ctp-flash 0.9s ease-out infinite}
      @keyframes ctp-pulse{
        0%{box-shadow:0 0 0 8px rgba(255,45,26,.45),0 0 28px rgba(255,45,26,.9),inset 0 0 18px rgba(255,80,40,.3);transform:scale(1)}
        55%{box-shadow:0 0 0 28px rgba(255,45,26,0),0 0 48px rgba(255,45,26,.35),inset 0 0 8px rgba(255,80,40,.1);transform:scale(1.18)}
        100%{box-shadow:0 0 0 8px rgba(255,45,26,.4),0 0 28px rgba(255,45,26,.85),inset 0 0 18px rgba(255,80,40,.25);transform:scale(1)}
      }
      @keyframes ctp-flash{
        0%{opacity:.95;transform:scale(.55)}
        45%{opacity:.55;transform:scale(1.15)}
        100%{opacity:0;transform:scale(1.45)}
      }
      #ctp-hl{position:fixed;border:5px solid #ff2d1a;border-radius:14px;box-shadow:0 0 0 8px rgba(255,45,26,.28),0 0 28px rgba(255,80,40,.55),inset 0 0 0 999px rgba(255,60,30,.12);opacity:0;transition:opacity .18s;pointer-events:none;z-index:1}
      #ctp-hl.on{opacity:1;animation:ctp-hl-blink 1s ease-in-out infinite}
      @keyframes ctp-hl-blink{0%,100%{box-shadow:0 0 0 8px rgba(255,45,26,.28),0 0 28px rgba(255,80,40,.55)}50%{box-shadow:0 0 0 14px rgba(255,45,26,.4),0 0 40px rgba(255,200,60,.65)}}
      #ctp-caption{position:fixed;left:50%;bottom:42px;transform:translateX(-50%);max-width:78%;padding:14px 28px;border-radius:999px;background:rgba(8,12,24,.92);color:#fff;font-size:32px;line-height:1.35;letter-spacing:.02em;box-shadow:0 8px 28px rgba(0,0,0,.5),0 0 0 3px rgba(255,45,26,.35);opacity:0;transition:opacity .2s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;z-index:4}
      #ctp-caption.on{opacity:1}
      #ctp-caption b{color:#ffd28a;font-weight:700}
    </style>
    <div id="ctp-hl"></div>
    <div id="ctp-flash"></div>
    <div id="ctp-ring"></div>
    <div id="ctp-cursor"><svg viewBox="0 0 56 56" xmlns="http://www.w3.org/2000/svg"><path d="M8 6 L8 44 L18 34 L26 50 L34 46 L26 30 L42 30 Z" fill="#fff" stroke="#111" stroke-width="2.2" stroke-linejoin="round"/></svg></div>
    <div id="ctp-caption"></div>
  `;
  document.documentElement.appendChild(root);
  const cursor = root.querySelector("#ctp-cursor");
  const ring = root.querySelector("#ctp-ring");
  const flash = root.querySelector("#ctp-flash");
  const hl = root.querySelector("#ctp-hl");
  const cap = root.querySelector("#ctp-caption");
  let hideCapTimer = null;

  function moveTo(x, y) {
    cursor.style.left = x + "px";
    cursor.style.top = y + "px";
    ring.style.left = x + "px";
    ring.style.top = y + "px";
    flash.style.left = x + "px";
    flash.style.top = y + "px";
  }
  function setCaption(text) {
    if (hideCapTimer) clearTimeout(hideCapTimer);
    if (!text) { cap.classList.remove("on"); return; }
    cap.innerHTML = String(text).replace(/【(.+?)】/g, "<b>$1</b>");
    cap.classList.add("on");
  }
  function boxAround(el) {
    if (!el || !el.getBoundingClientRect) return;
    const r = el.getBoundingClientRect();
    const pad = 10;
    hl.style.left = Math.max(0, r.left - pad) + "px";
    hl.style.top = Math.max(0, r.top - pad) + "px";
    hl.style.width = Math.min(window.innerWidth, r.width + pad * 2) + "px";
    hl.style.height = Math.min(window.innerHeight, r.height + pad * 2) + "px";
    hl.classList.add("on");
  }

  window.__chatxPointer = {
    move(x, y) { moveTo(x, y); },
    point(el, caption, ms) {
      if (!el) return Promise.resolve();
      const r = el.getBoundingClientRect();
      const x = r.left + Math.min(r.width * 0.55, Math.max(24, r.width / 2));
      const y = r.top + Math.min(r.height * 0.55, Math.max(24, r.height / 2));
      moveTo(x, y);
      boxAround(el);
      ring.classList.add("on");
      flash.classList.remove("on");
      void flash.offsetWidth;
      flash.classList.add("on");
      setCaption(caption || "");
      const wait = typeof ms === "number" ? ms : 1400;
      return new Promise((resolve) => {
        setTimeout(() => {
          ring.classList.remove("on");
          flash.classList.remove("on");
          resolve();
        }, wait);
      });
    },
    caption(text, holdMs) {
      setCaption(text || "");
      if (holdMs) {
        hideCapTimer = setTimeout(() => cap.classList.remove("on"), holdMs);
      }
    },
    clearHighlight() { hl.classList.remove("on"); ring.classList.remove("on"); flash.classList.remove("on"); },
    hide() { cap.classList.remove("on"); hl.classList.remove("on"); ring.classList.remove("on"); flash.classList.remove("on"); },
  };
  moveTo(960, 540);
})();
