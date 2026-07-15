# platform/licensing · 授权 / 计量 / SKU 注册表

## sku_registry.json — 全域 SKU 单一源（闭环基石）

由 `tools/build_sku_registry.py` 从 **`products/*/product.yaml`** 汇总生成，是"官网 ↔ SKU ↔ license ↔ 交付"闭环的唯一 SKU 事实：

- **官网** 定价读它（`website/lib/pricing.ts` 后续对齐同一 id/价）；
- **license 门控** 按 `sku_id` 判权/计量；
- **order 下单** 引用同一 `sku_id`，成交后开通对应能力。

一份清单三处用，改价只改产品 `product.yaml` → 重跑 builder → 三处同步。

### 重新生成
```powershell
<带 pyyaml 的 python> tools/build_sku_registry.py
# 产物：platform/licensing/sku_registry.json（当前 7 产品 / 21 SKU / 5 个 TBD 待定价）
```

### 结构
- `products[]`：每产品 id/brand_key/category/**visibility(合规可见性)**/risk/engine/landing/skus。
- `flat_skus[]`：拍平的 SKU 行（product+category+visibility+sku_id+price+currency+unit），便于 license/order 直接索引。
- `summary`：产品数 / SKU 数 / TBD 定价数 / 按系与按可见性分布。

> 依赖方向：**builder 在 `tools/`（工具层可读产品），产物是纯数据落 platform/**——platform 自身不 import products/engines，守住"platform 不反向依赖"。

## 计量 / 门控（后续在 env 机落地）
- 计量：VoiceX 按字符、ChatX 按 LLM token、VoxX 按场次/时长、FaceX 按张/分钟（见各 product.yaml `unit`）。
- 门控：按 license edition × sku_id 开/关能力位（合规主站 public SKU 直售；gated SKU 走准入）。
