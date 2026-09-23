"""fleet —— 主控（fleet_control 域实例）↔ 节点 Agent（每台受控电脑）的共享层。

* ``protocol``：协议版本、任务种类、信封形状（两端唯一事实源）。
* ``identity``：节点机器标识（复用 licensing 机器指纹，取不到则本地稳定回退）。
* ``store``：主控侧 SQLite（nodes / node_tasks / node_heartbeats / enroll_codes）。
* ``agent``：节点侧出站 Agent（注册 → 心跳 → 长轮询领任务 → 执行 → 幂等 ack）。

设计铁律（docs/FLEET_CONTROL_CONTRACT.md）：**节点永远出站连主控**，主控不反向连节点；
心跳只带指标/摘要，不带聊天原文；任务幂等（task_id）、带 TTL、stop 类最先。
"""
