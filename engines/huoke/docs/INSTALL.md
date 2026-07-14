# OpenClaw B2B 客服 SaaS — 部署安装文档

> v1.0 · 2026-04-26 · 基于本仓库 13 phase + 真实运维测试踩过的所有坑

适用场景：单 coordinator + 1-N worker (每 worker 接 N 部 Android 手机), B2B 客户私有部署。

---

## 0. 系统要求

### 0.1 Coordinator 主控

| 资源 | 最低 | 推荐 |
|---|---|---|
| OS | Windows 10/11 / Ubuntu 22.04 | Windows 11 (与 Worker 一致) |
| CPU | 4 核 | 8 核 |
| RAM | 8 GB | 16 GB |
| 磁盘 | 50 GB | 200 GB SSD |
| 网络 | 100 Mbps | 千兆内网 + 公网 |
| Python | 3.13 | 3.13 |
| PostgreSQL | 16+ | 16+ |
| GPU | 可选 | RTX 30+ (跑本地 Ollama LLM) |

### 0.2 Worker 节点

| 资源 | 最低 |
|---|---|
| OS | Windows 11 |
| CPU | 4 核 |
| RAM | 8 GB |
| 磁盘 | 50 GB |
| USB 端口 | 至少 N+2 (N=接的手机数) |
| ADB | platform-tools 35+ |
| Python | 3.13 |

### 0.3 设备 (Android)

- Android 11+ (实测 Xiaomi Redmi Note 13 Pro Android 13)
- ADB debugging 已开启
- 已安装 Facebook + Messenger
- VPN App (V2RayNG 推荐, 配日本节点)
- USB 数据线稳定 (建议 USB 3.0 hub)

---

## 1. Coordinator 安装

### 1.1 PostgreSQL 安装 + 关键修复

```bash
# Windows
choco install postgresql16
# Ubuntu
sudo apt install postgresql-16
```

#### ⚠️ Chinese Windows 必做：lc_messages 修复

中文 Windows 上 PG 默认错误消息是 GBK 编码，psycopg2 解码会崩。必须 superuser 永久设置：

```bash
# 替换 <SUPERUSER_PW> 为 postgres 超级用户密码
PGPASSWORD=<SUPERUSER_PW> psql -h 127.0.0.1 -U postgres -d openclaw \
  -c "ALTER ROLE openclaw_app SET lc_messages='C';"

# 验证 (应输出 C)
PGPASSWORD=<APP_PW> psql -h 127.0.0.1 -U openclaw_app -d openclaw \
  -c "SHOW lc_messages;"
```

**这条不做的后果**: dashboard 随机出现 503 "central store unavailable: 'utf-8' codec can't decode byte 0xd6"。

### 1.2 创建数据库 + 用户

```sql
-- 用 postgres 超级用户登录后执行
CREATE USER openclaw_app WITH PASSWORD 'YOUR_STRONG_PASSWORD';
CREATE DATABASE openclaw OWNER openclaw_app;
CREATE DATABASE openclaw_test OWNER openclaw_app;
ALTER ROLE openclaw_app SET lc_messages='C';
```

### 1.3 应用 schema migrations

```bash
cd /path/to/openclaw
PGPASSWORD=$OPENCLAW_PG_PASSWORD psql -h 127.0.0.1 -U openclaw_app -d openclaw \
  -f migrations/001_central_customer_schema.sql

# 后续 phase 的 migration 按顺序应用
for f in migrations/00{2,3}*.sql; do
  PGPASSWORD=$OPENCLAW_PG_PASSWORD psql -h 127.0.0.1 -U openclaw_app -d openclaw -f "$f"
done

# 同样应用到 openclaw_test (跑测试用)
```

### 1.4 .env 配置

```bash
# /path/to/openclaw/.env
OPENCLAW_PG_HOST=127.0.0.1
OPENCLAW_PG_PORT=5432
OPENCLAW_PG_DB=openclaw
OPENCLAW_PG_USER=openclaw_app
OPENCLAW_PG_PASSWORD=YOUR_STRONG_PASSWORD
OPENCLAW_PORT=8000
PYTHONIOENCODING=utf-8

# 可选 (启用 API key 鉴权 / webhook 通知)
# OPENCLAW_API_KEY=YOUR_API_KEY
# OPENCLAW_NOTIFY_WEBHOOK=https://oapi.dingtalk.com/...
# OPENCLAW_NOTIFY_TYPE=dingtalk  # generic|slack|dingtalk|feishu
```

### 1.5 Python 依赖

```bash
pip install -r requirements.txt
```

### 1.6 启动

```bash
# Windows: 编辑 run_server.bat 确保含
#   set OPENCLAW_PORT=8000
# 然后双击运行 (或用 schtasks 注册自启)

# Linux:
set -a; source .env; set +a
python server.py
```

