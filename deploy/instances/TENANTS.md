# 托管租户实例运维手册（tenant_ops）

> 托管 SaaS 档「**一客户一实例**」的机器侧操作手册。与本目录 README.md（生产双实例手册）
> 的关系：README 管 zhiliao/tongyi 生产双实例；本手册管**客户租户实例**——同一套引擎与
> 启动器（`start_zhiliao.ps1` 参数化），生命周期由 `engines/chengjie/scripts/tenant_ops.py`
> 一条命令收口。生产实例被 CLI 硬拒在管辖之外（`guard_not_core`），防误操作。
>
> 首次真机验证：2026-08-05 本机试点 `zhiliao_pilot`（见文末记录）。

## 一条命令生命周期

```powershell
cd D:\boundless\engines\chengjie

# 开通（默认干跑只打印规划；--apply = 写盘+junction+登记+拉起+等就绪+交付卡，实测 ~28s）
python scripts/tenant_ops.py provision --customer "Acme Ltd" --apply

# 盘点 / 运行态（只读）
python scripts/tenant_ops.py list
python scripts/tenant_ops.py status

# 欠费暂停（停进程 + 落旗；到期治理在托管档就是停机，比授权码更硬）
python scripts/tenant_ops.py suspend zhiliao_acme_ltd --reason overdue

# 续费恢复（删旗 + 拉起 + 等就绪）
python scripts/tenant_ops.py resume zhiliao_acme_ltd

# 退租数据导出（要求已停；在线打包 SQLite 有一致性风险，--force 才越过）
python scripts/tenant_ops.py export zhiliao_acme_ltd

# 退租 = 暂停 + 导出；数据根保留原地（回滚点，防呆不自动删）
python scripts/tenant_ops.py deprovision zhiliao_acme_ltd

# 公网暴露 / 下线（auto：子域 DNS 就绪走子域+certbot；否则端口形态兜底=主域证书+alt_port）
python scripts/tenant_ops.py expose zhiliao_acme_ltd --slug acme
python scripts/tenant_ops.py unexpose zhiliao_acme_ltd

# 自愈一轮（DOWN 且应在跑 → 幂等拉起；冷却 10min、连败 3 次转人工；--reset <iid> 重置计数）
python scripts/tenant_ops.py watch

# 灾备打包（活库安全：SQLite backup API 快照；含授权+登录态，与 export 口径相反）
python scripts/tenant_ops.py backup --all --keep 14

# 灾备恢复（从备份重建；要求已停，运行中拒恢复防腐化；--backup 指定包/缺省最新；--dry-run 预览）
python scripts/tenant_ops.py suspend zhiliao_acme_ltd --reason dr
python scripts/tenant_ops.py restore  zhiliao_acme_ltd          # ← 恢复后 resume 拉起
python scripts/tenant_ops.py resume   zhiliao_acme_ltd

# AI 中转配置注入（读 .ops\tenant_ai_preset.yaml，保注释写租户 overlay；provision 时自动注入）
python scripts/tenant_ops.py set-ai zhiliao_acme_ltd
```

## 托管开通守护（订单 → 全自动开通+暴露+回填；与装机 license 履约并行）

`scripts/tenant_fulfill_watch.py`——paid **托管订单** → `provision` 实例 → `expose` 公网
→ 回填订单 code=「网址+登录令牌」（website 私信客户）→ state 幂等标记。与
`fulfill_chatx_watch.py`（装机版 license 签发）是**并行的两条履约路径**。

```powershell
python scripts/tenant_fulfill_watch.py --self-test   # 假订单源端到端自测（零网络零副作用）
python scripts/tenant_fulfill_watch.py --dry-run     # 拉真实订单只看不开通（首验）
python scripts/tenant_fulfill_watch.py               # 单次开通轮（计划任务入口）
```

**安全不变量（门禁钉死）**：托管识别**保守**——只认 `order.delivery == "hosted"` 或
`sku_id` 含独立 `hosted` token，**绝不匹配现有 chatx-entry/team/flagship / lingox-\* 装机
SKU**（否则会给装机客户误开实例）。今日线上 SKU 一个都不带该信号 → 守护对现网**零选中**
（已实测 dry-run 打真实订单 API 选 0 单），可安全常驻/误跑。

⚠ **website 上线托管 SKU 时的 co-change**（`src/ops/tenant_fulfillment.py` docstring 有备忘）：
让装机侧 `chatx_fulfillment.fulfillment_payload_for_order` 排除 `delivery==hosted` 单，
防两条守护同抢一单。两边共用同一判据 `is_hosted_delivery`。

**尚缺一步才能真开通**：website 需给托管档 SKU 加交付标（`delivery: hosted`）+ orders API
暴露该字段。守护、provision、expose 全链已就绪并自测通过，届时只是「订单带上标 → 守护自动认领」。

## 运维告警（watch/backup 已接 ops_alert 集团 TG 中继）

