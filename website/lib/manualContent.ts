/**
 * AvatarHub 产品手册与安装教程内容（单一数据源）。
 *
 * - INSTALL_GUIDE：详细安装步骤 + 常见安装问题，下载页教程版块与手册页共用。
 * - MANUAL_SECTIONS：在线手册（/manual）完整章节，支持浏览器打印导出 PDF。
 * - GLOSSARY：术语表，术语气泡（RichText）与手册「术语表」章节共用。
 *
 * 富文本标记（由 components/RichText.tsx 解析渲染）：
 * - `**文本**` → 关键操作/参数强调（按钮名、菜单路径、数值）
 * - `[[术语]]` → 带解释气泡的术语，解释取自 GLOSSARY（按当前语言匹配 term）
 * 结构化数据（HowTo JSON-LD）等纯文本场景用 stripRich() 剥离标记。
 *
 * 内容依据 engines/avatarhub 的《安装教程_图文版》《使用教程_图文版》《使用说明》
 * 整理为面向客户（安装包用户）的版本；产品行为变更时在此同步维护。
 */

export type ManualLang = "zh" | "en";

/** 术语表条目：term 为正文中 [[..]] 引用的原文（按语言各自匹配），def 为悬停/点击解释 */
export interface GlossaryEntry {
  term: { zh: string; en: string };
  def: { zh: string; en: string };
}

/**
 * 术语表（按正文出现顺序排列）。
 * 新增术语：在此加条目，正文用 [[术语]] 引用即可；未命中的 [[..]] 会降级为纯文本。
 */
export const GLOSSARY: GlossaryEntry[] = [
  {
    term: { zh: "薄核心安装包", en: "thin-core installer" },
    def: {
      zh: "安装器本体仅约 45 MB；几十 GB 的 AI 组件与模型在首次启动时按需下载，不用一次性下载完整包。",
      en: "The installer itself is only ~45 MB; the multi-GB AI components and models download on demand at first launch instead of shipping in one huge package.",
    },
  },
  {
    term: { zh: "SHA-256", en: "SHA-256" },
    def: {
      zh: "文件的唯一「数字指纹」。算出的校验值与官网公布一致，即可确认安装包完整、未被篡改。",
      en: "A unique digital fingerprint of a file. If the checksum you compute matches the one published here, the installer is intact and untampered.",
    },
  },
  {
    term: { zh: "SmartScreen", en: "SmartScreen" },
    def: {
      zh: "Windows 内置的程序信誉提示：对发布不久的安装包会例行提醒「已保护你的电脑」，这不是病毒报警。核对 SHA-256 后点「更多信息 → 仍要运行」即可。",
      en: "Windows' built-in reputation check. Recently published installers routinely trigger the \"Windows protected your PC\" prompt — it is not a virus alert. Verify the SHA-256, then click More info → Run anyway.",
    },
  },
  {
    term: { zh: "首启向导", en: "first-run wizard" },
    def: {
      zh: "第一次打开 AvatarHub 时的引导流程：自动检测硬件、推荐功能档位、下载所需组件，全程无需命令行。",
      en: "The guided flow on first launch: detects your hardware, recommends a capability tier and downloads the required components — no command line involved.",
    },
  },
  {
    term: { zh: "档位", en: "capability tier" },
    def: {
      zh: "按显卡能力划分的功能等级（入门 / 标准 / 旗舰），决定可开启哪些 AI 功能与组件下载体积。装后可在「设置 → 硬件档位」随时查看。",
      en: "Feature levels (Lite / Standard / Flagship) mapped to your GPU. The tier decides which AI features are available and how much gets downloaded. Check any time in Settings → Hardware tier.",
    },
  },
  {
    term: { zh: "显存", en: "VRAM" },
    def: {
      zh: "显卡的专用内存（VRAM）。AI 模型加载在显存里运行——显存越大，可同时开启的功能越多、画质档位越高。",
      en: "The GPU's dedicated memory. AI models load into VRAM to run — more VRAM means more features can run simultaneously at higher quality tiers.",
    },
  },
  {
    term: { zh: "断点续传", en: "resumable download" },
    def: {
      zh: "下载中断后从已完成的位置继续，已下载的部分不会重下。网络波动、关机重开都不影响进度。",
      en: "Interrupted downloads continue from where they stopped — finished parts are never re-downloaded, even across restarts.",
    },
  },
  {
    term: { zh: "在线激活", en: "online activation" },
    def: {
      zh: "在「设置 → 授权」输入订单号，客户端自动取回已签名的授权文件并生效，无需手动粘贴一长串激活码。",
      en: "Enter your order number in Settings → License and the client fetches and applies the signed license automatically — no pasting long activation strings.",
    },
  },
  {
    term: { zh: "冒烟测试", en: "smoke test" },
    def: {
      zh: "用最短路径验证核心功能可用：克隆一段 10 秒声音并试听，出声即代表「语音识别 → 合成 → 播放」整条链路正常。",
      en: "The quickest end-to-end check: clone a 10-second voice sample and play it back. If you hear it, the whole STT → synthesis → playback pipeline works.",
    },
  },
  {
    term: { zh: "OBS 虚拟摄像头", en: "OBS Virtual Camera" },
    def: {
      zh: "OBS Studio 提供的「虚拟摄像头」：把 AI 处理后的画面伪装成一个摄像头设备，微信 / 抖音 / Zoom 里直接选它即可出镜。",
      en: "A virtual webcam provided by OBS Studio: the AI-processed video shows up as a camera device that WeChat / TikTok / Zoom can select directly.",
    },
  },
  {
    term: { zh: "VB-Cable 虚拟声卡", en: "VB-Cable" },
    def: {
      zh: "虚拟声卡驱动：把 AI 处理后的声音变成一个「麦克风」设备（CABLE Output），聊天软件选它作麦克风即可让对方听到处理后的声音。",
      en: "A virtual audio driver: AI-processed audio becomes a microphone device (CABLE Output) that chat apps can pick as their mic input.",
    },
  },
  {
    term: { zh: "Ed25519 签名", en: "Ed25519 signature" },
    def: {
      zh: "更新清单的防伪数字签名。客户端只接受官方私钥签发的更新，被篡改的更新包会被直接拒绝。",
      en: "A cryptographic signature on release manifests. The client only accepts updates signed by the official key; tampered packages are rejected outright.",
    },
  },
  {
    term: { zh: "一键诊断包", en: "diagnostic pack" },
    def: {
      zh: "客户端内置的故障收集器：约 3–10 秒自动收集脱敏日志并直传客服，生成 6 位诊断码。联系客服时报码即可远程定位，无需手动发文件。",
      en: "A built-in troubleshooter: collects redacted logs in ~3–10 s, uploads them and returns a 6-digit code. Give support the code — no manual file sending.",
    },
  },
];

