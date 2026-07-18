# 无界 Boundless 全域统一 ID 规范

- **状态**：已拍板（P0 契约层，先立契约后落库）
- **适用范围**：集团统一后台（集团账本）、七个产品独立后台、三个引擎、官网，凡新建的业务实体主键一律遵循本规范
- **参考实现**：本目录 `ids.py`（Python，仅标准库）；TypeScript 实现落地时必须通过本文 §6.2 的已知答案向量
- **本文档与 `ids.py` 中的常量（正则、字母表、前缀表）互为镜像，改任何一处必须同步另一处**

> ⚠️ **先读这个坑：顶层目录 `platform` 与 Python 标准库 `platform` 模块同名。**
> **不要**把仓库根加入 `sys.path` 后 `import platform.identity`——标准库的 `platform` 常规模块会赢得解析，import 直接失败，且行为随路径顺序微妙变化。也**绝对不要**给 `platform/` 补 `__init__.py`（会遮蔽标准库，殃及所有调用 `platform.system()` 的第三方库）。
> 推荐用法见附录 A：把 `platform/identity/` 目录本身加入 `sys.path` 后 `import ids`，或用 `importlib.util.spec_from_file_location` 按文件路径加载。

---

## 1. 目标与动机

矩阵内有多套彼此独立的存储：官网（Next.js + JSON 文件存储）、七个产品后台（各自建库）、三个引擎（Python）、集团账本（统一后台）。同一个客户会在多处留下痕迹：官网留资、下单、开通授权、产品内产生事件。

**跨库关联靠全域唯一 ID，不靠跨库 join。**任何系统把记录交给另一个系统（上报集团账本、引擎回写产品后台）时，只传 ID；接收方不需要访问发送方的库就能建立关联。这要求 ID 满足：

1. **全域唯一**：任何两个系统独立生成的 ID 永不冲突（80 bit 加密随机数保证，无需中心发号器）；
2. **自带类型**：看到 `ord_...` 就知道是订单，跨系统传递时不需要伴随"这是什么表的 ID"的带外信息（前缀保证）；
3. **时间有序**：跨毫秒的 ID 按字典序即按生成时间序，落库即近似追加写，索引友好（ULID 的 48 bit 毫秒时间戳保证）；
4. **无协调、无状态**：生成 ID 不需要连库、不需要锁、不需要发号服务，官网 Node 进程和引擎 Python 进程各自生成即可。

对比曾经的做法：官网订单号 `AH-20260718-XXXXXX` 用 `Math.random()` 取 6 位 base36，非加密随机、仅日期内去重，量大后有撞号风险；留资主键 `tg:<id>` / `c:<contact>` 把业务含义编进了主键，联系方式一变主键就变。这些遗留 ID 的处置见 §4。

## 2. 格式定义

```
<prefix>_<ULID>
```

| 组成 | 规则 |
| --- | --- |
| `prefix` | 2–5 个小写 ASCII 字母，必须已在 §3 注册表登记 |
| 分隔符 | 固定一个下划线 `_` |
| `ULID` | 26 字符，Crockford Base32 **大写**，高 48 bit 为 Unix **毫秒**时间戳（UTC），低 80 bit 为**加密安全**随机数 |
| 总长 | 29–32 字符（随前缀长度），存储建议 `VARCHAR(32)`，整串作为不透明主键 |

**校验正则（唯一权威，所有语言逐字符一致）：**

```
^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$
```

**Crockford Base32 字母表（32 字符，按码点升序，不含 I、L、O、U）：**

```
0123456789ABCDEFGHJKMNPQRSTVWXYZ
```

**位布局与编码**：把 48 bit 时间戳左移 80 位、按位或上 80 bit 随机数，得到 128 bit 整数；从最高位起每 5 bit 一组查字母表，得 26 字符（26×5=130 bit，最高 2 bit 恒为 0）。因此**合法 ULID 的首字符必然在 `0`–`7`**；正则本身放行到 `Z`，完整校验必须额外做该溢出检查（`is_valid` 已内置，两级校验：正则 + 前缀已注册 + 首字符 ≤ `7`）。

**解码取严格模式**：只接受规范形（大写、无连字符、无 I/L/O/U），**不做** Crockford 原始提案里的宽松纠错（小写归一、`I/L→1`、`O→0`）。宽松解码会让"同一个 ID 有多种写法"，破坏字符串相等即 ID 相等的原则。

