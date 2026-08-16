# 实施21 · avatarhub 授权核查（无需转正）+ 三私钥离线备份 + 客户治理

> 日期：2026-07-19 ｜ 上承：实施20（客户归并 / 协议登录通道 / 快照守护）
> 主题：核查 avatarhub 授权体系是否需转正（结论=不需要），落实其最高优先运维风险，并继续客户数据治理。

---

## 一、avatarhub 授权体系核查结论：已是正式密钥，无需转正 ✅

（只读核查，私钥仅看元数据/PEM 头，全程不外传内容）

- 架构：**厂商持私钥签发、产品内置公钥离线验签**（Ed25519 全链）。公钥是
  `engines/avatarhub/license.py` 的 `_VENDOR_PUBKEY_PEM` 常量，`license_admin.py keygen`
  时自动内置；三条签发路（离线 issue / 在线 activate-refresh / 官网订单 fulfill+sign_worker）
  共用幻声机 `secrets/license_vendor_sk.pem`；官网 VPS 只做订单载体不持私钥。
- **与 chengjie 转正前处境本质不同**：chengjie 是"代码躺 demo 公钥、无配对自有私钥"；
  avatarhub 从 2026-06-15 起就是自有 keygen 产的正式密钥对——私钥仓外（gitignore 三重覆盖）、
  内置公钥与生产私钥**配对验证一致（指纹 c173838d）**、已在真实营收链路签发过 2 笔官网订单 + 1 笔试用。
- **无 demo 残留**；.104/.140 算力节点不做验签、无需同步公钥。

## 二、三私钥离线备份（核查报告 §四.1 最高优先风险，已落实）⭐

**风险**：`license_vendor_sk.pem` 全球唯一一份，盘损 = 全部已发授权无法重签/续签。

**处置**：从幻声机 .176 `secrets\` 拉三把私钥 + 一把公钥到 **.117 仓外离线区**
`D:\key-backups\avatarhub-176-20260719\`（SSH 加密传输、落地 gitignore 外目录，
与 chengjie 私钥同款离线posture）：
- `license_vendor_sk.pem`（授权签发）/ `release_sign_ed25519_sk.pem`（发布签名）/
  `rollout_control_ed25519_sk.pem`（放量控制）——三把私钥 Ed25519 加载校验全通过；
- 附 `README.txt` 说明来源/指纹/恢复步骤；
- **两机冗余已成**（.176 生产 + .117 备份），彻底消除单盘单点。

**留给老板**：再复制一份到真正离线介质（U 盘/加密云盘）与生产机物理隔离——这是最后一层。

## 三、avatarhub 次要风险（记录，非本轮修）

- **`/api/refresh` 404**：客户端周期刷新打向 `bd2026.cc/api/refresh`（`license.py`
  `_DEFAULT_ACTIVATION_URL`），但该端点是 `license_server.py` 的、官网 VPS 未实现 →
  "后台续费自动生效"实际不通。**当前影响为零**（现有 avatarhub 授权都是 pro 永久
  `expires_ts=0`，不需刷新）；等有时间限授权业务时再决策：官网实现该端点 vs 客户端改指向
  真实 license_server。属架构决策，下轮评估。
- **授权钥无双钥轮换**：`license.py` 验签是单公钥常量，`keygen --force` 会令已发授权全失效。
  低频风险，记录在案。

## 四、客户数据治理（归并实时化 + 测试数据标记）

（并行 agent 交付：见 ledger 侧改动）
- **A 归并实时化**：新订单/新留资入账即自动建客户主档（复用 link-customers 归并规则，
  fail-safe 不阻断入账）；
- **B 测试数据标记**：schema 加 is_test，e2e/@internal/test/drill/smoke 信号打标；
  KPI 与商机默认排除测试数据；客户 360 显示"测试"徽章但不隐藏。
- 目的：让首份真实 KPI 周报与商机清单不被 9 笔 cancelled 测试单 + e2e 客户污染。

## 五、扫码接入（等老板）

固定版扫码桥 `D:\chengjie-instances\zhiliao\tg_backend_scan.py`（引擎正路 pyrogram
provider，20 分钟窗口，自刷新页）已弹出——老板扫一次即接入总机。

## 六、相对方案的二次优化

1. **核查先行避免无用功**：本打算给 avatarhub 也走一遍转正流程（改公钥+重启 hub），
   核查发现它本就正式 → 省掉一次幻声机重启窗口与风险，只做备份纪律；
2. **备份走两机冗余而非加密归档**：幻声机无 7z，与其纠结口令管理（口令与密文同地=形同虚设），
   不如立即两机物理冗余，把"离线介质"最后一层明确交回老板；
3. **私钥备份连 release/rollout 一次备齐**：核查发现同目录还有两把发布/放量私钥同样零备份，
   顺手一起备（一次 SSH 往返解决三类密钥单点）。

## 七、下一阶段（实施22 建议）

1. **老板扫码接总机** → 台账置 active → 真机私聊冒烟（顾嘉接待，验证事件/收件箱全链）；
2. **归并实时化生产实跑 + 测试数据打标生产实跑**（本轮 agent 交付代码，验证后我部署）；
3. **avatarhub `/api/refresh`**：决策端点归属（低优先，有限时授权业务时再做）；
4. **私钥离线介质**：老板把 `D:\key-backups\` 复制到 U 盘/加密云盘；
5. 07-26 两节点：群聊开关评估 + enforce 灰度（enforce_readiness 每日 READY 留痕中）。
