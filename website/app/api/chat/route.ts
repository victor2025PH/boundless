import { NextRequest, NextResponse } from "next/server";
import { streamDeepSeek, deepseekEnabled, type ChatTurn } from "@/lib/deepseek";
import { matchFreeText, buildFallback, detectKnowledgeLang, type BotLang } from "@/lib/bot-knowledge";
import { cleanMarkdown } from "@/lib/clean-markdown";
import { logChat, dailyGuard } from "@/lib/chat-log";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// naive in-memory per-IP limiter (resets on redeploy)
const hits = new Map<string, { n: number; ts: number }>();
const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 15;

function limited(ip: string) {
  const now = Date.now();
  const cur = hits.get(ip);
  if (!cur || now - cur.ts > WINDOW_MS) {
    hits.set(ip, { n: 1, ts: now });
    return false;
  }
  cur.n += 1;
  return cur.n > MAX_PER_WINDOW;
}

function textStream(text: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      controller.enqueue(enc.encode(text));
      controller.close();
    },
  });
}

export async function POST(req: NextRequest) {
  const ip =
    req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    req.headers.get("x-real-ip") ||
    "anon";
  if (limited(ip)) {
    return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
  }

  let body: { message?: string; lang?: string; history?: ChatTurn[]; kb?: boolean };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }

  const message = String(body?.message ?? "").trim().slice(0, 1000);
  if (!message) {
    return NextResponse.json({ ok: false, error: "empty" }, { status: 400 });
  }
  // grounding language follows the message text so non-CJK users get en facts
  // (the system prompt mirrors the user's actual language for output)
  const lang: BotLang = detectKnowledgeLang(message);
  // 小语种落地页（/ko /ja）访客：输入语言不明确时默认用页面语言回复
  const uiLang = String(body?.lang ?? "");
  const PAGE_HINTS: Record<string, string> = {
    ko: "Page context: the visitor is on the Korean landing page (/ko/voice). If their language is ambiguous (numbers, brand names, short Latin fragments), reply in Korean by default. Clear language mirroring still takes priority.",
    ja: "Page context: the visitor is on the Japanese landing page (/ja/voice). If their language is ambiguous (numbers, brand names, short Latin fragments), reply in Japanese by default. Clear language mirroring still takes priority.",
  };
  const extraSystem = PAGE_HINTS[uiLang] ?? "";
  const history: ChatTurn[] = Array.isArray(body?.history)
    ? body!.history!
        .filter((h) => (h.role === "user" || h.role === "assistant") && typeof h.content === "string")
        .map((h) => ({ role: h.role, content: String(h.content).slice(0, 800) }))
        .slice(-6)
    : [];

  // AI 不可用（熔断/日限）时的小语种兜底：韩/日访客给母语话术而非英文 KB 片段。
  // 判定：页面语境（body.lang）优先，其次消息文字（谚文/假名区段零误判）。
  const KOJA_FALLBACK: Record<string, string> = {
    ko: "지금 접속이 많아 답변이 잠시 어렵습니다 🙏\n\n다음 키워드로 다시 시도해 보세요: 요금 / 음성 클로닝 / AI 자동 성사\n급하시면 Telegram @WJKJ2026 로 문의하시거나, 아래에 연락처를 남겨주시면 상담원이 곧 연락드립니다.",
    ja: "ただいまアクセスが集中しており、回答が一時的に難しい状況です 🙏\n\n次のキーワードでもう一度お試しください：料金 / 音声クローン / AI自動成約\nお急ぎの場合は Telegram @WJKJ2026 へ、または下記に連絡先をご記入いただければ担当者よりご連絡します。",
  };
  const kojaKey =
    uiLang === "ko" || uiLang === "ja"
      ? uiLang
      : /[\uac00-\ud7af]/.test(message)
        ? "ko"
        : /[\u3040-\u30ff]/.test(message)
          ? "ja"
          : "";

  const fallbackText = () => {
    if (kojaKey) return KOJA_FALLBACK[kojaKey];
    const fb = matchFreeText(message, lang) ?? buildFallback(lang);
    return cleanMarkdown(fb.replace(/<\/?[^>]+>/g, ""));
  };

  // cost guard + AI availability → stream; else single-shot fallback
  // forceKb：仅限管理员的调试开关，跳过 AI 直接走兜底路径（生产验证用）
  const forceKb = body?.kb === true && requireAdmin(req);
  const guard = dailyGuard();
  if (!deepseekEnabled() || !guard.allowed || forceKb) {
    const text = fallbackText();
    void logChat({ q: message, a: text, lang, source: guard.allowed ? "kb" : "capped", ip });
    return new Response(textStream(text), {
      headers: { "Content-Type": "text/plain; charset=utf-8", "X-Chat-Source": "kb" },
    });
  }

  const upstream = await streamDeepSeek(message, lang, history, 20000, extraSystem);
  if (!upstream || !upstream.body) {
    const text = fallbackText();
    void logChat({ q: message, a: text, lang, source: "kb", ip });
    return new Response(textStream(text), {
      headers: { "Content-Type": "text/plain; charset=utf-8", "X-Chat-Source": "kb" },
    });
  }

  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";
  let full = "";

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const reader = upstream.body!.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            const t = line.trim();
            if (!t.startsWith("data:")) continue;
            const payload = t.slice(5).trim();
            if (payload === "[DONE]") continue;
            try {
              const json = JSON.parse(payload);
              const delta = json?.choices?.[0]?.delta?.content;
              if (typeof delta === "string" && delta) {
                full += delta;
                controller.enqueue(encoder.encode(delta));
              }
            } catch {
              /* ignore partial json */
            }
          }
        }
      } catch {
        /* upstream aborted */
      } finally {
        controller.close();
        const clean = cleanMarkdown(full) || fallbackText();
        void logChat({ q: message, a: clean, lang, source: "ai", ip });
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Chat-Source": "ai",
    },
  });
}
