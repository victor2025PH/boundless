# platform/compliance · 抽取方案（已按 Phase 6 深挖修正）

> **重大修正（2026-07-15，读 avatarhub `provenance.py` 后）**：原"把 `provenance.py`/`watermark.py` 移到 `platform/compliance/` + re-export"方案**被否**，两个硬伤——
>
> 1. **`provenance.py` 深度耦合 `app_config.BASE`**：它 `import app_config` 并把签名密钥/证书/审计库都指向 avatarhub 本地目录（`provenance_key.bin`、`provenance_ed25519_*.pem`、`c2pa_*.pem`、`provenance.db`、`data/brand.json`）。这些是 **avatarhub 本机私钥/状态**（`.gitignore` 忽略、绝不入库）。把 .py 移走并**不能**让别的引擎复用——它仍读 avatarhub 目录，别的引擎没有这些密钥。
> 2. **Python stdlib `platform` 名冲突**：若把仓根加进 `sys.path` 再 `import platform.compliance`，会**遮蔽标准库 `platform`**（`platform.system()` 等到处在用），全线可能崩。
>
> 结论：合规能力**是 avatarhub 本机私钥绑定的**，不是可跨引擎 import 的纯库。正确的"共享"是**服务契约（HTTP）**，不是移文件。

## 修正后的架构：compliance 作为「服务契约」

```
avatarhub Hub(9000) ── 已内建 provenance/watermark（持本机私钥）
   └── 对外暴露合规端点（C2PA 嵌入 / Ed25519 验真 / 水印 / 伦理校验）
        ▲ HTTP（带服务令牌 AVATARHUB_SERVICE_TOKEN）
   其它引擎/产品 ── 经 platform/compliance 契约调用（不各自持私钥、不 import 实现）
```

- **实现留在 avatarhub**（它拥有私钥与审计库，天然是签发方）。
- **platform/compliance 提供**：①`CONTRACT.md`（端点/数据格式/鉴权，机器可查）；②（后续）一个 stdlib 瘦客户端 `client.py`，其它引擎按需 `import`，只发 HTTP、不碰私钥。
- 这也符合本仓多语言多机现实：TS 官网、Python 引擎、多台机——**共享服务** > 共享库。

## 端点契约（待与 avatarhub 路由核对后固化到 CONTRACT.md）
| 能力 | 方法 端点（avatarhub Hub） | 入 | 出 |
|---|---|---|---|
| 溯源签发 | POST `/api/provenance/sign` | 媒体/产出物 | C2PA + Ed25519 签名 + manifest id |
| 对外验真 | GET/POST `/api/provenance/verify` | 产出物 | 真伪 + 签发者 + 时间 |
| 不可见水印 | POST（随产出链默认开） | 媒体 | 带水印媒体 |
| 克隆伦理校验 | 合成前闸门 | 音色/请求 | 放行/拒绝 |

> 落地前先在 avatarhub 侧核对真实路由（`avatar_hub.py` 的 `@app.` + `provenance.py` 的公开函数），把签名/入参对齐再写 client。

## 若确需「同机复用」（仅 avatarhub 自身微服务）
那是引擎**内部**重构（把 provenance 收进 avatarhub 内的子包），与"全域 platform"无关，且**不要**用 `platform` 作为 Python 包名（stdlib 冲突）；如需仓内共享 Python，用非冲突包名（如 `bd_platform`）并以 `pip install -e` 装入各引擎 env，而非往 sys.path 塞仓根。
