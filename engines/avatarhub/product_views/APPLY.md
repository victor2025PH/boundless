# 接线说明（APPLY）— 故意未改 avatar_hub.py

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