- `watch` 连败转人工（give_up）→ `tenant_down` 告警（防抖 4h）；
- `backup` 失败 → `tenant_backup_fail` 告警（防抖 6h）；
- 守护 `provision` 失败 → `tenant_provision_fail` 告警（防抖 1h）。
经 `src/ops/ops_alert.notify`（VPS `/api/ops/alert`，Bearer=EVENT_INGEST_KEY→Telegram）——
与 watchdog/cron_sentinel 同一条中继，老板一处收全部告警；无密钥优雅降级只落日志。

## 公网暴露架构（2026-08-06 全链验证到 VPS 边缘）

```
客户浏览器 → https://<slug>.bd2026.cc（VPS nginx，certbot 每租户证书，443）
          或 https://bd2026.cc:<alt_port>（端口形态兜底，复用主域证书）
        → proxy_pass VPS 127.0.0.1:<web_port>（SSE 禁缓冲/WS upgrade/50m 媒体）
        → SSH 反向隧道（deploy\instances\tenant_tunnel.ps1，计划任务 TenantTunnel 常驻，
          端口清单 .ops\tenant_tunnel_ports.txt，expose 自动维护+踢重连）
        → 117 本机 127.0.0.1:<web_port>（租户实例）
```

- expose 顺序防呆：隧道验通 → （子域形态）certbot http-01（走既有 80 catch-all 的
  ACME 路径，**先签证书再落站点**）→ nginx `-t` 不过即回滚站点文件绝不带病 reload →
  overlay `cookie_secure: true`（保注释）→ 公网探针验收；失败自动分诊
  （VPS 回环 200 + 公网不可达 = 安全组拦截，非链路故障）。
- ⚠ expose 后该实例**本地 http 登录会失效**（secure cookie）——运维调试走公网
  https 或临时翻回 `cookie_secure: false`。
- **两项运营动作（解锁最后一公里，二选一，推荐 ①）**：
  ① 域名商（Dynadot，NS=dyna-ns.net）加 `*.bd2026.cc A 165.154.233.121` 泛解析 →
    重跑 expose 自动升级子域形态（走 443，安全组零改动）；
  ② 云厂商控制台放行租户 alt_port 段（18987/19087/…）→ 端口形态直接可用。

## 客户侧交付与登录（2026-08-06 实测口径）

**凭据职责分离（本轮定案并实施）**：交付给客户的是租户实例内的 **`owner` 账号**
（provision 自动创建，独立随机密码，master 角色）；`web_admin.auth_token` **只留我方
运维应急通道、绝不进交付串**。两条凭据互不相干——真机实测：owner 密码可登、运维令牌
直登仍有效、拿运维令牌当 owner 密码登录被拒。

**为什么必须分离**（实测依据，别退回旧口径）：引擎首启若配了 `auth_token` 且用户表为空，
会**用它当 `admin` 的密码**播种 master 账号——那一串于是同时是「admin 密码」和「令牌直登
凭据」。实测确认**客户改密后令牌直登通道仍然有效**（token 是 config 独立值，不随改密变化），
即交付串一旦泄露，客户改密**不能止血**。分离后客户改密即完全止血，我方通道不受影响。
门禁 `test_delivery_code_never_ships_ops_token` + 守护 self-test 都钉住「运维令牌不得进交付串」。

**客户自助改密链路（已端到端实测通过）**：账号+初始密码登录 → `POST /api/change-password`
（需 `X-CSRF-Token`，页面内自带）→ `{"ok":true}` → 新密码可登、旧密码失效。运维侧可见：
`tenant_ops status` 的「密码」列（`初始`/`已改`/`?`），**按交付卡上的账号名判定**，
只读零写入（刻意不用 `WebUserStore.verify`——它会 UPDATE last_login 污染活库）。

⚠️ **`cookie_secure` 与访问协议必须一致**：`expose` 会把它置 `true`（TLS 后端的正确语义），
此后**该实例的 http 访问登录必失败**（Secure cookie 不在明文回传，表现为登录后弹回 `/login`，
不报错、极易误判成"密码错"）。本机调试需临时置 `false` 并重启实例。

## 客户首登引导（**已存在，别重复建**）

新租户的「三步开通引导」**引擎里早已实现**，本轮只做了接线，没有新建：

- **数据源** `GET /api/setup/checklist` → `src/utils/golive.py::build_checklist`（纯函数）：
  AI 已配置 / 渠道就绪 / 配置自检 / 知识库 / 坐席在线 五项 + 总体红绿灯 + 每项 `action_url`；
- **引导块**：`workspace_dashboard.html` 顶部橙色卡（读上面那个接口）——**绿灯自动隐藏
  不打扰成熟实例**（生产 智聊 安全）、红灯强制显示、黄灯可忽略（localStorage
  `hl_onboard_dismiss`），逐项列「问题 + 去修」按钮；
- **配套向导页**（均主管专属）：`/workspace/setup` 渠道接入向导、`/workspace/kb-start`
  知识库冷启动、`/workspace/golive` 完整自检清单；顶部导航对主管显示这三个入口。