/** 剥离富文本标记，供 JSON-LD / meta 等纯文本场景使用 */
export function stripRich(s: string): string {
  return s.replace(/\*\*(.+?)\*\*/g, "$1").replace(/\[\[(.+?)\]\]/g, "$1");
}

/** 装前 30 秒自查（下载页教程头部的转化前置块） */
export const PRE_CHECK: { zh: string; en: string }[] = [
  { zh: "系统：Windows 10/11（64 位）", en: "OS: Windows 10/11 (64-bit)" },
  { zh: "显卡：NVIDIA 4 GB 显存起（推荐 8 GB+）", en: "GPU: NVIDIA 4 GB VRAM and up (8 GB+ recommended)" },
  { zh: "磁盘：SSD 预留 80 GB", en: "Disk: 80 GB free on SSD" },
  { zh: "网络：首次安装需联网下载组件", en: "Network: internet needed for first-time component download" },
];

/** 安装教程单步：标题 + 说明 + 可选要点 / 预计耗时 / 常见卡点警示 */
export interface InstallStep {
  title: { zh: string; en: string };
  detail: { zh: string; en: string };
  sub?: { zh: string[]; en: string[] };
  /** 预计耗时（展示为步骤徽章，给用户明确的进度预期） */
  time?: { zh: string; en: string };
  /** 常见卡点：该步骤最高频的疑惑/报错，用警示样式就地展示（不必翻 FAQ） */
  warn?: { zh: string; en: string };
}

/** 常见安装问题 */
export interface InstallFaq {
  q: { zh: string; en: string };
  a: { zh: string; en: string };
}

