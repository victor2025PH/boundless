---
name: whatsapp-automation
description: Automate WhatsApp on real Android devices via uiautomator2. Covers sending/reading messages, media sharing, group interactions, status updates, and auto-reply. Use when working with WhatsApp automation, messaging, group management, or cross-device WhatsApp tasks.
---

# WhatsApp Automation Skill

## Module Location

`src/app_automation/whatsapp.py` — `WhatsAppAutomation` class

## Architecture

```
WhatsAppAutomation
├── 消息操作
│   ├── send_message(contact, message)
│   ├── read_messages(contact, count)
│   ├── send_media(contact, file_path, caption)
│   ├── forward_message(from_contact, to_contact)
│   └── reply_to_message(contact, original_text, reply)
├── 群组操作
│   ├── send_group_message(group_name, message)
│   ├── read_group(group_name, count)
│   ├── create_group(name, members)
│   └── add_group_member(group_name, contact)
├── 状态/朋友圈
│   ├── post_status(text_or_media)
│   └── view_statuses()
├── 联系人
│   ├── search_contact(query)
│   └── add_contact(phone_number, name)
├── 自动回复
│   ├── auto_reply_start(rules)
│   └── auto_reply_stop()
└── 导航
    ├── go_home()
    ├── go_back()
    └── ensure_main_screen()
```

## 当前状态

WhatsApp 模块 **已有基础框架但需真机校准**:
- `send_message` / `read_messages` — 选择器需更新
- 其他功能需从零开发

### 校准步骤 (Phase 2 优先任务)

1. 在两台设备上启动 WhatsApp
2. `d.dump_hierarchy()` 导出各个页面的 XML
3. 提取关键元素的 `resource-id` / `content-desc`
4. 更新选择器映射表
5. 逐个功能测试

## UI 选择器映射 (待校准)

```python
SELECTORS = {
    "search": {
        "search_btn": 'resourceId="com.whatsapp:id/menuitem_search"',
        "search_input": 'resourceId="com.whatsapp:id/search_src_text"',
    },
    "chat": {
        "message_input": 'resourceId="com.whatsapp:id/entry"',
        "send_btn": 'contentDescription="Send"',
        "attach_btn": 'contentDescription="Attach"',
    },
    "navigation": {
        "chats_tab": 'text="Chats"',
        "status_tab": 'text="Status"',
        "calls_tab": 'text="Calls"',
        "back_btn": 'contentDescription="Back"',
    },
}
```

## 合规参数

WhatsApp 没有公开限制，但批量操作会被封号:

```yaml
whatsapp:
  message_interval_sec: [5, 15]       # 消息间隔
  hourly_message_limit: 20            # 保守限制
  daily_message_limit: 100
  only_existing_contacts: true        # 只给已有联系人发
  media_interval_sec: [10, 30]        # 媒体发送间隔
  group_message_interval_sec: [10, 30]
  session_active_min: [15, 30]
  session_rest_min: [10, 30]
```

### 封号风险行为 (绝对避免)

- 给非联系人群发消息
- 短时间大量加群
- 发送相同内容到多个联系人
- 频繁添加陌生号码

## 与 Telegram 的差异

| 特性 | Telegram | WhatsApp |
|------|----------|----------|
| 搜索 | 用户名搜索 | 只能搜联系人/群名 |
| 多账号 | 单设备多账号 | 单设备单账号 (需双开) |
| 机器人 | 原生 Bot API | 无 (只能 UI 自动化) |
| 消息限制 | Flood wait (宽松) | 封号 (严格) |
| 文件传输 | 2GB | 2GB (较新版本) |

## Device Map

| 设备 | Device ID | 状态 |
|------|-----------|------|
| Phone1 | 89NZVGKFD6BYUO5P | 待确认 WhatsApp 登录状态 |
| Phone2 | R8CIFUBIOVCIUW5H | 待确认 WhatsApp 登录状态 |

## API Endpoints (FastAPI)

```
POST /api/whatsapp/send             # 发送消息
POST /api/whatsapp/send-media       # 发送媒体
GET  /api/whatsapp/messages         # 读取消息
POST /api/whatsapp/search           # 搜索联系人
POST /api/whatsapp/group/send       # 群发消息
GET  /api/whatsapp/group/messages   # 读群消息
POST /api/whatsapp/status           # 发状态
POST /api/whatsapp/auto-reply/start # 开启自动回复
POST /api/whatsapp/auto-reply/stop  # 关闭自动回复
```