**新租户实测（zhiliao_pilot，2026-08-06）**：清单返回 `light=red`，5 项里
`fail` ×2（AI api_key 占位 / 尚无渠道接入，两者 action_url 都指 `/workspace/setup`）、
`warn` ×2（配置自检 1 警告 / 无坐席在线）、`ok` ×1（知识库 24 条域包种子）——
**内容正是客户进门该看到的**。

✅ **首登错配已根治（2026-08-06）**：
1. `/login?next=` 安全回跳（`src/web/login_redirect.py`，防开放重定向）；未登录页鉴权
   失败也会带 `next=` 回登录页；
2. **master / admin 密码登录默认落地 `/workspace/dash`**（令牌直登仍默认 `/`，运维肌肉记忆）；
3. 交付卡 `login_url` + 交付串网址均为 `…/login?next=/workspace/dash`；
4. `expose` 经 `apply_public_base` 把 `login_url` / `workspace_url` / `start_here_url`
   一并改写为公网基址（此前卡上仍是 127.0.0.1）。

门禁：`tests/test_login_redirect.py` + `test_web_auth.py` 深链/开放重定向用例。

**真机验证（2026-08-06 11:52，zhiliao_pilot resume 实测）**：新启动链
`AITR_INSTANCE_ID=zhiliao_pilot` 注入 ✓（进程命令行可见）→ `/login?next=/workspace/dash`
隐藏域透传 ✓ → 未登录访问 `/workspace/dash` 回跳 `Location=/login?next=%2Fworkspace%2Fdash` ✓
→ **owner 真实登录带 next → 303 直达 `/workspace/golive`** ✓。验证后已回挂 suspended，
试点交付卡三 URL 已刷成深链口径。
⚠ 生产 zhiliao 尚未装载本批 .py（restart_preflight 因兄弟线 ACTIVE 文件 NO-GO，
等安静窗搭车；重启后交付/登录链全量生效）。

**官网托管下单入口（2026-08-06 实施）**：`/order` 页 ChatX 产品线新增「交付方式」切换
（💻 装机版 / ☁️ 云端托管，仅 chatx 家族显示，切走自动回落 installed）：
- 深链 `/order?plan=autochat-team&delivery=hosted` 可直达托管下单（URL 同步可分享）；
- 结算弹窗带「交付方式」行 + 托管专属文案（自动开通/浏览器直用/无需安装）；
- 托管单 POST 带 `delivery:"hosted"` → TG 管理员通知带「☁️ 云端托管（勿手发授权码）」标；
- 订单状态页 activated 时按 delivery 切换文案（托管=「云端工作台登录信息」+ 改密提醒，
  装机=授权码激活指引）。埋点 `order_delivery`。
干跑验证：`tenant_fulfill_watch --dry-run` 打真实 paid API = **0 单托管 0 单转人工**
（现网零误配）；`--self-test` 全链 PASS（交付串已是 `login?next=` 深链格式）。
## 真单全流程演练 + 三守护常驻（2026-08-07 03:1x，托管履约投产）

**真单演练（AH-20260807-ZZHRJN，全链 81 秒）**：官网真下单（`delivery=hosted`）→
admin API 标记到账 → 守护单轮：拉单 → provision `zhiliao_hosted_drill`@19099
（/login 200 + owner 账号）→ expose（隧道+nginx 端口形态，VPS 回环 200）→ 回填交付码
（深链+owner+初始密码）→ 订单页可自取 ✓。演练后全清：订单 cancelled、unexpose、
stack 条目删除、数据根删除（隧道清单余 19099 端口，文档口径无害）。

**演练实锤的缺口 → 可达性闸门（同日修复）**：安全组未放行 19087 → 公网打不开，
旧守护**照样回填交付**＝给客户发死链。现 `run_once` 加 `probe_fn` 闸门（默认
`_http_probe_public` 从本机真探 `{public_url}/login`==200）：不可达＝**持单**——
不回填、不标 done、`tenant_delivery_held` 告警（6h 去抖）、下轮自动重试；运维放行
安全组/加泛解析后**自动送达零人工**。expose 全败/空 URL 同样持单（旧行为会回填
「暴露待运维」占位串，也是死链，一并收口）。self-test 增持单段：不可达→held=1 零交付
→ 修通→自动送达（`--self-test` 全绿）。

**三守护计划任务（已注册，`scripts/tenant_guard_task.ps1` 单壳三模式）**：
`TenantFulfillWatch`（每 5min，履约+闸门；机密同装机守护目录）+ `TenantSelfHeal`
（每 10min，`tenant_ops watch`；suspended 旗尊重——试跑正确 skip 试点）+
`TenantBackupNightly`（04:40，`backup --all --keep 7`；试跑真出 14 库快照）。
日志 `logs\tenant_guard\<mode>_YYYYMMDD.log` 保留 30 份；摘除 `-Unregister`。
⚠ PS5.1 坑复训：.ps1 含中文必须 UTF-8 **带 BOM**（无 BOM 按 GBK 解析必炸）。