### 1.7 验证

```bash
curl http://127.0.0.1:8000/health
# 期待: {"status":"ok","version":"1.2.0",...}

python scripts/e2e_smoke.py --base http://127.0.0.1:8000
# 期待: 29 stages 全 passed
```

---

## 2. Worker 节点安装

### 2.1 Python + ADB

```bash
# 推荐路径: C:\platform-tools\adb.exe
# 或: C:\Android\android-sdk\platform-tools\adb.exe
```

### 2.2 同步项目代码

**方式 A (推荐): OTA 自动从 coord 拉**

```bash
# Worker 节点跑
python -c "
import urllib.request, zipfile, io, os
url = 'http://<COORD_IP>:8000/cluster/update-package'
data = urllib.request.urlopen(url, timeout=60).read()
target = r'C:\openclaw\mobile-auto-project'
os.makedirs(target, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(data)) as zf:
    zf.extractall(target)
print('done')
"
```

**方式 B: git clone**

```bash
git clone <REPO_URL> C:\openclaw\mobile-auto-project
```

### 2.3 cluster.yaml

```yaml
# C:\openclaw\mobile-auto-project\config\cluster.yaml
role: worker
coordinator_url: "http://<COORD_IP>:8000"
local_port: 8000
shared_secret: "<MATCH_COORD>"
heartbeat_interval: 10
host_timeout: 30
auto_join: true
host_name: "W01"
host_id: "worker-01"
```

### 2.4 run_server.bat

```bat
@echo off
set USERPROFILE=C:\Users\Administrator
set HOME=C:\Users\Administrator
set OPENCLAW_PORT=8000
set PATH=C:\platform-tools;C:\Users\Administrator;C:\Windows\System32;C:\Windows
"C:\platform-tools\adb.exe" kill-server
ping 127.0.0.1 -n 2 > nul
"C:\platform-tools\adb.exe" start-server
ping 127.0.0.1 -n 4 > nul
cd /d C:\openclaw\mobile-auto-project
"C:\Program Files\Python313\python.exe" server.py
```

### 2.5 schtasks 自启 (Windows)

```powershell
schtasks /Create /SC ONSTART /RU SYSTEM /TN "OpenClaw-W01" \
  /TR "C:\openclaw\run_server.bat" /F
```

### 2.6 验证

```bash
# 主控查看 worker 已注册
curl http://<COORD_IP>:8000/cluster/devices
# 应能看到 host_id=worker-01 的设备
```

---

## 3. Android 设备初始化

### 3.1 ADB 准备

设备开发者选项打开, USB debugging 启用, 点 "Always allow from this computer"。

```bash
adb devices
# 应能看到 device (不是 unauthorized)
```

### 3.2 Facebook + Messenger

设备装 Facebook (com.facebook.katana) + Messenger (com.facebook.orca), 登录企业自己的客服账号。

### 3.3 VPN 配置 (V2RayNG)

```bash
# 装 V2RayNG
# 导入企业的 SOCKS/V2Ray/Trojan 配置文件
# 主界面: 选中节点 → 点右下角 ▶ FAB → 等"已连接"toast

# 验证 IP 切换
adb -s <DEVICE_ID> shell curl -m 10 -s https://ipinfo.io/json
# 期待 country=JP (或目标地区)
```

### 3.4 输入法统一

```bash
# 确保设备能输入中文/日文
python scripts/unify_ime.py
```

### 3.5 关闭 MIUI 安全弹窗 (如果是小米)

```bash
python scripts/disable_miui_security_popups.py
```

---

## 4. 集群健康验证

### 4.1 e2e smoke (**每次部署/重启后必跑**)

```bash
python scripts/e2e_smoke.py --base http://127.0.0.1:8000
# 29 stages 必须全 passed
```

### 4.2 SLO 红线检查 (cron 每 5 min)

```bash
# Linux cron
*/5 * * * * cd /path && python scripts/slo_check.py --webhook >> logs/slo.log

# Windows Task Scheduler
schtasks /Create /SC MINUTE /MO 5 /TN OpenClawSLO \
  /TR "python C:\openclaw\mobile-auto-project\scripts\slo_check.py --webhook"
```

### 4.3 daily snapshot (cron 每天)

```bash
# 凌晨 3 点跑
0 3 * * * cd /path && python scripts/daily_snapshot.py >> logs/snapshot.log
```

### 4.4 真实流量诊断 (新设备激活后)

```bash
# 跑 cluster_load_test 看每台设备健康
python scripts/cluster_load_test.py --base http://127.0.0.1:8000

# 设备网络/SIM 诊断
python scripts/device_diagnose.py
python scripts/sim_captive_diagnose.py
```

