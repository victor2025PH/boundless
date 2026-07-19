# 实施20 · 客户主档自动归并 + 协议号登录通道就绪 + avatarhub 授权核查

> 日期：2026-07-19 ｜ 上承：实施19（厂商密钥转正 / 双实例授权 / 配置快照守护）
> 主题：让集团账本的「客户」维度活起来（商机引擎的前提），并为老板扫码接入协议号扫清最后障碍。

---

## 一、客户主档自动归并（解锁 customer 360 与商机引擎）⭐

**问题**：生产账本 orders=9/leads=5/licenses=6/personas=32，但 **customers=0**——
订单/留资从来只落原始行、没建客户主档，客户 360 与跨售商机（都要 customer_id）全程空转。

**方案**：`website/scripts/ledger-link-customers.mjs`（幂等）——
- 扫四表联系标识，按**强信号归并**（规范化 tg handle / tgid / email），纯名字仅作弱回退；
- 无主行→建客户主档（cust_ ULID）+ identities 登记 + 回填各表 customer_id；audit 留痕；
- 铁律：绝不删数据、绝不合并两个已存在客户、已归属行跳过。

**生产实跑结果**：dry-run 预演 → 实跑新建 2 客户 + 归并 4 身份 + 回填 3 行；
**二次跑零新增（幂等验证通过）**。当前生产数据里真实客户信号少（多为 e2e 测试单），
但主档结构与归并链路已就绪——真实订单进来即自动成档。

**接线建议（已记录未实施）**：backfill 双写钩子层可在新订单/留资入账时顺带调归并，
让"建档"实时化而非靠定时脚本；下轮可做。

## 二、协议号登录通道就绪（为老板扫码铺平最后一步）

- 实例 overlay 补 `platform_login.enabled + telegram.protocol_enabled=true`——
  之前后台「账号管理→扫码新增」发起 TG 登录报"protocol 登录方式暂未启用"，就是缺这段；
- 开启后 API `/api/platforms/telegram/login/start` 正常返回 `qr_url + qr_image`（pyrogram
  provider 已注册，引擎正路）；
- 配套 `D:\tmp\tg_backend_qr.py`（临时桥）：调后台登录接口把二维码渲染成自刷新浏览器页 +
  轮询 status 到 authorized——老板扫一次即接入，session 由引擎 registry 管理（支持多号）。
  本轮已弹出扫码页两轮（各 20 分钟窗口），等老板扫。

## 三、通译触点品牌收尾

- 通译 web_chat 的英文占位 greeting/title 改为通译 LingoX 双语品牌口径。

## 四、卫生整改

- `engines/chengjie/config/events/`（运行时 telemetry spool）**误入 git 暂存**——已撤回并
  加 `.gitignore`（引擎侧），运行数据不进版本库。

## 五、配置快照守护实证有效

本轮给智聊 overlay 加 `platform_login` 段后，10 分钟快照任务**自动捕获**该变更
（`git -C …config log` 可见 diff）——实施19 建的竞态防护第一次真实证明：任何配置改动
现在都有 git 留痕可回溯。

## 六、avatarhub 授权核查结论

（并行 agent 只读核查，见其报告）——用于决策 avatarhub 是否需要像 chengjie 那样转正密钥，
结论并入下一阶段行动项（若为 demo 残留，等幻声机 hub 切仓窗口一并转正，避免多次重启）。

## 七、相对方案的二次优化

1. **归并"宁缺毋滥"**：纯名字弱信号只在无其它标识时建档并标注来源，避免误并不同客户；
2. **协议登录桥走后台 API 而非独立 telethon**：彻底贴合引擎（实施18 的教训——telethon
   session 与引擎 pyrogram 不兼容），session 由 registry 统一管理、天然支持多号；
3. **归并脚本先 dry-run 预演再实跑**：生产数据零风险，先看清 49 行无标识都是什么再落库。

## 八、下一阶段（实施21 建议）

1. **老板扫码接入总机**（扫码页已弹）→ 渠道台账总机置 active → 真机私聊冒烟（顾嘉接待）；
2. **归并实时化**：backfill 双写钩子接入自动建档，新单即成客户；
3. **avatarhub 密钥转正**（按 agent 核查结论，等 hub 切仓窗口）；
4. **测试数据归档**：9 笔 cancelled + e2e 客户标注为测试（Console 加 is_test 标记或归档视图），
   让 KPI/商机只算真实数据；
5. 07-26 两节点不变：群聊开关评估 + enforce 灰度（enforce_readiness 已每日 READY 留痕）。