示例（`ids.py` 实际生成）：

```
cust_01KXRVYDNRXDDVYVASN96TN631
ord_01KXRVYDNRF058A2WZJP3DXP5D
evt_01KXRVYDNRKGZ1HQP10RPA1B9M
```

**不变性**：ID 一经签发永不变更、永不复用。实体"删除"用状态字段表达，不回收 ID。

## 3. 前缀注册表

### 3.1 本期前缀（全部 7 个）

新增前缀必须走 §8 流程；未注册的前缀即使匹配正则也判非法（`is_valid` 返回 `False`）。

| 前缀 | 含义 | 属主系统 | 示例 |
| --- | --- | --- | --- |
| `cust` | 客户（自然人身份，跨产品唯一） | 集团账本 | `cust_01KXRVYDNRXDDVYVASN96TN631` |
| `org` | 组织（企业客户/团队） | 集团账本 | `org_01KXRVYDNR65D1SHGE3XPJSK56` |
| `ord` | 订单 | 集团账本 | `ord_01KXRVYDNRF058A2WZJP3DXP5D` |
| `lic` | 授权（许可实例，非 SKU） | 集团账本 | `lic_01KXRVYDNRFAY36N5GXCQ6XBWP` |
| `prs` | 人设（persona，数字形象/账号人格） | 引擎 | `prs_01KXRVYDNRV4K43YH50XTR3VC7` |
| `evt` | 事件（审计/埋点/状态变迁流水） | 集团账本（各系统生成、账本归集） | `evt_01KXRVYDNRKGZ1HQP10RPA1B9M` |
| `wsp` | 工作区（workspace，产品内协作空间） | 引擎 | `wsp_01KXRVYDNR5AP7F1JKHR6MM45E` |

"属主系统"指该类实体的**权威记录**归谁：属主负责该 ID 对应实体的生命周期与最终一致性；其他系统只持有 ID 引用。`evt` 特殊在于任何系统都可以生成事件 ID（生成无需协调），但事件的归集与查询权威在集团账本。

### 3.2 例外：SKU 不用生成式 ID

SKU（可售卖项）**不适用**本规范，不加前缀、不生成 ULID。SKU 的唯一事实源是
`platform/licensing/sku_registry.json`，沿用其中语义化的 `sku_id`（当前形如
`voicex-pro`、`lingox-team`、`chatx-flagship`）。理由：

- SKU 是**目录数据**（catalog），量小、人工定义、需要人读人记（出现在报价单、订单摘要、对账单里），语义化命名是功能不是缺陷；
- SKU 的生命周期由产品定价决策驱动，不是运行时批量生成的实体，不需要无协调发号。

订单、授权等运行时实体引用 SKU 时，直接存 `sku_id` 字符串即可。若日后 SKU 命名演进（例如出现 `lingox.pro.month` 之类的复合形式），仍由 `sku_registry.json` 定义，与本规范无关。**除 SKU 外，不再豁免任何新实体类型。**

## 4. 遗留 ID 共存与映射策略

官网已在生产运行，存量 ID 不重写、不迁移改号。原则：**集团账本内部主键一律用新 ID；遗留 ID 降级为 `source_key`，用于对外展示、客户沟通与对账回查。**

### 4.1 现存两类遗留 ID

**① 官网订单号**（`website/lib/order-store.ts` 第 74–79 行的 `newOrderId()`）：

```ts
function newOrderId(): string {
  const d = new Date();
  const ymd = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  const rand = Math.random().toString(36).slice(2, 8).toUpperCase();
  return `AH-${ymd}-${rand}`;
}
```

形如 `AH-20260718-XK3F9Q`。特征：客户可读可念（用于 Telegram 客服沟通、`/order?check=` 自助查询、客户端激活时手输），但随机段仅 6 位 base36 且来自非加密的 `Math.random()`。

**② 官网留资去重键**（`website/lib/lead-store.ts` 第 55–58 行的 `dedupKey()`）：

```ts
function dedupKey(rec: { tg_user_id?: string; contact: string }): string {
  if (rec.tg_user_id) return `tg:${rec.tg_user_id}`;
  return `c:${rec.contact.trim().toLowerCase().replace(/\s+/g, "")}`;
}
```

