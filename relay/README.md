# ChatX 官网中继（relay.bd2026.cc）

把公网 HTTPS 回调带到 NAT 后的智聊实例。企业微信（微信客服回调、成员扫码登录）只接受 **HTTPS 可信域名**；客户的智聊
跑在自己电脑上拿不到域名和证书——中继给每台设备一个公网前缀：

```
https://relay.bd2026.cc/d/<device_id>/wechat/kf/callback    企微「接收消息服务器 URL」
https://relay.bd2026.cc/d/<device_id>/login/wecom/callback   企业微信登录 redirect_uri
https://relay.bd2026.cc/WW_verify_xxx.txt                    企微「可信域名」归属验证文件（设备经 WebSocket 发布）
```

## 工作方式

- 设备（智聊实例，`relay.enabled: true`）主动出站连 `wss://relay.bd2026.cc/ws/device`，带 `device_id + secret`
  （首次即注册；`RELAY_REGISTER_KEY` 可限制谁能注册）。断线指数退避重连，每 25s 心跳。
- `/d/<id>/<path>` 只转**白名单**路径（回调 / webhook / 探活），JSON 打包经 WebSocket 转到设备，设备在本机回环重放，
  响应 ≤ 8s 回到公网侧；设备不在线 503，超时 504。不是通用反向代理，工作台不会被暴露。
- 成员登录回跳：state 第 4 段带设备**签名过的浏览器来源**（私网/回环），中继直接 302 把浏览器送回本地实例，不过隧道；
  公网来源一律 400（无开放重定向）。
- 设备可发送 `{"type":"verify_file","name":"WW_verify_xxx.txt","content":"..."}` 把企微验证文件发布到中继根目录。

## 部署 / 运维

```powershell
powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1                       # 全流程（venv、systemd、nginx、certbot、体检）
powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1 -CodeOnly             # 只更新代码并重启
powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1 -VerifyFile WW_verify_xxx.txt   # 手工上传验证文件
powershell -ExecutionPolicy Bypass -File relay\deploy_relay.ps1 -RegisterKey <key>    # 限制设备注册
```

VPS 侧：`systemctl status chatx-relay`、`journalctl -u chatx-relay -f`、`/var/log/nginx/relay.*.log`、
`/home/ubuntu/relay/{app.py,venv,state/devices.json,verify/,relay.env}`；nginx 站点 `/etc/nginx/sites-available/relay.bd2026.cc`。
探活：`https://relay.bd2026.cc/healthz`；某设备：`https://relay.bd2026.cc/api/device/<id>/status`。

## 测试

`cd relay && python -m pytest tests -q`（真起 uvicorn + websockets 假设备）；引擎侧 `engines/chengjie/tests/test_relay_client.py`
与 `test_wechat_e2e_parity.py::test_relay_carries_wecom_callbacks_into_nat_instance`（真中继 + 真 app 的企微回调 / 成员登录往返）。
