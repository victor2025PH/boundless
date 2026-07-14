"use client";

import { useEffect } from "react";

/** 视频聚光模式:任意 <video> 播放时给 <html> 挂 fx-video,背景动效深度调暗
 *  (样式见 globals.css),播放结束/暂停自动恢复。媒体事件不冒泡,用捕获阶段
 *  全局监听,站内现有与未来新增的视频零接入成本。 */
export default function VideoSpotlight() {
  useEffect(() => {
    const active = new Set<HTMLVideoElement>();
    const sync = () => {
      // 视频节点若被卸载(无 pause 事件)则剔除,避免调暗状态残留
      for (const el of active) if (!el.isConnected) active.delete(el);
      document.documentElement.classList.toggle("fx-video", active.size > 0);
    };
    const onPlay = (e: Event) => {
      if (e.target instanceof HTMLVideoElement) {
        active.add(e.target);
        sync();
      }
    };
    const onStop = (e: Event) => {
      if (e.target instanceof HTMLVideoElement) {
        active.delete(e.target);
        sync();
      }
    };
    document.addEventListener("play", onPlay, true);
    document.addEventListener("pause", onStop, true);
    document.addEventListener("emptied", onStop, true);
    return () => {
      document.removeEventListener("play", onPlay, true);
      document.removeEventListener("pause", onStop, true);
      document.removeEventListener("emptied", onStop, true);
      document.documentElement.classList.remove("fx-video");
    };
  }, []);

  return null;
}
