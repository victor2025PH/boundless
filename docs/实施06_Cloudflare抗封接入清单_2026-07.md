# 实施 06 · Cloudflare 抗封接入清单（私域出海）

**日期**：2026-07-12
**目标**：解决"三个域名同一源站 IP（`165.154.233.121`），服务器 IP 一被封三个域名一起挂"的问题。
**做法**：三个域名全部套 Cloudflare，**隐藏源站真实 IP**，只对外暴露 CF 的 IP；封禁方拿不到源站就封不掉服务器。这是私域站性价比最高的抗封手段。
**性质**：运维操作清单（在 Cloudflare 控制台 + Dynadot + 源站防火墙执行；代码侧已就绪）。

---

## 零、现状（已确认）

| 项 | 值 |
|---|---|
| 源站 VPS | `165.154.233.121`（user `ubuntu`，见 `website/scripts/deploy.ps1`） |
| 三个域名 | `ai26.sbs` / `13x.lol` / `aikf.lol`（Dynadot 注册，当前用 Dynadot DNS 解析到源站） |
| 部署方式 | 本机 `cd website; ./scripts/deploy.ps1` → SSH(22) 打包上传 → 服务器 `deploy.sh` 原子部署 Next.js |
| 站点性质 | 已全站 noindex（robots + meta + 响应头三重），私域分发 |
| 风险 | 三域名同 IP：能防域名级封禁，**防不了源站 IP 级封禁**（一封全挂） |

---

## 一、接入目标架构

```
用户 → 域名(ai26.sbs / 13x.lol / aikf.lol)
        → Cloudflare 边缘(橙云 Proxied，对外只暴露 CF IP)
          → 回源 → 源站 165.154.233.121（真实 IP 被隐藏）
运维部署 → SSH:22 直连 165.154.233.121（不经 CF，单独放行运维 IP）
最终兜底 → Telegram 触点（域名全挂也能找回新址）
```

---

## 二、分步清单

### 步骤 1：Cloudflare 添加三个站点

