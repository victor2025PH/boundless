"""全站模板「自由变量捕获」门禁——顶层函数引用闭包内标识符＝运行时必 ReferenceError。

事故原型（contact360，2026-08-01）：`loadEngagement`/`computeEngagement` 定义在 IIFE 外
（全局），函数体却引用 IIFE 内的 `CONTACT_ID`/`esc` → 启动序列一调用即 ReferenceError，
后续 `loadStlSide()`/`load()` 全被掐死，整页永远卡「加载中」。哑按钮门禁抓不到（函数本身
全局可达，崩在**体内自由变量**）；孤儿引用门禁只看 DOM id——这类「作用域错位」此前是盲区。

首轮全站扫描战果（2026-08-02）：除 contact360（已修）外又揪出两个同款潜伏 bug——
  - ops_overview::loadCredPool 引用 esc，而同页 19 个兄弟卡片函数**每个都有函数局部 esc**、
    唯独它漏了 → 凭据池一激活（cp.active）渲染即崩，卡片永远「加载失败」；
  - _channel_body_messenger::loadMrSendQueue 引用 IIFE 内的 esc（本函数须顶层供内联
    onclick 调）→ 发送队列**有内容时**渲染即崩，catch 兜底自己也调 esc 二次崩。
两处均已按同页先例补函数局部 esc。

判据（窄不变量，宁漏勿误伤）见 tests/_free_capture_scan.py 模块 docstring：
读取的标识符须同时 (a)非局部 (b)全局不可达 (c)**同页某闭包里有声明**——(c) 是高置信
关键：名字全站不见踪影多半来自外部 <script src>，刻意不 flag。
已知盲区（运行时由 _boot_error_guard.html 全局错误守卫兜底）：模板字面量 `${...}` 插值、
顶层匿名回调体、`x+=1` 复合赋值。
"""
from pathlib import Path

from tests import _free_capture_scan as fc
from tests import _inline_handler_scan as scan

_TPL_DIR = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
_ALL = sorted(_TPL_DIR.rglob("*.html"))
_AMBIENT = scan.ambient_globals(_TPL_DIR)

# 已知真 bug 待产品决策的登记台账：{模板名: {函数名: [标识符, ...]}}。
# 修好后从这里删除；test_pending_free_captures_still_broken 会在其不再命中时提醒回收。
_PENDING_FREE_CAPTURES = {}


def test_no_free_captures_sitewide():
    failures = {}
    for f in _ALL:
        html = f.read_text(encoding="utf-8")
        pend = _PENDING_FREE_CAPTURES.get(f.name, {})
        bad = {}
        for fn, names in fc.free_captures(html, ambient=_AMBIENT):
            missing = sorted(set(names) - set(pend.get(fn, ())))
            if missing:
                bad[fn] = missing
        if bad:
            failures[f.name] = bad
    assert not failures, (
        "有模板的**顶层函数**读取了只在闭包（IIFE/函数体）里声明的标识符——"
        "运行时一调用必 ReferenceError（contact360 死页面同款）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in failures.items())
        + "\n修法：把该函数移回闭包内（若无需全局可达）/ 在函数内补局部定义 / "
        "或把被引用的声明提升到顶层（或挂 window）。"
    )


def test_pending_free_captures_still_broken():
    """防 _PENDING 过期：修好后不再命中 → 提醒从台账回收，恢复门禁强度。"""
    stale = {}
    for name, fns in _PENDING_FREE_CAPTURES.items():
        f = _TPL_DIR / name
        if not f.exists():
            continue
        found = dict(fc.free_captures(f.read_text(encoding="utf-8"), ambient=_AMBIENT))
        for fn, names in fns.items():
            fixed = sorted(set(names) - set(found.get(fn, ())))
            if fixed:
                stale.setdefault(name, {})[fn] = fixed
    assert not stale, (
        "以下登记项已不再命中（bug 已修），请从 _PENDING_FREE_CAPTURES 移除：\n"
        + "\n".join(f"  {k}: {v}" for k, v in stale.items())
    )


# ── 扫描器自证（探测器有效性：合成事故原型必被抓到；良性模式零误报）──────────

_BUGGY_CONTACT360_PATTERN = """
<script>
(function(){
  const CONTACT_ID = 42;
  function esc(s){ return s; }
  loadEngagement();
})();
function loadEngagement(){
  apiFetch('/api/x/'+encodeURIComponent(CONTACT_ID)).then(function(r){ return r.json(); })
    .then(function(d){ box.innerHTML = '<i title="'+esc(d.t)+'"></i>'; });
}
</script>
"""


def test_scanner_catches_contact360_pattern():
    findings = dict(fc.free_captures(_BUGGY_CONTACT360_PATTERN))
    assert "loadEngagement" in findings
    assert findings["loadEngagement"] == ["CONTACT_ID", "esc"]
    # box 全站无声明（可能来自外部脚本）→ 刻意不 flag；apiFetch 同理
    assert "box" not in findings["loadEngagement"]
    assert "apiFetch" not in findings["loadEngagement"]


def test_scanner_zero_fp_on_benign_patterns():
    benign = """
<script>
(function(){
  const Chart = window.Chart;          // 外部库别名：window 读 = 全局存在断言
  const helper = 1;
  var g = 2;                            // 与正则 flags 同名的闭包变量
  window.exposedFn = function(){};
})();
function ok1(x){                        // 形参
  var local = 1;                        // 局部声明
  if (typeof helper !== 'undefined') return helper + local + x;  // typeof 防御式放行
  return Chart;                          // window.Chart 读过 → 可达
}
function ok2(){
  helper = 5;                            // 纯赋值目标（非严格模式不抛）
  for (const [k, v] of Object.entries({})) { void k; void v; }   // for-of 解构
  return 'a'.replace(/x/g, '').replace(/[&<>"']/g, '');           // 正则 flags 非标识符
}
function ok3(){
  const obj = { helper: 1, load(){ return 1; } };                 // 对象键/方法简写非引用
  return obj;
}
</script>
"""
    assert fc.free_captures(benign) == []


def test_fixed_contact360_stays_clean():
    """事故文件回归钉：loadEngagement/computeEngagement 必须留在 IIFE 内（或不再捕获闭包名）。"""
    f = _TPL_DIR / "contact360.html"
    assert f.exists()
    assert fc.free_captures(f.read_text(encoding="utf-8"), ambient=_AMBIENT) == []


def test_incident_files_stay_clean():
    """首轮扫描揪出的两个潜伏 bug 的回归钉（修法＝函数局部 esc，勿回退）。"""
    for name in ("ops_overview.html", "_channel_body_messenger.html"):
        f = _TPL_DIR / name
        assert f.exists()
        assert fc.free_captures(f.read_text(encoding="utf-8"), ambient=_AMBIENT) == [], name


def test_boot_error_guard_renders_into_page_families(auth_client):
    """运行时守卫（_boot_error_guard.html）真渲染进三类页面家族——静态门禁抓「入库前」，
    它兜「运行时才炸」的盲区（模板字面量插值/顶层回调等）。缺 include / partial 坏语法
    会 500 或缺标记。覆盖：workspace_base 家族（渠道中心）、base 家族（/ 仪表盘）、
    独立头页面（/admin/ops）。login.html 刻意不覆盖（预鉴权极简页）。"""
    for url in ("/workspace/channels/telegram", "/", "/admin/ops"):
        r = auth_client.get(url)
        assert r.status_code == 200, (url, r.status_code)
        assert "fe-boot-guard-bar" in r.text, url
