# 集团统一授权台账（P1）· 归一化导出/导入格式

> 状态：P1 施工中。本目录只定义**台账数据格式**（schema + 映射约定），不含任何签发逻辑。
> 导出适配器在 `tools/license_ledger/`；导入脚本 `website/scripts/ledger-import-licenses.mjs` 由 website 侧同事开发。

## 1. 定位：先统一台账、后统一签发

集团现存**两套并行的 Ed25519 授权实现**：

| | avatarhub 系 | chengjie 系 |
|---|---|---|
| 代码位置 | `engines/avatarhub/license*.py`、`license_server.py`、`fulfill_orders.py`、`sign_worker.py` | `engines/chengjie/src/licensing/`、`scripts/license_tool.py` |
| 授权载体 | `license.key`：`{"payload":{...},"sig":"<hex>","alg":"Ed25519"}` | 授权码 token：`<payload_b64url>.<sig_b64url>` |
| 绑定维度 | **机器指纹** + edition（trial/standard/pro） | **客户** + plan/seats/included_chars/channels（不绑机） |
| 服务的产品 | huansheng 幻声 / huanying 幻影 / huanyan 幻颜 / tongchuan 通传 | zhiliao 智聊 / tongyi 通译 |

P1 阶段**不动两套签发实现**，只做一件事：把两边的"发放记录"以只读方式导出成同一种归一化 JSON，导入集团账本（website 侧 SQLite），先让集团层面"发了多少授权、给谁、什么时候到期"有一本总账。统一签发（P2+）在总账数据跑顺之后再议。

## 2. 数据流与幂等键

```
engines/avatarhub（secrets/ 台账，只读）──→ export_avatarhub.py ──→ avatarhub_licenses.json ─┐
                                                                                            ├─→ website/scripts/ledger-import-licenses.mjs ──→ 集团账本（website 侧 SQLite）
engines/chengjie（config/license.key 等，只读）→ export_chengjie.py → chengjie_licenses.json ─┘
```

- 导出文件顶层：`{"version":1, "source_system":"...", "exported_at":"ISO8601", "records":[...]}`。
- **幂等键 = `(source_system, source_key)`**：同一 source_system 内 `source_key` 唯一（导出器已去重），导入方按此 upsert，重复导入不产生重复行。
- 格式的机器可读定义见同目录 [`ledger_import.schema.json`](./ledger_import.schema.json)（JSON Schema draft-07）。

## 3. 归一化记录字段（v1）