形如 `tg:8412345678` 或 `c:someone@mail.com`。特征：主键即联系方式，天然做了"同一联系人只留一条"的去重，但联系方式变更即产生新键，且把 PII 编进了主键。

### 4.2 映射规则

集团账本导入/关联遗留数据时，每条记录携带三元组：**内部主键（新 ID）+ `source`（来源系统枚举）+ `source_key`（遗留 ID 原文）**，并对 `(source, source_key)` 建唯一索引，保证重复导入幂等（同一遗留记录永远映射到同一个新 ID）。

| 遗留 ID | source | source_key（原样保留） | 集团账本内部主键 |
| --- | --- | --- | --- |
| `AH-20260718-XK3F9Q` | `website-order` | `AH-20260718-XK3F9Q` | 新发 `ord_...` |
| `tg:8412345678` | `website-lead` | `tg:8412345678` | 新发 `cust_...` |
| `c:someone@mail.com` | `website-lead` | `c:someone@mail.com` | 新发 `cust_...` |

约定：

- **官网侧不改**：官网继续签发 `AH-` 订单号并作为自己库的主键、继续用 `dedupKey` 去重（客户沟通与自助查询依赖它们）。等官网接入统一 ID 时再在创建订单/留资的同时生成新 ID，`AH-` 号降级为展示别名字段；
- **对外展示/对账**用 `source_key`（客户认识 `AH-` 号；链上打款尾数核销也按官网订单查）；**系统间引用**（事件、授权、报表）一律用新 ID；
- 同一自然人的多个留资键（换过联系方式、又绑了 TG）允许多条 `(source, source_key)` 映射到**同一个** `cust_...`——这正是遗留键做不到而新 ID 要解决的；
- 集团账本中禁止把 `source_key` 当外键连表；它只是回查凭据。

## 5. 排序性

- **保证**：不同毫秒生成的两个 ID，字典序 = 时间序。因为字母表按码点升序、ULID 定长 26 字符、时间戳在高位。参考实现自测里 sleep 2ms 生成两个 ID 验证此性质；
- **不保证**：同一毫秒内的多个 ID 顺序随机（低 80 bit 是独立随机数）。**本规范明确不要求同毫秒单调递增**，任何实现不得私自加"同毫秒 +1"之类的单调逻辑——那会引入进程内状态与跨进程不一致，还把随机位变成可预测的计数器；
- **排序只在同前缀内有意义**：前缀长度不一（2–5 字符），跨前缀比较字符串没有业务含义；
- ID 里的时间戳是**生成时刻的元数据**，用于调试与粗排。业务时间（支付时间、开通时间）必须落显式字段，不得从 ID 反推。

## 6. 跨语言实现要求

### 6.1 硬性要求

TypeScript 与 Python（以及未来任何语言）的实现必须**逐字节兼容**：

1. 使用与本文 §2 **完全相同**的正则字符串与字母表字符串（建议直接复制粘贴，禁止手打）；
2. 相同输入 `(timestamp_ms, randomness_10bytes)` 必须编码出**逐字节相同**的 26 字符 ULID；解码互逆；
3. 随机源必须是 CSPRNG：Python 用 `secrets`，Node/TS 用 `crypto.randomBytes` 或 `crypto.getRandomValues`，**禁止** `Math.random()` / `random` 模块；
4. 时间戳取 Unix 毫秒（UTC）：Python 用 `time.time_ns() // 1_000_000`（避免浮点毫秒截断歧义），TS 用 `Date.now()`；
5. 校验必须两级：正则 + 前缀在注册表内（+ 首字符 ≤ `7` 的溢出检查，见 §2）；
6. 新语言实现落地前必须通过 §6.2 全部已知答案向量，并跑通与 `ids.py --selftest` 等价的自测集。

### 6.2 已知答案向量（Known-Answer Tests）

| timestamp_ms | randomness（hex） | 期望 ULID |
| --- | --- | --- |
| `0` | `00000000000000000000` | `00000000000000000000000000` |
| `281474976710655`（2⁴⁸−1） | `ffffffffffffffffffff` | `7ZZZZZZZZZZZZZZZZZZZZZZZZZ` |
| `1784332800000`（2026-07-18T00:00:00Z） | `00112233445566778899` | `01KXS8BM00008J4CT4ANK7F24S` |