export const INSTALL_GUIDE: { steps: InstallStep[]; faqs: InstallFaq[] } = {
  steps: [
    {
      title: { zh: "下载安装包", en: "Download the installer" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "在本页点击下载按钮，获取 **AvatarHub-Setup-x.x.x.exe**（约 **45 MB** 的[[薄核心安装包]]，AI 组件稍后按需下载）。",
        en: "Click the download button on this page to get **AvatarHub-Setup-x.x.x.exe** (a **~45 MB** [[thin-core installer]]; AI components download on demand later).",
      },
      sub: {
        zh: [
          "下载完成后可核对本页给出的 [[SHA-256]] 校验值：在 PowerShell 运行 **certutil -hashfile 安装包路径 SHA256**，比对是否一致。",
        ],
        en: [
          "Optionally verify the [[SHA-256]] shown on this page: run **certutil -hashfile <installer path> SHA256** in PowerShell and compare.",
        ],
      },
    },
    {
      title: { zh: "运行安装程序", en: "Run the installer" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "双击安装包，一路点 **下一步** 即可。**按用户安装、免管理员权限**，可自选安装目录。安装完成后，桌面与开始菜单会出现 **AvatarHub** 图标。",
        en: "Double-click the installer and follow the wizard. It installs **per-user with no admin rights** and lets you choose the folder. An **AvatarHub** icon appears on the desktop and Start menu when done.",
      },
      warn: {
        zh: "最常见卡点：[[SmartScreen]] 提示「已保护你的电脑」——这是 Windows 对新发布程序的例行提醒，不是病毒报警。点 **更多信息 → 仍要运行** 即可；不放心可先核对 [[SHA-256]] 再放行。",
        en: "Most common snag: the [[SmartScreen]] \"Windows protected your PC\" prompt — a routine notice for recently published apps, not a virus alert. Click **More info → Run anyway**; verify the [[SHA-256]] first if you like.",
      },
    },
    {
      title: { zh: "首次启动 · 硬件检测", en: "First launch · hardware detection" },
      time: { zh: "约 2 分钟", en: "~2 min" },
      detail: {
        zh: "双击打开 AvatarHub，[[首启向导]]会自动检测你的显卡，并推荐适合的功能[[档位]]（入门 / 标准 / 旗舰），**不用敲一行命令**。",
        en: "Open AvatarHub. The [[first-run wizard]] detects your GPU and recommends a [[capability tier]] (Lite / Standard / Flagship) — **zero command line**.",
      },
      sub: {
        zh: [
          "**入门档（4–6 GB [[显存]]）**：声音克隆、图片 / 视频换脸。",
          "**标准档（8 GB+ 显存，RTX 3060 起）**：实时换脸、数字人直播。",
          "**旗舰档（24 GB 显存，RTX 4090 / 5090）**：实时高清 + 克隆音同传全家桶。",
        ],
        en: [
          "**Lite (4–6 GB [[VRAM]])**: voice cloning, photo / video face swap.",
          "**Standard (8 GB+ VRAM, RTX 3060 and up)**: live face swap, digital-human streaming.",
          "**Flagship (24 GB VRAM, RTX 4090 / 5090)**: real-time HD plus the full interpreting suite.",
        ],
      },
    },
    {
      title: { zh: "组件自动下载", en: "Automatic component download" },
      time: { zh: "约 10–30 分钟（视网速）", en: "~10–30 min (bandwidth)" },
      detail: {
        zh: "确认档位后，向导自动下载所需 AI 组件与模型。全程 [[SHA-256]] 校验、支持[[断点续传]]。组件体积按档位约 **10–60 GB**，建议预留 **80 GB SSD** 空间；下载期间可以先熟悉界面。",
        en: "Confirm the tier and the wizard downloads the required AI components and models — every file [[SHA-256]] verified, all downloads [[resumable download]]. Expect **10–60 GB** depending on tier; keep **80 GB free on an SSD**. Feel free to explore the UI meanwhile.",
      },
      warn: {
        zh: "下载慢或中断了？直接关掉重开 AvatarHub 即可——[[断点续传]]会从断点继续，**已完成的部分不会重下**。弱网建议换更稳的网络再继续。",
        en: "Slow or interrupted? Just reopen AvatarHub — [[resumable download]] continues from the breakpoint and **finished parts are never re-downloaded**. On weak networks, switch to a steadier connection.",
      },
    },
    {
      title: { zh: "激活或开始试用", en: "Activate or start the trial" },
      time: { zh: "约 1 分钟", en: "~1 min" },
      detail: {
        zh: "在 **设置 → 授权** 输入订单号即可[[在线激活]]（自动取回已签授权）；没有订单也可以直接开始 **14 天免费试用**，无需登记任何信息。",
        en: "Go to **Settings → License**, enter your order number for [[online activation]] (the signed license is fetched automatically) — or just start the **14-day free trial**, no sign-up required.",
      },
    },
    {
      title: { zh: "验证安装", en: "Verify the install" },
      time: { zh: "约 5–10 分钟", en: "~5–10 min" },
      detail: {
        zh: "启动器点 **启动全部**，等待各服务就绪灯变绿；点 **一键体检** 全绿即安装成功。再做个 10 分钟[[冒烟测试]]：控制台上传一段 **10 秒清晰人声** → 克隆 → 试听，**出声即一切正常**。",
        en: "Click **Start All** in the launcher and wait for the service lights to turn green; run the **health check** — all green means you're done. Then a quick [[smoke test]]: upload a **clear 10-second voice sample** in the console → clone → play. **If you hear it, everything works.**",
      },
    },
  ],
  faqs: [
    {
      q: { zh: "SmartScreen / 杀毒软件拦截安装包怎么办？", en: "SmartScreen or antivirus blocks the installer?" },
      a: {
        zh: "点 **更多信息 → 仍要运行**，或在杀毒软件中将安装包加入信任。[[SmartScreen]] 对新发布程序的提醒是例行行为，不是病毒报警；所有发布件的 [[SHA-256]] 校验值都公布在本页，可先校验再放行。**下载来源请认准官网。**",
        en: "Click **More info → Run anyway**, or whitelist the installer in your antivirus. The [[SmartScreen]] prompt is routine for recently published apps — not a virus alert. Every release's [[SHA-256]] is published here so you can verify first. **Only download from the official site.**",
      },
    },
    {
      q: { zh: "组件下载很慢或中断了怎么办？", en: "Component download is slow or got interrupted?" },
      a: {
        zh: "下载支持[[断点续传]]：重新打开 AvatarHub 会自动从断点继续，**不会重下已完成的部分**。弱网环境建议换到更稳的网络后继续。",
        en: "Downloads are [[resumable download]]: reopen AvatarHub and it continues from where it stopped — **finished parts are never re-downloaded**. On a weak network, switch to a steadier connection and resume.",
      },
    },
    {
      q: { zh: "我的显卡不够 8 GB，还能用吗？", en: "My GPU has less than 8 GB VRAM — can I still use it?" },
      a: {
        zh: "可以。[[首启向导]]按你这台机器实测推荐[[档位]]：**仅声音克隆 4 GB 起**即可；实时换脸 / 数字人直播建议 **8 GB+**；同传全家桶建议 **24 GB**。控制台 **设置 → 硬件档位** 随时可查每个功能能否开启。",
        en: "Yes. The [[first-run wizard]] recommends a [[capability tier]] from your actual hardware: **voice cloning runs from 4 GB**; live face swap / digital-human streaming wants **8 GB+**; the full interpreting suite wants **24 GB**. **Settings → Hardware tier** shows what each feature needs.",
      },
    },
    {
      q: { zh: "装完打不开控制台 / 某个服务一直未就绪？", en: "Console won't open / a service never becomes ready?" },
      a: {
        zh: "先用启动器 **一键体检** 定位问题；多数是[[显存]]不足或模型还没加载完，稍等或重启服务即可。仍未解决就点[[一键诊断包]]，把 **6 位诊断码**发给客服即可远程定位，无需手动发文件。",
        en: "Run the launcher's **health check** first; most cases are low [[VRAM]] or models still loading — wait or restart the service. If it persists, use the [[diagnostic pack]] and give support the **6-digit code** — no file juggling needed.",
      },
    },
    {
      q: { zh: "可以装到 D 盘或换组件存储位置吗？", en: "Can I install to another drive or move component storage?" },
      a: {
        zh: "可以。安装时可自选安装目录；AI 组件与模型的存储位置也可在 **设置** 中调整，建议放在剩余空间充足的 **SSD** 上。",
        en: "Yes. Pick any folder during install; the AI component / model storage location can also be changed in **Settings**. An **SSD** with plenty of free space is recommended.",
      },
    },
    {
      q: { zh: "完全不想自己动手怎么办？", en: "Don't want to set anything up yourself?" },
      a: {
        zh: "提供 **99 USD 远程代部署**服务：约好时间远程上机，装好即用。联系 Telegram 客服预约即可；也可以先点 **AI 协助安装** 让 AI 一步步带你装。",
        en: "We offer a **99 USD remote installation** service — book a session and we set everything up for you. Contact us on Telegram, or click **AI install assistant** first and let the AI walk you through it.",
      },
    },
  ],
};

