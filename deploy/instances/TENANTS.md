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
   **已停机（status≠running）不再催**——否则 suspend 后告警响到永远；
   **到期自动停机（2026-08-08 P3；P4 起经守护壳默认开，宽限 3 天）**：
   `tenant_guard_task.ps1 -Mode watch` 现默认带 `--auto-suspend-grace-days 3`
   （`-AutoSuspendGraceDays 0` 可退回只告警）——安全轨全在纯函数
   `tl.should_auto_suspend`：只停**订单驱动卡**（有 `expiry_order`，手工卡没有
   「续费→复机」联动仍只告警）/ 受保护租户绝不停 / 逾期须超宽限 N 天 / 已停不重复；
   自动停走与人工 suspend 同一套 `_suspend_core`（停进程+落旗+卡状态）+
   `tenant_auto_suspended` 告警。**配套复机**：停机租户收到续费单 →
   `tenant_fulfill_watch` 先 `tenant_ops resume`（清旗+拉起+卡状态——只靠
   provision 顺带拉起会留「进程在跑、暂停旗还在」分裂态）再走可达闸门交付；
   复机失败发 `tenant_resume_fail` 告警且持单重试。「客户付钱→服务回来」全自动；
4. **客户侧到期横幅（2026-08-08 P4）**：watch 巡检把 expiring/expired 镜像成
   `<data>/config/tenant_notice.json`（含按 `last_order_sku` 生成的续费深链，
   纯函数 `tl.build_tenant_notice` + `tf.renew_order_url`；ok/无账本=删文件，
   续费叠期时履约守护即刻删旧提醒防「续完费还显示已到期」）→ 租户实例
   `/api/workspace/ai-runtime-status.tenant_notice` 捎带（零新增轮询/路由，读取端
   `src/utils/tenant_notice.py` 自带 72h 陈旧守卫——watch 停摆就收横幅，不拿旧急迫度
   吓客户）→ 工作台 `#ws-expiry` 横幅（expiring 琥珀/expired 红 + 「立即续费」链）。
   生产 zhiliao/tongyi 与装机版无此文件 → 字段恒 null 永不显示；存量租户实例在
   下次自然重启（resume/heal）后开始带该字段，模板侧已热更新兼容旧进程。
   门禁：`tests/test_tenant_notice.py` + `test_tenant_lifecycle`（notice/renew_url
   纯函数）+ `test_key_pool_routes`（路由字段 + workspace_base 接线契约）；
5. **停机友好页（2026-08-08 P5，续费转化最后一环）**：per-tenant nginx 站点
   `error_page 502/503/504 → /__tenant_down.html`（`_nginx_proxy_body` 共用体，
   两种暴露形态同享；expose 每次幂等把仓库 SSOT `deploy/instances/tenant_down.html`
   推到 VPS `/var/www/html/`）。被停/断链的租户入口从裸 502 变成「①订阅到期续费即
   自动恢复 ②维护中 60s 自动重试」双语引导页 + 续费 CTA；**HTTP 状态刻意保持 502**
   （监控/edge 探针仍按故障计，只有人眼变友好）。已实弹演练：drill 开通→expose→
   suspend→公网 502 出友好页→拆除；pilot 配置已同批刷新。
6. **付费托管额度条措辞（2026-08-08 P5）**：付费托管与免费试用同走 hosted_gateway
   注入链（`_hosted_trial` 同为 True）→ 此前付费租户 owner 看到的是「免费试用中」。
   pages 路由新增 `ai_hosted_paid`（`licensing.hosted_ai.enabled` 且托管注入生效）：
   横幅/用尽文案切「按套餐供给/联系客服提额」口径（`ws.aihosted.*`），额度用尽不弹
   试用升级窗（bind_code 话术是试用语境）；另加**低额度预警档**（剩 ≤15% 琥珀提示
   `ws.aiquota.low`，试用/付费同享）。`.py` 改动随实例下次重启生效，旧进程回落试用
   措辞（模板已兼容）。

## ✅ 全生命周期真单演练（2026-08-08 P6）+ 抓出并修复续费误判首开

**演练路径（全走生产管线**：官网 `/api/order` 下单 → admin 标 paid → 计划任务
`TenantFulfillWatch`/`TenantSelfHeal` 零人工处置）：下单→**40s 交付**（250k 提额+
32 天账本+SKU 落卡）→ 回拨到期 → watch **自动停机**（旗 reason=`auto_expired:<订单号>`）
→ 公网 502 出友好页（含同档续费深链）→ 续费单 → **自动复机+叠期+续费串回填** →
公网 200。三笔演练订单已标 cancelled（防污染营收台账）、额度覆写已清、实例全拆。