---

## 5. 常见问题 (FAQ)

### Q1: dashboard 503 "central store unavailable: 'utf-8' codec..."

**原因**: PG `lc_messages` 没设 C, 错误消息走 GBK 编码崩。

**修复**: 见 §1.1 ALTER ROLE 命令; 然后**重启 coord**让新连接生效。

### Q2: worker 心跳一直不到 coord

```bash
# 1. 确认 worker 配的 coordinator_url 端口正确
cat config/cluster.yaml | grep coordinator_url
# 2. 确认 coord 在该端口监听
netstat -an | grep ":8000"
# 3. 确认 worker 防火墙允许出站 8000
# 4. 确认 worker run_server.bat 设了 OPENCLAW_PORT=8000 (新代码默认 18080!)
```

### Q3: facebook_add_friend 报 "phase=cold_start 禁止加好友"

**这是反风控保护**, 不是 bug。新账号需要先跑 `facebook_browse_feed` 累计 200+ 屏 + 24h+ 才能升 growth phase。

加速测试可以临时 task params 加 `phase: 'growth'` + `force_add_friend: true`。

### Q4: send_greeting 报 "概率门未命中"

**这是反风控 enabled_probability 设计** (默认 growth phase 0.8 = 80% 执行)。要么重派几次, 要么改 `config/facebook_playbook.yaml` 把 growth.send_greeting.enabled_probability 改 1.0。

### Q5: search 找不到目标用户

**FB 搜索有隐私限制**, 客户隐私设为"仅朋友的朋友"时搜不到。需要通过 profile URL 直加: `target_url: 'https://facebook.com/profile.php?id=...'`。

### Q6: VPN 出口仍是本地 IP, 不是日本

```bash
adb -s <DEV> shell dumpsys activity services | grep -i v2ray
# 没结果 = V2RayNG 没启动. 手动开 App 点 ▶ 或脚本模拟 tap (608, 1380)
```

### Q7: 任务一直 pending 不执行

可能 device_lock 被前一个任务卡住:

```bash
# 主控
curl http://127.0.0.1:8000/tasks/active-by-device

# 看哪个 task 占 lock, 必要时 cancel:
curl -X POST http://127.0.0.1:8000/tasks/<TASK_ID>/cancel
```

### Q8: Worker 上 git 不存在, OTA 升级失败

```bash
# 不需要 git, OTA 用 zip 包. 见 §2.2 方式 A
# 如果 OTA 也失败, 检查 Worker 网络连得到 coord:
curl http://<COORD_IP>:8000/cluster/update-package/info
```

### Q9: dashboard 看到 cluster_load 数据为 0

正常, 当前看到 0 表示集群没在跑真实业务任务。跑过几个真实任务后数据就有了:

```bash
python scripts/cluster_load_test.py
```

### Q10: LLM (chat_brain) 调用失败

```bash
# 1. 看本地 ollama 是否运行
curl http://127.0.0.1:11434/api/tags
# 2. 配置文件 config/llm_routing.yaml 检查模型名
# 3. fallback Anthropic 检查 ANTHROPIC_API_KEY env
```

---

## 6. 升级流程

### 6.1 主控升级

```bash
git pull origin main
# 应用新 migrations (如有)
ls migrations/*.sql | xargs -I{} psql -d openclaw -f {}
# 重启服务
taskkill /F /IM python.exe  # Windows
# 或 systemctl restart openclaw
python server.py
# e2e 验证
python scripts/e2e_smoke.py
```

### 6.2 Worker 升级 (OTA)

主控会自动打包最新代码到 `/cluster/update-package`. Worker 跑:

```bash
python scripts/upgrade_now.py  # 见 §2.2 方式 A 模板
schtasks /Run /TN "OpenClaw-W01"  # 重启 worker 服务
```

---

## 7. 监控 + 运维

每天:
- e2e_smoke 跑 1 次 (重启后)
- chat_review 5 min review 20 条对话
- 看 dashboard ⏰ 红色超时 / refer rate 健康区

每 5 min cron:
- slo_check --webhook (红线立即推钉钉/飞书)

每天凌晨:
- daily_snapshot 落 reports/

每周日:
- weekly_report 出周报 markdown

---

## 8. 文档清单

- `docs/INSTALL.md` (本文档) — 部署
- `docs/OPS_RUNBOOK.md` — 运维异常处置
- `docs/INTEGRATION_CONTRACT.md` — A/B 双 worker 协同契约
- `docs/SYSTEM_ARCHITECTURE.md` — 系统架构
- `docs/PITCH.md` — B2B 销售一页纸
- `docs/CASE_STUDY.md` — 实测案例

---

> 部署有问题? 邮件: support@openclaw.com (示例) / 提 issue