/** 手册内容块 */
export type ManualBlock =
  | { type: "p"; text: string }
  | { type: "bullets"; items: string[] }
  | { type: "steps"; items: { title: string; detail?: string }[] }
  | { type: "table"; headers: string[]; rows: string[][] }
  | { type: "tip"; text: string };

/** 手册章节（id 用于目录锚点，zh/en 两份内容结构一致） */
export interface ManualSectionData {
  id: string;
  title: string;
  blocks: ManualBlock[];
}

/** 术语表章节：与术语气泡共用 GLOSSARY 单一数据源，同时服务打印版与 SEO 长尾 */
function glossarySection(lang: ManualLang): ManualSectionData {
  return {
    id: "glossary",
    title: lang === "zh" ? "术语表" : "Glossary",
    blocks: [
      {
        type: "p",
        text:
          lang === "zh"
            ? "教程中带虚线下划线的术语，悬停或点击即可查看解释；此处汇总全部术语，方便打印留档与快速查阅。"
            : "Terms with a dotted underline in the guides show an explanation on hover or tap; this chapter collects them all for printing and quick reference.",
      },
      {
        type: "table",
        headers: lang === "zh" ? ["术语", "含义"] : ["Term", "Meaning"],
        rows: GLOSSARY.map((g) => [g.term[lang], g.def[lang]]),
      },
    ],
  };
}