**演练已工具化（P6 收尾）**：`python tools/drill_tenant_lifecycle.py --confirm`
一键复验六段闭环（下单→交付→自动停→友好页→续费复机→全清；无 --confirm 只打印
计划零副作用；实测 ~2min PASS）。**刻意不进计划任务/gate_sweep**——每跑真开实例/
真签 LE 证书/真发告警，改完履约/生命周期代码后按需跑。

**演练抓出的真 bug（已修+回归钉住）**：续费单曾被误判**首开**——把**初始密码再次
回填**给客户、且暂停旗残留（分裂态）。根因两层：① `cmd_provision` 幂等重入时
**整卡重写**（`build_tenant_card` 直写）抹掉 `expires_at/expiry_order/public_url` 等
账本字段；② `run_once` 拿 provision **之后**的卡判续费——账本已被抹，判定必翻车。
修复（防御纵深）：① provision 写卡改经 `tl.merge_preserved_card_fields`（纯函数：
`CARD_PRESERVE_KEYS` 账本/暴露字段旧值恒胜出；URL 族仅旧卡确已暴露才整组回填，
防「public 公网、login 却 127.0.0.1」分裂卡）；② `run_once` 增 `precard_fn` 注入
（生产读卡文件），**续费判定/停机判定/叠期旧到期/公网回落全部以 pre-provision 卡
为准**——「续费=本次履约之前已是交付过的租户」才是正确语义（post 卡被抹时未过期
续费会从 now 起算＝白吞客户剩余天数）。self-test 的假 provision 现在**刻意返回被
抹的卡**（模拟生产真行为），门禁 `test_merge_preserved_card_fields` +
self-test 全链钉住。修复后同一租户第三单实弹复验：`[续费]` 串（无密码）+ 复机
（旗清+running）+ 叠期 now+32d + 公网 200 全对。
3. **看板到期列**：托管租户卡表格加「到期」列（expired 红/expiring 黄/无账本 —），
   灯升级：expired（且仍在服务）=红、expiring=黄；suspended+过期=已处置不红。
无账本老卡（试点/参考件）恒 `none` 零告警——宁漏催不误停。
**续费自动叠期（2026-08-08）**：同 contact 再下一笔 `delivery=hosted` paid 单 → 守护识别
存量卡（`public_url` + 到期账本）→ `stack_expires_at(max(now,旧到期)+period)` 写卡 +
回填「续费已到账」串（**不重发初始密码**）+ 按套餐再提 `gw-budget`。手工 SOP 仅留作
漏单兜底（守护挂了才手改卡）。

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

## ✅ 托管能力供给闭环（2026-08-08，「空壳租户」根治）

**背景实锤**：pilot 实测「开出来的租户是空壳」——AI 占位（copilot 全回 canned
「您好，请稍等片刻～」+ 日志 Connection error → `YOUR-RELAY-ENDPOINT`）、渠道接不了
（credpool 未配 + config `YOUR_API_ID` 占位 → 扫码必失败）。根治＝**开通即接线**：

1. **AI 走官网托管网关**（`licensing.hosted_ai.enabled: true`）：实例启动时
   `hosted_gateway` 自动向 bd2026.cc 换设备令牌注入 `ai.*`（117 指纹
   `D316-8D51-7107-F1CB` 已在试用台账，签发闸直接过）；**按实例计量
   `IID:<instance_id>`**（网关 gw_quota；提额走 `/api/admin/gw-budget` 按主体覆写）；
   识图（hosted-vision → 网关 → 176/140 GPU）与语音中继一并接管。真云 Key 只在
   VPS 官网进程，租户配置/导出零密钥。此前文档「服务端尚未按 instance_id 计量」
   已过期——device-token 认 `instance_id` 于 8/6 收口，本轮实测生效。
2. **渠道走本机中央凭据池**（`platform_login.telegram.credpool` → 127.0.0.1:8000）：
   托管租户都在本机，凭据不出内网；**每租户专属卡密**自动签发
   （`CHATX-<IID>-XXXX`，池侧首用绑机、独立计量）。官网侧 `POOL_TG_CREDS`
   公网派发通道保持暗态（那是给装机版外网机器的，托管态不需要）。
3. **预设升级为多段白名单**（`tenant_ops` + `tenant_lifecycle`）：
   `.ops\tenant_ai_preset.yaml` 现支持 `ai` / `licensing` / `platform_login` 三段
   （白名单外一律忽略，防机密段批量下发）；`credpool.license_key: AUTO` = 开通时
   自动签发（幂等复用；池库缺失软失败回 free 档，绝不拦开通）。真值文件已就位。
   门禁 `tests/test_tenant_lifecycle.py`（多段白名单/深写保注释/卡密签发幂等/
   AUTO 解析 4 例新增，55 例全绿）。