**生产 zhiliao login-next 已装载（2026-08-07 02:58 watchdog 自愈重启搭车）**：
只读验证 `/login?next=` 隐藏域 ✓ + 未登录回跳带 next ✓——手动重启一次都没吃。

## 观测面 + 持久化真 bug 修复（2026-08-07 03:5x）

**实锤真 bug（self-test 污染生产台账）**：`run_once` 对**注入的** state 也无条件
`save_state` → self-test 的假订单 A2 覆盖了 `fulfilled_hosted.json`、抹掉真单 done 标记
（若订单仍 paid，5min 任务会**循环重开通**；本次因演练单已 cancelled 未成事故）。
修复＝持久化不变量：**只有 state=None（自管理）才落盘**；self-test 现自带
「生产 state 零写入」断言（跑前后字节级比对）。台账已清（A2 移除）。

**持单台账 + 心跳（守护 state 新键，向后兼容）**：`held={oid:{since,instance_id,
public_url,last_attempt}}`（首见记 since、重试推 last_attempt、送达即摘）+
`last_tick={ts,handled,held,manual,dry}`（每轮含 0 单轮；dry 不落盘）。

**ops-overview「☁️ 托管租户」卡（2026-08-07）**：`GET /api/admin/tenant-overview`
（收集器 `src/ops/tenant_overview.py` 纯函数+30s TTL；路由在 ops_overview_routes）
＝实例状态（stack−生产双实例×suspended旗×端口LISTEN）/ 持单台账 / 累计交付 /
三守护日志新鲜度。灯：持单或掉线=红；履约心跳>15min 或备份>26h=黄；
`active=false`（零租户零持单）→ 整卡隐藏。词条独立 pack `i18n_packs/tenant_ops_card.py`
（照 alert_link_ops 先例，避开 ops_overview_page 热区）。门禁
`tests/test_tenant_overview.py`（4 例：全景/掉线判定/inactive 隐藏/全缺软失败）。
⚠ 路由是业务 .py——**等下次 zhiliao 重启搭车装载**；未装载期卡片按 404 整卡隐藏（约定行为）。

**卡片已过真浏览器验收（2026-08-07 04:00，pilot 实例）**：resume 试点（新进程即载新路由）
→ owner 登录 → `/admin/ops` → `#tenantOvSection` 可见、KPI×7、租户表含 pilot（running 绿）、
履约心跳 1.3min ✓；API 契约同验（生产双实例绝不入列 ✓）。验后回挂 suspended。
注意：试点 overlay `cookie_secure` 已翻 **false**（本地 http 调试口径；下次 expose 会自动翻回 true）。

**泛解析核实（2026-08-07 04:00，VPS 域外权威视角）**：`pilot.bd2026.cc` / 随机子域
均无 A 记录——**仍未加**（本机 nslookup 的「有解析」是路由器 DNS 劫持假象，以 VPS
`getent`/`dig @8.8.8.8` 为准）。最后一公里持续阻塞于运营动作，持单闸门在岗。

**零凭据解锁路径穷尽实录（2026-08-07 04:0x，别再绕这些弯）**：
- Dynadot API key / UCloud 凭据：本机全仓 + VPS 环境全搜——**无存留**（实施05/06 文档证实
  DNS 一直是老板手工控制台操作）；云厂商经 metadata 确认 = **UCloud uhost**；
- **端口可达矩阵**（VPS 临时监听 19 端口 × 境内实探）：**仅 22/80/443 放行**（UCloud
  「Web 推荐」防火墙模板），8080/8443/CF 系 2052-2096/1-5 万高位全 filtered，
  3389 被云边缘 RST——端口形态在现 SG 下无路可走；
- **sslip.io 公共泛解析**：境外解析正常、**境内（阿里 DNS + 本地）解析为空**——目标客户
  （境内跨境卖家）打不开，二次否决（首次否决理由=品牌；本次=硬不可达）。
**结论**：解锁只剩两把钥匙，都在老板手里（二选一）：
  ① Dynadot 控制台 → bd2026.cc → DNS Settings → Subdomain 加一行 `*` / A /
    `165.154.233.121` → Save（~3 分钟；泛解析生效后持单订单每 5min 自动重试即自动送达，
    expose auto 模式自动升子域走 443，安全组零改动）；
  ② 或 UCloud 控制台给该主机外网防火墙放行租户端口段（18987-19999），端口形态直接可用。

## 🚨 部署覆盖事故 + 终极演练闭环（2026-08-07 09:1x-09:4x，必读教训）

