# -*- coding: utf-8 -*-
"""平台能力矩阵导出：从 worker 代码结构渲染 Markdown（不手写、不会漂移）。

与 ``scripts/feature_matrix.py`` 同族——那张表回答「卖什么档位」，这张回答
「**哪个平台能做什么**」。后者此前只存在于四个 worker 类的实现里和几条**已经写错的
注释**里（见 ``src/integrations/platform_capabilities.py`` 模块头）。

用法::

    python -m scripts.platform_matrix                      # 打印到 stdout
    python -m scripts.platform_matrix --out docs/平台能力矩阵.md
    python -m scripts.platform_matrix --live               # 用本机实例配置渲染

同步纪律：``docs/平台能力矩阵.md`` 由门禁 ``tests/test_platform_matrix.py`` 钉住与
代码逐字一致——改了 worker 就重跑上面第二条命令，别手改生成物。刻意不带时间戳
（内容只由代码决定，重复生成字节一致，否则同步门禁会被噪声逼疯）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.integrations.platform_capabilities import (  # noqa: E402
    CAPABILITY_LABELS, GROUP_ADMIN_LABEL, GROUP_RECV_LABEL, GROUP_SEND_LABEL,
    HARD_LIMITS, INBOUND_LABEL, capability_matrix, switched_cells,
)

_DOC_DEFAULT = Path(__file__).resolve().parent.parent / "docs" / "平台能力矩阵.md"

PLATFORM_LABEL = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "messenger": "Messenger",
    "line": "LINE",
    "zalo": "Zalo",
    "instagram": "Instagram",
    "douyin": "抖音",
    "tiktok": "TikTok",
}


def render_matrix(config: dict | None = None) -> str:
    cfg = config or {}
    matrix = capability_matrix(cfg)
    switched = switched_cells(cfg)
    caps = list(CAPABILITY_LABELS)

    lines = [
        "# 平台能力矩阵（编排器 worker）",
        "",
        "> 本文件由 `python -m scripts.platform_matrix --out docs/平台能力矩阵.md`",
        "> 自动生成，**不要手改**（门禁 `tests/test_platform_matrix.py` 钉住与代码",
        "> 逐字一致）。",
        "",
        "各列的**事实源不同**，别混着读：",
        "",
        "- **发送四列** = 各 worker 类上**方法是否存在**，与编排器运行时判据同源",
        "  （`owns_media()` 就是 `hasattr(worker, \"send_media\")`）。",
        "- **收媒体列** = 该平台入站链有没有把 `media_type` 带进 payload——Python",
        "  handler 用 AST 判定；Node 边车平台（WA/Messenger/Zalo/IG）判定**边车",
        "  server.js 的 ingest payload**（文本判定；共享 ingest 路由只证明「路由会",
        "  转发」，证明不了「边车真的送了」）。不带就等于客户发的图对 AI 不存在。",
        "- **群消息进箱列** = 群消息能否被标注/分流进统一收件箱，事实源分三种：",
        "  Telegram 两档＝落库层 `normalizer.infer_chat_type` 负数 chat_id 启发式",
        "  （判定＝真调用该函数）；LINE protocol＝handler 显式 `chat_type`（AST）；",
        "  边车平台＝ingest payload 里的 `chat_type` 真含 \"group\"（文本判定，",
        "  显式空串不算数）。**判据强度分档**：WA/LINE/TG 的群地址自描述（`@g.us` /",
        "  群 id / 负数 chat_id）＝硬事实；Messenger（一线程出现 ≥2 个不同发言人）与",
        "  Instagram（线程行叠 ≥2 张头像，`services/instagram-web/ig_threads.js`）＝",
        "  **网页 DOM 启发式**，判不出时如实回落私聊，平台改版可能失准——这两家的",
        "  这一格是「已接线」，不是「已在真号上验证过」。",
        "- **群内可发列** = 推导列：群消息进箱 ∧ 发文本。群地址在 WA/LINE/TG 自描述，",
        "  Zalo 由边车群注册表自动解析 ThreadType（`services/zalo-personal/",
        "  group-registry.js`）。看不见群的平台谈不上群内回复。",
        "- **群管理 API 列** = worker 方法内省（`invite_to_group`/`create_group`/",
        "  `kick_group_member`/`rename_group`，见 `GROUP_ADMIN_METHODS` 预留契约），",
        "  列出已有能力的中文标签，无则 `-`。",
        "",
        "判读：`Y` 支持 / `-` 不支持 / `开关` 需打开配置（开关名见表下）/ `?` 未知",
        "（本机缺依赖构造不出 worker，或入站接线点被重命名找不到）——`?` **不等于",
        "不支持**。",
        "",
        "⚠ 收媒体列判的是**代码接没接线**，不读运行时配置：LINE 另有一个默认开启的",
        "开关 `platform_login.line.media.inbound`，运营把它关掉后该格仍显示 `Y`（代码",
        "确实接着，只是被配置停用了）。`--live` 模式同理。群消息进箱同理：TG 的",
        "`telegram.process_groups` / 群自动回复档位是运行时策略，不改变接线事实。",
        "",
    ]

    header = ("| 平台（mode） | " + " | ".join(CAPABILITY_LABELS[c] for c in caps)
              + " | " + INBOUND_LABEL
              + " | " + GROUP_RECV_LABEL
              + " | " + GROUP_SEND_LABEL
              + " | " + GROUP_ADMIN_LABEL + " |")
    lines.append(header)
    lines.append("|---" * (len(caps) + 5) + "|")

    def _tri(v):
        return "?" if v is None else ("Y" if v else "-")

    for key in sorted(matrix):
        row = matrix[key]
        label = "%s（%s）" % (PLATFORM_LABEL.get(row["platform"], row["platform"]),
                             row["mode"])
        cells = []
        for c in caps:
            if not row["available"]:
                cells.append("?")
            elif row["caps"].get(c):
                cells.append("Y")
            elif c in (switched.get(key) or {}):
                cells.append("开关")
            else:
                cells.append("-")
        cells.append(_tri(row.get("recv_media")))
        cells.append(_tri(row.get("recv_group")))
        cells.append(_tri(row.get("group_send")))
        ga = row.get("group_admin")
        if ga is None:
            cells.append("?")
        else:
            cells.append("、".join(ga) if ga else "-")
        lines.append("| %s | %s |" % (label, " | ".join(cells)))

    if switched:
        lines += ["", "## 需要打开的开关", ""]
        for key in sorted(switched):
            for cap, dotted in sorted(switched[key].items()):
                lines.append("- `%s` 的**%s**：`%s`（开启后需重启实例）"
                             % (key, CAPABILITY_LABELS.get(cap, cap), dotted))

    if HARD_LIMITS:
        lines += ["", "## 协议层硬限制（不是没接，是对面没有）", ""]
        for (platform, cap), why in sorted(HARD_LIMITS.items()):
            lines.append("- %s 的**%s**：%s"
                         % (PLATFORM_LABEL.get(platform, platform),
                            CAPABILITY_LABELS.get(cap, cap), why))

    lines += [
        "",
        "## 本表刻意不覆盖的",
        "",
        "引用回复 / 撤回 / 贴纸这类**无法由代码结构可靠判定**的能力不进本表：形参叫",
        "`reply_to` 不代表真的透传（LINE 的 `send()` 收了它却忽略，一直到 2026-07-31",
        "才接上；Messenger 至今仍是收了不用）。按签名推断会造出假阳性——那正是本表要",
        "消灭的东西。要查这些请直接读 worker 实现。",
        "",
        "**频道（channel/OA/broadcast）刻意不进本表**：频道的「订阅/收帖/发布」是与",
        "会话收发不同构的实体（订阅关系 + 帖子流 + 发布管线），归 channel_hub 主线",
        "（P2/P3）落地时再进表；现状只有 Telegram 的 channel 类型会话能按 `chat_type`",
        "进箱（与群同判据，已被群消息进箱列覆盖）。",
        "",
        "**收媒体列只判「接没接线」，不判「支持哪几种」**：图 / 语音 / 视频 / 贴纸 /",
        "文件的逐项支持度取决于各平台上游（如 Messenger 网页侧文件只给占位），那要逐",
        "kind 判定，不宜按结构推断。**要看实际到货情况就用生产数据**：",
        "",
        "```",
        "python -m scripts.platform_matrix --with-live-data   # 代码接线 x 实际到货 对照",
        "python tools/check_outbound_media_mirror.py          # 出站记账口径专项",
        "```",
        "",
        "前者回答「代码说支持的，实际到货了吗；代码说不支持的，是不是另有旁路」；",
        "后者专查「媒体发出去了却只记成文字占位」这类记账缺陷。",
        "",
        "另：本表只描述**编排器接管的 worker**（protocol / web / official 模式）。",
        "`mode=device` 的手机 RPA 是另一套实现，能力面不同，不在此表。",
        "",
    ]
    return "\n".join(lines)


def _live_config() -> dict:
    from scripts._data_root import load_merged_config, resolve_data_roots
    return load_merged_config(resolve_data_roots("")[0])


_MEDIA_MARKS = ("[图片]", "[语音]", "[视频]")


def collect_live_stats(days: float = 14.0) -> dict:
    """从生产 ``inbox.db`` 只读收集各平台的收/发媒体到货量。

    ``mode=ro`` 连接、只做聚合查询，对活体库零写事务。
    """
    import sqlite3
    import time

    from scripts._data_root import resolve_data_roots

    since = time.time() - days * 86400
    out: dict = {}
    for root in resolve_data_roots(""):
        db = Path(root) / "config" / "inbox.db"
        if not db.is_file():
            continue
        uri = "file:%s?mode=ro" % str(db).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute(
                "SELECT c.platform, m.direction, COALESCE(m.media_type,''),"
                "       COALESCE(m.text,'')"
                " FROM messages m JOIN conversations c"
                "   ON c.conversation_id = m.conversation_id"
                " WHERE m.ts >= ?", (since,),
            ).fetchall()
        finally:
            con.close()
        for platform, direction, mtype, text in rows:
            st = out.setdefault(str(platform), {
                "in_total": 0, "in_media": 0,
                "out_total": 0, "out_media": 0, "out_marked_text": 0})
            side = "in" if str(direction) == "in" else "out"
            st["%s_total" % side] += 1
            if mtype:
                st["%s_media" % side] += 1
            if side == "out" and any(k in text for k in _MEDIA_MARKS):
                st["out_marked_text"] += 1
    return out


_VERDICT_MARK = {
    "ok": "OK  ", "no_traffic": "--  ", "dry": "WARN",
    "unexpected": "WARN", "mixed": "INFO", "explained": "INFO",
    "out_of_scope": "n/a ",
}
_DIM_LABEL = {"recv_media": "收媒体", "send_media": "发媒体"}


def render_live_reconcile(days: float = 14.0) -> str:
    """代码接线 × 实际到货的并排对照（**只打印，绝不进生成的文档**）。"""
    from src.integrations.platform_capabilities import code_flags, reconcile

    matrix = capability_matrix(_live_config())
    live = collect_live_stats(days)
    lines = ["", "-- 代码接线 x 实际到货（近 %.0f 天，本机生产库）--" % days]
    for r in reconcile(code_flags(matrix), live):
        mark = _VERDICT_MARK.get(r["verdict"], "?   ")
        lines.append(
            "  [%s] %-10s %s  代码=%-6s 实际=%s/%s"
            % (mark, r["platform"], _DIM_LABEL.get(r["dim"], r["dim"]),
               r["code"], r["media"], r["total"]))
        if r["reason"]:
            lines.append("         原因：%s" % r["reason"])
        # 记账口径提示（不是能力判词）：媒体发出去了、却只记成文字占位。
        # 这是**另一类**缺陷（镜像口径），专项探针见 tools/check_outbound_media_mirror.py。
        if r["dim"] == "send_media":
            st = live.get(r["platform"]) or {}
            gap = int(st.get("out_marked_text") or 0) - int(st.get("out_media") or 0)
            if gap > 0:
                lines.append(
                    "         注：另有 %d 条只记成文字占位（记账口径，非能力问题）→ "
                    "python tools/check_outbound_media_mirror.py" % gap)
    lines += [
        "",
        "  判词：OK=代码与实际一致 / WARN=需要看一眼 / INFO=已解释或分档不同 /",
        "        --=样本不足不下判断 / n/a=该平台不在矩阵范围（如内置 web 测试聊天）。",
        "        dry（接了没到货）多半是上游或配置；unexpected（没接却有货）说明",
        "        判据漏了某条旁路。**只要真到过货就判 OK，与总量无关**——样本阈值",
        "        只用来拦否定结论。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="平台能力矩阵导出")
    ap.add_argument("--out", default="", help="写入文件（缺省打印到 stdout）")
    ap.add_argument("--live", action="store_true",
                    help="用本机实例配置渲染（看**当前部署**的实际能力，不用于生成文档）")
    ap.add_argument("--with-live-data", action="store_true",
                    help="附「代码接线 x 实际到货」对照（读本机生产库，只读）")
    ap.add_argument("--days", type=float, default=14.0,
                    help="--with-live-data 的统计窗口天数")
    args = ap.parse_args()

    # 生成的文档必须是**确定性、机器无关**的（门禁逐字钉住），而实时读数天生因机
    # 因时而异 → 两者同用必然把机器特有的数字写进仓库文档，这里直接拒。
    if args.out and (args.with_live_data or args.live):
        print("--out 不能与 --live/--with-live-data 同用："
              "生成的文档必须确定性、机器无关（门禁逐字钉住）。"
              "看本机实况请去掉 --out。")
        return 2

    cfg = _live_config() if (args.live or args.with_live_data) else {}
    text = render_matrix(cfg)
    if args.with_live_data:
        text += render_live_reconcile(args.days)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print("written: %s" % p)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
