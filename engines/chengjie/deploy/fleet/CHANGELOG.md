# 智拓群控节点更新日志

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
