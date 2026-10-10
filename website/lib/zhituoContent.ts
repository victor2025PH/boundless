/**
 * 智拓 产品内容单一数据源（下载页 /download/zhituo 与 /en/download/zhituo 共用）。
 *
 * 合规隔离（lib/isolation.ts）：/download/zhituo 为 gated —— 主站不收录、链接 nofollow、
 * 不进结构化数据；对外口径保持中性（产品名 + 中性一句话），能力细节留在本页内。
 *
 * 发布新版本时：更新 download.version / size / filename / sha256 即可。
 * sha256 必须与实际上传到 /downloads/zhituo/ 的安装包一致（打包脚本 BUILD OK 行输出）。
 * 智拓安装包是 Windows Inno Setup 打包（非 electron-updater），故无 latest.yml 运行时校正，
 * 版本/体积/校验以本文件为唯一事实源。
 */
import type { BrandLang } from "./brand";

export interface Zt<T = string> {
  zh: T;
  en: T;
}

/** 下载源基址：与 VPS 永久下载目录一致（nginx 静态服务 /downloads/zhituo/）。 */
export const ZHITUO_RELEASE_BASE = "/downloads/zhituo";

export const ZHITUO = {
  hero: {
    trustline: {
      zh: "本地部署 · 数据不出本机 · 一体安装一次到位 · SHA-256 可校验",
      en: "Local deployment · data stays on-device · one-shot installer · SHA-256 verifiable",
    },
  },
  /** 下载构建信息（发布时同步）。 */
  download: {
    version: "1.0.1",
    size: { zh: "约 56 MB", en: "~56 MB" },
    filename: "ZhituoSetup-1.0.1.exe",
    os: { zh: "Windows 10 / 11（64 位）", en: "Windows 10 / 11 (x64)" },
    url: `${ZHITUO_RELEASE_BASE}/ZhituoSetup-1.0.1.exe`,
    sha256: "DA1A59CFF206F37A1AE982B331F5EC43AF34F93A8B60FC8F97AC687EF58967FF",
    nodeOnly: {
      filename: "ZhituoRoomNodeSetup-0.1.9.exe",
      url: `${ZHITUO_RELEASE_BASE}/ZhituoRoomNodeSetup-0.1.9.exe`,
      note: {
        zh: "仅节点安装包：这台机房电脑已装过群控组件时用，只更新智拓节点。",
        en: "Node-only installer: use it when the control component is already installed; it only updates the Zhituo node.",
      },
    },
    note: {
      zh: "一体安装包含智拓机房节点、群控组件、自带 Python 与 scrcpy，一个程序装好全部，装完按账号自动接入主控。",
      en: "The all-in-one installer bundles the Zhituo room node, the control component, a built-in Python and scrcpy — one program installs everything and enrolls to the master by account.",
    },
  },
  preCheck: [
    { zh: "系统：Windows 10 / 11（64 位）", en: "OS: Windows 10 / 11 (64-bit)" },
    { zh: "权限：以管理员身份运行安装包", en: "Run the installer as Administrator" },
    { zh: "组件：已启用「OpenSSH 客户端」（设置-应用-可选功能）", en: "Enable the OpenSSH Client (Settings - Apps - Optional features)" },
    { zh: "网络：这台电脑能访问主控智拓后台（局域网）", en: "Network: this PC can reach the master Zhituo console on the LAN" },
    { zh: "磁盘：预留约 2 GB 空间", en: "Disk: keep ~2 GB free" },
  ],
  steps: [
    {
      title: { zh: "下载一体安装包", en: "Download the all-in-one installer" },
      time: { zh: "约 1–2 分钟", en: "~1–2 min" },
      detail: {
        zh: "在本页点击下载按钮，获取 **ZhituoSetup-1.0.1.exe**（约 56 MB，含节点、群控组件、Python 与 scrcpy，无需再下其他组件）。",
        en: "Click the download button to get **ZhituoSetup-1.0.1.exe** (~56 MB, bundling node, control component, Python and scrcpy — nothing else to download).",
      },
    },
    {
      title: { zh: "以管理员身份运行", en: "Run as Administrator" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "右键安装包选 **以管理员身份运行**。若弹出「未知发布者」提示，点 **更多信息 → 仍要运行** 即可。",
        en: "Right-click the installer and choose **Run as administrator**. If an \"unknown publisher\" notice appears, click **More info → Run anyway**.",
      },
      warn: {
        zh: "最常见卡点：**[[SmartScreen]]** 对新发布、未签名程序的例行提醒，不是病毒报警。不放心可先核对本页 SHA-256。",
        en: "Most common bump: **[[SmartScreen]]** is a routine notice for newly published, unsigned apps — not a virus alert. Verify the SHA-256 on this page if unsure.",
      },
    },
    {
      title: { zh: "选角色「机房节点」填电脑编号", en: "Pick the Room Node role and enter the PC code" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "角色选 **机房节点（副控）**，填这台机房电脑的 **电脑编号**（1–6 位、字母开头，如 C、F、G2）。手机会显示为「编号-壁纸号」，例如 C-01。",
        en: "Choose **Room Node**, then enter this PC's **code** (1–6 chars, starts with a letter, e.g. C, F, G2). Phones show as \"code-wallpaper\", e.g. C-01.",
      },
    },
    {
      title: { zh: "接入主控（关键一步）", en: "Enroll to the master (key step)" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "填 **主控登录账号与密码**——就是主控智拓后台（局域网地址，如 http://192.168.0.176:18080）的登录账号，不知道就问管理员。通过后自动接入、不需要再等人批准。暂时不方便接入，可勾选 **暂不接入主控**，装完后在桌面「智拓后台」或开始菜单「重新接入主控」里再补，不用重装。",
        en: "Enter the **master login account and password** — the login for the master Zhituo console on your LAN (e.g. http://192.168.0.176:18080); ask your admin if unsure. On success it enrolls automatically, no manual approval. Not convenient now? Tick **Enroll later** and finish it afterwards from the desktop \"Zhituo Console\" or Start menu \"Re-enroll\" — no reinstall.",
      },
      sub: {
        zh: [
          "账号认证是唯一门槛，因此请妥善保管主控账号。",
          "接入有失败限流、可吊销、全程留接入记录。",
          "同一个编号换了电脑时，勾选「用这台替换它」即可。",
        ],
        en: [
          "Account auth is the only gate, so keep the master account safe.",
          "Enrollment has failure rate-limiting, is revocable, and is fully audit-logged.",
          "Replacing a PC under an existing code? Tick \"replace it with this PC\".",
        ],
      },
    },
    {
      title: { zh: "安装自检 + 手机授权", en: "Self-check and authorize phones" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "装完会自动打开 **安装自检** 页，标红项按提示处理即可。手机第一次接入要在手机上点 **允许 USB 调试**。",
        en: "A **self-check** page opens automatically; handle any red items as prompted. On first connect, tap **Allow USB debugging** on the phone.",
      },
    },
    {
      title: { zh: "开始运营", en: "Start operating" },
      time: { zh: "即刻", en: "Instant" },
      detail: {
        zh: "接入成功后，主控即可在后台看到并操作这台电脑上的手机。至此这台机房节点已上线。",
        en: "Once enrolled, the master can see and operate the phones on this PC from the console. The room node is now live.",
      },
    },
  ],
  faqs: [
    {
      q: { zh: "SmartScreen / 杀毒软件拦截安装包怎么办？", en: "SmartScreen / antivirus blocks the installer — what do I do?" },
      a: {
        zh: "这是 Windows 对新发布、未做代码签名程序的例行提醒，不是病毒。点 **更多信息 → 仍要运行**；不放心可先核对本页 SHA-256 再放行。",
        en: "It's a routine warning for newly published, unsigned apps — not a virus. Click **More info → Run anyway**; verify the SHA-256 on this page first if unsure.",
      },
    },
    {
      q: { zh: "提示「群控组件安装失败」怎么办？", en: "It says the control component failed to install — now what?" },
      a: {
        zh: "通常是旧版群控组件或手机助手还开着、占用了安装。1.0.1 已会 **自动先停占用再装、失败也不影响智拓节点**；若仍失败，关闭 ChatX / 手机助手后重新运行一次安装包即可，不用卸载。",
        en: "Usually an old control component or phone helper is still running and holding the installer. 1.0.1 **stops the occupant first, and a failure no longer blocks the Zhituo node**; if it still fails, close those apps and re-run the installer — no uninstall needed.",
      },
    },
    {
      q: { zh: "「接入主控」要填的账号密码是什么？", en: "What account does \"enroll to master\" ask for?" },
      a: {
        zh: "就是主控智拓后台（局域网地址，如 http://192.168.0.176:18080）的登录账号和密码。用哪个主控账号登录，这台电脑就归哪个主控。不知道账号请问管理员。",
        en: "The login account and password for the master Zhituo console on your LAN (e.g. http://192.168.0.176:18080). Whichever master account you log in with decides which master this PC belongs to. Ask your admin for the account.",
      },
    },
    {
      q: { zh: "现在不想接入，可以装完再接吗？", en: "Can I install now and enroll later?" },
      a: {
        zh: "可以。安装时勾选 **暂不接入主控**，装完后在桌面「智拓后台」或开始菜单「重新接入主控」里补接入即可，不用重装。",
        en: "Yes. Tick **Enroll later** during install, then complete enrollment afterwards from the desktop \"Zhituo Console\" or Start menu \"Re-enroll\" — no reinstall.",
      },
    },
    {
      q: { zh: "数据保存在哪里？会上传吗？", en: "Where is my data stored? Is it uploaded?" },
      a: {
        zh: "节点配置、密钥与运行数据全部保存在本机，**不上传公网**；主控通过局域网 / 受控隧道访问，数据不出你的网络。",
        en: "Node config, keys and runtime data are stored locally and **not uploaded to the public internet**; the master connects over the LAN / controlled tunnel, data stays within your network.",
      },
    },
    {
      q: { zh: "已经装过群控组件，怎么只更新节点？", en: "Control component already installed — how do I update only the node?" },
      a: {
        zh: "用本页的 **仅节点安装包**（ZhituoRoomNodeSetup），它只更新智拓节点，不动已装的群控组件。",
        en: "Use the **node-only installer** (ZhituoRoomNodeSetup) on this page; it updates only the Zhituo node and leaves the installed control component untouched.",
      },
    },
  ],
} as const;

/** 便捷取值：按语言取一段文案。 */
export function zt(field: Zt, lang: BrandLang): string {
  return field[lang];
}