### 6.3 Python 实现陷阱

- **必须用 `re.fullmatch`**：Python 的 `$` 允许结尾换行，`re.match(pattern, "cust_...\n")` 会误放行；
- `platform` 目录同名问题见文首警告与附录 A。

### 6.4 TypeScript 实现要点

- 128 bit 运算用 `BigInt`（`(BigInt(ts) << 80n) | rand`），或纯字节位移实现，禁止 `number`（53 bit 精度不够）；
- JS 正则 `^...$`（不带 `m` 标志）没有 Python 的换行陷阱，`new RegExp(ID_PATTERN).test(x)` 语义与 `fullmatch` 一致；
- 官网落地时建议放 `website/lib/ids.ts`，常量与本文保持镜像。

## 7. 反模式（禁止事项）

1. **禁止自增主键做业务实体 ID**：泄露业务量、跨库必撞、迁移分库即断；数据库内部仍可用自增列做物理主键优化，但**不得外泄**出该库；
2. **禁止在 ID 中编码业务含义（前缀除外）**：不要把产品线、地区、SKU、日期段拼进 ID（`AH-20260718-` 的日期段就是教训——信息该落字段，不该落主键）。业务属性变了主键不能变；
3. **禁止用 UUIDv4 混用替代**：v4 无时间有序性，且两套格式并存会让校验与索引策略分裂；
4. **禁止截断 ID**（取前 N 位当短码）：截掉随机位即制造碰撞。需要短码另建映射表；
5. **禁止解析 ID 做业务逻辑**：不得用 ID 里的时间戳当"创建时间"字段、不得按前缀路由核心流程（前缀用于人读与完整性校验，路由该看显式类型字段）；
6. **禁止大小写不敏感比较**：ID 是大小写敏感的精确字符串，存储与比较一律原样；
7. **禁止无协调地"优化"生成逻辑**：如加同毫秒单调计数器（见 §5）、换字母表、改随机位数——任何变更走 §8。

## 8. 变更管理

新增前缀流程：

1. 确认新实体确属**运行时生成**的业务实体（目录类数据参考 §3.2 SKU 例外单独评估）；
2. 前缀取 2–5 个小写字母，语义明确、与现有前缀无混淆；
3. 同步修改三处并在同一提交内落地：本文 §3.1 表格、`ids.py` 的 `PREFIXES`、TypeScript 端常量；
4. 跑通 `python platform/identity/ids.py --selftest`。

正则、字母表、位布局属于**冻结契约**，原则上不再变更；确需变更视同发布 ID v2，须全量评估存量数据兼容性。

---

## 附录 A：参考实现 `ids.py` 用法

API：`new_id(prefix)`、`is_valid(id_str)`、`parse(id_str)`（返回 `prefix` / UTC `timestamp` / `randomness` / `timestamp_ms`）、`ulid_encode` / `ulid_decode`、常量 `PREFIXES` / `ID_PATTERN` / `CROCKFORD_ALPHABET`。

因为顶层目录 `platform` 与标准库模块同名（见文首警告），**加载方式二选一**：

```python
# 方式 A：把本目录加入 sys.path，import ids
import sys
sys.path.insert(0, r"D:\workspace\boundless\platform\identity")
import ids
print(ids.new_id("cust"))          # cust_01KXRVYDNRXDDVYVASN96TN631
print(ids.is_valid("ord_01KXRVYDNRF058A2WZJP3DXP5D"))  # True
print(ids.parse("evt_01KXRVYDNRKGZ1HQP10RPA1B9M").timestamp)  # UTC datetime
```

```python
# 方式 B：importlib 按文件路径加载，不动 sys.path
import importlib.util
spec = importlib.util.spec_from_file_location(
    "boundless_ids", r"D:\workspace\boundless\platform\identity\ids.py")
ids = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ids)
```

自测（覆盖：契约常量、§6.2 已知答案向量、七前缀生成/校验/解析回环、sleep 2ms 时间有序性、1 万个 ID 无重复、16 类非法输入拒绝）：

```powershell
cd D:\workspace\boundless
python platform/identity/ids.py --selftest
```
