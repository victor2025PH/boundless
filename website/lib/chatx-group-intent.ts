/**
 * 群里没 @ bot 的消息：判断要不要主动答。
 *   answer = 明确在问产品相关问题 → 直接交给 AI（AI 仍可回 SKIP）
 *   maybe  = 像是提问但没提到产品 → 交给 AI 判断，不相关回 SKIP
 *   skip   = 闲聊 / 招呼 / 表情 / 成员之间对话 / 命令 / 转发 → 不说话
 */
export type GroupIntentKind = "answer" | "maybe" | "skip";
export type GroupIntent = { kind: GroupIntentKind; reason: string };
export type GroupIntentCtx = { replyToHuman?: boolean; forwarded?: boolean };

const QUESTION_RE =
  /[?？]|吗|怎么|怎样|咋|如何|为什么|为啥|能不能|能否|可不可以|可以.{0,8}[吗么]|有没有|有无|哪里|哪儿|在哪|哪个|多少|几[个天号点]|是否|支不支持|支持.{0,8}[吗么]|求助|请问|求教|教程|\b(how|what|why|where|when|which|can i|could|does|is there|any way|help)\b/i;
const PRODUCT_RE =
  /chatx|智聊|小界|机器人|下载|安装|更新|升级|版本|激活|授权|卡密|机器码|注册|登录|登陆|账号|会员|价格|多少钱|收费|付费|套餐|试用|续费|退款|翻译|自动回复|自动回|语音|音色|克隆|闪退|报错|打不开|多开|群发|客服|电脑版|手机版|安卓|苹果|\b(ai|bot|windows|win|mac|macos|android|ios|telegram|tg|whatsapp|line|apk|exe|vip|license|download|install|price|pricing|trial|translate|translation)\b/i;
const PROBLEM_RE =
  /报错|闪退|打不开|用不了|不能用|没法用|失败|卡住|卡死|没反应|登不上|收不到|不显示|不工作|\b(bug|error|crash|failed|not working|doesn'?t work)\b/i;
const CHITCHAT_RE =
  /^(哈+|呵+|嘿+|嗯+|哦+|噢+|啊+|好+的?|好吧|行+|可以|收到|了解|明白|谢谢.*|感谢.*|多谢.*|辛苦了?|早|早安|早上好|中午好|下午好|晚上好|晚安|大家好|你好|您好|在吗|在不在|有人吗|欢迎.*|牛+|厉害|666+|888+|\+1|赞|顶|哈哈.*|lol|lmao|haha+|ok+|okay|yes|no|yep|nope|thanks?.*|thank you.*|thx|ty|hi+|hello|hey|gm|gn|good (morning|night|evening))[!！。.~～\s]*$/i;

function core(text: string): string {
  return text
    .replace(/@\w+/g, " ")
    .replace(/(https?:\/\/|t\.me\/|www\.)\S+/gi, " ")
    .replace(/[\p{Extended_Pictographic}\u200d\ufe0f]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

const meaningfulLen = (s: string) => s.replace(/[\s\p{P}\p{S}]/gu, "").length;

export function classifyGroupText(text: string, ctx: GroupIntentCtx = {}): GroupIntent {
  const raw = text.trim();
  if (!raw) return { kind: "skip", reason: "empty" };
  if (raw.startsWith("/")) return { kind: "skip", reason: "command" };
  if (ctx.forwarded) return { kind: "skip", reason: "forwarded" };
  const t = core(raw);
  const len = meaningfulLen(t);
  if (len < 3) return { kind: "skip", reason: "short" };
  if (CHITCHAT_RE.test(t)) return { kind: "skip", reason: "chitchat" };
  const q = QUESTION_RE.test(t);
  const p = PRODUCT_RE.test(t);
  const e = PROBLEM_RE.test(t);
  if (ctx.replyToHuman) return p && (q || e) ? { kind: "maybe", reason: "reply_product" } : { kind: "skip", reason: "conversation" };
  if (p && (q || e)) return { kind: "answer", reason: e ? "problem" : "question" };
  if (q && len >= 5) return { kind: "maybe", reason: "question" };
  return { kind: "skip", reason: "chat" };
}

/** AI 判断「不该插话」时的约定输出。 */
export const isSkipReply = (s: string | null | undefined) => !s || /^\W*SKIP\W*$/i.test(s.trim()) || /^\W*SKIP\b/i.test(s.trim());
