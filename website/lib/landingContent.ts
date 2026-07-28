// 四条产品线独立落地页内容（/voice /face /interpreting /fate + /en/*）。
// 设计原则：一页一卖点，媒体证据前置（真实引擎产出），CTA 直达 Telegram。
// 指标口径与 engineContent.ts 保持一致——营销可以强，数字必须真。

export type LandingKey = "voice" | "face" | "interpreting" | "fate";

interface L {
  zh: string;
  en: string;
}

export interface LandingDict {
  slug: string; // zh 路由；en 为 /en + slug
  productLine: L; // kicker 徽章
  seo: {
    title: L;
    description: L;
    keywords: string[];
  };
  hero: {
    title: L;
    accent: L;
    subtitle: L;
    points: L[]; // 3 个带勾要点
  };
  demo: {
    title: L;
    subtitle: L;
    realNote: L; // “真实产出”说明
  };
  caps: { title: L; desc: L; proof: L }[];
  steps: { title: L; desc: L }[];
  faq: { q: L; a: L }[];
  finalCta: {
    title: L;
    desc: L;
  };
}

export const LANDINGS: Record<LandingKey, LandingDict> = {
  voice: {
    slug: "/voice",
    productLine: { zh: "幻声 VoiceX · AI 声音克隆", en: "VoiceX · AI voice cloning" },
    seo: {
      title: {
        zh: "AI 声音克隆 · 十几秒复刻你的音色 | 幻声 VoiceX — 无界科技",
        en: "AI Voice Cloning · Clone any voice in seconds | VoiceX — BOUNDLESS",
      },
      description: {
        zh: "十几秒参考音零样本克隆音色：三引擎自动择优，10 语种同一音色，情感语气自然连贯，48kHz 可商用。本地部署声音不出机房，产出带 C2PA 可验真水印。",
        en: "Zero-shot voice cloning from seconds of audio: tri-engine auto-pick, 10 languages in one voice, natural emotion, commercial-grade 48kHz. Private deployment, C2PA-verifiable output.",
      },
      keywords: ["AI声音克隆", "声音克隆软件", "voice cloning", "AI配音", "克隆音色", "TTS", "语音合成", "多语种配音"],
    },
    hero: {
      title: { zh: "十几秒参考音，", en: "A few seconds of audio," },
      accent: { zh: "克隆出一模一样的你", en: "and your voice is cloned" },
      subtitle: {
        zh: "三引擎（Fish / Qwen3 / VoxCPM）自动择优：实时对话、极速首包、48kHz 可商用各取所长。10 语种共用同一音色，情感语气自然连贯——全部本地部署，声音数据不出你的机器。",
        en: "Three engines (Fish / Qwen3 / VoxCPM) auto-picked per job: real-time chat, ultra-fast first packet, commercial 48kHz. Ten languages in one voice with natural emotion — fully private, audio never leaves your racks.",
      },
      points: [
        { zh: "Qwen3 首包 ≈97ms · 3 秒克隆 · 实时对话级", en: "Qwen3 ≈97ms first packet · 3s cloning · real-time grade" },
        { zh: "中 / 英 / 日 / 韩等 10 语种 · 同一音色", en: "10 languages · one identical voice" },
        { zh: "C2PA 可验真水印 · 克隆伦理校验", en: "C2PA-verifiable watermark · clone-ethics checks" },
      ],
    },
    demo: {
      title: { zh: "先听，再谈", en: "Listen first, talk later" },
      subtitle: {
        zh: "同一克隆音色朗读四种语言——点开即听。",
        en: "One cloned voice reading four languages — tap to play.",
      },
      realNote: {
        zh: "以上为引擎真实产出、未经剪辑。想听你自己的音色？发 10~40 秒样本，当场克隆给你听。",
        en: "Real, unedited engine output. Want your own voice? Send a 10-40s sample and we clone it on the spot.",
      },
    },
    caps: [
      {
        title: { zh: "三引擎克隆音 · 自动择优", en: "Tri-engine cloning · auto-pick" },
        desc: {
          zh: "几十秒样本零样本克隆。Fish 实时 / Qwen3 首包极速 10 语种 / VoxCPM 48kHz 可商用，克隆完引擎自动推荐最合适的一路。",
          en: "Zero-shot cloning from seconds of audio. Fish real-time / Qwen3 ultra-fast across 10 languages / VoxCPM commercial 48kHz — auto-recommended per use case.",
        },
        proof: { zh: "Qwen3 首包 ≈97ms · 3 秒克隆 · 10 语种", en: "Qwen3 ≈97ms first packet · 3s clone · 10 languages" },
      },
      {
        title: { zh: "情感与语气 · 像真人一样说话", en: "Emotion & prosody that feel human" },
        desc: {
          zh: "情感标签 + 自然语言指令双模式：开心、安抚、兴奋、耳语随点随换，长文朗读语气连贯不出戏。",
          en: "Emotion tags plus natural-language style prompts: happy, soothing, excited, whisper on demand — consistent prosody across long reads.",
        },
        proof: { zh: "情感引擎 + 指令模式 · 长文语气连贯", en: "Emotion engine + instruct mode" },
      },
      {
        title: { zh: "接直播 · 接电话 · 接对话大脑", en: "Plugs into live, calls and chat" },
        desc: {
          zh: "克隆音直通数字人直播、电话桥接与 AI 对话大脑：有记忆、懂情绪，答得准、聊得像真人。",
          en: "Cloned voice flows straight into digital-human streams, phone bridges and the AI conversation brain — memory, mood-awareness, human-grade replies.",
        },
        proof: { zh: "直播 / 电话 / 对话一条链路", en: "One pipeline: live / calls / chat" },
      },
      {
        title: { zh: "合规可溯源 · 一键验真", en: "Compliant & verifiable" },
        desc: {
          zh: "产出默认嵌 C2PA 内容凭证 + Ed25519 签名 + 不可见水印，第三方可离线验真；未授权音色直接拒绝克隆。",
          en: "C2PA credentials + Ed25519 signature + invisible watermark by default; unlicensed voices are refused outright.",
        },
        proof: { zh: "C2PA + Ed25519 · 克隆伦理校验", en: "C2PA + Ed25519 · ethics checks" },
      },
    ],
    steps: [
      {
        title: { zh: "发一段 10~40 秒干净人声", en: "Send 10-40s of clean speech" },
        desc: { zh: "手机录音即可，越干净越像；支持多段融合提升相似度。", en: "A phone recording works; cleaner audio clones better. Multi-clip fusion boosts similarity." },
      },
      {
        title: { zh: "引擎克隆 + 自动择优", en: "Engine clones & auto-picks" },
        desc: { zh: "约 3 秒完成克隆，引擎按你的场景自动推荐最合适的合成引擎。", en: "Cloning takes ~3 seconds; the engine recommends the best synth route for your scenario." },
      },
      {
        title: { zh: "输入文本，随处可用", en: "Type text, use it anywhere" },
        desc: { zh: "配音出片、直播开麦、电话客服、多语种内容矩阵——同一音色全场景复用。", en: "Dubbing, live streams, phone support, multilingual content — one voice everywhere." },
      },
    ],
    faq: [
      {
        q: { zh: "需要多长的声音样本？", en: "How much sample audio do I need?" },
        a: {
          zh: "10~40 秒干净人声即可克隆；样本越干净相似度越高，支持多段样本融合进一步提升。",
          en: "10-40 seconds of clean speech is enough; cleaner samples clone closer, and multi-clip fusion pushes similarity further.",
        },
      },
      {
        q: { zh: "支持哪些语言？中文克隆的音色能说英语吗？", en: "Which languages? Can a Chinese-cloned voice speak English?" },
        a: {
          zh: "支持中、英、日、韩等 10 语种，而且是同一音色跨语种——中文克隆完直接说英语、日语，听感还是同一个人。",
          en: "Ten languages including EN/ZH/JA/KO — with the same voice across all of them. Clone once in Chinese, speak English and Japanese as the same person.",
        },
      },
      {
        q: { zh: "声音数据安全吗？", en: "Is my voice data safe?" },
        a: {
          zh: "全部本地部署，样本和产出都不出你的机器；产出默认带 C2PA 可验真水印，未授权音色引擎会直接拒绝克隆。",
          en: "Everything runs on your own hardware — samples and output never leave it. Output carries C2PA verification, and unlicensed voices are refused.",
        },
      },
    ],
    finalCta: {
      title: { zh: "用你的声音，当场克隆给你听", en: "We clone your voice, live" },
      desc: {
        zh: "30 分钟真机演示：远程连你的机器或用我们的样机，你发样本我们当场克隆，跨语种朗读给你验货。",
        en: "30-minute live demo: on your machine or ours — send a sample, hear it cloned and reading across languages on the spot.",
      },
    },
  },

  face: {
    slug: "/face",
    productLine: { zh: "幻颜 FaceX × 幻影 LiveX · AI 换脸", en: "FaceX × LiveX · AI face swap" },
    seo: {
      title: {
        zh: "AI 换脸出片 + 实时直播换脸 · 活体数字人 | 幻颜 FaceX · 幻影 LiveX — 无界科技",
        en: "AI Face Swap + Real-time Live Swap · Digital Human | FaceX · LiveX — BOUNDLESS",
      },
      description: {
        zh: "直播实时换脸：脸区原生通道清晰度 4.5×，25fps 高清，双人同框各换各脸；高清活体数字人会眨眼摆头；图片视频成片级精修。本地部署，平台无感。",
        en: "Live face swap with a native face channel (4.5× sharper, 25fps HD), dual-face frames, living digital humans that blink and move, production-grade image/video refinement. Fully private.",
      },
      keywords: ["AI换脸", "实时换脸", "直播换脸", "face swap", "数字人直播", "虚拟主播", "换脸软件", "AI数字人"],
    },
    hero: {
      title: { zh: "直播里换一张脸，", en: "Swap your face on a live stream —" },
      accent: { zh: "清晰到看不出破绽", en: "sharp enough to fool anyone" },
      subtitle: {
        zh: "脸部走原生高分辨率通道，清晰度实测 4.5× 提升：720p 高清默认、1080p 超清随选，显卡吃紧自动降档不卡顿。双人同框各换各脸，活体数字人会眨眼摆头——全部本机运行，平台无感。",
        en: "Faces run through a native high-res channel — measured 4.5× sharper. 720p default, 1080p ultra on demand, auto-downshift under GPU pressure. Dual-face frames, living digital humans that blink and move — all on your own box.",
      },
      points: [
        { zh: "实时 25fps · 1080p 超清档 · 亚秒级首帧", en: "25fps live · 1080p ultra · sub-second first frame" },
        { zh: "双人同框各换各脸 · 第三张脸自动回退", en: "Two faces per frame · third face auto-fallback" },
        { zh: "开播前 3 秒设备体检 · 事故挡在开播前", en: "3-second pre-flight device check" },
      ],
    },
    demo: {
      title: { zh: "拖一下，眼见为实", en: "Drag the slider — see for yourself" },
      subtitle: {
        zh: "左原始右换脸，看脸区原生通道带来的清晰度差异；下方是活体数字人口播真实成片。",
        en: "Original vs swapped — see what the native face channel does. Below: a real living digital-human clip.",
      },
      realNote: {
        zh: "以上为引擎真实产出。想看你自己的脸？预约真机演示，一张正脸照当场生成。",
        en: "Real engine output. Want your own face? Book a live demo — one portrait photo is enough.",
      },
    },
    caps: [
      {
        title: { zh: "实时换脸 · 脸区原生通道", en: "Live swap · native face channel" },
        desc: {
          zh: "脸部原生高分辨率通道，实测 4.5× 清晰度；1080p 超清档并发均值 ≈323ms，显卡吃紧自动降档保流畅。",
          en: "Native high-res face channel, 4.5× sharper; 1080p ultra averages ≈323ms under concurrency and auto-downshifts to stay smooth.",
        },
        proof: { zh: "4.5× 清晰度 · 1080p ≈323ms", en: "4.5× clarity · 1080p ≈323ms" },
      },
      {
        title: { zh: "高清活体数字人", en: "HD living digital human" },
        desc: {
          zh: "会眨眼、摆头、有微表情的活体分身，不是死图对口型：克隆形象 + 克隆声 + 口型同步，直推 WebRTC / OBS。",
          en: "Blinks, head turns, micro-expressions — not a lip-synced still. Cloned face + voice + lip-sync, streamed to WebRTC / OBS.",
        },
        proof: { zh: "5090 上 25fps · 首帧 ≈0.9s", en: "25fps on a 5090 · ≈0.9s first frame" },
      },
      {
        title: { zh: "图片 / 视频换脸精修", en: "Image / video swap, refined" },
        desc: {
          zh: "成片级精修：inswapper + GFPGAN / CodeFormer + 光流时序平滑，三路并发池批量出片。",
          en: "Production pipeline: inswapper + GFPGAN / CodeFormer + optical-flow smoothing, batched via a 3-way pool.",
        },
        proof: { zh: "GFPGAN 8.8fps / CodeFormer 5.5fps", en: "GFPGAN 8.8fps / CodeFormer 5.5fps" },
      },
      {
        title: { zh: "直播虚拟背景 / 绿幕", en: "Live virtual background" },
        desc: {
          zh: "虚化 / 图片 / 绿幕一键热切换，CPU 抠像零显存占用，直播中切换毫无卡顿。",
          en: "Blur / image / green-screen hot-swap, CPU matting with zero VRAM cost — switch mid-stream without a hitch.",
        },
        proof: { zh: "抠像 ≈5ms/帧@720p · 零显存", en: "≈5ms/frame matting · zero VRAM" },
      },
    ],
    steps: [
      {
        title: { zh: "一张正脸照生成角色", en: "One portrait creates the character" },
        desc: { zh: "上传照片即建角色，开播前还能 AI 定妆换发型，整场直播生效。", en: "Upload a photo to build the character; optionally restyle hair/makeup before going live." },
      },
      {
        title: { zh: "本机开播，OBS 一键接入", en: "Go live from your own box" },
        desc: { zh: "虚拟摄像头 / OBS 通道即插即用，开播前 3 秒设备体检给你兜底。", en: "Virtual camera / OBS plug-and-play, with a 3-second pre-flight check before you start." },
      },
      {
        title: { zh: "平台无感直播", en: "Stream, platform-agnostic" },
        desc: { zh: "输出就是普通摄像头画面；显卡吃紧自动降档，绝不卡成 PPT。", en: "Output looks like any normal camera; auto-downshift keeps it smooth under load." },
      },
    ],
    faq: [
      {
        q: { zh: "需要什么显卡？", en: "What GPU do I need?" },
        a: {
          zh: "实时换脸入门 RTX 5070 Ti / 4080 16G 起；换脸 + 数字人专业档推荐 RTX 4090 24G / 5080；全功能旗舰推荐 RTX 5090 32G（可双卡）。",
          en: "Entry live swap: RTX 5070 Ti / 4080 16G. Pro (swap + digital human): RTX 4090 24G / 5080. Flagship everything-on: RTX 5090 32G (dual-ready).",
        },
      },
      {
        q: { zh: "直播平台会检测到吗？", en: "Will platforms detect it?" },
        a: {
          zh: "引擎在你本机把画面合成好，走虚拟摄像头输出——平台看到的就是一路普通摄像头信号。",
          en: "Everything is composited locally and delivered through a virtual camera — the platform just sees a normal camera feed.",
        },
      },
      {
        q: { zh: "两个人同框能各换各的脸吗？", en: "Two people in frame?" },
        a: {
          zh: "可以。左右槽位各绑定一张目标脸，出现第三张脸时自动回退不穿帮，访谈连麦都稳。",
          en: "Yes — left/right slots each bind a target face, and a third face triggers a clean auto-fallback. Solid for interviews and co-streams.",
        },
      },
    ],
    finalCta: {
      title: { zh: "用你的脸，当场换给你看", en: "Your face, swapped live" },
      desc: {
        zh: "30 分钟真机演示：在你的硬件上验证真实帧率与清晰度，用你的素材当场生成，数据不出你的机房。",
        en: "30-minute live demo on your hardware: verify real FPS and clarity with your own material — data never leaves your racks.",
      },
    },
  },

  interpreting: {
    slug: "/interpreting",
    productLine: { zh: "通达系 · 通译 LingoX + 通传 VoxX", en: "Lingo · LingoX chat + VoxX interpret" },
    seo: {
      title: {
        zh: "跨境聊天翻译 + AI 实时同传 | 通译 LingoX · 通传 VoxX — 无界科技",
        en: "Chat Translation + Real-time Interpreting | LingoX · VoxX — BOUNDLESS",
      },
      description: {
        zh: "通达系双产品：通译做多平台聊天文字/语音互译与客户资产沉淀，生产运行中；通传做克隆音双向同传与 OBS 双语字幕，面向会议 / 直播场景内测中，预约演示后按场景配置交付。术语锁定、本地部署、数据不出网。",
        en: "Lingo family: LingoX for omni-channel chat translation and customer assets, running in production; VoxX for cloned-voice interpreting and OBS bilingual subtitles, in beta for meeting / live-stream scenarios — demo first, delivery configured per scenario. Glossary-locked, privately deployed.",
      },
      keywords: [
        "AI同传",
        "实时翻译",
        "聊天翻译",
        "同声传译",
        "AI interpreting",
        "直播翻译",
        "双语字幕",
        "通译",
        "通传",
        "会议同传",
      ],
    },
    hero: {
      title: { zh: "打破语言之界，", en: "Break the language barrier —" },
      accent: { zh: "聊天翻译 + 同声传译", en: "chat translate + live interpret" },
      subtitle: {
        zh: "通译 LingoX 承接跨境聊天互译与统一收件箱，已在生产环境稳定运行；通传 VoxX 用你的克隆音做双向同传与 OBS 双语字幕，面向会议 / 直播场景内测中，预约演示后按场景配置交付。两条产品，两种场景，一套通达底座。",
        en: "LingoX covers cross-border chat translation and a unified inbox, running in production; VoxX does two-way cloned-voice interpreting with OBS bilingual subtitles, in beta for meeting and live-stream scenarios — demo first, delivery configured per scenario. Two products, two scenes, one Lingo core.",
      },
      points: [
        { zh: "通译 · 多平台聊天文字/语音互译", en: "LingoX · omni-channel chat translation" },
        { zh: "通传 · 克隆音同传 + 双语字幕（内测中）", en: "VoxX · cloned-voice interpret + subtitles (beta)" },
        { zh: "术语锁定 · 私有部署不出网", en: "Glossary lock · private, off-net" },
      ],
    },
    demo: {
      title: { zh: "同一个声音，两种语言", en: "One voice, two languages" },
      subtitle: {
        zh: "先听中文原声，再听引擎用同一克隆音色输出的英文同传。",
        en: "Hear the Chinese source, then the English interpretation — same cloned voice.",
      },
      realNote: {
        zh: "以上为引擎真实产出、未经剪辑。预约内测演示，用你的声音、你的术语表现场跑一遍。",
        en: "Real, unedited engine output. Book a beta demo and run it with your voice and your glossary.",
      },
    },
    caps: [
      {
        title: { zh: "克隆音同传 · 术语锁定", en: "Cloned-voice interpreting · term lock" },
        desc: {
          zh: "双向同传全程保留你的音色；术语表锁定行业专有名词，TM 缓存越用越快；支持抢话打断。",
          en: "Two-way interpreting that keeps your voice; glossary-locked terminology with a TM cache that speeds up over time; barge-in supported.",
        },
        proof: { zh: "术语表 + TM 缓存 · barge-in 打断", en: "Glossary + TM cache · barge-in" },
      },
      {
        title: { zh: "OBS 直播双语字幕", en: "OBS live bilingual subtitles" },
        desc: {
          zh: "OBS 浏览器源拖一条链接，直播间即出实时双语字幕；散场一键导出 SRT 直接投稿。",
          en: "One URL in an OBS Browser Source adds live bilingual subtitles; export SRT afterwards for publishing.",
        },
        proof: { zh: "SSE 实时推送 · 一键导出 SRT", en: "SSE live push · one-click SRT" },
      },
      {
        title: { zh: "三引擎克隆音底座", en: "Tri-engine voice foundation" },
        desc: {
          zh: "同传的声音底座即幻声 VoiceX：10 语种同一音色、情感语气自然，首包亚秒级不抢拍。",
          en: "Built on VoiceX: ten languages in one voice, natural prosody, sub-second first packet that keeps the conversation flowing.",
        },
        proof: { zh: "10 语种 · 首包亚秒级", en: "10 languages · sub-second first packet" },
      },
      {
        title: { zh: "会议 / 直播场景 · 内测中", en: "Meetings & live streams · in beta" },
        desc: {
          zh: "面向视频会议与跨境直播场景内测中，预约演示后按你的场景配置交付；电话桥接在评估后按需接入。",
          en: "In beta for video meetings and cross-border live streams — book a demo and we configure delivery for your scenario; phone bridging is scoped on request.",
        },
        proof: { zh: "内测中 · 按场景配置交付", en: "In beta · configured per scenario" },
      },
    ],
    steps: [
      {
        title: { zh: "克隆你的音色", en: "Clone your voice" },
        desc: { zh: "10~40 秒样本一次克隆，中外双向同传共用同一音色。", en: "One 10-40s sample powers both directions of interpretation." },
      },
      {
        title: { zh: "选语向 + 导入术语表", en: "Pick languages + import glossary" },
        desc: { zh: "中英日韩等 10 语种互译；把行业词表交给引擎，专有名词从此零翻车。", en: "Ten languages; hand the engine your term list and proper nouns never break." },
      },
      {
        title: { zh: "开会 / 开播", en: "Meet or stream" },
        desc: { zh: "实时同传自动跟话，支持抢话打断；直播加字幕只需一条 OBS 链接。内测期按你的场景配置后交付。", en: "Real-time interpretation with barge-in; live subtitles are one OBS URL away. Beta delivery is configured per scenario." },
      },
    ],
    faq: [
      {
        q: { zh: "延迟有多大？会不会抢拍？", en: "How much latency?" },
        a: {
          zh: "首包亚秒级，正常语速对话自然跟话；支持抢话打断（barge-in），插话时引擎立刻让位，更像真人翻译。",
          en: "Sub-second first packet keeps a natural pace, and barge-in support means the engine yields instantly when someone cuts in — like a human interpreter.",
        },
      },
      {
        q: { zh: "支持哪些语言？", en: "Which languages?" },
        a: {
          zh: "中、英、日、韩等 10 语种双向互译，同一克隆音色跨语种输出。",
          en: "Ten languages including ZH/EN/JA/KO, both directions, all in your one cloned voice.",
        },
      },
      {
        q: { zh: "能用在电话和会议软件里吗？", en: "Does it work with calls and meeting apps?" },
        a: {
          zh: "通传目前面向会议 / 直播场景内测：会议软件走虚拟声卡接入，直播可同步输出双语字幕；电话桥接在内测评估后按需配置。预约内测演示，我们按你的场景配置交付。",
          en: "VoxX is in beta for meeting and live-stream scenarios: meeting apps connect via virtual audio, and live streams get bilingual subtitles. Phone bridging is scoped after a beta assessment. Book a beta demo and we configure delivery for your scenario.",
        },
      },
    ],
    finalCta: {
      title: { zh: "带上你的术语表，预约内测演示", en: "Bring your glossary — book a beta demo" },
      desc: {
        zh: "30 分钟内测演示：用你的声音、你的行业词表现场双向同传，数据全程不出机房。通传内测期按你的会议 / 直播场景配置交付。",
        en: "30-minute beta demo: two-way interpreting with your voice and your term list — data never leaves the room. During beta, VoxX is delivered configured to your meeting / live-stream scenario.",
      },
    },
  },

  // 幻缘 FateX：AI 命理陪伴（engines/chengjie companion.bazi 技能栈）。
  // 文案红线：不写价格/SKU（详批付费未开闸）、不承诺式预言、不恐吓、不碰医疗/投资建议；
  // 可用数字仅工程事实（约 345 盘交叉验证 / 9/9 零幻觉基线 / 十年曲线 / 1080×640）。
  fate: {
    slug: "/fate",
    productLine: { zh: "幻缘 FateX · AI 命理陪伴", en: "FateX · AI fortune companion" },
    seo: {
      title: {
        zh: "AI 八字命理陪伴 · 每日灵签与人生 K 线 | 幻缘 FateX — 无界科技",
        en: "AI BaZi Fortune Companion · Daily Sign & Life K-Line | FateX — BOUNDLESS",
      },
      description: {
        zh: "在 AI 陪聊里自然聊八字：四柱十神大运流年全套排盘，算法经约 345 盘交叉验证，同一张盘永远算出同一个结果；每日灵签千人千面、当天恒定，人生 K 线十年逐年可复算。时辰未知不出时柱，AI 解读过零幻觉门禁——不知道就是不知道。",
        en: "BaZi astrology inside an AI companion chat: full charting cross-validated on ~345 charts — the same chart always yields the same result. A daily sign that's yours alone, a reproducible ten-year life K-line, and honest limits: no birth hour, no hour pillar; readings gated by zero-hallucination evals.",
      },
      keywords: ["AI算命", "八字排盘", "AI命理", "每日运势", "人生K线", "命理陪伴", "BaZi", "Chinese astrology", "fortune AI", "daily horoscope"],
    },
    hero: {
      title: { zh: "把八字聊明白，", en: "Ask fate like a friend," },
      accent: { zh: "把人生画成 K 线", en: "chart life as a K-line" },
      subtitle: {
        zh: "幻缘是长在 AI 陪聊里的命理师：四柱、十神、大运、流年一次排齐，算法经约 345 盘交叉验证——同一张盘，永远算出同一个结果。每天翻一张只属于你的灵签，十年运势画成一条可复算的人生 K 线；不知道就是不知道，时辰未知绝不瞎编时柱。",
        en: "FateX is a fortune reader living inside an AI companion chat: four pillars, ten gods, luck cycles and yearly stars charted in one pass, cross-validated on ~345 charts — the same chart always returns the same result. Flip a daily sign that's yours alone, see ten years drawn as a reproducible life K-line. And when something is unknown, it says so instead of making it up.",
      },
      points: [
        { zh: "全套八字排盘 · 约 345 盘交叉验证", en: "Full BaZi charting · cross-checked on ~345 charts" },
        { zh: "每日灵签千人千面 · 当天恒定可验证", en: "Daily sign, yours alone · fixed for the day" },
        { zh: "人生 K 线十年曲线 · 同盘同图可复算", en: "Ten-year life K-line · fully reproducible" },
      ],
    },
    demo: {
      title: { zh: "先看一张真盘", en: "See a real chart first" },
      subtitle: {
        zh: "上面是引擎画的人生 K 线卡，下面是同一张盘的排盘摘要——都是逐字原样输出，没有一处人工修饰。",
        en: "The life K-line card above and the chart summary below — both verbatim engine output from the same sample chart, untouched.",
      },
      realNote: {
        zh: "以上为引擎真实产出、未经修饰；示例样盘为虚构生辰（1995-08-17）。想看自己的盘？Telegram 上报个生辰，当场排给你。",
        en: "Real, unedited engine output; the sample chart uses a fictional birth date (1995-08-17). Want yours? Share a birth date on Telegram and get charted on the spot.",
      },
    },
    caps: [
      {
        title: { zh: "全套八字排盘 · 工程级可信", en: "Full BaZi charting, engineering-grade" },
        desc: {
          zh: "四柱、十神、藏干、纳音、五行强弱、喜用神、大运、流年一次排齐；立春分年，农历公历都认。算法经金标命例回归 + 十神双实现交叉验证约 345 盘——同一张盘，永远算出同一个结果。",
          en: "Four pillars, ten gods, hidden stems, nayin, elemental strength, favorable elements, luck cycles and yearly stars in one pass; solar-term year boundaries, lunar and solar dates both accepted. Golden-case regressions plus dual-implementation cross-checks on ~345 charts — the same chart always yields the same result.",
        },
        proof: { zh: "约 345 盘交叉验证 · 同盘同果", en: "~345 charts cross-checked · deterministic" },
      },
      {
        title: { zh: "不知道，就是不知道", en: "Honest about the unknown" },
        desc: {
          zh: "时辰未知就不出时柱，性别未知就不排大运——绝不瞎编凑数。命理是参考视角，宁可少算一柱，也不给你一个编出来的答案。",
          en: "No birth hour? Then no hour pillar. Gender unknown? No luck cycles. It never pads a chart with guesses — better one pillar short than one answer made up.",
        },
        proof: { zh: "时辰未知不出时柱 · 性别未知不排大运", en: "Missing inputs are never faked" },
      },
      {
        title: { zh: "每日灵签 · 人生 K 线", en: "Daily sign · life K-line" },
        desc: {
          zh: "签面由今日干支 × 你的日主生成：千人千面、当天恒定可验证，词库吉凶零断言，没有「恐吓式」用词；十年运势画成 1080×640 曲线卡，评分确定性可解释——不是玄学随机数。",
          en: "Your sign comes from today's stems crossed with your day master — unique to you, fixed for the whole day, zero doom verdicts in the wording. Ten years of fortune drawn as a 1080×640 K-line card with an explainable, deterministic score — not mystic dice.",
        },
        proof: { zh: "当天恒定可验证 · 同盘同图可复算", en: "Verifiable daily · reproducible curve" },
      },
      {
        title: { zh: "聊出来的运势 · 记得住的你", en: "A companion that remembers you" },
        desc: {
          zh: "以 AI 人设口吻自然展开，不是冷冰冰的排盘报告；「那我明年呢」这类追问接得住，记得你的生辰，晨安问候顺手翻一张今日签。",
          en: "Readings unfold in the companion's own voice — not a cold printout. Follow-ups like \"what about next year?\" just work; it remembers your birth data and slips your daily sign into the morning hello.",
        },
        proof: { zh: "追问接得住 · 生辰记得住", en: "Handles follow-ups · remembers your chart" },
      },
    ],
    steps: [
      {
        title: { zh: "报一个生辰", en: "Share a birth date" },
        desc: { zh: "聊天里说一句「我 1995 年 8 月 17 日早上十点生」就够了；农历也认，时辰不记得可以先不给。", en: "One line in chat is enough — lunar dates work too, and the hour can wait if you don't remember it." },
      },
      {
        title: { zh: "当轮排盘开聊", en: "Charted in the same turn" },
        desc: { zh: "盘当场排好，运势用人话聊开；追问明年、后年、某个年份，都接得住。", en: "The chart is cast on the spot and read in plain words; ask about next year or any year — it keeps up." },
      },
      {
        title: { zh: "每天翻签，十年看线", en: "Daily sign, ten-year line" },
        desc: { zh: "早安问候顺手翻今日签；想看长线，一张人生 K 线卡把十年趋势画给你。", en: "Mornings come with your daily sign; for the long view, one K-line card draws the whole decade." },
      },
    ],
    faq: [
      {
        q: { zh: "排盘准不准？每次算的会不会不一样？", en: "Is the charting reliable? Will it change between runs?" },
        a: {
          zh: "排盘是确定性算法，不是大模型即兴发挥：金标命例回归 + 十神双实现交叉验证约 345 盘，同一张盘永远算出同一个结果。AI 只负责把盘面聊成人话，解读过零幻觉评测门禁（基线 9/9 合格）——盘里没有的干支，AI 不会编。",
          en: "Charting is a deterministic algorithm, not LLM improvisation: golden-case regressions plus dual-implementation cross-checks on ~345 charts mean the same chart always yields the same result. The AI only turns the chart into conversation, gated by zero-hallucination evals (9/9 on the baseline set) — it never invents stems that aren't in your chart.",
        },
      },
      {
        q: { zh: "不记得出生时辰怎么办？", en: "What if I don't know my birth hour?" },
        a: {
          zh: "没关系，前三柱照样排——但我们不会替你编一个时柱凑数。时辰未知不出时柱、性别未知不排大运，这是幻缘的诚实边界：宁可少算，不瞎算。",
          en: "No problem — the first three pillars still work. What we won't do is invent an hour pillar to fill the gap. No hour, no hour pillar; no gender, no luck cycles. Better under-read than made up.",
        },
      },
      {
        q: { zh: "会不会吓唬人？「大凶」「血光」那种？", en: "Will it scare me with doom talk?" },
        a: {
          zh: "不会。灵签词库吉凶零断言，没有恐吓式用词；不预言死亡、重病、灾祸时点；你情绪低落时先共情、再谈运势——这些是写进产品的安全底线，不是客服话术。",
          en: "No. The daily-sign wording carries zero doom verdicts, it never predicts death, illness or disaster dates, and when you're feeling low it comforts first and reads later. These are safety rules built into the product, not a support script.",
        },
      },
      {
        q: { zh: "AI 算命能信到什么程度？", en: "How seriously should I take an AI fortune reading?" },
        a: {
          zh: "把它当参考和陪伴，不是命令——K 线卡自己的脚注就写着「运势是倾向不是命令」。重大决策仅供参考，医疗、投资请找专业人士；幻缘提供的是聊得来的情绪价值，不是改命服务。",
          en: "Treat it as reflection and companionship, not instruction — the K-line card itself is footnoted \"a tendency, not a command.\" Major decisions deserve professional advice (medical and financial included); FateX offers a companion worth talking to, not a destiny-fixing service.",
        },
      },
      {
        q: { zh: "每日灵签和别家「今日运势」有什么不一样？", en: "How is the daily sign different from a generic horoscope?" },
        a: {
          zh: "签面由今日干支 × 你的日主（十神关系）生成：千人千面，而且当天恒定——早上翻和晚上翻是同一张，可验证、不糊弄。宜忌、幸运色按确定性规则轮转，不是随机抽。",
          en: "Your sign is generated from today's stems crossed with your day master — different for everyone, yet fixed for the whole day, so you can check it isn't reshuffled. Do's, don'ts and lucky colors rotate deterministically, not randomly.",
        },
      },
    ],
    finalCta: {
      title: { zh: "把生辰交给幻缘，当场排给你看", en: "Give FateX a birth date — charted on the spot" },
      desc: {
        zh: "Telegram 上聊两句：报个生辰当场排盘，翻一张今日灵签，人生 K 线画给你看。知缘知运，聊过才知道。",
        en: "Two lines on Telegram: share a birth date, watch the chart cast live, flip today's sign and get your K-line card. Ask fate, chart life — one chat away.",
      },
    },
  },
};

