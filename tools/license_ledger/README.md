# tools/license_ledger · 授权发放记录只读导出适配器（P1）

把两套授权系统（engines/avatarhub、engines/chengjie）的发放记录导出为集团统一台账的归一化 JSON。
**绝对只读**：不写入/修改 engines/ 下任何文件。仅 Python 标准库。
格式定义与字段映射详见 `platform/licensing/ledger/`（README + `ledger_import.schema.json`）。

## 用法

```powershell
# avatarhub（默认读 <repo>/engines/avatarhub 下的 secrets/orders.json、secrets/trials.json、
# license.key、secrets/fulfilled_orders.json；revocations.json 仅作吊销状态标注）
python tools/license_ledger/export_avatarhub.py --out avatarhub_licenses.json

# chengjie（默认读 <repo>/engines/chengjie/config/license.key；签发端不留台账，
# 单机通常只有 0~1 条）
python tools/license_ledger/export_chengjie.py --out chengjie_licenses.json

# 数据源覆盖（--input）：
python tools/license_ledger/export_avatarhub.py --input D:\vendor\avatarhub            # 引擎目录
python tools/license_ledger/export_avatarhub.py --input D:\vendor\avatarhub\secrets    # secrets 目录
python tools/license_ledger/export_avatarhub.py --input D:\backup\orders.json          # 单个台账文件（按结构自动识别）
python tools/license_ledger/export_chengjie.py  --input D:\collected_keys\             # 目录：递归收集 *.key（补录回收的客户授权）
python tools/license_ledger/export_chengjie.py  --input D:\ledger\issued.jsonl         # .jsonl 手工台账（每行 token / {"token":...} / payload 对象）

# 管道联调：不读真实数据，各生成 3 条演示记录
python tools/license_ledger/export_avatarhub.py --demo --out demo_ah.json
python tools/license_ledger/export_chengjie.py  --demo --out demo_cj.json

# 自检导出文件是否符合 schema（纯标准库的最小校验器）
python tools/license_ledger/validate_export.py avatarhub_licenses.json chengjie_licenses.json
```

## 行为约定

- 数据源缺失/不可解析：stderr 警告 + 输出空 `records`，**退出码仍为 0**（空导出是合法结果）。
- 时间戳尽力转 ISO8601（UTC）；转不了 → 字段置 null，原值保留在 `raw`。
- `--out` 落在被读目录内会被拒绝（退出码 2）——只读纪律护栏。
- 敏感面：chengjie 的 `raw` 只存 `token_sha256` 不存 token 原文；avatarhub 的 `raw` 不带 `sig`。
  导出文件含指纹/客户名等经营数据，按 secrets 密级处理，勿入 git。
- 导出后交 `website/scripts/ledger-import-licenses.mjs`（website 侧同事开发）导入集团账本，
  幂等键 `(source_system, source_key)`。

## 实时 outbox（avatarhub「签发即导出」，P1 收尾）

除上述**全量导出**外，avatarhub 的三条签发路（`license_admin.py` 离线 issue/revoke、
`license_server.py` 在线激活/试用签发、`fulfill_orders.py`/`sign_worker.py` 官网履约）在各自
**成功点**内嵌了 `engines/avatarhub/ledger_outbox.py` 钩子：每次签发/激活/吊销实时追加一条
归一化记录（与本目录导出器同一 schema 的 `definitions.record`）到本地台账 outbox：

- 默认路径 `engines/avatarhub/secrets/ledger_outbox.jsonl`（secrets/ 已 gitignore，不入库）；
  环境变量 `AVATARHUB_LEDGER_OUTBOX` 可改道（设 `off`/`0`/`none` 整体停写）。
- **一行一条 JSON（JSON Lines，LF）**，append 单次写，多进程并发安全；`raw` 同样不含
  `sig`/token/私钥（写入前递归剥除兜底）。
- 钩子全程 try/except 静默，写失败最多一行 stderr 警告——**任何异常不影响签发主流程**。

### outbox 与全量导出的关系

