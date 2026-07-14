---
name: workflow-engine
description: Cross-platform workflow orchestration engine for coordinating tasks across Telegram, LinkedIn, and WhatsApp. Supports JSON workflow definitions, DAG execution, account routing, and compliance-aware scheduling. Use when building multi-step automation workflows, cross-platform task sequences, or account scheduling logic.
---

# Workflow Engine Skill

## Module Locations

- `src/workflow/engine.py` — `WorkflowEngine` 执行器
- `src/workflow/parser.py` — JSON 工作流解析
- `src/workflow/models.py` — 数据模型
- `src/behavior/account_router.py` — 账号路由

## Architecture

```
WorkflowEngine
├── 执行核心
│   ├── run_workflow(workflow_json) → WorkflowResult
│   ├── run_step(step) → StepResult
│   ├── pause_workflow(workflow_id)
│   └── resume_workflow(workflow_id)
├── 变量系统
│   ├── resolve_variable($step1.output.profiles)
│   └── for_each 循环展开
├── 错误处理
│   ├── on_failure: skip / retry / abort
│   └── retry_with_backoff(step, max_retries)
└── 事件
    ├── on_step_start(callback)
    ├── on_step_complete(callback)
    └── on_workflow_complete(callback)

AccountRouter
├── select_account(platform, action) → (device_id, account_name)
├── release_account(device_id, account_name)
├── get_quota_status(platform) → dict
└── cooldown_check(platform, account) → bool
```

## Workflow JSON Schema

```json
{
  "name": "workflow_name",
  "description": "工作流描述",
  "version": "1.0",
  "triggers": {
    "manual": true,
    "cron": "0 9 * * 1-5",
    "event": "new_message"
  },
  "variables": {
    "target_industry": "software engineering",
    "greeting_template": "Hi {name}, ..."
  },
  "steps": [
    {
      "id": "step_id",
      "name": "步骤名",
      "platform": "telegram|linkedin|whatsapp",
      "action": "action_name",
      "device": "auto|device_id",
      "account": "auto|account_name",
      "params": {},
      "output": "variable_name",
      "for_each": "$prev_step.output[:N]",
      "delay": {"min": 30, "max": 120},
      "condition": "$step1.output.count > 0",
      "on_failure": "skip|retry|abort",
      "max_retries": 3
    }
  ],
  "compliance": {
    "linkedin_daily_connections": 25,
    "linkedin_daily_messages": 30,
    "telegram_hourly_messages": 25,
    "whatsapp_hourly_messages": 20
  }
}
```

## AccountRouter 策略

```
选择账号流程:
1. 获取 platform + action 的所有可用账号
2. 过滤: 排除已达当日配额的
3. 过滤: 排除在冷却期的 (上次操作 < cooldown)
4. 排序: 按"最少使用"原则
5. 选择设备: 持有该账号的设备
6. 如需切换: 调用 platform.switch_account()
7. 返回 (device_id, account_name)
8. 执行后: record_action() 更新计数
```

## 预定义工作流模板

### LinkedIn 外展

```
搜索目标 → 浏览资料(模拟阅读) → 发连接请求(LLM改写备注)
→ [等待接受] → 发欢迎消息 → 记录到 CRM
```

### 跨平台通知

```
LinkedIn 收到新连接 → Telegram 通知管理账号
→ WhatsApp 发送日报摘要
```

### 多账号轮换发送

```
for account in TG_accounts:
  switch_account(account)
  send_messages(account.targets)
  rest(cooldown)
```

## Execution Model

```
WorkflowEngine 使用事件循环:

while steps_remaining:
    step = next_runnable_step()      # 考虑依赖关系
    if step.condition and not eval_condition(step.condition):
        skip(step)
        continue
    
    device, account = AccountRouter.select(step.platform, step.action)
    
    if not ComplianceGuard.check_quota(step.platform, step.action):
        wait_or_switch_account()
    
    HumanBehavior.wait_between_actions()
    
    result = execute_action(device, account, step)
    
    if step.for_each:
        expand_and_execute_loop(step, result)
    
    store_output(step.id, result)
    ComplianceGuard.record_action(step.platform, step.action)
```

## API Endpoints

```
POST /api/workflow/run              # 执行工作流
POST /api/workflow/pause/{id}       # 暂停
POST /api/workflow/resume/{id}      # 恢复
GET  /api/workflow/status/{id}      # 查询状态
GET  /api/workflow/list             # 列出所有工作流
POST /api/workflow/create           # 创建新工作流
GET  /api/accounts/status           # 所有账号配额状态
GET  /api/compliance/dashboard      # 合规面板数据
```