4. **验收实录（2026-08-08 04:5x）**：`provision --customer "drill preset" --apply`
   → 卡密 `CHATX-ZHILIAO-DRILL-PRESET-HBD6` 自动签发 + 8 键注入 + 24s 就绪 +
   **AI 真出话**（「我是小优…」）+ **QR ready=true 零阻塞**（modes 探针）→ 演练全清。
   pilot 同日已补齐同款配置（专属卡密 `CHATX-PILOT-0001`）：AI 1.9s 真回复、
   QR start 真拿到 `tg://login` 码、上线自检 AI 项 fail→ok。
5. **履约自动提额 + 续费叠期（2026-08-08 P2）**：`tenant_fulfill_watch` 在回填成功后
   调 `POST /api/admin/gw-budget`（主体 `IID:<instance_id>`；软失败不挡交付）。日额度纯函数
   `tf.gw_daily_chars_for_order`：席位×25k/日再按档夹 floor/cap（entry 100k / team 250k /
   flagship 1.25M；lingox 走月包÷30；未知付费托管 200k）。卡上镜像 `ai_daily_chars` /
   `ai_budget_subject` / `last_fulfill_kind`。续费见上节「续费自动叠期」。
   存量已开通租户若仍吃 50k 默认，运维一次性：
   `POST /api/admin/gw-budget {subject:"IID:…", budget:<套餐日额>}`。
   租户「计费口径」仍以网关 gw_quota 为权威。

## 托管态 AI 供给：现状与两个待解问题（2026-08-06 侦察结论；⚠ 已被上节 2026-08-08 闭环取代，仅留档）

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
| 保护旗 | `D:\chengjie-instances\.ops\protected\<iid>.flag` | **真人在用＝生产资产**（`tenant_ops protect <iid>`）：suspend/deprovision/restore/unexpose 见旗即拒，`--force-protected` 破玻璃（拦截与破玻璃均推 TG 留痕）；resume/backup/watch 自愈不受限 |
| 导出包 | `D:\chengjie-instances\.ops\exports\<iid>_<ts>.zip` | config 全套（db/yaml/presets）；**默认排除** `license.key`（厂商凭证）/`license_quota.db`（厂商台账）/`sessions/`（登录态不外流，`--include-sessions` 显式带）/`logs/` |
| stack 登记 | `deploy/stack.json` += `chengjie_<iid>`（enabled=false） | 端口占用登记（防后续分配冲突）；租户拉起走本 CLI，**不**走 deploy up 编排 |
| 端口 | 产品基址 + k×100（`instance_provisioner.allocate_ports`） | 智聊系：18799(生产) → 18999 → 19099 …；避开 stack 已登记 + README 保留表 |
| watch 状态 | `.ops\tenant_watch_state.json` | 连败计数/冷却戳（可再生，坏了从零）；**意图判据＝交付卡 status**（running 才自愈，--no-start/暂停/退租都不碰） |
| 灾备包 | `.ops\backups\<iid>\<iid>_<ts>.zip` | **与 export 口径相反**：license.key + sessions 在内（恢复接待必需）；*.db 走 SQLite backup API 快照（WAL 活库安全）；`--keep` 轮转。**恢复=`restore`**（要求已停，运行中拒；zip-slip 校验；覆盖损坏现场），DR 闭环已实测 |
| AI 预设 | `.ops\tenant_ai_preset.yaml`（样板 `.example.yaml` 同目录） | 运营填一次（中转端点+Key），provision 自动注入 / set-ai 补注入；**保注释**逐键写（set_yaml_key_preserving），拒绝整写降级 |

## 防呆边界（已由门禁钉住：`tests/test_tenant_lifecycle.py`）

- **生产硬闸**：`suspend/resume/export/deprovision zhiliao|tongyi` 一律 `[拒绝]`——生产操作走 `restart_instance.ps1` 唯一入口；
- **受保护租户闸**（2026-08-07 事故沉淀，见下方事故记录）：`protect <iid>` 落保护旗后，
  suspend/deprovision/restore/unexpose 须 `--force-protected`；已暴露实例被 suspend 时
  无论是否受保护都推 TG 告警（`tenant_suspended`）——入口对外变 502 的操作绝不静默；
- **停机三重判定**：只停「持有该租户端口 + 命令行含 main.py + cwd 落该租户数据根」的进程树（比 stop_instance.ps1 的两重判定更严，杜绝误杀别的实例）；
- **幂等重入**：provision 重跑沿用 stack 已登记的端口/数据根（中断续跑不换端口）、已有 config 绝不覆盖；
- **运行中拒导出**（`--force` 越过）；导出默认不带登录态与授权。

## 事故与机制 2026-08-07（pilot 上线首日三连断 → 保护旗 + 边缘探针）