const MANUAL_ZH: ManualSectionData[] = [
  {
    id: "overview",
    title: "产品概览",
    blocks: [
      {
        type: "p",
        text: "AvatarHub 是本地部署的实时数字人引擎：声音克隆、实时换脸、数字人直播、克隆音同传四大能力装在你自己的电脑或服务器上，数据不出机房。核心链路：说话 → 语音识别 → 大模型 → 克隆音合成 → 实时口型 / 换脸 → 直播推流或视频通话。",
      },
      {
        type: "bullets",
        items: [
          "声音克隆：10 秒样本零样本克隆，多语种真实合成。",
          "实时换脸：直播 / 视频通话实时出镜，支持 OBS 虚拟摄像头。",
          "数字人直播：克隆音 + 实时口型的数字人，支持互动问答。",
          "克隆音同传：说中文，对方听到你自己音色的外语。",
        ],
      },
      { type: "tip", text: "本手册面向使用官网安装包的用户。多机集群、二次开发等进阶话题请联系客服获取部署指南。" },
    ],
  },
  {
    id: "requirements",
    title: "系统要求",
    blocks: [
      {
        type: "table",
        headers: ["项目", "最低", "推荐"],
        rows: [
          ["操作系统", "Windows 10 (x64)", "Windows 11 (x64)"],
          ["显卡", "NVIDIA 4 GB 显存（仅声音克隆）", "NVIDIA 8 GB+（实时换脸 / 数字人）；24 GB（同传全家桶）"],
          ["内存", "16 GB", "32 GB"],
          ["磁盘", "SSD 预留 40 GB", "SSD 预留 80 GB"],
          ["网络", "首次安装需联网下载组件", "50 Mbps+（组件下载更快）"],
        ],
      },
      {
        type: "p",
        text: "macOS 12+（Apple Silicon / Intel）版本为轻量控制台 / 远程接入：换脸、数字人等重推理仍需 Windows / 服务器 N 卡，Mac 端连接远程引擎使用，即将上线。",
      },
      { type: "tip", text: "拿不准自己的机器能跑哪些功能？装好后首启向导会按实测硬件告诉你每个功能能不能开，也可以问右下角 AI 客服。" },
    ],
  },
  {
    id: "install",
    title: "下载与安装",
    blocks: [
      {
        type: "steps",
        items: INSTALL_GUIDE.steps.map((s) => ({
          title: s.title.zh + (s.time ? `（${s.time.zh}）` : ""),
          detail:
            s.detail.zh +
            (s.sub ? " " + s.sub.zh.join(" ") : "") +
            (s.warn ? " 常见卡点：" + s.warn.zh : ""),
        })),
      },
      { type: "tip", text: "安装遇到问题？先看本手册「故障排查」一章；也可以在官网下载页点 **AI 协助安装**，AI 客服会一步步带你排查。" },
    ],
  },
  {
    id: "activation",
    title: "激活与试用",
    blocks: [
      {
        type: "bullets",
        items: [
          "14 天免费试用：安装后无需任何操作即可开始试用全部已下载功能。",
          "在线激活：购买后在「设置 → 授权」输入订单号，自动取回已签授权，立即生效。",
          "授权与硬件绑定：更换整机请提前联系客服迁移授权。",
        ],
      },
      { type: "p", text: "查看授权状态：控制台「设置 → 授权」页展示当前版本、授权类型与到期时间。" },
    ],
  },
  {
    id: "voice-clone",
    title: "快速上手 · 克隆声音并对话",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "上传声音样本", detail: "控制台 → **角色/声音** → 上传一段约 **10 秒的清晰人声**（少杂音、别太短）。" },
          { title: "克隆并试听", detail: "点 **克隆** → **试听**，满意后 **激活** 该角色。" },
          { title: "开始对话", detail: "进入对话页（电脑）或手机页（**同一 WiFi** 下手机访问），打字或按住麦克风说话，数字人用克隆音 + 口型实时回应，**支持中途打断**。" },
        ],
      },
      { type: "tip", text: "手机端第一次按麦克风会弹「允许使用麦克风」，请点 **允许**；误点禁止后，在浏览器地址栏 🔒 → **网站设置 → 麦克风 → 允许**，然后刷新。" },
    ],
  },
  {
    id: "live-swap",
    title: "直播 / 视频通话换脸出镜",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "装虚拟设备", detail: "安装 **OBS Studio**（[[OBS 虚拟摄像头]]）与 **VB-Cable**（[[VB-Cable 虚拟声卡]]），**只需装一次**。" },
          { title: "准备出镜角色", detail: "控制台新建 / **激活** 一个出镜角色（上传要变成的人脸）。" },
          { title: "设备体检并开播", detail: "开播页点 **设备体检**，麦克风 / 摄像头 / 虚拟声卡 **三盏灯全绿** 后点 **一键开播**。" },
          { title: "在目标软件选择虚拟设备", detail: "微信 / 抖音 / Zoom 里把摄像头选成 **OBS Virtual Camera**，麦克风选成 **CABLE Output**。" },
        ],
      },
      { type: "tip", text: "手机竖屏视频通话请把画面比例选为 **竖屏 720×1280**；没有物理摄像头可以用手机当摄像头（见下一章）。" },
    ],
  },
  {
    id: "interpreting",
    title: "手机同传（说中文 · 对方听外语）",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "电脑启动同传服务", detail: "启动器 **启动全部**，确认 **实时同传** 服务运行中。" },
          { title: "手机扫码连接", detail: "电脑打开扫码页会显示两个二维码，做同传扫 **【① 做同传】**；手机需与电脑连 **同一 WiFi**。" },
          { title: "信任本机证书", detail: "手机提示「连接不是私密」**属正常**（本机自签证书）：点 **高级 / 显示详情 → 继续前往**。" },
          { title: "一键准备", detail: "点 **🚀 一键准备** → 允许麦克风，顶部引导条 **变绿即可说话**；需要出镜再点 **📷 开摄像头**。" },
        ],
      },
      { type: "tip", text: "手机页顶部有一条会自我判断的「下一步」引导条：该干嘛它会告诉你；点错「禁止」它会给出对应浏览器的恢复步骤。延迟大时改用 **5GHz WiFi** 或开 **低延迟模式**。" },
    ],
  },
  {
    id: "update",
    title: "软件更新与回滚",
    blocks: [
      {
        type: "bullets",
        items: [
          "产品内自更新：发现新版本时，启动器版本号处会亮起 **v当前 → v新版**，点击一键升级：下载 → 自动安装 → 自动重启，约 **1–3 分钟**。",
          "**数据全保留**：升级只更换控制台程序（几十 MB），已下载的 AI 组件与角色数据全部保留。",
          "安全校验：更新清单带 [[Ed25519 签名]]，篡改包一律拒绝安装。",
          "**一键回滚**：新版本不满意可在「软件更新」里回滚到上一版本，同样保留数据。",
          "直播避让：直播 / 同传进行中**不会应用更新**，结束后自动继续。",
        ],
      },
      { type: "p", text: "每个版本的具体更新内容见官网下载页「版本更新」版块。" },
    ],
  },
  {
    id: "troubleshooting",
    title: "故障排查",
    blocks: [
      {
        type: "table",
        headers: ["现象", "处理"],
        rows: [
          ["控制台打不开", "启动器「一键体检」定位；重启服务；页面硬刷新 Ctrl+F5"],
          ["某服务红灯 / 未就绪", "多为显存不足或模型没加载完：稍等或重启该服务；关闭不用的扩展功能"],
          ["显存爆（OOM）", "只跑核心功能（关扩展）；高清口型最吃显存，可降档"],
          ["手机连不上", "手机与电脑必须同一 WiFi；防火墙放行提示按引导允许"],
          ["「连接不是私密」提示", "正常（本机自签证书），点高级 → 继续前往即可"],
          ["麦克风开不了", "浏览器地址栏 🔒 → 网站设置 → 麦克风 → 允许 → 刷新"],
          ["同传没字幕", "确认「实时同传」服务在运行（启动器绿灯）"],
          ["换脸显示原图", "确认已激活出镜角色且换脸服务在跑"],
        ],
      },
      { type: "tip", text: "解决不了？点[[一键诊断包]]（约 3–10 秒自动收集直传客服），把 **6 位诊断码**发给客服即可远程定位——无需手动打包发文件。" },
    ],
  },
  glossarySection("zh"),
  {
    id: "help",
    title: "获取帮助",
    blocks: [
      {
        type: "bullets",
        items: [
          "AI 客服：官网右下角对话气泡，**7×24 秒回**，安装 / 使用 / 报价都能答。",
          "Telegram 人工客服：**@WJKJ2026**（工作时间内 5 分钟响应）。",
          "远程代部署：**99 USD** 预约远程上机，装好即用。",
          "诊断码：客户端[[一键诊断包]]生成 **6 位码**，报码即可远程定位问题。",
        ],
      },
    ],
  },
];