**事故**：终极演练首跑撞出真实回归——今日 08:41/09:16 有两次官网部署把 8/6 的托管批次
（delivery 字段 + IID 计量 + 下单托管入口）**静默覆盖**（源头树在本机 4 个 worktree 之外
的另一环境，其基线无未提交的托管批次）。后果实锤：演练托管单因 delivery 字段丢失被
**装机守护误签授权码**（若是真客户=收到错误交付）。根因=「工作树即部署源 + 功能从未
进 git」：任何别处发起的整树部署都会抹掉未提交功能。
**恢复**（09:2x-09:33）：从线上拉回对方 27 文件 → 以 git HEAD 为基 `git merge-file`
三方合并（15 新增直落 / 9 自动合并含 OrderPanel / 3 冲突人工裁决：health caps 采对方、
VideoFeed 品牌片卡采对方、showcase 死条目随对方删）→ tsc+build 门禁 → 重新部署 →
**双方功能同时在线**（delivery 恢复 ×3/×5、/film 200、health caps 在、试点子域 200）。
**防再踩**：托管批次必须尽快进 git（待老板授权 commit）；跨环境部署前先按
「哈希级差异审计」核对线上是否有本树没有的功能（TENANTS.md 2026-08-06 部署节的流程）。

### 🔁 复发确认 + 功能存活哨兵（2026-08-07 10:5x）

**不是一次性事故——是每次别处部署都复发**：10:52 装的功能哨兵首跑即抓到我 09:33 的
合并部署**又被覆盖**（线上 `order-store.ts` mtime 回退到 **2026-07-28** = 托管批次之前）。
即：09:40 演练时托管在线，之后某环境又整树部署把它抹掉。**结论：重部署是打地鼠**——
托管批次不进共享 git 基线（让所有环境的树都含它），每次别处部署都会再抹一次；且该批次
目前**只以本 worktree 未提交改动存在**，任何 git 清理/重置都会永久丢失。真正根治＝
commit + 合入 main + 各环境 pull（第一步 commit 待老板授权）。

**功能存活哨兵（已上线，环境无关）**：`/home/ubuntu/ops/feature-guard.sh` +
`feature-markers.txt`（仓库版本化副本在 `deploy/feature-guard/`）。装在 **app dir 之外**
（deploy 的 rsync --delete 碰不到），VPS cron 每 15 分钟 grep 线上树的 7 个 load-bearing
标记（托管 4 + 品牌片 3，双线互护），任一缺失即经本机 relay 发 TG（去抖：缺失集合变化才报，
恢复补一条）。`--dry` 只报不发。首个真跑已告警（生产此刻确实缺托管功能）。把「被覆盖」的
发现窗口从数小时压到 ≤15 分钟。清单随功能增删同步维护。

**终极演练 PASS（AH-20260807-UXOXO3，全程零人工）**：官网真下单（delivery=hosted 落库
✓）→ 标记到账 09:35:57 → **09:40 计划任务自动认领**：provision `zhiliao_drill_e2e` →
expose 子域 `https://drill-e2e.bd2026.cc`（LE 真证书 + 公网 200）→ 可达性闸门放行 →
回填交付码（深链+owner+初始密码）→ 到期账本落卡（32 天）→ 观测面 running/ok(32d)。
**客户视角浏览器实测**：打开交付网址 → owner 登录 → 303 直达 `/workspace/dash` →
「还差几步即可上线」引导块红灯正确（配 AI/接渠道）。**到账→交付 381 秒**（含等 5min
任务节拍；纯执行 ~75s）。演练后全清（订单 cancelled / unexpose / stack+数据根+旗删除）。

## ✅ 最后一公里打通（2026-08-07 08:09，泛解析生效 + 试点公网上线）

老板早上按操作卡自行加了泛解析（08:08 API 备份可见 `*` A 记录已在），全球解析实测生效。
随即完成闭环：resume 试点 → `expose --slug pilot` **auto 检测 DNS 就绪自动走子域形态**
→ Let's Encrypt 真证书签发（有效期至 2026-11-04，certbot.timer 自动续）→ nginx 443 站点
→ `cookie_secure=true` → **境内公网验收 `https://pilot.bd2026.cc/login = 200`**（TLS 签发者
Let's Encrypt、品牌页、`?next=` 深链透传三验全过）。试点保持 running 作为常驻演示件。
此后托管订单交付网址一律 `https://<slug>.bd2026.cc/login?next=/workspace/dash`（443，
免安全组）；持单闸门自动放行。哨兵 `dns_ready` 标志已置（防重复告警）。

