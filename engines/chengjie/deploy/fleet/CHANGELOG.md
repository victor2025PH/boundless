# 智拓群控节点更新日志

## 0.3.26
打开 Facebook 不再回桌面去点壁纸上的图标坐标。前台已经是 `com.facebook.katana` 或 `com.facebook.lite` 时留在原地。否则按包名 `am start`。点进去是别的应用（Telegram、桌面空位、启动器）回执 `wrong_app_launched:<包名>`；跳到 Play 商店回执 `fb_not_installed_or_store_redirect`。这两种都不再报未登录。

找赞：层次里没有 Like、只靠 Comment/Share 等距推出位置时，截图那一路（模板、动作条结构、轮廓）只在这个点的 ±40 像素以内、并且落在动作条这一行里才算数。偏出约 600 像素的落点不计入，不能和别的信号凑成点击。窗口里对上的截图信号和位置信号是两路。`like_diag` 增加 `screenshot_outside` 和 `screenshot_window_px`，主控脱敏白名单保留它们，原来的字段也还在。

现场待办面板的计划任务 `ChatX Fleet Panel` 用交互组 `S-1-5-4`（INTERACTIVE）注册，不指定 SYSTEM。安装包写入 `panel_task.xml`，卸载时删掉这个任务、Run 键 `ChatXFleetPanel` 和这份 XML。服务进程在会话 0 时先用活动控制台用户令牌拉起面板；会话 0 里计划任务 `/Run` 返回成功不算已经显示。诊断增加任务运行账户、Run 键是否存在、面板进程所在会话号。服务自己的会话号仍是 `session_id`。

wp01 那种第二次探测一直停在「已领取」：一次 pull 会把整批都标成已领取，节点若在第一条上卡住或回执失败，后面的任务不会再被领走，主控以前也不收已领取的过期任务。现在 pull、列表、单条查询和总览都会把过了 `expires_at` 仍没有回执的已领取任务写成 `failed`，原因 `timeout`。节点循环里一条回执失败不再丢掉同批剩下的任务；本机已经看到截止时间就自己回 `timeout`，不再开工。排队中的过期仍是 `expired` / `ttl_expired`。

横屏时先关自动旋转并把 `user_rotation` 设为 0（只放行这两个写入），再截一张图。仍然是横屏就回 `landscape_orientation`，不继续点。日志和审计仍只写壁纸号或 `[redacted]`，不写序列号，不打印密钥。0.3.25 的自动派发排除（直播机、173、序列号前缀 `3B1F`、显式排除、拿不准就拒绝）不变。公开下载页的 latest 仍是 0.3.7。

## 0.3.25
直播机、坐席机 173、受保护手机不再靠各自一段关键词。主控用同一个判断：显示名里有「直播」、分组或标签是 `live`、主机名里有 `176` 或 `GANZHI-176`、节点 173、配置里的排除名单（节点 id 或主机名，精确匹配）命中任意一条就排除。拿不准（节点不是一份正常记录、排除名单格式不对、检查本身出错）也排除。`site_todo`、`phone_app_restart`、点赞和其它手机操作、以及任务队列里的批量下发都走这一关。机房节点 CHINAMI、CHINAMI-B、AORY-A、AORY-B 照常下发。受保护手机除了原来的整条序列号，序列号以 `3B1F` 开头的也拒绝。日志和审计仍只写壁纸号或 `[redacted]`。

