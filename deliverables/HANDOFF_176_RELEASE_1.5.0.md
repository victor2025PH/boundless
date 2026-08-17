# 给 176 开发线的发布指令（AvatarHub 1.5.0 全量包）

**任务：立即暂停手头开发，收敛提交当前改动，构建并发布 AvatarHub 全量安装包（1.5.0）。以下背景和红线必须先读完再动手。**

## 一、你不知道的背景（2026-08-10 凌晨发生的事）

1. 另一条线通过 **app:core 热修**在现有 `releases/1.4.6` 发布树上连发了四版：1.4.7 → 1.4.8 → 1.4.9 → 1.4.10，存量用户已在自动更新。改动全部在两个文件里：
   - `launcher_wizard.py`（现 76,906 字节）：安装向导 UI 全面改版（解压/配置期实时活动行、双泳道状态、功能域实时百分比、暂停/失败态、混合 ETA、续装进度不归零）；
   - `pack_installer.py`（现 115,042 字节）：链式预取+实时总账（边下边装）、**吞吐择源**（替代延迟择源）、出网统一 UA（修复 r2.dev 镜像对 urllib 恒 403 的存量 bug）、`_apply_app_stage` 字节码缓存修复（utime 现势化+清 `__pycache__`）、**解压写线程池**（4 写线程，小文件实测 5x，真机装机 -11%）。
2. **你树上这两个文件当前的内容 = 线上 1.4.10 已发布状态，是权威版本，原样提交，绝不要回退**。树上的 `*.bak_20260810_*` 是热修前备份，提交时排除，确认后可删。
3. 线上 `releases/1.4.6/manifest.json` 已重签（Ed25519，公钥指纹 6b170b9e18a3472a），`components.app.core.app_version = 1.4.10`。**1.4.6 树正在给存量用户提供更新服务，禁止覆盖或删除**；新版本发到新目录。
4. VPS nginx（`/etc/nginx/snippets/avatarhub.conf`）有一条 `location ^~ /releases/1.4.6/packs/ { return 302 https://dl.bd2026.cc$request_uri; }` 把组件包分流到 R2。新版本树发布后需要同款一行（见第四节第 6 步）。
5. 新增了四个运维逃生门环境变量（客服排障用，写进发布说明）：`AVATARHUB_PREFETCH=0`、`AVATARHUB_PREFETCH_AHEAD`（默认 3）、`AVATARHUB_PROBE_THROUGHPUT=0`、`AVATARHUB_EXTRACT_WORKERS=0`。

## 二、硬性红线

- **先提交后构建**。1.4.6 就是从无基线的脏树直出的，git 里连发布提交都没有，这次绝不允许重演。
- 你自己未收敛的改动（`avatar_hub.py` +6,668 行、`doctor.py` +1,285、`hair_api.py` +855、`hair_feasible.py`、`speech_enhance.py` 均 untracked、`gen_download_manifest.py` +58）——**自行决定哪些进这次发布**：进的必须跑完你们自己的回归；不进的用 `git stash` 收起，绝不能让半成品被打进包。
- 发布期间不做任何其他开发操作，不并行第二个构建。

## 三、提交顺序建议

```
commit 1: 安装链 1.4.7-1.4.10 已发布状态（launcher_wizard.py + pack_installer.py，如实注明「线上已发布，补录入库」）
commit 2+: 你自己的功能改动（按模块拆，跑过回归的才进）
```

## 四、发布步骤（全部用仓库现成管线）

1. `version.py` 的 `APP_VERSION` 改为 `1.5.0`，跑 `python tools/version_lint.py --write` 同步派生文件（注意：树上现在还是 1.4.6，热修只改了包内副本）。
2. `release.config.json`：`version` → 1.5.0；`base_url` → `https://usdt2026.cc/releases/1.5.0`；`mirrors` 两条的版本段同步改 1.5.0；`prev_manifest` → `https://usdt2026.cc/releases/1.4.6/manifest.json`（增量发布：未变组件包不重传、服务器硬链克隆）。
3. 验收前置：`pack_acceptance.py`（B-15，上轮基线 496.6s 全绿）+ `pack_gui_acceptance.py`（B-16）必须全过才继续。
4. 走 `publish_release.py` 一键发布（构建 exe → 签名 → make_release --ci → 发布树 → 上传），完成后 `--verify-remote` 冒烟。
5. **R2 镜像同步**：按 `secrets/deploy/r2_upload_kit/` 把 `releases/1.5.0/` 整树同步到 R2 桶 `avatarhub`（与主源同布局），manifest 最后传（包先行、指针后行）。
6. 通知 117 侧（回报里带上）：需要在 VPS nginx 加一行 `location ^~ /releases/1.5.0/packs/ { return 302 https://dl.bd2026.cc$request_uri; }` 并 reload——117 侧有 VPS 权限可代做。
7. 官网 `/download` 页的版本文案（website 仓库 `lib/releaseNotes.ts`）不归你，117 侧处理。

## 五、完成后必须回报的清单

- [ ] 两个 commit 的 hash 与内容摘要；哪些自有改动进了/没进这次发布
- [ ] B-15 / B-16 结果与 install_v1 耗时
- [ ] `AvatarHub-Setup-1.5.0.exe` 的 sha256 与体积
- [ ] `https://usdt2026.cc/releases/1.5.0/manifest.json` 可达 + 验签通过的确认
- [ ] R2 侧 `dl.bd2026.cc/releases/1.5.0/manifest.json` 可达确认
- [ ] `versions.json` / `channels.json` 更新结果（存量 1.4.10 用户的跨版本升级路径）
