/**
 * AvatarHub 客户端版本更新记录（官网下载页「版本更新」版块的单一数据源）。
 *
 * 内容依据 engines/avatarhub 的开发记录整理（升级开发路线图_v3 / 远程更新与日志上报方案 /
 * 打包与分发方案等）；日期为依据记录推定的发布日，正式对外前可在此校对修订。
 * 跳过的版本号（1.0.2 / 1.0.5）为内部构建，未对外发布，故不列出。
 *
 * 发布新版本时：在数组头部插入一条 ReleaseNote，并同步更新 DownloadSection 的
 * FALLBACK 版本与下载站 release_manifest.json。
 */

/** 版本条目的主要变更类型（决定 UI 徽章配色与文案） */
export type ReleaseTag = "feature" | "fix" | "improve" | "security";

/** 单个版本的更新记录（zh/en 双语，与站点语言切换联动） */
export interface ReleaseNote {
  /** 版本号，如 "1.0.11"（不带 v 前缀） */
  version: string;
  /** 推定发布日期 YYYY-MM-DD */
  date: string;
  /** 一句话主题 */
  title: { zh: string; en: string };
  /** 变更类型标签（第一个为主标签） */
  tags: ReleaseTag[];
  /** 更新要点列表 */
  highlights: { zh: string[]; en: string[] };
}

