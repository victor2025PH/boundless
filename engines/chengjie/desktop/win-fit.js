"use strict";

// win-fit.js -- 小逻辑桌面自适配 + 显示指纹面包屑（2026-08-17 .173 事故根治件）。
//
// 事故：4K 面板按 Windows「推荐」300% 缩放 → 逻辑桌面 1280x720，比壳的固定
// 1280x820 还矮，composer 工具栏（AI回复/语音/媒体/翻译）永久在折叠线下，
// 坐席看起来「按钮全没了」，零代码故障。两件事在这收口：
//   1) fitWindowBounds：主窗尺寸按工作区收敛，放不下则最大化（纯函数，可测）；
//   2) display-metrics.json 面包屑：把 Chromium 实测的分辨率/缩放落到 userData，
//      供 deploy/desktop/_seat_disp_probe.ps1（fleet 台账 display 列 + push_chatx
//      装后检查）远程读取——SSH 会话里所有会话级显示 API 都撒谎（WinForms 见
//      幻影 1024x768、QueryDisplayConfig 0 路径，2026-08-17 于 .173 实测），
//      壳自己上报是唯一的精确通道。
// 全部 best-effort：任何失败不影响启动、不参与业务。

const fs = require("fs");
const path = require("path");

// 纯函数：按工作区收敛期望窗口尺寸。maximize=true 表示工作区任一维放不下
// 期望值——小逻辑桌面上最大化比 clamp 后的悬浮窗多出全部可用面积，
// 是坐席实际想要的形态（clamp 仅兜「maximize 之前的首帧」与还原态）。
function fitWindowBounds(workArea, desired) {
  const dw = Math.max(1, (desired && desired.width) || 1280);
  const dh = Math.max(1, (desired && desired.height) || 820);
  const ww = Math.max(1, (workArea && workArea.width) || dw);
  const wh = Math.max(1, (workArea && workArea.height) || dh);
  return {
    width: Math.min(dw, ww),
    height: Math.min(dh, wh),
    maximize: ww < dw || wh < dh,
  };
}

// 纯函数：从 Electron display 对象提炼面包屑（喂假 display 即可测）。
// scalePct 是探针消费的主键；physW/physH 供探针与当前 GPU 模式核对防陈旧
// （拔了显示器留下的旧面包屑不许赢）。
function displayBreadcrumb(display, extra) {
  const d = display || {};
  const size = d.size || {};
  const wa = d.workAreaSize || {};
  const sf = Number(d.scaleFactor) > 0 ? Number(d.scaleFactor) : 1;
  const lw = Math.round(size.width || 0);
  const lh = Math.round(size.height || 0);
  return Object.assign(
    {
      v: 1,
      ts: new Date().toISOString(),
      logicalW: lw,
      logicalH: lh,
      scaleFactor: sf,
      scalePct: Math.round(sf * 100),
      physW: Math.round(lw * sf),
      physH: Math.round(lh * sf),
      workW: Math.round(wa.width || 0),
      workH: Math.round(wa.height || 0),
    },
    extra || {}
  );
}

function _write(app, screen) {
  try {
    const bc = displayBreadcrumb(screen.getPrimaryDisplay(), {
      displays: screen.getAllDisplays().length,
    });
    fs.writeFileSync(
      path.join(app.getPath("userData"), "display-metrics.json"),
      JSON.stringify(bc),
      "utf8"
    );
  } catch (e) {
    /* 面包屑不参与业务，静默 */
  }
}

// app ready 后调用一次：立即落盘 + 订阅显示变化（改缩放/插拔显示器即刷新，
// fleet 下一轮巡检就能读到新值，不等壳重启）。
function installDisplayBreadcrumb(app, screen) {
  _write(app, screen);
  try {
    screen.on("display-metrics-changed", () => _write(app, screen));
    screen.on("display-added", () => _write(app, screen));
    screen.on("display-removed", () => _write(app, screen));
  } catch (e) {
    /* 订阅失败只失去自动刷新，首帧值仍在 */
  }
}

module.exports = { fitWindowBounds, displayBreadcrumb, installDisplayBreadcrumb };
