# platform/compliance · 合规出口契约（溯源 / 验真 / 水印）

> 定位修正（承接 Phase 6 结论）：合规的**签名私钥（Ed25519）/ C2PA 证书 / 审计库**天然绑定在
> **avatarhub（持钥方）本机**（都是 `.gitignore` 的机密）。把 `provenance.py` 挪进 platform 让别的引擎
> `import` **并不能复用**（它们没有那套私钥）。正确架构是：
> **签名/水印留在 avatarhub（合成时就地完成），platform 只定义『契约 + stdlib 瘦客户端』消费其 HTTP 验真面。**
> 因此本层是 **契约 + 客户端**，不是搬代码，守住 “platform 不反向依赖 engines”。

## 1. 能力与归属

| 能力 | 谁做 | 怎么触达 |
|---|---|---|
| **签名 / 加水印 / 嵌 C2PA** | avatarhub 合成时**就地**（`provenance.attach_credentials()`，私钥不出机） | 消费方**不直接调**：从 avatarhub 合成端点拿到的产物即已带凭证 |
| **验真 verify（音频）** | avatarhub | `POST /api/provenance/verify` |
| **验真 verify（视频/图片 C2PA）** | avatarhub | `POST /api/provenance/verify_media` |
| **取 manifest（软绑定解析）** | avatarhub | `GET /api/provenance/manifest/{payload_id}` |
| **状态（开关/算法/是否可离线验真）** | avatarhub | `GET /api/provenance/status` |
| **公钥分发（离线验真）** | avatarhub | `GET /api/provenance/pubkey`（Ed25519 PEM） |
| **声纹水印校验** | avatarhub | `POST /api/voice_clone/verify_watermark` |

> 设计含义：官网“可验证合规（verifiable-compliance）”的说法，可由**任意第三方仅凭公钥离线验签**背书；
> 无需信任我们的服务器——这正是 `pubkey` + `verify` 契约的价值。

## 2. 契约（stdlib 瘦客户端签名）

`platform/compliance/client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
ComplianceClient(base_url=None, timeout=5)   # base_url 缺省读环境变量 AVATARHUB_BASE_URL
  .status()                 -> dict          # /api/provenance/status（不可达则 available=False）
  .pubkey()                 -> dict|None     # /api/provenance/pubkey（Ed25519 PEM）
  .verify_audio(b64)        -> dict          # /api/provenance/verify
  .verify_media(b64, mime)  -> dict          # /api/provenance/verify_media
  .manifest(payload_id)     -> dict|None     # /api/provenance/manifest/{id}
  .available()              -> bool          # status.loaded 且 HTTP 可达
```

- **可降级**：avatarhub 不在线时不抛异常，返回 `{"available": False, "error": ...}`，
  让官网/下单/其它引擎能安全地“弱依赖”合规验真（有则显示验真徽章，无则隐藏）。
- **不做签名**：客户端**不提供 sign()**——签名只在 avatarhub 合成时发生（私钥不出机）。

## 3. 依赖方向

`website / products / engines → platform/compliance(契约+client) → (HTTP) → avatarhub 验真面`。
platform 不 import avatarhub 代码，仅通过 HTTP 契约交互；avatarhub 仍是私钥唯一持有方。

## 4. 落地状态与下一步

- 契约端点：**已在 avatarhub 存在并在跑**（`avatar_hub.py` 的 `/api/provenance/*`）。
- 本层交付：`CONTRACT.md`（本文件）+ `client.py`（瘦客户端，import 冒烟通过）。
- 待接线（下一阶段，需 avatarhub 在线的机器）：官网“验真徽章”组件调 `verify_audio/verify_media`；
  跨引擎产物流转时用 `manifest()` 做软绑定回溯。
