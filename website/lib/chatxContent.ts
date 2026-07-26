/**
 * 智聊 ChatX 桌面客户端下载页内容单一数据源（/download/chatx 与 /en/download/chatx 共用）。
 *
 * 与 AvatarHub（manualContent.ts）/ 智控（matrixxContent.ts）刻意分离：三个客户端的
 * 上手卡点完全不同——ChatX 是「聚合 AI 聊天工作台」桌面形态：不需要显卡（AI 推理走
 * 云端/内网服务），最大卡点是首启初始化（内嵌本地服务解包）与未签名安装包的
 * SmartScreen 提示，本页重点覆盖这两处。
 *
 * 发布新版本时：更新 download.version / size / filename / sha256；页面运行时还会
 * fetch /downloads/manifest.json（打包脚本生成）自动校正版本与哈希，此处是构建时兜底。
 */
import type { BrandLang } from "./brand";

export interface Cx<T = string> {
  zh: T;
  en: T;
}

/** 下载源基址：与 desktop/package.json 的 electron-updater publish url 指向同一目录
 *  （同源，官网下载版与客户端自动更新版一致）。 */
export const CHATX_RELEASE_BASE = "/downloads";

export const CHATX = {
  download: {
    version: "0.2.0",
    size: { zh: "214 MB", en: "214 MB" },
    filename: "ChatX-Setup-0.2.0.exe",
    os: { zh: "Windows 10 / 11（64 位）", en: "Windows 10 / 11 (x64)" },
    url: `${CHATX_RELEASE_BASE}/ChatX-Setup-0.2.0.exe`,
    /** 与实际上架 /downloads/ 的安装包一致（scripts/gen-chatx-manifest.ps1 计算）；
     *  运行时会被 manifest.json 的值覆盖。 */
    sha256: "d414928884ba35547746a17d43b4bfaf7448525a7d73bb0190b75927f1dfc0d2",
    /** 运行时清单（打包脚本生成，含 version/size/sha256/signed）。 */
    manifestUrl: `${CHATX_RELEASE_BASE}/manifest.json`,
    macNote: {
      zh: "macOS 版规划中：当前后端为 Windows 原生打包，跨平台需分别构建。",
      en: "A macOS build is planned: the bundled backend is currently packaged natively for Windows.",
    },
  },
  preCheck: [
    { zh: "系统：Windows 10 / 11（64 位）", en: "OS: Windows 10 / 11 (64-bit)" },
    { zh: "显卡：无需独立显卡（AI 推理在云端 / 内网服务完成）", en: "GPU: none required (AI inference runs on cloud / LAN services)" },
    { zh: "磁盘：预留约 1.5 GB 空间（安装 + 本地数据）", en: "Disk: keep ~1.5 GB free (install + local data)" },
    { zh: "网络：需能访问所接入的聊天平台与 AI 服务", en: "Network: access to your chat platforms and AI services" },
  ],
  install: {
    steps: [
      {
        title: { zh: "下载安装包", en: "Download the installer" },
        time: { zh: "约 2–5 分钟", en: "~2–5 min" },
        detail: {
          zh: "点击本页下载按钮获取 **ChatX-Setup-0.2.0.exe**（约 214 MB，含桌面壳与本地服务，一次到位无需再下组件）。",
          en: "Click the download button to get **ChatX-Setup-0.2.0.exe** (~214 MB, desktop shell plus local backend in one package — no extra components).",
        },
      },
      {
        title: { zh: "运行安装程序", en: "Run the installer" },
        time: { zh: "约 1 分钟", en: "~1 min" },
        detail: {
          zh: "双击安装包，一路点 **下一步** 即可，支持自选安装目录。完成后桌面与开始菜单出现「智聊 ChatX」图标。",
          en: "Double-click the installer and click **Next** through the wizard; choose an install folder if you like. A ChatX icon appears on the desktop and Start menu.",
        },
        warn: {
          zh: "最常见卡点：**SmartScreen** 提示「Windows 已保护你的电脑」——这是 Windows 对新发布、尚未建立签名信誉的程序的例行提醒，不是病毒报警。点 **更多信息 → 仍要运行** 即可。可在下方核对安装包 SHA-256 后再安装。",
          en: "Most common bump: **SmartScreen** shows \"Windows protected your PC\" — a routine notice for newly published apps without established signing reputation, not a virus alert. Click **More info → Run anyway**. You can verify the SHA-256 below before installing.",
        },
      },
      {
        title: { zh: "首次启动 · 本地服务初始化", en: "First launch · local service init" },
        time: { zh: "约 10–30 秒", en: "~10–30 s" },
        detail: {
          zh: "首次打开会解包并启动内嵌的本地服务，出现工作台登录页即就绪；之后每次启动都是秒级。数据保存在本机用户目录，卸载重装不丢配置。",
          en: "The first launch unpacks and starts the bundled local service; you're ready when the workspace login page appears. Subsequent launches are instant. Data lives in your local user folder and survives reinstalls.",
        },
      },
      {
        title: { zh: "登录工作台并接入渠道", en: "Sign in and connect channels" },
        time: { zh: "约 5–10 分钟", en: "~5–10 min" },
        detail: {
          zh: "用默认管理员账号登录后，在「渠道中心」接入你的聊天账号（Telegram / WhatsApp / Messenger / LINE 等），并在设置里填入 AI 服务凭据。",
          en: "Sign in with the default admin account, connect your chat accounts in Channel Center (Telegram / WhatsApp / Messenger / LINE and more), and add your AI service credentials in Settings.",
        },
      },
      {
        title: { zh: "开始接待", en: "Start closing" },
        time: { zh: "即刻", en: "Now" },
        detail: {
          zh: "统一收件箱汇聚全渠道消息：AI 自动拟稿 / 自动回复、实时互译、语音消息、客户画像与跟进提醒全部就位。",
          en: "The unified inbox aggregates every channel: AI drafting / auto-reply, live translation, voice messages, customer profiles and follow-up reminders — all ready to go.",
        },
      },
    ],
    faqs: [
      {
        q: { zh: "SmartScreen / 杀毒软件提示风险，安全吗？", en: "SmartScreen / antivirus flags the installer — is it safe?" },
        a: {
          zh: "安装包为新发布的未签名构建，Windows 与部分杀软会对「首次见到的程序」例行提示，属于信誉机制而非病毒检出。请只从本官网下载，并用页面公布的 **SHA-256** 校验（PowerShell：`Get-FileHash 安装包路径`），哈希一致即为官方原版。代码签名证书接入后此提示会逐步消失。",
          en: "The installer is a newly published, unsigned build. Windows and some antivirus tools routinely warn about first-seen programs — it's a reputation mechanism, not a virus detection. Download only from this site and verify the published **SHA-256** (PowerShell: `Get-FileHash <installer>`). A matching hash confirms the official build. Once code signing lands, the warning fades out.",
        },
      },
      {
        q: { zh: "以后怎么升级？要重新下载吗？", en: "How do updates work? Do I re-download?" },
        a: {
          zh: "客户端内置自动更新：启动时检查新版本，后台静默下载，退出时自动安装，账号与数据全保留，无需手动重装。",
          en: "Auto-update is built in: the client checks on launch, downloads in the background and installs on exit — accounts and data preserved, no manual reinstall.",
        },
      },
      {
        q: { zh: "我的账号和聊天数据存在哪里？", en: "Where do my accounts and chat data live?" },
        a: {
          zh: "全部保存在本机用户目录（登录会话、配置、聊天记录），不经过我们的服务器中转。AI 回复调用你自己配置的 AI 服务，凭据也只存本地。",
          en: "Everything stays in your local user folder (sessions, config, chat history) — nothing routes through our servers. AI replies call the AI service you configure; credentials are stored locally too.",
        },
      },
      {
        q: { zh: "支持哪些聊天平台？", en: "Which chat platforms are supported?" },
        a: {
          zh: "Telegram、WhatsApp、Messenger、LINE 等主流平台的消息可汇聚到统一收件箱；各平台的接入方式（协议 / 网页会话）在渠道中心里按向导完成。",
          en: "Telegram, WhatsApp, Messenger, LINE and more aggregate into the unified inbox; each platform's onboarding (protocol / web session) is wizard-guided in Channel Center.",
        },
      },
      {
        q: { zh: "需要显卡或很强的电脑吗？", en: "Do I need a GPU or a powerful machine?" },
        a: {
          zh: "不需要。AI 推理在云端或你的内网算力节点完成，本机只跑工作台与本地服务，普通办公电脑即可流畅使用。",
          en: "No. AI inference runs on cloud or your LAN compute nodes; the desktop only runs the workspace and a lightweight local service — a regular office PC is plenty.",
        },
      },
      {
        q: { zh: "免费吗？怎么获得正式授权？", en: "Is it free? How do I get a license?" },
        a: {
          zh: "下载与试用免费。正式授权按团队规模与功能模块订阅，联系顾问获取方案与试用码。",
          en: "Download and trial are free. Production licenses are subscription-based by team size and modules — contact us for a plan and trial code.",
        },
      },
    ],
  },
} as const;

/** 便捷取值：按语言取一段文案。 */
export function cx(field: Cx, lang: BrandLang): string {
  return field[lang];
}