// 落地页真实媒体（与 public/showcase/real/ 对应）
export const LANDING_MEDIA = {
  voiceClips: [
    { label: { zh: "中文", en: "Chinese" }, src: "/showcase/real/voice-zh.mp3?v=20260708" },
    { label: { zh: "English", en: "English" }, src: "/showcase/real/voice-en.mp3?v=20260708" },
    { label: { zh: "日本語", en: "Japanese" }, src: "/showcase/real/voice-ja.mp3?v=20260708" },
    { label: { zh: "한국어", en: "Korean" }, src: "/showcase/real/voice-ko.mp3?v=20260708" },
  ],
  interpPair: {
    src: { label: { zh: "中文原声（克隆音）", en: "Chinese source (cloned voice)" }, file: "/showcase/real/interp-src-zh.mp3" },
    out: { label: { zh: "英文同传（同一音色）", en: "English output (same voice)" }, file: "/showcase/real/interp-out-en.mp3" },
  },
  faceSwap: {
    before: "/showcase/live-before.png",
    after: "/showcase/live-after.png",
  },
  dhVideoZh: { src: "/showcase/real/digital-human.mp4?v=20260708", poster: "/showcase/real/digital-human-poster.png?v=20260708" },
  dhVideoEn: { src: "/showcase/real/digital-human-en.mp4", poster: "/showcase/real/digital-human-en-poster.jpg" },
  // 幻缘 /fate：真实引擎产出——K 线卡由 bazi_kline.render_kline_png 出图（1080×640，
  // 自带「仅供参考·运势是倾向不是命令」脚注），排盘摘要为 bazi_engine.format_chart_summary
  // 逐字输出。示例样盘=虚构生辰 1995-08-17（时辰性别齐全，故有时柱与大运）。
  fateKline: {
    img: "/fate/kline-sample.png?v=20260726",
    width: 1080,
    height: 640,
    chartSummary:
      "四柱：乙亥 甲申 庚辰 辛巳（1995-08-17 生，属猪）\n日主：庚金\n透干十神：年正财、月偏财、时劫财\n五行（月令双计）：金4 木2 水1 火1 土1；日主偏强（粗判），喜用候选：火、水、木\n当前大运：丁亥（28岁起）\n今年流年：2026 丙午",
  },
};
