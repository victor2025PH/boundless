import { NextRequest, NextResponse } from "next/server";
import { readFile, writeFile, mkdir } from "fs/promises";
import path from "path";
import { DATA_DIR } from "@/lib/data-dir";
import { requireAdmin } from "@/lib/admin-auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 活动皮肤运行时开关:GET 公开(前端 ThemeLoader 读取),POST 仅管理员。
 *  相比构建时 NEXT_PUBLIC_FX_THEME,后台切换即时生效、免重新部署。 */

const FILE = path.join(DATA_DIR, "fx-theme.json");
const FX_THEMES = ["", "gold", "emerald", "crimson"] as const;

export async function GET() {
  let theme = "";
  try {
    theme = String(JSON.parse(await readFile(FILE, "utf8"))?.theme ?? "");
  } catch {
    /* 未设置过:保持默认 */
  }
  return NextResponse.json(
    { theme },
    // 浏览器缓存 2 分钟:换肤是稀有操作,页面加载几乎总是命中缓存
    { headers: { "Cache-Control": "public, max-age=120" } }
  );
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const body = await req.json().catch(() => null);
  const theme = String(body?.theme ?? "");
  if (!(FX_THEMES as readonly string[]).includes(theme)) {
    return NextResponse.json({ ok: false, error: "bad_theme" }, { status: 400 });
  }
  await mkdir(DATA_DIR, { recursive: true });
  await writeFile(FILE, JSON.stringify({ theme, updated: new Date().toISOString() }));
  return NextResponse.json({ ok: true, theme });
}
