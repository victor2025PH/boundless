# 接线说明（APPLY）

## 状态：已接线（v2，2026-07-18）

按下方附录的方案 **A1 + B** 已合入，两处改动均 fail-open（任何异常 → UI 保持全菜单，零行为变化）：

| 位置 | 内容 |
|---|---|
| `avatar_hub.py` ≈ L30398（紧挨 `@app.get("/api/features")` 之后） | 新增只读 `GET /api/product_view`：sys.path 兜底 → `product_views.loader.load_product_view()`；返回 `{"ok": True, "enabled": mode=="filtered", **view}`；任何异常返回 `{"ok": False, "enabled": False}` |
| `static/hub.js` init() ≈ L1074（「同步定 Tab」块之后、`await Promise.all` 之前） | fire-and-forget `fetch('/api/product_view')`：仅当 `ok && enabled && allowed_tabs 非空` 时按白名单过滤 `this.tabs`；当前 Tab 被隐藏则 `goTab(default_tab ∥ 首个可见)`；整段双层 try/catch |

loader **未改动**：未设 / 未知 `AVATARHUB_PRODUCT_ID` 本就返回 `mode=full`，端点映射为 `enabled=False`，前端不进过滤分支。

注：采用 fetch（A1）而非 hub_ui 注入（A2），未启用时零首帧等待；启用时首帧可能短暂闪现全菜单后收敛（试点可接受，介意再升级 A2）。

### 现网启用（幻声机 .176 试点）

1. 在幻声机 avatarhub 实例的启动环境加 `AVATARHUB_PRODUCT_ID=huanying`（PowerShell：`$env:AVATARHUB_PRODUCT_ID = "huanying"`；或写进该实例的启动脚本 env 段），重启 hub。
2. 验证：`GET /api/product_view` 应回 `enabled=true, product_id=huanying`；浏览器开 `/ui` → 侧栏应无「唱歌 / 批量 / 同传」，默认落「开播」；深链 `/ui#sing` 应落回「开播」。
3. 回滚：删掉该 env 重启即回全菜单。另有双保险：端点 / yaml / loader 任一异常时接口回 `enabled=false`，UI 自动保持全菜单。

### 本机（无重依赖）验证记录（2026-07-18）

```powershell
python -m py_compile engines/avatarhub/avatar_hub.py      # exit 0
python product_views/loader.py --selftest                 # SELFTEST OK（cwd=engines/avatarhub）
node --check engines/avatarhub/static/hub.js              # exit 0
# 端点函数体直调（不 import avatar_hub）：unset→enabled=false/mode=full；
# huanying→enabled=true hide=[sing,batch,interp] default=stream；未知 id→enabled=false
```

---

# 附录：接线分析原文（v1，当时故意未改 avatar_hub.py）

## 为何 v1 不直接改引擎

| 点 | 事实 |
|---|---|
| 侧栏装配 | `static/hub.js` L10–26 `tabs: [...]`；渲染在 `static/ui.html` `tabs.filter(x=>x.group===g)` |
| 默认 Tab | `hub.js` L4 `tab: 'profiles'`；`init()` L1066–1072 再按 hash / `hub_tab` 覆盖 |
| UI 下发 | `avatar_hub.py` ≈ L30520 `hub_ui()` 原样读 `static/ui.html`（约 **3.2 万行** 热文件） |
| 现成产品开关 | **无** `AVATARHUB_PRODUCT_ID` / 产品视图过滤；仅有端口/水印/VCAM 等通用 env |

热路径文件过大，误改成本高；本脚手架先交付 **loader + 四 yaml**，接线用下方最小补丁（评审后手工合入）。

---

## 推荐接线（两处，合计约 15 行量级）

### A. 后端：下发视图 JSON（`avatar_hub.py` · `hub_ui` 附近）

建议插入点：`@app.get("/ui")` → `hub_ui()`（约 **30520** 行），在返回 HTML 前注入一行脚本；或紧挨 `/api/features`（约 **30391**）新增只读 API。

**方案 A1 — `/api/product_view`（更干净）**

```python
# 紧挨 @app.get("/api/features") 之后
@app.get("/api/product_view")
async def api_product_view():
    from product_views.loader import load_product_view
    return {"ok": True, **load_product_view()}
```

启动 cwd 须为 `engines/avatarhub`（现网惯例）；否则：

```python
import sys
from pathlib import Path
_pv = Path(__file__).resolve().parent / "product_views"
if str(_pv.parent) not in sys.path:
    sys.path.insert(0, str(_pv.parent))
```

**方案 A2 — 注入 `window.__AVATARHUB_PRODUCT_VIEW__`**

在 `hub_ui()` 读完 `ui.html` 后：

```python
from product_views.loader import load_product_view
import json
view = load_product_view()
inject = (
    "<script>window.__AVATARHUB_PRODUCT_VIEW__="
    + json.dumps(view, ensure_ascii=False)
    + ";</script>"
)
html = _UI_FILE.read_text(encoding="utf-8").replace(
    "<script src=\"/static/hub.js", inject + "\n<script src=\"/static/hub.js", 1
)
```

### B. 前端：按 hide 过滤侧栏（`static/hub.js`）

建议插入点：`init()` 内「同步定 Tab」块 **之前**（约 **1066** 行前），保证 hash 校验用的已是裁剪后的 `tabs`。

```javascript
// 产品视图裁剪（v1）：hide_tabs 来自 /api/product_view 或 window.__AVATARHUB_PRODUCT_VIEW__
(function applyProductView(pv){
  if (!pv || pv.mode !== 'filtered') return;
  const hide = new Set(pv.hide_tabs || []);
  if (hide.size) this.tabs = (this.tabs || []).filter(t => !hide.has(t.id));
  const ids = (this.tabs || []).map(t => t.id);
  if (pv.default_tab && ids.includes(pv.default_tab) && !location.hash) {
    this.tab = pv.default_tab;
    this.visitedTabs = [pv.default_tab];
  }
  if (pv.brand && pv.brand.zh) { try { document.title = pv.brand.zh + ' · 控制台'; } catch(_){} }
}).call(this, window.__AVATARHUB_PRODUCT_VIEW__);
// 若用 A1：可在 init 开头 await fetch(HUB+'/api/product_view')——注意须在「同步定 Tab」之前完成，
// 或改为同步 XHR / 启动时由 hub_ui 注入（推荐 A2，避免首帧闪全菜单）。
```

可选：`loadFeatures()` 结果里按同一 `hide_tabs` / `feature_flags` 过滤，避免命令面板绕过侧栏。

### C. 幻颜逻辑能力组

`lab_*` 不是侧栏 id。接线示例：

```javascript
const ff = (window.__AVATARHUB_PRODUCT_VIEW__ || {}).feature_flags || {};
// 用 ff.offline_faceswap / hair_preset / tryon 控制对应面板 x-show
```

---

## 验收（接线后）

```powershell
$env:AVATARHUB_PRODUCT_ID = "tongchuan"
python product_views/loader.py --selftest
python -m py_compile avatar_hub.py   # 若改了 py
# 浏览器开 /ui → 侧栏应无「开播/唱歌/批量」，默认落「同传」
```

未接线时：仅 loader/yaml 自检通过即可；UI 仍为全菜单。