现场待办弹窗改到当前登录用户的桌面上。节点服务在 session 0，只把名单写进 `%ProgramData%\ChatX\fleet\operator_alert.json`。安装包注册两个用户会话入口：计划任务 `ChatX Fleet Panel`（`ONLOGON`，交互用户，不是 SYSTEM），以及 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run` 的 `ChatXFleetPanel`。两者都在用户登录时启动面板进程，面板自己读快照，中英文切换不变。服务收到待办后会再 `schtasks /Run` 一次，让已经登录的桌面马上显示；任务还没装上时才退回原来的跨会话启动。公开下载页的 latest 仍是 0.3.7。

找赞：层次里同一行有 Comment 和 Share、但没有 Like 时，按动作条等距（Like 在 Comment 左侧、间距与 Comment 到下一个按钮相同）推出一个位置信号。这个点必须落在动作条这一行里，不能落在帖子头部那一行。它要和截图信号（模板、轮廓或动作条结构）对上才算两路，单独这一路不点。`like_diag` 增加 `position_inferred`，主控脱敏白名单保留这个字段以及原来的 hierarchy、attempts/via/error、shape_score、structure_bounds、template_score。`like_probe` 仍只定位。`dry_run` 仍不碰手机。点完仍要复核。

## 0.3.24
把已经分开验证过的三版收成一个金丝雀。机房电脑装这一份就同时有：0.3.21 的层次 dump 加固（先停自动播放，写固定文件再拉回，失败短退避重试）、0.3.22 的机房现场待办弹窗（按问题类别分组，中英文怎么做，序列号打成壁纸号或 `[redacted]`，直播机、173 和受保护手机排除）、0.3.23 的找赞定位（不再点截图上半部，离开信息流才按一次返回，`like_diag` 带层级计数和动作条区域信号，两路信号一致才点）以及 `phone_app_restart`（只对心跳里 `state=device` 的可操作手机 force-stop 再打开 Facebook，包名只许 `com.facebook.katana` / `com.facebook.lite`，单独的 `app_restart` 白名单门，直播机、173 和受保护手机拒绝，审计不写序列号）。登录检查失败仍带回前台包名和 activity。`like_probe` 仍只定位。`dry_run` 仍不碰手机。点完仍要复核。公开下载页的 latest 仍是 0.3.7。

## 0.3.23
找赞前不再点上一张截图的上半部。0.3.21 那一下轻点会把 Facebook 点进帖子或全屏，下一轮探测看不到顶栏，登录检查就报 `app_not_ready`。现在只发媒体暂停（`cmd media_session dispatch pause` 和 `input keyevent 127`）。dump 结束之后读一次前台：包名是 Facebook，页面又明确不是信息流（帖子详情、全屏、沉浸、故事、播放器），才按一次返回。主页、看不清的页面、别的应用都不按。公开下载页的 latest 仍是 0.3.7。

`like_diag.hierarchy` 只用于诊断，不参与点不点：`node_count`（全部节点）、`button_count`（过了尺寸的按钮）、`clickable_count`（可点节点，不看尺寸）、`row_counts`（每一行有几个过尺寸的按钮）、`top_package`（层级里第一个包名，信息流应是 `com.facebook.katana`）、`classes`（最常见的控件类型）。一次探测就能分开「层次里没有按钮」和「有按钮但被尺寸滤掉了」。

截图已经圈出动作条（`structure_bounds`）时，再在层次里找落在这个矩形里的节点，不看按钮尺寸。找到 Like / Comment / Share，或者 3 到 4 个间距差不多的可点节点，就和动作条结构合成两路信号。仍然要两路一致才点，点完仍要复核。单独这一路不点。`like_probe` 仍只定位。`dry_run` 仍不碰手机。

新增手机任务 `phone_app_restart`（端点 `.../phones/{serial}/app_restart`）。它只对心跳里 `state=device` 的可操作手机执行，先 `am force-stop` 再打开 Facebook 启动器。包名只允许 `com.facebook.katana` 和 `com.facebook.lite`。白名单把它单独放在 `app_restart` 门里，要 `allow_app_restart` 才放行；`allow_guarded_writes` 开了也不放行 Facebook 的 force-stop，这个标志也不放行别的包。没有别的任务会带 `allow_guarded_writes`。直播机、坐席机 173（`YUYAN-173` / `192.168.0.173`）、受保护手机 `3B1F4KE5MS140P4X` 在动手前拒绝。审计记谁下发的、壁纸号或 `[redacted]`、包名和结果，日志不写序列号。

登录检查失败（`not_logged_in` / `app_not_ready`）时，回执多一个 `foreground`：前台包名和 activity。序列号按原来的规则换成壁纸号或 `[redacted]`。不看截图也能分清是登出、弹窗还是点错图标。

## 0.3.22
机房现场待办。主控用每台电脑最新的网络体检、心跳里的手机状态和壁纸号台账，算出分类待办，只下发给这一台：没网、没信号、未授权、补壁纸号、USB 掉线、台账冲突、Facebook 未登录。Facebook 未登录要两个信号一致才报。每一项只有壁纸号或占位符，没有序列号。本机弹窗按类别分组，并写上怎么做（开流量或连 WiFi、点允许 USB 调试并贴号、补号、重插或换线、查 SIM 或摆位）；中英文切换还在。直播机和 173 不下发。受保护手机不进名单。离线节点在控制台显示「需重装 agent」。节点重启后从本机快照恢复网络行和待办。离线告警只留在主控日志和控制台，不推到外部。公开下载页的 latest 不因这一版改掉，仍是 0.3.7。

## 0.3.21
Facebook 信息流在播视频时，`uiautomator dump` 经常拿不到 idle，输出有字但解析不出层次。0.3.20 靠 dump 的三路信号（无障碍标签、resource-id、Comment/Share 同行最左按钮）因此一起落空。这一版不改「两个信号一致才点」，也不改 `like_probe` 只定位、`dry_run` 不碰手机。

找赞前先让画面停一下：`cmd media_session dispatch pause`、`input keyevent 127`（只暂停，不播放），再在上一张截图的上半部中点轻点一次。这不是点赞。然后把层次写到固定文件 `/sdcard/chatx_like_hierarchy.xml` 再 `adb pull` 回来读。dump 退出码不是 0 也照样 pull：idle 报错时文件里仍可能有完整 XML。失败就短退避再试：未压缩文件、再一次未压缩文件、`--compressed` 文件、stdout `/dev/tty`、最后才是 stdout `--compressed`。`--compressed` 必须写在路径前面。别的路径，包括 `/sdcard/window_dump.xml`，仍然拒绝。公开下载页的 latest 不因这一版改掉，仍是 0.3.7。

`like_diag` 新增：`uiautomator.attempts`（dump 次数）、`via`（`file` 或 `stdout`）、`compressed`、`error`（报错首行，走原来的序列号脱敏：对得上壁纸号就写成壁纸号，否则 `[redacted]`）。截图路补上 `shape_matched`、`shape_score`（没命中时也写出，最高 0.99），以及 `structure_matched` 命中的动作条整行 `structure_bounds`。这些字段只用于诊断，不参与是否点击。

## 0.3.20
`like_probe` 现在带回每个信号的诊断，方便看清只有图标的赞为什么没对上。`like_diag` 里有：uiautomator 有没有找到 Like/React、是靠 content-desc、text 还是 resource-id；动作条区域里实际出现的 content-desc / text / resource-id；模板匹配分（不到门槛也写出来）；结构回退有没有对上；以及候选动作条节点的 bounds、class、content-desc、resource-id、text。只含这些文字属性，不含截图。序列号会从这段文字里去掉。诊断不改变真点赞：仍然要两个信号一致，或者模板分足够高，点完仍要复核。

图标动作条的定位放宽了，但没有改成看到一个按钮就点。无障碍标签多了英语 / 他加禄语 / 中文（Gustuhin、点赞、讚、喜欢，以及原来的 Like / 赞 / いいね / React）。resource-id 里带 like 或 react 的算标签（例如 `feed_story_…like`）；单独的 `feed_story` 不算。同一行有 Comment / Share 时，最左边那个按钮可以和截图上的动作条、模板或拇指轮廓对上。标签本身仍然只和模板算两个信号。已赞、评论、分享、表情选择（Love / Haha / Wow）和没有评论/分享邻居的空白按钮都不点。点完仍要变蓝或变成 Liked，否则 `not_verified`。

手机任务失败时，回给主控的错误文字不再带原始序列号。节点在发送前回执里替换，主控在入库前再清一次。能对上壁纸号就写成壁纸号，对不上就写成 `[redacted]`。`error: device offline` 这种没有序列号的句子保持原样。公开下载页的 latest 不因这一版改掉，仍是 0.3.7。

## 0.3.19
Facebook 的发帖、点赞、评论、关注按账号限速，避免一个号在短时间里点太多而被停用。上限在 `config/compliance.yaml` 的 `facebook` 段，缺文件时用同一组内置默认值：赞每小时 6、每天 40；评论和关注每小时 3、每天 15；发帖每小时 1、每天 4。同一账号两次动作至少间隔 60 秒，再加 0–20 秒的固定间隔（由账号和上一次时间算出来，用来把动作摊开，不是用来躲检测）。本地时间按菲律宾 UTC+8，只在 8 点到 22 点之间执行（22 点整不算）。`enabled: false` 是总开关，关掉后这些动作都不再下发。超出上限是 `rate_capped`，不在时段是 `outside_active_hours`，间隔不够是 `min_delay`，总开关关掉是 `compliance_disabled`。主控直接 409，不入队。`like_probe` 和 `dry_run` 只定位或只出计划，不计数、不受这些上限。远程操作开关仍优先：没开还是 `remote_ops_disabled`。计数写在主控库，按节点 + 账号（没有账号就用壁纸号，再没有用序列号）。`GET /api/fleet/social-pace` 和总览里的 `social_pace` 能看到今天相对上限做了多少。节点执行前再查一次本机账本。养号和看视频里的赞还不计。控制台页面还没画这张表。这一版同时带上只读 `net_health`、分组 adb 白名单，以及只有图标的 Facebook 赞定位（见下面 0.3.18 两条，能力都在这个金丝雀里）。公开下载页的 latest 不因这一版改掉。

## 0.3.18
只读任务 `net_health`：对每台 `state=device` 的手机报告能不能上网（generate_204，失败再 ping / DNS）、当前是 WiFi 还是移动数据、SIM 和信号、飞行模式、移动数据开关、移动流量计数，以及 Facebook 是否安装、前台是不是登录页。手机按壁纸编号显示。本机告警窗口在壁纸号后面加一行，例如 `07 号：网络✓ wifi`、`网络✗ 无流量`、`移动数据弱信号`、`Facebook未安装`、`Facebook未登录`。命令被拒绝或本机没有该命令时，对应字段是 `unavailable`，任务继续，不把整单打成失败。

Android 没有稳定的 adb 接口能读运营商剩余流量或话费。这个字段默认是 `available: false`，说明是 `carrier-specific, not available by default`。只有 agent.json 里 `net_health_ussd_enabled` 为 JSON `true` 且该壁纸号配了 `*数字#` 时，才会试一次 USSD，并标明实验性；拨号不在默认允许的命令里，配置关掉时不会拨。`foreground_facebook: true` 才会用 `am start` 把 Facebook 拉到前台，默认不拉。

