/** 智聊 ChatX 视频教程合集单一真相（2026-09-17）：/chatx/tutorials 播放列表页、/videos 入口卡、
 *  /download/chatx 嵌入的安装集、JSON-LD ItemList 共用一份数据。
 *
 *  为什么不进 feed-store（/videos 日更流）：那是「按日期倒序的扁平流」，12 集课程需要固定顺序、
 *  集号、时长与「真机录屏」标注，且上架接口默认会连发 12 条 Telegram 帖并打「AI 概念演示」徽章。
 *  与 lib/film.ts 同款做法：typed 静态数据随仓版本化，页面可静态渲染。
 *
 *  视频本体：nginx /media/ 直出（/var/www/media/tutorials/chatx/，部署不覆盖）；海报随仓部署（public/）。
 *  产物由 deliverables/video_songs_20260916/publish_web.py 出厂（脱敏模糊 + 裁尾卡 + H.264 High@L4.1 +
 *  faststart + 双验），此处字段抄自其 manifest.json。视频为中文说唱 + 烧入卡拉 OK 字幕，英文版待翻制。 */

export type TutorialLang = "zh" | "en";

export interface TutorialEpisode {
  /** 课程集号 E1..E12；T1 为第 0 集「安装」 */
  id: string;
  /** 0 = 安装集，1..12 正课 */
  ep: number;
  slug: string;
  title: { zh: string; en: string };
  desc: { zh: string; en: string };
  /** 对应客户端功能名（列表副题） */
  feature: { zh: string; en: string };
  src: string;
  poster: string;
  durationSec: number;
}

const MEDIA = "/media/tutorials/chatx";
const POSTER = "/videos/tutorials/chatx";

function ep(
  id: string,
  n: number,
  slug: string,
  durationSec: number,
  title: { zh: string; en: string },
  feature: { zh: string; en: string },
  desc: { zh: string; en: string },
): TutorialEpisode {
  const file = n === 0 ? `chatx-t1-${slug}` : `chatx-e${String(n).padStart(2, "0")}-${slug}`;
  return { id, ep: n, slug, title, feature, desc, durationSec, src: `${MEDIA}/${file}.mp4`, poster: `${POSTER}/${file}.jpg` };
}