| | outbox（增量实时） | export_avatarhub.py（全量对账） |
|---|---|---|
| 触发 | 每次签发/激活/吊销成功即追加一行 | 手动/周期跑一次，扫全部台账 |
| 覆盖 | 仅接钩子后的新发放 + 吊销事件行 | 历史存量 + 状态重算（expired/CRL 命中） |
| 独有价值 | 离线 issue（无台账）与官网履约的 payload 细节只有这里有 | disabled/续费（renew）/过期等状态变化的权威快照 |

**两者 source_key 规则完全一致**（激活=`lic_id` 缺则 `act:<sha16(code|fp|issued)>`；试用=`lic_id`
缺则 `trial:<指纹>`；离线/队列签发=`payload.lic_id` 缺则 `local:<sha16(canonical)>`；履约=
`order:<订单号>`；吊销带 `lic_id` 时同原记录 key），幂等键 `(source_system, source_key)` 相同
→ 同一份授权不论从 outbox 还是全量导出进账本都 upsert 到同一行，**可重复、交叉导入**。
outbox 是追加流，同一 key 可能多行（如同机幂等重发、先签发后吊销），导入按行序 upsert 即
后行覆盖前行状态。改 `export_avatarhub.py` 的 key 规则时必须同步 `ledger_outbox.py`（两文件
头部都有对齐契约注释）。

### 导入方法

`website/scripts/ledger-import-licenses.mjs` 即将支持 `.jsonl` 输入（website 侧同事实现中），
届时在厂商机周期执行：

```bash
node website/scripts/ledger-import-licenses.mjs engines/avatarhub/secrets/ledger_outbox.jsonl
```

.jsonl 每行即一条 `record`（无顶层信封，`source_system` 以行内字段为准）。在此之前，outbox
只管持续追加（体积很小，一次发放一行），不影响现有全量导出流程照常对账。

钩子模块可独立自测（临时目录读写，不做真实签发）：

```powershell
python engines/avatarhub/ledger_outbox.py --selftest
```

## 实时 outbox（chengjie「签发即导出」，P1 收尾）

chengjie 侧同机制：唯一签发入口 `engines/chengjie/scripts/license_tool.py`（离线 CLI，仅
genkeys / issue 两个子命令，无 renew/revoke 类命令）在 **issue 成功路径**（token 打印之后）
内嵌 `engines/chengjie/scripts/ledger_outbox.py` 钩子，每次签发实时追加一条归一化记录
（同 schema `definitions.record`）到本地 outbox：

- 默认路径 `engines/chengjie/config/ledger_outbox.jsonl`（已在 `engines/chengjie/.gitignore`
  忽略，不入库）；环境变量 `CHENGJIE_LEDGER_OUTBOX` 可改道。
- 一行一条 JSON（JSON Lines，LF），append 单次写；`raw` 只存 `token_sha256` + 解码后的
  payload，**不含 token 原文/签名/私钥**（与 export_chengjie.py 同口径）。
- 钩子全程 try/except 静默（import 也在 try 内），写失败不打印——**license_tool 的既有
  stdout 输出一个字符都不变**。
- **source_key 规则与 export_chengjie.py 完全一致**：`payload.lic_id`，缺失时
  `token:<sha256 前 16 位>` → outbox 实时记录与全量导出/回收客户 key 补录交叉导入，按
  `(source_system, source_key)` upsert 合并到同一行。outbox 只覆盖挂钩后的新签发，
  历史存量仍靠 export_chengjie.py 补录。

导入（注意：outbox 行是**归一化 record**，不是 `export_chengjie.py --input` 期待的
token/payload 台账行，勿直接喂 export_chengjie）：

```powershell
# outbox → 带 v1 信封的导入 JSON（同 source_key 取最新一行）→ 自检 → 导入
python engines/chengjie/scripts/ledger_outbox.py --export chengjie_outbox_licenses.json
python tools/license_ledger/validate_export.py chengjie_outbox_licenses.json
node website/scripts/ledger-import-licenses.mjs chengjie_outbox_licenses.json
```

待导入脚本支持 .jsonl 直导后（见上节 avatarhub「导入方法」），chengjie outbox 也可整文件
直喂（每行即一条 record，`source_system` 行内自带）。

钩子模块自测（临时目录读写，不做真实签发）：

```powershell
python engines/chengjie/scripts/ledger_outbox.py --selftest
```