/** 按版本号从新到旧排列 */
export const RELEASE_NOTES: ReleaseNote[] = [
  {
    version: "1.0.11",
    date: "2026-07-13",
    title: { zh: "当前最新版 · 安装与更新体验打磨", en: "Latest · polished install & update experience" },
    tags: ["improve", "fix"],
    highlights: {
      zh: [
        "安装器与组件下载链路整体优化，弱网环境下载更稳。",
        "首次启动向导的硬件档位检测更准确，推荐配置更贴合实际显卡能力。",
        "若干稳定性与性能修复，长时间运行更可靠。",
      ],
      en: [
        "Overall polish of the installer and component-download pipeline; steadier downloads on weak networks.",
        "More accurate GPU-tier detection in the first-run wizard, with recommendations that better match your hardware.",
        "Assorted stability and performance fixes for long-running sessions.",
      ],
    },
  },
  {
    version: "1.0.10",
    date: "2026-07-11",
    title: { zh: "一键诊断包 · 报 6 位码即可远程定位", en: "One-click diagnostics with 6-digit code" },
    tags: ["feature", "improve"],
    highlights: {
      zh: [
        "新增「一键诊断包」：后台自动收集诊断信息（约 3–10 秒）直传客服，换取 6 位诊断码——联系客服报码即可，无需手动发文件。",
        "升级结果匿名回执：升级成功率有了真实数据，灰度放量从「凭感觉」变「看数据」。",
        "更新弹窗与下载进度体验优化。",
      ],
      en: [
        "New one-click diagnostic pack: collects logs in the background (~3–10 s) and uploads them for a 6-digit code — just tell support the code, no file juggling.",
        "Anonymous update receipts: real success-rate data now drives staged rollouts.",
        "Nicer update dialog and download-progress experience.",
      ],
    },
  },
  {
    version: "1.0.9",
    date: "2026-07-09",
    title: { zh: "在线激活 · 输订单号即取回授权", en: "Online activation by order number" },
    tags: ["feature"],
    highlights: {
      zh: [
        "在线激活上线：客户端「设置 → 授权」输入订单号即可自动取回已签授权，告别手动粘贴一长串激活码。",
        "授权流程与官网订单系统打通，下单后开箱即激活。",
      ],
      en: [
        "Online activation: enter your order number in Settings → License and the signed license is fetched automatically — no more pasting long base64 strings.",
        "License flow is now wired to the website order system: buy, open, activate.",
      ],
    },
  },
  {
    version: "1.0.8",
    date: "2026-07-07",
    title: { zh: "产品内自更新 · 一键升级与回滚", en: "In-app self-update with one-click rollback" },
    tags: ["feature", "security"],
    highlights: {
      zh: [
        "产品内自更新上线：发现新版本后一键升级，下载 → 自动安装 → 自动重启约 1–3 分钟完成，已下载的 AI 组件与角色数据全部保留。",
        "更新清单启用 Ed25519 数字签名校验，杜绝被篡改的安装包。",
        "增量热修与一键回滚：应用前语法校验拦截坏更新，异常时自动回退上一版本。",
        "直播 / 同传进行中自动避让更新，绝不中断正在进行的直播。",
        "修复启动器「打开控制台」长期显示未就绪的问题。",
      ],
      en: [
        "In-app self-update: one click to download, install and restart (about 1–3 minutes); downloaded AI components and character data are fully preserved.",
        "Release manifests are now Ed25519-signed and verified — tampered packages are rejected.",
        "Incremental hotfixes with one-click rollback: syntax-gated before applying, auto-revert if the app fails to start.",
        "Updates automatically defer while a live stream / interpreting session is running.",
        "Fixed the launcher showing 'console not ready' indefinitely.",
      ],
    },
  },
  {
    version: "1.0.7",
    date: "2026-07-05",
    title: { zh: "授权状态持久化修复", en: "License persistence fix" },
    tags: ["fix"],
    highlights: {
      zh: ["修复在启动器中激活的授权重启后失效的问题，激活状态可靠持久化。"],
      en: ["Fixed licenses activated in the launcher being lost after a restart; activation state now persists reliably."],
    },
  },
  {
    version: "1.0.6",
    date: "2026-07-03",
    title: { zh: "官网正式发布版 · 装机链路全流程打磨", en: "Public release · end-to-end install QA" },
    tags: ["improve", "fix"],
    highlights: {
      zh: [
        "两台标准机全流程装机 QA：「安装 → 组件下载 → 运行」链路逐项磨顺，开箱体验显著提升。",
        "修复界面底部版本号误显示组件清单版本的问题（曾让用户误以为装了旧程序）。",
        "组件下载稳定性优化。",
      ],
      en: [
        "Full install QA on two reference machines: the install → component download → run pipeline is polished end to end.",
        "Fixed the footer showing the component-manifest version instead of the app version (which made users think they had an old build).",
        "More reliable component downloads.",
      ],
    },
  },
  {
    version: "1.0.4",
    date: "2026-06-28",
    title: { zh: "首帧预热 · 批量部署验证", en: "Warm-up on boot · silent install verified" },
    tags: ["fix", "improve"],
    highlights: {
      zh: [
        "修复安装包缺失 tools 工具目录的问题（40 个运维脚本随包落位）。",
        "安装包内置预热人脸素材：新装机首次换脸从约 6 秒冷启动缩短为开机预热 3.4 秒完成。",
        "静默安装（/VERYSILENT）与批量部署流程验证通过，适配网吧 / 机房批量装机。",
        "修复 HTTPS 组件下载自检。",
      ],
      en: [
        "Fixed the installer missing the tools directory (40 ops scripts now ship in the package).",
        "Bundled a warm-up face asset: first face swap on a fresh machine drops from ~6 s cold start to a 3.4 s boot-time warm-up.",
        "Silent install (/VERYSILENT) and fleet deployment verified for batch provisioning.",
        "Fixed the HTTPS self-check for component downloads.",
      ],
    },
  },
  {
    version: "1.0.3",
    date: "2026-06-26",
    title: { zh: "新装机环境误判修复", en: "Fresh-machine environment detection fix" },
    tags: ["fix"],
    highlights: {
      zh: [
        "修复新装机上运行环境「假就位」误判：此前 conda 未安装也可能被判为已就绪，导致首启向导被跳过、组件未下载。",
        "修复启动器可能误停正在运行的生产服务的问题。",
      ],
      en: [
        "Fixed false-positive environment detection on fresh machines: a missing conda could be treated as ready, silently skipping the first-run wizard and component downloads.",
        "Fixed the launcher potentially killing production services that were already running.",
      ],
    },
  },
  {
    version: "1.0.1",
    date: "2026-06-22",
    title: { zh: "组件清单增补 · 发布清单上线", en: "Component manifest additions" },
    tags: ["improve"],
    highlights: {
      zh: [
        "组件清单增补人脸检测模型（buffalo_l），换脸链路开箱即用。",
        "官网发布清单（SHA-256 校验值）正式上线，下载可验真。",
        "安装包体积优化至 41 MB。",
      ],
      en: [
        "Added the face-detection model (buffalo_l) to the component manifest — face swap works out of the box.",
        "Published the release manifest with SHA-256 checksums for verifiable downloads.",
        "Installer size trimmed to 41 MB.",
      ],
    },
  },
  {
    version: "1.0.0",
    date: "2026-06-19",
    title: { zh: "首个正式版", en: "First public release" },
    tags: ["feature"],
    highlights: {
      zh: [
        "薄核心安装包（约 40 MB）：按用户安装、免管理员权限，AI 组件与模型按需下载。",
        "首次启动向导：自动检测显卡并推荐功能档位，组件下载全程 SHA-256 校验、断点续传。",
        "图形启动器：各服务就绪状态一目了然，内置一键体检。",
        "简体中文安装界面；14 天免费试用开箱即用。",
      ],
      en: [
        "Thin-core installer (~40 MB): per-user install, no admin rights; AI components and models download on demand.",
        "First-run wizard: detects your GPU, recommends a capability tier; all component downloads are SHA-256 verified and resumable.",
        "Graphical launcher with live service-readiness lights and a built-in health check.",
        "Simplified-Chinese installer UI; 14-day free trial out of the box.",
      ],
    },
  },
];

/** 最新对外发布版本号（下载卡片兜底与 JSON-LD softwareVersion 共用） */
export const LATEST_VERSION = RELEASE_NOTES[0].version;
