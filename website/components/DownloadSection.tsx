"use client";

import { useEffect, useState } from "react";
import { Apple, AudioLines, BadgeCheck, Download, HardDrive, Monitor, ShieldCheck } from "lucide-react";
import { useLang } from "./LanguageContext";
import Reveal from "./fx/Reveal";
import BeforeAfter from "./fx/BeforeAfter";
import { AudioClip } from "./fx/MediaClips";
import { ENGINE } from "@/lib/engineContent";
import { track } from "@/lib/track";
import { CONTACT_URL, TELEGRAM_DISPLAY } from "@/lib/site";

interface Build {
  os: string;
  ver: string;
  size: string;
  url: string;
  sha256: string;
  filename: string;
  ready: boolean;
}

// 兜底清单：/releases/release_manifest.json（发布脚本生成）不可达时展示。
const FALLBACK: Build[] = [
  {
    os: "Windows 10/11 (x64)",
    ver: "1.0.1",
    size: "41 MB",
    url: "/releases/AvatarHub-Setup-1.0.1.exe",
    sha256: "",
    filename: "AvatarHub-Setup-1.0.1.exe",
    ready: true,
  },
  {
    os: "macOS 12+ (Apple Silicon / Intel)",
    ver: "-",
    size: "",
    url: "",
    sha256: "",
    filename: "",
    ready: false,
  },
];

