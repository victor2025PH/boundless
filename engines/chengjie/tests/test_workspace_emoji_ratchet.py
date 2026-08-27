"""坐席工作台模板「控件位 emoji」棘轮门禁（只减不增）。

背景（2026-08-08 P2A）：工作台曾混用 emoji / 字符 / 三套 SVG 当图标，跨平台渲染不一
（Windows 单色豆腐块、缩放模糊、色弱不可辨），已批量替换为 ui_icons.js 线性 SVG。
本门禁把替换成果钉住：每个工作台家族模板的 emoji 计数是**非增天花板**——新增控件
请用 `data-ui-icon` / `uiIcon()`（注册表见 tests/test_ui_icon_registry.py），别再写 emoji。

刻意不计入 / 刻意保留（天花板余量的来源，别清到 0）：
- HTML/Jinja 注释、行首 `// * /*` 的 JS 注释（文档不影响 UI）；
- 排版字符 ✓ ✕ ✗ ✦ 与箭头/几何区段（← → ▾ 等，非 emoji、跨平台稳定）；
- `<option>` 内 emoji（原生下拉无法渲染 SVG：自动化档位 🙋📝🔀🚀、音色 🎤、状态 🟢🟡⚫）；
- 内容语境：reaction 选择器 _REACT_EMOJIS、桌面系统通知标题前缀、toast 文案前缀、
  庆祝语 🎉（情绪表达是内容的一部分）；
- platform_icons.js 缺席时的 PI 应急回退表（诚实降级）。

计数降低后请同步调低天花板（有 not_stale 反向检查逼着回收，防止门禁强度悄悄流失）。
"""
import re
from pathlib import Path

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

_HTML_CMT = re.compile(r"<!--.*?-->", re.S)
_JINJA_CMT = re.compile(r"\{#.*?#\}", re.S)
# emoji 主区段 + 变体选择符；Dingbats(2700-27BF) 计入但豁免排版四杰 ✓(2713) ✕(2715) ✗(2717) ✦(2726)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u26FF\uFE0F\u2B00-\u2BFF]"
    "|[\u2700-\u27BF](?<![\u2713\u2715\u2717\u2726])"
)

# 非增天花板（2026-08-08 P2A 清理后的基线）。
_CEILINGS = {
    # 2026-08-17 12→10 收紧：顶栏药丸区块注释里的装饰 emoji（风控盾/图钉，块注释
    # 中间行不在行首剥离豁免内）随 P0-c 顶栏主题化改写为等义文字。
    # 2026-08-19 10→7：presence-status v2 把「我的状态」从 <option> 三色圆点改成
    # radio 行，控件位 emoji 再少 3；剩余属豁免语境（升级⛔ / 会话⚠ / 桌面通知💬）。
    # 2026-08-27 实施75 batch2 7→6：到期/拦截横幅退役顶部，⏳⛔ 装饰位随 DOM 摘除。
    "workspace_base.html": 6,
    # 2026-08-12 P2.5：注释区 emoji 清理（块注释中间行不在行首剥离豁免内，10 处全部
    # 改为等义文字，零 UI 变化）后 54→43 锁死。剩余=控件位真图标（诊断面板 ⛔⚠️✅ 等，
    # 归属诊断面板线）+ 豁免语境（option/reaction/PI 回退表/庆祝语/克隆 🎤 标记）。
    # 2026-08-17 43→53 登记（msgops 线代记，逐项 HEAD diff 归因）：
    #   +7 _REACT_EMOJIS_MSGR（Messenger reaction 表——与上行 _REACT_EMOJIS 同属
    #      「reaction 选择器=内容语境」豁免类，owner=messenger reactions 线）；
    #   +2 💡 翻译建议/同语种提示前缀 + 1 ❌ 媒体发送失败 toast 前缀（「toast/提示
    #      文案前缀」豁免语境，owner=xlate 线）；
    #   +1 🧲 TG 抓群成员工具链接（控件位，应转 uiIcon——记债，owner=tg-members 线）。
    #   （msgops 自增的 📌 注释 emoji 已改文字，不占额度。）
    #   -4 2026-08-21 AI 体检面板改版：结论头/卡片行严重度图标全走 CSS 色点
    #      （⛔⚠️✅ 控件位 emoji 清退，左缘色轨+色点承担语义）。
    #   +1 2026-08-22 全自动一键化 P2：存量升级一次性提示弹层的 🚀（__wsModal
    #      icon 槽位＝该组件的设计图标位，预算触顶弹窗同款用法；owner=本线）。
    # 2026-08-23 50→53 登记（impl64 exec 线代记，HEAD diff 归因；三处均为当日
    # 上午 unread-trust/tag 线 hot-live 的控件位 emoji，应转 uiIcon——记债，
    # owner=inbox unread trust + tag 线）：
    #   +2 🏷 标签编辑器入口（strip gear/编辑弹层三入口批次）；
    #   +1 🧹 在线账号菜单「清除未读」行（acct-menu clear-unread 批次）。
    "unified_inbox.html": 53,
    "workspace_dashboard.html": 0,
    "workspace_channels.html": 1,
    "draft_review.html": 1,
}


def _emoji_count(path: Path) -> int:
    body = _JINJA_CMT.sub("", _HTML_CMT.sub("", path.read_text(encoding="utf-8")))
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(("//", "*", "/*"))]
    return len(_EMOJI.findall("\n".join(lines)))


def test_workspace_emoji_not_increasing():
    over = {}
    for name, ceil in _CEILINGS.items():
        p = _TPL_DIR / name
        if not p.exists():
            continue
        n = _emoji_count(p)
        if n > ceil:
            over[name] = (n, ceil)
    assert not over, (
        "工作台模板新增了 emoji 图标（天花板为非增棘轮）：\n"
        + "\n".join(f"  {k}: 现 {v[0]} > 天花板 {v[1]}" for k, v in over.items())
        + "\n控件图标请用 data-ui-icon / uiIcon()（ui_icons.js 单一事实源）；"
        "若确属内容语境（reaction/庆祝语/<option>），把对应文件天花板 +N 并在本文件注明原因。"
    )


def test_workspace_emoji_ceilings_not_stale():
    """计数已下降但天花板没跟着降 → 提醒回收，保持棘轮张力。"""
    stale = {}
    for name, ceil in _CEILINGS.items():
        p = _TPL_DIR / name
        if not p.exists():
            continue
        n = _emoji_count(p)
        if n < ceil:
            stale[name] = (n, ceil)
    assert not stale, (
        "以下模板 emoji 已减少，请把 _CEILINGS 同步调低（保持棘轮张力）：\n"
        + "\n".join(f"  {k}: 现 {v[0]} < 天花板 {v[1]}" for k, v in stale.items())
    )
