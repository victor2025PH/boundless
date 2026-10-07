# 智拓群控节点更新日志

## 0.3.17
Facebook 的 `phone_like` 不再点坐标文件里的固定 `like_button`。现场（壁纸 09）帖子动作条只有图标和计数（`92k`、`1.5k`），没有 Like / Comment / Share 这几个字，旧坐标会点进帖子内容。节点对截图做三件事：可选文字（Like / Gusto / I-like / 赞，同一行还要有评论和分享）、打包进去的浅色/深色赞图标模板（多尺寸）、以及计数行下面那排 2–4 个均匀图标的最左一格。至少两个信号一致，或者模板分足够高，才点一下。点完再截图，图标或文字变成蓝色 / Liked 才算成功；对不上就 `not_verified`，不连点。划了默认 3 次（`like_swipes`，0–8）仍没有帖子是 `empty_feed`，调度可以拿去养号。`like_probe` 走原来的点赞任务（同样受远程操作开关限制），只打开、滑动、截图、定位，不点赞。`dry_run` 仍不碰手机，计划里的 `like_button` 标成 `like_button_deprecated`。模板和文字引擎都没有时拒绝，原因 `ocr_unavailable`，不会退回固定坐标。养号和看视频仍用原来的坐标。`feed_tab` 改为顶部 Home `[82, 124]`。受保护手机、直播机、频率限制和默认关闭都不变。文字引擎是可选的 rapidocr-onnxruntime（拉丁文和中文都能读，不依赖 Windows 语言包）；没装时模板和动作条仍然工作，打包也不失败。公开下载页的 latest 不因这一版改掉。

## 0.3.16
`push_config` 可以写本机操作员告警：`operator_alert_enabled`（只有 JSON `true` 才打开）、`operator_alert_refresh_sec`、`operator_alert_language`（`zh` 或 `en`）、`operator_alert_fail_streak`、`wallpaper_map`。这些键写入 agent.json，下一次热加载或观察周期生效。未知键、类型不对、空补丁仍是 `not_supported_in_agent_v1`，不写文件。直播机在写入之前拒绝，告警保持关闭。回执带回开关和壁纸条数，不带回序列号，日志也不打序列号。只读任务 `operator_alert_diag` 返回开关、语言、刷新间隔，以及不能用的手机的壁纸编号和原因，不返回 adb 序列号。受保护手机和直播机判定不变。公开下载页的 latest 不因这一版改掉。

## 0.3.15
机房电脑可以在本机弹出「哪台手机不能用」。默认关闭。agent.json 里 `operator_alert_enabled` 写成 JSON `true` 才打开。窗口和气泡按壁纸编号列出当前不能用的手机（`wallpaper_map`），不显示 adb 序列号。adb 服务没起来、找不到 adb、或一台手机都没有时，提示这台电脑本身有问题。手机从离线、未授权、断开或连续失败里恢复后，名单会去掉它。窗口上可以在中文和 English 之间切换，选择记在 `%ProgramData%\ChatX\fleet\operator_alert_lang.json`，下次启动仍用这个语言。没有桌面的机器只把状态写进 `operator_alert.json`，不拖垮节点服务。直播机（`CHATX_FLEET_LIVE_STREAM` 或 `live-stream.flag`）即使写成 true 也不弹。受保护手机仍不出现。公开下载页的 latest 不因这一版改掉。

## 0.3.14
0.3.13 的现场包一启动就退出：构造 `PhoneFlows` 时传入了 `state_dir`，旧的 `__init__` 不收这个参数。0.3.14 的构造函数接收 `state_dir`，节点启动改走 `PhoneFlows.from_agent_settings`，和打包前的检查用的是同一组参数。`push_config` 的坐标路径、json、base64 行为不变。直播机和受保护手机的规则不变。公开下载页的 latest 不因这一版改掉。

## 0.3.13
`push_config` 可以写 `phone_flows_enabled`，也可以把 `phone_ui_map` 指到本机已经存在的坐标文件。远程下发坐标用 `phone_ui_map_json` 或 `phone_ui_map_b64`：先按 `validate_ui_map` 检查、不超过 256KiB，再写到 `%ProgramData%\ChatX\fleet\phone_ui_map.remote.json`，然后把 `phone_ui_map` 设成这个路径。回执只带回路径和字节数，不带回坐标正文，日志也不打全文。别的键、空补丁、两种正文同时给、正文和路径同时给，仍是 `not_supported_in_agent_v1`，不写文件。路径不存在是 `ui_map_missing`；JSON 或形状不对是 `ui_map_invalid`；超过 256KiB 是 `ui_map_too_large`。直播机在写入之前拒绝。受保护手机和直播机判定不变。公开下载页的 latest 不因这一版改掉。

## 0.3.12
单文件 chatx-agent 会带上 `phone_ui_map.json`，和冻结后的 `phone_flows` 放在同一目录。打包漏掉这份文件时，节点再读 `%ProgramData%\ChatX\fleet\phone_ui_map.json`。安装器只在那个文件还不存在时放一份默认坐标，已经有的不覆盖。agent.json 里的 `phone_ui_map` 仍然优先。

## 0.3.8
机房节点可以自带 adb。安装时加 `-ManageAdbServer`（安装包 `/MANAGEADBSERVER=1`）会在 agent.json 写入 `adb_manage_server: true`：本机没有 adb 服务时，只用安装目录里的 adb 把服务拉起来。默认关闭，已有安装不变。直播机不会拉起 adb 服务。
远程升级可以另带安装包：任务里同时有 `setup_url` 和 `setup_sha256` 时，节点先校验 sha256，再静默运行安装包（`manage_adb_server: true` 才加 `/MANAGEADBSERVER=1`）。已经登记的节点会带 `/KEEPIDENTITY=1`，不重新生成机器码，升级后仍是原来的节点。没有这两个字段时仍只更换 chatx-agent.exe。已经装好 platform-tools 的节点可以收 `enable_phone_adb`，打开 `adb_manage_server` 并拉起自带 adb。直播机拒绝安装包，也拒绝拉起 adb。`publish_agent.ps1 -VersionedOnly` 只上传带版本号的文件和金丝雀清单，不改公开的最新下载页。镜像 nginx 同时提供 `manifest-<ver>.json`。
管理员可让这台电脑上的手机发帖、点赞、评论、关注（Facebook / Instagram / TikTok；需开启远程操作，并在 agent.json 打开社交动作）。同一版心跳可以同时带上 phone_ops_v1 和 phone_flows_v1。
截图核对和步骤间的随机间隔写在 phone_ui_map.json，默认关闭，不打开时动作和间隔与原来一样。锚点没到就停在这一步并有限次重试；打开应用后先确认已登录，未登录或还在登录页就停，不会接着发帖。
同一开关下还可以养号、发私信、看短视频（Facebook / Instagram / TikTok）。心跳同时带 phone_flows_v1 和 phone_flows_v2。养号只浏览不发帖；私信先确认会话已打开；回执不带回文字和账号名。

## 0.3.7
管理员可远程查看并操作这台电脑上的手机（需在主控为该电脑开启远程操作，直播手机受保护）。

## 0.3.6
上报手机清单。

## 0.3.5
解决克隆电脑机器码重复。

## 0.3.4
节点安装器可以把电脑登记到智拓群控，等待管理员批准后上线。
