"use client";

import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { MessageSquare, X, Send, Bot, Sparkles, CheckCircle2, Globe } from "lucide-react";
import { useLang } from "./LanguageContext";
import { useTelegram } from "./TelegramProvider";
import { cleanMarkdown } from "@/lib/clean-markdown";
import { CONTACT_URL } from "@/lib/site";
import { track } from "@/lib/track";
import { detectLang } from "@/lib/detect-lang";
import { getSession, setSession } from "@/lib/safe-storage";
import { getLeadUtm } from "@/lib/attribution";

type Msg = { role: "user" | "assistant"; content: string };

const INTENT = /价格|多少钱|报价|购买|下单|怎么收费|套餐|合作|定制|price|cost|buy|order|quote|pricing|plan|deploy/i;

/** 外部唤起 AI 客服的自定义事件名（detail.scenario 可指定进入场景，如 "install"） */
export const OPEN_CHAT_EVENT = "boundless:open-ai-chat";

/**
 * 从任意组件打开全局 AI 客服。
 * @param scenario 进入场景："install" = 安装协助（下载页按钮用），不传为默认售前场景
 */
export function openAiChat(scenario?: "install") {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: { scenario } }));
}

const COPY = {
  zh: {
    title: "AI 在线客服",
    sub: "由 AI 自动成交聊天系统驱动",
    greet: "你好 👋 我是无界科技 BOUNDLESS 的 AI 客服。换脸、克隆声音、直播换脸换声、实时换语言、AI 自动成交、私有部署、价格——都可以问我。\n（支持任意语言：用你客户的母语问我试试 🌍）",
    placeholder: "任意语言输入你的问题…",
    suggestions: ["AI 自动成交怎么收费？", "实时换脸支持视频通话吗？", "¿Cuánto cuesta el chat con IA?"],
    leave: "留个联系方式，让客服联系我",
    disclaimer: "AI 回答仅供参考，详情以客服确认为准。",
    error: "网络繁忙，请稍后重试或点下方留资。",
    leadPrompt: "想要专属方案 / 报价？留个联系方式，客服 5 分钟内联系你 👇",
    contactPh: "Telegram / WhatsApp / 邮箱",
    leadSubmit: "提交",
    leadOk: "已收到，马上联系你 ✅",
    teaser: "在找出海获客方案？问我 AI 自动成交怎么帮你多赚 👋",
    human: "转人工客服",
    replyIn: "AI 将用此语言实时回复",
    installGreet:
      "已进入安装协助模式 🛠 我可以一步步带你完成 AvatarHub 的下载与安装：从下载安装包、首次启动向导、组件下载，到激活试用。\n遇到报错把提示原文发给我就行；也可以先点下面的常见问题。",
    installSuggestions: ["完整的安装步骤是什么？", "SmartScreen 拦截了安装包怎么办？", "我的显卡能跑哪些功能？", "组件下载中断了怎么办？"],
  },
  en: {
    title: "AI Live Support",
    sub: "Powered by our AI auto-closing chat",
    greet: "Hi 👋 I'm BOUNDLESS's AI agent. Ask me about face swap, voice cloning, live face/voice swap, live translation, AI auto-closing, private deployment, pricing.\n(Any language works — try your customer's native tongue 🌍)",
    placeholder: "Type in any language…",
    suggestions: ["How is AI closing priced?", "Does live swap work on video calls?", "¿Cuánto cuesta el chat con IA?"],
    leave: "Leave my contact for support",
    disclaimer: "AI answers are for reference; confirm details with support.",
    error: "Busy now, please retry later or leave your contact below.",
    leadPrompt: "Want a tailored plan / quote? Leave your contact and we'll reach you in ~5 min 👇",
    contactPh: "Telegram / WhatsApp / email",
    leadSubmit: "Submit",
    leadOk: "Got it — reaching out shortly ✅",
    teaser: "Scaling cross-border sales? Ask how AI auto-closing earns you more 👋",
    human: "Talk to a human",
    replyIn: "AI replies live in this language",
    installGreet:
      "Install-assist mode 🛠 I'll walk you through downloading and installing AvatarHub: the installer, the first-run wizard, component downloads and activation.\nHit an error? Paste the exact message here — or start with a common question below.",
    installSuggestions: ["What are the full install steps?", "SmartScreen blocked the installer — what now?", "What can my GPU run?", "Component download got interrupted?"],
  },
  // 小语种落地页（/ko /ja）专用界面文案；AI 回复语言由后端「语言镜像」指令保证。
  ko: {
    title: "AI 실시간 상담",
    sub: "AI 자동 성사 채팅 시스템 구동",
    greet:
      "안녕하세요 👋 BOUNDLESS의 AI 상담원입니다. 음성 클로닝, 페이스 스왑, 라이브 실시간 변환, 실시간 통역, AI 자동 성사, 온프레미스 도입, 요금 — 무엇이든 물어보세요.\n(어떤 언어로도 OK — 한국어로 편하게 질문하세요 🌍)",
    placeholder: "질문을 입력하세요…",
    suggestions: ["음성 클로닝은 한국어를 지원하나요?", "AI 자동 성사 요금은 어떻게 되나요?", "온프레미스 도입은 어떻게 진행되나요?"],
    leave: "연락처 남기고 상담 요청",
    disclaimer: "AI 답변은 참고용이며, 상세 내용은 상담원 확인 기준입니다.",
    error: "지금 접속이 많습니다. 잠시 후 다시 시도하시거나 아래에 연락처를 남겨주세요.",
    leadPrompt: "맞춤 플랜 / 견적이 필요하신가요? 연락처를 남기시면 5분 내 연락드립니다 👇",
    contactPh: "Telegram / WhatsApp / 이메일",
    leadSubmit: "보내기",
    leadOk: "접수되었습니다. 곧 연락드리겠습니다 ✅",
    teaser: "음성 클로닝이 궁금하세요? 요금과 도입 방법을 물어보세요 👋",
    human: "상담원 연결",
    replyIn: "AI가 이 언어로 실시간 답변합니다",
    installGreet:
      "설치 지원 모드입니다 🛠 AvatarHub 다운로드와 설치를 단계별로 도와드립니다. 오류 메시지를 그대로 붙여넣어 주세요.",
    installSuggestions: ["전체 설치 단계는?", "SmartScreen이 설치를 차단했어요", "제 GPU로 어떤 기능을 쓸 수 있나요?"],
  },
  ja: {
    title: "AIライブサポート",
    sub: "AI自動成約チャットシステム搭載",
    greet:
      "こんにちは 👋 BOUNDLESSのAIエージェントです。音声クローン、フェイススワップ、ライブ配信のリアルタイム変換、リアルタイム通訳、AI自動成約、オンプレミス導入、料金——何でもお聞きください。\n（どの言語でもOK——日本語でお気軽にどうぞ 🌍）",
    placeholder: "ご質問を入力してください…",
    suggestions: ["音声クローンは日本語に対応していますか？", "AI自動成約の料金は？", "オンプレミス導入の流れは？"],
    leave: "連絡先を残して相談する",
    disclaimer: "AIの回答は参考情報です。詳細はサポートにご確認ください。",
    error: "混み合っています。しばらくしてから再試行するか、下記に連絡先をご記入ください。",
    leadPrompt: "最適なプラン / お見積りをご希望ですか？連絡先をご記入いただければ約5分でご連絡します 👇",
    contactPh: "Telegram / WhatsApp / メール",
    leadSubmit: "送信",
    leadOk: "承りました。まもなくご連絡します ✅",
    teaser: "音声クローンにご興味は？料金や導入方法をお尋ねください 👋",
    human: "担当者に相談",
    replyIn: "AIがこの言語でリアルタイム回答",
    installGreet:
      "インストール支援モードです 🛠 AvatarHubのダウンロードからインストールまでステップごとにご案内します。エラーはそのまま貼り付けてください。",
    installSuggestions: ["インストール手順の全体は？", "SmartScreenにブロックされました", "私のGPUで使える機能は？"],
  },
};