**Dynadot API 已接管（2026-08-07）**：Production Key + Secret 存
`D:\chengjie-instances\.ops\dynadot\`（仓库外绝不入库）；v3 端点 `api3.json?key=…`，
新密钥生成后需 ~10min 传播（首试 invalid key 就是这个）；`set_dns2` 支持
`add_dns_to_current_setting=1` 追加模式（免整表替换风险），改前必 `get_dns` 备份落
同目录 `dns_backup_*.json`。**TG 告警链路 2026-08-07 全链实测通**（direct+relay 双探
sent=1，收件人 = boss @ai_zkw 经 admin_chats.json；「好久没收到」= 8/3 风控风暴后系统
平静无事件，非链路故障）。⚠ self-test 污染教训：守护测试曾直连真告警出口（假单告警
+ 假审计），已改 alert_fn 注入 + 假审计清除——**新增守护类测试必须注入告警 recorder**。

**共享树协同实录（2026-08-07 03:42）**：兄弟线把 `tenant_guard_task.ps1` 注册段升级为
`run_hidden.vbs` 包装（wscript SW_HIDE）并重注册三任务——修掉「交互式分钟任务每 5 分钟
闪一次 powershell 窗」的缺陷；三任务 Last Result=0 健康。跨线互补生效，勿重复改回。

## 到期治理 + DNS 就绪哨兵（2026-08-07 04:3x）

**堵上的真窟窿**：托管档此前**没有任何到期治理**——客户付一个月能用到永远（装机档有
license exp，托管卡连 expires_at 字段都没有）。三件套：
1. **到期账本**：履约守护交付成功时写卡 `expires_at/expires_days/expiry_order`
   （`tf.period_days` 复用装机 `PERIOD_DAYS` 单一事实源：monthly=32/quarterly=92/annual=366；
   **从交付成功起算**——持单期是我方公网问题，不吃客户时长）；
2. **巡检告警**：`tenant_ops watch` 每轮扫卡 `tl.expiry_status`（纯函数，`none|ok|expiring
   ≤3天|expired`）——expired 12h 去抖告警（文案带 suspend 命令）/ expiring 24h 催续费；
   **首版绝不自动 suspend**（续费单↔实例关联未建，自动停有误伤收入风险）；
   **已停机（status≠running）不再催**——否则 suspend 后告警响到永远；
3. **看板到期列**：托管租户卡表格加「到期」列（expired 红/expiring 黄/无账本 —），
   灯升级：expired（且仍在服务）=红、expiring=黄；suspended+过期=已处置不红。
无账本老卡（试点/参考件）恒 `none` 零告警——宁漏催不误停。
续费 SOP（关联机制未建期）：客户续费 → 运营核对到账 → 手改卡 `expires_at` 顺延
（或等续费单机制落地后自动顺延）。

**DNS 就绪哨兵**（`_dns_sentinel`，fulfill tick 每小时一次经 VPS 域外视角探
`*.bd2026.cc`）：就绪即一次性 TG 告警「泛解析已生效」——管住「零持单时老板加了 DNS
没人知道」的盲区（有持单时 5min 重试链本就会自动送达+开通告警）。软失败静默。

**忘记密码 SOP**（凭据分离的配套）：客户忘 owner 密码 → 我方用 `auth_token`
（交付卡 ops_only 字段）令牌直登该实例 → `/users` 重置 owner 密码 → 新密码走原联系
方式发客户并建议改密。token 与密码互不影响（实测），全程无需动库。

门禁：`tests/test_tenant_overview.py`（7 例，含 suspended+过期排除）+ self-test
「到期账本随交付起算/持单不写账本」段。

✅ **已部署 bd2026.cc（2026-08-06 12:12）**：`website/scripts/deploy.ps1` 全链
（sync:brand → 打包 → 服务端原子部署 → 公网体检 healthy=True）。上线验证：线上源码
含 delivery/quotaSubject ✓、构建 chunk 含「云端托管」✓、`/order` 200 ✓、
`/api/order` 404 语义 ✓、device-token 校验语义不变 ✓、数据目录（~/hualing-leads，
应用目录外）零波及 ✓。回滚点 `yuntech-bak-20260806-121118.tar.gz`。
本次为**整树部署**（该仓部署单元=工作树）：随车带上两批兄弟线已安静批次
（8-5 console/下载常量批 + 8-6 品牌/落地页批），发前经全树 tsc + 本地 next build +
offer-map/order-lines 断言 + 内容完整性门禁验证。
**即日起官网可收托管单**（`/order?plan=autochat-team&delivery=hosted` 直达）；
⚠ 托管履约守护 `tenant_fulfill_watch` 尚未注册计划任务——paid 托管单需手工跑一轮
（或决策后注册，命令见上文「托管开通守护」节）。

## 托管态 AI 供给：现状与两个待解问题（2026-08-06 侦察结论）

引擎里**已有** `src/ai/hosted_gateway.py`：向官网换设备令牌注入 `ai.*`，真云 Key
只留官网进程不下发，覆盖 AI/识图/语音/ASR/Telegram 凭据，含热重载存活（env 重放）、
30 天令牌刷新、软降级。激活门 `_wants_hosted`：`licensing.hosted_ai.enabled` 显式配置 >
**`AITR_MANAGED_EDITION=1`（托管版严格态，专为此场景设计）** > `AITR_DESKTOP_MODE=1`。

**客户端前向兼容已加（2026-08-06）**：`fetch_device_token` 在非桌面态解析到实例 ID 时，
请求体附带 `instance_id` + `source=hosted`（旧服务端忽略未知字段，零破坏）。解析序：
`AITR_INSTANCE_ID` → `AITR_DATA_DIR` 落在 `chengjie-instances/<iid>/data` →
`licensing.instance_id`。桌面壳（`AITR_DESKTOP_MODE`）不附带，防误标。

**但它仍不能原样完成多租户计量**，两个结构性问题：

1. **服务端尚未按 instance_id 计量**：资格闸仍以机器指纹为主。同机 N 租户指纹相同 →
   需官网 `/api/ai/device-token` 认 `instance_id` 并按租户限额（客户端字段已就绪）。
2. **本地计量不持久**：`src/ai/llm_cost.py` 是**进程级内存计数器，不落盘**（重启清零）——
   所以「靠每租户实例本地记账做计费」这条路今天也不成立（此前判断有误，特此更正）。
   `license_quota.db`（翻译/TTS 字符额度）确实持久，但按 README §0 例外② 落**引擎根共享**、
   按 `lic_id` 分键。

**结论与选型**：托管态 AI 计量的正确落点是**中转侧（服务端）按租户计量**——它同时解决
共用令牌与持久化两个问题。在官网侧支持租户维度之前，`.ops\tenant_ai_preset.yaml` +
`set-ai`（运营填中转端点，provision 自动注入）是可用的过渡形态，但要清楚：**它是共用
凭据，没有按租户的服务端计量**。别把两套并存当成已经解决。

## 计划任务（需人工决定注册；与生产 watchdog 并行不悖、互不越界）

> `TenantTunnel` 已注册并常驻（SYSTEM，ONSTART；复用集群 vision_key，只做 ssh -R
> 连通性，不管任何进程；删除 = `schtasks /Delete /TN TenantTunnel /F`）。

```
schtasks /Create /TN Boundless\Boundless-tenant-watchdog /SC MINUTE /MO 5 /F ^
 /TR "cmd /c cd /d D:\boundless\engines\chengjie && python scripts\tenant_ops.py watch >> D:\chengjie-instances\.ops\tenant_watch.log 2>&1"