/** 播放顺序即数组顺序：先装（T1），再 12 集正课。 */
export const CHATX_TUTORIALS: readonly TutorialEpisode[] = [
  ep("T1", 0, "install", 79.3,
    { zh: "三分钟装好智聊", en: "Install ChatX in three minutes" },
    { zh: "安装与首启", en: "Install & first run" },
    { zh: "下载 → SmartScreen「更多信息 → 仍要运行」→ 安装 → 首启向导 → 主界面四个地方。", en: "Download → SmartScreen “More info → Run anyway” → install → first-run wizard → the four places in the main window." }),
  ep("E1", 1, "inbox", 50.2,
    { zh: "一个收件箱，接住所有平台", en: "One inbox for every platform" },
    { zh: "统一收件箱", en: "Unified inbox" },
    { zh: "TG / WhatsApp / LINE / Messenger 的会话都在左栏；账号页签切换工作号，私聊 / 群组 / 未读随手筛。", en: "Telegram, WhatsApp, LINE and Messenger threads all in the left column; switch accounts from the top tabs, filter private / groups / unread." }),
  ep("E2", 2, "channels", 40.6,
    { zh: "扫码接入四个平台", en: "Connect four platforms by QR" },
    { zh: "渠道接入", en: "Channels" },
    { zh: "账号管理 → ＋新增账号 → 扫码 / 授权；顶栏账号坞一号一格，绿灯＝在线。", en: "Accounts → Add → scan or authorise; one slot per account in the top dock, green light = online." }),
  ep("E3", 3, "translate", 63.6,
    { zh: "用客户的母语聊，像老乡一样", en: "Chat in your customer's language" },
    { zh: "拟人互译", en: "Human-like translation" },
    { zh: "入站一键显示译文，出站用中文写、发出去是对方母语；粤语 / 日文 / 韩文进同一收件箱。标准翻译永久免费。", en: "Inbound shows translations in one tap; write in Chinese and it lands in the customer's language. Standard translation stays free forever." }),
  ep("E4", 4, "ai-reply", 40.9,
    { zh: "仅建议、值守中、全自动", en: "Suggest, standby, or fully automatic" },
    { zh: "AI 拟稿与自动回复", en: "AI drafts & auto-reply" },
    { zh: "每个会话选档位：手动 / AI 草稿我审 / 全自动；点【AI 回复】出人设化草稿，随时【接管】变人工。", en: "Pick a mode per thread: manual / AI draft with review / fully automatic. Tap AI Reply for a persona-shaped draft; take over any time." }),
  ep("E5", 5, "persona", 38.4,
    { zh: "造一个有人设的数字员工", en: "Build a digital employee with a persona" },
    { zh: "人设工作室", en: "Persona studio" },
    { zh: "语气、边界、背景一次配好，可从 JSON 导入；试聊通过再在工具箱里绑到会话。", en: "Set tone, boundaries and backstory once (JSON import supported); test-chat, then bind it to a thread from the toolbox." }),
  ep("E6", 6, "knowledge", 41.9,
    { zh: "知识库：问什么答什么", en: "Knowledge base: answer what's asked" },
    { zh: "知识库", en: "Knowledge base" },
    { zh: "条目有标题、触发词、示例回复；回复台输入 / 可直接插入知识与模板。", en: "Entries carry a title, trigger words and sample replies; type / in the composer to insert knowledge or templates." }),
  ep("E7", 7, "voice", 52.6,
    { zh: "用你的声音发语音", en: "Send voice notes in your own voice" },
    { zh: "克隆语音", en: "Voice cloning" },
    { zh: "工具箱 → 语音克隆 / 发送：生成 → 试听 → 发送，所听即所发。", en: "Toolbox → Voice: generate, preview, send. What you hear is what goes out." }),
  ep("E8", 8, "goals", 48,
    { zh: "每天十秒，今日拍", en: "Ten seconds a day: the daily call" },
    { zh: "工作目标与今日拍", en: "Goals & the daily call" },
    { zh: "客户关系 → 工作目标：先设一条目标，再对 AI 的今日动作十秒反馈（采纳 / 让路）。", en: "Relationship → Goals: set one goal, then give the AI a ten-second verdict on today's move (adopt / step aside)." }),
  ep("E9", 9, "care-memory", 53.6,
    { zh: "它记得你，也知道什么时候别吵你", en: "It remembers you — and when to stay quiet" },
    { zh: "主动关怀与记忆", en: "Care schedule & memory" },
    { zh: "关怀日程主动跟进、守安静时段；AI 记忆记住偏好，回答更贴人。", en: "The care schedule follows up proactively and respects quiet hours; AI memory keeps preferences so replies stay personal." }),
  ep("E10", 10, "xiaozhi", 42,
    { zh: "不会用？问小智", en: "Stuck? Ask Xiaozhi" },
    { zh: "小智助手球", en: "Xiaozhi assistant" },
    { zh: "右下角小球：问怎么用、带我去、截图报障。", en: "The bubble in the corner: ask how, get taken there, report a bug with a screenshot." }),
  ep("E11", 11, "guardrails", 40.8,
    { zh: "该转人的它不乱答", en: "Hands off when a human should answer" },
    { zh: "风险与护栏", en: "Risk & guardrails" },
    { zh: "会话上的风险标签、AI 让位转人工；设置里配自动化与风控分级。", en: "Risk tags on threads, AI stepping aside for a human; automation and risk tiers configured in settings." }),
  ep("E12", 12, "billing", 41.6,
    { zh: "免费开始，用多少充多少", en: "Start free, pay as you go" },
    { zh: "充值与额度", en: "Top-up & quota" },
    { zh: "会员中心看额度与档位；购买 / 续费按量充值；标准翻译永久免费，余额用尽也不断线。", en: "See quota and tier in the membership centre; top up as you go; standard translation stays free and you never get cut off." }),
];

export const CHATX_TUTORIALS_PATH = "/chatx/tutorials";

/** 正课集数（不含安装集） */
export const CHATX_TUTORIAL_COUNT = CHATX_TUTORIALS.filter((e) => e.ep > 0).length;

/** 正课合计时长（秒） */
export const CHATX_TUTORIAL_TOTAL_SEC = CHATX_TUTORIALS.filter((e) => e.ep > 0).reduce((s, e) => s + e.durationSec, 0);

export const CHATX_TUTORIALS_OG = `${POSTER}/og.jpg`;
/** 出厂日期（JSON-LD uploadDate） */
export const CHATX_TUTORIALS_UPLOAD_DATE = "2026-09-17";

export function findTutorial(id: string | null | undefined): TutorialEpisode | undefined {
  if (!id) return undefined;
  const key = id.toUpperCase();
  return CHATX_TUTORIALS.find((e) => e.id === key);
}

export function fmtDuration(sec: number): string {
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** ISO 8601 时长（schema.org VideoObject.duration） */
export function isoDuration(sec: number): string {
  const s = Math.round(sec);
  return `PT${Math.floor(s / 60)}M${s % 60}S`;
}

/** 教程 → 下载页深链（utm 归因：t-e01-zh） */
export function tutorialUtm(e: TutorialEpisode, lang: TutorialLang, path = "/download/chatx"): string {
  const base = lang === "en" ? `/en${path}` : path;
  const c = e.ep === 0 ? "t1" : `e${String(e.ep).padStart(2, "0")}`;
  return `${base}?utm_source=site&utm_medium=tutorial&utm_campaign=t-${c}-${lang}`;
}