/** 页面语言：小语种落地页优先（/ko /ja），否则跟随全站 zh/en 字典。 */
function pageLocaleOf(pathname: string | null): "ko" | "ja" | null {
  if (!pathname) return null;
  if (pathname === "/ko" || pathname.startsWith("/ko/")) return "ko";
  if (pathname === "/ja" || pathname.startsWith("/ja/")) return "ja";
  return null;
}

export default function AIChat() {
  const { lang } = useLang();
  const { isMiniApp } = useTelegram();
  const pathname = usePathname();
  const pageLocale = pageLocaleOf(pathname);
  const c = COPY[pageLocale ?? lang];
  // 传给后端的语言提示：小语种页直接声明页面语言，让首答就是访客语言
  const chatLang = pageLocale ?? lang;

  const [open, setOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [userTurns, setUserTurns] = useState(0);
  const [showLead, setShowLead] = useState(false);
  const [leadContact, setLeadContact] = useState("");
  const [leadDone, setLeadDone] = useState(false);
  const [teaser, setTeaser] = useState(false);
  const [scenario, setScenario] = useState<"default" | "install">("default");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [msgs, busy, showLead]);

  // 外部唤起（如下载页「AI 协助安装」按钮）：打开窗口并切换到对应场景
  useEffect(() => {
    function onOpenEvent(e: Event) {
      const detail = (e as CustomEvent<{ scenario?: string }>).detail;
      setOpen(true);
      setTeaser(false);
      setSession("yt-teaser", "1");
      if (detail?.scenario === "install") {
        setScenario("install");
        track("ai_chat_open", { from: "install_assist" });
      } else {
        track("ai_chat_open", { from: "external" });
      }
    }
    window.addEventListener(OPEN_CHAT_EVENT, onOpenEvent);
    return () => window.removeEventListener(OPEN_CHAT_EVENT, onOpenEvent);
  }, []);

  // 飞行机器人（AISprite）/ 全息播报点击 → 打开真实 AI 客服；也可被其它入口复用。
  // detail.seed 为入口带来的种子问题：全息播报的 CTA 是「点我 · 立即咨询」，
  // 没有历史对话时直接代发种子问题（点击即得到答案，履行 CTA 承诺）；
  // 已有对话或正忙时退化为预填输入框，绝不打断进行中的会话。
  const sendRef = useRef<(text: string) => void>(() => {});
  const chatStateRef = useRef({ msgCount: 0, busy: false });
  chatStateRef.current = { msgCount: msgs.length, busy };
  useEffect(() => {
    const onOpen = (e: Event) => {
      const detail = (e as CustomEvent).detail as { from?: string; seed?: string } | undefined;
      setOpen(true);
      setTeaser(false);
      const seed = detail?.seed ? String(detail.seed).slice(0, 200) : "";
      if (seed) {
        const { msgCount, busy: chatBusy } = chatStateRef.current;
        if (detail?.from === "hologram" && msgCount === 0 && !chatBusy) {
          track("ai_chat_seed_autosend");
          sendRef.current(seed);
        } else {
          setInput(seed);
        }
      }
      track("ai_chat_open", { from: detail?.from ?? "sprite" });
    };
    window.addEventListener("bl:open-chat", onOpen as EventListener);
    return () => window.removeEventListener("bl:open-chat", onOpen as EventListener);
  }, []);

  // 保持 sendRef 指向最新的 send（事件监听器只挂载一次，避免闭包过期）
  useEffect(() => {
    sendRef.current = send;
  });

  // proactive greeting: once per session, only on web, after dwell
  useEffect(() => {
    if (isMiniApp) return;
    if (typeof window === "undefined") return;
    if (getSession("yt-teaser") === "1") return;
    const id = setTimeout(() => {
      if (!open) {
        setTeaser(true);
        track("ai_chat_teaser");
      }
    }, 18000);
    return () => clearTimeout(id);
  }, [isMiniApp, open]);

  function dismissTeaser() {
    setTeaser(false);
    setSession("yt-teaser", "1");
  }

  async function send(text: string) {
    const q = text.trim();
    if (!q || busy) return;
    setInput("");
    const baseHistory = msgs.slice(-6);
    const next: Msg[] = [...msgs, { role: "user", content: q }];
    setMsgs(next);
    setBusy(true);
    setStreaming(true);
    track("ai_chat", { len: q.length });

    const turns = userTurns + 1;
    setUserTurns(turns);
    const intent = INTENT.test(q);

    // add empty assistant message to stream into
    setMsgs((m) => [...m, { role: "assistant", content: "" }]);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: q, lang: chatLang, history: baseHistory }),
      });
      if (!res.ok || !res.body) throw new Error("bad");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";
      setStreaming(false); // first byte → stop the "thinking" dots
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        const display = acc;
        setMsgs((m) => {
          const copy = [...m];
          copy[copy.length - 1] = { role: "assistant", content: display };
          return copy;
        });
      }
    } catch {
      setMsgs((m) => {
        const copy = [...m];
        copy[copy.length - 1] = { role: "assistant", content: c.error };
        return copy;
      });
    } finally {
      setBusy(false);
      setStreaming(false);
      // conversation → lead: trigger on buy-intent or after 2 turns
      if (!leadDone && (intent || turns >= 2)) {
        setShowLead(true);
        track("ai_chat_lead_prompt", { intent, turns });
      }
    }
  }

  async function submitLead() {
    const contact = leadContact.trim();
    if (!contact) return;
    const recent = msgs.filter((m) => m.role === "user").slice(-3).map((m) => m.content).join(" | ");
    try {
      await fetch("/api/lead", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          contact,
          interest: chatLang === "zh" ? "AI 在线咨询" : "AI live chat",
          message: recent,
          lang: chatLang,
          source: "ai_chat",
          utm: getLeadUtm(),
          path: typeof window !== "undefined" ? window.location.pathname : "",
        }),
      });
      setLeadDone(true);
      setShowLead(false);
      track("lead_submit", { source: "ai_chat" });
    } catch {
      /* keep form open on failure */
    }
  }

  function goLead() {
    setOpen(false);
    const el = document.getElementById("contact");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    setTimeout(() => document.querySelector<HTMLInputElement>("#lead-contact")?.focus(), 500);
  }

  // hide on mobile inside mini app to avoid covering TG MainButton
  const hideLauncher = isMiniApp;

  return (
    <>
      {/* proactive teaser */}
      <AnimatePresence>
        {teaser && !open && !hideLauncher && (
          <motion.div
            initial={{ opacity: 0, y: 12, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.95 }}
            className="fixed bottom-36 right-4 z-50 w-[min(260px,calc(100vw-2rem))] rounded-2xl rounded-br-sm border border-neon-cyan/30 bg-ink-900/95 p-3 shadow-2xl backdrop-blur lg:bottom-20 lg:right-5"
          >
            <button
              onClick={dismissTeaser}
              aria-label="dismiss"
              className="absolute -right-1.5 -top-1.5 grid h-5 w-5 place-items-center rounded-full bg-ink-800 text-slate-400 ring-1 ring-white/10 hover:text-white"
            >
              <X className="h-3 w-3" />
            </button>
            <button
              onClick={() => {
                setOpen(true);
                dismissTeaser();
                track("ai_chat_open", { from: "teaser" });
              }}
              className="flex items-start gap-2 text-left"
            >
              <span className="grid h-7 w-7 shrink-0 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950">
                <Bot className="h-4 w-4" />
              </span>
              <span className="text-xs leading-relaxed text-slate-200">{c.teaser}</span>
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {!hideLauncher && (
        <div className="group fixed bottom-20 right-4 z-50 lg:bottom-5 lg:right-5" data-robot-avoid="true">
          {/* 悬停「在线客服」标签 */}
          {!open && (
            <span className="pointer-events-none absolute right-16 top-1/2 hidden -translate-y-1/2 whitespace-nowrap rounded-full border border-neon-cyan/30 bg-ink-900/90 px-3 py-1.5 text-xs font-medium text-neon-cyan opacity-0 shadow-lg backdrop-blur transition-all duration-300 group-hover:opacity-100 lg:block">
              {c.title} · {lang === "zh" ? "在线" : "online"}
            </span>
          )}
          <button
            onClick={() => {
              setOpen((v) => !v);
              dismissTeaser();
              track("ai_chat_open");
            }}
            aria-label="AI chat"
            className="relative grid h-14 w-14 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950 shadow-lg shadow-neon-cyan/40 transition-transform duration-300 hover:scale-110 active:scale-95"
          >
            {/* 旋转 conic 光环 */}
            {!open && (
              <span
                className="pointer-events-none absolute -inset-[3px] rounded-full opacity-70"
                style={{
                  background: "conic-gradient(from 0deg, transparent 0deg, #22d3ee 90deg, transparent 160deg, #8b5cf6 250deg, transparent 320deg)",
                  animation: "bl-spin 3.2s linear infinite",
                  WebkitMask: "radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px))",
                  mask: "radial-gradient(farthest-side, transparent calc(100% - 3px), #000 calc(100% - 3px))",
                }}
              />
            )}
            {/* 呼吸涟漪 */}
            {!open && <span className="pointer-events-none absolute inset-0 animate-ping rounded-full bg-neon-cyan/25" style={{ animationDuration: "2.4s" }} />}
            {open ? <X className="relative h-6 w-6" /> : <MessageSquare className="relative h-6 w-6" />}
            {!open && <span className="absolute -right-0.5 -top-0.5 h-3.5 w-3.5 animate-pulse rounded-full bg-emerald-400 ring-2 ring-ink-950" />}
          </button>
        </div>
      )}

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: 24, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 24, scale: 0.96 }}
            transition={{ duration: 0.2 }}
            className="fixed bottom-36 right-4 z-50 flex h-[min(520px,64vh)] w-[min(380px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-white/10 bg-ink-900/95 shadow-2xl backdrop-blur lg:bottom-24 lg:right-5"
          >
            {/* header */}
            <div className="flex items-center gap-3 border-b border-white/10 bg-gradient-to-r from-neon-cyan/15 to-neon-violet/15 px-4 py-3">
              <span className="grid h-9 w-9 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950">
                <Bot className="h-5 w-5" />
              </span>
              <div className="flex-1">
                <div className="text-sm font-semibold text-white">{c.title}</div>
                <div className="flex items-center gap-1 text-[11px] text-emerald-300">
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  {c.sub}
                </div>
              </div>
              <button onClick={() => setOpen(false)} aria-label="close" className="text-slate-400 hover:text-white">
                <X className="h-5 w-5" />
              </button>
            </div>

            {/* messages */}
            <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-4">
              <Bubble role="assistant">{scenario === "install" ? c.installGreet : c.greet}</Bubble>

              {msgs.length === 0 && (
                <div className="space-y-2 pt-1">
                  {(scenario === "install" ? c.installSuggestions : c.suggestions).map((s) => (
                    <button
                      key={s}
                      onClick={() => send(s)}
                      className="block w-full rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-left text-xs text-slate-300 transition hover:border-neon-cyan/40 hover:text-white"
                    >
                      <Sparkles className="mr-1.5 inline h-3 w-3 text-neon-cyan" />
                      {s}
                    </button>
                  ))}
                </div>
              )}

              {msgs.map((m, i) => {
                if (m.role === "assistant" && !m.content) return null;
                return (
                  <Bubble key={i} role={m.role}>
                    {m.role === "assistant" ? cleanMarkdown(m.content) : m.content}
                  </Bubble>
                );
              })}

              {streaming && (
                <div className="mr-auto flex items-center gap-1.5 rounded-2xl rounded-tl-sm border border-white/10 bg-ink-800/80 px-3 py-2.5">
                  {[0, 1, 2].map((i) => (
                    <motion.span
                      key={i}
                      className="h-1.5 w-1.5 rounded-full bg-neon-cyan"
                      animate={{ opacity: [0.3, 1, 0.3] }}
                      transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.18 }}
                    />
                  ))}
                </div>
              )}

              {/* conversation → lead capture */}
              <AnimatePresence>
                {showLead && !leadDone && (
                  <motion.div
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    className="rounded-2xl border border-neon-cyan/30 bg-neon-cyan/[0.06] p-3"
                  >
                    <p className="text-xs text-slate-200">{c.leadPrompt}</p>
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void submitLead();
                      }}
                      className="mt-2 flex gap-2"
                    >
                      <input
                        value={leadContact}
                        onChange={(e) => setLeadContact(e.target.value)}
                        placeholder={c.contactPh}
                        maxLength={200}
                        className="flex-1 rounded-lg border border-white/10 bg-ink-950/60 px-3 py-2 text-xs text-white placeholder:text-slate-500 outline-none focus:border-neon-cyan/50"
                      />
                      <button
                        type="submit"
                        disabled={!leadContact.trim()}
                        className="rounded-lg bg-gradient-to-r from-neon-cyan to-neon-violet px-3 py-2 text-xs font-semibold text-ink-950 disabled:opacity-50"
                      >
                        {c.leadSubmit}
                      </button>
                    </form>
                  </motion.div>
                )}
              </AnimatePresence>

              {leadDone && (
                <div className="flex items-center justify-center gap-1.5 rounded-xl border border-emerald-400/30 bg-emerald-400/10 px-3 py-2 text-xs text-emerald-300">
                  <CheckCircle2 className="h-4 w-4" />
                  {c.leadOk}
                </div>
              )}
            </div>

            {/* footer */}
            <div className="border-t border-white/10 p-3">
              <div className="mb-2 flex gap-2">
                <button
                  onClick={goLead}
                  className="flex-1 rounded-lg border border-neon-cyan/30 bg-neon-cyan/5 py-1.5 text-xs font-medium text-neon-cyan transition hover:bg-neon-cyan/10"
                >
                  {c.leave}
                </button>
                <a
                  href={CONTACT_URL}
                  target="_blank"
                  rel="noreferrer"
                  onClick={() => track("cta_click", { where: "ai_chat_human" })}
                  className="rounded-lg border border-white/10 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-white/30 hover:text-white"
                >
                  {c.human}
                </a>
              </div>
              {input.trim() &&
                (() => {
                  const d = detectLang(input);
                  return d.code ? (
                    <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-medium text-neon-cyan">
                      <Globe className="h-3 w-3" />
                      <span>
                        {d.native} · {c.replyIn}
                      </span>
                    </div>
                  ) : null;
                })()}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  send(input);
                }}
                className="flex items-center gap-2"
              >
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder={c.placeholder}
                  maxLength={1000}
                  className="flex-1 rounded-full border border-white/10 bg-ink-950/60 px-4 py-2.5 text-sm text-white placeholder:text-slate-500 outline-none focus:border-neon-cyan/50"
                />
                <button
                  type="submit"
                  disabled={busy || !input.trim()}
                  aria-label="send"
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-gradient-to-br from-neon-cyan to-neon-violet text-ink-950 disabled:opacity-50"
                >
                  <Send className="h-4 w-4" />
                </button>
              </form>
              <p className="mt-1.5 text-center text-[10px] text-slate-500">{c.disclaimer}</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

function Bubble({ role, children }: { role: "user" | "assistant"; children: React.ReactNode }) {
  const out = role === "user";
  return (
    <div
      className={`max-w-[88%] whitespace-pre-wrap rounded-2xl px-3.5 py-2.5 text-sm ${
        out
          ? "ml-auto rounded-tr-sm bg-gradient-to-r from-neon-cyan to-neon-violet text-ink-950"
          : "mr-auto rounded-tl-sm border border-white/10 bg-ink-800/80 text-slate-100"
      }`}
    >
      {children}
    </div>
  );
}