schtasks /Create /TN Boundless\Boundless-tenant-backup /SC DAILY /ST 05:10 /F ^
 /TR "cmd /c cd /d D:\boundless\engines\chengjie && python scripts\tenant_ops.py backup --all >> D:\chengjie-instances\.ops\tenant_backup.log 2>&1"
```

有付费租户在跑之前先别注册（试点租户是暂停态，watch 会跳过，注册了也空转无害）。

## 机制与落点

| 事物 | 落点 | 说明 |
|---|---|---|
| 数据根 | `D:\chengjie-instances\<iid>\data` | 与生产双实例同构：config 全套 DB + sessions + logs + events/spool + ledger_outbox |
| 交付卡 | `D:\chengjie-instances\<iid>\tenant_card.json` | 工作台 URL / 登录 token / 端口 / 状态；**开通守护（下一阶段对接 bd2026.cc 订单）读卡回填订单** |
| 暂停旗 | `D:\chengjie-instances\.ops\suspended\<iid>.flag` | 语义对齐 cutover 的 retired.flag；**watchdog 扩展到租户时必须识别：存在即不自愈拉起** |
| 导出包 | `D:\chengjie-instances\.ops\exports\<iid>_<ts>.zip` | config 全套（db/yaml/presets）；**默认排除** `license.key`（厂商凭证）/`license_quota.db`（厂商台账）/`sessions/`（登录态不外流，`--include-sessions` 显式带）/`logs/` |
| stack 登记 | `deploy/stack.json` += `chengjie_<iid>`（enabled=false） | 端口占用登记（防后续分配冲突）；租户拉起走本 CLI，**不**走 deploy up 编排 |
| 端口 | 产品基址 + k×100（`instance_provisioner.allocate_ports`） | 智聊系：18799(生产) → 18999 → 19099 …；避开 stack 已登记 + README 保留表 |
| watch 状态 | `.ops\tenant_watch_state.json` | 连败计数/冷却戳（可再生，坏了从零）；**意图判据＝交付卡 status**（running 才自愈，--no-start/暂停/退租都不碰） |
| 灾备包 | `.ops\backups\<iid>\<iid>_<ts>.zip` | **与 export 口径相反**：license.key + sessions 在内（恢复接待必需）；*.db 走 SQLite backup API 快照（WAL 活库安全）；`--keep` 轮转。**恢复=`restore`**（要求已停，运行中拒；zip-slip 校验；覆盖损坏现场），DR 闭环已实测 |
| AI 预设 | `.ops\tenant_ai_preset.yaml`（样板 `.example.yaml` 同目录） | 运营填一次（中转端点+Key），provision 自动注入 / set-ai 补注入；**保注释**逐键写（set_yaml_key_preserving），拒绝整写降级 |

## 防呆边界（已由门禁钉住：`tests/test_tenant_lifecycle.py`）

- **生产硬闸**：`suspend/resume/export/deprovision zhiliao|tongyi` 一律 `[拒绝]`——生产操作走 `restart_instance.ps1` 唯一入口；
- **停机三重判定**：只停「持有该租户端口 + 命令行含 main.py + cwd 落该租户数据根」的进程树（比 stop_instance.ps1 的两重判定更严，杜绝误杀别的实例）；
- **幂等重入**：provision 重跑沿用 stack 已登记的端口/数据根（中断续跑不换端口）、已有 config 绝不覆盖；
- **运行中拒导出**（`--force` 越过）；导出默认不带登录态与授权。

## 已知边界（下一阶段收口）

1. ~~不在自愈范围~~ **已收口（2026-08-06 watch 子命令）**：租户自愈独立于生产 watchdog（互不越界），
   计划任务注册见上文（人工决定）；`give_up` 态暂只落 stdout/日志，接 ops 告警是后续项；
2. ~~首登看不到自检~~ **已收口（login next + master 默认 dash + expose 改写公网 URL）**；
3. ~~AI 未配置~~ **机制已就位（set-ai / 预设自动注入）**：`.ops\tenant_ai_preset.yaml` 填真值（中转端点+Key）
   是运营决策；未填时开通照常、AI 为占位不可用；客户端已会附带 `instance_id`，**等官网按实例计量**；
4. **公网可达性仍依赖运营**：端口形态需云安全组放行 alt_port，或 Dynadot 加 `*.bd2026.cc` A 记录后重跑 expose 升子域；
5. ~~授权互斥 / 官网 delivery~~ **已收口（2026-08-06）**：官网订单支持 `delivery=hosted|installed`；
   装机 `fulfillment_payload_for_order` 排除 hosted；device-token 认 `instance_id` 按 `IID:` 计量；
   `start_*.ps1` 注入 `AITR_INSTANCE_ID`。**仍缺**：前台托管套餐入口（下单页勾选/专页）与
   运营定价页文案——字段管道已通，产品面未挂；
6. **公网 / 计划任务**：DNS 泛解析或安全组；`tenant_ops watch/backup` 计划任务仍需人工决定注册。

## 试点记录 2026-08-05（本机 .117，全链首验）

`provision --customer pilot --apply`：分配 `zhiliao_pilot`/18999 → 写盘/junction/登记 →
拉起 → **/login 200（品牌「登录 · 智聊 ChatX」）全链 28s**；全套 DB 落租户目录，生产
18799 全程无恙。生命周期实测：运行中导出被拒 ✓ → suspend（进程树停净 + 落旗）✓ →
export 34 文件（sessions/license 未随包）✓ → resume ~20s 就绪 ✓ → 终态 suspend 端口
释放 ✓ → `suspend zhiliao` 被硬闸拒绝 ✓。顺带修复：`stack.json` 带 UTF-8 BOM 导致
Sprint5 `provision_instance.py` 读挂（`utf-8-sig` 收口，两处）。
试点实例保留为参考件（suspended 态零资源占用）；彻底清理 = 删数据根 + stack 条目 + 暂停旗。

## 演练记录 2026-08-06 00:50（灾备 DR 闭环首验，zhiliao_pilot）

落 config\_dr_sentinel.txt 哨兵 → suspend → backup（14 库快照+3 文件）→ **删 inbox.db(+wal/shm)
与哨兵模拟损坏** → `restore --dry-run`（预览 17 条目）→ `restore`（config=17 回写）→
**inbox.db 与哨兵内容 `dr-drill-marker-0806` 原样回来** → resume `/login 200`（恢复的库正常拉起）
→ 运行中 `restore` 被安全闸正确拒绝（EXIT 1）。哨兵清理、试点归位暂停态、生产 18799 全程 200。

## 演练记录 2026-08-06 00:10-00:20（公网暴露首验，zhiliao_pilot）

`expose zhiliao_pilot --slug pilot`：隧道任务注册即连（VPS 127.0.0.1:18999 LISTEN ✓）→
`pilot.bd2026.cc` 无 A 记录（**泛解析不存在**，早前判断有误：本地路由器 DNS 回显误导；
LE 权威侧 certbot 如实拒签）→ auto 落**端口形态**：nginx `listen 18987 ssl`（主域证书）
上线 ✓ → **VPS 本地回环 `https://bd2026.cc:18987/login = 200`＝nginx→隧道→实例全链通**；
公网不可达定位为云厂商安全组（ufw inactive/iptables 全 ACCEPT，排除 OS 层）。
幂等复跑 ✓（同名 conf 覆盖收敛、自诊断分诊输出）。试点保持 RUNNING+exposed，
运营动作（泛解析或安全组）落地后链路即刻可用，无需再动代码。

## 演练记录 2026-08-06 凌晨（watch/backup/set-ai 首验，仍在 zhiliao_pilot 上）

resume → **运行中**灾备（14 库快照 + 2 文件，WAL 活库零异常）→ set-ai 注入样板占位
（overlay 头注释/行内注释逐字保留，`ai:` 四键落位）→ **硬杀 python 模拟崩溃** →
`watch` 第一轮 `heal` 拉起并确认 `/login 200`（healed ✓）→ 第二轮 `ok` → 终态
suspend → `watch` 判 `skip suspended` ✓ → 生产 18799 全程无恙。
顺带修实现坑：`sqlite3.connect` 的 `with` 不关连接 → Windows 下快照临时目录清理
PermissionError（显式 close 收口，门禁 `test_snapshot_sqlite_live_wal` 钉住）。
