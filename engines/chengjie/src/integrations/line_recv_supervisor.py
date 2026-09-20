"""LINE 接收循环退避监督器（2026-08-27 06:11 宕机事故沉淀，纯决策零 IO）。

事故机制：okline ``OperationReceiver.stream(reconnect=True)`` 的重连循环**零退避**
——HMAC 签名桥崩坏（``memory access out of bounds``）后 ``_open_sse`` 即抛即重试，
每秒上百轮、每轮一条 WARNING；与日志轮转故障共振把实例打死（详见
``src/utils/resilient_logging.py`` 模块注释）。okline 是第三方包不改它，
控制点收在我们自己的接收线程包装层：

- worker 改用 ``bot.run(reconnect=False)``（一击语义：流断即返回/抛出），
  重连节奏由本监督器决策；
- 短命**失败**（异常，含签名桥即抛即返）按指数退避（5s → 10 → 20 → … 封顶 300s），
  连败 ``give_up_after`` 次后指示**放弃**——接收线程退出、
  ``_recv_started_ts`` 清零，``healthy()`` 随之变假，交给编排器的
  既有「error → 退避 → 重启」监督循环接管（那层还有自己的冷却与告警面）。
  两层退避叠加后，签名桥崩坏的最坏代价＝分钟级低频重试，绝无风暴。

⚠ ``clean``（本轮有没有抛异常）是**判据的一半，漏传即回归**（2026-08-27 09:30
实测事故）：LINE 网关的 SSE 本来就是 **~10s 一个轮回**——发一个 ``ping`` 事件
后**正常关流**，等客户端自己重连（这是 Chrome 扩展的设计，okline 内建
``reconnect=True`` 就是干这个的）。首版只按存活秒数判、把 60s 当门槛，于是
**每一次正常轮回都被记成短命失败** → 连败 6 次放弃 → 编排器重启 worker →
约 4 分钟一圈无限循环，两个 LINE 号 ``inbound_count`` 恒为 0（收消息全停），
而干净返回那条路径不抛异常、一条 WARNING 都不打，空转两小时无人察觉。
故门槛按结束方式分流：

- **干净结束**（网关正常关流）存活 ≥ ``clean_min_sec``＝正常轮回，小憩即重连、
  失败计数清零；
- **异常结束**要存活 ≥ ``healthy_min_sec`` 才算「真跑过一段的会话」而清零；
- 干净结束但**秒内即返**（200 后立刻关流那种病态）仍按失败退避——限速是硬要求
  （风暴防线），代价只是「病态时重连从 1/秒降到分钟级」。

**本层刻意不管「连着但永久聋」**（网关照常 10s 轮回却一条 op 都不来，多半是
token 该续期了）：那要靠 ``start()`` 探活 + ``status()`` 的
``last_inbound_ts``/``recv_cycles`` 读数，不是重连节奏能判的——在这里加「N 轮无 op
就放弃」只会把安静的账号误杀。

纯 dataclass 零依赖：决策可门禁（``tests/test_line_recv_supervisor.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LineRecvSupervisor"]


@dataclass
class LineRecvSupervisor:
    """接收线程的重连决策器（每次连接尝试结束时问它下一步）。"""

    #: **异常**结束时，存活达到该秒数才算健康会话（失败计数清零）
    healthy_min_sec: float = 60.0
    #: **干净结束**（网关正常关流）时的同类门槛——网关实测 ~10s 一轮，
    #: 取 3s 只为把「200 后秒内即返」那种病态挡在外面
    clean_min_sec: float = 3.0
    #: 健康会话正常断开后的重连间歇（礼貌小憩，防对端刚断就砸门）
    healthy_resume_sec: float = 1.0
    #: 短命失败的首次退避
    base_sleep_sec: float = 5.0
    #: 退避封顶
    cap_sleep_sec: float = 300.0
    #: 连败多少次后放弃（线程退出交编排器重启）
    give_up_after: int = 6
    #: 当前连败计数（跨 attempt 保持，健康会话清零）
    streak: int = 0

    def on_attempt_end(self, lived_sec: float, *, clean: bool = False) -> dict:
        """一次连接尝试结束 → 下一步决策。

        ``clean``＝本轮**没有抛异常**（生成器耗尽＝网关正常关流）。它决定用哪个
        存活门槛判「这轮算正常还是算失败」，见模块注释——**调用方必须如实传**，
        默认 False 只是为了老调用点不至于崩，不是可省参数。

        返回 ``{"sleep_sec": float, "give_up": bool, "streak": int}``：
        - ``give_up=True`` 时调用方应结束接收线程（sleep_sec 恒 0）；
        - 否则 sleep 后重连。
        """
        floor = float(self.clean_min_sec) if clean else float(self.healthy_min_sec)
        if float(lived_sec) >= floor:
            self.streak = 0
            return {"sleep_sec": float(self.healthy_resume_sec),
                    "give_up": False, "streak": 0}
        self.streak += 1
        if self.streak >= int(self.give_up_after):
            return {"sleep_sec": 0.0, "give_up": True, "streak": self.streak}
        sleep = min(float(self.cap_sleep_sec),
                    float(self.base_sleep_sec) * (2 ** (self.streak - 1)))
        return {"sleep_sec": sleep, "give_up": False, "streak": self.streak}