每条记录一个 JSON 对象，14 个字段**全部必出现**（无值用 `null`，不允许缺键）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_system` | `"avatarhub"` \| `"chengjie"` | 必填，与顶层一致 |
| `source_key` | string，非空 | 该系统内唯一键（取值规则见 §5 映射表） |
| `product_id` | 七产品 id 或 `null` | `huansheng/huanyan/huanying/tongchuan/tongyi/zhiliao/zhituo`；分不清填 `null`（见 §6） |
| `sku_id` | string 或 `null` | `platform/licensing/sku_registry.json` 里的 `sku_id`；分不清填 `null` |
| `plan` | string 或 `null` | 原始套餐名（chengjie：community/basic/pro/flagship；avatarhub 无此概念，恒 null） |
| `edition` | string 或 `null` | 原始版本名（avatarhub：trial/standard/pro；chengjie 无此概念，恒 null） |
| `seats` | int 或 `null` | 席位。**注意 chengjie 的 `0` 表示"不限"，原样保留不转 null** |
| `customer_name` | string 或 `null` | 客户名 |
| `customer_contact` | string 或 `null` | 联系方式（两套引擎侧台账均无独立联系方式字段，目前恒 null，见 §7 已知局限） |
| `machine_fingerprint` | string 或 `null` | avatarhub 机器指纹（`XXXX-XXXX-XXXX-XXXX`；`*`=站点授权）；chengjie 不绑机，恒 null |
| `issued_at` | ISO8601 或 `null` | 签发时间；原值转不了 ISO8601 → null，原值留在 `raw` |
| `expires_at` | ISO8601 或 `null` | 到期时间；`null` = 永久（原值 0/缺省）**或**未知（如未激活兑换码尚未起算），由 `raw` 区分 |
| `status` | `active/expired/revoked/trial/unknown` | 导出时刻状态，判定口径见 §4 |
| `raw` | object | 原始记录原样保留 + `kind` 标注记录子类（安全例外见 §8） |

## 4. status 判定口径（两侧统一）

判定优先级从高到低（`now` = 导出时刻）：

1. `revoked` —— avatarhub：兑换码被 `disabled`，或按 `revocations.json`（CRL）字段匹配命中（条目内 AND、名单内 OR，匹配键 `lic_id/machine/licensee/issued`，与 `license.py` 同语义；**导出器不验签 CRL**，仅作状态标注）。chengjie 无吊销机制，仅当 JSONL 台账行显式带 `"revoked": true` 时出现。
2. `expired` —— 有到期时间且已过。**宽限期（grace）内也算 expired**：宽限是产品端软着陆策略，不改变"已到期"这一台账事实。
3. `trial` —— 未到期的试用授权（avatarhub：`edition=="trial"` 或 `lic_id` 以 `trial-` 开头；chengjie：payload `trial=true`）。已到期的试用按规则 2 记 `expired`。
4. `active` —— 其余有效授权（含永久）。
5. `unknown` —— 无法判定：未激活的兑换码（有效期未起算）、仅有完成标记的履约订单、token 解析失败等。

## 5. 侦察结论：两套系统的授权记录存储与字段映射

### 5.1 avatarhub 系

**签发链路**：三条路，最终都是同一把私钥（`secrets/license_vendor_sk.pem`）签 `{payload, sig, alg}`：
- 离线：`license_admin.py issue` → 直接产出 `license.key` 文件（**不留台账**，仅打印到控制台）；
- 在线激活服务：`license_server.py`（addcode 发兑换码 / activate 激活占座 / trial_upgrade 试用升级 / renew 续费），台账落 `secrets/`；
- 官网自动履约：`fulfill_orders.py`（拉官网已到账订单→本地签发→回填官网）与 `sign_worker.py`（拉官网签发队列→签名→回填），**授权细节留在官网订单库**（website 侧），引擎本地只留完成标记。

**引擎侧持久化台账**（默认都在 `engines/avatarhub/` 下，`secrets/` 已 gitignore，只在厂商机存在）：

| 文件 | 内容 | 结构要点 |
|---|---|---|
| `secrets/orders.json` | **兑换码台账（主台账）** | `{"codes": {"<兑换码>": {edition, days, seats, licensee, features, created, disabled?, via?, fp_hint?, activations: [{fingerprint, issued, expires, lic_id}]}}, "qi_opened": {...}}` |
| `secrets/trials.json` | 一键试用台账（一机一次） | `{"fps": {"<机器指纹>": {issued, expires, lic_id("trial-…"), notified_48h?}}}` |
| `secrets/fulfilled_orders.json` | 官网订单履约标记 | `{"done": {"<订单号>": <完成时间戳>}, "reminded": {...}}`（无档位/指纹细节） |
| `license.key`（引擎根） | 本机当前生效授权（客户侧） | `{"payload": {v, lic_id, machine, edition, licensee, issued, expires, features?}, "sig", "alg"}` |
| `revocations.json`（引擎根） | 已签名吊销名单 CRL | `{"payload": {v, updated, revoked: [{lic_id?/machine?/licensee?/issued?, reason, ts}]}, "sig"}` |

**字段映射（avatarhub → 归一化）**，按记录子类（`raw.kind`）：

| 归一化字段 | `orders_activation`<br>（codes[\*].activations[\*]） | `orders_code_unactivated`<br>（无激活的兑换码） | `trial_upgrade`<br>（trials.fps[\*]） | `local_license_key`<br>（license.key.payload） | `fulfilled_order`<br>（fulfilled_orders.done[\*]） |
|---|---|---|---|---|---|
| `source_key` | `lic_id`（缺则 `act:<sha16>`） | `code:<兑换码>` | `lic_id`（缺则 `trial:<指纹>`） | `lic_id`（缺则 `local:<sha16>`） | `order:<订单号>` |
| `edition` | 所在码的 `edition` | `edition` | null（引擎签发时固定 pro 档，但台账记录本身无此字段，不虚构） | `edition` | null |
| `seats` | `1`（一次激活占一座） | 码的 `seats`（总座位数） | null | null | null |
| `customer_name` | 所在码的 `licensee` | `licensee` | null | `licensee` | null |
| `machine_fingerprint` | `fingerprint` | null | 键本身（指纹） | `machine`（`*`=站点授权） | null |
| `issued_at` | `issued`（unix 秒→ISO） | `created` | `issued` | `issued` | 完成时间戳 |
| `expires_at` | `expires`（0→null 永久） | null（未激活未起算） | `expires` | `expires`（0→null） | null |
| `status` | §4 口径 | `disabled`→revoked，否则 unknown | §4（通常 trial/expired） | §4 口径 | unknown |
| `plan` / `product_id` / `sku_id` / `customer_contact` | 均 null（见 §6/§7） | 同左 | 同左 | 同左 | 同左 |

去重顺序：activations → 未激活码 → trials → 本机 license.key → 履约标记；同 `source_key` 首见优先（本机 license.key 的 `lic_id` 若已被 orders 台账覆盖则跳过）。

注意：`licensee` 在官网自动履约链路里被写成**客户联系方式**（`fulfill_orders.py` 用 `o.contact` 填 licensee），所以 `customer_name` 里可能出现联系方式明文——导入侧按敏感字段对待。

### 5.2 chengjie 系

**签发链路**：`scripts/license_tool.py issue`（离线 CLI）用私钥（`config/.vendor_license_private.pem`，hex 存储）签发 token 并写到 `--out`。**关键侦察结论：签发端不留任何台账**——payload 只打印到控制台，没有 orders.json 之类的持久记录。

**引擎侧持久化数据**：

| 文件 | 内容 | 是否发放记录 |
|---|---|---|
| `config/license.key`（默认路径，`LicenseManager._default_license_path()`） | 当前部署生效的授权 token（单条）；也可经 `LICENSE_KEY` 环境变量注入 | ✅ 唯一可导出的授权记录 |
| `config/license_quota.db`（SQLite，表 `license_char_usage(lic_id, day, category, chars)`） | 字符额度**用量**计量 | ❌ 是用量不是发放记录，不导出 |

**token payload 字段**（`src/licensing/license_manager.py` 文档表）：`sub`（客户名）、`plan`（community/basic/pro/flagship）、`iat`/`exp`（unix 秒，exp 缺省或 0=永久）、`seats`（0=不限）、`channels`（渠道列表）、`features`（功能位）、`grace_days`（默认 7）、`lic_id`、`included_chars`（0=不限）、`trial`（bool）。

**字段映射（chengjie → 归一化）**：

| 归一化字段 | 取值 |
|---|---|
| `source_key` | `payload.lic_id`；缺失时 `token:<sha256(token) 前 16 位>` |
| `plan` | `payload.plan` |
| `seats` | `payload.seats`（**0=不限，原样保留**；payload 缺该键才是 null） |
| `customer_name` | `payload.sub` |
| `issued_at` / `expires_at` | `payload.iat` / `payload.exp`（unix 秒→ISO；0/缺省→null=永久） |
| `status` | §4 口径（`trial=true` → trial；过期→expired） |
| `edition` / `machine_fingerprint` / `customer_contact` / `product_id` / `sku_id` | 均 null（chengjie 无这些概念，见 §6/§7） |
| `raw` | `{kind, origin(来源文件), token_sha256, payload}`——**不含 token 原文**（见 §8） |

`channels` / `features` / `included_chars` / `grace_days` 不设归一化字段，保留在 `raw.payload`。

导出器支持的输入形态（`--input`）：单个 token 文件（默认 `engines/chengjie/config/license.key`）/ 目录（递归收集其中 `*.key`）/ `.jsonl` 台账（每行一个 token 字符串、`{"token": "..."}` 或直接 payload 对象——为厂商日后手工维护签发台账预留；行内可显式带 `customer_name` / `customer_contact` / `product_id` / `sku_id` / `revoked` 等归一化同名字段，导出器透传优先于 payload 推导）。

## 6. product_id / sku_id 映射规则与已知局限

**现状：两套授权载荷里都没有产品标识**。avatarhub 授权是"整机 Hub"级（edition 管能力位，不区分幻声/幻影/幻颜/通传）；chengjie 的 plan/channels 同样区分不了智聊 vs 通译。因此**存量记录的 `product_id`、`sku_id` 一律为 null**，原始 edition/plan 保留在同名字段与 `raw`，集团账本可后期人工/规则回填。

**预留的尽力映射**：若原始记录（payload 或台账行）日后带上 `product_id`/`product` 或 `sku_id`/`sku` 字段，导出器自动透传——`product_id` 须在七产品 id 集合内，`sku_id` 在 `sku_registry.json` 可读时校验存在性（不存在→置 null，原值仍在 raw）。这样未来签发侧只要在 payload 里加一个字段，台账即自动归位，导出器无需再改。

## 7. 已知局限（映射拿不准的点）

1. **customer_contact 恒 null**：两侧台账均无独立联系方式字段。avatarhub 官网履约链路把联系方式塞进了 `licensee`（→ `customer_name`），无法可靠拆分，不猜。
2. **avatarhub 试用台账的 edition**：`trial_upgrade` 签发时引擎固定 pro 档试签，但 `trials.json` 记录本身无 edition 字段，归一化记 null（不虚构原始数据）。
3. **未激活兑换码的 expires_at=null 是"未知"不是"永久"**：码的 `days` 在激活时才起算；`days` 在 `raw` 里。
4. **`fulfilled_order` 记录信息量极少**（仅订单号+完成时间）：授权细节在官网订单库，等 website 侧订单数据进集团账本后按订单号关联。
5. **离线签发无台账**：avatarhub `license_admin.py issue` 与 chengjie `license_tool.py issue` 直接产出授权文件不留档。这部分历史发放只能靠回收客户侧 license.key（导出器支持 `--input` 指向收集来的 key 文件/目录）补录。
6. **导出器不做 Ed25519 验签**（纯标准库、无 cryptography 依赖）：`invalid`（被篡改）状态检测不了；CRL 匹配只按字段不验名单签名。台账定位是经营对账不是防伪，防伪仍由产品端验签负责。

## 8. 安全注意

- **chengjie 的 `raw` 不含 token 原文**：完整 token 即可直接激活的授权凭证，进台账等于扩散授权码，故以 `token_sha256` 摘要替代（payload 已完整保留，对账无损失）。这是"raw 原样保留"的唯一例外。
- avatarhub 的 `raw` 不带 `sig` 签名串（防拼回完整 license.key），payload 全量保留。
- 机器指纹、licensee/联系方式属敏感经营数据：导出文件与集团账本按 `secrets/` 同级密级管理，勿入 git、勿贴聊天。

## 9. 导入流程（website 侧，预留说明）

导入脚本 `website/scripts/ledger-import-licenses.mjs` 由 website 侧同事开发，本节为接口约定：

1. 在厂商机（avatarhub 台账所在机）/ 部署机（chengjie）上跑导出：

```powershell
python tools/license_ledger/export_avatarhub.py --out avatarhub_licenses.json
python tools/license_ledger/export_chengjie.py  --out chengjie_licenses.json
```

2. 把导出 JSON 交给导入脚本（预期用法，以同事最终实现为准）：

```bash
node website/scripts/ledger-import-licenses.mjs avatarhub_licenses.json chengjie_licenses.json
```

3. 导入方约定：
   - 校验顶层 `version==1`、`source_system` 与每条记录一致（schema 表达不了跨字段约束，导入方补验）；
   - 按 `(source_system, source_key)` upsert，重复导入幂等；
   - `records` 为空是合法输出（数据源缺失时导出器输出空数组并在 stderr 警告，退出码 0）；
   - 导出文件可先用 `python tools/license_ledger/validate_export.py <file>` 自检（最小 schema 校验，纯标准库）。

联调不依赖真实数据：两个导出器都带 `--demo`（生成 3 条覆盖主要形态的演示记录）。