const MANUAL_EN: ManualSectionData[] = [
  {
    id: "overview",
    title: "Product overview",
    blocks: [
      {
        type: "p",
        text: "AvatarHub is a locally deployed real-time digital human engine: voice cloning, live face swap, digital-human streaming and cloned-voice interpreting run on your own PC or server — data never leaves your premises. Core pipeline: speech → STT → LLM → cloned-voice TTS → real-time lip sync / face swap → live stream or video call.",
      },
      {
        type: "bullets",
        items: [
          "Voice cloning: zero-shot cloning from a 10-second sample, realistic multi-language synthesis.",
          "Live face swap: real-time on-camera presence for streams / video calls, OBS virtual camera supported.",
          "Digital-human streaming: cloned voice + real-time lip sync with interactive Q&A.",
          "Cloned-voice interpreting: speak Chinese, your audience hears a foreign language in your own voice.",
        ],
      },
      { type: "tip", text: "This manual is for users of the official installer. For multi-machine clusters or custom development, contact support for the deployment guide." },
    ],
  },
  {
    id: "requirements",
    title: "System requirements",
    blocks: [
      {
        type: "table",
        headers: ["Item", "Minimum", "Recommended"],
        rows: [
          ["OS", "Windows 10 (x64)", "Windows 11 (x64)"],
          ["GPU", "NVIDIA 4 GB VRAM (voice cloning only)", "NVIDIA 8 GB+ (live swap / digital human); 24 GB (full interpreting suite)"],
          ["RAM", "16 GB", "32 GB"],
          ["Disk", "40 GB free on SSD", "80 GB free on SSD"],
          ["Network", "Internet required for first-time component download", "50 Mbps+ for faster downloads"],
        ],
      },
      {
        type: "p",
        text: "The macOS 12+ (Apple Silicon / Intel) build is a lightweight console / remote client: heavy inference (face swap, digital human) still runs on a Windows / server NVIDIA GPU, with the Mac connecting remotely. Coming soon.",
      },
      { type: "tip", text: "Not sure what your machine can run? The first-run wizard benchmarks your hardware and tells you exactly which features are available — or ask the AI assistant in the corner." },
    ],
  },
  {
    id: "install",
    title: "Download & install",
    blocks: [
      {
        type: "steps",
        items: INSTALL_GUIDE.steps.map((s) => ({
          title: s.title.en + (s.time ? ` (${s.time.en})` : ""),
          detail:
            s.detail.en +
            (s.sub ? " " + s.sub.en.join(" ") : "") +
            (s.warn ? " Common snag: " + s.warn.en : ""),
        })),
      },
      { type: "tip", text: "Stuck during install? See the Troubleshooting chapter, or click **AI install assistant** on the download page and let the AI walk you through it." },
    ],
  },
  {
    id: "activation",
    title: "Activation & trial",
    blocks: [
      {
        type: "bullets",
        items: [
          "14-day free trial: starts automatically after install — no steps needed.",
          "Online activation: after purchase, enter your order number in Settings → License; the signed license is fetched and applied instantly.",
          "Licenses are hardware-bound: contact support before moving to a new machine.",
        ],
      },
      { type: "p", text: "Check license status any time in Settings → License: current version, license type and expiry." },
    ],
  },
  {
    id: "voice-clone",
    title: "Quick start · clone a voice and talk",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "Upload a voice sample", detail: "Console → **Characters/Voices** → upload a **clear ~10-second** voice clip (minimal noise, not too short)." },
          { title: "Clone and preview", detail: "Click **Clone** → **Preview**; happy with it? **Activate** the character." },
          { title: "Start talking", detail: "Open the chat page (PC) or the phone page (**same-WiFi** phone), type or hold the mic — the digital human replies in the cloned voice with lip sync, and you can **barge in mid-sentence**." },
        ],
      },
      { type: "tip", text: "The first mic press on mobile asks for permission — tap **Allow**. If you tapped Block, use the address-bar 🔒 → **Site settings → Microphone → Allow**, then refresh." },
    ],
  },
  {
    id: "live-swap",
    title: "Live face swap for streams / video calls",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "Install virtual devices", detail: "Install **OBS Studio** ([[OBS Virtual Camera]]) and **VB-Cable** ([[VB-Cable]]) — **one-time setup**." },
          { title: "Prepare an on-camera character", detail: "Create / **activate** a character in the console (upload the target face)." },
          { title: "Device check, then go live", detail: "On the streaming page run the **Device Check**; once mic / camera / virtual audio are **all green**, click **Go Live**." },
          { title: "Select virtual devices in the target app", detail: "In WeChat / TikTok / Zoom, set the camera to **OBS Virtual Camera** and the microphone to **CABLE Output**." },
        ],
      },
      { type: "tip", text: "For portrait video calls choose the **720×1280 portrait** aspect. No physical camera? Use your phone as the camera (next chapter)." },
    ],
  },
  {
    id: "interpreting",
    title: "Phone interpreting (speak Chinese, they hear your language)",
    blocks: [
      {
        type: "steps",
        items: [
          { title: "Start interpreting services on the PC", detail: "Click **Start All** in the launcher and confirm the **live-interpreting** service is running." },
          { title: "Scan the QR code", detail: "The PC shows two QR codes — scan **① Interpreting**. Phone and PC must be on the **same WiFi**." },
          { title: "Trust the local certificate", detail: "The \"connection is not private\" warning is **expected** (self-signed local cert): tap **Advanced / Details → Proceed**." },
          { title: "One-tap setup", detail: "Tap **🚀 Prepare** → allow the microphone; when the guide bar **turns green**, start speaking. Tap **📷** to add camera if you need to be on screen." },
        ],
      },
      { type: "tip", text: "The guide bar at the top of the phone page tells you the next step at every stage, including how to recover if you tapped Block. High latency? Switch to **5 GHz WiFi** or enable **low-latency mode**." },
    ],
  },
  {
    id: "update",
    title: "Updates & rollback",
    blocks: [
      {
        type: "bullets",
        items: [
          "In-app self-update: when a new version is available the launcher shows **v-current → v-new** — one click downloads, installs and restarts in about **1–3 minutes**.",
          "**Your data stays**: updates replace only the console program (tens of MB); downloaded AI components and characters are untouched.",
          "Verified updates: release manifests carry an [[Ed25519 signature]]; tampered packages are rejected.",
          "**One-click rollback**: not happy with a version? Roll back to the previous one from Software Update — data preserved.",
          "Stream-safe: updates **never apply** while a live stream / interpreting session is running.",
        ],
      },
      { type: "p", text: "Per-version changes are listed in the Release notes section of the download page." },
    ],
  },
  {
    id: "troubleshooting",
    title: "Troubleshooting",
    blocks: [
      {
        type: "table",
        headers: ["Symptom", "Fix"],
        rows: [
          ["Console won't open", "Run the launcher health check; restart services; hard-refresh with Ctrl+F5"],
          ["A service stays red / not ready", "Usually low VRAM or models still loading: wait or restart it; disable unused extensions"],
          ["Out of GPU memory (OOM)", "Run core features only (disable extras); HD lip sync is the heaviest — step down a tier"],
          ["Phone can't connect", "Phone and PC must share the same WiFi; allow the firewall prompt"],
          ["\"Connection is not private\"", "Expected (self-signed local cert): Advanced → Proceed"],
          ["Microphone won't enable", "Address-bar 🔒 → Site settings → Microphone → Allow → refresh"],
          ["No interpreting subtitles", "Make sure the live-interpreting service is running (green in the launcher)"],
          ["Face swap shows the original face", "Confirm a character is activated and the face-swap service is running"],
        ],
      },
      { type: "tip", text: "Still stuck? Use the [[diagnostic pack]] (~3–10 s, auto-uploaded) and give support the **6-digit code** — they can pinpoint the issue remotely, no manual file sending." },
    ],
  },
  glossarySection("en"),
  {
    id: "help",
    title: "Getting help",
    blocks: [
      {
        type: "bullets",
        items: [
          "AI assistant: the chat bubble in the corner, **24/7 instant answers** on installing, usage and pricing.",
          "Human support on Telegram: **@WJKJ2026** (about 5-minute response during working hours).",
          "Remote installation: **99 USD** — book a session and we set everything up.",
          "Diagnostic code: generate a **6-digit code** with the in-app [[diagnostic pack]] and share it with support.",
        ],
      },
    ],
  },
];

export const MANUAL_SECTIONS: Record<ManualLang, ManualSectionData[]> = {
  zh: MANUAL_ZH,
  en: MANUAL_EN,
};