**事故**：坐席上线首日全程工作在 `https://pilot.bd2026.cc`（当日 VPS 日志 3800+ 请求）；
17:54 隧道任务被重启（性能优化）、18:47 pilot 被当「演练件」`tenant_ops suspend`（落旗
→ 自愈见旗跳过，**永不自动恢复**）、19:05 前后再次被停——三段公网 502 窗口合计 2h+，
老板比机器先发现（「为什么又连接中断」）。根因不是手滑，是**「演练/退租语义的生命周期
命令」与「真人在用的生产租户」零机制隔离** + **监控只探本地 /login、不探坐席真实走的
公网整链**。

**机制**（同日落地，门禁 `tests/test_tenant_lifecycle.py` 边缘/保护两族）：
1. **保护旗**：`tenant_ops protect zhiliao_pilot --reason "坐席生产入口"`（已执行）；
   危险命令见旗即拒，破玻璃 `--force-protected`，拦截/破玻璃/暴露实例被停三类事件全部
   `emit_tenant_alert` 推 TG 留痕；`status`/`list` 显示 🔒。
2. **边缘探针**（并入 `watch` 子命令，TenantSelfHeal 每 10min 顺带跑，零新任务）：
   对「应在跑+未暂停+已暴露+本地健康」的租户从本机直打 `public_url/login`，覆盖
   DNS→VPS nginx→反向隧道→本机端口 的坐席同款整链；两振确认 → `kick_tunnel` 自愈
   （杀隧道 ssh，runner 10s 重连；机器级冷却 20min 防反复踢）→ 踢过仍不通 →
   `tenant_edge_down` TG 告警（防抖 30min）。决策纯函数 `plan_edge_actions`，
   状态挂 `tenant_watch_state.json` 的 `_edge` 命名空间。`--no-edge` 可关。
3. **演练纪律**：生命周期/DR 演练一律用专用演练实例（`drill-e2e` 系），受保护租户
   的演练需求先 `protect --off`（本身即是显式决策点）再操作，完毕恢复保护。
4. **交付即保护**（同日追加）：`expose` 成功默认自动打保护旗（真人要用的入口不能
   静默打断），不靠人记得跑 protect；**演练件豁免**＝id/客户名/slug 含 `drill`
   （`is_drill_instance`，判定刻意宽松：误判成演练件只损失自动保护、可手动补，
   反向误判会卡死演练自动化）。

## 生产工作台公网入口 2026-08-07（katie.bd2026.cc，与租户体系平行）

坐席要接的真实客户会话在**生产实例 zhiliao（18799）**里；泛解析 `*.bd2026.cc` 当日
生效后按租户同款链路给生产开了独立入口（**生产不入租户管辖**，guard_not_core 依旧）：

```
坐席浏览器 → https://katie.bd2026.cc（VPS nginx prod-katie.conf, LE 证书）
  → VPS 127.0.0.1:18799 → ssh 反向隧道（ProdTunnel 任务，专用不与租户共联）→ 本机 18799
```

三件套（`deploy/instances/`，均 ASCII-only 防 PS5.1 GBK 坑）：
- `prod_tunnel.ps1` + 任务 **ProdTunnel**（ONSTART/SYSTEM）：专用 -R 18799 隧道——
  租户 expose 踢 tenant_tunnel 重列端口时生产入口不陪跳（隧道隔离 doctrine 同
  vision/tenant 之分）；pid 落 `.ops\prod_tunnel.pid`。
- `expose_prod.ps1`：幂等建链/修链（DNS→隧道腿→certbot→nginx 站点→公网验收→
  写 `.ops\prod_public_url.txt` 标记）；VPS 被 deploy 误清后重跑即复原。
- `prod_edge_watchdog.ps1` + 任务 **ProdEdgeWatchdog**（每 5min/SYSTEM）：双腿探针
  （VPS 回环 18799 = 隧道腿；本机直打公网 URL = 坐席同款整链），两振 → 重启
  ProdTunnel 自愈（15min 冷却）→ 救不回 → notify_webhooks.json 直发 TG（恢复补报）。
  状态 `.ops\prod_edge_watchdog.state.json`。

注意：生产入口的**实例侧**监控/重启仍归 watchdog_instances / restart_instance.ps1
（本入口只管「链路」）；租户边缘探针在 `tenant_ops watch` 内，两套互不越界。

## 已知边界（下一阶段收口）

1. ~~不在自愈范围~~ **已收口（2026-08-06 watch 子命令）**：租户自愈独立于生产 watchdog（互不越界），
   计划任务注册见上文（人工决定）；`give_up` 态暂只落 stdout/日志，接 ops 告警是后续项；
2. ~~首登看不到自检~~ **已收口（login next + master 默认 dash + expose 改写公网 URL）**；
3. ~~AI 未配置~~ **已闭环（2026-08-08，见「托管能力供给闭环」节）**：预设真值已就位
   （hosted_ai + credpool 多段），新开通租户 AI/渠道开箱即用；按实例计量已实测生效；
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
