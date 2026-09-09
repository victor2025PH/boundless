// pm2 进程定义（Q-14 #262 C，2026-09-09）：官网 yuntech 从 fork(npm start) 改 cluster×2。
//
// 为什么：publish_chatx / push_announcement / deploy.sh 此前都 `pm2 restart yuntech`——fork 模式
// 重启 = 老进程先死、新进程再起，中间 4–8s 全站 5xx（09-08 19h R78 部署窗 nginx 记了 7 次）。
// cluster 模式下 `pm2 reload yuntech` 是滚动：先起新 worker、就绪后再停旧 worker，端口由
// pm2 master 持有，部署期 /api/ai/hub/health 零 5xx。
//
// 一次性迁移（VPS，只做一次；之后所有脚本只 reload）：
//   cd /home/ubuntu/yuntech && pm2 delete yuntech && pm2 start ecosystem.config.js && pm2 save
//
// 约束：
// - 绑定 127.0.0.1（deploy.sh 校验；nginx 反代到 127.0.0.1:3000）。
// - cluster 需要 node 脚本入口，故直接跑 next 的 bin 而不是 `npm start`（npm 进程不可 cluster）。
// - instances=2：4C/3.9G 小鸡上两份 Next.js（各 ~250–400M）够用；内存吃紧就改 1（仍可 reload，
//   pm2 会先起新再停旧——单实例 reload 也是零停机，只是切换瞬间两份并存）。
// - 进程内状态（额度 gw_quota 在 SQLite 共享；识图/慢模型冷却表在内存）按 worker 各自维护，
//   冷却语义弱化为「每 worker 独立判定」——可接受。
module.exports = {
  apps: [
    {
      name: "yuntech",
      cwd: "/home/ubuntu/yuntech",
      script: "node_modules/next/dist/bin/next",
      args: "start -H 127.0.0.1 -p 3000",
      exec_mode: "cluster",
      instances: Number(process.env.YUNTECH_INSTANCES || 2),
      // next start 不 process.send('ready')：按 listen 事件判就绪（pm2 cluster 会拦到 listen）
      wait_ready: false,
      listen_timeout: 15000,
      kill_timeout: 12000,
      max_memory_restart: "900M",
      autorestart: true,
      exp_backoff_restart_delay: 200,
      merge_logs: true,
      time: true,
      env: { NODE_ENV: "production", PORT: "3000" },
    },
  ],
};
