# Changelog

## [1.1.0] - 2026-03-28

### 安全加固
- API Key 从配置文件移至环境变量（DEEPSEEK_API_KEY）
- 集群密钥外部化（OPENCLAW_CLUSTER_SECRET），为空自动生成
- 密码哈希从 SHA256 静态盐升级为 PBKDF2-SHA256（100,000次迭代）
- 创建 .env.example 环境变量模板 + .gitignore

### 架构重构
- api.py: 2,942 → 439行（-85%），提取 29个路由模块 + 4个辅助模块
- dashboard.py: 8,693 → 1,699行（-80%），CSS/JS 提取为独立静态文件
- dashboard.js: 6,626行拆分为 16个功能模块
- 集群设备发现增强: Coordinator 主动刷新 Worker 设备（30秒周期）

### Bug 修复
- 修复 leads/store.py 数据库连接提前关闭导致的 ProgrammingError
- 修复 Windows 下 YAML 文件 GBK 编码错误
- 补充 /compliance/{platform}/{action}/remaining 缺失端点

### 新功能
- POST /cluster/refresh-devices: 手动刷新集群设备列表
- Worker 增量更新脚本 (update_worker.py)
- 设备发现缓存（10秒 TTL）减少 ADB 调用

### 测试
- 600个测试全部通过（修复了5个预先存在的失败）

## [1.0.0] - 2026-03-06

### 初始版本
- 基础 ADB 设备控制
- 7 平台应用自动化
- FastAPI REST API
- Web Dashboard
- 432个测试用例