export default function DownloadSection() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [builds, setBuilds] = useState<Build[]>(FALLBACK);

  useEffect(() => {
    fetch("/releases/release_manifest.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j?.builds?.length) setBuilds(j.builds as Build[]);
      })
      .catch(() => {});
  }, []);

  const steps = zh
    ? [
        "下载对应平台的安装包并安装（Windows 免管理员，按用户安装）。",
        "首次启动按向导选择显卡档位，自动下载所需组件与模型。",
        "在「设置 → 授权」输入密钥激活，或先用 14 天免费试用。",
      ]
    : [
        "Download and install the package for your platform (no admin needed on Windows).",
        "On first launch, the wizard detects your GPU and downloads the required components.",
        "Activate with your license key in Settings → License, or start the 14-day free trial.",
      ];

  return (
    <section className="relative pb-24 pt-32">
      <div className="pointer-events-none absolute left-1/3 top-24 h-80 w-80 rounded-full bg-neon-blue/15 blur-[130px]" />

      <div className="relative mx-auto max-w-5xl px-5">
        <Reveal eager className="text-center">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-neon-cyan/30 bg-neon-cyan/10 px-3 py-1 text-xs text-neon-cyan">
            <ShieldCheck className="h-3.5 w-3.5" />
            {zh ? "薄核心安装包 · 组件按需下载 · SHA-256 可校验" : "Thin-core installer · on-demand components · SHA-256 verifiable"}
          </span>
          <h1 className="mt-4 text-3xl font-bold text-white md:text-5xl">
            {zh ? "下载客户端" : "Download the Client"}
          </h1>
          <p className="mx-auto mt-3 max-w-2xl text-slate-400">
            {zh
              ? "AvatarHub 实时数字人引擎：声音克隆、实时换脸、数字人直播、克隆音同传，本地部署数据不出机房。"
              : "AvatarHub real-time digital human engine: voice cloning, live face swap, digital-human streaming and interpreting — deployed locally, data stays on-prem."}
          </p>
        </Reveal>

        <div className="mt-12 grid gap-6 md:grid-cols-2">
          {builds.map((b, i) => {
            const mac = /mac/i.test(b.os);
            return (
              <Reveal key={b.os} delay={i * 0.08}>
                <div className="glass flex h-full flex-col rounded-2xl border border-white/10 p-6">
                  <div className="flex items-center gap-3">
                    {mac ? <Apple className="h-8 w-8 text-slate-300" /> : <Monitor className="h-8 w-8 text-neon-cyan" />}
                    <div>
                      <div className="font-semibold text-white">{b.os}</div>
                      <div className="text-xs text-slate-500">
                        {b.ready
                          ? `${zh ? "版本" : "Version"} v${b.ver}${b.size ? ` · ${b.size}` : ""}`
                          : zh
                            ? "即将上线 · 轻量控制台 / 远程接入"
                            : "Coming soon · lightweight console / remote access"}
                      </div>
                    </div>
                  </div>

                  <div className="mt-5 flex-1">
                    {b.ready && b.url ? (
                      <a
                        href={b.url}
                        download
                        onClick={() => track("download_click", { os: b.os, ver: b.ver })}
                        className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet px-6 py-2.5 text-sm font-medium text-ink-950 transition hover:opacity-90"
                      >
                        <Download className="h-4 w-4" />
                        {zh ? "下载" : "Download"} {b.filename}
                      </a>
                    ) : b.ready ? (
                      <span className="inline-block rounded-full border border-white/15 px-6 py-2.5 text-sm text-slate-500">
                        {zh ? "下载链接待发布" : "Link pending"}
                      </span>
                    ) : (
                      <a
                        href={CONTACT_URL}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-block rounded-full border border-neon-violet/40 px-6 py-2.5 text-sm text-neon-violet transition hover:bg-neon-violet/10"
                      >
                        {zh ? "上线后通知我" : "Notify me"}
                      </a>
                    )}
                  </div>

                  {b.sha256 && (
                    <div className="mt-4 break-all rounded-lg bg-ink-950/60 px-3 py-2 font-mono text-[11px] text-slate-600">
                      SHA-256: {b.sha256}
                    </div>
                  )}
                  {mac && (
                    <p className="mt-3 text-xs leading-relaxed text-slate-500">
                      {zh
                        ? "Mac 版为轻量控制台 / 远程接入：换脸、数字人等重推理需 Windows / 服务器 N 卡，Mac 端连接远程引擎使用。"
                        : "The Mac build is a lightweight console: heavy inference (face swap, digital human) runs on a Windows / server GPU; the Mac connects remotely."}
                    </p>
                  )}
                </div>
              </Reveal>
            );
          })}
        </div>

        {/* 真实效果试听/对比：下载前先看引擎实际产出（复用首页实证素材，单一真相） */}
        <Reveal className="mt-12">
          <div className="grid gap-6 md:grid-cols-2">
            <div className="glass flex flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-2 font-semibold text-white">
                <AudioLines className="h-5 w-5 text-neon-cyan" />
                {zh ? "克隆音色 · 真实合成试听" : "Cloned voice · real synthesis samples"}
              </div>
              <p className="mt-1 text-xs text-slate-500">
                {zh ? "同一克隆音色的多语种真实产出，非演员配音。" : "Real multi-language output from one cloned voice — no voice actors."}
              </p>
              <div className="mt-4 space-y-2.5">
                {ENGINE.proof.audioClips.slice(0, 3).map((clip) => (
                  <AudioClip key={clip.src} label={clip.label[lang]} src={clip.src} />
                ))}
              </div>
            </div>
            <div className="glass flex flex-col rounded-2xl border border-white/10 p-6">
              <div className="flex items-center gap-2 font-semibold text-white">
                <BadgeCheck className="h-5 w-5 text-emerald-400" />
                {zh ? "实时换脸 · 前后对比" : "Live face swap · before / after"}
              </div>
              <p className="mt-1 text-xs text-slate-500">{ENGINE.proof.swapDesc[lang]}</p>
              <div className="mt-4">
                <BeforeAfter
                  before={ENGINE.proof.swapBefore}
                  after={ENGINE.proof.swapAfter}
                  beforeLabel={ENGINE.proof.beforeLabel[lang]}
                  afterLabel={ENGINE.proof.afterLabel[lang]}
                  hint={ENGINE.proof.dragHint[lang]}
                  aspectClass="aspect-[11/16]"
                  className="mx-auto max-w-[240px]"
                />
              </div>
            </div>
          </div>
        </Reveal>

        {/* 安装步骤 */}
        <Reveal className="mt-12">
          <div className="glass rounded-2xl border border-white/10 p-6">
            <div className="flex items-center gap-2 font-semibold text-white">
              <HardDrive className="h-5 w-5 text-neon-cyan" />
              {zh ? "三步开始使用" : "Get started in 3 steps"}
            </div>
            <ol className="mt-4 space-y-3">
              {steps.map((s, i) => (
                <li key={i} className="flex items-start gap-3 text-sm text-slate-300">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-neon-cyan to-neon-violet text-xs font-bold text-ink-950">
                    {i + 1}
                  </span>
                  {s}
                </li>
              ))}
            </ol>
            <p className="mt-5 text-xs text-slate-500">
              {zh ? (
                <>推荐配置：NVIDIA 8GB+ 显存（实时换脸 / 数字人）；仅语音克隆 4GB 起。安装遇到问题？<a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">联系 {TELEGRAM_DISPLAY}</a> 预约 99 USDT 远程代部署，装好即用。</>
              ) : (
                <>Recommended: NVIDIA GPU with 8 GB+ VRAM (live swap / digital human); 4 GB for voice-only. Trouble installing? <a className="text-neon-cyan hover:underline" href={CONTACT_URL} target="_blank" rel="noreferrer">Contact {TELEGRAM_DISPLAY}</a> for the 99 USDT remote install service.</>
              )}
            </p>
          </div>
        </Reveal>

        <Reveal className="mt-10 text-center">
          <a href={zh ? "/order" : "/en/order"} className="text-sm text-neon-cyan hover:underline">
            {zh ? "查看套餐与价格，选择适合你的授权 →" : "See plans & pricing →"}
          </a>
        </Reveal>
      </div>
    </section>
  );
}
