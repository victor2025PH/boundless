# 下载站改为 R2 独立承担（2026-09-10）

老板拍板：VPS（165.154.233.121，58 GB 盘）不再保存 AvatarHub 组件包镜像，下载全部走 Cloudflare R2（`dl.bd2026.cc` / r2.dev）。

## 改了什么

| 项 | 之前 | 之后 |
|---|---|---|
| `/var/www/dl/releases/<ver>/packs/*.tar.zst`、`AvatarHub-Setup-*.exe`、`matrixx/*.exe` | 本机保存一份（硬链接跨版本共用，共 38 GB） | **删除**；只留清单类小文件（manifest / versions / channels / notices / release_manifest / SHA256SUMS / *.md） |
| nginx `snippets/avatarhub.conf` | 白名单版本块：带 Range 302 到 R2，不带 Range / HEAD 本机直出（08-12 事故后加的兜底） | `/releases/**` 下所有二进制后缀一律 302 到 `https://dl.bd2026.cc$request_uri`；JSON 仍本机 |
| R2 `avatarhub/releases/` | 只有 1.4.3 / 1.4.6 / 1.5.0–1.5.3 / matrixx | 补齐 1.0.1 / 1.3.0 / 1.4.0 / 1.4.1 / 1.4.2（大包与 1.4.3 同 inode → R2 服务端复制 160 个对象 126.7 GB，不走 VPS 带宽；小文件 96 个上传 702 MB）+ 各版本缺的 md / SHA256SUMS / `app-1.4.7~9` 热修包 |
| Next `/dl/[...path]` 路由 | 探测 R2 → 302 R2，否则回落本站同路径 | 不变；回落到本站后 nginx 再 302 到 R2（不会死循环，只会失败） |

## 代价（已接受）

- 用户到 Cloudflare 边缘不可达时安装器会失败（08-12 之前一刀切 302 时出过一次 1.4.6 Lite 卡 99.1%）。
- R2 多存约 127 GB 历史版本（≈ $2 / 月）。

## 应急回退

1. `sudo cp /etc/nginx/snippets/avatarhub.conf.bak_20260910 /etc/nginx/snippets/avatarhub.conf && sudo nginx -t && sudo systemctl reload nginx`
2. `rclone copy r2:avatarhub/releases /var/www/dl/releases --transfers 4`（约 38 GB，按 VPS 15 Mbps 需数小时；可只拉当前版本 1.5.3）

## 发新版的新规矩

发布脚本必须先 `rclone copy` 到 `r2:avatarhub/releases/<ver>/` 再改清单——本机不再有兜底，R2 缺文件 = 用户 404。

## 同日：智拓方案站挂到 /reachx-story/

`https://bd2026.cc/reachx-story/` 是智拓 ReachX 故事频道方案的静态站（给合作方看）。最初放在 Next 的 `public/reachx-story/`，
当天 20:33–20:41 的两次全站部署（`deploy.sh` 的 `rsync --delete`）把它刷掉了两次；现改为 nginx 别名直出 `/var/www/reachx-story`
（`snippets/reachx-story.conf`，本仓 `scripts/reachx-story.conf` 是同步副本），与应用目录脱钩，部署不再影响、更新不用重启。
内容由智拓仓 `scripts/content/story_site_publish.ps1` 上传。

## 记录

- 实施与验证脚本：`/home/ubuntu/ops/r2_complete.py`（补齐 + `rclone check --size-only` 逐版本核对）、日志 `/home/ubuntu/ops/r2_complete.log`、删除前清单 `/home/ubuntu/ops/dl-local-inventory-20260910.txt`。
- 同日还做了：ChatX 安装包只留 latest（三通道）、journald 上限 200M（drop-in）、apt / npm 缓存与孤儿部署包清理。磁盘 96% → 85% →（本项完成后）约 20%。