1. 注册/登录 [Cloudflare](https://dash.cloudflare.com)（免费版足够）。
2. **Add a Site**，分别添加 `ai26.sbs`、`13x.lol`、`aikf.lol`（三个各做一次）。
3. 选 **Free** 方案。CF 会扫描现有 DNS，并给出**两个 Cloudflare NS**（形如 `xxx.ns.cloudflare.com`）。

### 步骤 2：Dynadot 把 NS 改到 Cloudflare（三个域名各做）

1. Dynadot 控制台 → 该域名 → **Nameservers**。
2. 从「Dynadot DNS」改为「Custom / 使用 Cloudflare 提供的两个 NS」。
3. 保存。NS 生效通常几分钟到数小时（CF 会邮件通知 Active）。

> 注：改 NS 后，域名 DNS 由 CF 管理，Dynadot 的 `165.154.233.121` 记录以 CF 里的为准。

### 步骤 3：Cloudflare DNS 记录（三个域名各做）

在每个域名的 CF **DNS** 面板加：

| 类型 | 名称 | 内容 | 代理状态 |
|---|---|---|---|
| A | `@` | `165.154.233.121` | **Proxied（橙云）** ← 关键，隐藏源站 |
| A | `www` | `165.154.233.121` | **Proxied（橙云）** |

**橙云（Proxied）= 流量经 CF、隐藏源站 IP**。灰云（DNS only）会直接暴露源站，务必橙云。

### 步骤 4：SSL/TLS

1. **SSL/TLS → Overview → 模式**：源站有有效证书用 **Full (strict)**；不确定先用 **Full**（勿用 Flexible，会导致重定向循环）。
2. **Edge Certificates**：开 **Always Use HTTPS**、**Automatic HTTPS Rewrites**。
3. 若源站还没证书：可在源站装 [Cloudflare Origin Certificate](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)（CF 签发、15 年有效）配合 Full (strict)。

### 步骤 5：锁源站（防直连 IP 绕过 CF）——抗封的关键一步

套 CF 后，若别人仍能直连 `165.154.233.121` 就等于没隐藏。用源站防火墙**只放行 Cloudflare IP 段**访问 80/443，SSH 22 单独放行运维 IP。

Cloudflare 官方 IP 段：<https://www.cloudflare.com/ips/>（IPv4 约 15 段，会更新）。

源站（ubuntu）ufw 示例：
```bash
# ⚠ 先放行 SSH（用你的运维出口 IP，别把自己锁外面！）
sudo ufw allow from <你的运维IP> to any port 22 proto tcp

# 只允许 Cloudflare 段访问 80/443（下面示例段，以官网最新为准，逐条加）
for ip in 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 \
          141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 \
          197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 \
          104.24.0.0/14 172.64.0.0/13 131.0.72.0/22; do
  sudo ufw allow from $ip to any port 80 proto tcp
  sudo ufw allow from $ip to any port 443 proto tcp
done

sudo ufw default deny incoming
sudo ufw enable
sudo ufw status numbered
```

> 部署不受影响：`deploy.ps1` 走 SSH(22) 直连源站 IP，已单独放行；只要保留运维 IP 的 22 端口即可。

### 步骤 6：Telegram Webhook 复核

- 站上 Telegram Bot webhook 走 `https://<域名>/api/telegram/...`，经 CF 到源站。
- Telegram 只支持 webhook 端口 443/80/88/8443——经 CF 用 **443** 正常。
- 套 CF 后重新 set 一次 webhook（用当前主域名），确认 `/api/health?deep=1` 的 webhook 项正常。

### 步骤 7：三个域名重复步骤 1–6

三个域名都套 CF、都锁源站（源站防火墙一次配好即对三域名生效，因为都回同一源站）。

---

## 三、可选增强（私域出海）

- **WAF / Bot 防护**：CF Free 自带基础规则，开 **Bot Fight Mode** 挡爬虫扫描（私域站无需搜索爬虫）。
- **地域策略**：不做大陆——可用 **WAF 自定义规则**对 `CN` 来源挑战/阻断，降低大陆监测与举报面。⚠ 权衡：部分华人用户可能用大陆网络/漫游，误伤则放宽；建议先"挑战(Challenge)"而非直接"阻断(Block)"。
- **"Under Attack" 模式**：被 CC/DDoS 时一键开启 5 秒盾。
- **Page Rules / Cache**：静态资源（`/products/*`、`/showcase/*`，代码里已设长缓存）走 CF 缓存，减源站压力。

---

## 四、验证

```bash
# 1) 域名应解析到 CF IP（不再是 165.154.233.121）
dig +short ai26.sbs        # 期望：CF 段 IP（104.x / 172.64.x 等）
nslookup 13x.lol

# 2) 直连源站 IP 应被防火墙拒绝（锁源站生效）
curl -m 5 http://165.154.233.121   # 期望：超时/拒绝（非 CF 来源）

# 3) 经域名访问正常
curl -I https://ai26.sbs           # 期望：200 + cf-ray 头 + x-robots-tag: noindex

# 4) 健康检查
curl https://ai26.sbs/api/health
```

`cf-ray` 响应头出现 = 流量确实经过 Cloudflare。`x-robots-tag: noindex` = 私域 noindex 生效（本轮代码已加全站响应头）。

---

## 五、与代码侧域名池的衔接（换域名 SOP）

代码侧已就绪（`lib/domains.ts` + `/api/active-domain` + `/go/*` + `FallbackNotice`）。套 CF 后换域名流程：

1. 新域名注册 → CF 添加站点 + 改 NS + 加橙云 A 记录到**同一源站**（源站不变，无需重新部署）。
2. 更新各镜像部署的 `NEXT_PUBLIC_MIRROR_DOMAINS` / `NEXT_PUBLIC_PRIMARY_DOMAIN`（或改 `lib/domains.ts` 后重新部署）。
3. Telegram Bot 播报新域名（用户从 TG 锚点跟随）。
4. 旧域名弃用。

> 套 CF 后，源站 IP 隐藏 → 域名不易被"顺着 IP"连坐封禁；主要剩"域名本身被 GFW/registrar 封"，换域名即可，Telegram 锚点永久兜底。

---

## 六、风险与边界（诚实）

- **便宜 TLD 风险**：`.sbs` / `.lol` 属低价 TLD，被批量滥用后可能整段被部分网络封锁——多备域名、分散 TLD 更稳。
- **源站 IP 历史泄露**：CF 之前若域名曾灰云直连过，历史 DNS/证书透明日志可能已记录 `165.154.233.121`；锁源站防火墙是必须的补救。
- **CF 政策**：遵守 Cloudflare 服务条款；违规内容仍可能被 CF 处理。
- **Telegram 是最终兜底**：无论域名/CF 怎样，`t.me` 触点不会被封，`FallbackNotice` 已把它固定在页脚。

---

*基于 2026-07-12 部署实况（deploy.ps1 源站 165.154.233.121 + 三域名）。CF IP 段以官网最新为准。此为运维清单，代码侧域名池已就绪。*