adb 白名单在 `adb_allowlist.py`，按类分开：只读诊断、仅限 Facebook 的启动、以及默认关闭的写入（改设置、开关流量、重启、卸载、force-stop）。写入要调用方显式带 `allow_guarded_writes`，没有群控任务会带这个标志。只读诊断里多一条 `shell uiautomator dump /dev/tty`（不写到手机存储）；写到文件的 dump 仍拒绝。公开下载页的 latest 不因这一版改掉。

Facebook 点赞在只有图标的动作条上也能找到赞。现在的帖子底下是反应 / 评论 / 分享图标，加上 `92k`、`1.5k` 这种计数，没有 Like / Comment / Share 这几个字。节点不装 rapidocr 也能定位：评论输入条上方那排 3–4 个均匀图标的最左格，加上赞的轮廓或实心模板（浅色/深色，多尺寸），或者拇指轮廓。仍然要两个信号一致，或者模板分足够高，才点一下；点完仍要变蓝或变成 Liked，否则 `not_verified`。只有一个弱信号不点。打开 Facebook 不再只在 0.5 秒时看一次顶部蓝色 Home：会轮询大约 9 秒。前一次滑动把顶栏藏起来时，这段时间里向上滑，把顶栏带回来。确认已在信息流之后，先滑回顶部并停 3 秒，再找动作条。找赞时先做一次只读的 `uiautomator dump /dev/tty`：无障碍标签是 Like / 赞 / いいね / React（和 huoke 同一套排除，已赞、评论、分享、reactions 计数都不算），再要赞的模板在同一位置对上，才算两个信号。dump 失败或这台机读不到层次时，退回截图上的动作条结构加模板或拇指轮廓。`like_probe` 同样如此，只是不点赞。文字引擎仍是可选的，默认安装包不带 rapidocr / onnx / opencv。公开下载页的 latest 不因这一版改掉。

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
