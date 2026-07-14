---
name: telegram-automation
description: Automate Telegram on real Android devices via uiautomator2. Covers sending/reading messages, file transfer, user search, multi-account switching, group operations, message monitoring, and AI auto-reply. Use when working with Telegram automation, messaging, account management, or cross-device Telegram tasks.
---

# Telegram Automation Skill

## Module Location

`src/app_automation/telegram.py` — `TelegramAutomation` class

## Architecture

```
TelegramAutomation
├── 核心操作
│   ├── send_message(username, message)
│   ├── read_messages(username, count)
│   ├── send_file(username, file_path)
│   ├── search_user(query)
│   └── forward_message(from_user, to_user, count)
├── 多账号管理
│   ├── list_accounts(device_id)
│   ├── switch_account(account_name, device_id)
│   └── get_current_account(device_id)
├── 群组操作
│   ├── join_group(invite_link)
│   ├── read_group(group_name, count)
│   └── send_group_message(group_name, message)
├── 消息监听
│   ├── monitor_start(username, callback)
│   └── monitor_stop(username)
└── 导航辅助
    ├── go_home() → 返回主界面
    ├── go_back()
    └── ensure_main_screen()
```

## Key Patterns

### UI 选择器优先级

1. `resource-id` (最稳定): `org.telegram.messenger:id/xxx`
2. `content-desc` (次选): 按钮图标描述
3. XML dump + 坐标计算 (最可靠的 fallback): `d.dump_hierarchy()` → `lxml.etree` 解析 → `d.click(cx, cy)`
4. 视觉 Fallback (最后手段): 截图 → Qwen3-VL → 坐标

### 账号切换 (Phone1 三账号)

Phone1 使用侧边栏 UI:
1. `fresh_start(d)` — 确保在主界面
2. 点击左上角汉堡菜单
3. 点击 "Expand accounts" / 检测 "Show accounts"
4. XML dump 找到目标账号名 → 坐标点击

Phone2 使用底部 Tab UI:
1. 长按 Profile tab
2. 从弹出列表选择账号

### 搜索兼容性

不同设备 Telegram 版本的搜索入口不同:
- **有搜索按钮**: 点击放大镜 → 输入关键词
- **直接 EditText**: `d(text="Search Chats")` → 点击 → 输入

### 合规参数

```yaml
telegram:
  message_interval_sec: [3, 8]        # 消息间隔（均匀随机）
  hourly_message_limit: 30
  search_interval_sec: [5, 15]
  flood_wait_backoff_sec: 300         # 遇到 flood wait 等待
  session_active_min: [20, 40]        # 活跃周期
  session_rest_min: [5, 15]           # 休息周期
```

## Device Map

| 设备 | Device ID | 账号 |
|------|-----------|------|
| Phone1 | 89NZVGKFD6BYUO5P | Carlin, Vivian, Chaya Chaya |
| Phone2 | R8CIFUBIOVCIUW5H | Vyanka (@vyanks) |

## Dependencies

- `uiautomator2`: 设备连接 + UI 操作
- `lxml`: XML 层级解析
- `HumanBehavior`: 人类行为模拟引擎 (see [human-behavior skill](../human-behavior/SKILL.md))
- `ComplianceGuard`: 速率限制

## Error Handling

| 错误类型 | 处理策略 |
|---------|---------|
| 元素未找到 | 重试 3 次 + XML dump 验证 + 视觉 Fallback |
| Flood wait | 暂停 5 分钟，降低后续频率 |
| 设备断连 | HealthMonitor 自动重连 |
| 对话框弹窗 | `ensure_main_screen()` 自动关闭 |

## API Endpoints (FastAPI)

```
POST /api/telegram/send          # 发送消息
POST /api/telegram/search        # 搜索用户
POST /api/telegram/switch        # 切换账号
GET  /api/telegram/accounts      # 列出账号
GET  /api/telegram/messages      # 读取消息
POST /api/telegram/monitor/start # 开始监听
POST /api/telegram/monitor/stop  # 停止监听
POST /api/telegram/forward       # 转发消息
```
