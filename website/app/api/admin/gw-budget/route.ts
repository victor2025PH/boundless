import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import {
  DAILY_CHAR_BUDGET,
  listBudgetOverrides,
  normalizeFingerprint,
  normalizeInstanceId,
  quotaSnapshot,
  setBudgetOverride,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * AI 网关按主体（机器指纹 / IID:实例）日额度覆写管理。
 *
 *   GET  /api/admin/gw-budget                       → 全部覆写 + 今日用量
 *   GET  /api/admin/gw-budget?subject=<mid|IID:x>   → 单主体额度快照
 *   POST /api/admin/gw-budget {subject, budget, note?}
 *        budget<=0 或 clear:true = 清除覆写（回到 env 默认）
 *
 * 语义：有覆写的主体按覆写额度计费，且不受全局日预算熔断拒绝
 * （管理员显式授予的额度不该被试用池成本保险丝掐掉；用量仍计入全局账）。
 * 鉴权：`x-setup-key`（ADMIN_KEY / TELEGRAM_SETUP_KEY），与 trial-claims 同口径。
 */

function normalizeSubject(raw: string): string {
  const s = String(raw || "").trim();
  if (!s) return "";
  if (/^iid:/i.test(s)) {
    const iid = normalizeInstanceId(s.slice(4));
    return iid ? `IID:${iid}` : "";
  }
  return normalizeFingerprint(s);
}

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const subjectRaw = req.nextUrl.searchParams.get("subject") || "";
  if (subjectRaw) {
    const subject = normalizeSubject(subjectRaw);
    if (!subject) {
      return NextResponse.json({ ok: false, error: "bad_subject" }, { status: 400 });
    }
    const snap = await quotaSnapshot(subject);
    return NextResponse.json({ ok: true, subject, quota: snap });
  }
  return NextResponse.json({
    ok: true,
    default_budget: DAILY_CHAR_BUDGET,
    overrides: listBudgetOverrides(),
  });
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const data = await req.json().catch(() => ({}));
  const subject = normalizeSubject(String(data?.subject || data?.fingerprint || ""));
  if (!subject) {
    return NextResponse.json({ ok: false, error: "subject_required" }, { status: 400 });
  }
  const clear = data?.clear === true;
  const budget = clear ? 0 : Math.floor(Number(data?.budget));
  if (!clear && (!Number.isFinite(budget) || budget <= 0)) {
    return NextResponse.json({ ok: false, error: "budget_required" }, { status: 400 });
  }
  const out = setBudgetOverride(subject, budget, String(data?.note || ""));
  const snap = await quotaSnapshot(out.subject);
  return NextResponse.json({
    ok: true,
    subject: out.subject,
    budget: out.budget,
    cleared: clear,
    quota: snap,
  });
}
