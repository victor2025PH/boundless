function hub() {
  const HUB = window.location.origin;
  return {
    tab: 'profiles',
    visitedTabs: ['profiles'],   // 已访问过的 Tab（为内容懒加载预留）
    sidebarCollapsed: (function(){ try{ return localStorage.getItem('hub_sidebar_collapsed')==='1'; }catch(_){ return false; } })(),  // 侧栏折叠态
    cmdShow:false, cmdQuery:'', cmdIndex:0,   // 命令面板（Ctrl+K）
    // [统一图标·2026-07-16] ic=brand-icons.svg 线性图标(侧栏/横向导航/命令面板经 icx() 渲染)。
    // emoji 过渡字段已退役(消费面归零核实于同日)；功能注册表(/api/features)的 icon 仍保留作旧缓存回退。
    // [四视角 P0-2·2026-07-16] 分组按「任务流」而非「东西是什么」：建分身 → 做内容 → 推上线 → 看数据。
    //   同传从「运营」还位为上线能力；「对话」以跳出型页签入列（href= 新窗口打开 /phone，不离开控制台）；
    //   opsOnly=1 的页签在演示模式隐藏（对客户演示时藏起运维噪音）。
    //   tooltip 的产品线副标题运行时取自 /api/features 注册表（tabTitle()），不在此重复维护。
    tabs: [
      // ── 我的分身：角色与声音是一切的起点 ─────────────────
      {id:'profiles', ic:'users',  label:'角色库', group:'我的分身'},
      {id:'clone',    ic:'copy',   label:'克隆',   group:'我的分身'},
      // ── 内容创作 ──────────────────────────────────────────
      {id:'voice',    ic:'mic',    label:'语音',   group:'内容创作'},
      {id:'sing',     ic:'music',  label:'唱歌',   group:'内容创作'},
      {id:'batch',    ic:'package',label:'批量',   group:'内容创作'},
      // ── 上线：把分身推向观众 / 客户 ───────────────────────
      {id:'stream',   ic:'signal', label:'开播',   group:'上线'},
      {id:'phone',    ic:'chat',   label:'对话',   group:'上线', href:'/phone'},
      {id:'interp',   ic:'globe',  label:'同传',   group:'上线'},
      // ── 数据与运维 ────────────────────────────────────────
      {id:'dashboard',ic:'chart',  label:'看板',   group:'数据与运维'},
      {id:'history',  ic:'clock',  label:'历史',   group:'数据与运维'},
      {id:'selfcheck',ic:'check',  label:'交付体检', group:'数据与运维', opsOnly:1},
      {id:'logs',     ic:'file',   label:'日志',   group:'数据与运维', opsOnly:1},
      {id:'settings', ic:'gear',   label:'设置',   group:'数据与运维'},
    ],
    // 演示模式下的侧栏/导航过滤（隐藏运维专用页签）；正常模式全量
    navTabs(g){ return this.tabs.filter(t => t.group===g && (!this.demoMode || !t.opsOnly)); },
    // 页签 tooltip：功能注册表有产品线（幻声 VoiceX 等）时挂副标题——单一真相在 /api/features
    tabTitle(t){
      const f = (this.features||[]).find(x => x.id===t.id);
      return (f && f.line && /X/.test(f.line)) ? (t.label+' · '+f.line) : t.label;
    },
    // [P2-2 版位引导·2026-07-16] PRO 角标：注册表 edition==='pro' 的页签（唱歌/批量/开播/同传），
    // 对「非正式旗舰」客户显示金色 PRO 徽（与首页卡片同款语义），点击直达 /order。
    // 保护规则：旗舰/企业正式授权、试签旗舰中、演示模式、授权态未知（lic 未加载）都不显示——
    // 绝不对付费客户/客户演示露出付费墙暗示；uivr 回归模式因 licChip 停拉授权而天然隐藏（基线确定性）。
    tabProBadge(t){
      if(this.demoMode || t.href) return false;
      const f=(this.features||[]).find(x=>x.id===t.id);
      if(!f || f.edition!=='pro') return false;
      const s=this.lic; if(!s) return false;
      if(s.status==='valid' && (s.edition==='pro'||s.edition==='enterprise')) return false;
      if(s.trial_up && s.trial_up.active) return false;
      return true;
    },
    goPro(id){
      // 埋点带 sid/page（与 home.html track() 同口径）：/ops 聚合才有会话去重与来源页维度
      let sid=''; try{ sid=sessionStorage.getItem('bd_sid')||''; if(!sid){ sid=Math.random().toString(36).slice(2,16); sessionStorage.setItem('bd_sid', sid); } }catch(_){}
      try{ fetch(HUB+'/api/ui/event',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ev:'pro_upsell', id:id, page:'/ui', sid:sid})}); }catch(_){}
      window.open('/order','_blank');
    },
    // 线性图标渲染(单一真相=static/brand-icons.svg)：功能位统一单色线性图标,emoji 只留内容位。
    // 尺寸/描边写成 SVG 呈现属性(不只靠 .bd-ic 类)：即使 brand.css 被旧缓存粘住,图标也只是 15px 小图,
    // 绝不再出现"无样式 SVG 默认 300×150 黑块爆版"(2026-07-16 实锤事故)。
    icx(name, cls){ return '<svg class="bd-ic'+(cls?(' '+cls):'')+'" width="15" height="15" fill="none" stroke="currentColor"'
      +' stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">'
      +'<use href="/static/brand-icons.svg?v=20260716b#i-'+name+'"/></svg>'; },
    demoAlertsOpen: false,   // 演示模式下告警横幅收敛为顶栏小胶囊,点开临时展开
    tabGroups: ['我的分身','内容创作','上线','数据与运维'],
    features: [],   // P1: 功能注册表(/api/features)的跨页入口，喂命令面板(Ctrl+K) + 全局可达
    interpUp: null,                                    // 同传服务(7900)在线状态:null未知/true在线/false离线
    interpUrl: 'http://'+location.hostname+':7900/',   // 同传页地址(独立端口;init 时经 /api/ports 按本套安装校准)
    svcPorts: {},                                      // 本套安装的生效端口表(/api/ports;两套并存时防串门)
    portsInfo: null,                                   // /api/ports 全量(base=安装目录/offset=端口偏移;体检页示身份)
    // [去重·2026-07-16] 同传外层观测态与双 CTA busy 态退役：外层观测条与开始入口
    // 让位给 iframe 内面板(嵌入态自动瘦身)，观测/开始/停止单一真相在 live_interpreter 页内。
    profiles: [], active: '', services: {}, voices: [],
    svcAlerts: [],   // 全部活动告警（含 audience 字段，来自 /ws/status 心跳）
    dismissedAlerts: [],  // 用户已关闭的告警 key（仅本会话内隐藏）
    alertHubOpen: false,  // [降噪·WS2] 告警中枢「还有 N 条」展开态（仅本会话）
    streamStepsOpen: false, // [P2 极简] 用户模式：开播步骤日志默认折叠为一行摘要，点开看全量
    streamClock: 0,         // [P2 价值条] 在播时长的秒级心跳（signalTick 每 ~1.1s 刷新，仅开播页）
    sessPeakFps: 0,         // [P3 场次小结] 本场峰值 fps（signalTick 累计）
    sessUsedRvc: false,     // [P3 场次小结] 本场是否用过变声
    lastSession: null,      // [P3 场次小结] 停播后即时结算 {durSec,peakFps,usedRvc,stabilityPct,endedTs}；开播即清空、可手动关闭
    sessStartTs: 0,         // [P5 修正] 本场开播起点(tab 无关，由 $watch('perf.streaming') 设定)，供时长/小结精确计时
    sessTicksTotal: 0,      // [S5 成绩单·稳定度] 本场健康采样数（signalTick 累计，warmup 不计入）
    sessTicksOk: 0,         // [S5 成绩单·稳定度] 其中「换脸生效中(ok)」的采样数
    startingSince: 0,       // [S5 启动仪式] 本次点击开播的时刻（oneClickStart 置位，驱动耗时/里程碑）
    // [S6 新手引导] 开播页三步引导：只对「从没成功开过播」的人出现；首播成功或手动 ✕ 后永久退场。
    //   存储异常时宁可不显示（返回 true），避免老手每次刷新都被引导骚扰。
    streamGuideDone: (function(){ try{ return localStorage.getItem('hub_stream_guide_done')==='1'; }catch(_){ return true; } })(),
    startingRestart: false, // [S5 启动仪式] 本次是否为「重新开播」（文案区分）
    demoMode: (function(){ try{ return localStorage.getItem('hub_demo')==='1'; }catch(_){ return false; } })(),  // 演示模式：隐藏运维噪音
    // P2-4 质量分档阈值单源（canvas_brand.js，与 /phone 同一份真相）；兜底值仅护加载失败
    QA: (window.BD_CANVAS && window.BD_CANVAS.QA) || {GOLD:0.75, COS_OK:0.6, COS_MID:0.45, NAT_OK:0.85, NAT_MID:0.7},
    brand:'79 122 255',   // 当前品牌主色（R G B）= 无界蓝，与 brand.css --bd-acc 同源
    brandPresets:[
      {name:'无界蓝',rgb:'79 122 255'}, {name:'经典蓝',rgb:'88 166 255'},
      {name:'幻紫',rgb:'168 85 247'},   {name:'靛紫',rgb:'139 122 255'},
      {name:'翠绿',rgb:'63 185 80'},   {name:'青色',rgb:'56 189 248'},
      {name:'暖橙',rgb:'247 129 102'}, {name:'玫红',rgb:'244 114 182'},
    ],
    watchdog: null,  // 看门狗运行态（巡检健康/自愈统计/守护复活）
    svcLabel: {fish_tts:'克隆音TTS', stt:'语音识别', lipsync:'活体口型', vcam:'广播中枢', latentsync:'高清口型',
               tts:'基础TTS', emotion_tts:'情感TTS', rvc:'变声', faceswap:'换脸', enhance:'画质增强',
               hair:'发型', singing:'唱歌', ditto:'Ditto数字人', echomimic:'EchoMimic口型', faceswap2:'换脸2'},
    svcPort:  {fish_tts:7855, stt:7854, lipsync:8090, vcam:9001, latentsync:8091,
               tts:7851, emotion_tts:7852, rvc:6242, faceswap:8000, enhance:8092,
               hair:8001, singing:7853},
    // 核心链路：缺任一即"红"；其余为可选增强，离线只算"黄"不报错（与后端 doctor 静音口径一致）
    coreSvcs: ['fish_tts','stt','lipsync','vcam'],   // 回退用；优先用后端 broadcast.core(模式感知单一真相)
    broadcast: null,                                  // 后端 /health 下发：{mode,core[],na[],core_down[],ok}
    // 开播模式（两条链路 OBS 已拆分）：real_faceswap=真人换脸(独占OBS) / avatar_lipsync=数字人(独占OBS)。
    //   前端选择 → 后端持久化为“单一真相”，开播时按模式只起对应链路 + 做 OBS 设备互斥编排。
    broadcastMode: (function(){ try{ return localStorage.getItem('hub_broadcast_mode')||''; }catch(_){ return ''; } })(),
    broadcastModeExplicit:false,   // 是否已显式选过模式（未选→引导先选，避免误开错链路）
    dualAvailable:false,           // 是否装了 Unity Capture → 允许「双路同开」(真人→OBS + 数字人→Unity)
    broadcastDual:false,           // 用户是否开启双路同开
    stepLabels: {check_services:'检查服务', activate_profile:'激活角色', park_unused:'腾出未用引擎', obs_mutex:'画面通道锁定', camera_preflight:'摄像头预检', start_video:'启动视频', start_rvc:'启动变声', auto_audio:'配置音频设备', stop:'已停止'},
    sysLabels: {gpu:'显卡', gpu_mem:'显存', ram:'内存', disk:'磁盘', profiles:'角色数', voices:'音色数'},
    sysInfo: {},

    // 性能
    perf: {gpu_util:-1, gpu_mem_used:-1, gpu_mem_total:0, ram_percent:-1, streaming:false, rvc_running:false, swap_latency_last:0, swap_latency_avg:0, swap_ok:0, swap_fail:0, fps:0, faces_tgt:0, faces_used:0, faces_filtered:0, detect_ms:0, swap_ms:0, enhance_ms:0, smooth_ms:0},
    perfThreshold: {lat_high:800, lat_mid:500, fail:10},
    autoLowPreset:false, lowPresetApplied:false,
    perfLive: false,
    profilesVersion: 0,   // WS 推送的版本号，变化时自动重载
    healthPressure: 'green', // green/yellow/red，来自 /health 轮询
    degradedServices: [], // 远端掉线→本机兜底中的服务名（来自 /api/capacity gpu_pools），用于优雅降级提示

    // 角色创建（照片 + 录音）
    newP: {name:'', desc:'', voice:'', hair:'', rvcModel:'', faceB64:'', facePreview:'', previewAudio:''},
    // P0: 表单内直接录/传新声音——与克隆向导共用质检/录音管线（数据域独立，互不干扰）
    newVoice: {mode:'existing', audioB64:'', audioName:'', drag:false, quality:null, recCheck:null,
               override:false, checking:false, agreed:false, playUrl:''},
    // 编辑抽屉「录一段新声音替换绑定」折叠面板（同一管线的第三个数据域）
    editVoice: {open:false, audioB64:'', audioName:'', drag:false, quality:null, recCheck:null,
                override:false, checking:false, agreed:false, playUrl:''},
    newFaceDrag:false,     // 人脸照片拖拽落区高亮
    newPAdvOpen:false,     // 高级选项（RVC/发型）折叠态
    newPBusy:false,        // 创建中（含克隆合规管道）
    newPDone:'',           // 创建成功的角色名 → 成功态面板
    newPDoneVoice:false,   // 成功创建的角色是否带声音（决定是否显示「试听」出口）
    createMsg:'', createOk:false,

    // 真人视频数字人（上传即建，路线A）
    vidAv: {name:'', desc:'', file:null, fileName:'', busy:false, msg:'', ok:false, thumb:'', meta:null},

    // 角色编辑
    editShow:false, editP:{}, editMsg:'', editOk:false,
    drawerTab:'overview',   // P0(UI改版): 角色详情抽屉当前标签 overview|edit|advanced（editShow 复用为抽屉开关）

    // 预览
    previewShow:false, previewName:'', previewImg:'', previewLoading:false, previewInfo:'',

    // 语音
    speakText:'', speakLang:'zh-cn',
    speakEmotion:'neutral', speakInstruct:'', speakLipsync:false,
    speakLoading:false, streamLoading:false,
    speakMsg:'', speakOk:false, audioSrc:'',
    streamInfo:'', streamPct:0,
    speakDetectedEmotion:'', lipsyncVideoSrc:'',
    streamAbortCtrl: null, streamQueueCtrl: null,  // P14-1: 流式停止控制
    speakSentences:[],   // P21-4: 分句列表
    speakCurrentIdx:-1,  // P21-4: 当前播放句子索引
    // UI-P0: 合成模式（standard=整段+口型 / stream=逐句串流），跨会话记忆
    speakMode: (()=>{ try{ return localStorage.getItem('ah_speak_mode_v1')==='stream'?'stream':'standard'; }catch(_){ return 'standard'; } })(),
    speakHistId:0, speakStarred:false,   // UI-P0: saved 事件回传的历史 id → ⭐ 收藏
    speakResultMode:'',                  // UI-P0: 播放器中结果的来源模式（standard/stream）
    // UI-P0: 场景示例快填（带推荐情感/语言，顺带演示情感能力）
    speakExamples: [
      {label:'🛍 带货开场', emotion:'excited', lang:'zh-cn',
       text:'家人们！今天这款宝贝真的可以闭眼入，库存不多，喜欢的抓紧下单！'},
      {label:'📚 知识口播', emotion:'serious', lang:'zh-cn',
       text:'很多人不知道，其实每天只需要十分钟，效率就能翻倍。今天我用三句话给你讲清楚。'},
      {label:'🌙 晚安电台', emotion:'gentle', lang:'zh-cn',
       text:'夜深了，忙碌了一天的你，也该把心事放一放。愿你今晚有个好梦，晚安。'},
      {label:'👋 English 开场', emotion:'happy', lang:'en',
       text:"Hi everyone, I'm your digital host. Great to meet you here, let's get started!"},
    ],
    // UI-P1: 本次会话结果卡（最新在前，≤5 条；Blob URL 随淘汰 revoke，防内存累积）
    speakResults: [], _speakResSeq:0, speakResultsShow:false, _lastSpeakItem:null,
    voiceRecent: [],         // UI-P2-4: 语音页内嵌"最近合成"（历史前 3 条，会话为空时显示）
    speakStreamFull:false,   // 流式整段音频已回填播放器（此时才允许下载/收藏，避免误下载最后一句）
    _streamFullUrl:'',
    emotionsExpanded:false,  // UI-P1: 情感两级化（首行高频 6 个，其余折叠；已选中的冷门项自动钉住）
    instructOpen:false,      // UI-P1: 情感指令收进「高级」入口
    vqAdvanced:false,        // UI-P1: 音质优化高级手段折叠（漏斗化：分数条+推荐动作常驻）

    // 音质优化（seed 校准 / best-of-N 择优 / 盲听 A/B）
    vqShow:false,
    vqCalibLoading:false, vqCalibResult:null,
    vqBestOf:4, vqBestLoading:false, vqBestResult:null,
    vqBlind:[], vqBlindLoading:false, vqBlindPicked:-1, vqBlindReveal:false,
    vqOptLoading:false, vqOptResult:null, vqOptProgress:'', vqOptFile:'', vqOptFileName:'',
    vqOptMaxRefs:1,
    vqSegments:[], vqListenAlt:'', vqListenLoading:false, vqListen:[], vqListenPicked:-1, vqListenReveal:false,
    vqListenApplying:false,

    // 情感选项（胶囊按钮用）
    emotionOptions: [
      {value:'auto',      label:'🤖 自动'},
      {value:'neutral',   label:'😐 普通'},
      {value:'happy',     label:'😊 开心'},
      {value:'excited',   label:'🤩 兴奋'},
      {value:'gentle',    label:'🌸 温柔'},
      {value:'calm',      label:'😌 平静'},
      {value:'sad',       label:'😢 悲伤'},
      {value:'angry',     label:'😡 愤怒'},
      {value:'serious',   label:'😤 严肃'},
      {value:'fearful',   label:'😨 恐惧'},
      {value:'surprised', label:'😲 惊讶'},
      {value:'disgusted', label:'🤢 厌恶'},
    ],

    // 唱歌（Sing-P0 诚实化改版：引擎结构化/风格/取消/估时/参考试听/音色来源）
    singLyrics:'', singLang:'zh', singSpeed:0.85, singEmotion:'gentle',
    singLoading:false, singMsg:'', singOk:false, singAudioSrc:'',
    singEngine:'', singHistId:null,
    singRefB64:'', singRefName:'', singRefUrl:'', singRefDur:0, singDrag:false,
    singVoiceMode:'profile',            // profile=用当前角色 / upload=换个声音
    singT0:0, singEta:0, singElapsedMs:0, _singTimer:null, _singAbort:null,
    singStyles: [
      {value:'gentle', label:'🌙 深情'},
      {value:'happy',  label:'☀️ 欢快'},
      {value:'sad',    label:'🌧️ 忧伤'},
      {value:'calm',   label:'😌 平静'},
    ],
    singExamples: [   // 全部公有领域曲目/古诗，版权安全
      {label:'送别', lang:'zh', lyrics:'长亭外，古道边，芳草碧连天。晚风拂柳笛声残，夕阳山外山。'},
      {label:'茉莉花', lang:'zh', lyrics:'好一朵美丽的茉莉花，好一朵美丽的茉莉花，芬芳美丽满枝桠，又香又白人人夸。'},
      {label:'静夜思', lang:'zh', lyrics:'床前明月光，疑是地上霜。举头望明月，低头思故乡。'},
      {label:'Twinkle', lang:'en', lyrics:'Twinkle twinkle little star, how I wonder what you are. Up above the world so high, like a diamond in the sky.'},
      {label:'Greensleeves', lang:'en', lyrics:'Alas my love you do me wrong to cast me off discourteously, for I have loved you well and long, delighting in your company.'},
    ],

    // Song-P1: AI 翻唱（YingMusic-SVC 零样本歌声转换）
    singMode:'lyrics',                        // cover=AI 翻唱 / lyrics=歌词念白
    songCaps:{}, songOnline:false, songCapsLoaded:false,
    songFileObj:null, songFileName:'', songFileUrl:'', songFileDur:0, songDrag:false,
    songPitch:'auto', songQuality:'standard', songDryVocal:false,
    songTaskId:'', songStatus:'', songProgress:0, songDetail:'',
    songT0:0, songElapsedMs:0, _songTimer:null, _songPollStop:false,
    songResult:null, songMsg:'', songOk:false,

    // Song-P2/F1: 直播间点歌台
    stationLoaded:false, stationBusy:false, stationReqName:'',
    station:{enabled:false, chat_enabled:true, auto_prepare:true, auto_play:false,
             announce:false, quality:'standard', playing_id:null, queue:[], library:[],
             chat_hint:'', yield:null},
    stationLibOpen:true, _stationTimer:null,

    // Song-P4/F3: 原创歌（ACE-Step 文本成曲）
    createStyle:'pop, mandarin, female vocal, warm, acoustic guitar, 90 bpm',
    createLyrics:'', createDur:60, createQuality:'turbo', createSwap:false,
    createTopic:'', createLyricsBusy:false,
    createTaskId:'', createStatus:'', createProgress:0, createDetail:'', createStage:'',
    createT0:0, createElapsedMs:0, _createTimer:null, _createPollStop:false,
    createResult:null, createMsg:'', createOk:false,
    createStylePresets:[
      {label:'流行女声', v:'pop, mandarin, female vocal, warm, acoustic guitar, 90 bpm'},
      {label:'流行男声', v:'pop, mandarin, male vocal, emotional, piano, 85 bpm'},
      {label:'国风', v:'chinese style, mandarin, female vocal, guzheng, bamboo flute, elegant, 75 bpm'},
      {label:'电子舞曲', v:'edm, dance, electronic, energetic, female vocal, synth, 128 bpm'},
      {label:'摇滚', v:'rock, mandarin, male vocal, electric guitar, powerful drums, 120 bpm'},
      {label:'抒情民谣', v:'folk, ballad, mandarin, soft female vocal, acoustic guitar, gentle, 70 bpm'},
      {label:'说唱', v:'hip hop, rap, mandarin, male vocal, heavy bass, 95 bpm'},
      {label:'纯音乐', v:'instrumental, cinematic, piano, strings, emotional, no vocal'},
    ],

    // Song-P2: 15 秒高光 MV
    mvBusy:false, mvUrl:'', mvInfo:null,

    // Song-P5: 我的任务看板 / 整曲MV异步任务 / 风格魔改
    songBoard:[], songBoardOpen:0, songBoardShow:false, songBoardLoaded:false,
    mvTask:null,                       // 异步MV当前任务 {task_id,status,progress,detail,url…}
    createRemixOf:null, createRemixName:'', createRemixStrength:0.5,
    stationBoard:[], stationBoardShow:false,

    // Song-P6: 唱腔 LoRA（音乐人格）/ 批量魔改（专辑化）
    createLora:'', createLoraWeight:1.0,   // 引擎 capabilities.loras 上报可用名单
    remixBatchSel:[], remixBatchBusy:false, remixBatchMsg:'',

    // 历史记录
    historyList:[], historyLoading:false,
    historySearch:'', historyStarredOnly:false, historyEmotion:'', batchSaveHistory:true,
    historyPage:1, historyPageSize:20, historyHasMore:false, historyTotalCount:0,
    historyStats:{today:0,week:0,total:0,avg_elapsed_ms:0,most_used:null,distribution:[],max_count:1,daily_7:[]},
    historyMultiSelect:false, historySelected:[],  // 多选批量操作
    bulkVrfBusy:false, bulkVrfProg:'', bulkVrfSummary:null,  // UH3: 批量验真（整批交付自证）

    // 情感对比
    compareShow:false, compareLoading:false,
    compareSelected:['happy','excited','sad'],
    compareResultMap:{},

    // P22-4: 角色A/B对比
    abCompareShow:false, abCompareLoading:false,
    abCompareSelected:[],  // 选中的角色名列表
    abCompareResults:[],   // 对比结果数组

    // 引擎 A/B（Fish vs CosyVoice）
    engAbShow:false, engAbLoading:false, engAbResults:[], engAbWinner:'',
    probeSentencesText:'',

    // StreamOut Phase E
    soShow:false, soLoading:false, soPlugins:[], soSelected:['vcam','webrtc'],
    soStatus:null, soPreviewUrl:'', soRtmpUrl:'', soRecord:false, soRecordings:[],

    // 合成阶段追踪（用于分阶段进度）
    speakPhase: '',
    speakRvcApplied: false,  // 第一次返回后是否应用了 RVC
    speakElapsedTts: 0,      // TTS 耗时ms
    speakElapsedRvc: 0,      // RVC 耗时ms
    speakDoneVisible: false, // done 后进度条持绣 2s

    // P14-5: WS 连接状态
    wsConnected: true,
    // P14-2: 运行指标面板
    metricsData: {}, metricsLoading: false, metricsAutoRefresh: false,  // P15-1
    // P16-5: CSV 导出日期过滤
    csvFilterShow: false, csvStartDate: '', csvEndDate: '',

    // P20-5: 服务配置热重载
    configData: null, svcEdits:{},   // svcEdits: 服务地址草稿态（显式保存，避免误改热重载技术项）

    // 移动端遥控
    mobileQr: null,   // {qr_base64, ctrl_url, local_ip}
    mobileQrLoading: false,

    // 批量配音
    batchText: '', batchLang: 'zh-cn', batchEmotion: 'neutral',
    batchLoading: false, batchDone: false, batchError: '',
    batchLines: [],   // [{index, text, status:'pending'|'ok'|'error', elapsed, emotion}]
    batchZipUrl: '', batchAbortCtrl: null, batchStopped: false, batchRetrying: false,  // P15-4
    batchFoldOk: false,  // P16-3: 折叠已完成行
    batchETA: 0,         // P19-2: 预估剩余秒数
    batchAudioMap: {},   // P17-5: 持久化音频映射，支持单行重试后重新打包
    batchPlayingIdx: 0,  // UI-P6-2: 行内试听中的行号（0=没在播）
    batchPlayAllOn: false, // UI-P7-2: 连播验收进行中（按行序自动接续）
    batchRunMs: 0,       // UI-P5-4: 本批总用时（墙钟），成绩单卡用
    batchSavedHist: false, // UI-P5-4: 本批是否落了历史（决定成绩单的历史入口/未存原因）

    // 声音克隆（引导式三步）
    cloneStep: 1,     // 1=上传质检 2=协议确认 3=克隆中/结果
    cloneAudioB64: '', cloneAudioName: '', cloneDrag: false,
    cloneQuality: null,   // {ok, duration_s, snr_db, reason}
    cloneQualityLoading: false,
    recCheck: null,       // 录音深度自检 {grade, snr_db, peak_dbfs, clipping_pct, advice...}
    recOverride: false,   // 明显削幅时的显式放行
    recording: false, recSecs: 0, recorder: null, recStream: null,
    recChunks: [], recTimer: null, recMime: '',
    // 录音实时反馈：麦克风电平（0~100，对数映射）+ 连续静音秒数（≥3s 提示「没录上」）
    recLevel: 0, recSilentSecs: 0, _recCtx: null, _recAnalyser: null, _recMeterT: null,
    _recTarget: 'clone',  // 录音结果路由：'clone'=克隆向导 / 'newP'=照片建角色表单 / 'editP'=编辑抽屉换声（硬件态全局唯一，同时只录一路）
    cloneName: '', cloneAgreed: false, cloneCreateProfile: true,
    cloneLoading: false, cloneResult: null, cloneError: '',
    cloneEngineRec: null, cloneEngineRecLoading: false,
    cloneOverwriteOk: false,  // R1-1: 同名角色覆盖需显式确认（防静默清掉形象/引擎/RVC 配置）
    cloneEngineRecErr: '',    // R1-3: 引擎对比失败的诚实降级文案（不再静默消失）

    // Alpine computed
    get historyFiltered() {
      if(this.historyStarredOnly) return this.historyList.filter(i=>i.starred);
      return this.historyList;
    },
    // 可作为声音来源的角色（含克隆向导自动建的角色）——创建/编辑表单「复用角色声音」选项组
    get voiceDonorProfiles() {
      return (this.profiles||[]).filter(p=>p.has_voice);
    },
    // 新声音（现录/上传）就绪校验：''=可用；创建表单与编辑抽屉共用同一套拦截规则
    _capVoiceReason(st, tgt) {
      if(this.recording && this._recTarget===tgt) return '正在录音…点「停止录音」后继续';
      if(!st.audioB64) return '请现场录一段或上传一段声音';
      if(st.checking) return '音质检测中…';
      if(st.quality && !st.quality.ok)
        return '录音未通过质检：'+(st.quality.reason||'请重录或换一段');
      if((st.recCheck?.clipping_pct||0)>0.5 && !st.override)
        return '检测到明显削幅，请勾选放行或重录';
      if(!st.agreed) return '请勾选「我拥有该声音的合法使用权」';
      return '';
    },
    // 照片建角色：创建按钮的拦截原因（''=可创建）；实时显示，替代「点了才报错」
    get newPBlockReason() {
      if(!this.newP.name.trim()) return '请填写角色名称';
      if(this.newVoice.mode==='new'){
        const why=this._capVoiceReason(this.newVoice,'newP');
        if(why) return why==='请现场录一段或上传一段声音' ? why+'（也可切「稍后绑定」）' : why;
      }
      if(this.newVoice.mode==='existing' && this.newP.voice==='' &&
         (this.voices.length || this.voiceDonorProfiles.length)) return '请选择一个声音（或切「稍后绑定」）';
      return '';
    },
    // 编辑抽屉：新声音面板的拦截原因（面板未展开或无音频时不拦截保存）
    get editVoiceReason() {
      if(!this.editVoice.open) return '';
      if(!this.editVoice.audioB64 && !(this.recording && this._recTarget==='editP')) return '';
      return this._capVoiceReason(this.editVoice,'editP');
    },
    // 编辑抽屉：保存时是否会用新声音替换绑定
    get editVoiceReady() {
      return this.editVoice.open && !!this.editVoice.audioB64 && !this._capVoiceReason(this.editVoice,'editP');
    },
    // 照片建角色：左侧实时角色卡的声音徽章
    get newPVoiceBadge() {
      const nv=this.newVoice;
      if(nv.mode==='none') return {label:'🎙️ 声音·稍后绑定', cls:'bg-hub-border/50 text-hub-muted'};
      if(nv.mode==='existing')
        return this.newP.voice
          ? {label:'🎙️ '+(this.newP.voice.startsWith('profile:')?this.newP.voice.slice(8)+'（角色）':this.newP.voice), cls:'bg-green-900/40 text-green-300'}
          : {label:'🎙️ 声音·未选择', cls:'bg-hub-border/50 text-hub-muted'};
      if(!nv.audioB64) return {label:'🎙️ 声音·待录制', cls:'bg-hub-border/50 text-hub-muted'};
      if(nv.checking)  return {label:'🎙️ 质检中…', cls:'bg-yellow-900/30 text-hub-yellow'};
      if(nv.quality && !nv.quality.ok) return {label:'🎙️ 未过质检', cls:'bg-red-900/30 text-hub-red'};
      return {label:'🎙️ 新声音·已就绪', cls:'bg-green-900/40 text-green-300'};
    },

    // 效果配置
    // v3(2026-07-05)：+enginePreset(引擎画质档)；faceEnhance ''=跟随档位；jpegQuality 语义改为「输出画质」(默认85)。
    // 不迁移 v2：旧档常见「增强=codeformer(生产机未部署→空转)」「输出65(偏糊)」正是要修的坑，v3 以新默认起步。
    // P9(2026-07-06)：服务端持久化 data/effect_cfg.json 为单一真相(手机/PC/换浏览器同步)，
    // localStorage 降级为离线缓存——加载先本地快渲染，再用服务端覆盖并回写缓存。
    _effectFields:['videoWidth','videoHeight','videoFps','swapFps','mjpegFps','jpegQuality','crossfade','faceBlend','faceThreshold','faceSmooth','faceEnhance','enginePreset','outputAspect'],
    // 输出画面比例预设(名→宽高)：手机竖屏为主，供比例选择器与开播透传
    _aspectDefs:{
      portrait916:   {label:'竖屏 9:16', w:720,  h:1280, tip:'手机全屏(视频通话/Telegram) — 推荐'},
      portrait34:    {label:'竖屏 3:4',  w:720,  h:960,  tip:'手机竖屏，画面更宽'},
      landscape169:  {label:'横屏 16:9', w:1280, h:720,  tip:'电脑/横屏平台'},
      landscape169hd:{label:'横屏 1080P',w:1920, h:1080, tip:'横屏高清(需摄像头支持1080p)'},
      square11:      {label:'方形 1:1',  w:720,  h:720,  tip:'部分社媒方形画幅'},
    },
    // 切换输出比例：写本地状态+持久化；在播则调 Hub /realtime/output/aspect 热切(vcam 重开虚拟摄像头)
    async setAspect(name){
      const p=this._aspectDefs[name]; if(!p) return;
      const changed=(this.outputAspect!==name);
      this.outputAspect=name; this.videoWidth=p.w; this.videoHeight=p.h;
      this.saveEffectConfig();
      try{
        const d=await fetch(HUB+'/realtime/output/aspect?name='+encodeURIComponent(name)).then(r=>r.json());
        if(d && d.ok){
          this.showToast('画面比例 → '+p.label+(d.applied_live?'（已即时生效）':'（已保存，下次开播生效）'),'success');
          // 分辨率变了，Telegram/微信 会缓存旧分辨率——必须在对端 App 重选一次摄像头才吃新比例。
          // 只在"在播且比例真的变了"时提示(此时才需要重选)，避免无谓打扰。
          if(d.applied_live && changed) this.aspectReselectTip=p.label;
        } else this.showToast('比例切换失败：'+((d&&d.error)||'未知'),'error');
      }catch(_){ this.showToast('比例已保存本机；服务端不可达，开播时生效','info'); }
    },
    aspectReselectTip:'',   // 非空=显示"请在 Telegram 重选摄像头"一次性引导条(切竖屏后新分辨率需重选才生效)
    _effectCfgObj() {
      const cfg={};
      this._effectFields.forEach(k=>{ cfg[k]=this[k]; });
      return cfg;
    },
    _effectApply(cfg) {
      this._effectFields.forEach(k=>{ if(cfg && cfg[k]!==undefined) this[k]=cfg[k]; });
    },
    async saveEffectConfig() {
      const cfg = this._effectCfgObj();
      try{ localStorage.setItem('avatarhub_effect_cfg_v3', JSON.stringify(cfg)); }catch(_){}
      try{
        const d = await fetch(HUB+'/api/effect_cfg', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(cfg)}).then(r=>r.json());
        this.showToast(d && d.ok ? '已保存效果配置（本机+服务端，多端同步）' : ('已存本机；服务端保存失败：'+(d&&d.detail||'')), d&&d.ok?'success':'error');
      }catch(_){ this.showToast('已存本机；服务端不可达，联网后再点一次保存','error'); }
    },
    loadEffectConfig() {
      try {
        const raw = localStorage.getItem('avatarhub_effect_cfg_v3');
        if(raw) this._effectApply(JSON.parse(raw));
      } catch(e) {}
      // 服务端单一真相覆盖(异步)：手机/别台电脑存的配置在这台也生效
      fetch(HUB+'/api/effect_cfg').then(r=>r.json()).then(d=>{
        if(d && d.ok && d.cfg && Object.keys(d.cfg).length){
          this._effectApply(d.cfg);
          try{ localStorage.setItem('avatarhub_effect_cfg_v3', JSON.stringify(this._effectCfgObj())); }catch(_){}
        }
      }).catch(()=>{});
    },
    resetEffectConfig() {
      // 默认=「高清脸」档(2026-07-05 拍板)：引擎 hd(512px+GFPGAN精修·自适应保底) + 输出85
      this.videoWidth=1280; this.videoHeight=720; this.videoFps=30;
      this.swapFps=8; this.mjpegFps=12; this.jpegQuality=85; this.crossfade=0.3;
      // 换脸默认：blend=1(完全替换)、阈值0(自动不过滤)、平滑0(关，最清晰)、增强跟随档位
      this.faceBlend=1; this.faceThreshold=0; this.faceSmooth=0; this.faceEnhance='';
      this.enginePreset='hd'; this.outputAspect='';
      try{ localStorage.removeItem('avatarhub_effect_cfg_v3'); }catch(_){}
      fetch(HUB+'/api/effect_cfg', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({reset:true})}).catch(()=>{});
      this.showToast('已恢复默认（高清脸档）','info');
    },
    // ── P9 参数热更：在播时 融合/阈值/平滑/精修/淡化/输出画质 即调即生效(告别「下次开播生效」) ──
    _lpTimer:null,
    liveParamsPush(now=false){
      if(!this.perf.streaming) return;   // 没在播：改的值走开播参数，无需热更
      clearTimeout(this._lpTimer);
      const doPush = async()=>{
        try{
          const q = new URLSearchParams({blend:this.faceBlend, threshold:this.faceThreshold,
            smooth:this.faceSmooth, enhance:(this.faceEnhance==null?'':this.faceEnhance),
            crossfade:this.crossfade, out_q:this.jpegQuality});
          const d = await fetch(HUB+'/realtime/swap/params?'+q.toString()).then(r=>r.json());
          if(d && d.lic_note) this.showToast('🔒 '+d.lic_note,'info');   // P5 服务端预设闸门降级如实告知
          else if(d && d.ok) this.showToast('换脸参数已热更，即时生效','success');
          else if(d && (d.detail||d.error)) this.showToast('参数热更失败：'+(d.detail||d.error),'error');
        }catch(_){}
      };
      if(now) doPush(); else this._lpTimer=setTimeout(doPush, 500);
    },
    applyLowLatencyPreset(){
      this.applyPreset('low');
      this.saveEffectConfig();
    },
    // S2 画质预设单一定义表：applyPreset 写入 + presetActive() 反推选中态，双向同源不漂移。
    // 2026-07-05 预设合一：每档同时驱动「画布层(分辨率/帧率/输出画质)」+「引擎层 enginePreset(处理宽/精修)」。
    // 关键认知：脸部清晰度由引擎档决定(512px+GFPGAN 精修)，不是画布 1080p——1080p 只放大画布反而稀释脸部像素，
    // 故「高清脸」用 720p 画布 + hd 引擎档；1080p 留给 P2 脸区原生通道落地后再放开。
    _presetDefs:{
      low:      {label:'流畅优先', hint:'720p·20fps·288px — 弱机/弱网兜底，延迟最低', videoWidth:1280, videoHeight:720, videoFps:20, swapFps:6, mjpegFps:10, jpegQuality:75, crossfade:0.15, enginePreset:'eco', faceEnhance:''},
      balanced: {label:'自然',     hint:'720p·384px·无精修 — 性能与观感均衡',      videoWidth:1280, videoHeight:720, videoFps:30, swapFps:8, mjpegFps:12, jpegQuality:85, crossfade:0.25, enginePreset:'natural', faceEnhance:''},
      quality:  {label:'高清脸',   hint:'512px+GPEN轻精修·TensorRT — 换脸12fps·脸区清晰~4.5×(裁剪通道) — 推荐默认', videoWidth:1280, videoHeight:720, videoFps:30, swapFps:12, mjpegFps:12, jpegQuality:85, crossfade:0.15, enginePreset:'hd', faceEnhance:''},
      // [2026-07-06 P2 解锁] 脸区原生通道上线后 1080p 才有意义：脸按原生像素送检(不再被整帧稀释)、背景原生直出。
      ultra:    {label:'超清1080P', hint:'1080p·脸区原生+背景直出 — 画面最锐·需摄像头支持1080p', videoWidth:1920, videoHeight:1080, videoFps:30, swapFps:8, mjpegFps:12, jpegQuality:88, crossfade:0.3, enginePreset:'hd', faceEnhance:''},
      style:    {label:'美颜风格', hint:'448px+柔化精修 — 更浓美妆感，适合带货/娱乐',      videoWidth:1280, videoHeight:720, videoFps:25, swapFps:8, mjpegFps:12, jpegQuality:85, crossfade:0.35, enginePreset:'beauty', faceEnhance:''},
      // crossfade 0.4→0.15(2026-07-09)：0.4 比 5fps 换脸帧间隔(200ms)还长 → 永久 50/50 叠影"白斑"事故根源；
      // 推流端已加动态钳制护栏，这里同步归位存量配置。
      vocal:    {label:'口播极致', hint:'CodeFormer 极致精修 · 单帧最锐~4-5fps — 口播/慢节奏专用', videoWidth:1280, videoHeight:720, videoFps:30, swapFps:5, mjpegFps:10, jpegQuality:88, crossfade:0.15, enginePreset:'hd', faceEnhance:'codeformer'},
    },
    _presetFields:['videoWidth','videoHeight','videoFps','swapFps','mjpegFps','jpegQuality','crossfade','enginePreset','faceEnhance'],
    _tierLabels:{eco:'省电', natural:'自然', beauty:'美颜', hd:'高清'},
    // P4 授权分层：仅「强制模式 + effective 明确为 false」才锁（评估模式/旧后端无此键 → 不锁，零破坏）
    presetLocked(pk){
      const eff=this.lic&&this.lic.effective;
      if(!eff||!eff.enforced) return false;
      if(pk==='ultra') return eff.preset_ultra===false;
      if(pk==='vocal') return eff.preset_vocal===false;
      return false;
    },
    presetLockTip(pk){
      const p=this._presetDefs[pk];
      return '🔒 「'+(p?p.label:pk)+'」不在当前授权档位内 — 点右上角 🔑 徽章查看升级/续费';
    },
    applyPreset(name){
      const p=this._presetDefs[name]; if(!p) return;
      if(this.presetLocked(name)){
        this.showToast(this.presetLockTip(name),'info');
        // 升级引导：展开授权卡(含续费申请文本)。仅在卡收起时模拟点击——重复点锁定档不会把已开的卡又收回去
        try{
          const card=document.getElementById('licCard');
          if(card && card.classList.contains('hidden')){ const c=document.getElementById('licChip'); if(c) c.click(); }
        }catch(_){}
        return;
      }
      const canvasChanged = this.perf.streaming &&
        ['videoWidth','videoHeight','videoFps','swapFps','mjpegFps'].some(f=>this[f]!==p[f]);
      for(const f of this._presetFields) this[f]=p[f];
      // 在播：引擎档 + 逐帧参数(精修/淡化/输出画质)都热切即时生效(P9)；只剩画布层(分辨率/帧率)需重新开播
      if(this.perf.streaming && p.enginePreset){
        fetch(HUB+'/realtime/swap/preset?name='+p.enginePreset).then(r=>r.json()).then(d=>{
          if(d && d.ok) this.showToast('引擎画质档已热切到「'+(this._tierLabels[p.enginePreset]||p.enginePreset)+'」，即时生效','success');
        }).catch(()=>{});
        this.liveParamsPush(true);
      }
      this.showToast('已应用画质预设：'+p.label+(canvasChanged?'（分辨率/帧率变化需重新开播，其余已即时生效）':''),'info');
    },
    // 反推当前参数命中的预设（浮点容差比较）；全不匹配 = 自定义 → 返回 ''
    presetActive(){
      for(const [k,p] of Object.entries(this._presetDefs)){
        // 字段可能是字符串(enginePreset)或数字：字符串按全等比，数字按容差比(避免 0.3!=0.30000001)
        if(this._presetFields.every(f=> (typeof p[f]==='string')
              ? String(this[f]||'')===p[f]
              : Math.abs((+this[f]||0)-p[f])<0.001)) return k;
      }
      return '';
    },
    get historyGrouped() {
      // UH1-5: 「今天/昨天」必须按本地日期算。原 toISOString() 是 UTC——UTC+8 的 0~8 点会把
      //   昨晚的记录标成「今天」、真·今天的记录按日期平铺（服务端 item.date 是本地日期）。
      const _loc = t => { const x=new Date(t);
        return x.getFullYear()+'-'+String(x.getMonth()+1).padStart(2,'0')+'-'+String(x.getDate()).padStart(2,'0'); };
      const today    = _loc(Date.now());
      const yesterday= _loc(Date.now()-86400000);
      const groups   = {};
      for(const item of this.historyFiltered){
        const d = item.date || item.created_at?.slice(0,10) || 'unknown';
        if(!groups[d]) groups[d]=[];
        groups[d].push(item);
      }
      const todayTotal = this.historyStats.today || 0;  // 服务端全量当日数
      return Object.entries(groups).map(([date, items])=>({
        date,
        isToday: date===today,
        label: date===today
          ? ('🗓 今天' + (todayTotal > items.length ? '(当前'+items.length+'/全量'+todayTotal+'条)' : '(共'+items.length+'条)'))
          : (date===yesterday ? '🗓 昨天' : '🗓 '+date),
        items
      }));
    },
    // 历史搜索防抖 300ms
    _historySearchTimer: null,
    onHistorySearchChange() {
      clearTimeout(this._historySearchTimer);
      this._historySearchTimer = setTimeout(()=>this.loadHistory(), 300);
    },
    async bulkStar(starred=true) {
      const ids = [...this.historySelected];
      await Promise.all(ids.map(id=>
        fetch(HUB+`/api/history/${id}/star?starred=${starred}`,{method:'POST'}).catch(()=>{})
      ));
      ids.forEach(id=>{ const item=this.historyList.find(i=>i.id===id); if(item) item.starred=starred; });
      this.historySelected=[]; this.showToast(`已${starred?'收藏':'取消收藏'} ${ids.length} 条`,'success');
    },
    // UH3: 批量验真——交付前整批自证来源（复用单条 verifyHistory 的五档口径，逐条结论落行尾角标）
    async bulkVerify() {
      if(this.bulkVrfBusy) return;
      const items = this.historySelected
        .map(id=>this.historyList.find(i=>i.id===id))
        .filter(i=>i && i.has_audio);
      const skipped = this.historySelected.length - items.length;
      if(!items.length){ this.showToast('所选记录都没有音频，无法验真','error'); return; }
      this.bulkVrfBusy=true; this.bulkVrfSummary=null;
      let done=0; const sum={ok:0, warn:0, none:0, skipped};
      // 2 路并发：每条=取回 base64 + 服务端水印解码（CPU 活），全并发会打爆 Hub
      const queue=[...items];
      const worker=async()=>{
        while(queue.length){
          const it=queue.shift();
          await this.verifyHistory(it);
          done++; this.bulkVrfProg=done+'/'+items.length;
          const v=it._vrf||'';
          if(v.startsWith('✅')) sum.ok++;
          else if(v.startsWith('⚠')) sum.warn++;
          else sum.none++;
        }
      };
      await Promise.all([worker(), worker()]);
      this.bulkVrfSummary=sum; this.bulkVrfBusy=false; this.bulkVrfProg='';
      const bits=['✅ '+sum.ok]; if(sum.warn) bits.push('⚠ '+sum.warn); if(sum.none) bits.push('— '+sum.none);
      this.showToast('批量验真完成：'+bits.join(' · '), sum.warn?'error':'success');
    },
    async bulkDelete() {
      const ids = [...this.historySelected];
      if(!confirm(`确删除已选 ${ids.length} 条历史记录？`)) return;
      await Promise.all(ids.map(id=>
        fetch(HUB+`/api/history/${id}`,{method:'DELETE'}).catch(()=>{})
      ));
      this.historyList=this.historyList.filter(i=>!ids.includes(i.id));
      this.historySelected=[]; this.historyMultiSelect=false;
      this.showToast(`已删除 ${ids.length} 条`,'success');
      this.loadHistoryStats();
    },

    // 开播
    streaming:false, streamSteps:[], mjpegOn:false, streamNonce:Date.now(),
    cameras:[], selectedCamera:-1,
    phoneGuideOpen:false,   // 开播页·手机开播向导 折叠态
    rawPeek:false,          // 失败卡内联"原始摄像头实拍"开关（未检测到人脸时一眼看出是否没对准）
    swapWin:null, _swapPrev:null,   // 换脸成功/失败的近窗增量（识别"中途丢脸"，不只"从未换上"）
    swapPs:{ok:0,fail:0},   // 每秒换脸成功/失败速率（后端源头计算，专家诊断展示）
    streamHealthBE:null,    // 后端开播健康裁决（单一真相；前端优先读它，缺失才本地兜底）
    healthState:'', healthSince:0, healthDwell:0, _lastOkToast:0,   // 开播健康驻留计时 + 恢复提示防抖
    freeVramBusy:false,     // 「一键腾显存」进行中
    // 复盘时间线（P3-C③）：按需拉取服务端守护记录的「状态跳迁 + 自愈动作」，无人值守也照常累计
    healthTLOpen:false, healthTLLoading:false, healthTimeline:[], healthStats:null, healthAutohealOn:false, healthSessions:[],
    // 服务端自愈/告警的运行时开关（经 /api/heal/config 读写，免改环境变量、免重启灰度）
    autoHealServer:false, healAlertsOn:true, healCfg:null, testAlertBusy:false,
    devAutoSwOn:false,    // P6-1 设备缺席自动热切（服务端守护执行，无人值守；默认关）
    idleLiveGovOn:true,   // Song-P6 空转直播治理（无人上镜30min/画面死更10min→自动下播；默认开）
    dangerArm:'', _dangerArmTimer:null,   // 开播页 P0-1：停止/在播重启二次确认（3s 回弹）
    _latHist:[], monitorCustomOpen:false, heroTipsOpen:false,
    webhookHintDismissed:(function(){ try{ return localStorage.getItem('hub_webhook_hint_dismiss')==='1'; }catch(_){ return false; } })(),
    _sensPresets:{loose:{lat_high:1000,lat_mid:650,fail:15},standard:{lat_high:800,lat_mid:500,fail:10},tight:{lat_high:650,lat_mid:400,fail:8}},
    _visBound:false,
    exportBusy:false,
    // P7: ?lt=1 强制开启（uivr 大字过目截图用——headless 全新 profile 无 localStorage，真实用户不带此参）
    largeText:(function(){ try{ return /[?&#]lt=1/.test(location.href) || localStorage.getItem('hub_large_text')==='1'; }catch(_){ return false; } })(),
    // P3 Web Share：能否把 PNG 文件交给系统分享面板（安卓/iOS/Win10+ 支持；不支持则按钮不出现，仍走下载）
    webShareOk:(function(){ try{ return !!(navigator.canShare && navigator.canShare({files:[new File([''],'x.png',{type:'image/png'})]})); }catch(_){ return false; } })(),
    lic:null,                  // P4 授权状态快照（licChip 轮询经 bd-lic 事件广播过来，单源不重复拉）
    _autoSwSeenTs:0,      // P7-1 已感知的最近自动热切事件时间戳（防重复 toast）
    _autoSwBaselined:false,  // P7-1 首轮轮询已建基线（页面打开前的旧事件不重放）
    autoSwFail:null,      // P8-1 自动热切失败自救卡（常驻直到转换恢复/切换成功/手动✕；含失败原因）
    // P7-2 「建议开启自动热切」：后端按人工战绩裁决(≥3次缺席+≥3次点击+成功率≥95%)；
    //   一次性提示——开启或点「不再提示」后永久退场（localStorage）
    autoSwAdvice:null,
    autoSwHintDone:(function(){ try{ return localStorage.getItem('hub_autosw_hint_done')==='1'; }catch(_){ return false; } })(),
    // ── 自愈策略表（P3）：单源声明"状态→失败卡动作"与"状态×驻留秒数→自动动作"，UI 与轮询都读它 ──
    autoHeal: (function(){ try{ return localStorage.getItem('hub_autoheal')==='1'; }catch(_){ return false; } })(),  // 无人值守自动自愈：默认关
    healLog:[], _healEpisode:{}, _healCooldown:{}, _healCount:{},   // 执行轨迹 / 本回合已执行(oncePerEpisode) / 冷却 / 本场次数
    healPolicy:{
      svc_down:[ {key:'goSettings', label:'去「设置」检查换脸地址 →'} ],
      stalled:[
        {key:'rawPeek', label:'👁 看原始画面', labelOff:'收起原始画面', toggle:'rawPeek'},
        {key:'restart', label:'🔄 换源重连(重新开播)', disableWhenBusy:true},
      ],
      noface:[
        {key:'rawPeek', label:'👁 看原始画面', labelOff:'收起原始画面', toggle:'rawPeek'},
        {key:'restart', label:'🔄 换源重连(重新开播)', disableWhenBusy:true},
      ],
      lag:[ {key:'downgrade', label:'⚡ 一键降档'} ],
    },
    healAuto:[
      {state:'noface', dwellSec:8,  key:'autoRaw', oncePerEpisode:true},                  // 非破坏：始终开（仅展开原始画面）
      {state:'noface', dwellSec:30, key:'restart', requires:'autoHeal', guardBusy:true, destructive:true,
       cooldownSec:90, maxPerSession:2, trail:'无脸过久·自动换源重连'},                    // 破坏类：需开关 + 冷却/限次护栏；服务端接管时让渡
    ],
    // P3/P4 音频自愈：推流中「变声服务(6242)掉线」(=变声链路断=直播必没声) → 自动拉起变声 api 恢复声音。
    //   信号取 services.rvc（/health 用 /inputDevices 探，准确）——不是 rvc_running(那追 gui_v1.py 手动路径，标准开播不用、恒 false 会漏判)。
    //   动作 startRvcApi 幂等(端口活着即 no-op)；主播停顿不会让端口掉线，故无"没说话"误触。受「自动自愈」开关 + 持续掉线/冷却/限次护栏约束；仅动变声，不碰视频。
    healAutoAudio:[
      {dwellSec:20, key:'audioRecover', requires:'autoHeal', cooldownSec:120, maxPerSession:3, trail:'变声服务掉线·自动拉起'},
    ],
    streamSimple: (function(){ try{ return localStorage.getItem('hub_stream_simple')!=='0'; }catch(_){ return true; } })(),  // 开播页 用户(true)/专家(false)：默认用户模式，只露关键操作
    // [v2 面板化] 开播页设置面板折叠状态（localStorage 记忆）：fx=效果调节 visual=视觉效果 dev=设备 rvc=变声 pre=前置检查
    panelOpen: (function(){ const def={fx:true, visual:false, dev:false, rvc:false, pre:false, monitor:false};
      try{ return Object.assign(def, JSON.parse(localStorage.getItem('hub_stream_panels')||'{}')); }catch(_){ return def; } })(),
    showPerfDetail:false,   // [收敛] 技术指标改由专家模式驱动；保留变量避免历史引用报错
    previewTick:0, _previewTimer:null,   // 内嵌 换脸前/后 预览的刷新心跳（mjpegOn 时启停）
    // 数字人模式(avatar_lipsync)：实时画面卡改走 vcam 广播预览（与 OBS 虚拟摄像头同一路画面）。
    // vcamPrev=引擎可达性+OBS 摄像头就绪(3s 轮询)；nonce 在(重新)开播/引擎恢复时换值，强制 MJPEG 重连。
    vcamPrev:{reachable:false, enabled:false, device:{}, checked:false},
    vcamPrevNonce:Date.now(), _vcamPrevTimer:null,
    // P1-B 连接状态条·实时信号：视频活帧(fps/健康态) + 音频监听电平(rms)，stream 页 ~1.1s 快轮询 /realtime/signal
    signal:{video:{live:false,fps:0,state:'',tone:''}, audio:{ok:false,rms:0,dev:''}, cable:{ok:false,rms:0,dev:''}},
    audioActiveTs:0, _signalTimer:null,   // 最近有声时刻(衰减平滑，避免字间静音闪烁) + signal 轮询计时器
    cableActiveTs:0, streamingSinceTs:0, _wasStreaming:false,   // P1-F 广播馈线(CABLE)最近有声时刻 + 本轮开播起点(判「直播端持续无声」)
    cableOkTs:0,   // P1-F+ 馈线探针最近一次「可信(能读到电平)」时刻 → 判「探针长时测不到=测不到直播声」
    _rvcApiSeenUp:false,   // P3/P4 本场变声服务(6242)是否曾在线 → 没配好音频的场次不越权乱拉
    _rvcApiDownSince:0,    // P3/P4 变声服务连续掉线起点(ms) → 须持续≥dwell 才动手，滤瞬时探测抖动
    preflightOpen:true,   // (旧 P2 预检卡展开态；S1 就绪度组件接管后保留变量防历史引用)
    readyOpen:false,      // S1 就绪度清单展开态（有 fail 项时强制展开）
    fixState:{},          // S3 修复动作·原地反馈 {key:{busy,note,ok}}
    _fixTimers:{},        // S3 反馈文案自动消隐计时器
    fixAllBusy:false,     // S3 「修复全部」进行中
    cableWiz:{show:false, busy:false, note:'', found:false},   // S3 VB-Cable 虚拟声卡安装向导
    preflightGate:{show:false, blockers:[]},   // P2.1 开播按钮·预检拦截弹窗
    selfCheck:{show:false, loading:false, text:''},   // P5 一键开播自检报告(预检+四路信号+服务/显存/设备/自愈→可复制文本)
    videoModal:false,  // 视频放大modal
    toasts:[],           // 通知队列
    _toastSeq:0,         // 自增 id（替代 Date.now，杜绝同毫秒撞 :key）
    _toastsPaused:false, _toastTick:null,   // 悬停暂停自动消失 + 单一倒计时心跳句柄

    // 导入/导出
    importShow:false, importFile:null, importData:null,
    importPreview:null, importMode:'skip', importLoading:false,
    exportSelected:[], exportSelectAll:false,
    batchMode:false,      // P2(UI改版): 批量选择模式——平时隐藏复选框去杂乱，开启后整卡点击=选择
    pkgImportForce:false, pkgImportMsg:'', pkgImportOk:false,
    // 导入两步流：选包→轻预览（清单/体积/加密态/覆盖预警）→确认才落库；.ahpkg 加密包凭口令
    pkgFile:null, pkgPeek:null, pkgPw:'', pkgPeekLoading:false, pkgBusy:false,

    // P22-2: 长任务进度存储（WebSocket推送）
    taskProgress:{},  // {task_id: {type, total, done, pct, status}}
    activation:{ name:'', stage:'' },  // 单一事实源：当前激活流水线 stage∈ activating|preparing|failed（''=就绪/空闲，HTTP 与 WS 协同只写此处）
    faceElapsed:0, _faceTimer:null,    // 准备中秒级计时（卡片/横幅显示"准备中… Ns"，消除卡住焦虑）
    faceEta:18,                        // 人脸预计算 ETA（后端 EMA 自校准秒数），驱动确定性进度条
    selfcheck:{doctor:null, delivery:null},  // 交付体检面板数据
    selfcheckLoading:false,
    svcCatalog: [],       // UK1: /api/services/catalog 服务目录（label/port/core/env/script，单一真相）
    svcStartBusy: '',     // UK1: 正在一键启动的服务名（含就绪轮询期，串行防并发拉起）
    selfcheckFocus: '',   // UK1: 来源聚焦——从哪条「去启动」跳来，高亮对应服务行

    // 角色搜索与排序
    profileSearch:'',
    profShowN:30,        // P2: 网格增量渲染——每张卡约15个DOM节点，导入几十个音色后一次全渲会顿
    profilesLoaded:false, // P2: 角色首次是否加载完成（控制骨架屏）
    profilesLoadError:false, // 加载 /profiles 失败(后端未起/断连)→ 空态显示"连不上"而非"没角色"
    showTour:false,       // P4: 首访上手引导气泡
    smhDismissed:false, smhOpen:false,  // P3: 「静态口型属正常」认知引导——已关闭(持久化) / 手动再唤出
    createHubShow:false,  // P1(UI改版): 创建中心——统一「新建数字人」方式选择（卡片化）
    createHubMode:'',     // 创建中心两步流：''=方式选择；'photo'|'video'|'pkg'=在弹窗内直接填表单
    pkgDrag:false,        // P1: 配置包拖拽落区高亮态
    showSvcCfg:false,     // P5: 服务地址配置折叠态（技术面板，默认收起）
    showShortcuts:false,  // B: 键盘快捷键帮助卡（? 键打开 / Esc 关）
    expandedCard:'',     // P1: 点开展开详情的角色名（触屏可用，替代纯 hover）
    menuCard:'',         // P1: 打开操作菜单(编辑/复制/删除)的角色名
    profileSort:'name',   // P21-3: 'name' | 'usage' | 'recent' | 'cosine' | 'naturalness'
    profileUsage:{},     // P21-3: {profileName: count}
    qualityFilter:'all', // 双轴筛选: 'all' | 'cos' | 'nat' | 'both'
    // VL1 三库分区：'all' | 'human'(数字人=脸+声) | 'photo'(照片=有脸待配声) | 'voice'(声音=纯声资产)
    //   选择跨会话记忆；'draft'(两者皆无) 不单列页签——并入「全部」，数量极少不值一个入口
    profileLib:(function(){ try{ return localStorage.getItem('hub_prof_lib')||'all'; }catch(_){ return 'all'; } })(),
    qpLoading:'',        // 体检中的角色名
    probeElapsed:0,      // 体检已用秒数（用于进度反馈）
    _probeTimer:null,    // 体检计时器句柄
    get probeStageText(){ // 根据已用时长给出阶段文案，消除"无反馈"焦虑
      const s=this.probeElapsed;
      if(s<6) return '正在准备标准句…';
      if(s<16) return '正在合成语音样本…';
      if(s<30) return '正在评分（音色 / 自然度）…';
      return '即将完成，正在汇总…';
    },
    probeAllRunning:false, probeAllProgress:'',  // 全员体检状态
    qualityAlerts:{},    // {profileName: alertObj} 自然度回归
    onboardShow:false, onboardStep:1, onboardProfile:'', onboardProbing:false,

    // 告警分流：面向用户(进首屏横幅) vs 运维/安全(进系统状态弹层)
    get userAlerts() { return (this.svcAlerts || []).filter(a => (a.audience || 'user') === 'user'); },
    get opsAlerts()  { return (this.svcAlerts || []).filter(a => a.audience === 'ops'); },
    // P4: 品牌主色设置（实时改 CSS 变量 + 持久化）
    setBrand(rgb) {
      this.brand = rgb;
      document.documentElement.style.setProperty('--hub-blue', rgb);
      // 同源：经白标配置统一落库 + 应用（--bd-acc 全站 bd-* 令牌跟随）；旧环境降级。
      try {
        if (window.__brandConfig) window.__brandConfig.set({color: rgb});
        else { document.documentElement.style.setProperty('--bd-acc', 'rgb('+rgb+')'); localStorage.setItem('avatarhub_brand', rgb); }
      } catch(_){}
    },
    setBrandHex(hex) {
      const m = (hex||'').replace('#','');
      if (m.length < 6) return;
      const r=parseInt(m.slice(0,2),16), g=parseInt(m.slice(2,4),16), b=parseInt(m.slice(4,6),16);
      this.setBrand(`${r} ${g} ${b}`);
    },
    // P4: 关闭首访引导（记 localStorage，不再弹）
    dismissTour() { this.showTour=false; try{ localStorage.setItem('avatarhub_seen_tour','1'); }catch(_){} },
    // P4: 无真人缩略图时的渐变首字母头像（品牌色渐变 + 姓名首字）
    avatarSeed(name) {
      let h=0; for (const ch of (name||'')) h=(h*31+ch.charCodeAt(0))>>>0;
      const hue=h%360;
      return { initial:(name||'?').trim().charAt(0).toUpperCase(),
               bg:`linear-gradient(135deg, hsl(${hue} 60% 45%), hsl(${(hue+40)%360} 65% 38%))` };
    },
    // 服务名→中文（永不出现 undefined：未知服务回退原名）
    svcName(name) { return this.svcLabel[name] || name; },
    // 系统状态汇总：核心全在→green；核心有缺→red；仅可选缺→yellow
    get svcSummary() {
      const s = this.services || {};
      const b = this.broadcast || null;
      // 模式感知：核心=后端按开播模式给的单一真相；na=本模式不参与(掉线不报警/不计数)。无后端数据时回退旧静态表。
      const core = (b && b.core && b.core.length) ? b.core : this.coreSvcs;
      const na   = (b && b.na) ? b.na : [];
      // 核心定生死：核心全在=「系统正常」(绿)，核心有缺=「核心服务异常」(红)。
      // 可选/备用引擎离线只进弹层、不把徽标拉黄——杜绝"一堆未用引擎离线→半数飘红"的焦虑误报。
      let coreDown = [], optDown = [], up = 0;
      for (const n of Object.keys(s)) {
        if (na.includes(n)) continue;                 // 本模式不需要→不计入
        if (core.includes(n)) { if (s[n]) up++; else coreDown.push(n); }
        else if (!s[n]) optDown.push(n);              // 可选离线：仅供弹层展示与计数
      }
      const level = coreDown.length ? 'red' : 'green';
      return { level, up, total: core.length, coreDown, optDown, mode: b && b.mode };
    },
    // 弹层分组：核心 / 可选 / 本模式不需要，附中文名与端口
    get svcGroups() {
      const s = this.services || {};
      const bc = this.broadcast || null;
      const core = (bc && bc.core && bc.core.length) ? bc.core : this.coreSvcs;
      const na   = (bc && bc.na) ? bc.na : [];
      const C = [], O = [], N = [];
      for (const n of Object.keys(s)) {
        const row = { name: n, label: this.svcName(n), port: this.svcPort[n] || '', ok: !!s[n] };
        if (na.includes(n)) N.push(row);
        else if (core.includes(n)) C.push(row);
        else O.push(row);
      }
      const byOk = (a, b) => (a.ok === b.ok ? 0 : a.ok ? 1 : -1); // 故障置顶
      return { core: C.sort(byOk), opt: O.sort(byOk), na: N.sort(byOk) };
    },
    // VL1: 角色归库（后端 lib 字段权威；旧后端未重启时前端按 has_face/has_voice 同口径兜底）
    libOf(p) {
      return p.lib || (p.has_face ? (p.has_voice ? 'human' : 'photo')
                                  : (p.has_voice ? 'voice' : 'draft'));
    },
    // VL1: 三库计数（吃搜索/质量筛选之前的全量口径——页签数字代表库存，不随筛选跳变）
    get libCounts() {
      const c = {all: this.profiles.length, human: 0, photo: 0, voice: 0, draft: 0};
      this.profiles.forEach(p => { c[this.libOf(p)] = (c[this.libOf(p)] || 0) + 1; });
      return c;
    },
    setLib(v) {
      this.profileLib = v; this.profShowN = 30;
      try{ localStorage.setItem('hub_prof_lib', v); }catch(_){}
    },
    // 计算属性: 过滤并排序后的角色列表
    get filteredProfiles() {
      let list = this.profiles;
      // VL1 三库分区筛选（在搜索之前：先选库再搜，心智同语音页「先选男女声再搜」）
      if (this.profileLib !== 'all') {
        list = list.filter(p => this.libOf(p) === this.profileLib);
      }
      // 搜索过滤
      if (this.profileSearch.trim()) {
        const q = this.profileSearch.toLowerCase();
        list = list.filter(p =>
          p.name.toLowerCase().includes(q) ||
          (p.description && p.description.toLowerCase().includes(q))
        );
      }
      // 双轴筛选：按音色/自然度达标过滤
      if (this.qualityFilter === 'cos') {
        list = list.filter(p => (p.quality_axes?.cosine||0) >= 0.6);
      } else if (this.qualityFilter === 'nat') {
        list = list.filter(p => (p.quality_axes?.naturalness||0) >= 0.85);
      } else if (this.qualityFilter === 'both') {
        list = list.filter(p => (p.quality_axes?.cosine||0) >= 0.6 && (p.quality_axes?.naturalness||0) >= 0.85);
      }
      // P21-3: 排序
      if (this.profileSort === 'usage') {
        list = [...list].sort((a,b)=>{
          const ua=this.profileUsage[a.name]||0, ub=this.profileUsage[b.name]||0;
          if(ub!==ua) return ub-ua;  // 使用次数降序
          return a.name.localeCompare(b.name);  // 次数相同按名称
        });
      } else if (this.profileSort === 'recent') {
        list = [...list].sort((a,b)=>{
          const ra=a.voice_quality?.ts||0, rb=b.voice_quality?.ts||0;
          if(rb!==ra) return rb-ra;  // 最近克隆时间降序
          return a.name.localeCompare(b.name);
        });
      } else if (this.profileSort === 'cosine') {
        list = [...list].sort((a,b)=>{
          const ca=a.quality_axes?.cosine||0, cb=b.quality_axes?.cosine||0;
          if(cb!==ca) return cb-ca;
          return a.name.localeCompare(b.name);
        });
      } else if (this.profileSort === 'naturalness') {
        list = [...list].sort((a,b)=>{
          const na=a.quality_axes?.naturalness||0, nb=b.quality_axes?.naturalness||0;
          if(nb!==na) return nb-na;
          return a.name.localeCompare(b.name);
        });
      }
      // 'name' 默认按名称排序
      return list;
    },
    // P2: 网格只渲染前 profShowN 张，其余点「显示更多」再上（音色库批量导入后角色可到上百个）
    get visibleProfiles(){ return this.filteredProfiles.slice(0, this.profShowN); },

    // 摄像头热重载状态
    lastCamIdx:-1, camStatusTs:0,

    // 日志
    logs:[], logsLoading:false, logsFilter:'', logAutoRefresh:false,

    // RVC
    rvcModels:[], rvcAliases:{}, rvcInputDevices:[], rvcOutputDevices:[],   // P2-RA: aliases={rel_id:显示别名}
    rvcWeightsDir:'',
    rvcActive:false,
    devHotBusy:false,     // P3-1 拔插热切进行中（防连点；config→stop→start 全链约 1~2s）
    _rvcRunDevs:{in:'',out:''},   // P3-1 本次转换实际用的设备快照（开始变声/热切时记）：热切成功后 CTA 自动退场
    rvcMsg:'', rvcOk:false,
    rvcPhoneAudioMsg:'',  // P23-1: 手机音频配置提示
    rvc: {model:'', pitch:0, inputDevice:'', outputDevice:'', indexRate:0.5, protect:0.33},
    rvcStatus:null,       // RVC-P1: 引擎 /status 真相（/realtime/status 顺风车；null=引擎离线/旧版）
    _rvcZeroSince:0,      // RVC-P1: 输出电平持续为 0 的起点（说话间隙也是 0，须持续才告警）
    rvcIndexActive:null,  // RVC-P1: 最近一次应用配置后 index 检索是否生效（false=无 .index，贴合度滑块无效）
    rvcAb:{state:'idle', sec:0, busy:false, rawUrl:'', outB64:'', playing:''},  // RVC-P1: 录 5 秒 AB 试听

    // 一键开播前置配置
    videoSource:'auto',        // auto / scrcpy / camera（auto=优先 WiFi 手机 cam.mjpeg）
    phoneRelayCam:{live:false, url:'', fps:0},
    phoneRelay:{reachable:false, ok:false, url:'', https_url:'', show_url:'', audio:{}, mic:{}, cam:{}},
    // 本次开播「实际用了哪个视频源」的解读（后端 auto_reason 单一真相）→ 连接状态条 + 降级显形
    lastVideo:{kind:'', label:'', isPhone:false, degraded:false, reason:'', url:'', wanted:''},
    videoNoticeDismissed:false,   // 用户点「就用本机，知道了」后收起降级提示
    // 一键无线开播编排:等手机端就绪→自动用手机摄像头开播(复用 oneClickStart)
    wireless:{running:false, phase:'', msg:'', checklist:{term:false,audio:false,cam:false}},
    cameraOverride:'',         // [废弃] 已统一到 selectedCamera，保留占位避免历史引用报错
    audioInput:'',             // 选择的音频输入（如 DroidCam Virtual Audio）
    audioOutput:'',            // 选择的音频输出（如 VB-Cable）
    // ── P0 设备人话层（/rvc/devices 的 inputs_ex/outputs_ex：去重/翻译/分组/推荐，后端单一真相）──
    rvcInputsEx:null,          // {devices:[{value,label,group,danger,hidden,variants,..}], raw_count, merged_count, groups, pick}
    rvcOutputsEx:null,
    devShowAll:(function(){ try{ return localStorage.getItem('hub_dev_show_all')==='1'; }catch(_){ return false; } })(),  // 开=完整原始设备列表(排障)
    _prevAudioIn:'', _prevAudioOut:'',   // 危险设备确认取消时的回退值
    micTest:{busy:false, res:null, url:'', ts:0},   // P1 试音（录选中麦 3 秒 → 结论+回放）
    outTest:{busy:false, res:null},           // P1 试听（向选中输出播提示音；CABLE 自带回环自证）
    vbWizard:null, vbWizardBusy:false,        // P1-3 VB-Cable 安装向导（前置检查内联展开）
    // ── P2-1 设备偏好持久化 + 拔插自愈（后端 audio_prefs.json 单一真相）──
    rvcPrefs:null,                       // /rvc/devices 回传的 prefs：{input,input_set,input_present,input_canon,input_label, output…}
    devLost:{in:null,out:null},          // 偏好设备当前缺席：{value,label}；null=在线/没偏好。驱动警告条+回归自愈
    devBack:{in:null,out:null},          // P3-1b 首选已插回但变声还跑在顶替设备上：{value,label,run}。驱动「切回首选」CTA
    _devFlowSeen:{in:false,out:false},   // P4-2 漏斗曝光去重：每个缺席 episode 只记一次曝光
    rvcFreshNote:'',                     // P4-3 转换中新插设备提示（后端 fresh_note；🆕 徽章的说明行）
    _userPickTs:{in:0,out:0},            // 最近一次手动选择时刻：防「在途刷新带回旧偏好」把新选择顶回去
    _lastDevRefreshTs:0,                 // 漂移兜底轮询节流（devicechange 事件之外每 45s 重枚举一次）
    _devChangeT:null,
    // P2-2 最近一次试音摘要（localStorage 持久，进开播就绪度黄灯项）
    _micTestSaved:(function(){ try{ return JSON.parse(localStorage.getItem('hub_mic_test')||'null'); }catch(_){ return null; } })(),
    // P3-2 最近一次「直播声卡回环」试听摘要（同款：7 天内同一路 heard=true 绿灯）
    _outTestSaved:(function(){ try{ return JSON.parse(localStorage.getItem('hub_out_test')||'null'); }catch(_){ return null; } })(),

    // 视频 / 换脸效果参数（默认=「高清脸」档：引擎 hd(512px+GFPGAN精修)+输出85，2026-07-05 拍板）
    videoWidth:1280,
    videoHeight:720,
    videoFps:30,
    swapFps:8,
    mjpegFps:12,
    jpegQuality:85,            // 输出(预览/手机端)JPEG质量；已与送检压缩解耦(送检按引擎档走)
    crossfade:0.3,             // 与「高清脸」预设一致，让默认状态命中 ✓高清脸 而非「自定义」
    faceBlend:1,
    faceThreshold:0,
    faceSmooth:0,
    faceEnhance:'',            // ''=跟随画质档(推荐)；none=强制关；gfpgan/codeformer=强制指定
    enginePreset:'hd',         // 引擎画质目标档 eco/natural/beauty/hd(开播时 swap_preset 下发+在播热切)
    outputAspect:'',           // 输出画面比例 ''/auto=横屏默认 · portrait916/portrait34=手机竖屏 · landscape169/landscape169hd · square11
    swapTier:{up:false},       // /realtime/swap/status 轮询快照(生效档/目标档/降档原因/引擎能力)
    orphanStream:null,         // [P8] 孤儿画面进程摘要(hub无句柄但流在跑,且自动接管未成)：横幅提示一键开播接管
    orphanAdopted:null,        // [06t] 无闪断收养记录(pid/ts)：hub重启后自动认领了在跑的画面进程→chip如实展示
    qc:null, qcBusy:false,     // 画质体检结果/进行中(点按钮才算,不常驻)
    calib:{busy:false, phase:'', msg:'', level:'', saved:null, needs:false, age:null},   // P9 一键画质标定 + P10 过期提醒
    devCheck:null, devBusy:false,   // D-1 设备体检分(麦/摄像头/CABLE→开播总分,点按钮才算)
    hwGuide:null,                   // E-2 硬件档位与每功能能力预期(设置卡,进设置页拉一次)
    holdRaw:false,             // 按住换脸预览=临时看原始镜头(同位 A/B 对比)
    // Phase 12 C：虚拟背景 / 双人换脸 / 发型定妆
    bgCfg:{up:false, mode:'none', image:'', images_available:[], ms:0, enabled:false},
    bgBusy:false,
    // 06v 离席画面：持久存 effect_cfg(开播自动注入)，在播改动经 /realtime/swap/away 即时热切
    awayCfg:{style:'blur', text:'', image:''},
    awayBusy:false,
    bgImagesAll:[],            // /api/bg_images：hub 直读目录清单(不依赖 realtime 存活)
    bgUpBusy:false,            // 背景图上传中（虚拟背景/离席品牌图共用一个入口）
    // 录播增强（MatAnyone 2 离线抠像）：inputs=已上传录播,job=当前/最近任务(轮询驱动)
    ma:{inputs:[], input:'', bg:'green', prores:false, upBusy:false, busy:false, job:{}, history:[], queue:[], running:false, diskFree:-1},
    faceMap:{enabled:false, slots:['','']},
    faceMapBusy:false,
    hairPresetProfile:'', hairPresetBusy:false, hairPresetPreview:'',
    // 换脸+发型跟随源照片（离线出片）：脸来自角色源、发型来自角色源照片、身体背景来自目标图
    faceHair:{targetB64:'', targetPreview:'', targetName:'', targetSource:'upload', assets:[],
              assetsFor:'', carryHair:true, pasteBack:true,
              busy:false, liveStep:'', result:'', hairOk:false, pastedBack:false, histId:'',
              liveVideo:'', liveAnimated:false},
    hairStyles:[], hairStyleSel:'',   // 阶段8：开播页发型直选（与妆容样式同构）
    dfmModels:[], dfmEngineUp:false,   // S2: 可直播 DFM 整脸换角色（换脸引擎 /api/dfm/models 代理）
    dfmHot:{cur:null, busy:false, msg:'', _wasOnReplica:false},   // S4: 当前换脸角色 + 一键预热本场角色（开播区）
    dfmAudit:{issues:[], n_bindings:0, ts:0},   // S5: DFM 库对账（绑定 vs 直播机库存；missing/offline_only）
    fullLookBusy:false, fullLookStep:'', fullLookSteps:[],   // 阶段9：一键出片编排器
    // 阶段10 出片历史：每次定妆/试衣/微动成功自动存档（Hub 端 24 版滚动），点图回滚
    lookHistOpen:false, lookHist:[], lookHistBusy:'', lookHistZoom:'',
    // 阶段11 对比模式：开着时点缩略图=选入对比槽（最多2张并排），关着=看大图
    lookHistCmpMode:false, lookHistCmp:[],
    makeupStyle:'', makeupStyles:[], makeupStylesDetail:{}, lookPackBusy:false, lookPackPreview:'',
    liveMakeup:{enabled:false, lip:'#a51c30', lipS:40, blush:'#f08296', blushS:22, eye:'#6e5550', eyeS:18},
    liveMakeupBusy:false, liveMakeupLoaded:'',
    labSvc:{},                 // /api/lab/services 快照(发型/试衣在线态,C-4 专家入口按真实就绪显隐)
    svcStartBusy:'',           // P1: 正在一键启动的离线服务 key（按钮防连点/显示进行中）
    // C-4 试衣间（FitDiT 后端）：选装→传全身照→预览→写入角色底片
    fitting:{up:false, backend:'', clothes:[], cloth:'', personB64:'', personName:'',
             result:'', busy:false, applyBusy:false, resolution:'768x1024', elapsed:0,
             clothType:'upper',    // 阶段7：试穿部位 upper/lower/dress（抠衣同步跟随）
             stored:false},        // 阶段12：该角色有存照（全身照记忆，换装免重传）
    fittingExtractBusy:false,  // 截图抠衣（穿着照/商品截图→白底服装入库）
    fittingExtractProg:'',     // 批量抠衣进度文案（"2/5…"）
    fittingSearch:'',          // 服装库搜索词（库过百件后的管理刚需）
    fittingPage:0,             // 服装库当前页（0 起）
    fittingPageSize:12,        // 每页缩略图数
    idleMotionBusy:false,      // 待机微动独立入口（任意角色照→Ditto 微动视频）
    // 阶段15 动态试衣（CatV2TON 视频换装，作业制 ~2-6 分钟）：待机微动视频+服装→整段换装视频
    vtryon:{jobId:'', state:'', progress:0, detail:'', busy:false, preview:'',
            applied:false, applyBusy:false, elapsed:0, timer:null},

    // TTS 试听
    previewAudioLoading:false,

    // 资产管理面板（声音 / RVC 变声模型 / 回收站，含引用关系）
    vaShow:false, vaLoading:false, vaAssets:[], vaEmbedded:[], vaPlayId:'', vaBusy:'',
    vaTab:'voice', vaRvc:[], vaRvcTotal:0, vaTrash:[], vaTrashTotal:0,
    vaRvcUp:false, vaRvcPreviewBusy:'',   // 变声试听：引擎在线位 + 正在转换的模型 id
    vaPresetId:'', vaPresetForm:{pitch:0,index_rate:0.75,protect:0.33,f0method:'rmvpe'}, vaPresetBusy:false,  // 模型默认参数：卡片内联展开编辑
    vaPreviewSample:'',   // 变声试听输入：''=库存样本人声；角色名=用该角色参考音（听到≈实际开播）
    trashPolicy:{auto_clean:false, max_mb:500, older_days:30, last_clean_ts:0, last_clean_n:0, total_cleaned:0}, trashPolicyBusy:false,  // 回收站自动清理策略（服务端持久化）
    vaQuery:'',           // 资产面板搜索：一个输入过滤四页签（名字 / 绑定角色 / 原名 / 音色库）
    // 真人音色库页签（voice_pack_aishell3 只读素材包：浏览/试听/一键导入为角色）
    vpRows:[], vpPlayId:'', _vpAudio:null, vpBusy:'',
    vpGender:'', vpBand:'', vpHq:false, vpSort:'snr', vpPage:0,
    vpFavs:{}, vpFavOnly:false,   // 收藏（服务端持久化，跨浏览器一致）：{spk:1} + 只看收藏开关
    vpPvText:'', vpPvTpl:'welcome', vpPvTemplates:[], vpPvStaleN:0,  // P4: 克隆试听台词（场景模板）
    vpPvEdit:false, vpPvDraft:'',  // P5: 自定义台词编辑态（客户贴自己的话术，比模板更有代入感）
    vpUpOpen:false, vpUpLabel:'', vpUpGender:'auto', vpUpBusy:false, vpUpWarns:[],  // P6: 音色上新
    vpRec:'idle', vpRecSec:0, vpRecTakes:[], vpRecLevel:0,  // P9/P10: 麦克风试录（多段累积，一起入库）
    vpCat:{open:false,url:'',items:[]},  // P10: 远程分发清单（从 URL 导入）
    vpDistProtected:false,  // P11: 服务端设了分发口令（VP_DIST_TOKEN）→ 导出前要口令
    vpSubs:{open:false,list:[],newUrl:'',busy:false,autoHours:6,n:0},  // P12: 订阅式自动同步
    vpRepOpen:false, vpRep:null, vpRepBusy:false,  // P13: 运营小报表
    vpTrashN:0, vpTrashOpen:false, vpTrashItems:[],  // P7: 上新回收目录（还原/彻底删除）
    vpTier:{mode:'off',locked_n:0},  // P8: 分层解锁（off=全解锁；featured_free=长尾仅试听）
    vaSel:{},             // 批量勾选集：键=回收站 't:kind:name' / 声音页资产 id；刷新后自动清空
    voiceHealth:{},       // 角色素材链路体检（声音+形象视频）{角色名:{level:'ok|warn|bad', issues:[{code,sev,fix,text}]}}
    vhBusy:'',            // 体检修复动作进行中（角色名）
    assetHealth:null, nudgeDismissed:false,   // 资产巡检轻横幅（孤儿克隆音/回收站占用超阈值时角色库页提示）

    // 导出配置包选项面板（勾选人脸/变声模型 + 体积预估 + 可选口令加密）
    expShow:false, expName:'', expInfo:null, expLoading:false, expFace:false, expRvc:false, expPw:'',

    // 环境
    envChecks:[],
    kbCount:0, kbLoading:false, kbMsg:'', kbOk:false,
    kbUploadProfile:'阿习讲话', kbSearchQ:'', kbSearchHits:[], kbSearchLoading:false,

    // ── 初始化 ──
    async init() {
      try{ sessionStorage.removeItem('hub_boot_healed'); }catch(_){}   // 启动成功→复位自愈守卫，使日后异常仍可自愈
      // 端口单一真相(2026-07-17)：跨端口链接(同传等)以本套安装的 /api/ports 为准——
      // 两套安装并存(端口偏移)时不再硬编码 7900 指到别家服务。失败保持内置默认(零回归)。
      fetch('/api/ports').then(r=>r.json()).then(p=>{
        if(p&&p.ports){ this.svcPorts=p.ports; this.portsInfo=p;
          if(p.ports.interpreter) this.interpUrl='http://'+location.hostname+':'+p.ports.interpreter+'/'; }
      }).catch(()=>{});
      try { const b=localStorage.getItem('avatarhub_brand'); if(b) this.setBrand(b); } catch(_){}  // P4: 应用已保存品牌主色
      try{ if(this.largeText) document.documentElement.classList.add('bd-large-text'); }catch(_){}
      // P4 授权单源：licChip 脚本每 60s 拉 /api/license/status 并广播，这里只订阅（含 Alpine 启动晚于首播的补读）
      try{ this.lic=window.__bdLic||null; window.addEventListener('bd-lic', e=>{ this.lic=(e&&e.detail)||null; }); }catch(_){}
      // [uivr] 可视化回归模式(ui_visual_regress.py 截图带 ?uivr=1)：抑制一切"首访"浮层。
      //   无头截图每次都是全新浏览器 profile，首访浮层是否赶在截图前弹出取决于异步数据竞速
      //   (2026-07-06x 实锤:「三步开始对话」在 5/11 尺寸随机入镜)——回归基线必须是确定性的
      //   回头客稳态视图。真实用户 URL 不会带此参数，产品行为零影响。
      this._uivr = /[?&#]uivr=1/.test(location.href);
      try { if(!this._uivr && !localStorage.getItem('avatarhub_seen_tour')) this.showTour=true; } catch(_){}      // P4: 首访上手引导
      try { this.smhDismissed = localStorage.getItem('hub_seen_staticmouth_hint')==='1'; } catch(_){}  // P3: 恢复「静态口型正常」引导关闭态
      // [S6 老操作者免打扰] 本浏览器点过开播方式(hub_broadcast_mode 仅在用户点选时写入)=不是第一次开播 → 三步引导直接退场
      try { if(!this.streamGuideDone && localStorage.getItem('hub_broadcast_mode')) this.guideDismiss(); } catch(_){}
      // [P7 走查修复·2026-07-16] hashchange 跟随：已打开的控制台里改 URL 锚点（外部深链/手改地址栏）
      // 此前不会切页签（重启走查实锤：location.hash 变更被静默忽略，仅首载读取一次）。
      // goTab 内部写回相同 hash 不再触发本监听（值相同浏览器不发事件），无循环。
      try {
        window.addEventListener('hashchange', () => {
          const _hid = (location.hash || '').replace('#', '');
          const _ht = (this.tabs || []).find(x => x.id === _hid && !x.href);
          if (_ht && this.tab !== _hid) this.goTab(_hid);
        });
      } catch(_){}
      // [同步定 Tab] 开屏即定初始 Tab（hash → 上次持久化 → 默认），置于任何 await 之前：
      //   ① 消除刷新时"先闪 角色库 再回上次 Tab"的抖动；② 让 /ui#clone 等深链首帧即命中（懒挂载按 visitedTabs 渲染，故同步入列）。
      //   仅依赖 this.tabs/visitedTabs 静态数据属性，与下方数据加载无耦合。
      try {
        const _hash0 = (location.hash || '').replace('#', '');
        const _tabIds0 = (this.tabs || []).filter(t => !t.href).map(t => t.id);   // 跳出型页签（对话）无面板，不参与初始定 Tab
        if (_hash0 === 'streamout') { this.tab = 'voice'; this.soShow = true; }
        else if (_tabIds0.includes(_hash0)) this.tab = _hash0;
        else { const _saved = localStorage.getItem('hub_tab'); if (_saved && _tabIds0.includes(_saved)) this.tab = _saved; }
        if (!this.visitedTabs.includes(this.tab)) this.visitedTabs.push(this.tab);
      } catch(_){}
      // [产品视图 v2] 配置驱动侧栏裁剪（product_views/*.yaml → GET /api/product_view，契约见 product_views/APPLY.md）。
      //   fire-and-forget 置于「同步定 Tab」之后：未启用（未设 AVATARHUB_PRODUCT_ID）/接口缺失/异常 → 不进过滤分支，
      //   零行为变化零首帧等待（fail-open）；启用时到货即裁剪，当前 Tab 被隐藏则落视图默认页（走 goTab 补 visitedTabs/hash）。
      try {
        fetch(HUB+'/api/product_view').then(r=>r.json()).then(pv=>{
          try {
            if (!pv || !pv.ok || !pv.enabled || !Array.isArray(pv.allowed_tabs) || !pv.allowed_tabs.length) return;
            const _allow = new Set(pv.allowed_tabs);
            const _trimmed = (this.tabs || []).filter(t => _allow.has(t.id));
            if (!_trimmed.length) return;   // 白名单与侧栏 id 零交集 → 视为配置错，保持全菜单
            this.tabs = _trimmed;
            const _ids = _trimmed.map(t => t.id);
            if (!_ids.includes(this.tab)) this.goTab(_ids.includes(pv.default_tab) ? pv.default_tab : _ids[0]);
          } catch(_){}
        }).catch(_=>{});
      } catch(_){}
      await Promise.all([this.loadProfiles(), this.loadVoices(), this.refreshServices(),
                         this.loadSysInfo(), this.rvcRefreshModels(), this.checkEnv(),
                         this.loadCameras(), this.loadConfig(), this.soRefresh(), this.loadPhoneRelayStatus()]);
      this._syncFaceReady();   // P4: 首屏回填就绪态（刷新后正 preparing/降级 立即可见，不等下次事件）
      this.loadHealConfig().then(()=>this.checkAutoSwAdvice());   // 服务端自愈/告警开关现状（驱动专家区开关）→ 随后取 P7-2 开启建议（依赖开关现状判显隐），fire-and-forget
      this.loadEffectConfig();
      this.calibLoadSaved();   // P9: 已保存的画质标定结果(专家区展示"开播自动采用"行)，fire-and-forget
      this.loadFeatures();   // P1: 拉功能注册表，喂命令面板跨页跳转（fire-and-forget，非关键）
      this.loadVoiceRecent();   // UI-P2-4: 语音页"最近合成"内嵌入口，fire-and-forget
      try{ this.nudgeDismissed = sessionStorage.getItem('hub_asset_nudge_dismissed')==='1'; }catch(_){}
      this.loadAssetHealth();   // 资产巡检轻横幅数据（孤儿/回收站占用），fire-and-forget
      this._restoreSpeakResults();   // UI-P4-3: 刷新后从 sessionStorage 找回结果卡引用（音频按需取回）
      this.loadBroadcastMode();   // 开播模式(真人换脸/数字人)现状 + 是否可双路同开，fire-and-forget
      this.refreshDfmCur();   // S4: 当前换脸角色（DFM）+ 引擎可达性，开播区「预热本场角色」条显隐用，fire-and-forget
      this.refreshDfmAudit();   // S5: DFM 绑定对账（绑定失效/仅离线档 → 角色库横幅+卡片黄标），fire-and-forget
      // 读取已保存的摄像头索引
      try { const d=await fetch(HUB+'/realtime/camera').then(r=>r.json()); this.selectedCamera=d.index; } catch(e){}
      // 初始 Tab 已在 await 之前同步敲定（见上「同步定 Tab」），此处不再重复处理，避免二次赋值/闪动。
      const _qs = new URLSearchParams(location.search);
      const _prof = _qs.get('profile');
      if (_prof && this.profiles.some(p => p.name === _prof)) {
        await this.activateProfile(_prof);
      }
      // UK2: 跨页「去启动」着陆——独立页面(对话页等)带 ?fix=服务名 跳进控制台，直落体检页对应服务行
      const _fix = _qs.get('fix');
      if (_fix && /^[a-z0-9_]{1,32}$/.test(_fix)) this.goFix(_fix);
      if (_qs.get('open') === 'tune') {
        this.goTab('voice');   // R1-2: 走 goTab 补 visitedTabs/hash（原直赋值漏 visitedTabs，懒挂载内容可能不渲染）
        this.vqShow = true;
        this.$nextTick(() => {
          // UI-P1-6: 音质优化卡已面板化，滚动锚点从 .rounded-xl 改挂 .bd-panel
          const el = document.querySelector('[x-data] [x-show="vqShow"]')?.closest('.bd-panel');
          el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
        });
      }
      this.connectWs();
      this.pollHealth();
      this.pollCameraStatus();
      // 监听tab切换，进入logs时加载日志
      this.$watch('tab', (val)=>{
        if(val==='logs'){ this.loadLogs(); this.startLogRefresh(); }
        if(val==='stream'){ this.rvcRefreshDevices(); this.loadCameras(); this.loadPhoneRelayStatus(); this.loadPhoneRelayCam(); this.streamNonce=Date.now(); this.startRtPoll(); this.startSignalTick(); this.startVcamPrevTick(); this.loadBgStatus(); this.loadFaceMap(); this.loadLabServices(); this.loadMakeupStyles(); this.loadAwayCfg(); this.maRefresh(); if(this.mjpegOn) this.startPreviewTick(); }
        else { this.stopPreviewTick(); this.stopSignalTick(); this.stopVcamPrevTick(); }
        if(val==='history'){ this.loadHistory(); }
        if(val==='settings'){ this.loadMetrics(); this.kbRefresh(); this.loadHwGuide(); }
        if(val==='selfcheck'){ this.loadSvcCatalog(); this.refreshServices(); if(!this.selfcheck.doctor) this.runSelfcheck(); }
        else this.selfcheckFocus='';   // UK1: 离开体检页清聚焦，下次非 goFix 进入不残留「你要找的就是它」
        if(val==='interp'){ this.checkInterp(); }
        if(val==='sing'){ if(!this.songCapsLoaded) this.songHealthCheck(); this.songBoardStartPoll(); }   // Song-P1/P5
      });
      if(this.tab==='sing'){ this.songHealthCheck(); this.songBoardStartPoll(); }   // F5 直达唱歌页时 $watch 不触发
      // UH1-4: 同款深链坑补齐——F5/书签直达 #history 列表恒空（库里有记录，loadHistory 没人调）；
      //   直达 #settings 时运行指标/决策①面板全是 0。与上面 sing/stream 的修法同一模式。
      if(this.tab==='history') this.loadHistory();
      if(this.tab==='settings'){ this.loadMetrics(); this.kbRefresh(); this.loadHwGuide(); }
      // UK1: F5 直达 #selfcheck 同款深链坑——doctor 不自动跑、服务目录不装载
      if(this.tab==='selfcheck'){ this.loadSvcCatalog(); if(!this.selfcheck.doctor) this.runSelfcheck(); }
      // 首屏即在开播页（F5/书签直达 #stream）：$watch('tab') 不触发，进页装载得在这补齐——
      // 此前只开了信号轮询，设备/摄像头列表要等切走再切回才加载（P0 顺手修的老坑）
      if(this.tab==='stream'){
        this.startSignalTick();
        this.startVcamPrevTick();
        this.rvcRefreshDevices(); this.loadCameras(); this.loadPhoneRelayStatus();
        this.loadPhoneRelayCam(); this.loadBgStatus(); this.loadFaceMap(); this.loadLabServices();
        this.loadMakeupStyles(); this.loadAwayCfg(); this.maRefresh();
      }
      // P2-1b 拔插自愈：系统音频设备增删时浏览器会发 devicechange（无需麦克风权限），
      // 防抖 1.2s 后重新枚举 → applyAudioPrefs 完成 缺席回退/插回恢复。仅开播页或直播中才响应。
      try{
        if(navigator.mediaDevices && navigator.mediaDevices.addEventListener){
          navigator.mediaDevices.addEventListener('devicechange', ()=>{
            if(this.tab!=='stream' && !this.perf.streaming) return;
            clearTimeout(this._devChangeT);
            this._devChangeT=setTimeout(()=>{ this.rvcRefreshDevices(); this.loadCameras(); }, 1200);
          });
        }
      }catch(_){}
      this.startRtPoll();   // 实时状态轮询常开(WS perf 推送为主源,此环补 swap 档位/健康裁决;幂等防重入)
      // 内嵌预览：随 mjpegOn 启停刷新心跳
      this.$watch('mjpegOn', (val)=>{ val ? this.startPreviewTick() : this.stopPreviewTick(); });
      this._bindVisibility();   // P0-2：后台标签页暂停预览/降频信号轮询
      // 照片建角色：弹窗关闭 / 离开 photo 表单时若本表单仍在录音 → 停止占用麦克风
      this.$watch('createHubShow', (v)=>{ if(!v && this.recording && this._recTarget==='newP') this.stopRec(); });
      this.$watch('createHubMode', (v)=>{ if(v!=='photo' && this.recording && this._recTarget==='newP') this.stopRec(); });
      // 编辑抽屉：关抽屉时若换声面板仍在录音 → 同样释放麦克风
      this.$watch('editShow', (v)=>{ if(!v && this.recording && this._recTarget==='editP') this.stopRec(); });
      this.$watch('logAutoRefresh', (val)=>{ if(val && this.tab==='logs') this.startLogRefresh(); });
      this.$watch('metricsAutoRefresh', (val)=>{ if(val && this.tab==='settings') this.startMetricsRefresh(); });  // P15-1
      // [P5 修正·场次小结] 开播/停播沿改为 tab 无关（perf.streaming 由全局轮询驱动）：无论在哪个页签开/停，时长与小结都精确
      this.$watch('perf.streaming', (now, was)=>{
        if(now && !was){ this.sessStartTs=Date.now(); this.sessPeakFps=0; this.sessUsedRvc=false; this.lastSession=null;
                         this.sessTicksTotal=0; this.sessTicksOk=0;    // 新场次：起点 + 清零累计/上场小结/稳定度采样
                         if(!this.streamGuideDone) this.guideDismiss(); }   // [S6] 首播成功 → 三步引导使命完成，永久退场
        if(!now && was){ this.lastSession={ durSec:Math.max(0,Math.floor((Date.now()-(this.sessStartTs||Date.now()))/1000)),
                                            peakFps:this.sessPeakFps||this.perf.fps||0, usedRvc:!!this.sessUsedRvc,
                                            // [S5 成绩单] 稳定度=健康采样中 ok 占比；采样<5 次(极短场/不在开播页)不妄下结论 → null 显示为 —
                                            stabilityPct:(this.sessTicksTotal>=5)?Math.round(this.sessTicksOk/this.sessTicksTotal*100):null,
                                            endedTs:Date.now() };
                         this._pushSessHist(this.lastSession.stabilityPct);   // P5 稳定度史入环形账本(分享卡趋势第二轨)
                         this.fetchSwapRecap();       // 停播：即时结算 + 后端场次质量报告异步补齐
                         this.fetchHotSwitchRecap(); }  // P5-2 本场设备热切次数/明细入成绩单（账本即时可读）
      });
      // 恢复上次批量配音状态（1小时内有效）
      try{
        const saved = localStorage.getItem('avatarhub_batch_last');
        if(saved){
          const d = JSON.parse(saved);
          if(Date.now()-d.ts < 3600000){
            this.batchText=d.text||''; this.batchLang=d.lang||'zh-cn';
            this.batchEmotion=d.emotion||'neutral';
            this.batchLines=(d.lines||[]);
            this.batchDone=this.batchLines.some(l=>l.status==='ok');
            this.batchRunMs=d.runMs||0; this.batchSavedHist=!!d.savedHist;  // UI-P5-4: 恢复成绩单
          }
        }
        // P18-2: 恢复 audioMap 并自动重建 ZIP
        try {
          const savedAudio = localStorage.getItem('avatarhub_batch_audio');
          if(savedAudio){
            this.batchAudioMap = JSON.parse(savedAudio);
            if(Object.keys(this.batchAudioMap).length > 0)
              this.$nextTick(()=>this._rebuildBatchZip());
          }
        }catch(_){}
      }catch(_){}
      // 实时情感预测：speakText 或 speakEmotion 变化时 debounce 调用
      this.$watch('speakText', val=>{ if(this.speakEmotion==='auto') this._scheduleEmotionDetect(val); });
      this.$watch('speakEmotion', val=>{
        if(val==='auto') this._scheduleEmotionDetect(this.speakText);
        else this.speakDetectedEmotion='';
      });
      // P15-3: 历史无限滚动——用 IntersectionObserver 代替手动点击「加载更多」
      this.$nextTick(()=>{
        const sentinel = this.$refs.historySentinel;
        if(sentinel && 'IntersectionObserver' in window){
          new IntersectionObserver(entries=>{
            if(entries[0].isIntersecting && !this.historyLoading && this.historyHasMore)
              this.loadMoreHistory();
          },{rootMargin:'300px'}).observe(sentinel);
        }
      });
      // P19-5/B: 全局键盘快捷键（Ctrl+Enter 合成 · Esc 关浮层/停流 · ? 帮助）
      document.addEventListener('keydown', e=>{
        const tag = document.activeElement?.tagName;
        const inField = (tag==='TEXTAREA' || tag==='INPUT' || document.activeElement?.isContentEditable);
        if(e.key==='Escape'){
          // Esc 优先关最上层浮层（弹窗/菜单/帮助卡）；没有浮层再停流式合成
          if(this.escClose()){ e.preventDefault(); return; }
          if(this.streamLoading){ e.preventDefault(); this.stopStream(); }
          return;
        }
        // Ctrl/⌘+K 打开命令面板（输入框内也允许）
        if((e.ctrlKey||e.metaKey) && (e.key==='k'||e.key==='K')){
          e.preventDefault(); this.openCmd(); return;
        }
        // ? 打开快捷键帮助（输入框内不拦截，避免影响打字）
        if((e.key==='?' || (e.key==='/' && e.shiftKey)) && !inField){
          e.preventDefault(); this.showShortcuts=true; return;
        }
        if(e.ctrlKey && e.key==='Enter' && !e.shiftKey){
          if(inField) return; // 文本框内 Ctrl+Enter 不拦截（语音文本框自带 @keydown.ctrl.enter → speakGo）
          e.preventDefault();
          if(this.tab==='voice') this.speakGo();   // UI-P0: 跟随当前合成模式，不再固定流式
          else if(this.tab==='batch' && !this.batchLoading) this.doBatch();
        }
      });
      if (!this._uivr && !localStorage.getItem('ah_onboard_v1') && this.profiles.length) {
        this.openOnboard(true);
      }
    },

    needsProbe(p) {
      if (!p || !p.has_voice) return false;
      const q = p.quality_axes || {};
      return !(q.cosine > 0 || q.naturalness > 0);
    },
    // P1: 角色卡综合状态徽标（单一、互斥、按严重度优先），替代一堆角标
    // ── 激活状态机（单一事实源）：HTTP 与 WS 都只经 _setStage 写入，杜绝双源拼装导致的闪烁/残留 ──
    stageOf(name) { return this.activation.name === name ? this.activation.stage : ''; },
    _setStage(name, stage, opts) {
      opts = opts || {};                               // {eta, reason}
      if (!stage) {                                    // 清空 = 就绪/空闲，回落 p.active 徽标
        this.activation = { name:'', stage:'', reason:'' };
        clearInterval(this._faceTimer); this._faceTimer = null;
        return;
      }
      const samePreparing = this.activation.name === name && this.activation.stage === 'preparing';
      this.activation = { name, stage, reason: opts.reason || '' };
      if (opts.eta) this.faceEta = Math.max(4, Math.round(opts.eta));    // 采纳后端自校准 ETA
      if (stage === 'preparing' && !samePreparing) {   // 进入准备中：秒级计时起表
        this.faceElapsed = 0;
        clearInterval(this._faceTimer);
        this._faceTimer = setInterval(()=>{ this.faceElapsed++; }, 1000);
      } else if (stage !== 'preparing') {              // 离开准备中：停表
        clearInterval(this._faceTimer); this._faceTimer = null;
      }
    },
    // 确定性进度%：按 已用/ETA 线性推进，封顶 92%（真正 ready 事件到达才落地并清空），起步 3% 保证条可见
    facePct() {
      const eta = this.faceEta || 18;
      return Math.min(92, Math.max(3, Math.round(this.faceElapsed / eta * 100)));
    },
    // P2-A: 人脸/口型降级的"人话"解释（结合依赖服务在线态），让"静态口型/回退"自解释
    faceDegradeHint() {
      if (this.activation.stage !== 'failed') return '';
      const lipOff = this.services && this.services.lipsync === false;
      if (this.activation.reason === 'service_off')
        return '实时换脸模式：口型预计算服务（lipsync）未启用，按静态口型运行，属正常降级。';
      // P-Harden2: 后端预算超时（服务繁忙/挂起）——先按静态口型出镜，预计算后台继续，稍后重试即秒过
      if (this.activation.reason === 'timeout')
        return '口型预计算超时（服务繁忙或挂起），已先按静态口型运行；后台仍在计算，稍后点「重试」通常立即就绪。';
      return '口型预计算未成功，已按静态口型回退运行' + (lipOff ? '（lipsync 服务离线）' : '') + '，可点「重试」。';
    },
    // P2-A: 把原始服务健康翻译成"数字人口型"这一用户关心的能力状态（服务面板顶部展示）
    lipPipelineStatus() {
      const s = this.services || {};
      if (!Object.keys(s).length) return { kind:'checking', label:'检测中…', dot:'bg-hub-muted', tone:'text-hub-muted' };
      const mode = this.broadcast && this.broadcast.mode;
      if (s.lipsync) return { kind:'online', label:'实时口型在线', dot:'bg-hub-green', tone:'text-hub-green' };
      if (mode === 'real_faceswap') return { kind:'static', label:'实时换脸模式 · 静态口型（正常）', dot:'bg-hub-muted', tone:'text-hub-muted' };
      return { kind:'degraded', label:'口型服务离线 · 静态回退', dot:'bg-amber-400', tone:'text-amber-400' };
    },
    // P3: 「静态口型属正常」认知层引导——把用户可能困惑的降级，从源头讲成"正常且更自然"的工作方式。
    // 仅在真为实时换脸模式(kind=static)时出现；一次性(持久化关闭)，能力面板可再次唤出。
    staticMouthNormal() { return this.lipPipelineStatus().kind === 'static'; },
    // _uivr：横幅随 lipsync 在线态出没，截图回归模式下抑制（与 tour/onboard/资产巡检横幅同一确定性口径）
    showStaticMouthHint() { return !this._uivr && this.staticMouthNormal() && (!this.smhDismissed || this.smhOpen); },
    dismissStaticMouthHint() {
      this.smhDismissed = true; this.smhOpen = false;
      try { localStorage.setItem('hub_seen_staticmouth_hint','1'); } catch(_){}
    },
    openStaticMouthHint() { this.smhOpen = true; },
    // P4: 首屏/重连就绪态回填——读 /api/profile/ready 把"正在准备中/降级"补进状态机，
    // 使刷新后立即看到进度条或静态口型态，无需等下次激活事件；并收敛断连期间漏收的 ready。
    async _syncFaceReady() {
      try {
        if (this.activation.stage === 'activating') return;      // 首激活 HTTP 在途→由它/WS 驱动，勿抢
        const d = await fetch(HUB+'/api/profile/ready').then(r=>r.json());
        if (!d || !d.name || d.name !== this.active) return;     // 仅回填当前激活角色
        if (d.state === 'preparing')      this._setStage(d.name, 'preparing', { eta: d.eta_s });
        else if (d.state === 'failed')    this._setStage(d.name, 'failed', { reason: d.reason });
        else if (this.activation.name === d.name) this._setStage(d.name, '');   // ready/unknown：收敛清空（含断连漏收 ready），空闲态不误触
      } catch(e) {}
    },

    // Pass E: cls 返回 bd-chip 变体名（brand.css 单源定色），不再散拼 Tailwind 色组合。
    //   宿主元素统一挂 class="bd-chip"，此处只给语义变体：solid=过程态抢眼 / dim=常驻安静态。
    cardStatus(p) {
      if (!p) return {label:'', cls:''};
      // 启用过程态（读单一事实源 activation，最高优先，实时覆盖静态徽标）
      const st = this.stageOf(p.name);
      if (st === 'activating')
        return {label:'⏳ 启用中…', cls:'info', spin:true, ring:'ring-2 ring-hub-blue'};
      if (st === 'preparing')
        return {label:`⏳ 准备中 ${this.facePct()}%`, cls:'warn', spin:true, ring:'ring-2 ring-amber-400'};
      if (st === 'failed') {
        // 依赖服务故意不起（实时换脸模式）= 预期降级，讲成"静态口型"正常态，绿色不吓人
        if (this.activation.reason === 'service_off')
          return {label:'✓ 已启用 · 静态口型', cls:'ok', title:this.faceDegradeHint()};
        // 真异常：琥珀告警（非红色恐慌）+ 卡片露出「重试」
        return {label:'⚠ 静态回退 · 可重试', cls:'warn strong', retry:true, title:this.faceDegradeHint()};
      }
      if (p.active) return {label:'✓ 已启用 · 就绪', cls:'ok'};
      if (this.qualityAlerts[p.name]) return {label:'⚠ 音质回归', cls:'err'};
      if (!p.has_voice) return {label:'未配置音色', cls:'muted'};
      // 待体检/可优化：同为安静琥珀（旧版 orange-300/amber-300 双色系肉眼难辨，Pass E 有意合并降噪）
      if (this.needsProbe(p)) return {label:'待体检', cls:'warn dim'};
      if (this.needsRefOptimize(p)) return {label:'可优化', cls:'warn dim'};
      return {label:'✓ 就绪', cls:'ok dim'};
    },
    // P1: 整卡点击=展开/收起详情（不再直接激活，避免误触切换形象）
    selectCard(name) { this.expandedCard = (this.expandedCard === name ? '' : name); this.menuCard=''; },
    // P0(UI改版): 抽屉「概览」用的实时角色对象——跟随 loadProfiles 刷新，避免用 openEdit 时的快照导致质量/能力/启用态过期
    drawerP() { return this.profiles.find(x => x.name === (this.editP && this.editP.orig_name)) || {}; },
    // P2(UI改版): 批量模式开关——关闭即清空所选，杜绝"隐藏的已选"造成误删/误导
    toggleBatch() { this.batchMode = !this.batchMode; if (!this.batchMode) { this.exportSelected = []; this.exportSelectAll = false; } },
    toggleSelect(name) { const i = this.exportSelected.indexOf(name); if (i >= 0) this.exportSelected.splice(i,1); else this.exportSelected.push(name); },
    // P1: 当前出镜角色对象（供顶部 hero）
    get activeProfileObj() { return (this.profiles || []).find(p => p.active) || null; },
    needsRefOptimize(p) {
      if (!p || !p.has_voice) return false;
      const c = (p.quality_axes || {}).cosine || 0;
      return c > 0 && c < 0.72;
    },
    profileReady(p) {
      if (!p || !p.has_voice) return false;
      const q = p.quality_axes || {};
      return (q.cosine > 0 || q.naturalness > 0);
    },
    openOnboard(silent=false) {
      const voiceProfiles = this.profiles.filter(p => p.has_voice);
      if (!voiceProfiles.length && !silent) {
        this.showToast('请先创建并绑定声音的角色', 'info');
        this.goTab('profiles');   // R1-2: 统一走 goTab（hash/最近页/离开批量停播 三件事别漏）
        return;
      }
      this.onboardProfile = this.active || voiceProfiles[0]?.name || this.profiles[0]?.name || '';
      this.onboardStep = 1;
      this.onboardShow = true;
    },
    onboardPick(name) {
      this.onboardProfile = name;
      this.activateProfile(name);
    },
    async onboardNext() {
      if (this.onboardStep === 1) {
        if (!this.onboardProfile) { this.showToast('请选择一个角色', 'error'); return; }
        await this.activateProfile(this.onboardProfile);
        this.onboardStep = 2;
        return;
      }
      if (this.onboardStep === 2) {
        const p = this.profiles.find(x => x.name === this.onboardProfile);
        if (p && this.needsProbe(p)) {
          await this.probeQuality(this.onboardProfile);
        }
        this.onboardStep = 3;
      }
    },
    onboardFinish() {
      localStorage.setItem('ah_onboard_v1', '1');
      this.onboardShow = false;
      window.open('/phone?profile=' + encodeURIComponent(this.onboardProfile || this.active || ''), '_blank');
    },
    onboardSkip() {
      localStorage.setItem('ah_onboard_v1', '1');
      this.onboardShow = false;
    },

    // B: Esc 统一关闭最上层浮层（按 z 层级从高到低）；关掉返回 true，无浮层返回 false。
    escClose() {
      if(this.cmdShow){ this.cmdShow=false; return true; }
      if(this.showShortcuts){ this.showShortcuts=false; return true; }
      if(this.videoModal){ this.videoModal=false; return true; }
      if(this.onboardShow){ this.onboardSkip(); return true; }
      if(this.createHubShow){ if(this.createHubMode){ this.createHubMode=''; } else { this.createHubShow=false; } return true; }
      if(this.importShow){ this.importShow=false; return true; }
      if(this.editShow){ this.editShow=false; return true; }
      if(this.previewShow){ this.previewShow=false; return true; }
      if(this.menuCard){ this.menuCard=''; return true; }
      if(this.showTour){ this.dismissTour(); return true; }
      if(this.expandedCard){ this.expandedCard=''; return true; }
      return false;
    },

    // ── WebSocket ──
    connectWs() {
      const ws = new WebSocket(HUB.replace('http','ws')+'/ws/status');
      ws.onmessage = e => {
        const d = JSON.parse(e.data);
        if (d.services) this.services = d.services;
        if (d.broadcast) this.broadcast = d.broadcast;
        if (d.stream_health) this.streamHealthBE = d.stream_health;   // 后端健康裁决（WS 推送，跨端单一真相）
        if (d.alerts !== undefined) this.svcAlerts = d.alerts;
        if (d.watchdog !== undefined) this.watchdog = d.watchdog;
        if (d.active_profile !== undefined) this.active = d.active_profile;
        // profiles_version 变化时自动重载（多端实时同步）
        if (d.profiles_version !== undefined && d.profiles_version !== this.profilesVersion) {
          this.profilesVersion = d.profiles_version;
          if (d.event !== 'init') this.loadProfiles();
        }
        if (d.event === 'init') this._syncFaceReady();   // P4: WS(重)连即回填就绪态，收敛断连期间漏收的进度/ready
        if (d.perf) { this.perf = d.perf; this.perfLive = true; }
        const reload = ['profile_created','profile_deleted','profile_updated','profile_renamed','profiles_imported'];
        if (reload.includes(d.event)) this.loadProfiles();
        // P12-5: 历史更新推送：统计始终刷新；历史 Tab 用 since_ts 增量追加新条
        if (d.event==='history_updated') {
          this.loadHistoryStats();
          this.loadVoiceRecent();  // UI-P2-4: 语音页"最近合成"同源刷新
          if(this.tab==='history') this.loadHistoryIncremental();  // 增量而非全量重载
        }
        if (d.event==='profile_activated') {
          const nm = d.active_profile || d.name;
          this.active = nm;
          // 同步卡片启用态：此前只更新 active 字符串，角色库卡片/hero 用的 profiles[].active
          // 不跟着翻，导致「开播横幅=后端真相、角色库=本地旧点选」的分裂显示（2026-07-07 事故）。
          (this.profiles||[]).forEach(p => p.active = (p.name === nm));
          // P13-5: 切换角色时清空旧媒体，避免残留视频/音频误导用户
          this.lipsyncVideoSrc=''; this.audioSrc='';
          // B1: 首激活就绪态落地状态机——preparing 由卡片/横幅内联进度承载（秒级计时，不弹 toast 刷屏）
          this._setStage(nm, d.face_ready === 'preparing' ? 'preparing' : '', d.face_eta_s);
          // 自动回填 RVC 滑块参数
          const s = d.rvc_settings || {};
          if (s.pitch       !== undefined) this.rvc.pitch      = s.pitch;
          if (s.index_rate  !== undefined) this.rvc.indexRate  = s.index_rate;
          if (s.protect     !== undefined) this.rvc.protect    = s.protect;
        }
        if (d.event==='face_ready') {
          if (d.name === this.active) {
            // 就绪→清空 overlay（回落就绪徽标）；失败→置 failed 并带原因（service_off=预期降级 / error=真异常）
            this._setStage(this.active, d.state === 'failed' ? 'failed' : '', { reason: d.reason });
            // 去恐慌化：预期降级(依赖服务故意不起)不弹错；仅真异常用较缓和的 warn 提示 + 引导重试
            if (d.state === 'failed' && d.reason !== 'service_off')
              this.showToast('口型预计算失败，已按静态回退运行（可在卡片点「重试」）', 'warn');
          }
        }
        // P22-2: 长任务进度推送（批量/视频等）
        if (d.event === 'task_progress' && d.task_id) {
          this.taskProgress[d.task_id] = d;
          // 清理已完成任务的旧进度（保留最近10个）
          const keys = Object.keys(this.taskProgress);
          if(keys.length > 20) {
            keys.filter(k => this.taskProgress[k].status === 'completed').slice(0, keys.length-20).forEach(k => delete this.taskProgress[k]);
          }
        }
        // Song-P2/F1: 点歌台事件（入队/备好/上麦/失败）→ 面板可见时即时刷新 + 关键节点提示
        if (d.event === 'song_station') {
          if (this.tab==='sing' && this.singMode==='station') this.stationRefresh();
          if (d.action==='ready'){
            const r=(this.station.queue||[]).find(x=>x.id===d.id);
            this.showToast('🎶 备歌完成'+(r?('《'+r.song_name+'》'):'')+'，可以上麦了','success');
          }
        }
      };
      ws.onopen  = () => { this.wsConnected=true; };   // P14-5
      ws.onclose = () => { this.wsConnected=false; this.perfLive=false; setTimeout(()=>this.connectWs(), 3000+Math.floor(Math.random()*2000)); };  // P14-5 P17-4: jitter 防惊群
    },

    // ── Profiles ──
    async loadProfiles() {
      try {
        const d = await fetch(HUB+'/profiles').then(r=>r.json());
        this.profiles = d.profiles;
        if (d.active) this.active = d.active;
        this.profilesLoadError = false;   // 成功 → 清除错误态
      } catch(e) {
        this.profilesLoadError = true;    // 失败(后端未起/断连) → 空态区分"连不上"与"真的没角色"
      }
      this.profilesLoaded = true;   // P2: 首次加载完成 → 关闭骨架屏
      this.loadQualityAlerts();
      this.loadVoiceHealth();       // 声音链路体检跟着角色列表走（fire-and-forget）
    },
    // ── 角色声音链路体检：断链（备份缺失/文件被删）在角色卡与开播预检就看得见 ──
    async loadVoiceHealth(){
      try{
        const d=await fetch(HUB+'/api/profile_voice_health').then(r=>r.json());
        if(d.ok) this.voiceHealth=d.profiles||{};
      }catch(_){}
    },
    vh(name){ return this.voiceHealth[name]||{level:'ok', issues:[]}; },
    // 一键落盘备份：内嵌参考音写成克隆文件（内容哈希自动回算引用；已有一致备份则幂等）
    async vhRepair(name){
      if(this.vhBusy) return;
      this.vhBusy=name;
      try{
        const r=await fetch(HUB+'/api/profile_voice_repair',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name, action:'backup_voice'})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'备份失败');
        this.showToast(d.existed?`磁盘已有一致备份（${d.file}）`:`参考音已落盘备份为 ${d.file}`,'success');
        await this.loadVoiceHealth();
      }catch(e){ this.showToast('落盘备份失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vhBusy=''; }
    },
    // 清除失效的变声绑定（模型文件已不存在，绑定名存实亡；复用 bind id='' 的清绑通道）
    async vhClearRvc(name){
      if(this.vhBusy) return;
      if(!confirm(`清除「${name}」的失效变声绑定？\n\n模型文件已不存在，绑定不会生效；清除后可到资产面板换绑其他模型。`)) return;
      this.vhBusy=name;
      try{
        const r=await fetch(HUB+'/api/rvc_assets/bind',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:name, id:''})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清除失败');
        this.showToast('已清除失效的变声绑定','success');
        await this.loadProfiles();   // 末尾会顺带刷新 voiceHealth
      }catch(e){ this.showToast('清除失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vhBusy=''; }
    },
    // 清除失效的形象视频引用（文件已不存在，运行链早已静默回退单图；清掉让配置与现实一致）
    async vhClearVideo(name, fix){
      if(this.vhBusy) return;
      const label=fix==='clear_idle_video'?'待机循环视频':'口型底视频';
      if(!confirm(`清除「${name}」的失效${label}引用？\n\n视频文件已不存在，运行时已自动回退单图方案；清除后体检不再报断链。`)) return;
      this.vhBusy=name;
      try{
        const r=await fetch(HUB+'/api/profile_voice_repair',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({name, action:fix})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清除失败');
        this.showToast('已清除失效的'+label+'引用','success');
        await this.loadProfiles();
      }catch(e){ this.showToast('清除失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vhBusy=''; }
    },
    // 批量落盘：所有「仅内嵌无备份」角色的参考音一次写盘（导入过一批角色包后逐个点是折磨；内容哈希幂等可重按）
    async vhBackupAll(){
      if(this.vaBusy) return;
      const n=this.vaHealth.unbacked||0;
      if(!confirm(`把 ${n||'所有'} 个角色的内嵌参考音落盘成克隆文件？\n\n已有一致备份的角色自动跳过，不会重复生成文件。`)) return;
      this.vaBusy='backup:all';
      try{
        const r=await fetch(HUB+'/api/profile_voice_repair',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({action:'backup_voice_all'})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'批量备份失败');
        const skip=(d.done||[]).length-(d.backed_n||0), fail=(d.failed||[]).length;
        this.showToast(`已落盘 ${d.backed_n} 个参考音`+(skip?`，${skip} 个已有备份跳过`:'')+(fail?`，${fail} 个失败`:''),
                       fail?'error':'success');
        await this.vaRefresh();
      }catch(e){ this.showToast('批量备份失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    // 可自动执行的修复项（rebind_voice 要人工挑声音，不进全修清单）
    vhAutoFixes(name){
      const AUTO=['backup_voice','fix_rvc','clear_idle_video','clear_body_video'];
      return this.vh(name).issues.filter(i=>AUTO.includes(i.fix));
    },
    // 一键全修：把该角色所有可自动修复项按序执行（备份落盘 + 清各类失效引用），一项失败不中断其余
    async vhFixAll(name){
      if(this.vhBusy) return;
      const fixes=this.vhAutoFixes(name);
      if(!fixes.length) return;
      const label={backup_voice:'落盘备份参考音', fix_rvc:'清除失效变声绑定',
                   clear_idle_video:'清除失效待机视频引用', clear_body_video:'清除失效底视频引用'};
      if(!confirm(`一键修复「${name}」的 ${fixes.length} 项：\n\n· `+fixes.map(i=>label[i.fix]).join('\n· ')
                  +'\n\n提示：失效变声绑定若想找回文件，请取消后走「去换绑模型」查看回收站。')) return;
      this.vhBusy=name;
      let ok=0, fail=0;
      for(const i of fixes){
        try{
          const r = i.fix==='fix_rvc'
            ? await fetch(HUB+'/api/rvc_assets/bind',{method:'POST',
                headers:{'Content-Type':'application/json'}, body:JSON.stringify({profile:name, id:''})})
            : await fetch(HUB+'/api/profile_voice_repair',{method:'POST',
                headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, action:i.fix})});
          const d=await r.json();
          if(!r.ok||!d.ok) throw 0;
          ok++;
        }catch(_){ fail++; }
      }
      this.vhBusy='';
      this.showToast(fail?`修复完成 ${ok} 项，${fail} 项失败`:`已一键修复全部 ${ok} 项`, fail?'error':'success');
      await this.loadProfiles();
    },
    // 徽标直达：点角色卡断链徽标 → 开抽屉概览并滚到素材链路卡（省去「进抽屉再找卡片」两步）
    vhOpenCard(p){
      this.openEdit(p,'overview');
      setTimeout(()=>{ try{ document.getElementById('vh-card').scrollIntoView({behavior:'smooth', block:'center'}); }catch(_){} }, 400);
    },
    // 换绑跳转（智能落点）：被删模型若还躺在回收站 → 直达回收站一键还原（比换绑更对症）；
    // 不在回收站 → 停在模型页签、清空过滤，浏览全部模型挑替代
    async vhJumpRebind(name){
      const p=(this.profiles||[]).find(x=>x.name===name)||{};
      const base=(((p.rvc_model||'').split(/[\\/]/).pop())||'').replace(/\.pth$/i,'');
      this.editShow=false;
      await this.vaOpen('rvc', base);
      if(base && this.vaTrashF.some(t=>t.kind==='rvc')) this.vaTab='trash';
      else this.vaQuery='';
    },
    async loadQualityAlerts() {
      try {
        const d = await fetch(HUB+'/api/metrics?recent=30').then(r=>r.json());
        const m = {};
        (d.quality_alerts||[]).forEach(a => { if (a.profile) m[a.profile] = a; });
        this.qualityAlerts = m;
      } catch(e) {}
    },
    async loadProfileStats() {
      // P21-3: 加载角色使用频率统计
      try {
        const d = await fetch(HUB+'/api/profiles/stats').then(r=>r.json());
        if(d.ok) this.profileUsage = d.usage || {};
      } catch(e) {}
    },

    async activateProfile(name, force=false) {
      if (this.activation.stage === 'activating') return;   // 防重复点击（串行 GPU 合成，避免并发）
      const target = this.profiles.find(p => p.name === name);
      if (!target) return;
      if (!force && target.active) return;                  // 常规点击：已激活则忽略；force=重试口型则继续
      // 乐观更新：立即让点击项变绿、其余取消，按钮/绿框即时反馈（不等后端 5-10s）
      const prevActive = (this.profiles.find(p => p.active) || {}).name || '';
      this.profiles.forEach(p => p.active = (p.name === name));
      this.active = name;
      this._setStage(name, 'activating');   // 进度由按钮/卡片徽标/头像色环承载，不再弹 toast
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 15000);  // 兜底超时，避免永久 loading
      try {
        const r = await fetch(HUB+`/profiles/${enc(name)}/activate`, {method:'POST', signal: ctrl.signal});
        const d = await r.json();
        if (!d.ok) throw new Error(d.detail || '启用失败');
        try {
          const det = await fetch(HUB+`/profiles/${enc(name)}?include_face=false`).then(r=>r.json());
          const ps = det.probe_sentences || [];
          this.probeSentencesText = ps.length ? ps.join('\n') : '';
        } catch(e) {}
      } catch(e) {
        // 失败：回滚乐观更新 + 清空流水线，保持 UI 与后端一致
        this.profiles.forEach(p => p.active = (p.name === prevActive));
        this.active = prevActive;
        if (this.activation.name === name) this._setStage(name, '');
        const msg = (e && e.name === 'AbortError') ? '启用超时，请重试' : ('启用失败：' + ((e && e.message) || e));
        this.showToast(msg, 'error');
      } finally {
        clearTimeout(timer);
        // 仅当 WS 尚未把流水线推进到 preparing/failed 时才收尾为就绪，避免 clobber 实时事件
        if (this.activation.name === name && this.activation.stage === 'activating') this._setStage(name, '');
      }
    },
    // P1-B: 口型预计算真异常后重试——force 重激活当前角色，重新触发后端 _prep_face
    retryFace() {
      const nm = this.activation.name || this.active;
      if (nm) this.activateProfile(nm, true);
    },

    async probeQuality(name) {
      if (this.qpLoading) return;
      this.qpLoading = name;
      this.probeElapsed = 0;
      clearInterval(this._probeTimer);
      this._probeTimer = setInterval(()=>{ this.probeElapsed++; }, 1000);
      try {
        const body = {profile: name};
        if (name === this.active && this.probeSentencesText.trim()) {
          body.sentences = this.probeSentencesText.split('\n').map(s=>s.trim()).filter(Boolean);
        }
        const d = await fetch(HUB+'/api/profile_quality_probe', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(body)
        }).then(r=>r.json());
        if (d.ok) {
          const p = this.profiles.find(x=>x.name===name);
          if (p) p.quality_axes = d.quality_axes;
          this.showToast(`「${name}」体检完成 · 音色 ${d.quality_axes.cosine} · 自然度 ${d.quality_axes.naturalness}`, 'success');
        } else {
          this.showToast(d.detail||'体检失败','error');
        }
      } catch(e) { this.showToast('体检失败: '+e,'error'); }
      finally { this.qpLoading = ''; clearInterval(this._probeTimer); this._probeTimer=null; }
    },

    async probeAllQuality() {
      if (this.probeAllRunning) return;
      const targets = this.profiles.filter(p=>p.has_voice);
      if (!targets.length) { this.showToast('没有可体检的角色','info'); return; }
      this.probeAllRunning = true;
      let done=0, ok=0;
      // GPU 合成本就串行，逐个体检并实时刷新徽章，避免并发抢显存
      for (const p of targets) {
        this.probeAllProgress = `${++done}/${targets.length} · ${p.name}`;
        try {
          this.qpLoading = p.name;
          const d = await fetch(HUB+'/api/profile_quality_probe', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({profile:p.name})
          }).then(r=>r.json());
          if (d.ok) { p.quality_axes = d.quality_axes; ok++; }
        } catch(e) {}
        finally { this.qpLoading = ''; }
      }
      this.probeAllRunning = false; this.probeAllProgress = '';
      this.showToast(`全员体检完成 · ${ok}/${targets.length} 成功`, 'success');
    },

    async deleteProfile(name) {
      if (!confirm(`删除角色「${name}」？`)) return;
      try {
        const r = await fetch(HUB+`/profiles/${enc(name)}`,{method:'DELETE'});
        const d = await r.json().catch(()=>({ok:r.ok}));
        if (!r.ok || d.ok===false) throw new Error(d.detail||('HTTP '+r.status));
        this.showToast(`已删除「${name}」`, 'success');
      } catch(e) {
        this.showToast('删除失败：'+((e&&e.message)||e), 'error');
      }
      this.loadProfiles();
    },

    async cloneProfile(name) {
      const newName = prompt(`复制角色「${name}」\n新角色名（留空自动为"${name}_副本"）：`);
      if (newName === null) return; // 取消
      try {
        const d = await fetch(HUB+`/profiles/${enc(name)}/clone`, {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({new_name: newName.trim()||undefined})
        }).then(r=>r.json());
        if (d.ok) {
          this.showToast(`已复制为「${d.name}」`, 'info');
          this.loadProfiles();
        } else {
          this.showToast('复制失败：'+(d.detail||'未知错误'), 'error');
        }
      } catch(e) {
        this.showToast('复制失败：'+((e&&e.message)||e), 'error');
      }
    },

    clearProfiles() {
      this.profiles.forEach(p=>fetch(HUB+`/profiles/${enc(p.name)}`,{method:'DELETE'}));
      setTimeout(()=>this.loadProfiles(),600);
    },

    onFaceFile(ev, target) {
      this._acceptFaceFile(ev.target.files[0], target);
      ev.target.value='';
    },
    _acceptFaceFile(file, target) {
      if (!file) return;
      const apply = (b64, dataUrl) => {
        this[target].faceB64 = b64;
        this[target].facePreview = dataUrl;
        if (target==='editP') this[target].face_b64 = b64;
      };
      // 手机原图常见 3~10MB，超后端 5MB(base64 7M字符)限制会 413——先在浏览器压到长边 1280 的 JPEG
      this._compressImage(file, 1280, 0.9)
        .then(r => apply(r.b64, r.dataUrl))
        .catch(() => {
          // 解码失败（如个别浏览器的 HEIC）→ 原始字节直传；仍超限则给出可行动提示
          const rd = new FileReader();
          rd.onload = e => {
            const dataUrl = e.target.result, b64 = (dataUrl.split(',')[1] || '');
            if (b64.length > 6_900_000) { this.showToast('图片过大且无法自动压缩，请转成 JPG/PNG 后重试', 'error'); return; }
            apply(b64, dataUrl);
          };
          rd.readAsDataURL(file);
        });
    },
    // 客户端压图：长边≤maxSide 重采样 + JPEG 重编码（透明底垫白，防转 JPEG 变黑）。
    // 已是小尺寸 JPEG 时原样保留（避免无谓的二次有损编码）。
    _compressImage(file, maxSide=1280, q=0.9) {
      return new Promise((resolve, reject) => {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
          try {
            const w0 = img.naturalWidth || img.width, h0 = img.naturalHeight || img.height;
            if (!w0 || !h0) throw new Error('bad dims');
            if (Math.max(w0, h0) <= maxSide && /jpe?g/i.test(file.type||'') && file.size <= 400*1024) {
              URL.revokeObjectURL(url);
              const rd = new FileReader();
              rd.onload = e => resolve({b64: e.target.result.split(',')[1], dataUrl: e.target.result});
              rd.onerror = () => reject(new Error('read failed'));
              rd.readAsDataURL(file);
              return;
            }
            const k = Math.min(1, maxSide / Math.max(w0, h0));
            const w = Math.max(1, Math.round(w0*k)), h = Math.max(1, Math.round(h0*k));
            const cv = document.createElement('canvas'); cv.width = w; cv.height = h;
            const ctx = cv.getContext('2d');
            ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h);
            ctx.drawImage(img, 0, 0, w, h);
            const dataUrl = cv.toDataURL('image/jpeg', q);
            URL.revokeObjectURL(url);
            resolve({b64: dataUrl.split(',')[1], dataUrl});
          } catch(e) { URL.revokeObjectURL(url); reject(e); }
        };
        img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片解码失败')); };
        img.src = url;
      });
    },
    // 人脸照片拖拽上传（照片建角色表单）
    newFaceDrop(ev) {
      this.newFaceDrag=false;
      const f=ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if(!f) return;
      if(!/^image\//.test(f.type||'')){ this.showToast('请拖入图片文件（JPG / PNG）','error'); return; }
      this._acceptFaceFile(f,'newP');
    },

    // 声音下拉试听：声音库条目走 quick_preview；「profile:名称」播该角色试听；「__current__」播本角色现绑声音
    previewVoiceSel(sel, target) {
      if(!sel) return;
      if(sel==='__current__') this.playVoicePreview(this.editP.orig_name);
      else if(sel.startsWith('profile:')) this.playVoicePreview(sel.slice(8));
      else this.quickPreview(sel, target);
    },
    // 声音来源切换（离开「现录/上传」时若本表单正在录音则先停止）
    setVoiceMode(m) {
      if(m!=='new' && this.recording && this._recTarget==='newP') this.stopRec();
      this.newVoice.mode=m;
    },

    async createProfile() {
      const nm = this.newP.name.trim();
      const block = this.newPBlockReason;
      if (block) { this.createMsg='⚠️ '+block; this.createOk=false; return; }
      // 重名保护：后端 POST /profiles 对同名是静默整体覆盖，先让用户显式确认
      if ((this.profiles||[]).some(p=>p.name===nm)) {
        if (!confirm(`已存在同名角色「${nm}」，继续创建将覆盖其全部配置（声音 / 形象 / 参数，不可撤销）。\n\n仍要继续吗？`)) return;
      }
      this.newPBusy=true; this.createMsg=''; this.createOk=false;
      try {
        // ① 解析声音来源
        let voice_name='', voice_b64='', voice_quality=null;
        if (this.newVoice.mode==='new') {
          // 合规红线：新音频必须先过 /api/voice_clone（授权 + 质检 + 水印 + 合规日志），不裸传原始音频
          const cr = await fetch(HUB+'/api/voice_clone',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({wav_base64:this.newVoice.audioB64, name:nm,
                                 agreed_terms:true, user_id:'local'})});
          const cd = await cr.json();
          if (!cr.ok || cd.ok===false) throw new Error(cd.detail||cd.reason||'声音克隆失败');
          voice_b64 = cd.voice_b64||'';
          const q = this.newVoice.quality;
          // check_quality 返回键为 duration_sec；深度自检(recCheck)为 duration_s，取可用者
          if (q) voice_quality = {snr_db:q.snr_db||0,
                                  duration_s:(this.newVoice.recCheck?.duration_s ?? q.duration_sec ?? q.duration_s ?? 0),
                                  ts:Date.now()/1000};
        } else if (this.newVoice.mode==='existing' && this.newP.voice) {
          if (this.newP.voice.startsWith('profile:')) {
            // 复用已有角色的声音：取其参考音（克隆向导产物）或声音库文件名
            const src = await fetch(HUB+`/profiles/${enc(this.newP.voice.slice(8))}?include_face=true`).then(r=>r.json());
            if (src.voice_b64) voice_b64 = src.voice_b64;
            else voice_name = src.voice_name||'';
          } else {
            voice_name = this.newP.voice;
          }
        }
        // ② 创建角色
        const body = {name:nm, description:this.newP.desc, face_b64:this.newP.faceB64,
                      voice_name, voice_b64, hair_style:this.newP.hair,
                      rvc_model:this.newP.rvcModel||''};
        if (voice_quality) body.voice_quality = voice_quality;
        const r = await fetch(HUB+'/profiles',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.detail||'创建失败');
        // ③ 成功态：弹窗内展示出口（试听 / 启用 / 再建），不直接关闭
        this.newPDone = nm;
        this.newPDoneVoice = !!(voice_b64 || voice_name);
        this.createMsg=''; this.createOk=true;
        this.loadProfiles();
        this.showToast('✅ 角色「'+nm+'」已创建','success');
      } catch(e) {
        this.createMsg='❌ '+((e&&e.message)||e); this.createOk=false;
        setTimeout(()=>{ if(!this.createOk) this.createMsg=''; },6000);
      } finally { this.newPBusy=false; }
    },
    // 照片建角色：进入表单时的初始化（成功后再进来=全新表单；未动过则按现有声音数定默认来源）
    initPhotoForm() {
      if (this.newPDone) this.resetNewP();
      if (!this.newVoice.audioB64 && !this.newP.voice) {
        this.newVoice.mode = (this.voices.length || this.voiceDonorProfiles.length) ? 'existing' : 'new';
      }
      this.$nextTick(()=>{ const el=document.getElementById('newPName'); el&&el.focus(); });
    },
    resetNewP() {
      if (this.recording && this._recTarget==='newP') this.stopRec();
      this.newP={name:'',desc:'',voice:'',hair:'',rvcModel:'',faceB64:'',facePreview:'',previewAudio:''};
      this.newVoice={mode:(this.voices.length||this.voiceDonorProfiles.length)?'existing':'new',
                     audioB64:'',audioName:'',drag:false,quality:null,recCheck:null,
                     override:false,checking:false,agreed:false,playUrl:''};
      this.newPDone=''; this.newPDoneVoice=false; this.createMsg=''; this.newFaceDrag=false;
      this.$nextTick(()=>{ const el=document.getElementById('newPName'); el&&el.focus(); });
    },
    // 成功态「立即启用出镜」：关弹窗 → 激活
    async newPActivate() {
      const nm=this.newPDone;
      this.createHubShow=false; this.createHubMode='';
      this.resetNewP();
      if (nm) await this.activateProfile(nm);
    },

    onVidAvFile(ev) {
      const f = ev.target.files[0];
      this.vidAv.file = f || null;
      this.vidAv.fileName = f ? f.name : '';
    },
    async createFromVideo() {
      if (!this.vidAv.name.trim()) { this.vidAv.msg='⚠️ 请填写角色名'; this.vidAv.ok=false; return; }
      if (!this.vidAv.file) { this.vidAv.msg='⚠️ 请选择一段真人半身视频'; this.vidAv.ok=false; return; }
      this.vidAv.busy=true; this.vidAv.ok=false; this.vidAv.thumb=''; this.vidAv.meta=null;
      this.vidAv.msg='⏳ 上传并自动裁切/抽帧/建角色中（约 10-20 秒）…';
      try {
        const fd = new FormData();
        fd.append('video', this.vidAv.file);
        fd.append('name', this.vidAv.name.trim());
        fd.append('description', this.vidAv.desc||'');
        fd.append('activate', 'true');
        const r = await fetch(HUB+'/api/avatar/from_video', {method:'POST', body:fd});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.vidAv.ok=true; this.vidAv.thumb=d.thumbnail||''; this.vidAv.meta=d.meta||null;
          const m=d.meta||{};
          this.vidAv.msg=`✅ 已创建并激活「${d.name}」 · 裁切 ${m.canvas?m.canvas.join('×'):''} · ${m.body_frames||''} 帧底`;
          this.vidAv.name=''; this.vidAv.desc=''; this.vidAv.file=null; this.vidAv.fileName='';
          this.loadProfiles();
          this.showToast(this.vidAv.msg,'success'); this.vidAv.msg=''; this.vidAv.thumb='';
          this.createHubShow=false; this.createHubMode='';
        } else {
          this.vidAv.ok=false; this.vidAv.msg='❌ '+((d&&d.detail)||'建角色失败');
        }
      } catch(e) {
        this.vidAv.ok=false; this.vidAv.msg='❌ 请求失败：'+e;
      } finally {
        this.vidAv.busy=false;
      }
    },

    async openEdit(p, tab='overview') {
      // P0(UI改版): 乐观即开抽屉（无需等 system_prompt 拉取），system_prompt/授权对比等异步回填
      // 声音下拉的真实表示：克隆/现录参考音(voice_b64)没有文件名，用哨兵值 __current__ 表示
      // 「保持现状」；否则会显示成「不绑定」，误导用户以为没绑声音（保存也无法表达解绑意图）
      this.editP = {orig_name:p.name, new_name:'', description:p.description,
                    voice_name:(p.voice_name || (p.has_voice ? '__current__' : '')), hair_style:p.hair_style,
                    rvc_model:p.rvc_model||'',
                    dfm_model:p.dfm_model||'',
                    rvc_strict_mode: p.rvc_strict_mode!==false,
                    system_prompt:'',
                    allow_reference_preview:false,
                    face_b64:'', facePreview:'', previewAudio:'',
                    galleryCount: p.face_gallery_count||0};
      if(!this.dfmModels.length) this.loadDfmModels();   // S2: 拉可直播 DFM 角色（换脸引擎离线则下拉隐藏）
      if(this.editP.dfm_model) this.prewarmDfm(this.editP.dfm_model);   // S2: 已绑 DFM→打开即后台预热，激活时秒切

      if (this.recording && this._recTarget==='editP') this.stopRec();   // 上一次抽屉遗留的录音
      this._resetCapVoice(this.editVoice);
      this.editMsg=''; this.drawerTab = tab; this.editShow=true;
      if(!this.rvcModels.length) this.rvcRefreshModels();
      try {
        const d = await fetch(HUB+`/profiles/${enc(p.name)}?include_face=false`).then(r=>r.json());
        if (this.editP.orig_name === p.name) {   // 防竞态：期间未切换角色才回填
          this.editP.system_prompt = d.system_prompt || '';
          this.editP.allow_reference_preview = !!d.allow_reference_preview;
          this.editP.galleryCount = d.face_gallery_count || 0;
        }
      } catch(e) {}
    },

    // ── 多照片源脸集（辨识度增强）：即传即存即生效，独立于「保存修改」 ──
    async onGalleryFiles(ev) {
      const files = Array.from(ev.target.files||[]); ev.target.value='';
      if (!files.length) return;
      const name = this.editP.orig_name;
      const b64s = [];
      for (const f of files) {
        try { const r = await this._compressImage(f, 1280, 0.9); b64s.push(r.b64); }
        catch(e) { this.showToast('有图片无法解码，已跳过（HEIC 请先转 JPG）', 'error'); }
      }
      if (!b64s.length) return;
      try {
        const r = await fetch(HUB+`/api/profiles/${enc(name)}/face_gallery`,{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({add:b64s})});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.detail||'上传失败');
        this.editP.galleryCount = d.gallery_count;
        this.showToast(`✅ 已加入 ${b64s.length} 张（共 ${d.gallery_count} 张参与平均）`);
      } catch(e) { this.showToast('❌ 附图保存失败：'+((e&&e.message)||e),'error'); }
    },

    async clearGallery() {
      const name = this.editP.orig_name;
      if (!confirm(`清空「${name}」的多照片源脸集？将回到单照模式。`)) return;
      try {
        const r = await fetch(HUB+`/api/profiles/${enc(name)}/face_gallery`,{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({clear:true})});
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.detail||'');
        this.editP.galleryCount = 0;
        this.showToast('已清空，回到单照模式');
      } catch(e) { this.showToast('❌ 清空失败：'+((e&&e.message)||e),'error'); }
    },

    async saveEdit() {
      const name = this.editP.orig_name;
      // 换声面板有音频但未就绪（质检/削幅/授权未过）→ 明确拦截，不能静默忽略用户已录的内容
      if (this.editVoice.open && this.editVoice.audioB64 && !this.editVoiceReady) {
        this.editMsg='❌ 新声音未就绪：'+this._capVoiceReason(this.editVoice,'editP'); this.editOk=false; return;
      }
      if (this.recording && this._recTarget==='editP') {
        this.editMsg='❌ 正在录音…请先点「停止录音」'; this.editOk=false; return;
      }
      const body = {};
      if (this.editP.new_name.trim()) body.new_name = this.editP.new_name.trim();
      if (this.editP.description !== undefined) body.description = this.editP.description;
      if (this.editVoiceReady) {
        // 换声优先级最高；合规红线：新音频必须过 /api/voice_clone（授权 + 水印 + 合规日志）
        this.editMsg='⏳ 正在处理新声音（质检 / 加水印）…'; this.editOk=true;
        try {
          const cr = await fetch(HUB+'/api/voice_clone',{
            method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({wav_base64:this.editVoice.audioB64,
                                 name:(body.new_name||name), agreed_terms:true, user_id:'local'})});
          const cd = await cr.json();
          if (!cr.ok || cd.ok===false || !cd.voice_b64) {
            this.editMsg='❌ 新声音处理失败：'+(cd.detail||cd.reason||'请重试'); this.editOk=false; return;
          }
          body.voice_b64 = cd.voice_b64; body.voice_name = '';
        } catch(e) { this.editMsg='❌ 新声音处理失败：'+((e&&e.message)||e); this.editOk=false; return; }
      } else if (this.editP.voice_name !== undefined) {
        const vsel = this.editP.voice_name;
        if (vsel === '__current__') {
          // 保持现有专属参考音：不带任何 voice 字段
        } else if (vsel && vsel.startsWith('profile:')) {
          // 「复用角色声音」：拷贝源角色的参考音（或其声音库文件名）
          try {
            const src = await fetch(HUB+`/profiles/${enc(vsel.slice(8))}?include_face=true`).then(r=>r.json());
            if (src.voice_b64) { body.voice_b64 = src.voice_b64; body.voice_name = ''; }
            else body.voice_name = src.voice_name||'';
          } catch(e) { this.editMsg='❌ 读取源角色声音失败'; this.editOk=false; return; }
        } else if (vsel === '' && this.drawerP().has_voice) {
          // 显式解绑（清 voice_b64+voice_name）：不可逆，先确认
          if (!confirm(`确定解绑「${name}」的声音？\n\n解绑后该角色暂不能对话 / 配音；若这段声音没有被其他角色复用，将无法从「复用角色声音」里找回。`)) return;
          body.clear_voice = true;
        } else {
          body.voice_name = vsel;
        }
      }
      if (this.editP.hair_style !== undefined) body.hair_style = this.editP.hair_style;
      if (this.editP.rvc_model !== undefined) body.rvc_model = this.editP.rvc_model;
      if (this.editP.dfm_model !== undefined) body.dfm_model = this.editP.dfm_model;   // S2: 整脸换角色绑定
      body.rvc_strict_mode = !!this.editP.rvc_strict_mode;
      if (this.editP.system_prompt !== undefined) body.system_prompt = this.editP.system_prompt;
      body.allow_reference_preview = !!this.editP.allow_reference_preview;
      if (this.editP.face_b64) body.face_b64 = this.editP.face_b64;

      try {
        const r = await fetch(HUB+`/profiles/${enc(name)}`,{method:'PATCH',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d = await r.json();
        this.editMsg = d.ok ? (body.clear_voice ? '✅ 已保存，声音已解绑'
                               : body.voice_b64!==undefined && this.editVoice.open ? '✅ 已保存，声音已替换'
                               : '✅ 保存成功')
                            : ('❌ 保存失败：'+(d.detail||''));
        this.editOk = d.ok;
        if (d.ok) { this._resetCapVoice(this.editVoice); this.loadProfiles(); this.refreshDfmAudit(); setTimeout(()=>{this.editShow=false;this.editMsg=''},1500); }
      } catch(e) {
        this.editMsg = '❌ 保存失败：'+((e&&e.message)||e);
        this.editOk = false;
      }
    },

    // ── Preview ──
    async previewProfile(name) {
      this.previewShow=true; this.previewName=name; this.previewLoading=true; this.previewImg=''; this.previewInfo='';
      try {
        const d = await fetch(HUB+`/profiles/${enc(name)}/preview`).then(r=>r.json());
        this.previewLoading=false;
        if (d.ok&&d.preview_image) {
          this.previewImg=d.preview_image;
          this.previewInfo=`模式:${d.mode} | ${d.elapsed_ms}ms`;
        } else this.previewInfo=d.detail||'预览失败';
      } catch(e) { this.previewLoading=false; this.previewInfo='请求失败'; }
    },

    // ── P18-4: 角色语音试听（懒加载，复用现有 audio player）──
    async playVoicePreview(name) {
      try {
        const url = HUB+`/profiles/${enc(name)}/voice_preview?lang=zh-cn`;
        const r = await fetch(url);
        if(!r.ok) { this.showToast('无语音预览，请先激活角色生成','error'); return; }
        const blob = await r.blob();
        const objUrl = URL.createObjectURL(blob);
        const audio = new Audio(objUrl);
        audio.onended = ()=>URL.revokeObjectURL(objUrl);
        audio.onerror = ()=>{ URL.revokeObjectURL(objUrl); this.showToast('语音预览播放失败','error'); };
        audio.play();
        this.showToast(`▶ 试听「${name}」`, 'success');
      } catch(e) { this.showToast('试听失败: '+e,'error'); }
    },

    // ── Voices ──
    async loadVoices() {
      try { const d=await fetch(HUB+'/voices').then(r=>r.json()); this.voices=d.voices||[]; } catch(e){}
    },

    // ── 情感辅助映射 ──
    // UI-P3: 此处原有一对 emotionCN/emotionEmoji 与 5500 行附近的重复定义（同一对象字面量，
    //   后者覆盖前者=这里是死代码），删除并把独有的 calm 词条并入存活定义，杜绝两处规则漂移。
    // P20-2: 情感对应颜色
    emotionColor(e) {
      const m={neutral:'#60a5fa',happy:'#facc15',excited:'#f97316',sad:'#818cf8',
               angry:'#f87171',fearful:'#a78bfa',surprised:'#34d399',disgusted:'#4ade80',
               gentle:'#f9a8d4',calm:'#67e8f9',serious:'#94a3b8',auto:'#a3e635'};
      return m[e]||'#60a5fa';
    },

    // ── P20-1: 波形可视化 (AudioContext + AnalyserNode) ──
    _initAudioContext() {
      if(this._audioCtx) return;
      try {
        const player = this.$refs.player;
        if(!player) return;
        this._audioCtx  = new (window.AudioContext || window.webkitAudioContext)();
        this._analyser  = this._audioCtx.createAnalyser();
        this._analyser.fftSize = 64;  // 32个频率桶
        this._analyser.smoothingTimeConstant = 0.75;
        this._audioCtx.createMediaElementSource(player).connect(this._analyser);
        this._analyser.connect(this._audioCtx.destination);
      } catch(e) { this._audioCtx = null; }
    },
    _startWaveAnim() {
      this._initAudioContext();
      if(!this._analyser) return;
      const canvas = this.$refs.waveCanvas;
      if(!canvas) return;
      const ctx = canvas.getContext('2d');
      const data = new Uint8Array(this._analyser.frequencyBinCount);
      const W = canvas.width, H = canvas.height;
      const draw = () => {
        if(!this.streamLoading) { ctx.clearRect(0,0,W,H); return; }
        this._waveRaf = requestAnimationFrame(draw);
        this._analyser.getByteFrequencyData(data);
        ctx.clearRect(0,0,W,H);
        const barW = Math.floor(W / data.length) - 1;
        for(let i=0;i<data.length;i++) {
          const h = Math.max(2, Math.round(data[i]/255*H));
          const ratio = i / data.length;
          ctx.fillStyle = `hsl(${220+ratio*80},80%,${45+ratio*25}%)`;
          ctx.fillRect(i*(barW+1), H-h, barW, h);
        }
      };
      if(this._audioCtx?.state === 'suspended') this._audioCtx.resume();
      draw();
    },
    _stopWaveAnim() {
      cancelAnimationFrame(this._waveRaf);
      const canvas = this.$refs.waveCanvas;
      if(canvas) canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
    },

    // ── 实时情感检测（debounce 600ms）──
    _scheduleEmotionDetect(text) {
      clearTimeout(this._emotionTimer);
      if(!text || text.trim().length < 2) { this.speakDetectedEmotion=''; return; }
      this._emotionTimer = setTimeout(async ()=>{
        try {
          const d=await fetch(HUB+'/api/emotion_detect?text='+encodeURIComponent(text.trim())).then(r=>r.json());
          if(this.speakEmotion==='auto') this.speakDetectedEmotion=d.emotion||'';
        } catch(e){}
      }, 600);
    },

    // ── TTS（SSE 进度流）──
    // ── UI-P0: 模式统一入口（主按钮 / 文本框 Ctrl+Enter / 全局快捷键都走这里）──
    speakGo(){
      if(this.speakLoading||this.streamLoading) return;
      if(this.speakMode==='stream') this.doSpeakStream(); else this.doSpeak();
    },
    setSpeakMode(m){
      this.speakMode = (m==='stream'?'stream':'standard');
      try{ localStorage.setItem('ah_speak_mode_v1', this.speakMode); }catch(_){}
    },
    // UI-P4-2: 批量页示例互通——语音页场景示例做首行 + 两行续写 = 3 行起步脚本；
    //   情感默认切「逐行智能检测」（混合脚本单一情感必不合身，auto 是唯一合理档）
    batchExampleFill(ex){
      const cont={
        // "注意查收"会被关键词检测误判成 serious（实验证实），带货语境改"记得领"避开
        '🛍 带货开场': ['这个价格今天真的只有这一场，错过就要再等一个月。',
                        '拍下的家人们记得领优惠券，我们马上开始讲解。'],
        '📚 知识口播': ['第一步，把大任务拆成三个小步骤，先做最容易的那个。',
                        '第二步，给每个步骤定一个十分钟的闹钟，到点就停，绝不拖延。'],
        '🌙 晚安电台': ['不管今天过得顺不顺利，此刻都值得对自己说一句辛苦了。',
                        '把手机放远一点，闭上眼睛，我们明晚同一时间再见。'],
        '👋 English 开场': ["Today I'll walk you through three quick tips you can use right away.",
                            "Grab a coffee, get comfortable, and let's dive in."],
      };
      const lines=[ex.text, ...(cont[ex.label]||[])];
      this.batchText=lines.join('\n');
      this.batchLang=ex.lang||this.batchLang;
      if(this.batchEmotion==='neutral') this.batchEmotion='auto';   // 不覆盖用户手选的具体情感
      this.showToast('已填入 '+lines.length+' 行示例脚本，点「开始批量配音」试听','info');
    },
    useSpeakExample(ex){
      this.speakText = ex.text;
      this.speakLang = ex.lang || 'zh-cn';
      if(ex.emotion) this.speakEmotion = ex.emotion;
    },
    // UI-P0: 下载当前合成音频（对齐唱歌页 downloadSing；audioSrc 为 data URL 直接触发下载）
    downloadSpeak(){
      if(!this.audioSrc) return;
      const a=document.createElement('a');
      a.href=this.audioSrc;
      a.download='speak_'+(this.active||'voice')+'_'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.wav';
      document.body.appendChild(a); a.click(); a.remove();
    },
    // UI-P0: 收藏当前合成（复用历史 star API；id 来自 saved 事件）
    async toggleStarSpeak(){
      if(!this.speakHistId) return;
      const next=!this.speakStarred;
      try{
        await fetch(HUB+`/api/history/${this.speakHistId}/star?starred=${next}`,{method:'POST'});
        this.speakStarred=next;
        const it=this.speakResults.find(x=>x.histId===this.speakHistId);
        if(it){ it.starred=next; this._persistSpeakResults(); }   // UI-P1: 与会话结果卡同步；UI-P4-3: 星标随卡持久化
        this.showToast(next?'已收藏到历史':'已取消收藏','success');
      }catch(e){ this.showToast('收藏失败: '+e,'error'); }
    },
    // ── UI-P1-1: 会话结果卡（本次会话最近 5 条，可回放对比）──
    _b64ToBlobUrl(b64, mime){
      try{
        const bin=atob(b64); const u8=new Uint8Array(bin.length);
        for(let i=0;i<bin.length;i++) u8[i]=bin.charCodeAt(i);
        return URL.createObjectURL(new Blob([u8],{type:mime||'audio/wav'}));
      }catch(_){ return ''; }
    },
    pushSpeakResult(r){
      const url = r.url || (r.audioB64 ? this._b64ToBlobUrl(r.audioB64,'audio/wav') : '');
      if(!url) return null;
      const item = {id:++this._speakResSeq, ts:Date.now(),
        text:(r.text||'').trim().slice(0,48), mode:r.mode||'standard',
        emotion:r.emotion||'', route:r.route||'', ms:r.ms||0, url,
        lipsyncUrl:(r.lipsyncB64 ? this._b64ToBlobUrl(r.lipsyncB64,'video/mp4') : ''),
        histId:r.histId||0, starred:false};
      this.speakResults.unshift(item);
      while(this.speakResults.length>5){
        const old=this.speakResults.pop();
        try{   // 播放器可能正借用被淘汰项的 URL：先摘引用再 revoke，防播放器黑掉
          if(this.audioSrc===old.url) this.audioSrc='';
          if(old.url.startsWith('blob:')) URL.revokeObjectURL(old.url);
          if(old.lipsyncUrl){ if(this.lipsyncVideoSrc===old.lipsyncUrl) this.lipsyncVideoSrc=''; URL.revokeObjectURL(old.lipsyncUrl); }
        }catch(_){}
      }
      this._persistSpeakResults();
      return item;
    },
    // ── UI-P4-3: 结果卡跨刷新找回——sessionStorage 只存已入历史的引用（histId+摘要），
    //   不存音频本体（Blob 刷新即死，base64 会撑爆配额）；音频在回放/下载时按需从历史接口取回。
    //   隐身合成无 histId 天然不持久化（隐私语义一致）；关标签页即清（sessionStorage 生命周期）。
    _persistSpeakResults(){
      try{
        const slim=this.speakResults.filter(x=>x.histId && !x.dead)   // P5-3: 确认被清理的卡不再复活
          .map(x=>({text:x.text,mode:x.mode,emotion:x.emotion,route:x.route,
                    ms:x.ms,histId:x.histId,starred:!!x.starred,ts:x.ts}));
        sessionStorage.setItem('ah_speak_results_v1', JSON.stringify(slim.slice(0,5)));
      }catch(_){}
    },
    _restoreSpeakResults(){
      try{
        const raw=sessionStorage.getItem('ah_speak_results_v1');
        if(!raw) return;
        const arr=JSON.parse(raw);
        if(!Array.isArray(arr)||!arr.length) return;
        this.speakResults=arr.slice(0,5).map(x=>({id:++this._speakResSeq,
          ts:x.ts||Date.now(), text:x.text||'', mode:x.mode||'standard',
          emotion:x.emotion||'', route:x.route||'', ms:x.ms||0,
          url:'', lipsyncUrl:'', histId:x.histId||0, starred:!!x.starred}));
      }catch(_){}
    },
    // P5-3: 取回结果三态——'ok' 拿到音频 / 'gone' 服务器确认记录没了（404，卡片就地标灰不复活）
    //   / 'net' 网络层失败（服务器没表态，卡片保持可重试）。fetching 驱动卡上"取回中…"瞬时态。
    async _ensureResultAudio(it){
      if(it.url) return 'ok';
      if(!it.histId || it.dead) return 'gone';
      it.fetching=true;
      try{
        const r=await fetch(HUB+`/api/history/${it.histId}/audio`);
        if(r.status===404){ it.dead=true; this._persistSpeakResults(); return 'gone'; }
        const d=await r.json().catch(()=>null);
        if(!d||!d.audio_base64) return 'net';
        it.url=this._b64ToBlobUrl(d.audio_base64,'audio/wav');
        return it.url?'ok':'net';
      }catch(_){ return 'net'; }
      finally{ it.fetching=false; }
    },
    _resultFetchFailToast(st){
      this.showToast(st==='gone'?'这条音频已从历史清理，没法找回了':'网络不给力，音频没取回来，稍后再点一次','error');
    },
    async downloadSpeakResult(it){
      const st=await this._ensureResultAudio(it);
      if(st!=='ok'){ this._resultFetchFailToast(st); return; }
      const a=document.createElement('a');
      a.href=it.url; a.download='语音_'+(it.histId||it.id)+'.wav';
      document.body.appendChild(a); a.click(); a.remove();
    },
    async replaySpeakResult(it){
      const st=await this._ensureResultAudio(it);
      if(st!=='ok'){ this._resultFetchFailToast(st); return; }
      this.audioSrc=it.url; this.speakResultMode=it.mode;
      this.speakHistId=it.histId||0; this.speakStarred=!!it.starred;
      this.speakStreamFull=(it.mode==='stream');   // 卡内流式项都是整段音频，可直接下载
      this.lipsyncVideoSrc=it.lipsyncUrl||'';      // 无口型的结果要清掉上一条的视频，防张冠李戴
      this.$nextTick(()=>{ try{ this.$refs.player?.play(); }catch(_){} });
    },
    // ── UI-P1-2: 流式完播→saved 回执→拉整段音频回填播放器与结果卡 ──
    async _loadStreamFull(histId, meta, ctrl){
      if(this.speakResults.some(x=>x.histId===histId)) return;  // 断线重试会二次 saved（24h 去重同 id），不重复建卡
      try{
        const d=await fetch(HUB+`/api/history/${histId}/audio`).then(r=>r.json());
        if(!d||!d.audio_base64) return;
        const it=this.pushSpeakResult({text:meta.text, mode:'stream',
                                       emotion:meta.emotion||'', route:meta.route||'clone',
                                       ms:meta.ms||0, audioB64:d.audio_base64, histId});
        if(!it) return;
        this._lastSpeakItem=it;
        this._streamFullUrl=it.url;
        // 逐句还在播 → 交给 _playQueueCtrl 尾部接手；已播完/被停 → 立即回填
        if(!ctrl || ctrl.stopped || (ctrl.eof && ctrl.playIdx>=ctrl.buf.length)) this._applyStreamFull();
      }catch(_){}
    },
    _applyStreamFull(){
      if(!this._streamFullUrl) return;
      this.audioSrc=this._streamFullUrl; this._streamFullUrl='';
      this.speakStreamFull=true;
    },
    async doSpeak() {
      if (!this.speakText.trim()||!this.active) {
        this.speakMsg=!this.active?'⚠️ 请先激活角色':'⚠️ 请输入文字'; this.speakOk=false; return;
      }
      this.speakLoading=true; this.speakMsg=''; this.speakPhase='queued';
      this.speakDetectedEmotion=''; this.lipsyncVideoSrc='';
      this.speakElapsedTts=0; this.speakElapsedRvc=0; this.speakRvcApplied=false;
      this.speakHistId=0; this.speakStarred=false; this.speakResultMode='standard';  // UI-P0
      this.speakStreamFull=false; this._streamFullUrl=''; this._lastSpeakItem=null;  // UI-P1
      try {
        const payload={text:this.speakText, profile:this.active, language:this.speakLang,
                       emotion:this.speakEmotion, generate_lipsync:this.speakLipsync};
        if(this.speakInstruct.trim()) payload.instruct=this.speakInstruct.trim();
        const r = await fetch(HUB+'/avatar/speak/stream',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        });
        const reader=r.body.getReader(); const dec=new TextDecoder(); let buf='';
        while(true){
          const{value,done}=await reader.read(); if(done) break;
          buf+=dec.decode(value,{stream:true});
          const evts=buf.split('\n\n'); buf=evts.pop()||'';
          for(const evt of evts){
            const dl=evt.split('\n').find(l=>l.startsWith('data: '));
            if(!dl) continue;
            try{
              const d=JSON.parse(dl.slice(6));
              if(d.phase==='tts_start')      this.speakPhase='tts';
              else if(d.phase==='tts_done'){  this.speakElapsedTts=d.elapsed_tts_ms||0; }
              else if(d.phase==='rvc_start')  this.speakPhase='rvc';
              else if(d.phase==='rvc_done'){  this.speakElapsedRvc=d.elapsed_rvc_ms||0; this.speakRvcApplied=d.rvc_applied; }
              else if(d.phase==='lipsync_start') this.speakPhase='lipsync';
              else if(d.phase==='lipsync_done'){}
              else if(d.phase==='done'){
                this.speakPhase='done';
                this.speakDoneVisible=true;
                if(d.audio_base64){
                  this.audioSrc='data:audio/wav;base64,'+d.audio_base64;
                  const tParts=[];
                  if(d.elapsed_tts_ms) tParts.push('声音 '+d.elapsed_tts_ms+'ms');
                  if(d.rvc_applied&&d.elapsed_rvc_ms) tParts.push('贴合音色 '+d.elapsed_rvc_ms+'ms');
                  if(d.tts_route==='emotion') tParts.push('情感引擎');   // UI-P0: 路由透明化
                  const detail=tParts.length?' ('+tParts.join(' · ')+')':"​";
                  const warn=d.warning?' ⚠️'+d.warning:'';
                  this.speakMsg=`✅ ${d.elapsed_ms}ms${detail}${warn}`; this.speakOk=true;
                  if(d.detected_emotion) this.speakDetectedEmotion=d.detected_emotion;
                  if(d.lipsync_video_b64) this.lipsyncVideoSrc='data:video/mp4;base64,'+d.lipsync_video_b64;
                  // UI-P1-1: 记入会话结果卡（最近 5 条可回放对比）
                  this._lastSpeakItem=this.pushSpeakResult({
                    text:payload.text, mode:'standard',
                    emotion:(d.detected_emotion||this.speakEmotion), route:d.tts_route||'',
                    ms:d.elapsed_ms||0, audioB64:d.audio_base64,
                    lipsyncB64:d.lipsync_video_b64||''});
                  this.$nextTick(()=>this.$refs.player?.play());
                } else { this.speakMsg='⚠️ TTS 未启动'; this.speakOk=false; }
              } else if(d.phase==='saved'){
                // UI-P0: 历史落库回执 → 点亮「☆ 收藏」；UI-P1: 同步到结果卡
                this.speakHistId=d.history_id||0;
                if(this._lastSpeakItem){ this._lastSpeakItem.histId=this.speakHistId; this._persistSpeakResults(); }  // UI-P4-3: histId 补到卡上后才值得持久化
              } else if(d.phase==='error'){
                this.speakMsg='❌ '+d.message; this.speakOk=false;
                this.speakDoneVisible=false;
              }
            } catch(_){}
          }
        }
      } catch(e) { this.speakMsg='❌ '+e; this.speakOk=false; this.speakDoneVisible=false; }
      finally {
        this.speakLoading=false;
        // done 状态持绣 2s再清除，错误状态立即清除
        const delay = this.speakDoneVisible ? 2000 : 0;
        setTimeout(()=>{ this.speakPhase=''; this.speakDoneVisible=false; }, delay);
        // UI-P0: 仅成功消息 4s 自清；错误常驻（配「↻ 重试」按钮），下次合成时自然清除
        setTimeout(()=>{ if(this.speakOk) this.speakMsg=''; }, 4000);
      }
    },

    // ── 唱歌（Sing-P0 诚实化改版）──
    onSingRefAudio(ev) { this.onSingRefFile(ev.target.files[0]); try{ ev.target.value=''; }catch(_){} },
    // 参考音频：文件选择 / 拖拽共用（Sing-P0: 10MB 上限 + 读时长供 3~30s 提示 + 可试听）
    onSingRefFile(file) {
      if(!file) return;
      if(file.size > 10*1024*1024){ this.showToast('参考音频超过 10MB，请截取 3~30 秒片段','error'); return; }
      this.singRefName=file.name;
      if(this.singRefUrl){ try{ URL.revokeObjectURL(this.singRefUrl); }catch(_){} }
      this.singRefUrl=URL.createObjectURL(file);
      this.singRefDur=0;
      const probe=new Audio(); probe.preload='metadata';
      probe.onloadedmetadata=()=>{ this.singRefDur=Math.round(probe.duration||0); };
      probe.src=this.singRefUrl;
      const r=new FileReader();
      r.onload=e=>{ this.singRefB64=e.target.result.split(',')[1]; };
      r.readAsDataURL(file);
    },
    singRefDurHint() {
      if(!this.singRefDur) return '';
      if(this.singRefDur<3)  return '偏短，建议 3~30 秒';
      if(this.singRefDur>30) return '偏长，引擎只取前段';
      return '时长合适';
    },
    // P0：拖拽上传参考音频（校验为音频）
    singDrop(ev) {
      this.singDrag=false;
      const file=ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if(!file) return;
      if(!/^audio\//.test(file.type||'') && !/\.(wav|mp3|m4a|flac|ogg|aac)$/i.test(file.name||'')){
        this.showToast('请拖入音频文件（WAV / MP3 等）','error'); return;
      }
      this.singVoiceMode='upload';
      this.onSingRefFile(file);
    },
    clearSingRef() {
      this.singRefB64=''; this.singRefName=''; this.singRefDur=0;
      if(this.singRefUrl){ try{ URL.revokeObjectURL(this.singRefUrl); }catch(_){} }
      this.singRefUrl='';
    },
    singCharCount() { return this.singLyrics.trim().length; },
    // 按歌词长度估时（保守系数：CosyVoice .117 实测约 0.6s/字；P1 基准脚本标定后回填）
    singEtaSec() {
      const n=this.singCharCount();
      return Math.min(120, Math.max(10, Math.round(5 + n*0.6)));
    },
    // 音色是否就绪（不就绪不拦生成——引擎会回退系统默认声音，只在体检 chip 里说明白）
    singVoiceReady() {
      if (this.singVoiceMode==='upload') return !!this.singRefB64;
      const p=this.activeProfileObj;
      return !!(p && p.has_voice);
    },
    // 就绪体检（音色 / 歌词 / 引擎），悬停看指引
    singPreflight() {
      const engOk = !!this.services.singing || !!this.services.emotion_tts;
      return [
        {key:'voice', label:'音色', ok:this.singVoiceReady(),
         hint:this.singVoiceMode==='upload'
              ? (this.singRefB64?'将用上传的参考音色演绎':'请先上传一段 3~30 秒参考音频')
              : (this.singVoiceReady()?'将用「'+(this.active||'')+'」的克隆音色演绎'
                                      :'当前角色还没有克隆音色，将用系统默认声音；想用自己的声音请去克隆页录一段')},
        {key:'lyrics', label:'歌词', ok:this.singCharCount()>0,
         hint:this.singCharCount()?'已输入歌词':'请输入要演绎的歌词'},
        {key:'engine', label:this.services.singing?'完整唱歌引擎':'念白引擎', ok:engOk,
         hint:this.services.singing?'完整唱歌引擎在线'
              :(engOk?'当前为深情念白模式：以角色音色朗读歌词；完整唱歌（AI 翻唱）在路线图中'
                     :'音色引擎离线：请在启动器启动情感 TTS 服务')},
      ];
    },
    // 下载演唱结果（文件名带角色署名）
    downloadSing() {
      if(!this.singAudioSrc) return;
      const a=document.createElement('a');
      a.href=this.singAudioSrc;
      const who=(this.active||'角色').replace(/[\\/:*?"<>|]/g,'');
      a.download='唱歌_'+who+'_'+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.wav';
      document.body.appendChild(a); a.click(); a.remove();
    },
    cancelSing() { if(this._singAbort){ try{ this._singAbort.abort(); }catch(_){} } },
    // 工程异常 → 人话（原始信息保留在控制台）
    singHumanErr(msg) {
      try{ console.warn('[sing]', msg); }catch(_){}
      const s=String(msg||'');
      if(/超过|上限|不能为空|过大|分段/.test(s)) return s;
      if(/并发|繁忙/.test(s)) return '引擎正忙，稍等几秒再试';
      if(/不可达|Failed to fetch|NetworkError|ECONN|timeout|超时/i.test(s)) return '音色服务未响应，多半在重启或太忙；稍候点「重新演绎」';
      return '引擎开小差了，稍候重试；歌词很长时建议分段';
    },
    async doSing() {
      if (!this.singLyrics.trim()) { this.singMsg='请先输入歌词'; this.singOk=false; return; }
      if (this.singCharCount()>1000) { this.singMsg='歌词超过 1000 字上限，请分段演绎'; this.singOk=false; return; }
      this.singLoading=true; this.singMsg=''; this.singAudioSrc=''; this.singEngine=''; this.singHistId=null;
      this.singEta=this.singEtaSec(); this.singT0=Date.now(); this.singElapsedMs=0;
      if(this._singTimer) clearInterval(this._singTimer);
      this._singTimer=setInterval(()=>{ this.singElapsedMs=Date.now()-this.singT0; }, 500);
      this._singAbort=new AbortController();
      const hardTimeout=setTimeout(()=>{ try{ this._singAbort.abort(); }catch(_){} }, 150000);
      try {
        const payload={lyrics:this.singLyrics, language:this.singLang, speed:this.singSpeed, emotion:this.singEmotion};
        if(this.singVoiceMode==='upload' && this.singRefB64) payload.reference_audio_b64=this.singRefB64;
        if(this.active) payload.profile=this.active;
        const resp=await fetch(HUB+'/avatar/sing',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload), signal:this._singAbort.signal});
        const d=await resp.json().catch(()=>({}));
        if(resp.ok && d.audio_base64){
          this.singAudioSrc='data:audio/wav;base64,'+d.audio_base64;
          this.singEngine=d.engine_used||'';
          this.singHistId=d.history_id||null;
          this.singMsg='完成 · 用时 '+((d.elapsed_ms||0)/1000).toFixed(1)+' 秒'+(this.singHistId?' · 已存入历史':'');
          this.singOk=true;
        } else {
          this.singMsg='没生成出来：'+this.singHumanErr(d.detail||('HTTP '+resp.status)); this.singOk=false;
        }
      } catch(e){
        if(e && e.name==='AbortError'){ this.singMsg='已取消'; this.singOk=false; }
        else { this.singMsg='没生成出来：'+this.singHumanErr(e); this.singOk=false; }
      }
      finally{
        clearTimeout(hardTimeout);
        if(this._singTimer){ clearInterval(this._singTimer); this._singTimer=null; }
        this._singAbort=null;
        this.singLoading=false;
        setTimeout(()=>this.singMsg='',8000);
      }
    },

    // ── Song-P1: AI 翻唱（整曲 → 拆人声 → 换音色 → 混音出品）──
    // 能力探测：引擎在线且 capabilities.cover 才展示翻唱模式（诚实降级，绝不假在线）
    async songHealthCheck(){
      try{
        const d=await fetch(HUB+'/api/song/health').then(r=>r.json());
        this.songOnline=!!d.online;
        // P4/F3: create 引擎能力挂在 songCaps.create（独立于翻唱引擎在线状态）
        this.songCaps=Object.assign({}, d.capabilities||{}, {create:d.create||null});
      }catch(_){ this.songOnline=false; this.songCaps={}; }
      this.songCapsLoaded=true;
      // 首次进入：翻唱可用则默认落在翻唱模式（主打能力优先）
      // _uivr：默认子模式随引擎在线态翻转会让截图不确定（引擎起落=面板整体切换），回归模式固定 lyrics
      if(this.songCaps.cover && !this._songModeTouched && !this._uivr) this.singMode='cover';
    },
    setSingMode(m){
      this.singMode=(['cover','lyrics','station','create'].includes(m)?m:'lyrics');
      this._songModeTouched=true;
      if(m==='station'){ this.stationRefresh(); this.stationStartPoll(); }  // Song-P2/F1
    },
    songCoverReady(){ return this.songOnline && !!this.songCaps.cover; },
    songTaskRunning(){ return ['queued','separating','converting','mixing'].includes(this.songStatus); },
    onSongFile(ev){ this.onSongFilePick(ev.target.files[0]); try{ ev.target.value=''; }catch(_){} },
    songFileDrop(ev){
      this.songDrag=false;
      const f=ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if(!f) return;
      if(!/^audio\//.test(f.type||'') && !/\.(wav|mp3|m4a|flac|ogg|aac)$/i.test(f.name||'')){
        this.showToast('请拖入音频文件（MP3 / WAV / M4A 等）','error'); return;
      }
      this.onSongFilePick(f);
    },
    onSongFilePick(f){
      if(!f) return;
      if(f.size > 40*1024*1024){ this.showToast('歌曲文件超过 40MB 上限，请压缩或截选','error'); return; }
      this.songFileObj=f; this.songFileName=f.name; this.songFileDur=0;
      if(this.songFileUrl){ try{ URL.revokeObjectURL(this.songFileUrl); }catch(_){} }
      this.songFileUrl=URL.createObjectURL(f);
      const probe=new Audio(); probe.preload='metadata';
      probe.onloadedmetadata=()=>{ this.songFileDur=Math.round(probe.duration||0); };
      probe.src=this.songFileUrl;
    },
    clearSongFile(){
      this.songFileObj=null; this.songFileName=''; this.songFileDur=0;
      if(this.songFileUrl){ try{ URL.revokeObjectURL(this.songFileUrl); }catch(_){} }
      this.songFileUrl='';
    },
    songDurHint(){
      if(!this.songFileDur) return '';
      if(this.songFileDur>480) return '超过 8 分钟上限，请截选';
      const m=Math.floor(this.songFileDur/60), s=this.songFileDur%60;
      return m+'分'+(s<10?'0':'')+s+'秒';
    },
    // 阶段人话（进度流水线文案）
    songPhaseLabel(st){
      return {queued:'排队中', separating:'① 拆出人声', converting:'② 换成目标音色',
              mixing:'③ 混音出品', done:'完成', error:'失败', cancelled:'已取消'}[st]||st;
    },
    songEtaText(){
      // 经验估时：约为歌曲时长的 0.6~1.2 倍（5090 实测后由基准脚本回填系数）
      if(!this.songFileDur) return '';
      const lo=Math.round(this.songFileDur*0.6), hi=Math.round(this.songFileDur*1.3);
      return '预计 '+lo+'~'+hi+' 秒';
    },
    async doCover(){
      if(!this.songFileObj){ this.songMsg='请先选择一首歌曲'; this.songOk=false; return; }
      if(this.songFileDur>480){ this.songMsg='歌曲超过 8 分钟上限，请截选后再试'; this.songOk=false; return; }
      const hasVoice = this.singVoiceMode==='upload' ? !!this.singRefB64 : this.singVoiceReady();
      if(!hasVoice){ this.songMsg='翻唱需要目标音色：请选一个有克隆音的角色，或上传一段 3~30 秒参考声音'; this.songOk=false; return; }
      this.songMsg=''; this.songResult=null; this.songTaskId='';
      this.songStatus='queued'; this.songProgress=2; this.songDetail='正在上传歌曲…';
      this.songT0=Date.now(); this.songElapsedMs=0; this._songPollStop=false;
      // P2/O5: 首次提交时顺手请求通知权限（分钟级任务，用户大概率切走）
      try{
        if(window.Notification && Notification.permission==='default')
          Notification.requestPermission().catch(()=>{});
      }catch(_){}
      if(this._songTimer) clearInterval(this._songTimer);
      this._songTimer=setInterval(()=>{ this.songElapsedMs=Date.now()-this.songT0; }, 500);
      try{
        const fd=new FormData();
        fd.append('song', this.songFileObj, this.songFileName);
        if(this.singVoiceMode==='upload' && this.singRefB64){
          const bin=atob(this.singRefB64); const u8=new Uint8Array(bin.length);
          for(let i=0;i<bin.length;i++) u8[i]=bin.charCodeAt(i);
          fd.append('reference', new Blob([u8],{type:'audio/wav'}), this.singRefName||'reference.wav');
        }
        if(this.active) fd.append('profile', this.active);
        fd.append('pitch', this.songPitch);
        fd.append('quality', this.songQuality);
        fd.append('dry_vocal', this.songDryVocal?'true':'false');
        const r=await fetch(HUB+'/api/song/cover',{method:'POST', body:fd});
        const d=await r.json().catch(()=>({}));
        if(!r.ok || !d.task_id){
          this._songFail(this.songHumanErr(d.detail||('HTTP '+r.status))); return;
        }
        this.songTaskId=d.task_id;
        this._songPoll();
      }catch(e){ this._songFail(this.songHumanErr(e)); }
    },
    async _songPoll(){
      if(this._songPollStop || !this.songTaskId) return;
      try{
        const d=await fetch(HUB+'/api/song/task/'+this.songTaskId).then(r=>r.json());
        this.songStatus=d.status||''; this.songProgress=d.progress||0; this.songDetail=d.detail||'';
        if(d.status==='done'){
          this._songStopTimer();
          this.songResult=d; this.songOk=true;
          this.songMsg='翻唱完成 · 用时 '+Math.round((d.elapsed_ms||this.songElapsedMs)/1000)+' 秒'+(d.history_id?' · 已存入历史':'');
          this._songNotify('🎤 翻唱完成', (this.songFileName||'成品')+' 已就绪，回唱歌页试听');  // P2/O5
          this.loadHistoryIncremental();
          return;
        }
        if(d.status==='error'){
          this._songNotify('翻唱失败', this.songHumanErr(d.detail||'引擎处理失败'));  // P2/O5
          this._songFail(this.songHumanErr(d.detail||'引擎处理失败')); return;
        }
        if(d.status==='cancelled'){ this._songStopTimer(); this.songMsg='已取消'; this.songOk=false; this.songStatus='cancelled'; return; }
      }catch(e){ /* 单次轮询失败不终止：网络抖动继续等下一轮 */ }
      setTimeout(()=>this._songPoll(), 2500);
    },
    _songStopTimer(){ if(this._songTimer){ clearInterval(this._songTimer); this._songTimer=null; } },
    // P2/O5: 分钟级任务完成通知——切去别的页/最小化也能收到（页面可见时 toast 已够）
    _songNotify(title, body){
      if(this.tab!=='sing' || this.singMode!=='cover') this.showToast(title+'：'+body, 'success');
      try{
        if(document.hidden && window.Notification && Notification.permission==='granted')
          new Notification(title, {body: body});
      }catch(_){}
    },
    _songFail(msg){
      this._songStopTimer(); this._songPollStop=true;
      this.songStatus='error'; this.songMsg='没翻成：'+msg; this.songOk=false;
    },
    async cancelCover(){
      this._songPollStop=true;
      if(this.songTaskId){
        try{ await fetch(HUB+'/api/song/task/'+this.songTaskId+'/cancel',{method:'POST'}); }catch(_){}
      }
      this._songStopTimer();
      this.songStatus='cancelled'; this.songMsg='已取消'; this.songOk=false;
    },
    songHumanErr(msg){
      try{ console.warn('[cover]', msg); }catch(_){}
      const s=String(msg||'');
      if(/超过|上限|不能为空|过大|截选|音色|没有可用/.test(s)) return s;
      if(/权重未就绪|未部署/.test(s)) return '翻唱引擎还没部署好：先运行部署脚本（详见页面提示）';
      if(/不可达|Failed to fetch|NetworkError|ECONN|timeout|超时/i.test(s)) return '翻唱引擎未响应：可能未启动或正在加载模型，稍候再试';
      if(/繁忙|并发/.test(s)) return '引擎正忙（前面还有任务），稍等一会儿';
      return '引擎开小差了：'+s.slice(0,80);
    },
    songSimilarityText(){
      const s=this.songResult && this.songResult.similarity;
      if(!s) return '';
      return '人声贴合度 '+s.cosine+'（'+s.label+'）';
    },
    downloadCover(){
      const r=this.songResult;
      if(!r || !r.audio_url) return;
      const a=document.createElement('a');
      a.href=HUB+r.audio_url;
      const who=(r.singer||this.active||'角色').replace(/[\\/:*?"<>|]/g,'');
      a.download='翻唱_'+who+'_'+(this.songFileName||'song').replace(/\.[^.]+$/,'')+'.wav';
      document.body.appendChild(a); a.click(); a.remove();
    },

    // ── Song-P4/F3: 原创歌（ACE-Step 整曲文本成曲，可选角色声重唱）──
    songCreateReady(){
      const c=this.songCaps && this.songCaps.create;
      return !!(c && c.online && c.capabilities && c.capabilities.create);
    },
    createTaskRunning(){ return ['queued','loading','generating','converting'].includes(this.createStatus); },
    createPhaseLabel(st){
      return {queued:'排队中', loading:'加载作曲引擎', generating:'① 作曲编曲演唱',
              converting:'② 换成角色声音', done:'完成', error:'失败', cancelled:'已取消'}[st]||st;
    },
    async doLyricsAssist(){
      const topic=(this.createTopic||'').trim();
      if(!topic){ this.showToast('先在「主题」里写一句想唱什么，AI 才好下笔','error'); return; }
      this.createLyricsBusy=true;
      try{
        const r=await fetch(HUB+'/api/song/lyrics_assist',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({topic, style:this.createStyle, duration_s:+this.createDur||60})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok || !d.lyrics){ this.showToast(d.detail||'歌词生成失败，请再试或手写','error'); return; }
        this.createLyrics=d.lyrics;
        this.showToast('歌词写好了，可以直接改','success');
      }catch(e){ this.showToast('歌词服务未响应：'+e,'error'); }
      finally{ this.createLyricsBusy=false; }
    },
    async doCreate(){
      const style=(this.createStyle||'').trim();
      if(!style){ this.createMsg='先选一个风格（或自己写风格标签）'; this.createOk=false; return; }
      if(this.createSwap && !this.singVoiceReady()){
        this.createMsg='勾了「用角色声唱」需要角色克隆音：先去克隆页录一段，或去掉勾选';
        this.createOk=false; return;
      }
      this.createMsg=''; this.createResult=null; this.createTaskId='';
      this.createStatus='queued'; this.createProgress=2; this.createDetail='正在提交…';
      this.createStage='gen'; this.createT0=Date.now(); this.createElapsedMs=0;
      this._createPollStop=false;
      try{
        if(window.Notification && Notification.permission==='default')
          Notification.requestPermission().catch(()=>{});
      }catch(_){}
      if(this._createTimer) clearInterval(this._createTimer);
      this._createTimer=setInterval(()=>{ this.createElapsedMs=Date.now()-this.createT0; }, 500);
      try{
        const r=await fetch(HUB+'/api/song/create',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({style, lyrics:this.createLyrics||'',
            duration_s:+this.createDur||60, quality:this.createQuality,
            profile:this.active||'', svc_swap:!!this.createSwap,
            remix_of:this.createRemixOf||null,                       // P5/F5 风格魔改
            remix_strength:+this.createRemixStrength||0.5,
            lora:this.createLora||'',                                // P6 唱腔 LoRA
            lora_weight:+this.createLoraWeight||1.0})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok || !d.task_id){ this._createFail(this.songHumanErr(d.detail||('HTTP '+r.status))); return; }
        this.createTaskId=d.task_id;
        this._createPoll();
      }catch(e){ this._createFail(this.songHumanErr(e)); }
    },
    async _createPoll(){
      if(this._createPollStop || !this.createTaskId) return;
      try{
        const d=await fetch(HUB+'/api/song/create/'+this.createTaskId).then(r=>r.json());
        this.createStatus=d.status||''; this.createProgress=d.progress||0;
        this.createDetail=d.detail||''; this.createStage=d.stage||'';
        if(d.status==='done'){
          this._createStopTimer();
          this.createResult=d; this.createOk=true;
          const kind=this.createRemixOf?'魔改':'原创歌';
          this.createMsg=kind+'完成 · 用时 '+Math.round((this.createElapsedMs)/1000)+' 秒'+(d.history_id?' · 已存入历史':'');
          this._createNotify('🎼 '+kind+'完成', '整曲已就绪，回唱歌页试听');
          this.loadHistoryIncremental();
          return;
        }
        if(d.status==='error'){
          this._createNotify('原创歌失败', this.songHumanErr(d.detail||'生成失败'));
          this._createFail(this.songHumanErr(d.detail||'生成失败')); return;
        }
        if(d.status==='cancelled'){ this._createStopTimer(); this.createMsg='已取消'; this.createOk=false; this.createStatus='cancelled'; return; }
      }catch(e){ /* 单次轮询失败不终止 */ }
      setTimeout(()=>this._createPoll(), 2500);
    },
    _createStopTimer(){ if(this._createTimer){ clearInterval(this._createTimer); this._createTimer=null; } },
    _createNotify(title, body){
      if(this.tab!=='sing' || this.singMode!=='create') this.showToast(title+'：'+body, 'success');
      try{
        if(document.hidden && window.Notification && Notification.permission==='granted')
          new Notification(title, {body: body});
      }catch(_){}
    },
    _createFail(msg){
      this._createStopTimer(); this._createPollStop=true;
      this.createStatus='error'; this.createMsg='没做成：'+msg; this.createOk=false;
    },
    async cancelCreate(){
      this._createPollStop=true;
      if(this.createTaskId){
        try{ await fetch(HUB+'/api/song/create/'+this.createTaskId+'/cancel',{method:'POST'}); }catch(_){}
      }
      this._createStopTimer();
      this.createStatus='cancelled'; this.createMsg='已取消'; this.createOk=false;
    },
    downloadCreate(){
      const r=this.createResult;
      if(!r || !r.audio_url) return;
      const a=document.createElement('a');
      a.href=HUB+r.audio_url;
      const who=(r.singer||'ACE').replace(/[\\/:*?"<>|]/g,'');
      a.download='原创_'+who+'_'+Date.now()+'.wav';
      document.body.appendChild(a); a.click(); a.remove();
    },

    // ── Song-P2/F1: 直播间点歌台（曲库点歌 → 自动备歌 → 一键上麦）──
    async stationRefresh(){
      try{
        const d=await fetch(HUB+'/api/song/station').then(r=>r.json());
        this.station=Object.assign({queue:[],library:[]}, d);
        this.stationLoaded=true;
      }catch(_){ /* 单次失败静默，下一轮再试 */ }
    },
    stationStartPoll(){
      if(this._stationTimer) return;
      // 兜底轮询（主更新源是 WS song_station 事件）：仅点歌台面板可见时拉
      this._stationTimer=setInterval(()=>{
        if(this.tab==='sing' && this.singMode==='station') this.stationRefresh();
      }, 6000);
    },
    async stationConfig(patch){
      try{
        const d=await fetch(HUB+'/api/song/station/config',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(patch)}).then(r=>r.json());
        Object.assign(this.station, d||{});
        this.stationRefresh();
      }catch(_){ this.showToast('设置保存失败，请重试','error'); }
    },
    async stationRequest(file){
      if(this.stationBusy) return;
      this.stationBusy=true;
      try{
        const r=await fetch(HUB+'/api/song/station/request',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({file:file, requester:this.stationReqName||'主播'})});
        const d=await r.json().catch(()=>({}));
        if(r.ok && d.ok){
          this.showToast('已点歌《'+(d.song_name||file)+'》，排在第 '+(d.position||1)+' 位','success');
          this.stationRefresh();
        }else{
          this.showToast(d.detail||d.reason||'点歌失败','error');
        }
      }catch(e){ this.showToast('点歌失败：'+e,'error'); }
      finally{ this.stationBusy=false; }
    },
    async stationAct(rid, act){
      // act: cancel / top / retry / play
      try{
        const r=await fetch(HUB+'/api/song/station/'+rid+'/'+act,{method:'POST'});
        const d=await r.json().catch(()=>({}));
        if(!r.ok) this.showToast(d.detail||('操作失败 HTTP '+r.status),'error');
        else if(act==='play') this.showToast('已上麦，直播间开始放歌','success');
        this.stationRefresh();
      }catch(e){ this.showToast('操作失败：'+e,'error'); }
    },
    async stationDelete(rid){
      try{
        await fetch(HUB+'/api/song/station/'+rid,{method:'DELETE'});
        this.stationRefresh();
      }catch(_){}
    },
    async stationStop(){
      try{
        await fetch(HUB+'/api/song/station/stop',{method:'POST'});
        this.showToast('已停止放歌','info');
        this.stationRefresh();
      }catch(e){ this.showToast('停止失败：'+e,'error'); }
    },
    stationStatusLabel(st){
      return {queued:'排队中', preparing:'备歌中', ready:'✓ 就绪', playing:'▶ 播放中',
              done:'已唱完', failed:'失败', cancelled:'已取消'}[st]||st;
    },
    stationStatusClass(st){
      return {queued:'border-hub-border text-hub-muted',
              preparing:'border-hub-blue/40 text-hub-blue bg-hub-blue/10',
              ready:'border-hub-green/40 text-hub-green bg-hub-green/10',
              playing:'border-hub-green/40 text-hub-green bg-hub-green/20',
              done:'border-hub-border text-hub-muted',
              failed:'border-hub-red/40 text-hub-red bg-red-900/10',
              cancelled:'border-hub-border text-hub-muted'}[st]||'border-hub-border text-hub-muted';
    },
    stationPlayingReq(){
      const pid=this.station.playing_id;
      return pid ? (this.station.queue||[]).find(r=>r.id===pid) : null;
    },
    stationQueueView(){
      // 进行中的在前（保持队列序），唱完/取消的沉底
      const q=this.station.queue||[];
      const act=q.filter(r=>['queued','preparing','ready','playing'].includes(r.status));
      const rest=q.filter(r=>!['queued','preparing','ready','playing'].includes(r.status));
      return act.concat(rest);
    },
    // P5: 今日点歌榜（主播口播/贴片用）
    async stationLoadBoard(){
      try{
        const d=await fetch(HUB+'/api/song/station/leaderboard?days=1&top=10').then(r=>r.json());
        this.stationBoard=d.board||[];
      }catch(_){ this.stationBoard=[]; }
    },

    // ── Song-P5: 我的任务看板（翻唱/原创魔改/MV 一屏可见；关页照跑）──
    async songBoardRefresh(){
      try{
        const d=await fetch(HUB+'/api/song/tasks').then(r=>r.json());
        this.songBoard=d.tasks||[]; this.songBoardOpen=d.open||0; this.songBoardLoaded=true;
      }catch(_){ /* 单次失败静默 */ }
    },
    songBoardStartPoll(){
      this.songBoardRefresh();
      if(this._songBoardTimer) return;
      this._songBoardTimer=setInterval(()=>{
        if(this.tab==='sing' && (this.songBoardShow || this.songBoardOpen>0)) this.songBoardRefresh();
      }, 5000);
    },
    songBoardKindIcon(row){
      return {cover:'🎤', create:'🎼', remix:'🎛', mv:'🎬'}[row.kind]||'🎵';
    },
    songBoardKindLabel(row){
      return {cover:'翻唱', create:'原创', remix:'魔改', mv:'MV'}[row.kind]||row.kind;
    },
    songBoardStatusLabel(st){
      return {pending:'处理中', queued:'排队中', waiting:'等直播档期', loading:'加载引擎',
              generating:'生成中', separating:'分离人声', converting:'换声中', mixing:'混音中',
              rendering:'渲染中', done:'完成', error:'失败', cancelled:'已取消'}[st]||st||'…';
    },
    async songBoardCancel(row){
      const url={cover:'/api/song/task/'+row.tid+'/cancel',
                 create:'/api/song/create/'+row.tid+'/cancel',
                 remix:'/api/song/create/'+row.tid+'/cancel',
                 mv:'/api/song/mv_task/'+row.tid+'/cancel'}[row.kind];
      if(!url) return;
      try{ await fetch(HUB+url,{method:'POST'}); }catch(_){}
      this.songBoardRefresh();
    },

    // ── Song-P5: 整曲 MV（异步任务：排队+直播让路+可取消，关页照渲）──
    async doSongMvFull(histId){
      if(!histId) return;
      try{
        const r=await fetch(HUB+'/api/song/mv_task',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({history_id:histId, seconds:0, profile:this.active||''})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok || !d.task_id){ this.showToast(d.detail||'整曲 MV 提交失败','error'); return; }
        this.mvTask={task_id:d.task_id, status:'queued', progress:0, detail:'排队中'};
        this.showToast('🎬 整曲 MV 已排队：后台渲染几分钟，直播中会自动等档期——进度看「我的任务」','success');
        this._mvTaskPoll(); this.songBoardRefresh();
      }catch(e){ this.showToast('整曲 MV 提交失败：'+e,'error'); }
    },
    async _mvTaskPoll(){
      if(!this.mvTask || !this.mvTask.task_id) return;
      try{
        const d=await fetch(HUB+'/api/song/mv_task/'+this.mvTask.task_id).then(r=>r.json());
        this.mvTask=Object.assign({}, this.mvTask, d);
        if(d.status==='done'){
          if(d.url){ this.mvUrl=HUB+d.url; this.mvInfo=d; }
          this.showToast('🎬 整曲 MV 出片完成（'+Math.round(d.seconds||0)+' 秒），可下载','success');
          this.loadHistoryIncremental && this.loadHistoryIncremental();
          return;
        }
        if(d.status==='error'){ this.showToast('整曲 MV 失败：'+(d.detail||''),'error'); return; }
        if(d.status==='cancelled') return;
      }catch(_){ /* 单次轮询失败不终止 */ }
      setTimeout(()=>this._mvTaskPoll(), 4000);
    },
    mvTaskRunning(){
      return !!(this.mvTask && ['queued','waiting','rendering'].includes(this.mvTask.status));
    },

    // ── Song-P5/F5: 风格魔改（同一首歌换编曲曲风再演绎，可叠加角色声）──
    startRemix(histId, name){
      if(!histId){ this.showToast('这首歌还没入历史，没法作为魔改参考','error'); return; }
      if(!this.songCreateReady()){ this.showToast('风格魔改需要原创歌引擎在线（ACE-Step），先去「原创歌」页看部署指引','error'); return; }
      this.createRemixOf=histId; this.createRemixName=name||('历史 #'+histId);
      this.setSingMode('create');
      this.showToast('🎛 魔改模式：给《'+this.createRemixName+'》选个新曲风，同一首歌换种编曲','info');
    },
    clearRemix(){ this.createRemixOf=null; this.createRemixName=''; this.remixBatchSel=[]; this.remixBatchMsg=''; },

    // ── Song-P6: 唱腔 LoRA（音乐人格）──
    songLoras(){ try{ return ((this.songCaps.create||{}).capabilities||{}).loras||[]; }catch(_){ return []; } },
    loraLabel(n){ return n==='ACE-Step-v1-chinese-rap-LoRA' ? '中文说唱 · RapMachine' : n; },

    // ── Song-P6: 批量魔改（一首歌 × N 风格，专辑化素材流水线）──
    toggleRemixBatch(label){
      const i=this.remixBatchSel.indexOf(label);
      if(i>=0) this.remixBatchSel.splice(i,1);
      else if(this.remixBatchSel.length>=6) this.showToast('一批最多 6 个风格','error');
      else this.remixBatchSel.push(label);
    },
    async doRemixBatch(){
      if(this.remixBatchBusy || !this.createRemixOf) return;
      const styles=this.createStylePresets
        .filter(p=>this.remixBatchSel.includes(p.label))
        .map(p=>({style:p.v, label:p.label}));
      if(!styles.length){ this.showToast('先勾选要出的风格（可多选）','error'); return; }
      this.remixBatchBusy=true; this.remixBatchMsg='';
      try{
        const r=await fetch(HUB+'/api/song/remix_batch',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({history_id:this.createRemixOf, styles,
            remix_strength:+this.createRemixStrength||0.5, quality:this.createQuality,
            profile:this.active||'', lora:this.createLora||''})});
        const d=await r.json().catch(()=>({}));
        if(r.ok && d.ok){
          const n=(d.tasks||[]).length, bad=(d.errors||[]).length;
          this.remixBatchMsg='已排产 '+n+' 版'+(bad?('（'+bad+' 版提交失败）'):'')+'——进度看「📋 我的任务」，完成后进历史';
          this.showToast('🎛 批量魔改已排产 '+n+' 版：《'+this.createRemixName+'》×'+(d.tasks||[]).map(t=>t.label).join('/'),'success');
          this.songBoardShow=true; this.songBoardRefresh();
          this.remixBatchSel=[];
        }else{
          this.remixBatchMsg='';
          this.showToast(this.songHumanErr((d&&d.detail)||('HTTP '+r.status)),'error');
        }
      }catch(e){ this.showToast(this.songHumanErr(e),'error'); }
      finally{ this.remixBatchBusy=false; }
    },

    // ── Song-P2: 15 秒高光 MV（副歌自动选段 + 口型演唱视频）──
    async doSongMv(histId, force=false){
      if(this.mvBusy || !histId) return;
      this.mvBusy=true; this.mvUrl=''; this.mvInfo=null;
      this.showToast('🎬 正在做 15 秒高光 MV（自动选副歌 + 口型演唱，约半分钟）…','info');
      try{
        const r=await fetch(HUB+'/api/song/mv',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({history_id:histId, force:force})});
        const d=await r.json().catch(()=>({}));
        if(r.ok && d.ok){
          this.mvUrl=HUB+d.url; this.mvInfo=d;
          this.showToast('🎬 MV 做好了：副歌从 '+Math.round(d.start_s)+'s 起，'+Math.round(d.seconds)+' 秒','success');
        }else if(r.status===409 && !force){
          // P3/O6: 直播让路——出片会抢直播算力，问一句再强制
          this.mvBusy=false;
          if(confirm((d.detail||'直播进行中')+'\n\n仍要现在出片吗？')) return this.doSongMv(histId, true);
          return;
        }else{
          this.showToast(d.detail||'MV 生成失败','error');
        }
      }catch(e){ this.showToast('MV 生成失败：'+e,'error'); }
      finally{ this.mvBusy=false; }
    },
    downloadMv(){
      if(!this.mvUrl) return;
      const a=document.createElement('a');
      a.href=this.mvUrl; a.download='高光MV_'+(this.active||'角色')+'.mp4';
      document.body.appendChild(a); a.click(); a.remove();
    },

    // ── 历史记录 ──
    async loadHistory(append=false) {
      if(!append) this.historyPage=1;
      this.historyLoading=true;
      try {
        const params=new URLSearchParams({limit:this.historyPageSize});
        if(this.historySearch.trim()) params.set('search', this.historySearch.trim());
        if(this.historyStarredOnly)   params.set('starred_only','true');
        if(this.historyEmotion)       params.set('emotion', this.historyEmotion);
        if(this.historyPage>1)        params.set('offset', (this.historyPage-1)*this.historyPageSize);
        const d=await fetch(HUB+'/api/history?'+params).then(r=>r.json());
        const recs=(d.records||[]).map(i=>({...i, _open:false}));
        if(append) this.historyList=[...this.historyList, ...recs];
        else       this.historyList=recs;
        this.historyHasMore   = recs.length===this.historyPageSize;
        this.historyTotalCount= d.total_count ?? this.historyList.length;
        if(!append) this.loadHistoryStats();
      } catch(e){ if(!append) this.historyList=[]; }
      finally{ this.historyLoading=false; }
    },
    async loadHistoryIncremental() {
      // P12-5: WS 触发时只取最新记录，追加到列表头部
      if(this.historySearch.trim()||this.historyStarredOnly||this.historyEmotion){
        // 如果有活跃过滤器，全量重载才能保证顺序正确
        return this.loadHistory();
      }
      try {
        const latestTs = this.historyList[0]?.ts || 0;
        if(!latestTs) return this.loadHistory(); // 列表为空时全量加载
        const d = await fetch(HUB+'/api/history?since_ts='+latestTs+'&limit=50').then(r=>r.json());
        const recs = (d.records||[]).map(i=>({...i, _open:false}));
        if(recs.length) this.historyList = [...recs, ...this.historyList];
      } catch(_){}
    },
    async loadMoreHistory() {
      this.historyPage++;
      await this.loadHistory(true);
    },
    async loadHistoryStats() {
      try{
        const d = await fetch(HUB+'/api/history/stats').then(r=>r.json());
        if(d.ok) this.historyStats={
          today:          d.today          || 0,
          week:           d.week           || 0,
          total:          d.total          || 0,
          avg_elapsed_ms: d.avg_elapsed_ms || 0,
          most_used:      d.most_used      || null,
          distribution:   d.distribution   || [],
          max_count:      d.max_count      || 1,
          daily_7:        d.daily_7        || [],
        };
      }catch(_){}
    },
    updateHistoryStats() { /* 已由 loadHistoryStats() 服务端全量聊计替代 */ },
    async starHistory(item) {
      item.starred=!item.starred;
      try { await fetch(HUB+`/api/history/${item.id}/star?starred=${item.starred}`,{method:'POST'}); } catch(e){}
    },
    async deleteHistoryItem(id) {
      if(!confirm('删除此条记录？')) return;
      try {
        await fetch(HUB+`/api/history/${id}`,{method:'DELETE'});
        this.historyList=this.historyList.filter(i=>i.id!==id);
        this.updateHistoryStats();
      } catch(e){ this.showToast('删除失败','error'); }
    },
    async deleteAllHistory() {
      await fetch(HUB+'/api/history',{method:'DELETE'});
      this.historyList=[];
      this.updateHistoryStats();
    },

    // ── 情感对比 ──
    toggleCompare(emotion) {
      const idx = this.compareSelected.indexOf(emotion);
      if(idx>=0){
        if(this.compareSelected.length>2) this.compareSelected.splice(idx,1);
      } else {
        if(this.compareSelected.length<4) this.compareSelected.push(emotion);
      }
    },
    async doCompare() {
      if(!this.speakText.trim()||!this.active||this.compareSelected.length<2) return;
      this.compareLoading=true;
      // 初始化占位（每个情感一个 loading 条目）
      const init={};
      this.compareSelected.forEach(e=>{ init[e]={loading:true, audioSrc:'', elapsed:0, error:false, errorMsg:''}; });
      this.compareResultMap=init;
      // 并行发起（服务端 _TTS_LOCK 会自动串行）
      await Promise.all(this.compareSelected.map(async emotion=>{
        try{
          const r=await fetch(HUB+'/avatar/speak',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({text:this.speakText,profile:this.active,language:this.speakLang,emotion})
          });
          const d=await r.json();
          if(!r.ok || !d.audio_base64){
            // 显示详细错误信息
            const errMsg = d.detail || d.error || `HTTP ${r.status}`;
            this.compareResultMap={...this.compareResultMap,
              [emotion]:{loading:false, audioSrc:'', elapsed:0, error:true, errorMsg: errMsg}};
          } else {
            this.compareResultMap={...this.compareResultMap,
              [emotion]:{loading:false, audioSrc:'data:audio/wav;base64,'+d.audio_base64,
                         elapsed:d.elapsed_ms||0, error:false, errorMsg:''}};
          }
        } catch(err){
          this.compareResultMap={...this.compareResultMap, [emotion]:{loading:false, audioSrc:'', elapsed:0, error:true, errorMsg: String(err)}};
        }
      }));
      this.compareLoading=false;
    },

    // P22-4: 角色A/B对比方法
    toggleAbCompare(name){
      if(this.abCompareSelected.includes(name)){
        this.abCompareSelected = this.abCompareSelected.filter(n=>n!==name);
      } else if(this.abCompareSelected.length < 5){
        this.abCompareSelected.push(name);
      }
    },
    async doAbCompare(){
      if(!this.speakText.trim()||this.abCompareSelected.length<2) return;
      this.abCompareLoading = true;
      this.abCompareResults = [];
      try{
        const items = this.abCompareSelected.map(profile => ({
          text: this.speakText,
          profile: profile,
          emotion: this.speakEmotion,
          language: this.speakLang
        }));
        const d = await fetch(HUB+'/api/ab_compare', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(items)
        }).then(r=>r.json());
        if(d.ok){
          this.abCompareResults = d.results || [];
        } else {
          this.showToast('A/B对比失败: '+(d.detail||'未知错误'), 'error');
        }
      }catch(e){
        this.showToast('A/B对比请求失败: '+e, 'error');
      } finally {
        this.abCompareLoading = false;
      }
    },

    async doEngineAb(){
      if(!this.active||!this.speakText.trim()) return;
      this.engAbLoading=true; this.engAbResults=[]; this.engAbWinner='';
      try{
        const d=await fetch(HUB+'/api/engine_ab_compare',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, text:this.speakText, language:this.speakLang,
            emotion:this.speakEmotion==='auto'?'neutral':this.speakEmotion})
        }).then(r=>r.json());
        if(d.ok){
          this.engAbResults=d.results||[];
          this.engAbWinner=d.winner_cosine||'';
        } else {
          this.showToast('引擎对比失败: '+(d.detail||'未知'),'error');
        }
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.engAbLoading=false; }
    },

    async saveProbeSentences(){
      if(!this.active) return;
      const sents=this.probeSentencesText.split('\n').map(x=>x.trim()).filter(Boolean);
      try{
        const d=await fetch(HUB+`/profiles/${enc(this.active)}`,{
          method:'PATCH', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({probe_sentences:sents})
        }).then(r=>r.json());
        if(d.ok) this.showToast('已保存 '+sents.length+' 条体检语料','success');
        else this.showToast('保存失败','error');
      }catch(e){ this.showToast('保存失败: '+e,'error'); }
    },

    // ── 音质优化：seed 校准 / best-of-N 择优 / 盲听 A/B ──
    async vqCalibrate(){
      if(!this.active){ this.showToast('请先激活角色','error'); return; }
      this.vqCalibLoading=true; this.vqCalibResult=null;
      try{
        const d=await fetch(HUB+'/api/calibrate_seed',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, save:true})}).then(r=>r.json());
        if(d.ok){ this.vqCalibResult=d; this.showToast('校准完成，最佳 seed='+d.best?.seed+(d.saved?'（已固定）':''),'success'); }
        else this.showToast('校准失败: '+(d.detail||'未知'),'error');
      }catch(e){ this.showToast('校准请求失败: '+e,'error'); }
      finally{ this.vqCalibLoading=false; }
    },
    async vqBestOfRun(){
      if(!this.active||!this.speakText.trim()) return;
      this.vqBestLoading=true; this.vqBestResult=null;
      try{
        const d=await fetch(HUB+'/api/tts_only',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, text:this.speakText, language:this.speakLang,
            emotion:this.speakEmotion==='auto'?'neutral':this.speakEmotion, best_of:this.vqBestOf})}).then(r=>r.json());
        if(d.ok){ this.vqBestResult=d; }
        else this.showToast('择优合成失败: '+(d.detail||'未知'),'error');
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqBestLoading=false; }
    },
    async vqBlindGen(){
      if(!this.active||!this.speakText.trim()) return;
      this.vqBlindLoading=true; this.vqBlind=[]; this.vqBlindPicked=-1; this.vqBlindReveal=false;
      const pool=[42,123,777,2024,8888,31415];
      const a=pool[Math.floor(Math.random()*pool.length)];
      let b=pool[Math.floor(Math.random()*pool.length)]; while(b===a) b=pool[Math.floor(Math.random()*pool.length)];
      const seeds=[a,b];
      try{
        const outs=[];
        for(const sd of seeds){
          const d=await fetch(HUB+'/api/tts_only',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({profile:this.active, text:this.speakText, language:this.speakLang,
              emotion:this.speakEmotion==='auto'?'neutral':this.speakEmotion, fish_params:{seed:sd}})}).then(r=>r.json());
          if(!d.ok){ this.showToast('生成失败: '+(d.detail||'未知'),'error'); this.vqBlindLoading=false; return; }
          outs.push({seed:sd, audio:d.audio_base64});
        }
        if(Math.random()<0.5) outs.reverse();   // 随机左右，盲听
        outs[0].label='A'; outs[1].label='B';
        this.vqBlind=outs;
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqBlindLoading=false; }
    },
    async vqBlindPick(i){
      this.vqBlindPicked=i; this.vqBlindReveal=true;
      const seed=this.vqBlind[i]?.seed;
      if(seed==null) return;
      try{
        const d=await fetch(HUB+'/api/set_profile_seed',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, seed:seed})}).then(r=>r.json());
        if(d.ok) this.showToast('已固定 seed='+seed,'success');
        else this.showToast('固定失败: '+(d.detail||'未知'),'error');
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
    },
    async vqLoadSegments(){
      if(!this.active) return;
      try{
        const d=await fetch(HUB+'/api/voice_pack/segments/'+encodeURIComponent(this.active)).then(r=>r.json());
        this.vqSegments=d.segments||[];
        if(!this.vqListenAlt && this.vqSegments.length){
          const alt=this.vqSegments.find(s=>!s.active);
          if(alt) this.vqListenAlt=alt.name;
        }
      }catch(e){ this.vqSegments=[]; }
    },
    async vqListenAb(){
      if(!this.active||!this.speakText.trim()||!this.vqListenAlt) return;
      this.vqListenLoading=true; this.vqListen=[]; this.vqListenPicked=-1; this.vqListenReveal=false;
      try{
        const d=await fetch(HUB+'/api/listening_ab',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, text:this.speakText, alt_ref:this.vqListenAlt,
            language:this.speakLang, emotion:this.speakEmotion==='auto'?'neutral':this.speakEmotion})}).then(r=>r.json());
        if(!d.ok){ this.showToast('听感 A/B 失败: '+(d.detail||'未知'),'error'); return; }
        const rs=(d.results||[]).filter(r=>r.ok).map(r=>({
          audio:r.audio_base64, cosine:r.cosine, naturalness:r.naturalness,
          origLabel:r.label, label:r.label, segment:r.segment||'', is_current:!!r.is_current,
        }));
        if(rs.length<2){ this.showToast('合成不足两条','error'); return; }
        if(Math.random()<0.5) rs.reverse();
        rs[0].label='A'; rs[1].label='B';
        this.vqListen=rs;
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqListenLoading=false; }
    },
    vqListenPick(i){
      this.vqListenPicked=i; this.vqListenReveal=true;
    },
    async vqListenApply(force=false){
      const picked=this.vqListen[this.vqListenPicked];
      if(!picked?.segment || picked.is_current){
        this.showToast('当前参考已是落库参考','info'); return;
      }
      this.vqListenApplying=true;
      try{
        const d=await fetch(HUB+'/api/apply_segment_ref',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, segment:picked.segment, force:!!force})}).then(r=>r.json());
        if(d.applied){
          this.showToast(`✅ 已应用 ${picked.segment}`,'success');
          await this.loadProfiles(); await this.vqLoadSegments();
        } else if(d.can_force && !force){
          this.showToast('holdout 未通过：'+((d.gate||{}).reason||d.detail||''),'error');
        } else if(d.already_active){
          this.showToast('该参考已是当前参考','info');
        } else {
          this.showToast('应用失败: '+(d.detail||'未知'),'error');
        }
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqListenApplying=false; }
    },
    async vqListenFeedback(rating){
      try{
        await fetch(HUB+'/api/metrics/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, rating, source:'ui_listen_ab',
            text_preview:(this.speakText||'').slice(0,80)})});
        this.showToast(rating>0?'已记录好评 👍':'已记录反馈 👎','success');
      }catch(e){ this.showToast('反馈失败: '+e,'error'); }
    },
    vqOptPickFile(ev){
      const f=ev.target.files?.[0];
      if(!f){ this.vqOptFile=''; this.vqOptFileName=''; return; }
      this.vqOptFileName=f.name;
      const rd=new FileReader();
      rd.onload=()=>{ const s=rd.result; this.vqOptFile=(s.indexOf(',')>=0)?s.split(',')[1]:s; };
      rd.readAsDataURL(f);
    },
    async _vqPollJob(jid){
      for(let i=0;i<360;i++){
        await new Promise(r=>setTimeout(r,2500));
        const s=await fetch(HUB+'/api/optimize_references/status?job_id='+jid).then(r=>r.json()).catch(()=>null);
        if(!s) continue;
        if(s.progress) this.vqOptProgress=s.progress;
        if(s.status==='done'){
          this.vqOptResult=s.result;
          const pr=s.result?.probe;
          const cos=(pr?.quality_axes||{}).cosine ?? pr?.cosine;
          let msg=s.result?.applied?'已应用更优参考集':'优化完成（维持现状）';
          if(cos!=null) msg+=` · cos ${Number(cos).toFixed(3)}`;
          this.showToast(msg,'success');
          if(s.result?.applied) this.loadProfiles();
          return true;
        }
        if(s.status==='error'){ this.showToast('优化失败: '+(s.error||''),'error'); return false; }
      }
      this.showToast('优化超时','error');
      return false;
    },
    async vqOptimizeSegments(){
      if(!this.active) return;
      this.vqOptLoading=true; this.vqOptResult=null; this.vqOptProgress='预切参考…';
      try{
        const d=await fetch(HUB+`/api/optimize_references/segments/${encodeURIComponent(this.active)}?max_refs=${this.vqOptMaxRefs}`,
          {method:'POST'}).then(r=>r.json());
        if(!d.ok||!d.job_id){ this.showToast('启动失败','error'); return; }
        await this._vqPollJob(d.job_id);
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqOptLoading=false; }
    },
    async vqOptimizeVoicePack(){
      if(!this.active) return;
      if(!confirm('从声音包长录音切段优化（约 5~15 分钟），演讲域 holdout 验证，仅更优才应用？')) return;
      this.vqOptLoading=true; this.vqOptResult=null; this.vqOptProgress='声音包…';
      try{
        const d=await fetch(HUB+`/api/optimize_references/voice_pack/${encodeURIComponent(this.active)}?max_refs=${this.vqOptMaxRefs}`,
          {method:'POST'}).then(r=>r.json());
        if(!d.ok||!d.job_id){ this.showToast('启动失败','error'); return; }
        await this._vqPollJob(d.job_id);
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqOptLoading=false; }
    },
    async vqOptimize(){
      if(!this.active) return;
      this.vqOptLoading=true; this.vqOptResult=null; this.vqOptProgress='启动…';
      try{
        const d=await fetch(HUB+'/api/optimize_references',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active, source_b64:this.vqOptFile||'',
            max_refs:this.vqOptMaxRefs, apply_if_better:true, use_speech_corpus:true})}).then(r=>r.json());
        if(!d.ok||!d.job_id){ this.showToast('启动失败','error'); return; }
        await this._vqPollJob(d.job_id);
      }catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.vqOptLoading=false; }
    },

    // ── 移动端遥控 ──
    async genMobileQr() {
      this.mobileQrLoading=true;
      try{
        const d=await fetch(HUB+'/mobile/token',{method:'POST'}).then(r=>r.json());
        if(d.ok) this.mobileQr=d;
        else this.showToast('生成遥控码失败','error');
      } catch(e){ this.showToast('请求失败: '+e,'error'); }
      finally{ this.mobileQrLoading=false; }
    },

    // ── 批量配音（SSE 实时进度）──
    _saveBatchProgress() {
      try {
        localStorage.setItem('avatarhub_batch_last', JSON.stringify({
          text:this.batchText, lang:this.batchLang, emotion:this.batchEmotion,
          lines:this.batchLines.map(l=>({index:l.index,text:l.text,status:l.status,elapsed:l.elapsed,emotion:l.emotion})),
          runMs:this.batchRunMs||0, savedHist:!!this.batchSavedHist,  // UI-P5-4: 成绩单跨刷新
          ts:Date.now()
        }));
      }catch(_){}
      // P18-2: 单独保存 audioMap（大文件，Quota 超限时静默跳过）
      try {
        const entries = Object.entries(this.batchAudioMap);
        if(entries.length) localStorage.setItem('avatarhub_batch_audio',
          JSON.stringify(this.batchAudioMap));
        else localStorage.removeItem('avatarhub_batch_audio');
      }catch(_){ localStorage.removeItem('avatarhub_batch_audio'); }
    },
    // P14-1 + P15-5: 流式多句停止，停止后 toast 反馈已播句数
    stopStream() {
      const played = this.streamQueueCtrl?.playIdx || 0;  // P15-5: 捕获已播数
      const total  = this.streamQueueCtrl?.buf?.length || 0;
      if(this.streamAbortCtrl) this.streamAbortCtrl.abort();
      if(this.streamQueueCtrl) this.streamQueueCtrl.stopped=true;
      const p=this.$refs.player;
      if(p){ p.pause(); p.onended=null; }
      this.audioSrc='';
      this.streamLoading=false; this.streamInfo='';
      this.streamAbortCtrl=null; this.streamQueueCtrl=null;
      if(played>0) this.showToast('已停止，已播 '+played+(total>played?'/'+total:'')+'句','info');  // P15-5
    },
    stopBatch() {
      if(this.batchAbortCtrl) this.batchAbortCtrl.abort();
      this.batchStopped=true;
      this._saveBatchProgress();  // P12-1: 停止时立即持久化进度
    },
    resumeBatch() {
      const remaining = this.batchLines
        .filter(l=>l.status==='pending'||l.status==='error')
        .map(l=>l.text);
      if(!remaining.length) return;
      this.batchText = remaining.join('\n');
      this.batchStopped=false;
      this.$nextTick(()=>this.doBatch());
    },
    async doBatch() {
      if(!this.batchText.trim()||!this.active) return;
      // P20-4: 解析每行可选的「[角色名] 文本」语法
      const profileNames = new Set(this.profiles.map(p=>p.name));
      const lines = this.batchText.split('\n').map((t,i)=>{
        t = t.trim(); if(!t) return null;
        const m = t.match(/^\[([^\]]+)\]\s+(.+)/);
        if(m && profileNames.has(m[1])) return {index:i+1, text:m[2], profile:m[1], status:'pending', elapsed:0};
        if(m && !profileNames.has(m[1])) this.showToast(`角色「${m[1]}」不存在，已降级为默认角色`,'info');
        return {index:i+1, text:t, profile:this.active, status:'pending', elapsed:0};
      }).filter(Boolean);
      if(lines.length>500){ this.batchError='最多500行，当前'+lines.length+'行'; return; }
      // P19-4: 重复行检测
      const uniqTexts = new Set(lines.map(l=>l.text));
      const dupCount = lines.length - uniqTexts.size;
      if(dupCount>0) this.showToast(`⚠️ 发现 ${dupCount} 行重复文本，将正常合成`, 'info');
      this._batchAudioStop();   // UI-P7-2: 开新一批前掐掉上批的试听/连播（audioMap 即将被覆写）
      this.batchLines=lines; this.batchLoading=true; this.batchDone=false;
      this.batchStopped=false;
      this.batchError=''; this.batchZipUrl='';
      this.batchAbortCtrl = new AbortController();  // P11-3
      const _runT0 = Date.now();   // UI-P5-4: 成绩单记墙钟用时（含重试），不是逐行耗时求和

      try {
        const items = lines.map(l=>({text:l.text, profile:l.profile||this.active, language:this.batchLang, emotion:this.batchEmotion}));
        const saveHist = this.batchSaveHistory && lines.length <= 20;
        this.batchSavedHist = saveHist;
        const r = await fetch(HUB+'/avatar/speak/batch/stream?save_history='+saveHist, {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify(items),
          signal: this.batchAbortCtrl.signal  // P11-3: 支持 AbortController 中止
        });
        this.batchAudioMap = {};  // P17-5: 提升为实例属性，支持单行重试后重新打包
        const reader = r.body.getReader(); const dec = new TextDecoder(); let buf='';
        while(true){
          const{value,done}=await reader.read(); if(done) break;
          buf+=dec.decode(value,{stream:true});
          const evts=buf.split('\n\n'); buf=evts.pop()||'';
          for(const evt of evts){
            const dataLine=evt.split('\n').find(l=>l.startsWith('data: '));
            if(!dataLine) continue;
            try{
              const d=JSON.parse(dataLine.slice(6));
              if(d.done) continue;
              const idx=d.index??0;
              if(idx>=1&&idx<=this.batchLines.length){
                const emo = d.emotion||this.batchEmotion;
                this.batchLines[idx-1]=
                  {...this.batchLines[idx-1], status:d.ok?'ok':'error',
                   elapsed:d.elapsed_ms||0, emotion:emo};
                // 缓存音频供后续打包（SSE 已经合成，无需再次调 TTS）
                if(d.ok && d.audio_base64){
                  this.batchAudioMap[idx]={b64:d.audio_base64, emotion:emo, text:this.batchLines[idx-1].text};
                }
                // P19-2: ETA 预估（滑动平均 × 剩余行数）
                const doneElapsed = this.batchLines.filter(l=>l.elapsed>0).map(l=>l.elapsed);
                if(doneElapsed.length>0){
                  const avg = doneElapsed.reduce((a,b)=>a+b,0)/doneElapsed.length;
                  const rem = this.batchLines.filter(l=>l.status==='pending').length;
                  this.batchETA = rem>0 ? Math.round(avg*rem/1000) : 0;
                }
                // P13-1: 每5句成功写一次 localStorage，中途崩溃也能恢复
                const okCount=this.batchLines.filter(l=>l.status==='ok').length;
                if(okCount>0 && okCount%5===0) this._saveBatchProgress();
              }
            } catch(e){}
          }
        }
        // 用 JSZip 客户端打包（文件名含情感，零二次合成）
        const audioEntries = Object.entries(this.batchAudioMap);
        if(audioEntries.length>0 && typeof JSZip !== 'undefined'){
          const zip = new JSZip();
          const manifest = [];
          for(const [idxStr, item] of audioEntries){
            const n = String(idxStr).padStart(3,'0');
            const safe = (item.text||'').slice(0,20).replace(/[\/\\:*?"<>|]/g,'_');
            const fname = `${n}_${item.emotion}_${safe}.wav`;
            zip.file(fname, item.b64, {base64:true});
            manifest.push({index:Number(idxStr), filename:fname,
              text:item.text, emotion:item.emotion,
              profile:this.active, language:this.batchLang});
          }
          zip.file('manifest.json', JSON.stringify({generated:new Date().toISOString(),
            profile:this.active, language:this.batchLang, emotion:this.batchEmotion,
            total:manifest.length, items:manifest}, null, 2));
          // P21-2: 生成 report.csv
          const csvHeader = '\uFEFF序号,文本,角色,情感,耗时(ms),状态\n';
          const csvRows = this.batchLines.map(l=>{
            const status = l.status==='ok'?'成功':l.status==='error'?'失败':'未处理';
            const safeText = (l.text||'').replace(/"/g,'""');
            return `${l.index},"${safeText}",${l.profile||this.active},${l.emotion||this.batchEmotion},${l.elapsed||0},${status}`;
          }).join('\n');
          zip.file('report.csv', csvHeader+csvRows);
          const blob = await zip.generateAsync({type:'blob', compression:'DEFLATE', compressionOptions:{level:6}});
          this.batchZipUrl = URL.createObjectURL(blob);
        } else if(audioEntries.length>0){
          // JSZip 未加载时降级：仍调旧端点
          const form=new FormData();
          const blob=new Blob([this.batchLines.filter(l=>l.status==='ok').map(l=>l.text).join('\n')],{type:'text/plain'});
          form.append('file',blob,'batch.txt');
          form.append('profile',this.active); form.append('language',this.batchLang);
          const zipResp=await fetch(HUB+'/avatar/batch_dub',{method:'POST',body:form});
          if(zipResp.ok) this.batchZipUrl=URL.createObjectURL(await zipResp.blob());
        }
        // P12-4: 批量完成后自动重试失败行（最多1次）
        const errorLines = this.batchLines.filter(l=>l.status==='error');
        if(errorLines.length) this.batchRetrying=true;  // P15-4: 标记重试阶段
        for(const errLine of errorLines){
          // P15-4: 标记该行正在重试
          const ri=errLine.index-1;
          this.batchLines[ri]={...this.batchLines[ri],status:'retrying'};
          try {
            let retryOk=false; let retryAudio='';
            const retryR = await fetch(HUB+'/avatar/speak/stream',{
              method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({text:errLine.text,profile:this.active,
                language:this.batchLang,emotion:this.batchEmotion})});
            const retryReader=retryR.body.getReader(); const retryDec=new TextDecoder(); let rBuf='';
            outer: while(true){
              const{value:rv,done:rd}=await retryReader.read(); if(rd) break;
              rBuf+=retryDec.decode(rv,{stream:true});
              const revts=rBuf.split('\n\n'); rBuf=revts.pop()||'';
              for(const re of revts){
                const rl=re.split('\n').find(l=>l.startsWith('data: '));
                if(!rl) continue;
                const rd2=JSON.parse(rl.slice(6));
                if(rd2.phase==='done'&&rd2.audio_base64){retryOk=true;retryAudio=rd2.audio_base64;break outer;}
                if(rd2.phase==='error') break outer;
              }
            }
            if(retryOk){
              this.batchLines[ri]={...this.batchLines[ri],status:'ok'};
              if(retryAudio) this.batchAudioMap[errLine.index]={b64:retryAudio,
                emotion:this.batchEmotion,text:errLine.text};
            } else {
              this.batchLines[ri]={...this.batchLines[ri],status:'error'};  // P15-4: 恢复 error
            }
          }catch(_){ this.batchLines[ri]={...this.batchLines[ri],status:'error'}; }
        }
        this.batchRetrying=false;  // P15-4: 重试阶段结束
        this.batchDone=true;
        this.batchRunMs=Date.now()-_runT0;   // UI-P5-4: 成绩单用时（含自动重试段）
        this._saveBatchProgress();  // P12-1: 完成时也持久化
      } catch(e){ this.batchError='❌ '+e; }
      finally{ this.batchLoading=false; }
    },
    // ── UI-P5-4: 批量成绩单——情感分布 top3（auto 逐行检测的成果可视化，也是决策①的口碑素材）──
    batchEmoDist(){
      const cnt={};
      for(const l of this.batchLines){
        if(l.status==='ok' && l.emotion && l.emotion!=='neutral') cnt[l.emotion]=(cnt[l.emotion]||0)+1;
      }
      return Object.entries(cnt).sort((a,b)=>b[1]-a[1]).slice(0,3)
        .map(([e,n])=>`${this.emotionEmoji(e)}${this.emotionCN(e)}×${n}`).join(' · ');
    },
    // ── UI-P6-2: 批量行内试听——不解压 ZIP 直接听单行；配合「润色」构成听-改闭环 ──
    batchPlayLine(line){
      const rec=this.batchAudioMap[line.index];
      if(!rec||!rec.b64){ this.showToast('这行音频不在本地（可能是恢复的旧进度），点该行🔄重试可再生成','info'); return; }
      if(this.batchPlayingIdx===line.index){ this._batchAudioStop(); return; }   // 再点一次=停
      this._batchAudioStop();
      const url=this._b64ToBlobUrl(rec.b64,'audio/wav');
      if(!url) return;
      const a=new Audio(url);
      this._batchAudioEl=a; this._batchAudioUrl=url; this.batchPlayingIdx=line.index;
      a.onended=()=>this._batchAudioStop();
      a.onerror=()=>this._batchAudioStop();
      a.play().catch(()=>this._batchAudioStop());
    },
    // 单曲收尾（revoke + 复位行号）；连播链在 onended 里判断 batchPlayAllOn 决定是否续播
    _batchTrackDone(){
      try{ this._batchAudioEl?.pause(); }catch(_){}
      if(this._batchAudioUrl){ try{ URL.revokeObjectURL(this._batchAudioUrl); }catch(_){} }
      this._batchAudioEl=null; this._batchAudioUrl=''; this.batchPlayingIdx=0;
    },
    _batchAudioStop(){   // 全停：掐断连播队列 + 单曲收尾（单行播放/切页/送润色都走这里）
      this.batchPlayAllOn=false; this._batchQueue=[];
      this._batchTrackDone();
    },
    // ── UI-P7-2: 连播验收——从第 1 个成功行顺序试听整批，闭眼验收长脚本 ──
    batchPlayAll(){
      if(this.batchPlayAllOn){ this._batchAudioStop(); return; }   // 再点=停
      const q=this.batchLines.filter(l=>l.status==='ok' && this.batchAudioMap[l.index]?.b64)
                             .map(l=>l.index);
      if(!q.length){ this.showToast('没有可试听的行（恢复的旧进度音频不在本地）','info'); return; }
      this._batchAudioStop();
      this.batchFoldOk=false;   // 展开已完成行，让播放高亮可见
      this.batchPlayAllOn=true; this._batchQueue=q;
      this._batchPlayNext();
    },
    _batchPlayNext(){
      const idx=this._batchQueue.shift();
      if(idx==null){ this._batchAudioStop(); return; }
      const rec=this.batchAudioMap[idx];
      const url=(rec&&rec.b64)?this._b64ToBlobUrl(rec.b64,'audio/wav'):'';
      if(!url){ this._batchPlayNext(); return; }   // 个别行缺音频/解码失败 → 跳过不断链
      const a=new Audio(url);
      this._batchAudioEl=a; this._batchAudioUrl=url; this.batchPlayingIdx=idx;
      const next=()=>{ this._batchTrackDone(); if(this.batchPlayAllOn) this._batchPlayNext(); };
      a.onended=next; a.onerror=next;
      a.play().catch(next);
    },
    // UI-P6-2: 单行送语音页微调——复用 reSynthesize（文本/情感/角色回填 + 切页 + toast 一步到位）；
    //   auto 批的行带逐行检出情感，回填的就是这行实际用的情感
    batchLineToVoice(line){
      this._batchAudioStop();
      this.reSynthesize({text:line.text, language:this.batchLang,
                         emotion:line.emotion, profile:line.profile});
    },
    // UI-P5-1 → UD1: 决策①收口读数——verdict 由后端 /metrics.decision1 单一口径判定（基线以来增量），
    //   前端只翻译成人话。终身口径 emotion_auto_rate 分母含 auto 上线前历史，结构性低估，禁止用于拍板。
    decision1Hint(){
      const d1=this.metricsData.decision1;
      if(!d1){
        // 旧后端（未重启）没有 decision1 块——诚实标注读数口径不可靠，不给拍板建议
        const auto=this.metricsData.speak_emotion_auto||0;
        return `auto 已用 ${auto} 次 · 读数基线未生效（Hub 重启后自动起算 7 天窗口）`;
      }
      const rate=Math.round((d1.auto_rate||0)*100), missR=Math.round((d1.miss_rate||0)*100);
      const missTxt=d1.miss_since>0?` · 误判 ${missR}%`:'';
      const base=`auto 使用率 ${rate}%（${d1.auto_since}/${d1.total_since}）${missTxt}`;
      switch(d1.verdict){
        case 'sample_low': return `自 ${d1.baseline_date} 起 auto 已用 ${d1.auto_since} 次 · 样本不足（需≥20 次合成），暂不切默认`;
        case 'miss_high':  return `${base} · 误判偏高（>20%），先扩词表再「重新起算」`;
        case 'pass':       return `${base} · 已满 ${Math.floor(d1.days)} 天且达标 ✅ 可把默认情感切「🤖 自动」`;
        case 'pass_wait':  return `${base} · 达标中，满 7 天再拍板（还差 ${Math.max(1,Math.ceil(7-d1.days))} 天）`;
        default:           return `${base} · 继续观察，目标≥30%（第 ${Math.max(1,Math.ceil(d1.days))}/7 天）`;
      }
    },
    // UD1: 扩词表/改检测规则后旧读数失真——从当下重打 7 天窗口（终身计数器不动，看板无感）
    async d1Rebaseline(){
      if(!confirm('重新起算会把决策①的 7 天观察窗口从现在重计（终身累计指标不受影响）。\n通常在扩情感词表或改检测规则后使用。确定？')) return;
      try{
        const r=await fetch(HUB+'/api/metrics/rebaseline',{method:'POST'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'HTTP '+r.status);
        this.metricsData.decision1=d.decision1;
        this.showToast('决策①窗口已从今天重新起算','success');
      }catch(e){ this.showToast('重新起算失败: '+((e&&e.message)||e),'error'); }
    },
    // UI-P7: 「情感不对？」一键反馈——切手选（预选检出的情感便于对照换别的）+ 计误判指标。
    //   旧后端无此端点时 404 被静默吞掉，UI 行为（切手选）不受影响
    emotionAutoMiss(){
      const det=this.streamEmo||this.speakDetectedEmotion||'';
      this.speakEmotion=(det&&det!=='neutral')?det:'neutral';
      this.emotionsExpanded=true;
      try{ fetch(HUB+'/api/metrics/emotion_miss',{method:'POST'}).catch(()=>{}); }catch(_){}
      this.showToast('已切到手选（预选：'+(det?this.emotionCN(det):'普通')+'）——点别的情感胶囊换一种，再点「开口说话」','info');
    },

    // ── P17-5: 单行手动重试 ──
    async retrySingleLine(line) {
      const ri = line.index - 1;
      this.batchLines[ri] = {...this.batchLines[ri], status:'retrying'};
      try {
        let retryOk=false, retryAudio='';
        const r = await fetch(HUB+'/avatar/speak/stream', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({text:line.text, profile:this.active,
            language:this.batchLang, emotion:this.batchEmotion})
        });
        const reader=r.body.getReader(); const dec=new TextDecoder(); let buf='';
        outer: while(true){
          const{value,done}=await reader.read(); if(done) break;
          buf+=dec.decode(value,{stream:true});
          const evts=buf.split('\n\n'); buf=evts.pop()||'';
          for(const ev of evts){
            const dl=ev.split('\n').find(l=>l.startsWith('data: ')); if(!dl) continue;
            const d=JSON.parse(dl.slice(6));
            if(d.phase==='done'&&d.audio_base64){retryOk=true;retryAudio=d.audio_base64;break outer;}
            if(d.phase==='error') break outer;
          }
        }
        if(retryOk){
          this.batchLines[ri]={...this.batchLines[ri],status:'ok'};
          if(retryAudio) this.batchAudioMap[line.index]={b64:retryAudio,
            emotion:this.batchEmotion, text:line.text};
          // 自动重新打包 ZIP（若 JSZip 可用）
          await this._rebuildBatchZip();
          this.showToast(`#${line.index} 重试成功，ZIP 已更新`,'success');
        } else {
          this.batchLines[ri]={...this.batchLines[ri],status:'error'};
          this.showToast(`#${line.index} 重试仍失败`,'error');
        }
      } catch(e) {
        this.batchLines[ri]={...this.batchLines[ri],status:'error'};
        this.showToast(`#${line.index} 重试异常: `+e,'error');
      }
    },

    async _rebuildBatchZip() {
      const audioEntries = Object.entries(this.batchAudioMap);
      if(!audioEntries.length || typeof JSZip==='undefined') return;
      const zip=new JSZip();
      for(const [idxStr,item] of audioEntries){
        const n=String(idxStr).padStart(3,'0');
        const safe=(item.text||'').slice(0,20).replace(/[\/\\:*?"<>|]/g,'_');
        zip.file(`${n}_${item.emotion}_${safe}.wav`, item.b64, {base64:true});
      }
      const blob=await zip.generateAsync({type:'blob',compression:'DEFLATE',compressionOptions:{level:6}});
      if(this.batchZipUrl) URL.revokeObjectURL(this.batchZipUrl);
      this.batchZipUrl=URL.createObjectURL(blob);
    },

    // ── 声音克隆 ──
    // 上传/录音/质检管线数据域适配器：'clone'=克隆向导（沿用平铺字段名，HTML 绑定零改动）；
    // 'newP'=照片建角色表单（newVoice.* 统一字段）。两域状态互不干扰。
    _vc(tgt){
      if(tgt==='newP') return this.newVoice;
      if(tgt==='editP') return this.editVoice;
      const self=this;
      return {
        get audioB64(){return self.cloneAudioB64}, set audioB64(v){self.cloneAudioB64=v},
        get audioName(){return self.cloneAudioName}, set audioName(v){self.cloneAudioName=v},
        get quality(){return self.cloneQuality}, set quality(v){self.cloneQuality=v},
        get recCheck(){return self.recCheck}, set recCheck(v){self.recCheck=v},
        get override(){return self.recOverride}, set override(v){self.recOverride=v},
        get checking(){return self.cloneQualityLoading}, set checking(v){self.cloneQualityLoading=v},
        get playUrl(){return ''}, set playUrl(v){},   // 向导无内嵌播放器
      };
    },
    onCloneAudio(ev) { this.vcAcceptFile(ev.target.files[0],'clone'); },
    onNewVoiceFile(ev) { this.vcAcceptFile(ev.target.files[0],'newP'); ev.target.value=''; },
    // 文件选择 / 拖拽共用：读取→（非 WAV 先浏览器内转码）→质检（tgt 路由数据域）
    vcAcceptFile(file, tgt) {
      if(!file) return;
      if(file.size > 20*1024*1024){ this.showToast('音频过大（>20MB），请截取 15~30 秒片段','error'); return; }
      const st=this._vc(tgt);
      st.audioName=file.name;
      const isWav=/\.wav$/i.test(file.name||'') || /audio\/(wav|x-wav)/i.test(file.type||'');
      const reader=new FileReader();
      reader.onload=async e=>{
        let b64=e.target.result.split(',')[1], playUrl=e.target.result;
        if(!isWav){
          // MP3/M4A 等：质检（wave.open）与 TTS 参考音管线只认 WAV，浏览器内解码转 16bit PCM WAV
          try{
            const ab=await file.arrayBuffer();
            const ctx=new (window.AudioContext||window.webkitAudioContext)();
            const buf=await ctx.decodeAudioData(ab); try{ ctx.close(); }catch(_){}
            b64=this._audioBufToWavB64(buf); playUrl='data:audio/wav;base64,'+b64;
          }catch(_){ /* 解码失败 → 按原始字节继续（质检会给出明确原因） */ }
        }
        st.audioB64=b64;
        st.playUrl=playUrl;
        await this._runQualityChecks(b64, tgt);
      };
      reader.readAsDataURL(file);
    },
    onCloneFile(file) { this.vcAcceptFile(file,'clone'); },
    // P0：拖拽上传（校验为音频；录音进行中忽略）
    _vcDropFile(ev){
      if(this.recording) return null;
      const file=ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if(!file) return null;
      if(!/^audio\//.test(file.type||'') && !/\.(wav|mp3|m4a|flac|ogg|aac)$/i.test(file.name||'')){
        this.showToast('请拖入音频文件（WAV / MP3 等）','error'); return null;
      }
      return file;
    },
    cloneDrop(ev) {
      this.cloneDrag=false;
      const f=this._vcDropFile(ev); if(f) this.vcAcceptFile(f,'clone');
    },
    newVoiceDrop(ev) {
      this.newVoice.drag=false;
      const f=this._vcDropFile(ev); if(f) this.vcAcceptFile(f,'newP');
    },
    // ── 编辑抽屉「换一段新声音」面板（复用同一管线，数据域=editVoice）──
    _resetCapVoice(st){
      st.audioB64=''; st.audioName=''; st.drag=false; st.quality=null; st.recCheck=null;
      st.override=false; st.checking=false; st.agreed=false; st.playUrl='';
      if('open' in st) st.open=false;
    },
    editVoiceToggle(){
      if(this.editVoice.open){
        // 收起=放弃：录音中先停，已录内容一并丢弃，保存回退到上方下拉框的选择
        if(this.recording && this._recTarget==='editP') this.stopRec();
        this._resetCapVoice(this.editVoice);
      } else {
        this.editVoice.open=true;
      }
    },
    onEditVoiceFile(ev) { this.vcAcceptFile(ev.target.files[0],'editP'); ev.target.value=''; },
    editVoiceDrop(ev) {
      this.editVoice.drag=false;
      const f=this._vcDropFile(ev); if(f) this.vcAcceptFile(f,'editP');
    },

    // 文件上传与现场录音共用：并行跑克隆门槛质检 + 录音深度自检
    async _runQualityChecks(b64, tgt='clone'){
      const st=this._vc(tgt);
      st.quality=null; st.recCheck=null; st.override=false;
      st.checking=true;
      const p1=fetch(HUB+'/api/voice_clone/check_quality',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({wav_base64:b64})
        }).then(r=>r.json()).catch(err=>({ok:false,reason:'质检请求失败: '+err}));
      const p2=fetch(HUB+'/api/recording_check',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({audio_base64:b64})
        }).then(r=>r.json()).catch(()=>null);
      try{
        const [q,rc]=await Promise.all([p1,p2]);
        st.quality=q; st.recCheck=rc;
      } finally{ st.checking=false; }
    },

    // ── 现场录音（MediaRecorder → 浏览器内转 16bit PCM WAV → 同款质检管线）──
    // 硬件态（recorder/recording/recSecs…）全局唯一：同一时刻只允许一路录音；结果按 _recTarget 路由。
    async startRec(tgt='clone'){
      if(this.recording) return;
      if(!navigator.mediaDevices || !window.MediaRecorder){
        this.showToast('当前浏览器不支持录音，请改用文件上传','error'); return; }
      try{
        this.recStream=await navigator.mediaDevices.getUserMedia(
          {audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      }catch(e){ this.showToast(this.micErrText(e),'error'); return; }
      this._recTarget=tgt;
      this.recChunks=[];
      this.recMime=MediaRecorder.isTypeSupported('audio/webm')?'audio/webm':'';
      this.recorder=new MediaRecorder(this.recStream, this.recMime?{mimeType:this.recMime}:undefined);
      this.recorder.ondataavailable=e=>{ if(e.data && e.data.size) this.recChunks.push(e.data); };
      this.recorder.onstop=()=>this._onRecStop();
      this.recorder.start(); this.recording=true; this.recSecs=0;
      this._startRecMeter();   // 实时电平反馈：证明「正在收到声音」，静音 3s 即提醒
      // 上限对齐质检门槛 MAX_DURATION_SEC=30s（原 60s 上限录满必被质检拒绝）
      this.recTimer=setInterval(()=>{ this.recSecs++; if(this.recSecs>=30) this.stopRec(); },1000);
    },
    stopRec(){
      if(!this.recording) return;
      clearInterval(this.recTimer); this.recording=false;
      this._stopRecMeter();
      try{ this.recorder.stop(); }catch(e){}
      if(this.recStream){ try{ this.recStream.getTracks().forEach(t=>t.stop()); }catch(e){} }
    },
    // ── 录音电平表（与 P20-1 播放波形的 _audioCtx 互不相干；只分析不外放，无回授啸叫）──
    _startRecMeter(){
      this.recLevel=0; this.recSilentSecs=0;
      try{
        this._recCtx=new (window.AudioContext||window.webkitAudioContext)();
        this._recAnalyser=this._recCtx.createAnalyser();
        this._recAnalyser.fftSize=512; this._recAnalyser.smoothingTimeConstant=0.6;
        this._recCtx.createMediaStreamSource(this.recStream).connect(this._recAnalyser);
        const data=new Uint8Array(this._recAnalyser.fftSize);
        this._recMeterT=setInterval(()=>{
          this._recAnalyser.getByteTimeDomainData(data);
          let sum=0;
          for(let i=0;i<data.length;i++){ const v=(data[i]-128)/128; sum+=v*v; }
          const rms=Math.sqrt(sum/data.length);
          // 对数映射：-40dBFS→0%，-6dBFS→100%（人声 RMS 常见 -26~-14dBFS，落在条中段）
          const db=20*Math.log10(rms||1e-4);
          this.recLevel=Math.round(Math.min(1,Math.max(0,(db+40)/34))*100);
          this.recSilentSecs = this.recLevel<8 ? +(this.recSilentSecs+0.1).toFixed(1) : 0;
        },100);
      }catch(_){ /* 电平仅是辅助反馈，失败不影响录音本体 */ }
    },
    _stopRecMeter(){
      if(this._recMeterT){ clearInterval(this._recMeterT); this._recMeterT=null; }
      try{ this._recCtx && this._recCtx.close(); }catch(_){}
      this._recCtx=null; this._recAnalyser=null;
      this.recLevel=0; this.recSilentSecs=0;
    },
    async _onRecStop(){
      const tgt=this._recTarget||'clone';
      const st=this._vc(tgt);
      if(!this.recChunks.length){ this.showToast('未录到音频','error'); return; }
      st.audioName='现场录音 '+this.recSecs+'s';
      st.checking=true;
      try{
        const blob=new Blob(this.recChunks,{type:this.recMime||'audio/webm'});
        const ab=await blob.arrayBuffer();
        const ctx=new (window.AudioContext||window.webkitAudioContext)();
        const audioBuf=await ctx.decodeAudioData(ab); try{ ctx.close(); }catch(e){}
        const b64=this._audioBufToWavB64(audioBuf);
        st.audioB64=b64;
        st.playUrl='data:audio/wav;base64,'+b64;
        await this._runQualityChecks(b64, tgt);
      }catch(e){ st.checking=false; this.showToast('录音处理失败: '+e,'error'); }
    },
    _audioBufToWavB64(buf){
      const sr=buf.sampleRate; let data;
      if(buf.numberOfChannels>1){
        const a=buf.getChannelData(0), b=buf.getChannelData(1);
        data=new Float32Array(a.length);
        for(let i=0;i<a.length;i++) data[i]=(a[i]+b[i])/2;
      } else data=buf.getChannelData(0);
      const len=data.length, ab=new ArrayBuffer(44+len*2), view=new DataView(ab);
      const ws=(o,s)=>{ for(let i=0;i<s.length;i++) view.setUint8(o+i,s.charCodeAt(i)); };
      ws(0,'RIFF'); view.setUint32(4,36+len*2,true); ws(8,'WAVE'); ws(12,'fmt ');
      view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
      view.setUint32(24,sr,true); view.setUint32(28,sr*2,true); view.setUint16(32,2,true);
      view.setUint16(34,16,true); ws(36,'data'); view.setUint32(40,len*2,true);
      let off=44; for(let i=0;i<len;i++){ const s=Math.max(-1,Math.min(1,data[i])); view.setInt16(off,s<0?s*0x8000:s*0x7FFF,true); off+=2; }
      const bytes=new Uint8Array(ab); let bin=''; const ck=0x8000;
      for(let i=0;i<bytes.length;i+=ck) bin+=String.fromCharCode.apply(null,bytes.subarray(i,i+ck));
      return btoa(bin);
    },
    // R1-1: 同名检测（POST /profiles 对同名是整体覆盖——形象/引擎/RVC 全没，克隆自动建角色前必须确认）
    get cloneNameTaken(){
      const nm=(this.cloneName||'').trim();
      return !!nm && (this.profiles||[]).some(p=>p.name===nm);
    },
    async doClone() {
      if(!this.cloneAgreed||!this.cloneName.trim()||!this.cloneAudioB64) return;
      if(this.cloneCreateProfile && this.cloneNameTaken && !this.cloneOverwriteOk) return;  // R1-1: 与新建表单同级的重名保护
      this.cloneLoading=true; this.cloneStep=3; this.cloneError=''; this.cloneResult=null;
      this.cloneEngineRec=null; this.cloneEngineRecLoading=false; this.cloneEngineRecErr='';
      try{
        const d=await fetch(HUB+'/api/voice_clone',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({wav_base64:this.cloneAudioB64, name:this.cloneName.trim(),
                               agreed_terms:true, user_id:'local'})
        }).then(r=>r.json());
        if(d.ok===false) throw new Error(d.reason||d.detail||'克隆失败');
        this.cloneResult=d;
        // 修复：/api/voice_clone 返回键为 voice_b64（voice_clone.py），此前误读 d.wav_b64 致自动建角色静默失效
        this.fetchCloneEngineRec(d.voice_b64||this.cloneAudioB64);
        // 自动建 Profile（若勾选）
        if(this.cloneCreateProfile && d.voice_b64){
          try{
            const pname = this.cloneName.trim();
            const quality = this.cloneQuality
              ? {snr_db:this.cloneQuality.snr_db||0, duration_s:this.cloneQuality.duration_s||0, ts:Date.now()/1000}
              : {};
            await fetch(HUB+'/profiles',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({name:pname, voice_b64:d.voice_b64,
                description:'声音克隆 '+new Date().toLocaleDateString(), voice_quality:quality})
            });
            this.showToast('角色 '+pname+' 已自动创建！','success');
            await this.loadProfiles();
          } catch(pe){ this.showToast('角色创建失败: '+pe,'error'); }
        }
      } catch(e){ this.cloneError='❌ '+e.message; }
      finally{ this.cloneLoading=false; }
    },
    async fetchCloneEngineRec(wavB64){
      const b64=(wavB64||this.cloneAudioB64||'').trim();
      if(!b64) return;
      this.cloneEngineRecLoading=true; this.cloneEngineRec=null; this.cloneEngineRecErr='';
      try{
        const d=await fetch(HUB+'/api/clone_engine_recommend',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({wav_base64:b64, profile:(this.cloneName||'').trim(),
                               text:'你好，这是音色测试。', language:'zh-cn'})
        }).then(r=>r.json());
        if(d.ok) this.cloneEngineRec=d;
        // R1-3: 「正在对比…」之后不再静默消失——说清为什么没结果、去哪补选
        else this.cloneEngineRecErr='引擎对比暂不可用'+(d.reason?('：'+d.reason):'（语音服务未就绪）')+'，可稍后在角色编辑里手动选引擎';
      }catch(_){ this.cloneEngineRecErr='引擎对比暂不可用（语音服务未就绪），可稍后在角色编辑里手动选引擎'; }
      finally{ this.cloneEngineRecLoading=false; }
    },
    async applyCloneEngineRec(){
      const eng=this.cloneEngineRec&&this.cloneEngineRec.recommend;
      const nm=(this.cloneName||'').trim();
      if(!eng||!nm) return;
      try{
        const r=await fetch(HUB+'/profiles/'+encodeURIComponent(nm),{
          method:'PATCH', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({tts_engine:eng})
        });
        const d=await r.json();
        if(!r.ok||d.ok===false) throw new Error(d.detail||'更新失败');
        this.showToast('已设置「'+nm+'」使用 '+(eng==='qwen3_tts'?'Qwen3（高保真）':'Fish（实时）'),'success');
        await this.loadProfiles();
      }catch(e){ this.showToast('应用引擎失败: '+e.message,'error'); }
    },
    // P2：克隆成功后「用它合成一句」——已自动建档则激活该角色，再切到语音合成
    async cloneUseInVoice(){
      const nm=(this.cloneName||'').trim();
      try{ if(this.cloneCreateProfile && nm && this.activateProfile) await this.activateProfile(nm); }catch(e){}
      this.goTab('voice');   // R1-2
      this.showToast(nm?('已切到语音合成，可用「'+nm+'」试合成一句'):'已切到语音合成，选择角色后试合成','info');
    },

    // ── 历史记录 → 重新合成 ──
    async reSynthesize(item) {
      this.speakText    = item.text || '';
      this.speakLang    = item.language || 'zh-cn';
      // 'sing' 是唱歌记录的类型标记而非情感值，回填会生成非法情感 → 归一为普通
      this.speakEmotion = (item.emotion && item.emotion !== 'auto' && item.emotion !== 'sing') ? item.emotion : 'neutral';
      this.speakInstruct = '';
      // 若记录角色与当前不同则切换
      if(item.profile && item.profile !== this.active) {
        await this.activateProfile(item.profile);
      }
      this.goTab('voice');   // R1-2
      this.speakMsg = ''; this.audioSrc = '';
      this.showToast('已回填文本与情感，点击「开口说话」重新生成', 'info');
    },

    // UH1-3: 历史条目验真——与对话页/验真工具同一 /api/provenance/verify 口径，
    //   把 C2PA 凭证链串进"资产库"：交付前逐条自证来源，不用把文件挪去验真页
    // UH2 巡检：语音页结果卡复用（传 hid=卡上的 histId），验真动线四页同口径
    async verifyHistory(item, hid){
      if(item._vrfBusy) return;
      const _id = hid ?? item.id;
      if(!_id){ item._vrf='— 这条还没落历史（无法验真）'; return; }
      item._vrfBusy=true; item._vrf='';
      try{
        const d=await fetch(HUB+`/api/history/${_id}/audio`).then(r=>r.json()).catch(()=>null);
        if(!d||!d.audio_base64){ item._vrf='— 音频取回失败（可能已被清理）'; return; }
        const r=await fetch(HUB+'/api/provenance/verify',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({audio_base64:d.audio_base64})});
        if(!r.ok) throw new Error('HTTP '+r.status+(r.status===503?'（溯源模块未加载）':''));
        const v=await r.json();
        const wm=!!v.has_watermark, sig=!!v.signature_valid, ai=v.ai_generated===true;
        if(wm&&sig&&ai)      item._vrf='✅ 已验真 · 本系统生成 · 签名有效';
        else if(wm&&sig)     item._vrf='✅ 凭证有效 · 签名通过';
        else if(wm&&!sig)    item._vrf='⚠ 检出水印但签名未过（疑似被篡改）';
        else if(wm)          item._vrf='⚠ 检出 AI 水印（无完整凭证）';
        else                 item._vrf='— 未检出本系统凭证（早期记录或合成时未开水印）';
      }catch(e){ item._vrf='— 验真失败：'+(e.message||e); }
      finally{ item._vrfBusy=false; }
    },

    // ── 历史导出 ZIP ──
    async exportHistoryCsv() {
      // P13-3 + P16-5: 导出历史文本为 CSV，支持日期范围过滤
      try{
        const params = new URLSearchParams({limit: '9999'});
        if(this.csvStartDate) params.append('start_date', this.csvStartDate);
        if(this.csvEndDate)   params.append('end_date',   this.csvEndDate);
        const d = await fetch(HUB+'/api/history?'+params).then(r=>r.json());
        const recs = d.records||[];
        if(!recs.length){ this.showToast('没有历史记录','error'); return; }
        const headers=['id','date','profile','emotion','text','elapsed_ms','rvc_applied','starred'];
        const rows=recs.map(r=>[
          r.id, r.created_at, r.profile, r.emotion,
          '"'+(r.text||'').replace(/"/g,'""')+'"',
          r.elapsed_ms, r.rvc_applied?1:0, r.starred?1:0
        ]);
        const csv=[headers.join(','),...rows.map(r=>r.join(','))].join('\n');
        const blob=new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8;'});
        const url=URL.createObjectURL(blob);
        const a=document.createElement('a'); a.href=url;
        a.download='history_'+new Date().toISOString().slice(0,10)+'.csv'; a.click();
        setTimeout(()=>URL.revokeObjectURL(url),5000);
        const dateHint=(this.csvStartDate||this.csvEndDate)?' ('+( this.csvStartDate||'\u8d77')+'~'+(this.csvEndDate||'\u4eca')+')'  :'';
        this.showToast('已导出 '+recs.length+' 条历史'+dateHint,'success');
      }catch(e){ this.showToast('CSV 导出失败: '+e,'error'); }
    },
    async exportHistory(idsOrUndefined) {
      const ids = Array.isArray(idsOrUndefined) ? idsOrUndefined : this.historyFiltered.map(i=>i.id);
      if(!ids.length){ this.showToast('没有可导出的记录','error'); return; }
      try{
        const resp = await fetch(HUB+'/api/history/export',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ids})
        });
        if(!resp.ok) throw new Error('HTTP '+resp.status);
        const blob = await resp.blob();
        const url  = URL.createObjectURL(blob);
        const a    = document.createElement('a'); a.href=url;
        a.download='history_export.zip'; a.click();
        setTimeout(()=>URL.revokeObjectURL(url), 5000);
        this.showToast('已导出 '+ids.length+' 条记录', 'success');
      } catch(e){ this.showToast('导出失败: '+e,'error'); }
    },

    async doSpeakStream() {
      if (!this.speakText.trim()||!this.active) {
        this.speakMsg=!this.active?'⚠️ 请先激活角色':'⚠️ 请输入文字'; this.speakOk=false; return;
      }
      this.streamLoading=true; this.streamInfo='连接中...'; this.streamPct=0; this.speakMsg='';
      this.speakHistId=0; this.speakStarred=false; this.speakResultMode='stream';  // UI-P0
      this.speakStreamFull=false; this._streamFullUrl=''; this._lastSpeakItem=null; // UI-P1
      // P21-4: 智能分句用于高亮显示
      this.speakSentences = this._smartSplitClient(this.speakText);
      this.speakCurrentIdx = -1;
      this.$nextTick(()=>this._startWaveAnim());  // P20-1: 启动波形动画
      const MAX_PENDING=8;
      const queueCtrl={buf:[],playIdx:0,stopped:false,eof:false};
      this.streamQueueCtrl=queueCtrl;
      this.streamAbortCtrl=new AbortController();
      const txt0=this.speakText;   // UI-P1: 锁定本次文本（用户中途改输入不影响结果卡摘要）
      let n=0, t0=Date.now(), streamTotal=0, lastAudioIdx=-1,
          streamRoute='clone', streamEmo='';  // UI-P2-1/P3-1: 后端回报实际路由与整文检出情感
      // P21-1: 指数退避重试配置
      const MAX_RETRY=5; let retryCount=0; let aborted=false;
      const doFetch=async()=>{
        const streamPayload={text:this.speakText, profile:this.active, language:this.speakLang,
                             emotion:this.speakEmotion, _resume_idx:lastAudioIdx};  // 后端可选支持断点
        if(this.speakInstruct.trim()) streamPayload.instruct=this.speakInstruct.trim();
        const r=await fetch(HUB+'/tts/stream_sse',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(streamPayload), signal:this.streamAbortCtrl.signal});
        const reader=r.body.getReader(); const dec=new TextDecoder(); let buf='';
        while(true){
          let value,done; try{ ({value,done}=await reader.read()); }catch(err){ throw err; }
          if(done)break;
          buf+=dec.decode(value,{stream:true});
          const lines=buf.split('\n'); buf=lines.pop()||'';
          for(const line of lines){
            if(!line.startsWith('data: '))continue;
            try{
              const d=JSON.parse(line.slice(6));
              if(d.done)break;
              if(d.saved && d.history_id){   // UI-P1-2: 整段已落历史 → 取回整段音频点亮下载/收藏
                this.speakHistId=d.history_id;
                this._loadStreamFull(d.history_id, {text:txt0, ms:Date.now()-t0, route:streamRoute,
                  // UI-P3-1: auto 用后端整文检出值（服务端口径为准），手动用所选情感
                  emotion:(streamRoute==='emotion'?(streamEmo||this.speakEmotion):'')}, queueCtrl);
              }
              if(d.total_sentences){
                streamTotal=d.total_sentences;
                if(d.tts_route) streamRoute=d.tts_route;
                if(d.detected_emotion){   // UI-P3-1: 检出情感回填 banner（与本地防抖检测竞态时以后端为准）
                  streamEmo=d.detected_emotion;
                  if(this.speakEmotion==='auto') this.speakDetectedEmotion=d.detected_emotion;
                }
              }
              if(d.audio_base64){
                lastAudioIdx = d.sent_idx ?? lastAudioIdx+1;
                if(lastAudioIdx < n) continue;  // 已处理过的句子跳过
                n++;
                const pct = streamTotal>0 ? Math.round(n/streamTotal*90) : Math.min(90,n*30);
                this.streamInfo = streamTotal>0 ? `第${n}/${streamTotal}句 ${d.elapsed_ms}ms` : `第${n}句 ${d.elapsed_ms}ms`;
                this.streamPct = pct;
                while(queueCtrl.buf.length - queueCtrl.playIdx > MAX_PENDING)
                  await new Promise(r=>setTimeout(r,200));
                queueCtrl.buf.push(d.audio_base64);
                if(n===1){this.speakMsg=`⚡ 首句 ${Date.now()-t0}ms`;this.speakOk=true;this._playQueueCtrl(queueCtrl);}
              }
            }catch(e){}
          }
        }
      };
      try {
        while(retryCount<=MAX_RETRY){
          try{ await doFetch(); break; } // 成功则退出重试循环
          catch(err){
            if(this.streamAbortCtrl?.signal.aborted){ aborted=true; break; }
            if(retryCount>=MAX_RETRY){ throw err; }
            retryCount++;
            const delayMs = Math.min(500*(2**(retryCount-1)), 8000);  // 0.5,1,2,4,8s
            this.streamInfo = `⚠️ 连接中断，${delayMs/1000}s后重试(${retryCount}/${MAX_RETRY})...`;
            await new Promise(r=>setTimeout(r, delayMs));
          }
        }
        // UI-P2-1: 0 句产出=全失败（如情感引擎离线），报真错而不是"✅ 0句"
        if(!aborted && n===0){
          this.speakMsg='❌ 未产出任何音频'+(streamRoute==='emotion'?'：情感引擎可能未启动，可切「😐 普通」或到「交付体检」启动 emotion_tts':'：语音服务可能未就绪');
          this.speakOk=false; this.streamInfo='';
        } else {
          if(!aborted) this.streamPct=100;
          // UI-P3-1: auto 检出的情感在完成消息里点名（与标准模式 banner 同等透明度）
          this.streamInfo=`✅ ${n}句 ${Date.now()-t0}ms`
            +(streamRoute==='emotion'?(' · 情感引擎'+(streamEmo?'（'+this.emotionCN(streamEmo)+'）':'')):'');
        }
      } catch(e){ if(!e?.message?.includes('abort')) this.speakMsg='❌ '+e; this.speakOk=false; }
      finally {
        queueCtrl.eof=true;   // UI-P1: 通知播放尾部——队列播干即可回填整段音频
        this.streamLoading=false; this.streamAbortCtrl=null; this.streamQueueCtrl=null;
        this._stopWaveAnim();
        // UI-P0: 错误常驻（配「↻ 重试」），仅进度/成功消息自清
        setTimeout(()=>{ this.streamInfo=''; if(this.speakOk) this.speakMsg=''; },5000);
      }
    },

    _playQueueCtrl(ctrl){
      // P13-2: 消费者推进 playIdx，与生产者的背压检测协同
      const play=()=>{
        if(ctrl.stopped) return;  // P14-1: 被 abort 后立即停止
        if(ctrl.playIdx>=ctrl.buf.length){
          if(ctrl.eof){ this._applyStreamFull(); return; }   // UI-P1: 播完收尾→整段音频回填播放器
          setTimeout(()=>{ if(ctrl.stopped) return; (ctrl.playIdx<ctrl.buf.length||ctrl.eof)&&play(); },300);
          return;
        }
        this.audioSrc='data:audio/wav;base64,'+ctrl.buf[ctrl.playIdx];
        this.speakCurrentIdx = ctrl.playIdx;  // P21-4: 高亮当前播放句
        if(ctrl.playIdx>0) ctrl.buf[ctrl.playIdx-1]=null;  // UI-P2-3: 已播句出队释放（长文流式不吃内存；重播走整段/历史）
        ctrl.playIdx++;
        this.$nextTick(()=>{const p=this.$refs.player;if(p){p.play();p.onended=play;}});
      };
      play();
    },
    // P21-4: 客户端轻量分句（与后端_smart_split_text对齐）
    _smartSplitClient(text){
      if(!text.trim()) return [];
      const paras = text.split(/\n+/).filter(p=>p.trim());
      const sents = [];
      for(const para of paras){
        const parts = para.match(/[^。！？!?…]+[。！？!?…]+|[^。！？!?…\n]+/g) || [para];
        for(const part of parts){
          if(part.includes('. ')){
            // 简单英文分句（不考虑缩写保护，轻量实现）
            const enParts = part.split(/(?<=\.)\s+(?=[A-Z\u4e00-\u9fa5])/);
            sents.push(...enParts.map(s=>s.trim()).filter(Boolean));
          } else { sents.push(part.trim()); }
        }
      }
      // 合并短句（<5字）
      const merged = [];
      for(const s of sents){
        if(merged.length && merged[merged.length-1].length < 5) merged[merged.length-1] += s;
        else merged.push(s);
      }
      return merged.length ? merged : [text.trim()];
    },
    async loadMetrics() {
      // P14-2: 获取运行指标并存入 metricsData
      this.metricsLoading=true;
      try{
        const d=await fetch(HUB+'/metrics').then(r=>r.json());
        this.metricsData=d;
      }catch(e){ this.showToast('指标加载失败: '+e,'error'); }
      finally{ this.metricsLoading=false; }
    },

    // ── StreamOut Phase E ──
    async soRefresh(){
      try{
        const [pl, st, rec] = await Promise.all([
          fetch(HUB+'/api/stream_out/plugins').then(r=>r.json()),
          fetch(HUB+'/api/stream_out/status').then(r=>r.json()),
          fetch(HUB+'/api/stream_out/recordings?limit=8').then(r=>r.json()),
        ]);
        if(pl.ok) this.soPlugins = pl.plugins||[];
        if(st.ok){
          this.soStatus = st;
          const w = (st.plugins||[]).find(x=>x.plugin==='webrtc');
          this.soPreviewUrl = w?.preview_url || st.vcam_url+'/' || '';
        }
        if(rec.ok) this.soRecordings = rec.recordings||[];
      }catch(e){}
    },
    // 导出配置包：先开选项面板（勾人脸/变声模型，实时算预估体积），确认才真下载
    async exportPackage(name){
      if(!name) return;
      this.expName=name; this.expInfo=null; this.expShow=true; this.expLoading=true;
      this.expFace=false; this.expRvc=false; this.expPw='';
      try{
        const d=await fetch(HUB+'/api/profile/'+encodeURIComponent(name)+'/package_info').then(r=>r.json());
        if(!d.ok) throw new Error(d.detail||'加载失败');
        this.expInfo=d;
        // 默认勾选策略：人脸默认带上（迁移后可直接出镜）；变声模型 55MB+ 默认不带
        this.expFace=!!d.has_face;
      }catch(e){ this.showToast('读取配置包信息失败: '+((e&&e.message)||e),'error'); this.expShow=false; }
      finally{ this.expLoading=false; }
    },
    // 预估总体积 = 基础(参考音+预切段+KB) + 勾选项
    get expEstBytes(){
      const e=(this.expInfo&&this.expInfo.est_bytes)||{base:0,face:0,rvc:0};
      return e.base + (this.expFace?e.face:0) + (this.expRvc?e.rvc:0);
    },
    expConfirm(){
      const q=[];
      if(this.expFace) q.push('include_face=1');
      if(this.expRvc) q.push('include_rvc_model=1');
      const pw=(this.expPw||'').trim();
      if(pw) q.push('password='+encodeURIComponent(pw));
      window.location.href = HUB+'/api/profile/'+encodeURIComponent(this.expName)+'/export_package'+(q.length?('?'+q.join('&')):'');
      this.expShow=false;
      this.showToast(pw?'正在下载加密配置包…':'正在下载配置包…','info');
    },
    // 创建中心——统一入口；两步流：方式选择 → 表单在弹窗内完成（photo/video/pkg）；voice/json 仍路由到既有独立流程
    openCreateHub() { this.createHubShow = true; this.createHubMode = ''; },
    pickCreate(m) {
      if (m === 'photo' || m === 'video' || m === 'pkg') {
        this.createHubShow = true; this.createHubMode = m;
        if (m === 'photo') this.initPhotoForm();
        return;
      }
      this.createHubShow = false; this.createHubMode = '';
      if (m === 'voice')     { this.goTab('clone'); }                   // 声音克隆是独立 Tab（三步向导）R1-2: 走 goTab
      else if (m === 'json') { this.importShow = true; this.importFile=null; this.importPreview=null; this.importMode='skip'; }
    },

    // 选包后先轻预览（不落库不跑闸门）：清单/体积/加密态/覆盖预警，确认才真导入
    async _pkgSelect(file){
      if(!file) return;
      this.pkgFile=file; this.pkgPeek=null; this.pkgPw=''; this.pkgImportMsg=''; this.pkgImportOk=false;
      await this.pkgDoPeek();
    },
    async pkgDoPeek(){
      if(!this.pkgFile) return;
      this.pkgPeekLoading=true;
      try{
        const fd=new FormData();
        fd.append('file', this.pkgFile);
        if(this.pkgPw.trim()) fd.append('password', this.pkgPw.trim());
        const r=await fetch(HUB+'/api/profile/package_peek',{method:'POST', body:fd});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'读取失败');
        this.pkgPeek=d;
      }catch(e){
        this.pkgPeek=null;
        this.pkgImportMsg=(e&&e.message)||'配置包读取失败';
        this.showToast('配置包读取失败: '+((e&&e.message)||e),'error');
      }
      finally{ this.pkgPeekLoading=false; }
    },
    pkgReset(){ this.pkgFile=null; this.pkgPeek=null; this.pkgPw=''; this.pkgImportMsg=''; this.pkgImportOk=false; },
    // 确认导入（peek 通过后才可见）；加密包带口令
    async pkgConfirm(){
      if(!this.pkgFile) return;
      this.pkgBusy=true; this.pkgImportMsg='导入中…'; this.pkgImportOk=false;
      try{
        const fd = new FormData();
        fd.append('file', this.pkgFile);
        fd.append('mode', 'overwrite');
        fd.append('force', this.pkgImportForce ? 'true' : 'false');
        fd.append('dry_run', 'false');
        fd.append('import_kb', 'true');
        if(this.pkgPw.trim()) fd.append('password', this.pkgPw.trim());
        const d = await fetch(HUB+'/api/profile/import_package', {method:'POST', body:fd}).then(r=>r.json());
        if(d.ok){
          this.pkgImportOk=true;
          this.pkgImportMsg=`✓ ${d.profile||''} 已导入 · KB +${d.kb_added||0}`;
          this.showToast(this.pkgImportMsg,'success');
          await this.loadProfiles();
          this.createHubShow=false; this.createHubMode=''; this.pkgReset();
        } else {
          this.pkgImportMsg=d.detail||d.reason||'闸门未通过';
          this.showToast(this.pkgImportMsg+(d.can_force?' · 可勾选强制导入':''),'error');
        }
      }catch(e){
        this.pkgImportMsg='导入失败';
        this.showToast('配置包导入失败','error');
      }
      finally{ this.pkgBusy=false; }
    },
    async importPackage(ev){
      const file = ev.target?.files?.[0];
      await this._pkgSelect(file);
      if(ev.target) ev.target.value='';
    },
    // P1: 拖拽配置包到落区（.zip 明文 / .ahpkg 口令加密——此前 .ahpkg 根本进不来）
    pkgDrop(ev){
      this.pkgDrag=false;
      const f = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if(!f) return;
      if(!/\.(zip|ahpkg)$/i.test(f.name)){ this.pkgImportMsg='请拖入 .zip / .ahpkg 配置包'; this.pkgImportOk=false; return; }
      this.createHubShow=true; this.createHubMode='pkg';
      this._pkgSelect(f);
    },

    async soQuickStart(){
      this.soLoading=true;
      try{
        const d=await fetch(HUB+'/api/stream_out/quick_start',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({profile:this.active||'', record:this.soRecord})
        }).then(r=>r.json());
        if(d.ok) this.showToast('一键开播已启动','success');
        else this.showToast(d.detail||(d.results&&d.results[0]?.detail)||'开播失败','error');
        await this.soRefresh();
      }catch(e){ this.showToast('开播: '+e,'error'); }
      finally{ this.soLoading=false; }
    },
    async soStart(){
      this.soLoading=true;
      try{
        const d=await fetch(HUB+'/api/stream_out/start',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({plugins:this.soSelected, profile:this.active||'',
            rtmp_url:this.soRtmpUrl, record:this.soRecord})
        }).then(r=>r.json());
        if(d.ok) this.showToast('StreamOut 已启动','success');
        else this.showToast(d.detail||'启动失败','error');
        await this.soRefresh();
      }catch(e){ this.showToast('StreamOut: '+e,'error'); }
      finally{ this.soLoading=false; }
    },
    async soStop(){
      this.soLoading=true;
      try{
        await fetch(HUB+'/api/stream_out/stop',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({plugins:null})
        });
        this.showToast('StreamOut 已停止','info');
        await this.soRefresh();
      }catch(e){}
      finally{ this.soLoading=false; }
    },

    // ── 开播模式（真人换脸 / 数字人）：两条链路 OBS 已拆分，互斥切换 ──
    async loadBroadcastMode(){
      try{ const d=await fetch(HUB+'/api/broadcast_mode').then(r=>r.json());
        if(d&&d.mode){ if(!this.broadcastMode||d.explicit) this.broadcastMode=d.mode;
          this.broadcastModeExplicit=!!d.explicit; this.dualAvailable=!!d.dual_available; } }catch(_){}
    },
    async setBroadcastMode(m){
      if(m!=='real_faceswap' && m!=='avatar_lipsync') return;
      this.broadcastMode=m; this.broadcastModeExplicit=true;
      if(m!=='real_faceswap') this.broadcastDual=false;   // 只有真人换脸侧才有「双路同开」
      try{ localStorage.setItem('hub_broadcast_mode', m); }catch(_){}
      try{ await fetch(HUB+'/api/broadcast_mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})}); }catch(_){}
      if(m==='avatar_lipsync') this.loadVcamPrev();   // 切到数字人：实时画面卡立刻反映 vcam 现状（不等 3s 轮询）
      this.showToast(m==='real_faceswap'?'已选择：真人出镜 · 实时换脸':'已选择：AI 数字人 · 无人直播','info');
    },
    startCtaLabel(){
      if(this.broadcastMode==='avatar_lipsync') return '🚀 开始数字人直播';
      if(this.broadcastMode==='real_faceswap') return this.phoneRelayCam.live?'🚀 用手机摄像头开播(换脸)':'🚀 开始真人换脸';
      return this.phoneRelayCam.live?'🚀 用手机摄像头开播':'🚀 一键开播';
    },

    // ── 一键开播 ──
    // fromWireless=true 时为「手机无线开播」内部调用：不弹 toast、不被互斥拦截（避免与自身冲突）
    async oneClickStart(fromWireless=false) {
      if(!fromWireless && this.wireless.running){ this.showToast('「手机无线开播」准备中，请先等待或点取消','info'); return; }
      const _restart = this.perf.streaming;   // 已在直播→本次是「重新开播」
      // [Phase E 附加·设计说明] 设备红灯卡点收口在 guardedStart()（预检弹窗统一呈现,含体检 bad 项),
      // 此处不再二次探测：UI 全部入口走 guardedStart,双探测=双延迟+双弹窗。
      this.streaming=true; this.streamSteps=[];
      this.startingSince=Date.now(); this.startingRestart=_restart;   // [S5 启动仪式] 驱动里程碑面板耗时/文案
      if(!fromWireless) this.showToast(_restart?'正在重启直播画面…':'正在开播…','info');
      try {
        const payload={profile:this.active, restart:_restart};   // 已在直播→真重启管线(后端先停旧再起)，不再空转
        if(this.broadcastMode) payload.mode=this.broadcastMode;   // 开播模式(单一真相)：后端按此只起对应链路 + OBS 互斥
        if(this.broadcastDual) payload.dual=true;                 // 双路同开(需 Unity Capture)
        if(this.videoSource) payload.source=this.videoSource;
        if(this.videoSource==='camera' && this.selectedCamera>=0) payload.camera=this.selectedCamera;
        if(this.audioInput) payload.audio_in=this.audioInput;
        if(this.audioOutput) payload.audio_out=this.audioOutput;
        // 下发「效果调节」预设/参数：后端 one_click_start 已支持，此前前端漏传导致预设(低延迟/均衡/画质)不生效
        payload.video_width=this.videoWidth;   payload.video_height=this.videoHeight;
        payload.video_fps=this.videoFps;       payload.swap_fps=this.swapFps;
        payload.mjpeg_fps=this.mjpegFps;       payload.jpeg_quality=this.jpegQuality;
        payload.crossfade=this.crossfade;
        // 换脸效果：默认值即"保持当前观感"，仅当用户调整后才下发，避免影响既有直播效果。
        // 平滑为时域 alpha 语义(1=不混帧=最清晰)，UI 用"越大越稳"，故下发 1-faceSmooth。
        if(this.faceBlend>0 && this.faceBlend<1)          payload.face_blend=this.faceBlend;
        if(this.faceThreshold>0)                          payload.face_threshold=this.faceThreshold;
        if(this.faceSmooth>0)                             payload.face_smooth=+(1-this.faceSmooth).toFixed(3);
        // 增强：''=跟随引擎档(推荐,不下发)；none=强制关；gfpgan/codeformer=强制指定(压过按档策略)
        if(this.faceEnhance)                              payload.face_enhance=this.faceEnhance;
        if(this.enginePreset)                             payload.swap_preset=this.enginePreset;   // 引擎画质目标档
        if(this.outputAspect && this.outputAspect!=='auto') payload.aspect=this.outputAspect;      // 输出画面比例(竖屏/横屏)
        const d=await fetch(HUB+'/api/one_click_start',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
        this.streamSteps=d.steps||[];
        // 记录本次「实际用了哪个视频源」(后端 auto_reason 单一真相)→ 连接状态条 + 降级显形。
        // 空 kind(如"已在运行"空转)不覆盖，保留上次已知实况。
        const _vstep=(d.steps||[]).find(s=>s.step==='start_video');
        if(_vstep){ const _iv=this.interpretVideoSource(_vstep); if(_iv.kind){ _iv.wanted=this.videoSource; this.lastVideo=_iv; this.videoNoticeDismissed=false; }
                    if(_vstep.lic_note) this.showToast('🔒 '+_vstep.lic_note,'info'); }   // P5 服务端按授权降档时如实告知
        if(d.audio){   // 自动适配:后端选好的 手机麦→CABLE 直接套用,免手动选
          if(d.audio.input){ this.audioInput=d.audio.input; this.rvc.inputDevice=d.audio.input; this._prevAudioIn=d.audio.input; }
          if(d.audio.output){ this.audioOutput=d.audio.output; this.rvc.outputDevice=d.audio.output; this._prevAudioOut=d.audio.output; }
        }
        if(d.ok){ this.streamNonce=Date.now(); this.mjpegOn=true; this._swapPrev=null; this.swapWin=null; this.rawPeek=false;
          if(this.broadcastMode==='avatar_lipsync'){ this.vcamPrevNonce=Date.now(); this.loadVcamPrev(); }   // 数字人开播：重连 vcam 预览流并即时刷新状态徽标
          if(!_restart){ this._healCount={}; this._healCooldown={}; this._healEpisode={}; this.healLog=[]; this._rvcApiSeenUp=false; this._rvcApiDownSince=0; }   // 全新开播=新场次→重置自愈预算/回合 + 变声观测（场内重启则保留）
          await this.rvcRefreshDevices(); await this.loadPhoneRelayCam(); }
        if(!fromWireless){
          if(d.ok) this.showToast(_restart?'✅ 已重启直播画面':'✅ 已开播','success');
          else if(d.need==='camera') this.showToast('未检测到摄像头画面源：请先连手机/摄像头，或用下方「📱 手机无线开播」','error');
          else this.showToast('部分步骤未成功，见下方「开播步骤」','error');
        }
      } catch(e){ this.streamSteps=[{step:'error',ok:false,detail:String(e)}]; if(!fromWireless) this.showToast('开播失败：'+String(e),'error'); }
      finally { this.streaming=false; this.startingSince=0; }
    },

    async oneClickStop() {
      this.clearDangerArm();
      const d=await fetch(HUB+'/api/one_click_stop',{method:'POST'}).then(r=>r.json());
      this.streamSteps=[{step:'stop',ok:true,detail:d.detail}];
      this.mjpegOn=false; this.streamNonce=Date.now(); this._swapPrev=null; this.swapWin=null; this.rawPeek=false;
      this._healCount={}; this._healCooldown={}; this.healLog=[]; this._healEpisode={}; this._rvcApiSeenUp=false; this._rvcApiDownSince=0;   // 停播→清空自愈预算与轨迹
      this.lastVideo={kind:'', label:'', isPhone:false, degraded:false, reason:'', url:'', wanted:''};  // 停播→连接状态条回到就绪预览
      this.videoNoticeDismissed=false;
    },

    // P0-1 危险操作二次确认：第一次点击 arm，3s 内再点执行；不弹窗
    clearDangerArm(){
      this.dangerArm='';
      if(this._dangerArmTimer){ clearTimeout(this._dangerArmTimer); this._dangerArmTimer=null; }
    },
    armDanger(action){
      if(this.dangerArm===action){ this.clearDangerArm(); return true; }
      this.dangerArm=action;
      if(this._dangerArmTimer) clearTimeout(this._dangerArmTimer);
      this._dangerArmTimer=setTimeout(()=>{ this.dangerArm=''; this._dangerArmTimer=null; }, 3000);
      return false;
    },
    async clickStop(){
      if(!this.armDanger('stop')) return;
      await this.oneClickStop();
    },
    async clickRestart(){
      if(!this.armDanger('restart')) return;
      await this.oneClickStart();
    },

    // 一键无线开播:等手机端(终端/监听/摄像头)就绪→用 auto 源(选中手机摄像头)走现有 oneClickStart
    async wirelessStart(){
      if(this.wireless.running) return;
      if(this.streaming){ this.showToast('开播进行中，请稍候再试','info'); return; }
      if(!this.active){ this.wireless={running:false,phase:'need_profile',msg:'请先在上方选择出镜角色',checklist:{term:false,audio:false,cam:false}}; return; }
      this.wireless={running:true,phase:'check',msg:'检查手机终端…',checklist:{term:false,audio:false,cam:false}};
      const deadline=Date.now()+45000; let ready=false;
      while(this.wireless.running && Date.now()<deadline){
        await this.loadPhoneRelayStatus();
        const pr=this.phoneRelay||{};
        const term=!!pr.ok, audio=!!(pr.audio&&pr.audio.ok), cam=!!(pr.cam&&pr.cam.ok);
        this.wireless.checklist={term,audio,cam};
        if(!pr.reachable){ this.wireless.phase='relay'; this.wireless.msg='手机中继未在线，正在等待守护拉起…'; }
        else if(!term){ this.wireless.phase='open'; this.wireless.msg='请用手机扫码打开「手机端」并点【🚀 一键准备】'; }
        else if(!audio||!cam){ this.wireless.phase='prep'; this.wireless.msg='手机端准备中：'+(!audio?'开监听 ':'')+(!cam?'开摄像头 ':'')+'（手机端「一键准备」会自动完成）'; }
        else { ready=true; break; }
        await new Promise(r=>setTimeout(r,1500));
      }
      if(!this.wireless.running) return;            // 被取消
      if(!ready){ this.wireless={running:false,phase:'timeout',msg:'手机端未在 45 秒内就绪。请确认手机已打开手机端并点【一键准备】后重试。',checklist:this.wireless.checklist}; return; }
      this.wireless.phase='starting'; this.wireless.msg='手机端就绪，正在用手机摄像头开播…';
      this.videoSource='auto';
      await this.oneClickStart(true);
      const ok=(this.streamSteps||[]).length>0 && (this.streamSteps||[]).every(s=>s.ok);
      this.wireless={running:false,phase:ok?'done':'error',msg:ok?'✅ 已用手机摄像头无线开播':'开播过程有步骤失败，见下方「开播步骤」',checklist:this.wireless.checklist};
    },
    wirelessCancel(){ this.wireless.running=false; this.wireless.phase=''; this.wireless.msg='已取消'; },

    // ── 摄像头 ──
    async loadCameras() {
      try {
        const d=await fetch(HUB+'/cameras').then(r=>r.json());
        this.cameras=d.cameras||[];
      } catch(e){}
    },
    async loadPhoneRelayCam(){
      try{
        const d=await fetch(HUB+'/api/monitor/cam').then(r=>r.json());
        const st=d.status||{};
        this.phoneRelayCam={live:!!d.live, url:d.url||'', fps:st.fps||0,
          w:st.w||0, h:st.h||0, source:st.source||''};
      }catch(e){ this.phoneRelayCam={live:false,url:'',fps:0}; }
    },
    async loadPhoneRelayStatus(){
      try{
        const d=await fetch(HUB+'/api/monitor/status').then(r=>r.json());
        this.phoneRelay=d||{ok:false,audio:{},mic:{},cam:{}};
        const c=(d&&d.cam)||{};
        this.phoneRelayCam={live:!!c.ok, url:c.url||'', fps:c.fps||0,
          w:c.w||0, h:c.h||0, source:c.source||''};
      }catch(e){
        this.phoneRelay={ok:false,audio:{},mic:{},cam:{}};
      }
    },
    openPhoneRelay(kind='https'){
      const host=location.hostname;
      const url = kind==='show'
        ? (this.phoneRelay.show_url || ('http://'+host+':7878/show'))
        : kind==='http'
          ? (this.phoneRelay.url || ('http://'+host+':7878/'))
          : (this.phoneRelay.https_url || ('https://'+host+':7879/'));
      window.open(url, '_blank');
    },
    openInterpreter(){
      window.open(this.interpUrl, '_blank');
    },
    // 手机开播向导：二维码指向 HTTPS 终端(7879)——浏览器要求 HTTPS 才放行摄像头/麦克风(此前的踩坑点)
    get phoneRelayQrTarget(){
      return this.phoneRelay.https_url || ('https://'+location.hostname+':7879/');
    },
    get phoneRelayQrSrc(){
      return '/api/qr?data='+encodeURIComponent(this.phoneRelayQrTarget);
    },
    // 开播页模式：用户(streamSimple=true，极简) / 专家(false，全展开)。收敛 showPerfDetail 进专家模式。
    setStreamMode(expert){
      this.streamSimple=!expert;
      try{ localStorage.setItem('hub_stream_simple', this.streamSimple?'1':'0'); }catch(_){}
    },
    // [v2 面板化] 设置面板 折叠/展开 + 折叠头一句话摘要（面板收起也能看到当前档位/设备状态）
    togglePanel(k){
      this.panelOpen[k]=!this.panelOpen[k];
      try{ localStorage.setItem('hub_stream_panels', JSON.stringify(this.panelOpen)); }catch(_){}
    },
    openPanel(k){ if(!this.panelOpen[k]) this.togglePanel(k); },
    dismissWebhookHint(){ this.webhookHintDismissed=true; try{ localStorage.setItem('hub_webhook_hint_dismiss','1'); }catch(_){} },
    sensPresetActive(){
      const t=this.perfThreshold;
      for(const [k,v] of Object.entries(this._sensPresets)){
        if(t.lat_high===v.lat_high && t.lat_mid===v.lat_mid && t.fail===v.fail) return k;
      }
      return '';
    },
    applySensPreset(name){
      const p=this._sensPresets[name]; if(!p) return;
      this.perfThreshold.lat_high=p.lat_high; this.perfThreshold.lat_mid=p.lat_mid; this.perfThreshold.fail=p.fail;
      this.monitorCustomOpen=(name==='');
    },
    monitorSummary(){
      const lat=this.perf.swap_latency_last||0;
      const parts=[];
      if(lat>0) parts.push(lat+'ms');
      parts.push(this.streamHealth().label||'未开播');
      if(this.autoHealServer) parts.push('智能守护开');
      else parts.push('智能守护关');
      if(this.healAlertsOn) parts.push('告警开');
      return parts.join(' · ');
    },
    qcDetailLine(){
      const q=this.qc; if(!q||!q.ok) return '';
      let s='脸区 '+q.face_w+'×'+q.face_h+'px · 亮度 '+q.brightness+'/255';
      if(q.retention!=null) s+=' · 清晰度保留率 '+q.retention+'%';
      if(q.mouth_retention!=null) s+=' · 嘴部 '+q.mouth_retention+'%';
      s+=' · 生效档 '+this.tierLabel(q.effective);
      if(q.enhance&&q.enhance!=='none') s+=' · 精修';
      if(q.crop_active) s+=' · 脸区原生✓';
      return s;
    },
    _pushLatHist(v){
      if(!this._latHist) this._latHist=[];
      const n=Math.round(v);
      if(this._latHist[this._latHist.length-1]===n) return;
      this._latHist.push(n);
      if(this._latHist.length>60) this._latHist.shift();
    },
    latSparkPoints(){
      const h=this._latHist||[]; if(h.length<2) return '';
      const max=Math.max(...h,1), min=Math.min(...h,0), range=max-min||1, w=40, ht=12;
      return h.map((v,i)=>{
        const x=(i/(h.length-1))*w, y=ht-((v-min)/range)*(ht-2)-1;
        return (i===0?'M':'L')+x.toFixed(1)+','+y.toFixed(1);
      }).join(' ');
    },
    heroSecondaryTips(){
      const tips=[];
      if(this.tab==='stream' && !this.perf.streaming && this.orphanStream)
        tips.push({key:'orphan', pri:1});
      if(this.tab==='stream' && this.guideVisible())
        tips.push({key:'guide', pri:2});
      if(this.showVideoFallbackNotice)
        tips.push({key:'fallback', pri:3});
      if(this.streamReady() && !this.phoneRelayCam.live && this.phoneRelay.ok && this.broadcastMode==='real_faceswap' && this.streamPhase()!=='starting')
        tips.push({key:'phone', pri:4});
      if(this.tierChip() && this.tierChip().degraded)
        tips.push({key:'tier', pri:5});
      tips.sort((a,b)=>a.pri-b.pri);
      return tips;
    },
    heroTipVisible(key){
      const tips=this.heroSecondaryTips();
      if(tips.length<=2) return tips.some(t=>t.key===key);
      if(this.heroTipsOpen) return tips.some(t=>t.key===key);
      const vis=new Set(tips.slice(0,2).map(t=>t.key));
      return vis.has(key);
    },
    heroTipsOverflow(){ return Math.max(0, this.heroSecondaryTips().length-2); },
    async quickBgMode(mode){
      this.bgCfg.mode=mode;
      if(mode==='image'){
        this.setStreamMode(true);
        this.openPanel('visual');
        this.showToast('请在「视觉效果」面板选择背景图片','info');
        return;
      }
      if(this.perf.streaming) await this.applyBg();
      else this.showToast('背景已设为「'+({none:'关',blur:'虚化',green:'绿幕'}[mode]||mode)+'」，开播后自动应用','info');
    },
    _loadImg(url){ return window.BD_CANVAS.loadImg(url); },   // P10-3 单源：canvas_brand.js
    _canvasDownload(c, filename){
      return new Promise((resolve,reject)=>{
        c.toBlob(b=>{
          if(!b){ reject(new Error('blob')); return; }
          const a=document.createElement('a');
          a.href=URL.createObjectURL(b); a.download=filename;
          a.click(); URL.revokeObjectURL(a.href);
          resolve();
        }, 'image/png');
      });
    },
    // P2-3 状态条 tone 单源（Pass B）
    toneClass(tone, part){
      const t=tone||'orange';
      if(part==='strip') return t==='green'?'bd-strip ok':t==='red'?'bd-strip err':t==='blue'?'bd-strip info':'bd-strip warn';
      if(part==='text') return t==='green'?'text-hub-green strip-em':t==='red'?'text-hub-red strip-em':t==='blue'?'text-hub-blue strip-em':'text-hub-orange strip-em';
      return '';
    },
    devCheckStripClass(){
      const g=this.devCheck&&this.devCheck.grade;
      return (g==='green'?'bd-strip ok':g==='yellow'?'bd-strip warn':'bd-strip err')+' px-3 py-2 text-[13px] mb-2';
    },
    devCheckTitleClass(){
      const g=this.devCheck&&this.devCheck.grade;
      return g==='green'?'text-hub-green strip-em font-bold text-sm':g==='yellow'?'text-hub-orange strip-em font-bold text-sm':'text-hub-red strip-em font-bold text-sm';
    },
    qcStripClass(){
      return ((this.qc&&this.qc.level==='good')?'bd-strip ok':'bd-strip warn')+' px-3 py-2 text-[13px] flex items-start gap-2 mb-2';
    },
    micTestStripClass(){
      const r=this.micTest.res; if(!r) return '';
      if(!r.ok) return 'bd-strip err px-2.5 py-1.5 flex items-start gap-1.5';
      return (r.level==='good'?'bd-strip ok':'bd-strip warn')+' px-2.5 py-1.5 flex items-start gap-1.5';
    },
    outTestStripClass(){
      return this.outTestGood()?'bd-strip ok px-2.5 py-1.5 flex items-start gap-1.5':'bd-strip warn px-2.5 py-1.5 flex items-start gap-1.5';
    },
    toggleLargeText(){
      this.largeText=!this.largeText;
      try{ localStorage.setItem('hub_large_text', this.largeText?'1':'0'); }catch(_){}
      try{ document.documentElement.classList.toggle('bd-large-text', this.largeText); }catch(_){}
      this.showToast(this.largeText?'已开启大字模式（字号+2px）':'已关闭大字模式','info');
    },
    // P3 成绩单字段单源：文本复制(copyRecap)与分享卡(recapCardStats)取值同一出口，避免口径漂移
    swapCropVal(){ const sw=this.lastSession?.swap; return (sw&&sw.crop&&sw.crop.hit_pct!=null)?sw.crop.hit_pct+'%':'—'; },
    swapLatVal(){ const sw=this.lastSession?.swap; return (sw&&sw.latency_ms&&sw.latency_ms.med!=null)?sw.latency_ms.med+'ms':'—'; },
    swapDegVal(){ const sw=this.lastSession?.swap; return ((sw&&sw.degraded_pct)||0)+'%'; },
    recapCardStats(){
      const s=this.lastSession; if(!s) return [];
      const rows=[
        ['直播时长', this.fmtDur(s.durSec)],
        ['峰值画质', s.peakFps>0?s.peakFps+' fps':'—'],
        ['画面稳定度', s.stabilityPct!=null?s.stabilityPct+'%':'—'],
        ['变声', s.usedRvc?'已用':'未用'],
      ];
      if(s.swap){
        rows.push(['裁剪命中', this.swapCropVal()]);
        rows.push(['换脸时延', this.swapLatVal()]);
        rows.push(['自动降档', this.swapDegVal()]);
        rows.push(['精修引擎', this.swapRecapEnh()]);
      }
      return rows;
    },
    // P4 近场趋势数据：swapHist(最新在前) → [{ms},...] 旧→新，≥2 场才有趋势可言
    recapTrend(){
      const hist=this.lastSession?.swapHist;
      if(!hist||hist.length<2) return [];
      return hist.filter(r=>r&&r.latency_ms&&r.latency_ms.med!=null)
                 .map(r=>({ms:r.latency_ms.med})).reverse();
    },
    // P5 稳定度环形账本：前端采样值后端没有，落 localStorage(最多 8 条)；null(采样不足)不入账
    _pushSessHist(stab){
      if(stab==null) return;
      try{
        const k='hub_sess_hist';
        const a=JSON.parse(localStorage.getItem(k)||'[]');
        a.push({ts:Date.now(), stab:stab});
        while(a.length>8) a.shift();
        localStorage.setItem(k, JSON.stringify(a));
      }catch(_){}
    },
    sessHistStab(){
      try{
        return (JSON.parse(localStorage.getItem('hub_sess_hist')||'[]'))
          .filter(r=>r&&r.stab!=null).slice(-6);
      }catch(_){ return []; }
    },
    // P10-3 画布品牌基元收敛到 canvas_brand.js（与 phone 分享海报同一份真相），这里只留薄委托
    _roundRect(ctx,x,y,w,h,r){ window.BD_CANVAS.roundRect(ctx,x,y,w,h,r); },
    _accRgb(){ return window.BD_CANVAS.accRgb(); },
    // 有角色→海报带(132=头像底36+72再留24，数据格上缘 yy-24 恰贴头像底不叠压)；无角色→旧版标题区
    _recapHeadH(profile){ return profile ? 132 : 68; },
    // P9 分享链路可达性：控制台常开在 127.0.0.1，直接编码进 QR 手机扫了必打不开——
    //   借 phoneRelay 既有轮询里的 LAN 地址换出真实网段主机名，零新增请求；换不出再保持原样。
    _shareOrigin(){
      let host=location.hostname;
      if(/^(127\.|localhost$)/i.test(host)){
        try{
          const u=new URL((this.phoneRelay&&this.phoneRelay.https_url)||'');
          if(u.hostname) host=u.hostname;
        }catch(_){}
      }
      return location.protocol+'//'+host+(location.port?(':'+location.port):'');
    },
    async _drawRecapPosterBand(ctx, W, pad, profile, liveImg){
      const headH=this._recapHeadH(profile);
      const acc=this._accRgb();
      const grd=ctx.createLinearGradient(0,0,W,0);
      grd.addColorStop(0,'#141d33'); grd.addColorStop(1,'#0b0f1a');
      ctx.fillStyle=grd; ctx.fillRect(0,0,W,headH);
      const rg=ctx.createRadialGradient(W*0.28,headH*0.45,20,W*0.28,headH*0.45,220);
      rg.addColorStop(0,'rgba('+acc+',.35)'); rg.addColorStop(1,'transparent');
      ctx.fillStyle=rg; ctx.fillRect(0,0,W,headH);
      ctx.fillStyle='#e5e9f5'; ctx.font='bold 17px sans-serif';
      ctx.fillText('本场直播战报', pad, 28);
      if(!profile){
        ctx.font='13px sans-serif'; ctx.fillStyle='#aab4cc';
        ctx.fillText('无界 BOUNDLESS · 本地运行', pad, 50);
        return headH;
      }
      const BD=window.BD_CANVAS;
      const p=profile, qa=p.quality_axes||{}, cos=qa.cosine||0, pct=Math.round(cos*100), gold=BD.isGold(cos);
      const ax=pad, ay=36, asz=72;
      let face=null;   // P9 容错：坏缩略图只降级为色块，不炸掉整次导出
      if(p.thumbnail) face=await this._loadImg('data:image/jpeg;base64,'+p.thumbnail).catch(()=>null);
      ctx.save(); this._roundRect(ctx,ax,ay,asz,asz,14); ctx.clip();
      if(face) ctx.drawImage(face,ax,ay,asz,asz);
      else { ctx.fillStyle='#1e293b'; ctx.fillRect(ax,ay,asz,asz); }
      ctx.restore();
      ctx.strokeStyle='rgba('+acc+',.6)'; ctx.lineWidth=2;
      this._roundRect(ctx,ax,ay,asz,asz,14); ctx.stroke();
      ctx.textAlign='left'; ctx.fillStyle='#fff'; ctx.font='bold 16px sans-serif';
      // P11 超长角色名省略号截断：右侧还有直播缩略图，撞上会脏画面（按实际缩略图宽算安全区）
      const liveW=liveImg? liveImg.width*(Math.min(headH-12,liveImg.height)/liveImg.height)+12 : 0;
      ctx.fillText(BD.ellipsize(ctx, p.name||'', W-pad-liveW-(ax+asz+12)-8), ax+asz+12, ay+28);
      if(cos>0){   // P9 实拍修复：无质量数据不画 pill，杜绝「音色 0%」上分享图
        const btxt=(gold?'★ 金标 · ':'')+'音色 '+pct+'%';
        const bw=ctx.measureText(btxt).width+28, bx=ax+asz+12, by=ay+38;
        ctx.fillStyle=gold?BD.goldFill(ctx,bx,bx+bw):BD.OK_BG;   // P10-3 金标渐变/绿底单源
        this._roundRect(ctx,bx,by,bw,28,14); ctx.fill();
        ctx.fillStyle=gold?BD.GOLD_TEXT:BD.OK_TEXT; ctx.font='bold 12px sans-serif';
        ctx.fillText(btxt, bx+14, by+19);
      }
      if(liveImg){
        const th=Math.min(headH-12, liveImg.height), tw=liveImg.width*(th/liveImg.height);
        ctx.drawImage(liveImg, W-pad-tw, 6, tw, th);
        ctx.fillStyle='#8b96b0'; ctx.font='10px sans-serif'; ctx.textAlign='right';
        ctx.fillText('本场画面', W-pad, headH-4);
        ctx.textAlign='left';
      }
      return headH;
    },
    async _buildRecapCanvas(){
      const profile=this.activeProfileObj||null;
      // P9-4 同网 QR 深链：与 phone M2-B 海报同构（/s?profile= 落地页 + /api/qr 离线生成）。
      //   语义=同一局域网扫码直达角色体验页（销售演示递屏/主播自查），文案如实标注「同网」，
      //   跨网收图者不被误导。QR 与画面缩略图并行加载；任一失败→该块不画，海报不残缺。
      const shareUrl=(profile&&profile.name)   // from=recap 来源标记：落地页据此记 land_recap，扫码转化可量化(P10)
        ? this._shareOrigin()+'/s?profile='+encodeURIComponent(profile.name)+'&from=recap' : '';
      const [thumb, qr]=await Promise.all([
        this._loadImg('/realtime/swapped.jpg?t='+Date.now()).catch(()=>null),
        shareUrl ? this._loadImg('/api/qr?data='+encodeURIComponent(shareUrl)).catch(()=>null)
                 : Promise.resolve(null),
      ]);
      const W=640, pad=20, cols=2;
      const stats=this.recapCardStats();
      const trend=this.recapTrend();
      const stabs=this.sessHistStab();
      const trendH=(trend.length>=2||stabs.length>=2)?64:0;
      const gridRows=Math.ceil(stats.length/cols);
      const remarkH=this.recapRemark()?26:0, qrH=qr?84:0, footH=30;
      const headH=this._recapHeadH(profile);
      const H=headH+gridRows*38+trendH+remarkH+qrH+footH+pad;
      const c=document.createElement('canvas');
      c.width=W; c.height=H;
      const ctx=c.getContext('2d');
      await this._drawRecapPosterBand(ctx, W, pad, profile, thumb);
      ctx.fillStyle='#080b10'; ctx.fillRect(0,headH,W,H-headH);
      const cellW=(W-pad*2)/cols;
      stats.forEach((st,i)=>{
        const col=i%cols, row=Math.floor(i/cols);
        const x=pad+col*cellW, yy=headH+row*38;
        ctx.fillStyle='rgba(255,255,255,.06)'; ctx.fillRect(x, yy-24, cellW-10, 32);
        ctx.fillStyle='#8b96b0'; ctx.font='11px sans-serif'; ctx.fillText(st[0], x+8, yy-10);
        ctx.fillStyle='#e5e9f5'; ctx.font='bold 15px sans-serif'; ctx.fillText(String(st[1]), x+8, yy+8);
      });
      // P4/P5 近场趋势双轨（P9 实拍修复：柱顶数值上探会撞标题行，柱区整体下移 10px 留出净空）
      if(trendH){
        const ty=headH+gridRows*38+4;
        const bw=26, gap=10, bh=30, by=ty+26;
        if(trend.length>=2){
          ctx.fillStyle='#8b96b0'; ctx.font='11px sans-serif';
          ctx.fillText('近 '+trend.length+' 场换脸时延(ms) · 越低越稳', pad, ty+10);
          const maxMs=Math.max(...trend.map(t=>t.ms), 1);
          trend.forEach((t,i)=>{
            const h=Math.max(4, Math.round(bh*t.ms/maxMs));
            const x=pad+i*(bw+gap), last=(i===trend.length-1);
            ctx.fillStyle=last?'#4f7aff':'rgba(255,255,255,.18)';
            ctx.fillRect(x, by+bh-h, bw, h);
            ctx.fillStyle=last?'#e5e9f5':'#8b96b0'; ctx.font=(last?'bold ':'')+'10px sans-serif';
            ctx.fillText(String(t.ms), x, by+bh-h-3);
          });
        }
        if(stabs.length>=2){
          const sx=W/2+20;
          ctx.fillStyle='#8b96b0'; ctx.font='11px sans-serif';
          ctx.fillText('近 '+stabs.length+' 场稳定度% · 越高越稳', sx, ty+10);
          stabs.forEach((r,i)=>{
            const h=Math.max(4, Math.round(bh*r.stab/100));
            const x=sx+i*(bw+gap), last=(i===stabs.length-1);
            ctx.fillStyle=last?'#34d399':'rgba(255,255,255,.18)';
            ctx.fillRect(x, by+bh-h, bw, h);
            ctx.fillStyle=last?'#e5e9f5':'#8b96b0'; ctx.font=(last?'bold ':'')+'10px sans-serif';
            ctx.fillText(String(r.stab), x, by+bh-h-3);
          });
        }
      }
      const remark=this.recapRemark();
      if(remark){
        ctx.fillStyle='#fbbf24'; ctx.font='12px sans-serif';
        ctx.fillText('· '+remark, pad, headH+gridRows*38+trendH+6);
      }
      if(qr){
        const qy=headH+gridRows*38+trendH+remarkH+8, qs=60;
        ctx.fillStyle='rgba(255,255,255,.05)';
        this._roundRect(ctx, pad, qy, W-pad*2, 72, 10); ctx.fill();
        ctx.fillStyle='#fff';   // QR 必须白底，暗底扫不出
        this._roundRect(ctx, W-pad-qs-14, qy+6, qs+8, qs+8, 6); ctx.fill();
        ctx.drawImage(qr, W-pad-qs-10, qy+10, qs, qs);
        ctx.fillStyle='#e5e9f5'; ctx.font='bold 13px sans-serif';
        ctx.fillText('📱 同网扫码 · 和 TA 实时对话', pad+12, qy+30);
        ctx.fillStyle='#8b96b0'; ctx.font='11px monospace';
        // 展示行给人读→中文名不转码；QR 里才是编码后的真 URL
        ctx.fillText((this._shareOrigin().replace(/^https?:\/\//,'')+'/s?profile='+profile.name).slice(0,64), pad+12, qy+52);
      }
      ctx.fillStyle='#8b96b0'; ctx.font='11px sans-serif';
      ctx.fillText('无界 BOUNDLESS · 🔒 全程本地运行 · '+new Date().toLocaleString('zh-CN'), pad, H-10);
      return c;
    },
    async exportRecapCard(){
      if(this.exportBusy) return;
      const s=this.lastSession;
      if(!s){ this.showToast('暂无成绩单','info'); return; }
      this.exportBusy=true;
      try{
        const c=await this._buildRecapCanvas();
        await this._canvasDownload(c, 'boundless-recap-'+(s.endedTs||Date.now())+'.png');
        this.showToast('成绩单图片已下载','success');
      }catch(e){ this.showToast('导出失败：'+e.message,'error'); }
      finally{ this.exportBusy=false; }
    },
    // P3 一键分享：能走系统分享面板就走(微信/QQ/邮件…)，用户取消不报错；不支持的环境按钮本就不显示
    async shareRecapCard(){
      if(this.exportBusy) return;
      const s=this.lastSession;
      if(!s){ this.showToast('暂无成绩单','info'); return; }
      this.exportBusy=true;
      try{
        const c=await this._buildRecapCanvas();
        const blob=await new Promise(r=>c.toBlob(r,'image/png'));
        if(!blob) throw new Error('生成图片失败');
        const name='boundless-recap-'+(s.endedTs||Date.now())+'.png';
        try{
          await navigator.share({files:[new File([blob], name, {type:'image/png'})], title:'本场直播成绩单 · 无界 BOUNDLESS'});
          this.showToast('已发起分享','success');
        }catch(e){
          if(e && e.name==='AbortError'){ /* 用户取消，静默 */ }
          else{
            // 缩略图异步拉取可能耗尽浏览器的「用户手势」窗口(NotAllowedError)→ 退化为下载，分享动作不空手而归
            const a=document.createElement('a');
            a.href=URL.createObjectURL(blob); a.download=name;
            a.click(); URL.revokeObjectURL(a.href);
            this.showToast('系统分享未能调起，已改为下载图片','info');
          }
        }
      }catch(e){ this.showToast('分享失败：'+(e.message||e),'error'); }
      finally{ this.exportBusy=false; }
    },
    async exportPreviewCompare(){
      if(this.exportBusy) return;
      this.exportBusy=true;
      try{
        const t=Date.now();
        const [raw, swapped]=await Promise.all([
          this._loadImg('/realtime/raw.jpg?t='+t),
          this._loadImg('/realtime/swapped.jpg?t='+t),
        ]);
        const pad=8, labelH=28, footH=22, w=Math.max(raw.width, swapped.width, 320);
        const h=Math.max(raw.height, swapped.height, 180);
        const cw=w*2+pad*3, ch=h+labelH+footH+pad*2;
        const c=document.createElement('canvas'); c.width=cw; c.height=ch;
        const ctx=c.getContext('2d');
        ctx.fillStyle='#080b10'; ctx.fillRect(0,0,cw,ch);
        ctx.fillStyle='#e5e9f5'; ctx.font='bold 13px sans-serif';
        ctx.fillText('换脸前 · 原始', pad, 18);
        ctx.fillText('换脸后 · 直播', w+pad*2, 18);
        const drawFit=(img,x,y)=>{
          const sc=Math.min(w/img.width, h/img.height);
          const dw=img.width*sc, dh=img.height*sc;
          ctx.drawImage(img, x+(w-dw)/2, y+(h-dh)/2, dw, dh);
        };
        drawFit(raw, pad, labelH); drawFit(swapped, w+pad*2, labelH);
        ctx.fillStyle='#8b96b0'; ctx.font='11px sans-serif';
        const ts=new Date().toLocaleString('zh-CN');
        ctx.fillText('无界 BOUNDLESS · '+ts, pad, ch-footH+4);
        await this._canvasDownload(c, 'boundless-compare-'+t+'.png');
        this.showToast('对比图已下载','success');
      }catch(e){ this.showToast('导出失败：'+e.message,'error'); }
      finally{ this.exportBusy=false; }
    },
    _bindVisibility(){
      if(this._visBound || typeof document==='undefined') return;
      this._visBound=true;
      document.addEventListener('visibilitychange', ()=>{
        if(this.tab!=='stream') return;
        if(document.hidden){
          this.stopPreviewTick();
          this.startSignalTick();
        }else{
          if(this.mjpegOn) this.startPreviewTick();
          this.startSignalTick();
          this.signalTick();
        }
      });
    },
    fxSummary(){
      const res=this.videoWidth>=1920?'1080p':'720p';
      const q=this.jpegQuality>=92?'高':(this.jpegQuality>=85?'中':'低');
      const enh=this.faceEnhance===''?'':(this.faceEnhance==='none'?' · 精修关':' · '+({gfpgan:'GFPGAN',codeformer:'CodeFormer',gpen:'GPEN'}[this.faceEnhance]||this.faceEnhance));
      return res+' · '+this.videoFps+'fps · 画质'+q+enh;
    },
    visualSummary(){
      const parts=[];
      parts.push((this.bgCfg&&this.bgCfg.mode&&this.bgCfg.mode!=='none')?('背景:'+({blur:'虚化',image:'图片',green:'绿幕'}[this.bgCfg.mode]||this.bgCfg.mode)):'背景关');
      if(this.faceMap&&this.faceMap.enabled) parts.push('双人✓');
      if(this.hairPresetPreview) parts.push('定妆✓');
      return parts.join(' · ');
    },
    devSummary(){
      try{
        const c=this.audioChain();
        return '🎤 '+(c.mic||'默认')+' · '+(c.outLive?'直播声卡✓':(c.out||'默认输出'));
      }catch(_){ return ''; }
    },
    rvcSummary(){
      // RVC-P1: 引擎在跑时显示引擎真相（真实模型/变调），不再复述滑块值
      const s=this.rvcStatus;
      if(s && s.running){
        const m=(s.model||'').replace(/\.pth$/i,'');
        return '变声中'+(m?(' · '+m):'')+' · 变调'+(s.pitch>0?'+':'')+(s.pitch||0);
      }
      const m=this.rvc&&this.rvc.model?'模型✓':'未选模型';
      const p=this.rvc?(' · 变调'+(this.rvc.pitch>0?'+':'')+this.rvc.pitch):'';
      return m+p+(this.rvcActive?' · 变声中':'');
    },
    preSummary(){
      if(!this.envChecks||!this.envChecks.length) return '未检测';
      const ok=this.envChecks.filter(c=>c.ok).length;
      return ok+'/'+this.envChecks.length+' 项通过';
    },

    // 幂等启动器：deep-link 直落 #stream 时 $watch('tab') 不触发(初始 Tab 在 watcher 注册前已定)，
    // 历史上 pollRealtimeStatus 只挂在 watcher → 首屏直进开播页拿不到 swap 档位/后端健康裁决。init 里常开一环即根治。
    startRtPoll(){
      if(this._rtPollOn) return;
      this._rtPollOn = true;
      this.pollRealtimeStatus();
    },
    async pollRealtimeStatus(){
      try{
        const d = await fetch(HUB+'/realtime/status').then(r=>r.json());
        this.streamHealthBE = d.health || this.streamHealthBE;   // 后端健康裁决优先（缺失保留上次，避免回退抖动）
        this._autoSwNotice(d.dev_autoswitch_last, !!d.rvc_conv); // P7-1 秒级感知 + P8-1 自救卡进退场
        if(d.metrics){
          this.perf.fps = d.metrics.fps??this.perf.fps;
          this.perf.swap_ok = d.metrics.swap_ok??this.perf.swap_ok;
          this.perf.swap_fail = d.metrics.swap_fail??this.perf.swap_fail;
          this.perf.swap_latency_last = d.metrics.swap_latency_last??this.perf.swap_latency_last;
          this.perf.swap_latency_avg = d.metrics.swap_latency_avg??this.perf.swap_latency_avg;
          if((d.metrics.swap_latency_last||0)>0) this._pushLatHist(d.metrics.swap_latency_last);
          this.perf.faces_tgt = d.metrics.faces_tgt??this.perf.faces_tgt;
          this.perf.faces_used = d.metrics.faces_used??this.perf.faces_used;
          this.perf.faces_filtered = d.metrics.faces_filtered??this.perf.faces_filtered;
          this.perf.detect_ms = d.metrics.detect_ms??this.perf.detect_ms;
          this.perf.swap_ms = d.metrics.swap_ms??this.perf.swap_ms;
          this.perf.enhance_ms = d.metrics.enhance_ms??this.perf.enhance_ms;
          this.perf.smooth_ms = d.metrics.smooth_ms??this.perf.smooth_ms;
          this.perf.main_face = d.metrics.main_face;   // [P8] 锁主脸开关(多脸在镜 chip 用)
        }
        this.orphanStream = d.orphan||null;            // [P8] 孤儿画面进程(未受管且自动接管未成)→横幅提示一键接管
        this.orphanAdopted = d.orphan_adopted||null;   // [06t] 无闪断收养中→chip 如实展示(点停播/重新开播都照常管用)
        this.perf.streaming = !!d.video_running;
        this.perf.rvc_running = !!d.rvc_running;   // 注：rvc_running 追的是 gui_v1.py 手动实时路径(标准开播不用)，仅作展示；音频自愈改用 services.rvc(见 runHealAutoAudio)
        // RVC-P1: 引擎真实状态（null=引擎离线/旧版引擎无 /status → 徽章回退旧启发式）
        this.rvcStatus = ('rvc_status' in d) ? (d.rvc_status||null) : this.rvcStatus;
        if(this.rvcStatus){
          this.rvcActive = !!this.rvcStatus.running;   // 按钮态对齐引擎真相（页面刷新/别处起停都追平）
          // 输出静音观察：说话间隙 out_rms 也是 0，须「运行中持续 ≥20s 为 0」才亮告警
          if(this.rvcStatus.running && (this.rvcStatus.out_rms||0) < 0.0001){
            if(!this._rvcZeroSince) this._rvcZeroSince = Date.now();
          } else this._rvcZeroSince = 0;
        } else this._rvcZeroSince = 0;
        // 近窗增量（识别"中途丢脸"）：优先用后端在源头算的 swap_recent(更准更稳、不受前端 poll 抖动)，
        // 缺失时回退到"本轮相对上轮新增"的前端窗口。累计 swap_ok 很大但近窗 0 成功且失败在涨 ⇒ 中途丢脸。
        const _ok=this.perf.swap_ok||0, _fail=this.perf.swap_fail||0;
        const _sr=(d.metrics||{}).swap_recent;
        if(_sr && _sr.secs>0 && _sr.ok!=null && _sr.fail!=null){
          this.swapWin={ok:Math.max(0,_sr.ok||0), fail:Math.max(0,_sr.fail||0), src:'backend', secs:_sr.secs};
        } else if(this._swapPrev && _ok>=this._swapPrev.ok && _fail>=this._swapPrev.fail){
          this.swapWin={ok:_ok-this._swapPrev.ok, fail:_fail-this._swapPrev.fail, src:'frontend'};
        } else {
          this.swapWin=null;                       // 计数回退/重启→丢弃本窗，下轮重建（回退累计）
        }
        this._swapPrev={ok:_ok, fail:_fail};
        this.swapPs={ok:(d.metrics||{}).swap_ok_ps||0, fail:(d.metrics||{}).swap_fail_ps||0};  // 每秒速率（专家诊断用）
        // 驻留计时 + 策略表驱动的自动动作：坏状态持续越久越主动（无脸久了→展开原始画面；开了自动自愈则更久→换源重连）
        const _hs=this.streamHealth().state, _now=Date.now(), _prevHs=this.healthState;
        if(_hs!==this.healthState){
          this.healthState=_hs; this.healthSince=_now; this._healEpisode={};   // 新回合：清"本回合已执行"标记
          // 自愈确认：从坏态(无脸/服务掉线/卡顿/检测中)恢复到"换脸生效中"→给一次绿色提示（15s 防抖，避免抖动刷屏）
          if(_hs==='ok' && _prevHs && _prevHs!=='ok' && _prevHs!=='idle' && (_now-this._lastOkToast>15000)){
            this._lastOkToast=_now; this.showToast('✅ 换脸已生效','success');
          }
        }
        this.healthDwell=Math.round((_now-this.healthSince)/1000);
        this.runHealAuto(_hs);
        this.runHealAutoAudio();   // P3 音频自愈：正交于视频 state，独立评估「直播端持续无声」
        this.maybeAutoApplyLow();
      }catch(e){}
      if(this.tab==='stream' || this.perf.streaming) this.pollSwapTier();
      if(this.tab==='stream') this.loadPhoneRelayStatus();
      const pollMs=(typeof document!=='undefined' && document.hidden)?15000:4000;
      setTimeout(()=>this.pollRealtimeStatus(), pollMs);
    },

    // P7-1 自动热切即时感知：/realtime/status 顺风车字段（后端只带 10min 内的最近一次）。
    //   首轮轮询只做基线（页面打开前发生的事件不重放 toast——刷新页面不再弹一遍）；
    //   此后新出现的事件弹 toast 并立刻重枚举设备（缺席条/运行设备秒级追平，不等 45s 兜底轮询）。
    // P8-1 失败自救卡：toast 会溜走，失败是「变声可能已停」的持续危机——挂常驻卡直到解除。
    //   进场：新失败事件；或基线轮里最近事件=失败且变声没在跑（刷新页面不豁免一场进行中的事故）。
    //   退场：转换恢复在跑（任何途径：自救卡重试成功/手动重启变声/后续自动切成功）或手动 ✕。
    _autoSwNotice(asw, convOn){
      if(convOn && this.autoSwFail) this.autoSwFail=null;
      if(!this._autoSwBaselined){
        this._autoSwBaselined=true; this._autoSwSeenTs=(asw&&asw.ts)||0;
        if(asw && asw.ok===false && !convOn) this._autoSwFailShow(asw, false);
        return;
      }
      if(!asw || !asw.ts || asw.ts===this._autoSwSeenTs) return;
      this._autoSwSeenTs=asw.ts;
      if(asw.ok){
        this.autoSwFail=null;
        this.showToast('⚡ 设备缺席，已自动热切到 '+(asw.to||'推荐设备')
          +(asw.out?('（出='+asw.out+'）'):'')+' · 本场第'+(asw.n||1)+'次','success');
      } else this._autoSwFailShow(asw, true);
      this.rvcRefreshDevices();
    },
    _autoSwFailShow(asw, toast){
      if(!this.autoSwFail || this.autoSwFail.ts!==asw.ts) this._devFlow('expose','in','rescue');  // 卡片真亮出才记曝光（同一事件不重记）
      this.autoSwFail=asw;
      if(toast) this.showToast('设备缺席，自动热切失败：'+(asw.detail||'未知原因')+'——请检查设备或手动处理','error');
    },
    // 自救卡动作②：直接重启变声（设备其实已恢复/驱动缓过来时最快的一条路）。
    //   刻意不在这里清卡：转换真跑起来后 rvc_conv 顺风车 4s 内让卡自动退场——按真相收敛，不按乐观假设。
    async autoSwRescueRestart(){
      if(this.devHotBusy) return;
      this.devHotBusy=true;
      try{ await this.rvcStartConversion(); }
      finally { this.devHotBusy=false; }
    },
    // 引擎画质档快照：生效档/目标档/降档原因/增强能力(caps)。开播页状态条+增强下拉过滤共用
    async pollSwapTier(){
      try{
        const d = await fetch(HUB+'/realtime/swap/status').then(r=>r.json());
        this.swapTier = d || {up:false};
      }catch(e){ this.swapTier = {up:false}; }
    },
    tierLabel(k){ return this._tierLabels[k] || k || '—'; },
    // [P8] 多脸在镜 chip：画面里≥2张脸才出现。锁主脸开=绿(只换最大脸)；关=橙(全换,时延约翻倍)；
    // 双人档启用=蓝(source_map 显式优先,锁主脸自动让位——06s 联动,免得开关状态误导观感)
    facesChip(){
      const n=this.perf.faces_tgt;
      if(!this.perf.streaming||!n||n<2) return null;
      if(this.faceMap && this.faceMap.enabled)
        return {on:true, dual:true,
          txt:'👥'+n+'脸在镜·双人档',
          tip:'双人换脸映射已启用：左脸=槽0、右脸=槽1 各换各脸（第三人回退槽0）。此模式下「锁主脸」自动让位（引擎按槽全换），无需手动关。'};
      const mf=this.perf.main_face;
      return {on:mf===true,
        txt:'👥'+n+'脸在镜'+(mf===true?'·已锁主脸':(mf===false?'·全换模式':'')),
        tip:mf===true
          ?('画面里有 '+n+' 张脸：已锁定最大的一张(主播)换脸，海报/路人/屏幕里的脸保持原样。双人同框需全换时可关闭锁主脸。')
          :('画面里有 '+n+' 张脸且未锁主脸：每张都换脸并精修，时延约翻倍、裁剪通道退全帧。单人直播建议开启锁主脸。')};
    },
    // 生效档芯片：与目标一致=绿「✓高清」；被自适应压低=琥珀「自然 ↓目标高清」+降档原因，画质变化不再无声无息
    tierChip(){
      const st=this.swapTier||{};
      if(!st.up) return null;
      const au=st.auto||{};
      const eff=au.effective||st.preset, tgt=au.target||eff;
      const degraded=!!(au.enabled && eff && tgt && eff!==tgt);
      const enh=(st.params && st.params.enhance && st.params.enhance!=='none')?'·精修':'';
      return {degraded, eff, tgt, reason:au.reason||'',
              text: degraded ? ('引擎 '+this.tierLabel(eff)+enh+' ↓ 目标'+this.tierLabel(tgt))
                             : ('引擎 ✓ '+this.tierLabel(eff)+enh)};
    },
    // 增强能力(hub 直探换脸引擎 /health)：未部署的选项在下拉里禁用，杜绝"选了没效果"
    enhanceCap(k){
      const c=(this.swapTier||{}).caps;
      if(!c || !c.ok) return true;      // 探不到时不误禁用
      return c[k]!==false;
    },

    // E-2 硬件档位：本机 GPU → 档位 + 每功能「推荐/降档/不建议」预期(与销售一页纸同口径)
    async loadHwGuide(){
      if(this.hwGuide) return;   // 静态事实,进设置页拉一次即可
      try{ const d=await fetch(HUB+'/api/hardware/guide').then(r=>r.json());
        if(d && d.ok) this.hwGuide=d; }catch(_){}
    },

    // D-1 设备体检分：麦克风噪声底(录3秒) + 摄像头活帧/脸占比 + CABLE → 0-100 开播总分
    async deviceCheckup(){
      if(this.devBusy) return;
      this.devBusy=true;
      this.showToast('体检中：请保持安静环境（约 4 秒，说话可测信噪比）…','info');
      try{
        // P0 修 bug：把选中的麦传给后端（此前录音永远打系统默认麦，测的不是你选的那只）
        const dev=encodeURIComponent(this.audioInput||this.rvc.inputDevice||'');
        const r = await fetch(HUB+'/api/device/checkup?mic_secs=3&src=manual&device='+dev).then(r=>r.json());   // src=manual: 留痕(不进误拦分母)
        if(!r.ok) throw new Error(r.detail||'体检失败');
        this.devCheck = r;
      }catch(e){ this.showToast('设备体检失败：'+e, 'error'); }
      this.devBusy=false;
    },

    // 画质体检：抓当前帧按脸区对比换脸前后清晰度/亮度 → 一句可执行建议(结果条显示,15s 后自动收起)
    async qualityCheck(){
      if(this.qcBusy) return;
      this.qcBusy=true;
      try{
        this.qc = await fetch(HUB+'/realtime/swap/quality').then(r=>r.json());
        clearTimeout(this._qcTimer);
        this._qcTimer=setTimeout(()=>{ this.qc=null; }, 15000);
      }catch(e){ this.showToast('体检失败：'+e, 'error'); }
      this.qcBusy=false;
    },

    // ── P9 一键画质标定：POST 起后台任务 → 轮询到结束 → 展示建议阈值(已落盘,下次开播自动注入) ──
    async calibRun(){
      if(this.calib.busy) return;
      this.calib.busy=true; this.calib.msg=''; this.calib.phase='启动中';
      try{
        const d = await fetch(HUB+'/api/swap/calibrate', {method:'POST'}).then(r=>r.json());
        if(!d.ok){
          this.calib={...this.calib, busy:false, msg:'标定没能开始：'+(d.detail||''), level:'warn'};
          return;
        }
        this.showToast('画质标定已开始（约1-2分钟）：四个画质档各打一批实测请求','info');
        const poll = async()=>{
          let st=null;
          try{ st = await fetch(HUB+'/api/swap/calibrate/status').then(r=>r.json()); }catch(_){}
          const job = st && st.job;
          if(job && job.running){
            this.calib.phase=(job.phase||'').replace(/^\s*\[/,'[');
            this._calibTimer=setTimeout(poll, 3000);
            return;
          }
          this.calib.busy=false; this.calib.phase='';
          if(job && job.ok){
            this.calib.level='good';
            this.calib.msg='标定完成：'+(job.detail||'');
            this.calib.saved=(st&&st.saved)||null;
            this.calib.needs=!!(st&&st.needs_calib); this.calib.age=(st&&st.age_days)??null;
          }else{
            this.calib.level='warn';
            this.calib.msg='标定失败：'+((job&&job.detail)||'状态获取失败');
          }
        };
        this._calibTimer=setTimeout(poll, 3000);
      }catch(e){
        this.calib={...this.calib, busy:false, msg:'标定请求失败：'+e, level:'warn'};
      }
    },
    async calibLoadSaved(){
      try{
        const st = await fetch(HUB+'/api/swap/calibrate/status').then(r=>r.json());
        if(st && st.ok){ this.calib.saved = st.saved && st.saved.down_ms ? st.saved : null;
                         this.calib.needs=!!st.needs_calib; this.calib.age=st.age_days??null;
                         if(st.job && st.job.running){ this.calib.busy=true; this.calibResume(); } }
      }catch(_){}
    },
    calibResume(){   // 页面刷新时标定还在跑 → 接上轮询
      clearTimeout(this._calibTimer);
      this._calibTimer=setTimeout(async()=>{
        let st=null; try{ st=await fetch(HUB+'/api/swap/calibrate/status').then(r=>r.json()); }catch(_){}
        const job=st&&st.job;
        if(job&&job.running){ this.calib.phase=job.phase||''; this.calibResume(); return; }
        this.calib.busy=false; this.calib.phase='';
        if(job){ this.calib.level=job.ok?'good':'warn'; this.calib.msg=(job.ok?'标定完成：':'标定失败：')+(job.detail||''); this.calib.saved=(st&&st.saved)||this.calib.saved; }
      }, 3000);
    },
    calibSavedLine(){
      const s=this.calib.saved;
      if(!s || !s.down_ms) return '';
      const d=new Date((s.ts||0)*1000);
      const day=isNaN(d.getTime())?'':(' · '+(d.getMonth()+1)+'/'+d.getDate());
      return '已标定：降档>'+s.down_ms+'ms · 回升<'+s.up_ms+'ms'+day+'（开播自动采用）';
    },
    // P10 标定周期化：从未标定 / 超 14 天 → 温和提示重标（引擎负载特征会随驱动/温度/后台负载漂移）
    calibStaleLine(){
      if(!this.calib.needs) return '';
      if(!this.calib.saved) return '本机还没做过画质标定：跑一次，自适应降档阈值就贴合这台机器';
      return '上次标定已 '+Math.round(this.calib.age||0)+' 天：负载特征会漂移，建议重标一次';
    },

    // 最近一个轮询窗口内的换脸增量（识别中途丢脸）；无窗口历史时回退累计
    recentSwap(){
      const w=this.swapWin;
      if(w) return {ok:w.ok||0, fail:w.fail||0};
      return {ok:this.perf.swap_ok||0, fail:this.perf.swap_fail||0};
    },

    swapFailRate(){
      const ok=this.perf.swap_ok||0, fail=this.perf.swap_fail||0;
      const total=ok+fail; if(total===0) return 0;
      return Math.round(fail*100/total);
    },
    // P0-3 近窗失败率（与 swapPs 同源，播中主展示；累计进 title）
    swapFailRateLive(){
      const ok=this.swapPs.ok||0, fail=this.swapPs.fail||0;
      const total=ok+fail;
      if(this.perf.streaming && total>=0.05) return Math.round(fail*100/total);
      return this.swapFailRate();
    },
    swapStatsTitle(){
      const ok=this.perf.swap_ok||0, fail=this.perf.swap_fail||0;
      return '累计自进程启动：成功 '+ok+' · 失败 '+fail+'（含预热/离席时段）';
    },
    swapStatsLine(){
      const ok=this.swapPs.ok||0, fail=this.swapPs.fail||0;
      if(this.perf.streaming && (ok>0||fail>0))
        return '近 1 分钟 · 成功 '+ok.toFixed(1)+'/s · 失败 '+fail.toFixed(1)+'/s · 失败率 '+this.swapFailRateLive()+'%';
      return '成功 '+(this.perf.swap_ok||0)+' · 失败 '+(this.perf.swap_fail||0)+' · 失败率 '+this.swapFailRate()+'%（累计）';
    },

    perfSuggestion(){
      // 无脸优先：在播但"近窗 0 成功且失败在涨"→几乎一定没对准脸，给可操作指引而非误导性"调阈值/降分辨率"
      const _r=this.recentSwap();
      if(this.perf.streaming && _r.ok===0 && _r.fail>=3)
        return {msg:'未检测到人脸：把脸对准镜头、拉近距离、确保光线充足（手机出镜请确认已对焦人脸）', tone:'orange'};
      const lat = this.perf.swap_latency_last||0;
      const avg = this.perf.swap_latency_avg||lat;
      const fr = (this.perf.streaming && (this.swapPs.ok+this.swapPs.fail)>=0.05) ? this.swapFailRateLive() : this.swapFailRate();
      const hi = this.perfThreshold.lat_high||800;
      const mid= this.perfThreshold.lat_mid||500;
      const frw= this.perfThreshold.fail||10;
      if(lat>hi || avg>hi*0.9) return {msg:`延迟高（>${hi}ms）：降到720p/20fps，质量60，换脸6fps，必要时关闭增强`, tone:'red'};
      if(lat>mid || avg>mid) return {msg:`延迟偏高（>${mid}ms）：尝试720p/25fps，质量65，或减小 crossfade`, tone:'orange'};
      if(fr>frw) return {msg:`失败率>${frw}%：提高检测阈值或降低分辨率/质量`, tone:'orange'};
      if(this.perf.swap_ok>0) return {msg:'延迟正常，可适当提升分辨率/质量试验', tone:'green'};
      return {msg:'等待实时数据…', tone:'muted'};
    },
    needLowPreset(){
      const tone=this.perfSuggestion().tone;
      return tone==='red' || tone==='orange';
    },

    // 开播健康（单一真相，英雄区/卡/预览灯共用）：优先读后端裁决 streamHealthBE（跨端一致 + 服务端自愈同源），
    //   后端字段缺失（旧后端/未连）才本地兜底。视觉文案统一由 _healthView(state,tone) 映射，前后端两条路只产出 state/tone。
    streamHealth(){
      const be=this.streamHealthBE;
      if(be && be.state) return this._healthView(be.state, be.tone);
      return this._streamHealthLocal();
    },
    // state(+tone) → 视觉文案（单源映射，避免前后端两份视觉漂移）
    _healthView(state, tone){
      switch(state){
        case 'svc_down': return {state, label:'换脸服务离线', dot:'bg-hub-red', text:'text-hub-red', ring:'border-hub-red/50',
                hint:'换脸引擎未连接：请启动/检查换脸服务，或在「设置」切到可用的换脸地址'};
        case 'stalled':  return {state, label:'画面停更', dot:'bg-hub-red', text:'text-hub-red', ring:'border-hub-red/50',
                hint:'画面已停止更新（摄像头假死/被占用/断线）：点〔换源重连〕重启视频管线恢复画面'};
        case 'noface':   return {state, label:'未检测到人脸', dot:'bg-hub-orange', text:'text-hub-orange', ring:'border-hub-orange/50',
                hint:'把脸对准镜头、拉近距离、确保光线充足（手机出镜请确认已对焦人脸）'};
        case 'warmup':   return {state, label:'正在检测人脸…', dot:'bg-hub-muted animate-pulse', text:'text-hub-muted', ring:'border-hub-border', hint:''};
        case 'lag':      return (tone==='red')
                ? {state, label:'换脸生效·卡顿', dot:'bg-hub-red', text:'text-hub-red', ring:'border-hub-red/50', hint:this.perfSuggestion().msg}
                : {state, label:'换脸生效·一般', dot:'bg-hub-orange', text:'text-hub-orange', ring:'border-hub-orange/50', hint:this.perfSuggestion().msg};
        case 'ok':       return {state, label:'换脸生效中', dot:'bg-hub-green', text:'text-hub-green', ring:'border-hub-green/50', hint:''};
        default:         return {state:'idle', label:'未开播', dot:'bg-hub-muted', text:'text-hub-muted', ring:'border-hub-border', hint:''};
      }
    },
    // 本地兜底：与后端 _compute_stream_health 同逻辑，只在后端裁决缺失时用
    _streamHealthLocal(){
      const P=this.perf||{}, live=!!P.streaming, cumOk=P.swap_ok||0;
      const r=this.recentSwap();
      if(!live && cumOk===0) return this._healthView('idle');
      if(live && this.services && this.services.faceswap===false) return this._healthView('svc_down','red');
      if(live && r.ok>0){
        const tone=this.perfSuggestion().tone;
        if(tone==='red')    return this._healthView('lag','red');
        if(tone==='orange') return this._healthView('lag','orange');
        return this._healthView('ok','green');
      }
      if(live && r.fail>=3) return this._healthView('noface','orange');
      if(live) return this._healthView('warmup');
      return this._healthView('idle');
    },
    toggleRawPeek(){ this.rawPeek=!this.rawPeek; },

    // ── 自愈调度（单源）：失败卡按钮与轮询自动动作都经此分发，新增动作只改策略表+这里 ──
    runHealAction(key){
      switch(key){
        case 'goSettings': this.goTab('settings'); break;
        case 'rawPeek':    this.toggleRawPeek(); break;
        case 'restart':    this.oneClickStart(); break;          // 已在直播→真重启管线（同手动「换源重连」）
        case 'downgrade':  this.applyLowLatencyPreset(); break;
        case 'autoRaw':    if(!this.rawPeek) this.rawPeek=true; break;   // 仅展开，不与手动关闭对抗
        case 'freeVram':   this.freeVram(); break;
        case 'audioRecover': this.startRvcApi(); break;                 // P3/P4 音频自愈：拉起变声服务(6242)恢复声音，幂等·不碰视频
      }
    },
    healLabel(a){ return (a.toggle && this[a.toggle]) ? (a.labelOff||a.label) : a.label; },
    persistAutoHeal(){ try{ localStorage.setItem('hub_autoheal', this.autoHeal?'1':'0'); }catch(_){}
      this.showToast(this.autoHeal?'已开启自动自愈（无人值守，带冷却/限次护栏）':'已关闭自动自愈','info'); },
    // 策略表驱动的驻留自动动作；护栏：requires(开关)/guardBusy(进行中跳过)/cooldownSec(冷却)/maxPerSession(限次)/oncePerEpisode
    runHealAuto(state){
      const now=Date.now();
      for(const r of this.healAuto){
        if(r.state!==state || this.healthDwell<r.dwellSec) continue;
        if(r.oncePerEpisode && this._healEpisode[r.key]) continue;
        // 破坏类(重启)若服务端守护已接管(HUB_AUTOHEAL)，本端让渡——由服务端去重重启，避免双重启
        if(r.destructive && this.streamHealthBE && this.streamHealthBE.autoheal_server){
          if(!this._healEpisode['_srvDefer']){ this._healEpisode['_srvDefer']=true;
            this.healLog.unshift({ts:now, msg:'无脸过久·已交由服务端自愈重启'});
            if(this.healLog.length>5) this.healLog=this.healLog.slice(0,5); }
          continue;
        }
        if(r.requires && !this[r.requires]) continue;
        if(r.guardBusy && this.streaming) continue;
        if(r.cooldownSec && (now-(this._healCooldown[r.key]||0))<r.cooldownSec*1000) continue;
        if(r.maxPerSession && (this._healCount[r.key]||0)>=r.maxPerSession) continue;
        this.runHealAction(r.key);
        if(r.oncePerEpisode) this._healEpisode[r.key]=true;
        if(r.cooldownSec)   this._healCooldown[r.key]=now;
        if(r.maxPerSession) this._healCount[r.key]=(this._healCount[r.key]||0)+1;
        if(r.trail){
          this.healLog.unshift({ts:now, msg:r.trail+(r.maxPerSession?(' ×'+(this._healCount[r.key]||0)):'')});
          if(this.healLog.length>5) this.healLog=this.healLog.slice(0,5);
          this.showToast('🔧 自愈：'+r.trail,'info');
        }
      }
    },
    // 「变声中」判定（徽章用）：RVC-P1 起以引擎 /status 为单一真相（拾音流真在跑才算），
    //   引擎离线/旧版无 /status 时回退旧启发式——手动实时(gui_v1.py=rvc_running) 或 推流中且 6242 在线。
    rvcLive(){
      if(this.rvcStatus) return !!this.rvcStatus.running;
      return this.perf.rvc_running || (this.perf.streaming && !!(this.services&&this.services.rvc));
    },
    // RVC-P1: 输出静音告警——变声在跑但输出电平持续 ≥20s 为 0（说话间隙不误报）。
    //   典型原因：听/推的不是 CABLE、输出设备路由错、推理出声但被系统静音。
    rvcSilenceWarn(){
      return !!(this.rvcStatus && this.rvcStatus.running && this._rvcZeroSince
                && (Date.now()-this._rvcZeroSince) > 20000);
    },
    // RVC-P1: 面板状态行（引擎真相）：模型 / 变调 / 推理耗时 / 输出电平
    rvcStatusLine(){
      const s=this.rvcStatus; if(!s || !s.running) return '';
      const m=(s.model||'').replace(/\.pth$/i,'') || '未知模型';
      const parts=['正在用 '+m, '变调 '+(s.pitch>0?'+':'')+(s.pitch||0)];
      if(s.avg_infer_ms) parts.push('推理 '+Math.round(s.avg_infer_ms)+'ms/块');
      parts.push('输出电平 '+((s.out_rms||0)>=0.0001?((s.out_rms).toFixed(3)):'0'));
      if(s.stalled) parts.push('⚠ 推理疑似卡住');
      return parts.join(' · ');
    },
    // P3/P4 音频自愈评估（每轮轮询调一次）：仅直播中·stream 页 + 「本场变声服务(6242)曾在线、现在掉了且持续」→ 拉起变声 api。
    //   信号取 services.rvc（准确反映 6242 是否活着，比 rvc_running 靠谱）；主播停顿不会让端口掉线 ⇒ 无"没说话"误触。
    //   服务端守护开(HUB_AUTOHEAL)时本端让渡——由服务端去重拉起(它有 _LAST_RVC_API_START_TS)，避免双重启。护栏与视频侧同源(总开关/冷却/限次)。
    runHealAutoAudio(){
      if(this.tab!=='stream' || !this.perf.streaming || this.streaming) return;   // 非stream页/未直播/管线启动中 → 不评估
      const now=Date.now();
      const up=this.services&&this.services.rvc;
      if(up===true){ this._rvcApiSeenUp=true; this._rvcApiDownSince=0; return; }  // 在线→记见过 + 清掉线计时
      if(up!==false) return;                                                      // 未知(未上报)→不冒进
      if(!this._rvcApiSeenUp) return;                                             // 本场从没见它在线(可能没配音频)→不越权
      if(!this._rvcApiDownSince) this._rvcApiDownSince=now;                       // 首次见掉线→记起点，下轮再判是否持续
      // 服务端守护已接管 → 本端让渡（仅记一次轨迹，交服务端拉起并去重）
      if(this.streamHealthBE && this.streamHealthBE.autoheal_server){
        if(!this._healEpisode['_srvDeferAudio']){ this._healEpisode['_srvDeferAudio']=true;
          this.healLog.unshift({ts:now, msg:'变声服务掉线·已交由服务端自愈拉起'});
          if(this.healLog.length>5) this.healLog=this.healLog.slice(0,5); }
        return;
      }
      const downSecs=(now-this._rvcApiDownSince)/1000;
      const streamElapsed=this.streamingSinceTs?(now-this.streamingSinceTs)/1000:1e9;
      for(const r of this.healAutoAudio){
        if(streamElapsed < r.dwellSec) continue;                                  // 开播启动 grace，避免开播瞬间误判
        if(downSecs < r.dwellSec) continue;                                       // 须持续掉线≥dwell(滤瞬时探测抖动)
        if(r.requires && !this[r.requires]) continue;                             // 需开「自动自愈」总开关（默认关）
        if(r.cooldownSec && (now-(this._healCooldown[r.key]||0))<r.cooldownSec*1000) continue;
        if(r.maxPerSession && (this._healCount[r.key]||0)>=r.maxPerSession) continue;
        this.runHealAction(r.key);
        this._rvcApiDownSince=0;                                                  // 触发后复位掉线计时（下轮从头累计）
        if(r.cooldownSec)   this._healCooldown[r.key]=now;
        if(r.maxPerSession) this._healCount[r.key]=(this._healCount[r.key]||0)+1;
        if(r.trail){
          this.healLog.unshift({ts:now, msg:r.trail+(r.maxPerSession?(' ×'+(this._healCount[r.key]||0)):'')});
          if(this.healLog.length>5) this.healLog=this.healLog.slice(0,5);
          this.showToast('🔧 自愈：'+r.trail,'info');
        }
      }
    },

    // 显存吃紧（≥85%）；本模式有"用不到却在线"的重型引擎 → 可一键腾显存（精准卸载、不打断直播）
    get vramTight(){ return this.perf.gpu_mem_total>0 && (this.perf.gpu_mem_used/this.perf.gpu_mem_total)>=0.85; },
    // 本模式用不到、却仍在线占显存的候选引擎（当前在线者）；与后端 _FREE_UNUSED_CAND 口径一致
    freeVramCandidates(){
      const b=this.broadcast; if(!b||!b.mode) return [];
      const s=this.services||{};
      const cand = b.mode==='real_faceswap' ? ['ditto','lipsync','latentsync','echomimic','faceswap2']
                                            : ['faceswap','faceswap2','latentsync','echomimic'];
      return cand.filter(n=>s[n]===true);
    },
    canFreeVram(){ return this.freeVramCandidates().length>0; },
    async freeVram(){
      if(this.freeVramBusy) return;
      this.freeVramBusy=true;
      this.showToast('正在卸载本模式用不到的引擎以腾出显存…','info');
      try{
        const d=await fetch(HUB+'/api/gpu/free_unused',{method:'POST'}).then(r=>r.json());
        const n=d.stopped_ok||0;
        const bMb=(d.vram_before&&d.vram_before.used_mb)||0, aMb=(d.vram_after&&d.vram_after.used_mb)||0;
        const tot=(d.vram_before&&d.vram_before.total_mb)||this.perf.gpu_mem_total||0;
        const freedGb=bMb>aMb?((bMb-aMb)/1024):0;
        const pct=x=>tot>0?Math.round(x/tot*100):null;
        if(d.ok && n>0){
          const names=(d.targets||[]).map(t=>this.svcName(t)).join('、');
          let msg='✅ 已停 '+n+' 个未用引擎（'+names+'）';
          if(freedGb>=0.1) msg+='，释放约 '+freedGb.toFixed(1)+' GB';
          if(pct(bMb)!=null && pct(aMb)!=null) msg+='（'+pct(bMb)+'%→'+pct(aMb)+'%）';
          else msg+='，显存稍后回收';
          this.showToast(msg,'success');
        } else {
          this.showToast('当前没有可安全卸载的未用引擎——显存主要被直播必需的对话大脑与语音引擎占用（保留）','info');
        }
        setTimeout(()=>{ if(this.pollHealth) this.pollHealth(); }, 1500);
      }catch(e){ this.showToast('腾显存失败：'+String(e),'error'); }
      finally{ this.freeVramBusy=false; }
    },
    // 解读后端 start_video 步骤 → 本次「实际用了哪个视频源」；auto_reason(device_enum.pick_camera_source) 是单一真相
    interpretVideoSource(step){
      const r=((step&&step.auto_reason)||'').toLowerCase();
      const camStr=(step&&step.camera!=null)?String(step.camera):'';
      if(r.indexOf('phone-wifi')>=0)  return {kind:'phone_wifi',  label:'手机·WiFi 摄像头', isPhone:true,  degraded:false, reason:r, url:camStr};
      if(r.indexOf('android-adb')>=0) return {kind:'phone_scrcpy',label:'手机·USB 投屏',    isPhone:true,  degraded:false, reason:r, url:''};
      if(r.indexOf('phone-cam')>=0)   return {kind:'phone_vcam',  label:'手机·虚拟摄像头',   isPhone:true,  degraded:false, reason:r, url:''};
      if(r.indexOf('generic-cam')>=0) return {kind:'local_cam',   label:'本机摄像头',        isPhone:false, degraded:false, reason:r, url:''};
      if(r.indexOf('fallback')>=0)    return {kind:'fallback',    label:'投屏兜底(可能无画面)', isPhone:false, degraded:true,  reason:r, url:''};
      if(r.indexOf('auto-failed')>=0) return {kind:'error',       label:'自动选源异常',      isPhone:false, degraded:true,  reason:r, url:''};
      // auto_reason 为空 = 用户手动指定源（scrcpy / 摄像头索引 / mjpeg URL）
      if(camStr==='scrcpy')           return {kind:'phone_scrcpy',label:'手机·USB 投屏',     isPhone:true,  degraded:false, reason:'manual', url:''};
      if(/^https?:/.test(camStr))     return {kind:'phone_wifi',  label:'手机·WiFi 摄像头',  isPhone:true,  degraded:false, reason:'manual', url:camStr};
      if(/^\d+$/.test(camStr))        return {kind:'local_cam',   label:'摄像头 #'+camStr,   isPhone:false, degraded:false, reason:'manual', url:''};
      return {kind:'', label:'', isPhone:false, degraded:false, reason:r, url:''};
    },
    // 连接状态条·视频：直播中显示后端实际裁决，未开播显示「就绪预览」意图。dot: green手机/blue本机/red需处理/gray未连
    videoLive(){
      // 直播中：用后端真实「活帧+健康态」(出画/卡顿/没脸/启动中/引擎离线)，叠加来源标签(手机/本机)。真信号，非"选了谁"。
      if(this.perf.streaming){
        const sv=this.signal.video||{}, st=sv.state||this.streamHealth().state||'warmup';
        const src=this.lastVideo||{}, srcLabel=src.label||(src.isPhone?'手机摄像头':'本机摄像头');
        const fpsTxt=sv.fps?(' · '+sv.fps+'fps'):'';
        if(st==='svc_down') return {dot:'red',   label:'换脸引擎离线', sub:'画面无法输出，请查换脸服务'};
        if(st==='noface')   return {dot:'red',   label:srcLabel, sub:'没对准脸'+fpsTxt};
        if(st==='lag')      return {dot:'red',   label:srcLabel, sub:'画面卡顿'+fpsTxt};
        if(st==='ok')       return {dot:'green', label:srcLabel, sub:'出画中'+fpsTxt};
        return {dot:'blue', label:srcLabel, sub:'画面启动中…'};   // warmup / 刚起数据未新鲜
      }
      if(this.phoneRelayCam.live)
        return {dot:'green', label:'手机·WiFi 摄像头',
                sub:this.phoneRelayCam.w?(this.phoneRelayCam.w+'×'+this.phoneRelayCam.h+' @'+this.phoneRelayCam.fps+'fps'):'已推流'};
      if(this.videoSource==='scrcpy') return {dot:'blue', label:'手机·USB 投屏', sub:'开播时连接'};
      if(this.videoSource==='camera') return {dot:this.cameras.length?'blue':'gray', label:'本机摄像头', sub:this.cameras.length?'已就绪':'未检测到'};
      return {dot:this.cameras.length?'blue':'gray',
              label:this.cameras.length?'自动·本机兜底':'自动·等待视频源',
              sub:'手机在线将优先用手机'};
    },
    // 连接状态条·音频输入：设备判定(可靠)为主 + 真实监听电平(正向确认)。dev 名判 手机麦/普通麦/无；
    // monLevel/talking 来自 monitor_relay 输出环回(PC 监听电平，非麦输入)——只做「有声」正向提示，静音不误报。
    audioLive(){
      const dev=this.audioInput||'';
      const isPhone=/droid|ivcam|camo|epoccam|wo ?mic|iphone|phone|voicemeeter|蓝牙|bluetooth/i.test(dev);
      const a=this.signal.audio||{};       // P1-B monitor 输出电平(PC 听到的声)
      const c=this.signal.cable||{};       // P1-F 广播馈线(CABLE)=OBS 实际读到的「直播真声」
      const cableOk=!!c.ok;
      // 电平/有声：优先用广播馈线(直播真声，最可信)，其次 monitor 输出电平
      const monActive=cableOk||!!a.ok;
      const srcRms=cableOk?(c.rms||0):(a.rms||0);
      const monLevel=monActive?Math.max(0,Math.min(100,Math.round(srcRms*350))):-1;      // rms≈0.28 满格
      const talkingCable=!!(cableOk&&this.cableActiveTs&&(Date.now()-this.cableActiveTs<8000));
      const talkingMon=!!(this.audioActiveTs&&(Date.now()-this.audioActiveTs<6000));      // 最近6s有声(衰减)
      const talking=cableOk?talkingCable:talkingMon;                                      // 有馈线探针就以「直播真声」为准
      let base;
      if(dev){
        const lbl=this.audioDevLabel('in',dev)||dev;   // P0: 状态丸也讲人话（有结构化层时）
        base={dot:'green', label:isPhone?'手机麦克风':'麦克风', sub:lbl.length>18?lbl.slice(0,18)+'…':lbl};
      }
      else{
        const hasPhoneMic=this.rvcInputDevices.some(d=>/droid|ivcam|camo|epoccam|wo ?mic|iphone|phone|蓝牙|bluetooth|麦克风|mic/i.test(d));
        base = hasPhoneMic ? {dot:'blue', label:'麦克风待选', sub:'开播自动选择'} : {dot:'gray', label:'未连麦克风', sub:'点 ⟳ 刷新设备'};
      }
      return {...base, monLevel, monActive, talking, cableOk};
    },
    // P1-F 直播端持续无声：仅当直播中 + 广播馈线探针可信(cableOk) + 从最近有声(或开播起)已静音≥阈值。返回静音秒数(0=不判定)。
    cableSilentSecs(){
      if(!this.perf.streaming) return 0;
      const c=this.signal.cable||{};
      if(!c.ok) return 0;                                             // 探针不可信→不下「没声音」结论(不误报)
      const since=this.cableActiveTs||this.streamingSinceTs||Date.now();
      return Math.floor((Date.now()-since)/1000);
    },
    // P1-F+ 「真没声」判定与断点定位(单一真相)：仅直播中；返回 verdict 形状对象供 linkVerdict/自检报告直接用，无异常→null。
    //   分两档：①探针可信+持续静音=确证无声→按线索定位断点(变声掉线/没进声卡/麦克风侧)；②探针长时不可信=测不到直播声(弱一档，需持续够久避误报)。
    soundDiag(){
      if(!this.perf.streaming) return null;
      const c=this.signal.cable||{}, cdev=c.dev||'CABLE';
      const rvc=this.services&&this.services.rvc;                                     // 6242 在线?(/inputDevices 探，准)
      const monRecent=!!(this.audioActiveTs&&(Date.now()-this.audioActiveTs<8000));   // PC 监听最近有声=声音确在产出(用于区分「在出声但没进CABLE」)
      if(c.ok){                                                                       // 探针可信：能读到 OBS 实际采到的直播声电平
        const hadSound=!!this.cableActiveTs;
        const since=this.cableActiveTs||this.streamingSinceTs||Date.now();
        const silent=Math.floor((Date.now()-since)/1000);
        if(silent<(hadSound?15:30)) return null;                                      // 未静音到阈值(开场沉默更宽容)→不报
        if(rvc===false)                                                              // 断点①：变声服务掉线→声音处理链断
          return {level:'error', icon:'🔇', cause:'rvc_down', silent,
            title:'直播没声·变声服务掉线',
            detail:'变声服务(6242)离线，你的声音没法处理进直播（'+cdev+'）已 '+silent+'s 无声。点下方一键拉起变声恢复；或先关掉变声用原声直播。',
            ctaLabel:'▶ 拉起变声', ctaKey:'startRvc'};
        if(monRecent)                                                               // 断点②：在出声(监听有电平)但馈线没声→输出没进CABLE
          return {level:'error', icon:'🔇', cause:'route', silent,
            title:'直播没声·声音没进虚拟声卡',
            detail:'检测到你在出声、但直播馈线（'+cdev+'）已 '+silent+'s 收不到——多半是变声「输出设备」没选到 CABLE Input。去设置把变声输出改成 CABLE。',
            ctaLabel:'去设置检查输出', ctaKey:'goSettings'};
        // P3-1：无声 + 偏好麦确认已拔 + 变声在跑 → 给「切到推荐并重启变声」一键（比让用户手工
        //   选设备→停→配→启的 10 秒链路快得多，也不打断画面）；其余情形保持「刷新设备」。
        const _hot=this.devHotOffer('in');
        return hadSound                                                             // 断点③：麦克风侧(被静音/离开/没拾到)
          ? {level:'warn', icon:'🔇', cause:'mic_lost', silent,
             title:'直播声音中断了',
             detail:'广播馈线（'+cdev+'）约 '+silent+'s 无声——之前有声、现在没了。'+(_hot?'上次用的麦克风已拔出，可一键切到推荐麦并重启变声。':'检查麦克风是否被静音/拔线，或你是否离开了麦克风。'),
             ctaLabel:_hot?'⚡ 切到推荐麦':'⟳ 刷新设备', ctaKey:_hot?'audioHotSwap':'refreshDev'}
          : {level:'warn', icon:'🔇', cause:'mic_never', silent,
             title:'直播端一直没声音',
             detail:'已开播约 '+silent+'s，'+cdev+' 仍没检测到声音。'+(_hot?'上次用的麦克风已拔出，可一键切到推荐麦并重启变声。':'若你在说话：确认麦克风选对、没被静音，声音要能经变声送进 CABLE。'),
             ctaLabel:_hot?'⚡ 切到推荐麦':'⟳ 刷新设备', ctaKey:_hot?'audioHotSwap':'refreshDev'};
      }
      // 探针不可信：读不到 CABLE 电平——测不到直播声(比确证无声弱一档；需开播过宽限且持续不可信够久，避开开播瞬间/偶发抖动)
      const okAgo=this.cableOkTs?(Date.now()-this.cableOkTs)/1000:1e9;
      const streamAgo=this.streamingSinceTs?(Date.now()-this.streamingSinceTs)/1000:0;
      if(streamAgo>25 && okAgo>25)
        return {level:'warn', icon:'🔈', cause:'probe_blind', silent:0,
          title:'测不到直播声',
          detail:'读不到虚拟声卡（'+cdev+'）的电平——可能 CABLE 被别的程序独占、设备名变了或未装好。刷新设备重试；OBS 请确认音频源选的是 CABLE Output。',
          ctaLabel:'⟳ 刷新设备', ctaKey:'refreshDev'};
      return null;
    },
    // 降级显形：直播中本次没用上手机（回退本机/兜底）→ 明确提示 + 一步切手机。degraded 无论意图都提示；否则仅 auto 意图落本机才提示
    get showVideoFallbackNotice(){
      if(!this.perf.streaming || this.videoNoticeDismissed || !this.lastVideo.kind) return false;
      if(this.lastVideo.degraded) return true;
      return !this.lastVideo.isPhone && this.lastVideo.wanted==='auto';
    },

    // P1-C 整链路主裁决(单一真相汇总)：合并 视频(健康态/活帧)+音频(设备/电平)+服务(换脸)+显存 → 一条主状态 + 一个首要动作。
    //   优先级：硬故障(error红) > 预警(warn橙) > 启动中(info蓝) > 正常(ok绿)；未开播给「就绪/待修复」。
    //   CTA 全部复用现有动作(freeVram/换源重连/去设置/降档/切手机/刷新/去角色库)，零后端。是 P0 状态条 + P1-B 真信号的收口。
    linkVerdict(){
      const P=this.perf||{}, a=this.audioLive(), hs=this.streamHealth().state;
      const vramPct=(P.gpu_mem_total>0)?Math.round(P.gpu_mem_used/P.gpu_mem_total*100):0;
      const vram=this.vramTight&&this.canFreeVram();
      // ── 未开播：就绪裁决 ──
      if(!P.streaming){
        if(!this.streamReady())
          return {level:'error', icon:'🎭', title:'未选出镜角色', detail:'先在「角色库」启用一个出镜角色，再开播。', ctaLabel:'去角色库', ctaKey:'profiles'};
        if(vram)
          return {level:'warn', icon:'🧹', title:'显存吃紧 '+vramPct+'%', detail:'有当前用不到的引擎占着显存，建议开播前腾出（不影响后续直播）。', ctaLabel:'一键腾显存', ctaKey:'freeVram'};
        return {level:'ok', icon:'✅', title:'就绪，可开播', detail:'视频：'+this.videoLive().label+' · 音频：'+a.label, ctaLabel:'', ctaKey:''};
      }
      // ── 直播中：健康裁决（硬故障优先）──
      if(hs==='svc_down')
        return {level:'error', icon:'🛑', title:'换脸服务离线·画面无法输出', detail:'换脸引擎未连接，OBS 里会是空画面。去「设置」检查或切换换脸地址。', ctaLabel:'去设置检查', ctaKey:'goSettings'};
      if(hs==='stalled')
        return {level:'error', icon:'📵', title:'画面停更·可能摄像头假死', detail:'视频画面已停止更新（摄像头假死/被占用/断线），OBS 里会是卡住的旧画面。点换源重连重启视频管线。', ctaLabel:'🔄 换源重连', ctaKey:'restart'};
      const snd=this.soundDiag();               // P1-F+ 「真没声」：确证无声且断点明确(变声掉线/没进声卡)→与黑屏同级，先于 noface/lag 报
      if(snd && snd.level==='error') return snd;
      if(hs==='noface')
        return {level:'warn', icon:'📸', title:'没对准脸·未在换脸', detail:'把脸对准镜头、拉近距离、确保光线充足（手机出镜确认已对焦）。', ctaLabel:'🔄 换源重连', ctaKey:'restart'};
      if(hs==='lag')
        return {level:'warn', icon:'⚠️', title:'画面卡顿', detail:this.perfSuggestion().msg||'延迟偏高，建议一键降档。', ctaLabel:'⚡ 一键降档', ctaKey:'downgrade'};
      if(snd) return snd;                      // P1-F+ 其余无声情形(warn)：曾有声中断/一直没声/探针测不到——排在 noface/lag 之后
      if(this.showVideoFallbackNotice)
        return {level:'warn', icon:'📱', title:'没用上手机摄像头', detail:'本次用的是「'+(this.lastVideo.label||'本机')+'」。想手机出镜可一键切换。', ctaLabel:'📱 改用手机', ctaKey:'phone'};
      if(vram)
        return {level:'warn', icon:'🧹', title:'显存吃紧 '+vramPct+'%', detail:'有当前用不到的引擎占着显存，可腾出且不影响直播。', ctaLabel:'一键腾显存', ctaKey:'freeVram'};
      if(a.dot==='gray')
        return {level:'warn', icon:'🎙️', title:'未连麦克风', detail:'没检测到可用麦克风，观众可能听不到声音。刷新设备或接入麦克风。', ctaLabel:'⟳ 刷新设备', ctaKey:'refreshDev'};
      if(hs==='warmup')
        return {level:'info', icon:'⏳', title:'画面启动中…', detail:'正在建立管线/检测人脸，稍候几秒。', ctaLabel:'', ctaKey:''};
      // ── 一切正常 ──
      const fpsTxt=this.signal.video.fps?(' · '+this.signal.video.fps+'fps'):'';
      return {level:'ok', icon:'✅', title:'直播链路正常', detail:'出画中'+fpsTxt+(a.talking?' · 有声':'')+' · 显存 '+vramPct+'%', ctaLabel:'', ctaKey:''};
    },
    // 主裁决 CTA 分发：全部复用既有动作，不新增副作用
    verdictCta(key){
      switch(key){
        case 'freeVram':   this.freeVram(); break;
        case 'restart':    this.runHealAction('restart'); break;       // 换源重连(=重新开播管线)
        case 'goSettings': this.runHealAction('goSettings'); break;
        case 'downgrade':  this.runHealAction('downgrade'); break;
        case 'phone':      this.wirelessStart(); break;                 // 改用手机开播
        case 'refreshDev': this.rvcRefreshDevices(); this.loadCameras(); this.showToast('已刷新音视频设备','info'); break;
        case 'startRvc':   this.startRvcApi(); break;                   // P1-F+ 真没声·变声掉线：一键拉起变声服务(6242)，幂等
        case 'audioHotSwap': this.devHotSwitch('diag','in'); break;     // P3-1 拔插热切：切推荐设备并重启变声(无声诊断入口)
        case 'profiles':   this.goTab('profiles'); break;
      }
    },

    // ══════════════════════════════════════════════════════════════════════
    // [降噪 P1] 纯前端呈现层（数据源不变，只改「怎么显示」；整块删除即完全回退）
    //   WS1 : 健康「单一真相」——主裁决 linkVerdict() 已完整覆盖同一状态时，下方健康卡不重复
    //   WS1b: 开播页正实时呈现的健康类后端告警(stream_health/cable_silence*)收进告警中枢「更多」，
    //         杜绝「顶部红条说没脸/无声、页内却说在播 30fps」这类矛盾（实时信号比后端告警更新）
    //   WS2 : 告警中枢——多条告警只常驻最高优先级 1 条 + 「还有 N 条」可展开，不再堆叠顶掉正文
    //   WS3 : 黑话→人话（仅用户模式；原文保留在 title 悬浮 / 专家模式）
    // ══════════════════════════════════════════════════════════════════════
    _bdVerdictCoverMap:{svc_down:'goSettings', stalled:'restart', noface:'restart', lag:'downgrade'},
    verdictCoversHealth(){                       // 主裁决是否正在说「同一件事、给同一动作」
      if(!this.perf.streaming) return false;
      const want=this._bdVerdictCoverMap[this.streamHealth().state];
      return !!want && this.linkVerdict().ctaKey===want;
    },
    healthCardRedundant(){
      // 仅当健康卡「无独有工具」时才整卡隐藏：lag/svc_down 与主裁决完全重复（同标题同按钮）；
      // noface/stalled 保留——它带主裁决没有的独有诊断（内嵌原始摄像头画面），不误伤。
      const hs=this.streamHealth().state;
      return this.verdictCoversHealth() && (hs==='lag' || hs==='svc_down');
    },
    alertRank(level){ const r={critical:0,error:1,warn:2,info:3}; return (level in r)?r[level]:9; },
    alertBannerCls(level){
      return (level==='critical'||level==='error') ? 'bg-red-900/70 border-red-600/50 text-red-200'
           : level==='warn' ? 'bg-yellow-900/50 border-yellow-700/40 text-yellow-200'
           : 'bg-blue-900/50 border-blue-700/40 text-blue-200';
    },
    alertBadge(level){ return (level==='critical'||level==='error')?'⚠️ 提醒':(level==='warn'?'⚠️ 注意':'ℹ️ 提示'); },
    _bdPageCoveredKeys:['stream_health','cable_silence','cable_silence_long'],
    hubAlerts(){
      // 页内(开播页)正在实时呈现的健康类后端告警不占主位、只进「更多」；其余按严重度排序取最高做主条。
      // [uivr] 告警条=运行态快照（P11 实锤：CABLE 无声告警入镜 → ui_default 假红 8.26%），
      //   回归模式一律不渲染（与 tour/onboard/staticmouth 同一确定性口径）；
      //   告警条自身的渲染逻辑由 S 系静态契约覆盖（badge/cls/降噪折叠标记）。
      if(this._uivr) return { primary:null, extra:[] };
      const covered=a=> this.tab==='stream' && this._bdPageCoveredKeys.includes(a.key);
      const vis=(this.userAlerts||[]).filter(a=>
        !this.dismissedAlerts.includes(a.key) &&
        !(this.demoMode && (a.level==='warn'||a.level==='info')));
      const by=(x,y)=>this.alertRank(x.level)-this.alertRank(y.level);
      const front=vis.filter(a=>!covered(a)).sort(by);
      const back =vis.filter(a=> covered(a)).sort(by);
      return { primary: front[0]||null, extra: front.slice(1).concat(back) };
    },
    humanizeStepDetail(detail){                  // WS3：仅用户模式调用；原文另存于 title
      let d=(typeof detail==='string'?detail:''); if(!d) return '';
      d=d.replace(/\bpid[=:]?\s*\d+/gi,'')
         .replace(/麦克风\s*\(DroidCam Virtual Audio\)/gi,'手机麦克风')
         .replace(/DroidCam Virtual Audio/gi,'手机麦克风')
         .replace(/CABLE Input\s*\(VB-?Audio Virtual C[^)]*\)/gi,'观众声音通道')
         .replace(/VB-?Audio Virtual Cable/gi,'观众声音通道')
         .replace(/未找到 CABLE\(检查 VB-Cable\)/gi,'未找到观众声音通道（请检查虚拟声卡）')
         .replace(/\s*\((?:MME|WASAPI|DirectSound|WDM-?KS)\)/gi,'')
         .replace(/OBS(?:-Camera)?\s*虚拟摄像头/gi,'直播画面通道')
         .replace(/数字人广播中枢/g,'数字人画面引擎')
         .replace(/广播中枢/g,'画面引擎')
         .replace(/\bvcam\b/gi,'数字人画面引擎')
         .replace(/\bOBS\b/gi,'直播画面通道')
         .replace(/Unity ?Capture/gi,'第二画面通道')
         .replace(/Ditto\s*数字人|Ditto/gi,'数字人引擎')
         .replace(/\s{2,}/g,' ').replace(/^[·:：、,\s\-]+/,'').trim();
      return d;
    },
    streamDurStr(){                              // [P2/P5 价值条] 在播时长（起点用 tab 无关的 sessStartTs，心跳用 streamClock）
      if(!this.perf.streaming || !this.sessStartTs) return '';
      const base=this.streamClock||Date.now();
      return this.fmtDur(Math.max(0,Math.floor((base-this.sessStartTs)/1000)));
    },

    // P2 开播前预检：把攒齐的可信信号从「直播中被动报警」升级为「开播前主动拦截」。
    //   六项逐条给 ok/warn/fail + 一步修复(全复用 verdictCta)。fail=必须处理(否则黑屏/开不了)，warn=建议处理(可硬开)。
    //   仅未开播时用。目标：上播之前就把黑屏/没声/没角色挡住，而不是播出去才被观众发现。
    preflight(){
      const svc=this.services||{}, items=[];
      // 模式感知：真人换脸(需换脸服务+摄像头) vs 数字人(需口型/广播，不需摄像头)。避免用错模式的假阻塞拦住开播。
      const _mode=this.broadcastMode||(this.broadcast&&this.broadcast.mode)||'real_faceswap';
      const isFace=(_mode==='real_faceswap');
      // 1) 出镜角色（one_click_start 硬性前置，两种模式都要）
      items.push(this.streamReady()
        ? {key:'profile', icon:'🎭', label:'出镜角色', status:'ok',   detail:this.active||'已选', ctaKey:'', ctaLabel:''}
        : {key:'profile', icon:'🎭', label:'出镜角色', status:'fail', detail:'未选出镜角色，无法开播', ctaKey:'profiles', ctaLabel:'去角色库'});
      // 1.5) 素材链路体检（AS10/AS11）：出镜角色有真断链（模型/声音/形象视频文件被删）→ 黄灯提醒
      //   （声音退化、形象回退单图，都不黑屏，不拦）。「无备份」是导入包角色的正常态，
      //   不上预检（与资产横幅同口径防狼来了），只在角色抽屉里给建议。
      const vhA=this.active && this.voiceHealth[this.active];
      const vhBroken=vhA?vhA.issues.filter(i=>i.sev==='break'):[];
      if(vhBroken.length){
        items.push({key:'voicelink', icon:'🩹', label:'素材链路',
                    status:'warn',
                    detail:'「'+this.active+'」'+vhBroken[0].text+(vhBroken.length>1?('；等 '+vhBroken.length+' 项，见角色卡'):''),
                    ctaKey:'profiles', ctaLabel:'去修复'});
      }
      // 2) 主引擎：真人换脸=换脸服务(离线=黑屏,fail)；数字人=口型引擎(离线仅降级为静态口型,warn 不拦)
      if(isFace){
        const fs=svc.faceswap;
        items.push(fs===true
          ? {key:'faceswap', icon:'🔮', label:'换脸服务', status:'ok',   detail:'在线', ctaKey:'', ctaLabel:''}
          : (fs===false
              ? {key:'faceswap', icon:'🔮', label:'换脸服务', status:'fail', detail:'离线，开播会黑屏。去设置检查换脸地址', ctaKey:'goSettings', ctaLabel:'去设置'}
              : {key:'faceswap', icon:'🔮', label:'换脸服务', status:'warn', detail:'状态未知，正在检测…', ctaKey:'goSettings', ctaLabel:'去设置'}));
      } else {
        const ls=svc.lipsync;
        items.push(ls===true
          ? {key:'engine', icon:'🤖', label:'数字人口型', status:'ok',   detail:'口型引擎在线', ctaKey:'', ctaLabel:''}
          : {key:'engine', icon:'🤖', label:'数字人口型', status:'warn', detail:'口型引擎离线：将用静态口型（不影响出画/声音）', ctaKey:'goSettings', ctaLabel:'去设置'});
      }
      // 3) 视频源：仅真人换脸需要摄像头；数字人由 AI 形象出画，无需摄像头
      if(isFace){
        const camN=(this.cameras||[]).length, phoneCam=!!(this.phoneRelayCam&&this.phoneRelayCam.live);
        items.push((camN>0||phoneCam)
          ? {key:'video', icon:'📹', label:'视频源', status:'ok',   detail:phoneCam?'手机摄像头已连':(camN+' 个摄像头可用'), ctaKey:'', ctaLabel:''}
          : {key:'video', icon:'📹', label:'视频源', status:'warn', detail:'没检测到摄像头——可刷新、用手机出镜，或改用「AI 数字人」(无需摄像头)', ctaKey:'refreshDev', ctaLabel:'⟳ 刷新'});
      } else {
        items.push({key:'video', icon:'📹', label:'视频源', status:'ok', detail:'数字人无需摄像头（AI 形象出画）', ctaKey:'', ctaLabel:''});
      }
      // 4) 麦克风输入
      const micN=(this.rvcInputDevices||[]).length;
      items.push(micN>0
        ? {key:'mic', icon:'🎙️', label:'麦克风', status:'ok',   detail:micN+' 个输入设备', ctaKey:'', ctaLabel:''}
        : {key:'mic', icon:'🎙️', label:'麦克风', status:'warn', detail:'没检测到麦克风，观众可能听不到', ctaKey:'refreshDev', ctaLabel:'⟳ 刷新'});
      // 4.5) 麦克风试音（P2-2）：黄灯不拦——只提醒「这只麦还没验证过收音」。7 天内同一只麦的 good 结论视为通过
      if(micN>0){
        const mt=this.micTestJudge();
        items.push(mt.state==='good'
          ? {key:'mictest', icon:'🎧', label:'试音', status:'ok',   detail:'收音正常（'+mt.ago+'试过）', ctaKey:'', ctaLabel:''}
          : (mt.state==='warn'
              ? {key:'mictest', icon:'🎧', label:'试音', status:'warn', detail:'上次试音（'+mt.ago+'）：'+mt.verdict, ctaKey:'micTest', ctaLabel:'🎙 再试一次'}
              : {key:'mictest', icon:'🎧', label:'试音', status:'warn', detail:(mt.state==='stale'?'换了麦克风，还没试过这只':'还没试过音')+'——3 秒确认收音正常', ctaKey:'micTest', ctaLabel:'🎙 试音'}));
      }
      // 5) 虚拟声卡 CABLE（直播声音的必经通路，也是 P1-F 探针的落点）
      const hasCable=(this.rvcOutputDevices||[]).some(d=>/cable/i.test(d));
      items.push(hasCable
        ? {key:'cable', icon:'🔊', label:'虚拟声卡', status:'ok',   detail:'CABLE 通路就绪', ctaKey:'', ctaLabel:''}
        : {key:'cable', icon:'🔊', label:'虚拟声卡', status:'warn', detail:'未检测到 CABLE，声音进不了直播，需装 VB-Cable', ctaKey:'refreshDev', ctaLabel:'⟳ 刷新'});
      // 5.5) 直播声卡回环试听（P3-2）：与试音同款黄灯不拦——CABLE 在≠声音真能进去（被禁用/被独占时
      //   设备照样在枚举里）。2 秒自动回环验证，7 天内同一路 heard=true 视为通过。
      if(hasCable){
        const ot=this.outTestJudge();
        items.push(ot.state==='good'
          ? {key:'outtest', icon:'📢', label:'试听', status:'ok',   detail:'直播声卡收到回环（'+ot.ago+'验证）', ctaKey:'', ctaLabel:''}
          : (ot.state==='warn'
              ? {key:'outtest', icon:'📢', label:'试听', status:'warn', detail:'上次试听（'+ot.ago+'）直播声卡没收到回环——CABLE 可能被禁用/被独占', ctaKey:'outTest', ctaLabel:'🔊 再试一次'}
              : {key:'outtest', icon:'📢', label:'试听', status:'warn', detail:(ot.state==='stale'?'换了直播声卡，这一路还没验证':'还没验证声音能进直播声卡')+'——2 秒自动确认', ctaKey:'outTest', ctaLabel:'🔊 试听'}));
      }
      // 6) 显存
      const P=this.perf||{}, vramPct=(P.gpu_mem_total>0)?Math.round(P.gpu_mem_used/P.gpu_mem_total*100):0;
      items.push(this.vramTight
        ? {key:'vram', icon:'🧠', label:'显存', status:'warn', detail:'吃紧 '+vramPct+'%'+(this.canFreeVram()?'，可一键腾出':''), ctaKey:this.canFreeVram()?'freeVram':'', ctaLabel:'一键腾显存'}
        : {key:'vram', icon:'🧠', label:'显存', status:'ok',   detail:(vramPct||0)+'% 充足', ctaKey:'', ctaLabel:''});
      const fails=items.filter(i=>i.status==='fail').length;
      const warns=items.filter(i=>i.status==='warn').length;
      const oks  =items.filter(i=>i.status==='ok').length;
      return {items, oks, warns, fails, total:items.length, canBroadcast:fails===0, allReady:fails===0&&warns===0};
    },

    // P2.1 开播按钮·预检联动：预检清单的最后一道闸。
    //   仅「全新开播」拦截 fail 级阻塞(换脸离线/没选角色=开播必黑屏)——弹确认，可逐项修或强行开。
    //   「重新开播」是恢复动作(掉线重连管线)，永不拦；warn 级可硬开、也不拦。fail 只在「明确离线」时成立，绝不误拦。
    //   [Phase E 附加] 叠加 D-1 设备体检 quick 卡点：总分红灯(grade=red)时把 bad 项并入拦截弹窗
    //   (「红灯先修再播」)。quick 免录音 <1s；探测失败/超时(1.5s) = 不拦不等，绝不因体检挂掉卡开播。
    async guardedStart(){
      if(this.perf.streaming){
        return this.clickRestart();
      }
      const blockers=this.preflight().items.filter(i=>i.status==='fail');
        try{
          const ctrl=new AbortController(); const tid=setTimeout(()=>ctrl.abort(),1500);
          const r=await fetch(HUB+'/api/device/checkup?quick=1&src=gate',{signal:ctrl.signal}).then(r=>r.json());   // src=gate: 后端留痕,联查误拦率
          clearTimeout(tid);
          if(r && r.ok && r.grade==='red'){
            for(const it of (r.items||[])){
              if(!it.measured || it.level!=='bad') continue;
              blockers.push({key:'dev_'+it.key, icon:'🎛', label:it.label, status:'fail',
                detail:(it.detail||'')+(it.advice?('。'+it.advice):''),
                ctaKey:(it.key==='cable'?'goSettings':'refreshDev'),
                ctaLabel:(it.key==='cable'?'去装向导':'⟳ 刷新')});
            }
          }
        }catch(e){ /* 体检不可达=放行，开播不背体检的锅 */ }
        if(blockers.length){ this.preflightGate={show:true, blockers}; return; }
      this.oneClickStart();
    },
    proceedStartAnyway(){ this.preflightGate.show=false; this.oneClickStart(); },   // 强行开播(用户确有把握/预检误判时的逃生门)
    gateFix(it){ this.preflightGate.show=false; if(it&&it.ctaKey) this.verdictCta(it.ctaKey); },   // 关弹窗→跳去修复该阻塞项

    // ══════════════════════════════════════════════════════════════════════
    // [开播页 S1-S3] 就绪度收口 + 页面相位 + 修复动作原地反馈（纯前端呈现层，数据仍取 preflight() 单一真相）
    // ══════════════════════════════════════════════════════════════════════
    // S1 页面相位状态机：同屏只讲一件事——setup(缺角色/方式)/ready(可开播)/starting(启动中)/live(直播中)/ended(刚停播)
    streamPhase(){
      if(this.streaming) return 'starting';
      if(this.perf.streaming) return 'live';
      if(this.lastSession) return 'ended';
      return (this.streamReady() && this.broadcastMode) ? 'ready' : 'setup';
    },
    // ── S5 启动仪式：里程碑面板（前端演绎启动流程；真实逐步结果由返回后的 streamSteps 呈现）──
    startMilestones(){
      return this.broadcastMode==='avatar_lipsync'
        ? ['检查服务','激活角色','锁定画面通道','启动数字人画面','配置声音']
        : ['检查服务','激活角色','锁定画面通道','摄像头预检','启动视频','配置声音'];
    },
    startingElapsed(){
      if(!this.startingSince) return 0;
      void this.streamClock;   // 只作响应式心跳依赖（触发重渲）；取值用 Date.now()，定时器被节流也不冻表
      return Math.max(0, Math.floor((Date.now()-this.startingSince)/1000));
    },
    // 耗时启发式定位「当前里程碑」（仅视觉引导，不谎报完成；累计秒界：1/2/3/5/10）
    startingStageIdx(){
      const e=this.startingElapsed(), cuts=[1,2,3,5,10], n=this.startMilestones().length;
      let i=0; for(const c of cuts){ if(e>=c) i++; }
      return Math.min(i, n-1);
    },
    // [观察自动化] 停播后取后端聚合的场次换脸质量报告（hub 健康守护每 ~4s 采样 /swap/status，
    // 回 idle 才落盘 → 报告比停播晚 1~2 个采样周期），延时+重试后合入成绩单：裁剪命中/时延/降档/精修。
    async fetchSwapRecap(){
      const endedTs=this.lastSession?.endedTs||Date.now();
      for(const wait of [5000, 9000]){
        await new Promise(r=>setTimeout(r,wait));
        if(!this.lastSession || this.lastSession.endedTs!==endedTs) return;   // 成绩单已关/新场已开
        try{
          // P4: limit 2→6，多出的历史场喂成绩单分享卡「近场趋势」小图（账本本就落盘，读几条零成本）
          const d=await fetch(HUB+'/realtime/swap/sessions?limit=6').then(r=>r.json());
          const rep=(d.sessions||[])[0];
          // 只认「刚结束的这场」：报告收场时间须落在本次停播前后 2 分钟内(防拿到历史场)
          if(rep && Math.abs(rep.end*1000-endedTs)<120000){
            this.lastSession.swap=rep;
            // 上一场(须早于本场开播,防重复配对) → 成绩单出「对比上场」趋势行
            const prev=(d.sessions||[])[1];
            if(prev && prev.end<=rep.start) this.lastSession.swapPrev=prev;
            this.lastSession.swapHist=(d.sessions||[]).slice(0,6);
            return;
          }
        }catch(e){}
      }
    },
    // P5-2 成绩单·设备热切：停播后拉健康时间线，按本场时间窗过滤 event=device 的账本（次数/失败数/最近明细）。
    //   用时间线而非 health_sessions：前端知道精确开停播沿，且能带出每次「从哪只切到哪只·耗时·来源」的明细行。
    async fetchHotSwitchRecap(){
      const s=this.lastSession; if(!s) return;
      const endedTs=s.endedTs, startTs=this.sessStartTs||0;
      try{
        const d=await fetch(HUB+'/realtime/health_timeline?limit=80').then(r=>r.json());   // 80=服务端内存环上限
        if(!d || !d.ok) return;
        if(!this.lastSession || this.lastSession.endedTs!==endedTs) return;   // 成绩单已关/新场已开
        const evs=(d.timeline||[]).filter(e=>e.event==='device'
          && e.ts*1000>=startTs-5000 && e.ts*1000<=endedTs+10000);   // 停播沿放宽10s：切换在停播瞬间完成也算本场
        if(evs.length) this.lastSession.hotSwitch={ n:evs.length,
          fail:evs.filter(e=>String(e.label||'').indexOf('失败')>=0).length,
          items:evs.slice(-3).map(e=>({t:this.fmtClock(e.ts), label:String(e.label||'').slice(0,90)})) };
      }catch(_){}
    },
    // [观察自动化] 对比上场：同指标 上场→本场（裁剪命中↑好、时延↓好、降档↓好），连续实测直接看趋势
    _swapPair(kind){
      const a=this.lastSession?.swapPrev, b=this.lastSession?.swap;
      if(!a||!b) return null;
      const pick=r=>kind==='crop'?(r.crop&&r.crop.hit_pct):(kind==='lat'?(r.latency_ms&&r.latency_ms.med):(r.degraded_pct||0));
      const va=pick(a), vb=pick(b);
      if(va==null||vb==null) return null;
      return {a:va, b:vb, d:Math.round((vb-va)*10)/10};
    },
    swapDeltaTxt(kind){
      const p=this._swapPair(kind); if(!p) return '';
      const u=kind==='lat'?'ms':'%';
      return p.a+u+'→'+p.b+u+(p.d?('（'+(p.d>0?'+':'')+p.d+u+'）'):'（持平）');
    },
    swapDeltaTone(kind){
      const p=this._swapPair(kind); if(!p||!p.d) return 'text-hub-muted';
      const good=(kind==='crop')?(p.d>0):(p.d<0);
      return good?'text-hub-green':'text-hub-orange';
    },
    // 本场主用精修引擎（占比最高的一个；'无'=全程未开精修）
    swapRecapEnh(){
      const e=this.lastSession?.swap?.enhance; if(!e) return '—';
      const ks=Object.keys(e); if(!ks.length) return '—';
      const k=ks.sort((a,b)=>e[b]-e[a])[0];
      return k==='无'?'未开':k;
    },
    // [P8] 离席时长人话：'6分30秒(占12%)'——离席期观众看的是自动模糊稍等画面
    swapAwayTxt(){
      const sw=this.lastSession?.swap; if(!sw||!sw.away_s) return '';
      const m=Math.floor(sw.away_s/60), s=sw.away_s%60;
      const t=(m?m+'分':'')+(s||!m?s+'秒':'');
      const pct=sw.dur_s?Math.round(sw.away_s*100/sw.dur_s):null;
      return t+(pct!=null?('（占'+pct+'%）'):'');
    },
    // [P7 五期] 主观两问速记：真人观察仅剩的两件主观项(贴缝/精修观感)点选即写进场次账本，
    //   与量化指标同一本账。再点同值=撤销；改选=覆盖。失败只提示，不打断成绩单。
    humanLabel(v){ return v==='good'?'好':(v==='ok'?'一般':(v==='bad'?'差':'')); },
    async _swapRatePost(patch){
      const sw=this.lastSession?.swap; if(!sw||sw.start==null) return;
      try{
        const d=await fetch(HUB+'/realtime/swap/sessions/rate',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify(Object.assign({start:sw.start},patch))}).then(r=>r.json());
        if(d.ok&&d.session){ this.lastSession.swap=Object.assign({},sw,{human:d.session.human}); }
        else this.showToast('主观速记保存失败: '+(d.detail||'未知'),'error');
      }catch(e){ this.showToast('主观速记保存失败: '+e,'error'); }
    },
    rateSwapRecap(kind,val){
      const cur=(this.lastSession?.swap?.human||{})[kind]||'';
      this._swapRatePost({[kind]:(cur===val)?'':val});
    },
    noteSwapRecap(ev){ this._swapRatePost({note:(ev.target.value||'').trim()}); },
    // ── S5 成绩单：稳定度着色 + 一句话评语 ──
    stabilityTone(p){ return p==null?'text-hub-muted':(p>=97?'text-hub-green':(p>=85?'text-hub-orange':'text-hub-red')); },
    recapRemark(){
      const s=this.lastSession; if(!s) return '';
      if(s.stabilityPct==null) return '';
      if(s.stabilityPct>=97) return '全程平稳，状态很好';
      if(s.stabilityPct>=85) return '偶有波动（丢脸/卡顿），整体可用';
      return '波动较多——建议看「场次复盘」定位原因（光线/摄像头/显存）';
    },
    // ── S6 新手三步引导：步骤随真实状态自动打勾（不用用户手动点"下一步"），首播成功即永久退场 ──
    //   与既有引导不重复：showTour/onboardShow 只覆盖「角色库→对话」链路，开播链路此前无任何首次引导。
    guideSteps(){
      return [
        {n:1, label:'启用出镜角色', done:this.streamReady(),   act:'profiles', hint:'去「角色库」给要出镜的角色点 ▶ 启用'},
        {n:2, label:'选开播方式',   done:!!this.broadcastMode, act:'',         hint:'真人换脸 或 AI 数字人，就在下方二选一'},
        {n:3, label:'一键开播',     done:false,                act:'start',    hint:'就绪度变绿后，点绿色大按钮'},
      ];
    },
    guideVisible(){
      const ph=this.streamPhase();
      return !this.streamGuideDone && (ph==='setup'||ph==='ready');
    },
    guideCurrentIdx(){ const s=this.guideSteps(); const i=s.findIndex(x=>!x.done); return i<0?s.length-1:i; },
    guideAct(step){
      if(step.act==='profiles' && !step.done) this.goTab('profiles');
      else if(step.act==='start' && this.guideCurrentIdx()===2) this.guardedStart();
    },
    guideDismiss(){
      this.streamGuideDone=true;
      try{ localStorage.setItem('hub_stream_guide_done','1'); }catch(_){}
    },
    // S1 就绪度一句话结论（环形进度旁的主文案）
    readyLine(){
      const pf=this.preflight();
      if(pf.fails>0) return {tone:'red',    text:'还差 '+pf.fails+' 项才能开播'};
      if(pf.warns>0) return {tone:'orange', text:'可以开播 · '+pf.warns+' 项建议处理'};
      return {tone:'green', text:'一切就绪，可以开播'};
    },
    // S1 就绪度副文案：优先说「第一个问题是什么」，全绿时给视频/音频去向预告
    readySub(){
      const pf=this.preflight();
      const bad=pf.items.find(i=>i.status==='fail')||pf.items.find(i=>i.status==='warn');
      if(bad) return bad.label+'：'+bad.detail;
      return '视频：'+this.videoLive().label+' · 音频：'+this.audioLive().label;
    },
    // S2 开播按钮禁用/受限原因外显（不再藏在悬浮 title 里）；null=无需提示
    startDisabledReason(){
      if(this.streaming) return null;
      if(this.wireless.running) return {icon:'📱', tone:'blue', text:'「手机无线开播」准备中——等手机就绪后自动开播', cta:'', key:''};
      if(!this.streamReady())   return {icon:'🎭', tone:'red',  text:'还不能开播：未选出镜角色', cta:'去角色库启用', key:'profiles'};
      const pf=this.preflight();
      // fail 时清单已强制展开在上方，CTA 直接给「修复全部」（有可自动修复项时）
      if(pf.fails>0)            return {icon:'⛔', tone:'red',  text:'有 '+pf.fails+' 项必须处理（现在点开播会先弹确认）——逐项修复见上方清单',
                                        cta:this.fixableKeys().length?(this.fixAllBusy?'修复中…':'🛠 修复全部'):'', key:'fixAll'};
      if(!this.broadcastMode)   return {icon:'🎬', tone:'orange', text:'先在上方选一种开播方式（真人换脸 / AI 数字人）', cta:'', key:''};
      if(pf.warns>0)            return {icon:'💡', tone:'orange', text:pf.warns+' 项建议处理（不修也能开播，见上方就绪度清单）', cta:'', key:''};
      return null;
    },
    reasonCta(key){
      if(key==='profiles') this.goTab('profiles');
      else if(key==='fixAll'){ if(!this.fixAllBusy) this.fixAll(); }
      else if(key==='expand'){ this.readyOpen=true; }
    },
    // S3 修复反馈写入 + 自动消隐（8s 后清除，避免过期结论常驻）
    _setFix(key, v, holdMs=8000){
      this.fixState[key]=v;
      if(this._fixTimers[key]){ clearTimeout(this._fixTimers[key]); this._fixTimers[key]=null; }
      if(!v.busy && holdMs>0) this._fixTimers[key]=setTimeout(()=>{ this.fixState[key]=null; }, holdMs);
    },
    // S3 按项生成扫描结论（对比扫描前后设备数，给「变化感」）
    _fixNoteFor(key, before){
      if(key==='mic'){
        const n=(this.rvcInputDevices||[]).length;
        return n>0 ? {ok:true,  note:'✓ 检测到 '+n+' 个麦克风'+(n>before.mic?('（新增 '+(n-before.mic)+' 个）'):'')}
                   : {ok:false, note:'仍未检测到麦克风——检查 USB/蓝牙连接，或改用手机当麦克风（下方手机向导）'};
      }
      if(key==='cable'){
        const has=(this.rvcOutputDevices||[]).some(d=>/cable/i.test(d));
        return has ? {ok:true,  note:'✓ 找到虚拟声卡 CABLE，直播声音通路就绪'}
                   : {ok:false, note:'仍未找到 CABLE——点「安装向导」两分钟装好（装过则去 Windows 声音设置确认未被禁用）'};
      }
      if(key==='video'){
        const n=(this.cameras||[]).length, ph=!!(this.phoneRelayCam&&this.phoneRelayCam.live);
        return (n>0||ph) ? {ok:true,  note:'✓ '+(ph?'手机摄像头在线':('检测到 '+n+' 个摄像头'))+(n>before.cam?('（新增 '+(n-before.cam)+' 个）'):'')}
                         : {ok:false, note:'仍没有可用摄像头——可展开「手机无线开播」用手机出镜，或点「改用数字人」(无需摄像头)'};
      }
      return {ok:true, note:'✓ 已刷新'};
    },
    // S3 重新扫描（单项或多项共享一次设备刷新，原地给结论）
    async fixScan(keyOrKeys){
      const keys=Array.isArray(keyOrKeys)?keyOrKeys:[keyOrKeys];
      if(keys.some(k=>this.fixState[k]&&this.fixState[k].busy)) return;
      keys.forEach(k=>this._setFix(k,{busy:true,note:'正在重新扫描设备…',ok:null},0));
      const before={mic:(this.rvcInputDevices||[]).length, out:(this.rvcOutputDevices||[]).length, cam:(this.cameras||[]).length};
      try{ await Promise.allSettled([this.rvcRefreshDevices(), this.loadCameras(), this.loadPhoneRelayCam()]); }catch(_){}
      keys.forEach(k=>this._setFix(k, Object.assign({busy:false}, this._fixNoteFor(k, before))));
    },
    // S3 当前可自动修复的项（扫描类 + 腾显存）；角色/换脸服务需人工跳转，不算
    fixableKeys(){
      return this.preflight().items.filter(i=>i.status!=='ok' && ['video','mic','cable','vram'].includes(i.key)).map(i=>i.key);
    },
    // S3 一键修复全部：一次设备刷新覆盖所有扫描项 + 显存吃紧则顺手腾出；结束给整体结论
    async fixAll(){
      if(this.fixAllBusy) return;
      const keys=this.fixableKeys();
      if(!keys.length){ this.showToast('没有可自动修复的项','info'); return; }
      this.fixAllBusy=true;
      try{
        const scan=keys.filter(k=>k!=='vram');
        if(scan.length) await this.fixScan(scan);
        if(keys.includes('vram') && this.canFreeVram()) await this.freeVram();
        const pf=this.preflight();
        const leftF=pf.items.filter(i=>i.status==='fail').length;
        if(leftF>0){ this.readyOpen=true; this.showToast('自动修复完成，仍有 '+leftF+' 项需要手动处理（清单已展开）','info'); }
        else this.showToast(pf.warns>0?('自动修复完成 ✓ 剩余 '+pf.warns+' 项为建议项，可直接开播'):'自动修复完成 ✓ 全部就绪','success');
      } finally { this.fixAllBusy=false; }
    },
    // S3 就绪度行内·摄像头选择：选定即认定「用本机摄像头」意图；直播中还会热切换
    pickReadyCam(){
      if(this.selectedCamera>=0){
        this.videoSource='camera';
        if(this.perf.streaming) this.setCamera(this.selectedCamera);
        else{ const c=(this.cameras||[]).find(x=>x.index===this.selectedCamera);
              this.showToast('已指定摄像头：'+(c?c.label:('#'+this.selectedCamera))+'（开播时生效）','info'); }
      } else this.videoSource='auto';
    },
    // S3 VB-Cable 向导·装完重检
    async cableRecheck(){
      if(this.cableWiz.busy) return;
      this.cableWiz.busy=true; this.cableWiz.note='';
      try{
        await this.fixScan('cable');
        const f=this.fixState['cable']||{};
        this.cableWiz.found=!!f.ok; this.cableWiz.note=f.note||'';
        if(f.ok) this.showToast('虚拟声卡已就绪 ✓','success');
      } finally { this.cableWiz.busy=false; }
    },
    // S2 英雄区直播缩略图 → 点开「实时画面」大图
    openLivePreview(){
      this.mjpegOn=true;
      this.$nextTick(()=>{ try{ const el=document.getElementById('liveViewCard'); if(el) el.scrollIntoView({behavior:'smooth',block:'start'}); }catch(_){} });
    },

    // P5 一键「开播自检报告」：把预检六项 + 四路信号(视频/麦克风/CABLE馈线/变声服务) + 服务/显存/设备/自愈状态
    //   汇成一段可复制的诊断文本，便于远程排障(客服/自查)。纯读现有前端状态 + 尽力刷新一次，无副作用、失败兜底。
    async openSelfCheck(){
      this.selfCheck.show=true; this.selfCheck.loading=true; this.selfCheck.text='';
      try{ await Promise.allSettled([this.refreshServices(), this.signalTick(), this.rvcRefreshDevices()]); }catch(_){}
      // 优先取服务端故障包(含守护/自愈/日志尾/版本)；老后端无此端点→回退旧健康时间线，仍出报告(仅缺服务端段)
      let be=null;
      try{ be=await fetch(HUB+'/realtime/selfcheck?logs=8').then(r=>r.json()); }catch(_){}
      if(be && !be.ok) be=null;
      if(!be){ try{ const tl=await fetch(HUB+'/realtime/health_timeline?limit=8').then(r=>r.json());
        if(tl&&tl.ok) be={_tlOnly:true, timeline:tl.timeline, heal:{stats:tl.stats}}; }catch(_){} }
      try{ this.selfCheck.text=this.buildSelfCheckText(be); }
      catch(e){ this.selfCheck.text='生成自检报告出错：'+String(e); }
      this.selfCheck.loading=false;
    },
    async copySelfCheck(){
      const t=this.selfCheck.text||'';
      try{ await navigator.clipboard.writeText(t); this.showToast('已复制自检报告，可粘贴发给技术支持','success'); }
      catch(_){ try{ const ta=document.createElement('textarea'); ta.value=t; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta); this.showToast('已复制自检报告','success'); }
        catch(e){ this.showToast('复制失败，请手动选中文本复制','error'); } }
    },
    // [P4 场次小结·分享] 复制本场小结为可粘贴文本（创作者晒成果/发社群）；剪贴板+兜底范式与 copySelfCheck 一致
    async copyRecap(){
      const s=this.lastSession; if(!s) return;
      const line='时长 '+this.fmtDur(s.durSec)+(s.peakFps>0?(' · 峰值 '+s.peakFps+'fps'):'')
        +(s.stabilityPct!=null?(' · 稳定度 '+s.stabilityPct+'%'):'')+(s.usedRvc?' · 变声':'');
      // [观察自动化] 后端场次报告已到 → 量化行一并入剪贴板(实测记录直接可交)
      // P3 单源：数值取 swapCropVal/swapLatVal/swapDegVal，与分享卡 recapCardStats 同一出口
      const sw=s.swap;
      const line2=sw?('\n裁剪命中 '+this.swapCropVal()
        +' · 换脸时延中位 '+this.swapLatVal()
        +' · 自动降档 '+this.swapDegVal()
        +' · 精修 '+this.swapRecapEnh()
        +(sw.away_s?('\n离席 '+this.swapAwayTxt()+' · 有效失败率 '
          +(sw.swap&&sw.swap.fail_pct_active!=null?sw.swap.fail_pct_active+'%':'—')
          +'（含离席 '+(sw.swap&&sw.swap.fail_pct!=null?sw.swap.fail_pct+'%':'—')+'）'):'')):'';
      // 有上场对比 → 趋势行一并入剪贴板(连续实测的记录直接可交)
      const line3=(sw&&s.swapPrev)?('\n对比上场: 裁剪 '+this.swapDeltaTxt('crop')
        +' · 时延 '+this.swapDeltaTxt('lat')+' · 降档 '+this.swapDeltaTxt('deg')):'';
      // P5-2 本场有设备热切 → 一并入剪贴板（拔插自愈也是「这场怎么样」的一部分）
      const hs=s.hotSwitch;
      const lineHs=hs?('\n设备热切 '+hs.n+' 次'+((hs.fail||0)>0?('（失败 '+hs.fail+'）'):'（全部成功）')):'';
      // 主观速记已填 → 一并入剪贴板(量化+主观=完整观察记录)
      const hm=sw&&sw.human;
      const line4=(hm&&(hm.seam||hm.enhance||hm.note))?('\n主观: 贴缝 '+(this.humanLabel(hm.seam)||'—')
        +' · 精修观感 '+(this.humanLabel(hm.enhance)||'—')+(hm.note?(' · '+hm.note):'')):'';
      const txt='📺 本场直播成绩单\n'+line+line2+line3+lineHs+line4+'\n🔒 全程本地运行（画面/声音不经第三方）';
      try{ await navigator.clipboard.writeText(txt); this.showToast('本场小结已复制，可粘贴分享','success'); }
      catch(_){ try{ const ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta); ta.select();
        document.execCommand('copy'); document.body.removeChild(ta); this.showToast('本场小结已复制','success'); }
        catch(e){ this.showToast('复制失败，请手动选择复制','error'); } }
    },
    buildSelfCheckText(be){
      const P=this.perf||{}, sig=this.signal||{}, svc=this.services||{}, pad=n=>String(n).padStart(2,'0');
      const d=new Date();
      const ts=d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
      const modeMap={real_faceswap:'真人换脸', ditto:'数字人(Ditto)', lipsync:'口型', latentsync:'口型(LatentSync)', echomimic:'数字人(EchoMimic)'};
      const mode=(this.broadcast&&this.broadcast.mode)||'';
      const L=[];
      L.push('===== 开播自检报告 =====');
      L.push('时间: '+ts);
      L.push('模式: '+(modeMap[mode]||mode||'—')+' · 角色: '+(this.active||(this.activeProfileObj&&this.activeProfileObj.name)||'未选'));
      let sline='直播状态: ';
      if(P.streaming){ const secs=this.streamingSinceTs?Math.floor((Date.now()-this.streamingSinceTs)/1000):0;
        sline+='直播中'+(secs?('（已 '+this.fmtDur(secs)+'）'):''); } else sline+='未开播';
      L.push(sline);
      try{ const v=this.linkVerdict(); if(v) L.push('总体裁决: '+(v.icon||'')+' '+v.title+(v.detail?(' — '+v.detail):'')); }catch(_){}
      L.push('');
      // 预检六项
      let pf={items:[]}; try{ pf=this.preflight()||pf; }catch(_){}
      L.push('【开播预检】'+(pf.oks||0)+'/'+(pf.total||0)+' 就绪'+(pf.fails?(' · '+pf.fails+' 必须处理'):'')+(pf.warns?(' · '+pf.warns+' 建议处理'):''));
      (pf.items||[]).forEach(it=>{ const m=it.status==='ok'?'[OK]':(it.status==='fail'?'[必须]':'[建议]');
        L.push(' '+m+' '+it.label+': '+(it.detail||'')); });
      L.push('');
      // 四路信号
      L.push('【四路信号】'+(P.streaming?'':'(未开播，信号多为空)'));
      let hs={}; try{ hs=this.streamHealth()||{}; }catch(_){}
      L.push(' 视频: '+(sig.video&&sig.video.fps?(sig.video.fps+'fps'):'—')+' · '+(hs.label||hs.state||'—'));
      let a={}; try{ a=this.audioLive()||{}; }catch(_){}
      L.push(' 麦克风/监听: '+(a.label||'—')+(a.monActive?(' · 电平'+a.monLevel+'%'+(a.talking?'(有声)':'')):''));
      const c=sig.cable||{}; let silent=0; try{ silent=this.cableSilentSecs(); }catch(_){}
      L.push(' 直播馈线(CABLE→OBS): '+(c.ok?('有信号 rms='+(Math.round((c.rms||0)*1000)/1000)+(silent?(' · 静音'+silent+'s'):'')):(P.streaming?'未检测到直播声(探针不可信/无声)':'—'))+(c.dev?(' · '+c.dev):''));
      let snd=null; try{ snd=this.soundDiag(); }catch(_){}
      if(snd) L.push('   ⚠ 声音诊断: '+snd.title+' — '+(snd.detail||''));
      let rvcLive=false; try{ rvcLive=this.rvcLive(); }catch(_){}
      L.push(' 变声服务(6242): '+(svc.rvc===true?'在线':(svc.rvc===false?'离线':'未知'))+(rvcLive?' · 变声中':''));
      L.push('');
      // 服务在线/离线
      const keymap={faceswap:'换脸', rvc:'变声', tts:'TTS', fish_tts:'FishTTS', emotion_tts:'情感TTS', qwen3_tts:'Qwen3TTS', hair:'头发', enhance:'增强', lipsync:'口型', latentsync:'LatentSync', echomimic:'EchoMimic', ditto:'Ditto', singing:'唱歌', stt:'STT', nemo_stt:'NemoSTT'};
      const up=[], down=[];
      Object.keys(keymap).forEach(k=>{ if(svc[k]===true) up.push(keymap[k]); else if(svc[k]===false) down.push(keymap[k]); });
      L.push('【服务】在线: '+(up.join(' ')||'—'));
      if(down.length) L.push('       离线: '+down.join(' '));
      L.push('');
      // 显存/内存
      const vramPct=(P.gpu_mem_total>0)?Math.round(P.gpu_mem_used/P.gpu_mem_total*100):0;
      const gb=mb=>Math.round(mb/1024*10)/10;
      const pl=(this.healthPressure&&this.healthPressure.level)||'';
      L.push('【显存/内存】GPU '+(P.gpu_mem_total>0?(gb(P.gpu_mem_used)+'/'+gb(P.gpu_mem_total)+'GB('+vramPct+'%)'):'—')+' · RAM '+(P.ram_percent>=0?(P.ram_percent+'%'):'—')+(pl?(' · 压力'+pl):''));
      L.push('');
      // 设备
      L.push('【设备】变声输入: '+((this.rvc&&this.rvc.inputDevice)||this.audioInput||'未选')+' · 输出: '+((this.rvc&&this.rvc.outputDevice)||this.audioOutput||'未选'));
      L.push('       视频源: '+((this.lastVideo&&this.lastVideo.label)||this.videoSource||'—'));
      L.push('');
      // 自愈：前端开关 + 累计统计 + 本场服务端计数 + 本页近期轨迹
      const H=(be&&be.heal)||{};
      L.push('【自愈】本端自愈: '+(this.autoHeal?'开':'关')+' · 服务端自愈(无人值守): '+((typeof H.autoheal==='boolean')?(H.autoheal?'开':'关'):(this.autoHealServer?'开':'关')));
      const stats=H.stats||this.healthStats||null;
      if(stats) L.push('       累计: 自动换源'+(stats.auto_restart||0)+' · 变声拉起'+(stats.auto_rvc||0)+' · 腾显存'+(stats.auto_free_vram||0)+' · 告警'+(stats.alert||0));
      if(typeof H.rvc_relaunch_count==='number') L.push('       本场服务端: 变声拉起'+H.rvc_relaunch_count+' · 视频换源'+(H.video_restart_count||0));
      const AG=H.audio_guard; if(AG) L.push('       音频守护: 静音告警≥'+AG.silence_warn_s+'s(再+'+AG.silence_crit_extra_s+'s升级@人) · 变声掉线宽限'+AG.rvc_grace_s+'s/场上限'+AG.rvc_max+'次');
      if(this.healLog&&this.healLog.length) L.push('       近期(本页): '+this.healLog.slice(0,3).map(x=>x.msg).join(' | '));
      // 近期健康事件
      const evs=(be&&be.timeline)||[];
      if(evs.length){ L.push(''); L.push('【近期健康事件】');
        evs.slice(-6).forEach(e=>{ L.push(' '+this.fmtClock(e.ts)+' '+(e.label||((e.from||'-')+'→'+(e.to||'-')))+(e.dwell_s?(' ('+e.dwell_s+'s)'):'')); }); }
      // 服务端诊断(仅新后端 /realtime/selfcheck 提供)：运行时/守护异常/变声进程/日志尾
      if(be && !be._tlOnly){
        L.push(''); L.push('【服务端诊断】');
        const b=be.build||{};
        if(b.uptime_s!=null) L.push(' 运行: 已跑'+this.fmtDur(b.uptime_s)+(b.python?(' · py'+b.python):'')+(b.git?(' · '+b.git):'')+(b.port?(' · 端口'+b.port):''));
        const al=be.alerts_active||[];
        if(al.length) L.push(' 🚨 正在告警: '+al.map(x=>x.title+(x.since?('（'+x.since+'起）'):'')).join(' · '));
        const sup=be.supervisor||{};
        const flap=Object.keys(sup).filter(k=>{const s=sup[k]||{}; return (s.restarts>0)||s.tripped||s.offloaded;});
        if(flap.length) L.push(' 守护异常: '+flap.map(k=>{const s=sup[k]||{}; return k+'('+[s.restarts?('重启'+s.restarts):'', s.tripped?'熔断':'', s.offloaded?'卸载':''].filter(Boolean).join('/')+')';}).join('  '));
        else if(Object.keys(sup).length) L.push(' 守护: 全部平稳(无重启/熔断)');
        if(be.audio) L.push(' 变声进程(6242): '+(be.audio.rvc_api_alive?'存活':'未存活')+(be.audio.broadcast_dev?(' · 直播输出='+be.audio.broadcast_dev):''));
        const rl=be.recent_log||[];
        if(rl.length){ L.push(' 近期日志(WARN/ERROR):'); rl.slice(-6).forEach(x=>L.push('  '+String(x).slice(0,200))); }
        const ce=be.client_errors||[];
        if(ce.length){ L.push(' 前端上报错误:'); ce.slice(-3).forEach(x=>{ let s=String(x);
          try{ const o=JSON.parse(x); s=(o.msg||'')+(o.kind?(' ['+o.kind+']'):'')+(o.page?(' @'+o.page):''); }catch(_){}
          L.push('  '+s.slice(0,200)); }); }
      }
      L.push('');
      L.push('===== 报告结束 · 复制此文本发给技术支持 =====');
      return L.join('\n');
    },

    // 复盘时间线（P3-C③）：按需拉取服务端守护的近期事件，新事件在上；fmtClock 把 unix 秒格式化为时:分:秒
    fmtClock(ts){ try{ const d=new Date((ts||0)*1000), p=n=>String(n).padStart(2,'0');
      return p(d.getHours())+':'+p(d.getMinutes())+':'+p(d.getSeconds()); }catch(_){ return ''; } },
    async loadHealthTimeline(){
      if(this.healthTLLoading) return;
      this.healthTLLoading=true;
      try{
        const [d,sess]=await Promise.all([
          fetch(HUB+'/realtime/health_timeline?limit=60').then(r=>r.json()),
          fetch(HUB+'/realtime/health_sessions?limit=8').then(r=>r.json()).catch(()=>null)
        ]);
        if(d && d.ok){
          this.healthTimeline=(d.timeline||[]).slice().reverse();
          this.healthStats=d.stats||null;
          this.healthAutohealOn=!!d.autoheal_on;
        }
        if(sess && sess.ok) this.healthSessions=sess.sessions||[];
      }catch(_){}
      finally{ this.healthTLLoading=false; }
    },
    // 复盘渲染小工具：时长格式化 / 场次摘要一行 / 事件图标（告警🚨·重启🔄·腾显存🧹·其它自愈🔧）
    fmtDur(s){ s=Math.max(0,Math.floor(s||0)); const h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
      return h?(h+'h'+m+'m'):(m?(m+'m'+ss+'s'):(ss+'s')); },
    sessTitle(x){ const p=[];
      if(x.noface)p.push('丢脸'+x.noface); if(x.lag)p.push('卡顿'+x.lag); if(x.svc_down)p.push('掉线'+x.svc_down);
      if(x.restart)p.push('自动重启'+x.restart); if(x.free_vram)p.push('腾显存'+x.free_vram);
      if(x.hot_switch)p.push('设备热切'+x.hot_switch);   // P5-2 场次摘要同步带上热切次数
      if(x.dfm_switch)p.push('换脸热切'+x.dfm_switch);   // S5 场内换脸角色热切次数
      if(x.alert)p.push('告警'+x.alert); if(x.recovered)p.push('恢复'+x.recovered);
      return p.length?p.join(' · '):'全程平稳'; },
    healEventIcon(e){ if(!e) return ''; if(e.event==='alert') return '🚨';
      if(e.event==='device') return '🎚';   // P5-2 设备热切：与 ops 甘特同一图标语言
      if(e.event==='dfm') return '🎭';      // S5 换脸角色热切：切到谁/载入耗时/冷热（复盘切换卡顿归因）
      if(e.event==='auto_heal'){ const l=e.label||''; return l.indexOf('restart')>=0?'🔄':(l.indexOf('free_vram')>=0?'🧹':'🔧'); }
      return ''; },
    // 服务端自愈/告警运行时配置：读取（驱动开关状态）/ 切换（POST，免重启灰度）
    async loadHealConfig(){
      try{
        const d=await fetch(HUB+'/api/heal/config').then(r=>r.json());
        if(d && d.ok){ this.healCfg=d; this.autoHealServer=!!d.autoheal; this.healAlertsOn=!!d.alerts;
          this.devAutoSwOn=!!d.dev_autoswitch;     // P6-1 设备缺席自动热切开关
          this.idleLiveGovOn=!!d.idle_live_govern; }  // Song-P6 空转直播治理
      }catch(_){}
    },
    async setHealConfig(patch){
      try{
        const d=await fetch(HUB+'/api/heal/config',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(patch||{})}).then(r=>r.json());
        if(d && d.ok){ this.healCfg=d; this.autoHealServer=!!d.autoheal; this.healAlertsOn=!!d.alerts;
          this.devAutoSwOn=!!d.dev_autoswitch;
          this.idleLiveGovOn=!!d.idle_live_govern;
          if('autoheal' in patch) this.showToast(this.autoHealServer?'已开启服务端自愈（无人值守·全程，带去重/冷却/限次）':'已关闭服务端自愈','info');
          if('alerts' in patch)   this.showToast(this.healAlertsOn?'已开启开播健康告警':'已关闭开播健康告警','info');
          if('dev_autoswitch' in patch) this.showToast(this.devAutoSwOn
            ?'已开启设备缺席自动热切（确认25s·冷却90s·每场≤3次；切换瞬间约1.5s断声）'
            :'已关闭设备缺席自动热切（缺席时仍会亮一键热切按钮）','info');
          if('idle_live_govern' in patch) this.showToast(this.idleLiveGovOn
            ?'已开启空转直播治理（无人上镜30分钟/画面死更10分钟→自动下播，释放算力给唱歌/MV）'
            :'已关闭空转直播治理（僵尸直播将一直占卡并阻塞唱歌任务）','info');
        }
      }catch(e){ this.showToast('设置失败：'+String(e),'error'); }
    },
    // ── P14-A1 直播排班：到点开播/下播 + 断播自动重开（期望态对账，重启不丢场） ──
    schedOpen:false, schedList:[], schedProfiles:[], schedRt:null, schedNext:null, schedBusy:false,
    get schedNextText(){
      if(!this.schedNext) return this.schedList.length?'':'· 未设排班';
      const d=new Date(this.schedNext.ts*1000);
      const wd=['日','一','二','三','四','五','六'][d.getDay()];
      return `· 下一动作：周${wd} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')} ${this.schedNext.action==='start'?'开播':'下播'}（${this.schedNext.label}）`;
    },
    get schedLast(){
      const l=this.schedRt&&this.schedRt.last;
      if(!l) return '';
      return new Date(l.ts*1000).toLocaleString().slice(5)+' '+l.detail;
    },
    async schedLoad(){
      try{
        const d=await fetch(HUB+'/api/live_schedule').then(r=>r.json());
        if(d&&d.ok){ this.schedList=d.entries||[]; this.schedProfiles=d.profiles||[];
          this.schedRt=d.runtime||null; this.schedNext=d.next||null; }
      }catch(_){}
    },
    schedAdd(){
      this.schedList.push({id:'',enabled:true,label:'',days:[1,2,3,4,5,6,7],
                           start:'09:00',end:'12:00',mode:'',profile:''});
    },
    schedToggleDay(e,d){
      const i=e.days.indexOf(d);
      if(i>=0) e.days.splice(i,1); else { e.days.push(d); e.days.sort(); }
    },
    async schedSave(){
      this.schedBusy=true;
      try{
        const r=await fetch(HUB+'/api/live_schedule',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({entries:this.schedList})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'保存失败');
        this.showToast('排班已保存'+(d.next?('，'+this.schedNextText.replace('· ','')):''),'success');
        await this.schedLoad();
      }catch(e){ this.showToast('排班保存失败: '+((e&&e.message)||e),'error'); }
      finally{ this.schedBusy=false; }
    },
    async schedClearOverride(){
      try{
        await fetch(HUB+'/api/live_schedule/clear_override',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:(this.schedRt&&this.schedRt.active_id)||''})});
        this.showToast('已恢复排班接管，稍等自动开播','success');
        await this.schedLoad();
      }catch(e){ this.showToast('操作失败: '+String(e),'error'); }
    },
    // P7-2 数据驱动的「建议开启自动热切」：进页取一次漏斗裁决（auto_advice 由后端按人工战绩算好），
    //   开关已开/已提示过/条件未达 → 一律不出现。开启或「不再提示」都永久退场，不做第二次推销。
    async checkAutoSwAdvice(){
      if(this.autoSwHintDone || this.devAutoSwOn) return;
      try{
        const d=await fetch(HUB+'/api/metrics/devflow').then(r=>r.json());
        const a=d && d.ok && d.auto_advice;
        if(a && a.suggest && !this.autoSwAdvice){
          this.autoSwAdvice=a;
          this._advFlow('advice_expose');   // P8-2 建议条真亮出才记曝光（每次页面会话最多一次）
        }
      }catch(_){}
    },
    // P8-2 建议条效果埋点（fire-and-forget）：advice_* 独立键，不进主漏斗分母
    _advFlow(ev){
      try{
        fetch(HUB+'/api/metrics/devflow',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ev})}).catch(()=>{});
      }catch(_){}
    },
    _autoSwHintClear(){
      this.autoSwAdvice=null; this.autoSwHintDone=true;
      try{ localStorage.setItem('hub_autosw_hint_done','1'); }catch(_){}
    },
    autoSwHintDismiss(){            // 用户点「不再提示」（婉拒）——与开启路径分开计数
      this._advFlow('advice_dismiss');
      this._autoSwHintClear();
    },
    async autoSwHintEnable(){
      this._advFlow('advice_enable');
      await this.setHealConfig({dev_autoswitch:true});
      this._autoSwHintClear();
    },
    // 告警通路自检：发测试告警验证 webhook/本地弹窗真的送得出去（开播前确认，出问题才报得出）
    async testAlert(){
      if(this.testAlertBusy) return; this.testAlertBusy=true;
      this.showToast('正在发送测试告警…','info');
      try{
        const d=await fetch(HUB+'/api/heal/test_alert',{method:'POST'}).then(r=>r.json());
        if(d && d.ok){
          const wc=d.webhook_count||0, sent=d.sent||0;
          let msg = wc? ('已投递 '+sent+'/'+wc+' 个 webhook') : '未配置 webhook';
          if(d.local_toast) msg += (wc?'，':'（')+'已弹本地通知'+(wc?'':'）');
          const good = (sent>0 || d.local_toast);
          this.showToast((good?'✅ 测试告警 · ':'⚠ 测试告警 · ')+msg, good?'success':'error');
        } else this.showToast('测试告警失败：'+((d&&d.detail)||'未知'),'error');
      }catch(e){ this.showToast('测试告警失败：'+String(e),'error'); }
      finally{ this.testAlertBusy=false; }
    },

    // 注：旧的「四项数组版 preflight()」已并入上方 P2 富版(六项·带 status/detail/CTA)，此处删除以消除
    //   重复方法遮蔽——JS 对象字面量后者覆盖前者，旧数组版会让 guardedStart 的 .items 抛错、预检面板变空。
    streamReady(){ return !!this.active; },  // 最低开播条件：必须先选出镜角色（one_click_start 需要 profile）

    // 麦克风/录音设备错误 → 可执行人话；统一走全站共享的 window.friendlyErr(/static/_errutil.js)，留本地兜底
    micErrText(e){
      if(typeof window.friendlyErr==='function') return window.friendlyErr(e);
      return '无法访问麦克风，请检查浏览器权限后重试';
    },

    // 语音合成前置体检：把"角色/语音服务/文本"三项就绪与否提前可视化（与开播页同范式）
    voicePreflight(){
      const ttsOk = !!(this.services.fish_tts || this.services.tts || this.services.emotion_tts);
      return [
        {key:'profile', label:'角色', ok:!!this.active, hint:this.active?('已激活：'+this.active):'去「角色库」激活一个角色'},
        {key:'tts', label:'语音服务', ok:ttsOk, hint:ttsOk?'TTS 服务在线':'TTS 服务离线：到「交付体检/设置」启动语音服务后重试'},
        {key:'text', label:'文本', ok:this.speakText.trim().length>0, hint:this.speakText.trim()?'已输入文本':'请输入要合成的文字'},
      ];
    },
    // UI-P1-4: 3 颗 chips + 橙条 → 一行就绪度摘要（缺什么、点哪修，一句话说完）。
    //   只差文本≠故障：还没打字就飘橙色告警是狼来了，降级为中性引导（hard 才警示）
    voiceReady(){
      const missing=this.voicePreflight().filter(i=>!i.ok);
      return {ok:!missing.length, missing, hard:missing.some(m=>m.key!=='text')};
    },
    voiceFixLabel(k){ return k==='profile'?'去激活':k==='tts'?'去启动':'去输入'; },
    voiceFix(k){
      if(k==='profile') this.goTab('profiles');
      else if(k==='tts') this.goFix('fish_tts');   // UK1: 直落服务行，不再让用户在体检页自己找
      else this.$nextTick(()=>{ try{ this.$refs.speakTa?.focus(); }catch(_){} });
    },
    // UI-P2-2: 能力声明由 /health 服务探测驱动（根治"提示过时"：不再人肉维护能力文案）。
    //   情感引擎(emotion_tts)离线 → 非"普通"情感与指令必失败，胶囊灰化+给原因；
    //   口型：任一口型引擎(musetalk基线/ditto/echomimic/latentsync)在线即可用。
    //   探测未返回（services 还没加载/health 拉取失败）→ 视为可用，不做假告警。
    emotionSvcUp(){
      const s=this.services||{};
      if(!Object.keys(s).length) return true;
      return !!s.emotion_tts;
    },
    lipsyncSvcUp(){
      const s=this.services||{};
      if(!Object.keys(s).length) return true;
      return !!(s.lipsync||s.ditto||s.echomimic||s.latentsync);
    },
    // UI-P2-4: 语音页内嵌最近 3 条历史（会话为空时的"接着上次干"入口；与历史页 WS 同源刷新）。
    //   唱歌记录(emotion='sing')不进语音页：文本是歌词/曲名，回填会把 speakEmotion 污染成非法值
    async loadVoiceRecent(){
      try{
        const d=await fetch(HUB+'/api/history?limit=10').then(r=>r.json());
        this.voiceRecent=(d.records||[]).filter(r=>(r.emotion||'')!=='sing').slice(0,3)
          .map(r=>({id:r.id,ts:r.ts,text:r.text||'',
            emotion:r.emotion||'neutral',profile:r.profile||'',language:r.language||'zh-cn'}));
      }catch(_){ this.voiceRecent=[]; }
    },
    agoText(ts){
      const s=Math.max(0,Date.now()/1000-ts);
      return s<3600?`${Math.max(1,Math.round(s/60))} 分钟前`
           : s<86400?`${Math.round(s/3600)} 小时前`:`${Math.round(s/86400)} 天前`;
    },
    // UI-P1-5: 情感两级化——收起时只展示高频 6 个；已选中的冷门项自动钉住不消失
    emotionOptionsVisible(){
      if(this.emotionsExpanded) return this.emotionOptions;
      const PRIM=['auto','neutral','happy','excited','gentle','serious'];
      return this.emotionOptions.filter(e=>PRIM.includes(e.value)||e.value===this.speakEmotion);
    },
    // UI-P1-3: A/B 三卡动作闭环
    async applyEngAbWinner(){
      const eng=this.engAbWinner, nm=this.active;
      if(!eng||!nm) return;
      try{
        const r=await fetch(HUB+'/profiles/'+encodeURIComponent(nm),{method:'PATCH',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({tts_engine:eng})});
        const d=await r.json().catch(()=>({}));
        if(!r.ok||d.ok===false) throw new Error(d.detail||'更新失败');
        this.showToast(`已为「${nm}」固定 ${eng==='fish_speech'?'Fish-Speech':'CosyVoice'}，之后合成默认用它`,'success');
        await this.loadProfiles();
      }catch(e){ this.showToast('固定引擎失败: '+(e.message||e),'error'); }
    },
    useCompareEmotion(e){
      this.speakEmotion=e;
      this.showToast('已选用「'+this.emotionCN(e)+'」，点「开口说话」即可','success');
    },
    // UI-P1-6: 折叠面板动态摘要（收起也能看到关键状态）
    vcompareSum(){ return this.compareSelected.length ? ('已选 '+this.compareSelected.length+' 种情绪') : '同一句听多种情绪'; },
    vabSum(){ return this.abCompareSelected.length ? ('已选 '+this.abCompareSelected.length+' 个角色') : '同一句换角色听'; },
    vengSum(){ return this.engAbWinner ? ('上次更贴合：'+(this.engAbWinner==='fish_speech'?'Fish-Speech':'CosyVoice')) : 'Fish vs Cosy · 自动打分'; },
    voutSum(){ const n=(this.soStatus?.plugins||[]).filter(p=>p.running).length;
               return n ? ('● '+n+' 路输出中') : '虚拟摄像头 · WebRTC · RTMP · 录制'; },
    vqSum(){ const s=this.vqScore(); return s==null ? '先体检，再按推荐提升' : ('贴合度 '+s.toFixed(2)); },
    // UI-P1-7: 音质优化漏斗——分数条 + 单颗推荐动作，⓪~⑤ 全量工具收进「高级」
    vqScore(){
      const p=this.profiles.find(x=>x.name===this.active);
      const c=p?.quality_axes?.cosine;
      return (typeof c==='number'&&c>0)?c:null;
    },
    vqNext(){
      const s=this.vqScore();
      if(s==null)  return {label:'🩺 30 秒体检', why:'先量出当前贴合度，才能对症提升', act:'probe'};
      if(s<0.75)   return {label:'⚡ 换更优参考（快）', why:'贴合度 '+s.toFixed(2)+' 还有明显空间：优选参考段收益最大', act:'segments'};
      return {label:'🎯 校准最佳 seed', why:'贴合度 '+s.toFixed(2)+' 已不错：固定最佳 seed 再稳一点', act:'calib'};
    },
    vqBusy(){ return !!(this.qpLoading||this.vqOptLoading||this.vqCalibLoading); },
    vqRunNext(){
      const a=this.vqNext().act;
      if(a==='probe') this.probeQuality(this.active);
      else if(a==='segments') this.vqOptimizeSegments();
      else this.vqCalibrate();
    },

    // 失败步骤 → 人话原因+修复步骤（把后端原始报错翻译成可执行指引，与前置体检形成"预防+补救"闭环）
    streamStepHint(step, detail){
      const d=(typeof detail==='string'?detail:'').toLowerCase();
      if(/adb|usb|未授权|unauthor/.test(d)) return '手机未授权：解锁手机 → 允许「USB 调试」→ 重插数据线，再点一键开播';
      if(/scrcpy|投屏/.test(d)) return 'scrcpy 投屏未就绪：确认手机已连接并开启投屏，或改用「本机摄像头」视频源';
      if(/cable|vb-?cable|虚拟声卡/.test(d)) return '未找到 VB-Cable：安装 VB-Audio Virtual Cable 后点 ⟳ 刷新设备';
      if(/\.pth|model|模型/.test(d)) return '变声模型缺失：到「RVC 变声」选择 .pth 模型后重试';
      if(/port|address|占用|in use|bind|already/.test(d)) return '端口被占用：到「设置」重启相关服务，或重启程序后重试';
      if(/timeout|超时|timed out/.test(d)) return '服务响应超时：稍候重试，或到「设置」重启对应服务';
      if(/device|设备|not found|no such|找不到|无法打开|cannot open/.test(d)) return '设备名不匹配：点 ⟳ 刷新设备后重新选择音频输入/输出与摄像头';
      const byStep={
        check_services:'核心服务异常：到「交付体检」查看哪个服务离线并重启',
        start_video:'视频换脸启动失败：检查视频源（手机投屏/摄像头）与换脸服务是否在线',
        start_rvc:'变声启动失败：确认已选麦克风/虚拟声卡、RVC 服务已启动',
      };
      return byStep[step]||'';
    },

    // 内嵌预览：以 ~0.16s 心跳刷新 raw/swapped 快照（仅在预览开启时运行，避免空转占带宽）
    startPreviewTick(){
      this.stopPreviewTick();
      if(typeof document!=='undefined' && document.hidden) return;
      this._previewTimer=setInterval(()=>{
        if(typeof document!=='undefined' && document.hidden) return;
        this.previewTick=Date.now();
      }, 160);
    },
    stopPreviewTick(){ if(this._previewTimer){ clearInterval(this._previewTimer); this._previewTimer=null; } },

    // 数字人预览状态：查 vcam 引擎可达性 + OBS 虚拟摄像头就绪（实时画面卡的健康徽标/占位提示用）。
    // 引擎从不可达恢复为可达的瞬间换 nonce → <img> 重建 MJPEG 连接（否则停在断流前的最后一帧）。
    async loadVcamPrev(){
      try{
        const d=await fetch(HUB+'/api/vcam/status').then(r=>r.json());
        const was=this.vcamPrev.reachable;
        this.vcamPrev={reachable:!!d.reachable, enabled:!!d.enabled, device:d.device||{}, checked:true};
        if(!was && d.reachable) this.vcamPrevNonce=Date.now();
      }catch(_){ this.vcamPrev={reachable:false, enabled:false, device:{}, checked:true}; }
    },
    startVcamPrevTick(){
      this.stopVcamPrevTick();
      this.loadVcamPrev();
      this._vcamPrevTimer=setInterval(()=>{
        if(typeof document!=='undefined' && document.hidden) return;
        if(this.tab!=='stream' || this.broadcastMode!=='avatar_lipsync') return;
        this.loadVcamPrev();
      }, 3000);
    },
    stopVcamPrevTick(){ if(this._vcamPrevTimer){ clearInterval(this._vcamPrevTimer); this._vcamPrevTimer=null; } },

    // P1-B 实时信号轮询(~1.1s 前台 / ~5s 后台)：取 /realtime/signal → 更新 videoLive/audioLive 的真信号 + 电平
    startSignalTick(){
      this.stopSignalTick();
      this.signalTick();
      const ms=(typeof document!=='undefined' && document.hidden)?5000:1100;
      this._signalTimer=setInterval(()=>this.signalTick(), ms);
    },
    stopSignalTick(){ if(this._signalTimer){ clearInterval(this._signalTimer); this._signalTimer=null; } },
    async signalTick(){
      // 开播/停播沿：记录本轮开播起点，并清零上一场的「最近有声」时刻(防跨场次泄漏误判无声)
      const streaming=!!this.perf.streaming;
      if(streaming && !this._wasStreaming){ this.streamingSinceTs=Date.now(); this.cableActiveTs=0; this.audioActiveTs=0; this.cableOkTs=0; }
      if(streaming){                                                            // [P3/P5 场次小结] 本场累计：峰值 fps + 是否用过变声（开播页采样；起止沿见 $watch(perf.streaming)）
        if((this.perf.fps||0)>this.sessPeakFps) this.sessPeakFps=this.perf.fps||0;
        if(this.rvcLive && this.rvcLive()) this.sessUsedRvc=true;
        // [S5 成绩单·稳定度] 健康采样：ok=换脸生效中；warmup(启动检测)不计入分母，避免冤枉启动期
        try{ const hs=this.streamHealth().state;
          if(hs && hs!=='warmup'){ this.sessTicksTotal++; if(hs==='ok') this.sessTicksOk++; } }catch(_){}
      }
      this._wasStreaming=streaming;
      // S6/S8-2: 真人换脸在播时每 ~8s 顺带刷换脸容灾态；副本接管时加密到 ~3s 以便回切徽标及时灭
      const foOn = this.dfmHot.cur && this.dfmHot.cur.failover && this.dfmHot.cur.failover.on_replica;
      const dfmPollMs = foOn ? 3000 : 8000;
      if(streaming && this.broadcastMode==='real_faceswap'
         && Date.now()-(this._lastDfmCurTs||0)>dfmPollMs){ this._lastDfmCurTs=Date.now(); this.refreshDfmCur(); }
      this.streamClock=Date.now();   // [P2 价值条] 在播时长心跳：每轮信号轮询刷新，供 streamDurStr() 秒级更新
      // P2-1b 漂移兜底：devicechange 事件之外，开播页驻留时每 45s 静默重枚举一次设备
      //（拔插自愈不依赖浏览器事件百分百可靠；rvcRefreshDevices 内部有 _lastDevRefreshTs 记账）
      if(Date.now()-(this._lastDevRefreshTs||0)>45000) this.rvcRefreshDevices();
      try{
        const d=await fetch(HUB+'/realtime/signal').then(r=>r.json());
        if(d&&d.video) this.signal.video=d.video;
        if(d&&d.audio){ this.signal.audio=d.audio; if((d.audio.rms||0)>0.01) this.audioActiveTs=Date.now(); }  // >阈值即记「最近有声」
        if(d&&d.cable){ this.signal.cable=d.cable;
          if(d.cable.ok) this.cableOkTs=Date.now();                                          // P1-F+ 探针可信(能读电平)即记时，供「测不到直播声」判定
          if(d.cable.ok&&(d.cable.rms||0)>0.008) this.cableActiveTs=Date.now(); }            // P1-F 广播馈线真有声
      }catch(_){}
    },

    maybeAutoApplyLow(){
      if(!this.autoLowPreset) { this.lowPresetApplied=false; return; }
      const tone=this.perfSuggestion().tone;
      if((tone==='red' || tone==='orange') && !this.lowPresetApplied){
        this.applyLowLatencyPreset();
        this.lowPresetApplied=true;
      }
      if(tone==='green' || tone==='muted') this.lowPresetApplied=false;
    },

    async setCamera(idx) {
      try {
        await fetch(HUB+'/realtime/set_camera',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx})});
        this.showToast(`摄像头已切换至 #${idx}，5秒内自动热重载`, 'info');
      } catch(e){
        this.showToast('摄像头切换失败', 'error');
      }
    },

    // 情感中文名/emoji 单一定义（UI-P3 合并：原 1729 行死代码副本已删，calm 词条并入）
    emotionCN(e) {
      // 'sing' 是唱歌记录的类型标记（非情感值），历史页展示需要人话映射
      const m={neutral:'平稳',happy:'开心',sad:'悲伤',angry:'愤怒',gentle:'温柔',
               excited:'兴奋',surprised:'惊讶',fearful:'恐惧',disgusted:'厌恶',
               confused:'困惑',calm:'平静',serious:'严肃',auto:'自动',sing:'唱歌'};
      return m[e]||e||'平稳';
    },
    emotionEmoji(e) {
      const m={neutral:'😐',happy:'😊',sad:'😢',angry:'😡',gentle:'🥰',
               excited:'🤩',surprised:'😲',fearful:'😨',disgusted:'🤢',
               confused:'😕',calm:'😌',serious:'😤',auto:'🤖',sing:'🎵'};
      return m[e]||'😐';
    },

    // P0-3 结构化服务错误 → 人话：后端离线功能入口现在返回 detail={code:'SVC_DOWN', label, message,…}
    // （代启动失败/冷启动未就绪）。旧路径 detail 仍可能是字符串——两者都兜住，别再弹英文异常原文。
    svcErrText(d, fallback = '未知错误') {
      const det = d && d.detail;
      if (det && typeof det === 'object') {
        if (det.code === 'SVC_DOWN') {
          return (det.message || `「${det.label || det.service || '服务'}」未就绪`)
                 + (det.can_start ? '——已尝试自动启动，稍等 20-60 秒再点一次即可' : '');
        }
        return String(det.message || JSON.stringify(det)).slice(0, 140);
      }
      return String(det || (d ? JSON.stringify(d) : '') || fallback).slice(0, 140);
    },
    // 离线功能服务冷启动提示：点击时服务未在线 → 先告知用户后端会自动拉起（免得盯着按钮以为卡死）
    labSvcColdHint(key, label) {
      const s = this.labSvc && this.labSvc[key];
      if (s && !s.up) this.showToast(`${label}服务未启动，正在自动启动并继续（首次约 20-60 秒）…`, 'info');
    },
    // P1 一键启动离线服务（试衣等重服务：面板需服务在线才能列服装/样式，故给显式启动入口；
    // 定妆脸/妆容走后端全自动代启，无需此按钮）。/api/services/ensure 会阻塞到健康就绪。
    async startLabSvc(key, label) {
      if (this.svcStartBusy) return;
      this.svcStartBusy = key;
      this.showToast(`正在启动${label}服务（冷启动约 20 秒-2 分钟）…`, 'info');
      try {
        const d = await fetch(HUB + `/api/services/ensure?name=${encodeURIComponent(key)}&wait_s=150`,
                              {method: 'POST'}).then(r => r.json());
        if (d.ok) this.showToast(`${label}服务已就绪`, 'success');
        else this.showToast(`${label}服务未就绪：` + (d.detail || '未知原因'), 'warn');
      } catch (e) { this.showToast(`${label}服务启动失败：` + e, 'error'); }
      finally { this.svcStartBusy = ''; this.loadLabServices(); }
    },

    showToast(msg, type='info') {
      if (this.demoMode && type === 'info') return;                        // 演示模式：静音最低优先级信息提示
      const ttl = type === 'error' ? 5000 : 3500;
      const dup = this.toasts.find(t => t.msg === msg && t.type === type); // 去重：刷新存活时间而非叠加刷屏
      if (dup) { dup.remain = ttl; return; }
      const id = ++this._toastSeq;                                         // 自增 id，杜绝同毫秒 Date.now 撞 :key
      this.toasts.push({ id, msg, type, remain: ttl });
      if (this.toasts.length > 3) this.toasts.shift();                     // 最多 3 条，超出丢最旧
      this._ensureToastTick();
    },
    // 单一倒计时心跳（仅在有 toast 时运行）：悬停暂停不倒计时；remain 归零即移除
    _ensureToastTick() {
      if (this._toastTick) return;
      this._toastTick = setInterval(() => {
        if (this._toastsPaused) return;                                    // 悬停阅读时暂停自动消失
        for (const t of this.toasts) t.remain -= 250;
        if (this.toasts.some(t => t.remain <= 0))
          this.toasts = this.toasts.filter(t => t.remain > 0);
        if (!this.toasts.length) { clearInterval(this._toastTick); this._toastTick = null; }
      }, 250);
    },
    // 手动关闭单条（× 按钮）。重置暂停：× 关闭时被关元素的 mouseleave 可能不触发，避免暂停卡死其余条
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
      this._toastsPaused = false;
      if (!this.toasts.length && this._toastTick) { clearInterval(this._toastTick); this._toastTick = null; }
    },

    toggleDemo() {
      this.demoMode = !this.demoMode;
      if(this.demoMode) this.showPerfDetail=false;   // 演示模式：开播页收起技术指标，对客户更干净
      try{ localStorage.setItem('hub_demo', this.demoMode?'1':'0'); }catch(_){}
      this.showToast(this.demoMode?'已开启演示模式：隐藏运维告警/调试信息':'已退出演示模式', this.demoMode?'success':'info');
    },

    // 统一切换 Tab：更新状态 + URL hash + 持久化 + 记录已访问（为内容懒加载预留）
    goTab(id) {
      const _t = (this.tabs||[]).find(x => x.id===id);
      if (_t && _t.href) { window.open(_t.href, '_blank'); return; }   // 跳出型页签（对话 /phone）：新窗口打开，不离开控制台
      if(id!=='batch') this._batchAudioStop?.();   // UI-P7-2: 离开批量页掐断行内试听/连播（避免背景放 20 行）
      this.tab = id;
      if (!this.visitedTabs.includes(id)) this.visitedTabs.push(id);
      try{ location.hash = id; }catch(_){}
      try{ localStorage.setItem('hub_tab', id); }catch(_){}
      if (id === 'sing') this.songHealthCheck();   // Song-P1: 进页即探翻唱能力（诚实展示）
    },

    toggleSidebar() {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      try{ localStorage.setItem('hub_sidebar_collapsed', this.sidebarCollapsed?'1':'0'); }catch(_){}
    },

    // ── 命令面板（Ctrl/⌘+K）：跳转 Tab / 搜角色 / 触发动作 / 跨页前往 ──
    // P1: 跨页入口由后端功能注册表 /api/features 驱动（单一真相），命令面板与首页同源、不重复维护。
    async loadFeatures() {
      try {
        const d = await fetch(HUB + '/api/features').then(r => r.json());
        if (d && d.ok && Array.isArray(d.features)) {
          // [P2-2 修正] 存全量注册表：页签 tooltip 产品线（tabTitle）与 PRO 角标（tabProBadge）
          // 需要站内 /ui# 项的 line/edition；「跨页入口去重」下沉到命令面板 cmdItems 消费处。
          this.features = d.features;
        }
      } catch (_) { /* 注册表拉取失败不影响面板其余项 */ }
    },
    get cmdItems() {
      // [图标统一·2026-07-16] 面板项图标改线性图标(icx)，与侧栏/首页/桌面启动台同一图标库；
      // 注册表旧缓存无 ic 字段时回退 emoji（x-html 渲染两者皆可）。
      const items = [];
      (this.tabs||[]).forEach(t => items.push({icon:this.icx(t.ic), label:t.label, hint:'切换 · '+t.group, type:'tab', payload:t.id}));
      items.push({icon:this.icx('zap'), label:'三步上手向导', hint:'动作', type:'act', payload:'onboard'});
      items.push({icon:this.icx('demo'), label:(this.demoMode?'退出演示模式':'开启演示模式'), hint:'动作', type:'act', payload:'demo'});
      items.push({icon:this.icx('home'), label:'首页 · 全部功能', hint:'前往', type:'link', payload:'/'});
      // 跨页入口去重：/ui#xxx 的站内 Tab 已在上面的 tab 项里（features 现存全量注册表，见 loadFeatures）
      (this.features||[]).filter(f => f.href && !f.href.startsWith('/ui#'))
        .forEach(f => items.push({icon:(f.ic?this.icx(f.ic):(f.icon||'🔗')), label:f.name, hint:'前往 · '+(f.line||''), type:'link', payload:f.href}));
      (this.profiles||[]).forEach(p => items.push({icon:this.icx('users'), label:p.name, hint:'角色 · 打开对话', type:'profile', payload:p.name}));
      return items;
    },
    get cmdResults() {
      const q = (this.cmdQuery||'').trim().toLowerCase();
      const list = q ? this.cmdItems.filter(i => i.label.toLowerCase().includes(q)) : this.cmdItems;
      return list.slice(0, 12);
    },
    openCmd() {
      this.cmdShow = true; this.cmdQuery = ''; this.cmdIndex = 0;
      this.$nextTick(() => { const el = document.getElementById('cmdInput'); if (el) el.focus(); });
    },
    cmdMove(d) { const n = this.cmdResults.length; if (!n) return; this.cmdIndex = (this.cmdIndex + d + n) % n; },
    cmdEnter() { const r = this.cmdResults[this.cmdIndex] || this.cmdResults[0]; if (r) this.cmdRun(r); },
    cmdRun(item) {
      this.cmdShow = false;
      if (item.type === 'tab') this.goTab(item.payload);
      else if (item.type === 'profile') window.open('/phone?profile=' + encodeURIComponent(item.payload), '_blank');
      else if (item.type === 'act') { if (item.payload === 'onboard') this.openOnboard(); else if (item.payload === 'demo') this.toggleDemo(); }
      else if (item.type === 'link') window.open(item.payload, '_blank');   // P1: 跨页前往（新标签，不打断当前工作）
    },

    // ── TTS 声音快速预听 ──
    async quickPreview(voiceName, target) {
      if (!voiceName) return;
      this.previewAudioLoading=true;
      try {
        const d=await fetch(HUB+'/tts/quick_preview',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({voice_name:voiceName,text:'大家好，我是你们的数字人主播',language:'zh-cn'})}).then(r=>r.json());
        if (d.ok && d.audio_base64) {
          const src='data:audio/wav;base64,'+d.audio_base64;
          if (target==='newP') this.newP.previewAudio=src;
          else if (target==='editP') this.editP.previewAudio=src;
        } else {
          this.showToast('试听失败：'+(d.detail||'TTS 服务未就绪'),'error');
        }
      } catch(e){ this.showToast('试听请求失败','error'); }
      finally { this.previewAudioLoading=false; }
    },

    // ── 资产管理面板（声音 / RVC 变声模型 / 回收站统一视图）──
    // query 可选：跳转入口预填搜索（如断链修复「去换绑」带上旧模型名直达目标）；不传则保留用户上次输入
    async vaOpen(tab, query){ this.vaShow=true; if(tab) this.vaTab=tab; if(query!==undefined) this.vaQuery=query; this.trashPolicyLoad(); await this.vaRefresh(); },
    vaClose(){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; }
      if(this._vpAudio){ try{ this._vpAudio.pause(); }catch(_){} this._vpAudio=null; }
      this.vaPlayId=''; this.vpPlayId=''; this.vaShow=false;
    },
    async vaRefresh(){
      this.vaLoading=true;
      try{
        // 四类并行拉取：声音资产 / RVC 模型 / 回收站 / 真人音色库（后三者失败不掐死声音页签）
        const [v,r,t,vp,pt]=await Promise.all([
          fetch(HUB+'/api/voice_assets').then(r=>r.json()),
          fetch(HUB+'/api/rvc_assets').then(r=>r.json()).catch(()=>({assets:[],total_bytes:0})),
          fetch(HUB+'/api/asset_trash').then(r=>r.json()).catch(()=>({items:[],total_bytes:0})),
          fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>({rows:[]})),
          fetch(HUB+'/api/voicepack/preview_text').then(r=>r.json()).catch(()=>null),
        ]);
        this.vaAssets=v.assets||[]; this.vaEmbedded=v.profile_embedded||[];
        this.vaRvc=r.assets||[]; this.vaRvcTotal=r.total_bytes||0; this.vaRvcUp=!!r.rvc_up;
        this.vaTrash=t.items||[]; this.vaTrashTotal=t.total_bytes||0;
        this.vpRows=vp.rows||[];   // 音色包缺席（未下载）时 rows=[]，页签自动隐藏
        this.vpFavs=Object.fromEntries((vp.favs||[]).map(s=>[s,1]));
        this.vpTrashN=vp.ext_trash_n||0;
        this.vpTier=vp.tier||{mode:'off',locked_n:0};
        this.vpDistProtected=!!vp.dist_protected;
        this.vpSubs.n=vp.subs_n||0;
        if(pt&&pt.ok){
          this.vpPvText=pt.text||''; this.vpPvTpl=pt.template_id||'welcome';
          this.vpPvTemplates=pt.templates||[]; this.vpPvStaleN=pt.stale_n||0;
        }
        this.vaSel={};   // 列表换血后勾选集清零（避免幽灵勾选残留到下一批条目）
      }catch(e){ this.showToast('资产加载失败','error'); }
      finally{ this.vaLoading=false; }
      this.loadAssetHealth();   // 面板内清理后同步刷新角色库页横幅（fire-and-forget）
      this.loadVoiceHealth();   // 资产增删可能造成/修复断链 → 体检跟着资产面板同步
    },
    // ── 资产巡检轻提示：角色库页横幅（只在确定是垃圾的信号上提示：孤儿克隆音≥3 或 回收站≥500MB）──
    //    「未备份参考音」不触发横幅：导入配置包的角色天然只有内嵌音，属正常态，报警会狼来了。
    async loadAssetHealth(){
      try{ this.assetHealth=await fetch(HUB+'/api/asset_health').then(r=>r.json()); }catch(_){}
    },
    get assetNudge(){
      if(this._uivr || this.nudgeDismissed) return '';   // 截图回归/本会话已关 → 不打扰
      const h=this.assetHealth;
      if(!h||!h.ok) return '';
      const parts=[];
      if((h.orphans||0)>=3) parts.push(`${h.orphans} 段孤儿克隆音（没有角色在用）`);
      if((h.trash_bytes||0)>=500*1024*1024) parts.push(`回收站已占 ${this.vaFmtSize(h.trash_bytes)}`);
      return parts.length?parts.join('，'):'';
    },
    // 横幅点进去直达对应页签：有孤儿看声音页；只是回收站超限直达回收站
    get assetNudgeTab(){
      const h=this.assetHealth||{};
      return (h.orphans||0)>=3 ? 'voice' : 'trash';
    },
    nudgeDismiss(){
      this.nudgeDismissed=true;
      try{ sessionStorage.setItem('hub_asset_nudge_dismissed','1'); }catch(_){}
    },
    // 资产健康汇总（面板头部一行看清：孤儿 / 未备份参考音 / 回收站占用）
    get vaHealth(){
      const orphans=(this.vaAssets||[]).filter(a=>a.kind==='clone'&&a.orphan).length;
      const unbacked=(this.vaEmbedded||[]).filter(e=>!e.matched).length;
      const rvcOrphans=(this.vaRvc||[]).filter(a=>!a.refs.length).length;
      return {orphans, unbacked, rvcOrphans,
              trashN:(this.vaTrash||[]).length, trashBytes:this.vaTrashTotal};
    },
    // 试听单例：同一时刻只播一条，再点同一条=停止（跨页签也互斥：与真人音色库共用心智）
    vaPlay(a){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; }
      if(this._vpAudio){ try{ this._vpAudio.pause(); }catch(_){} this._vpAudio=null; this.vpPlayId=''; }
      if(this.vaPlayId===a.id){ this.vaPlayId=''; return; }
      const au=new Audio(HUB+'/api/voice_assets/'+encodeURIComponent(a.id)+'/audio');
      this._vaAudio=au; this.vaPlayId=a.id;
      au.onended=()=>{ if(this.vaPlayId===a.id) this.vaPlayId=''; };
      au.onerror=()=>{ this.showToast('试听失败','error'); if(this.vaPlayId===a.id) this.vaPlayId=''; };
      au.play().catch(()=>{});
    },
    // 绑定到角色：孤儿克隆音的找回通道（后端复用 PATCH 全部既有约束）
    async vaBind(a, profile){
      if(!profile) return;
      if(!confirm(`把「${a.name}」绑定到角色「${profile}」？\n\n将替换该角色现有声音。`)) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/voice_assets/bind',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:a.id, profile})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'绑定失败');
        this.showToast(`已把声音绑定到「${profile}」`,'success');
        await this.loadProfiles(); await this.vaRefresh();
      }catch(e){ this.showToast('绑定失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    async vaRename(a){
      const nn=prompt('新文件名（字母 / 数字 / - _，自动补 .wav）', a.name.replace(/\.wav$/i,''));
      if(!nn||!nn.trim()) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/voice_assets/rename',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:a.id, new_name:nn.trim()})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'改名失败');
        await this.vaRefresh();
      }catch(e){ this.showToast('改名失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    async vaDelete(a){
      if(!confirm(`把孤儿声音「${a.name}」移入回收目录？\n\n不影响任何角色；后悔可到 voice_clones/_trash 手工找回。`)) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/voice_assets/'+encodeURIComponent(a.id),{method:'DELETE'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'删除失败');
        this.showToast('已移入回收目录','success');
        await this.vaRefresh();
      }catch(e){ this.showToast('删除失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    // 一键清孤儿：横幅/面板共用（服务端按内容哈希回算引用，被引用文件绝不会被碰；软删可还原）
    // names 可选：只清名单内的孤儿（勾选批量清理走这里；服务端仍会二次校验引用，勾错也删不掉在用的）
    async vaPurgeOrphans(names){
      const n=(names&&names.length)||(this.assetHealth&&this.assetHealth.orphans)||this.vaHealth.orphans||0;
      const scope=names&&names.length?`选中的 ${names.length} 段`:`${n||'所有'} 段`;
      if(!confirm(`把${scope}孤儿克隆音移入回收站？\n\n只动没有角色在用的文件；可在资产面板「♻️ 回收站」还原。`)) return;
      this.vaBusy='purge:orphans';
      try{
        const body=names&&names.length?JSON.stringify({only:names}):'{}';
        const r=await fetch(HUB+'/api/voice_assets/purge_orphans',{method:'POST',
          headers:{'Content-Type':'application/json'}, body});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清理失败');
        this.showToast(`已把 ${d.moved} 段孤儿克隆音移入回收站`,'success');
        this.vaSel={};
        await this.vaRefresh();   // 末尾会顺带刷新 assetHealth → 横幅自动消退
      }catch(e){ this.showToast('清理失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    vaFmtSize(n){ return n>1048576 ? (n/1048576).toFixed(1)+' MB' : Math.round((n||0)/1024)+' KB'; },
    vaFmtDate(ts){ return ts ? new Date(ts*1000).toLocaleDateString() : ''; },

    // ── RVC 变声模型资产 ──
    // 变声试听共用核心：settings=null 用已保存预设；传对象则用未保存滑条参数（调参闭环）
    // 试听输入跟随 vaPreviewSample：库存样本 or 指定角色参考音（听到的效果≈该角色实际开播）
    async _vaRvcPreview(a, settings){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; }
      if(this.vaRvcPreviewBusy) return;
      if(!this.vaRvcUp){ this.showToast('RVC 变声引擎离线：到「开播」页启动变声后再试听','error'); return; }
      this.vaRvcPreviewBusy=a.id;
      try{
        const body={id:a.id}; if(settings) body.settings=settings;
        if(this.vaPreviewSample) body.sample_profile=this.vaPreviewSample;
        const r=await fetch(HUB+'/api/rvc_assets/preview',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'试听失败');
        const au=new Audio('data:audio/wav;base64,'+d.audio_base64);
        this._vaAudio=au; this.vaPlayId=a.id;
        au.onended=()=>{ if(this.vaPlayId===a.id) this.vaPlayId=''; };
        au.onerror=()=>{ this.showToast('试听播放失败','error'); if(this.vaPlayId===a.id) this.vaPlayId=''; };
        au.play().catch(()=>{});
      }catch(e){ this.showToast('变声试听失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaRvcPreviewBusy=''; }
    },
    // 卡片 ▶：按已保存预设试听（首次冷加载模型可能 10-30s，busy 态要撑住）
    async vaRvcPlay(a){
      if(this.vaPlayId===a.id){   // 再点同一条=停止
        if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; }
        this.vaPlayId=''; return;
      }
      await this._vaRvcPreview(a, null);
    },
    // 预设面板「试听当前参数」：拖滑条→立即听效果→满意再保存（不落任何盘）
    async vaPresetPreview(a){
      const f=this.vaPresetForm;
      await this._vaRvcPreview(a, {
        pitch:Math.round(+f.pitch||0), index_rate:+f.index_rate,
        protect:+f.protect, f0method:f.f0method});
    },
    // 模型默认参数（预设）：卡片内联展开滑条编辑，绑定角色时自动带入 rvc_settings
    vaPresetToggle(a){
      if(this.vaPresetId===a.id){ this.vaPresetId=''; return; }
      const p=a.preset||{};
      this.vaPresetForm={
        pitch: p.pitch!==undefined?p.pitch:0,
        index_rate: p.index_rate!==undefined?p.index_rate:0.75,
        protect: p.protect!==undefined?p.protect:0.33,
        f0method: p.f0method||'rmvpe',
      };
      // 听到即所得：还没手选试听用声时，默认跟随该模型绑定的角色参考音（调参听到的≈它实际开播）
      if(!this.vaPreviewSample && (a.refs||[]).length){
        const rp=(this.profiles||[]).find(x=>x.name===a.refs[0]);
        if(rp && rp.has_voice) this.vaPreviewSample=rp.name;
      }
      this.vaPresetId=a.id;
    },
    // 从角色现有 rvc_settings 复制参数到编辑表单（已绑定角色调好的参数一键回流到模型预设）
    vaPresetCopyFrom(pname){
      if(!pname) return;
      const prof=(this.profiles||[]).find(p=>p.name===pname);
      const s=(prof&&prof.rvc_settings)||{};
      if(!Object.keys(s).length){ this.showToast(`「${pname}」没有已保存的变声参数`,'info'); return; }
      if(s.pitch!==undefined) this.vaPresetForm.pitch=s.pitch;
      if(s.index_rate!==undefined) this.vaPresetForm.index_rate=s.index_rate;
      if(s.protect!==undefined) this.vaPresetForm.protect=s.protect;
      if(s.f0method) this.vaPresetForm.f0method=s.f0method;
      this.showToast(`已复制「${pname}」的变声参数，记得保存`,'success');
    },
    async vaPresetSave(a){
      this.vaPresetBusy=true;
      try{
        const f=this.vaPresetForm;
        const r=await fetch(HUB+'/api/rvc_assets/preset',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:a.id, settings:{
            pitch:Math.round(+f.pitch||0), index_rate:+f.index_rate,
            protect:+f.protect, f0method:f.f0method}})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'保存失败');
        this.showToast('已保存默认参数（之后绑定角色自动带入）','success');
        this.vaPresetId='';
        await this.vaRefresh();
      }catch(e){ this.showToast('保存失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaPresetBusy=false; }
    },
    async vaRvcBind(a, profile){
      if(!profile) return;
      if(!confirm(`把变声模型「${a.name}」绑定到角色「${profile}」？\n\n该角色说话/配音时将套用此变声。`)) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/rvc_assets/bind',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:a.id, profile})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'绑定失败');
        // 听到即所得：绑定给谁，试听用声就跟着切到谁的参考音（有参考音才切）
        const bp=(this.profiles||[]).find(x=>x.name===profile);
        const follow=!!(bp && bp.has_voice);
        if(follow) this.vaPreviewSample=profile;
        this.showToast(`已把变声模型绑定到「${profile}」`+(follow?'，试听用声已切到该角色':''),'success');
        await this.loadProfiles(); await this.vaRefresh();
      }catch(e){ this.showToast('绑定失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    async vaRvcUnbind(a, profile){
      if(!confirm(`解除「${profile}」与变声模型「${a.name}」的绑定？`)) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/rvc_assets/bind',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:'', profile})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'解绑失败');
        await this.loadProfiles(); await this.vaRefresh();
      }catch(e){ this.showToast('解绑失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    async vaRvcDelete(a){
      if(!confirm(`把变声模型「${a.name}」（${this.vaFmtSize(a.size)}）移入回收站？\n\n不影响任何角色；可在「回收站」页签还原。`)) return;
      this.vaBusy=a.id;
      try{
        const r=await fetch(HUB+'/api/rvc_assets/'+encodeURIComponent(a.id).replace(/%2F/g,'/'),{method:'DELETE'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'删除失败');
        this.showToast('已移入回收站','success');
        await this.vaRefresh();
      }catch(e){ this.showToast('删除失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },

    // ── 回收站 ──
    async vaTrashRestore(it, strategy=''){
      this.vaBusy='trash:'+it.name;
      try{
        const body={kind:it.kind, name:it.name};
        if(strategy) body.on_conflict=strategy;
        const r=await fetch(HUB+'/api/asset_trash/restore',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        if(r.status===409){
          // 同名冲突：安全路径（改名共存）放一键直达；覆盖是危险动作（可能顶掉正被角色引用的文件），退一步再确认
          this.vaBusy='';
          if(confirm(`原位置已存在同名文件「${it.orig_name}」。\n\n【确定】改名还原，两个文件都保留\n【取消】更多选择（覆盖现有文件 / 放弃）`))
            return this.vaTrashRestore(it,'rename');
          if(confirm(`用回收站版本覆盖现有的「${it.orig_name}」？\n\n现有文件将被顶替（若正被角色引用会跟着变声），不可逆。\n【确定】覆盖\n【取消】放弃还原`))
            return this.vaTrashRestore(it,'overwrite');
          return;
        }
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'还原失败');
        this.showToast(d.renamed?`同名冲突，已改名还原为 ${d.restored}`:`已还原 ${d.restored}`,'success');
        await this.vaRefresh();
      }catch(e){ this.showToast('还原失败: '+((e&&e.message)||e),'error'); }
      finally{ if(this.vaBusy==='trash:'+it.name) this.vaBusy=''; }
    },
    async vaTrashPurge(it){
      const msg = it ? `彻底删除「${it.orig_name}」？此操作不可逆。`
                     : `清空整个回收站（${this.vaTrash.length} 项 · ${this.vaFmtSize(this.vaTrashTotal)}）？此操作不可逆。`;
      if(!confirm(msg)) return;
      this.vaBusy=it?('trash:'+it.name):'trash:all';
      try{
        const body = it ? {kind:it.kind, name:it.name} : {};
        const r=await fetch(HUB+'/api/asset_trash/purge',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清理失败');
        this.showToast(`已彻底删除 ${d.purged} 项`,'success');
        await this.vaRefresh();
      }catch(e){ this.showToast('清理失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },
    // ── 搜索：一个输入过滤三页签（大小写不敏感子串；命中名字或绑定角色即留下）──
    _vaHit(...fields){
      const q=this.vaQuery.trim().toLowerCase();
      if(!q) return true;
      return fields.some(f=>String(f||'').toLowerCase().includes(q));
    },
    get vaAssetsF(){ return this.vaAssets.filter(a=>this._vaHit(a.name, ...(a.refs||[]))); },
    get vaRvcF(){ return this.vaRvc.filter(a=>this._vaHit(a.name, a.id, a.alias, ...(a.refs||[]))); },   // P2-RA: 搜「沉香木」也能命中
    get vaTrashF(){ return this.vaTrash.filter(it=>this._vaHit(it.orig_name)); },
    // 搜索时页签带命中数：跨页签命中一眼可见（不用逐个页签点过去确认「是不是真没有」）
    get vaTabHits(){
      return this.vaQuery.trim()
        ? {voice:this.vaAssetsF.length, rvc:this.vaRvcF.length, trash:this.vaTrashF.length,
           pack:this.vpQueryRows.length}
        : null;
    },

    // ── 真人音色库页签：精选橱窗 / 筛选 / 排序 / 分页 / 双试听 / 一键导入 ──
    // 搜索命中口径：名字 / 编号 / 场景词 / 已导入角色名（"搜『带货』出适合带货的声音"）
    get vpQueryRows(){ return this.vpRows.filter(r=>this._vaHit(r.name, r.spk, r.scene, r.band, r.style, r.imported)); },
    _vpChipFilter(rows){
      if(this.vpGender) rows=rows.filter(r=>r.gender===this.vpGender);
      if(this.vpBand)   rows=rows.filter(r=>r.band===this.vpBand);
      if(this.vpHq)     rows=rows.filter(r=>r.stars>=4);
      if(this.vpFavOnly)rows=rows.filter(r=>this.vpFavs[r.spk]);
      return rows;
    },
    // 精选橱窗：人工命名 12 席，吃同一套筛选（选了"女声"精选区就只剩女声，心智一致）
    // P8: 排除上新——EXT 设主打后仍留在「我的上新」区（那里有删除/补转写入口且本就置顶），不重复出现
    get vpFeatured(){ return this._vpChipFilter(this.vpQueryRows.filter(r=>r.featured&&!r.ext)); },
    // P6 我的上新：自有音色单列一区（自己的东西要一眼找到），同吃筛选
    get vpExt(){ return this._vpChipFilter(this.vpQueryRows.filter(r=>r.ext)); },
    get vpFiltered(){
      // 长尾网格排除精选与上新（各自已有专区，重复出现反而让人怀疑库存注水）
      let rows=this._vpChipFilter(this.vpQueryRows.filter(r=>!r.featured&&!r.ext));
      const k=this.vpSort;
      rows=rows.slice();
      if(k==='snr')        rows.sort((a,b)=>(b.snr||0)-(a.snr||0));
      else if(k==='f0asc') rows.sort((a,b)=>(a.f0_med||999)-(b.f0_med||999));
      else if(k==='f0desc')rows.sort((a,b)=>(b.f0_med||0)-(a.f0_med||0));
      else if(k==='rate')  rows.sort((a,b)=>(b.rate||0)-(a.rate||0));
      // P3 常用自动置顶：会话级使用次数(激活/同传开播/通话中切换)降序。稳定排序下
      // 两次二级 sort 形成分层：❤ 收藏 > 🔥 常用 > 主排序，未用过的条目相对顺序不变。
      rows.sort((a,b)=>(b.use_n||0)-(a.use_n||0));
      rows.sort((a,b)=>(this.vpFavs[b.spk]?1:0)-(this.vpFavs[a.spk]?1:0));
      return rows;
    },
    get vpPages(){ return Math.max(1, Math.ceil(this.vpFiltered.length/24)); },
    get vpPageRows(){
      const p=Math.min(this.vpPage, this.vpPages-1);   // 筛选收窄后页码越界自动回卷
      return this.vpFiltered.slice(p*24,(p+1)*24);
    },
    get vpImportedN(){ return this.vpRows.filter(r=>r.imported).length; },
    // 预生成对象=精选+上新（全量口径，不吃筛选——按钮别因为点了个"女声"chip 就消失）
    get vpPvTargets(){ return this.vpRows.filter(s=>s.featured||s.ext); },
    get vpNeedGen(){ return this.vpPvTargets.some(s=>!s.has_preview||s.pv_stale); },
    get vpStaleN(){ return this.vpPvTargets.filter(s=>s.pv_stale).length; },
    // 克隆试听 URL（pv_ver 防浏览器旧缓存；换台词后必须带新版本号）
    vpPvUrl(s){ const v=s.pv_ver||'0'; return HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/preview?v='+v; },
    // 双试听单例：kind=''=真人原声 / 'pv'=克隆效果；同刻只响一条，再点同一条=停
    vpPlay(s, kind){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; this.vaPlayId=''; }
      if(this._vpAudio){ try{ this._vpAudio.pause(); }catch(_){} this._vpAudio=null; }
      const pid = kind==='pv' ? (s.spk+':pv') : s.spk;
      if(this.vpPlayId===pid){ this.vpPlayId=''; return; }
      const ep = kind==='pv' ? this.vpPvUrl(s) : (HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/audio');
      const au=new Audio(ep);
      this._vpAudio=au; this.vpPlayId=pid;
      au.onended=()=>{ if(this.vpPlayId===pid) this.vpPlayId=''; };
      au.onerror=()=>{ this.showToast('试听失败','error'); if(this.vpPlayId===pid) this.vpPlayId=''; };
      au.play().catch(()=>{});
    },
    // A/B 对比连播：真人原声放 4 秒 → 自动切克隆整段。一耳朵听出"克隆有多像"，
    // 比手动来回点两个按钮少两次操作（试听场景里用户根本记不住 4 秒前的音色细节）。
    vpPlayAB(s){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; this.vaPlayId=''; }
      if(this._vpAudio){ try{ this._vpAudio.pause(); }catch(_){} this._vpAudio=null; }
      const pid=s.spk+':ab';
      if(this.vpPlayId===pid||this.vpPlayId===pid+'2'){ this.vpPlayId=''; return; }   // 再点=停
      const a1=new Audio(HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/audio');
      this._vpAudio=a1; this.vpPlayId=pid;
      const toClone=()=>{
        if(this.vpPlayId!==pid) return;        // 中途被停/切别的条目 → 不再接播
        try{ a1.pause(); }catch(_){}
        const a2=new Audio(this.vpPvUrl(s));
        this._vpAudio=a2; this.vpPlayId=pid+'2';
        a2.onended=()=>{ if(this.vpPlayId===pid+'2') this.vpPlayId=''; };
        a2.onerror=()=>{ this.showToast('克隆试听加载失败','error'); if(this.vpPlayId===pid+'2') this.vpPlayId=''; };
        a2.play().catch(()=>{});
      };
      a1.ontimeupdate=()=>{ if(a1.currentTime>=4) toClone(); };   // 原声只给 4 秒够建立印象
      a1.onended=toClone;                                          // 原声不足 4 秒的样本自然衔接
      a1.onerror=()=>{ this.showToast('试听失败','error'); if(this.vpPlayId===pid) this.vpPlayId=''; };
      a1.play().catch(()=>{});
    },
    // 收藏：乐观更新 + 服务端持久化（失败回滚）。收藏在长尾网格自动置顶。
    async vpFavToggle(s){
      const want=!this.vpFavs[s.spk];
      if(want) this.vpFavs[s.spk]=1; else delete this.vpFavs[s.spk];
      try{
        const r=await fetch(HUB+'/api/voicepack/favs',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({spk:s.spk, fav:want})});
        if(!r.ok) throw new Error();
      }catch(_){
        if(want) delete this.vpFavs[s.spk]; else this.vpFavs[s.spk]=1;
        this.showToast('收藏保存失败','error');
      }
    },
    get vpFavN(){ return Object.keys(this.vpFavs).length; },
    // 切换克隆试听场景台词：换模板后旧预览自动标陈旧，点「重生成」按新台词合成
    async vpSetPreviewText(tid){
      this.vpBusy='text';
      try{
        const r=await fetch(HUB+'/api/voicepack/preview_text',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({template_id:tid})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'切换失败');
        this.vpPvTpl=tid; this.vpPvText=d.text||''; this.vpPvStaleN=d.stale_n||0; this.vpPvEdit=false;
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
        if(d.stale_n) this.showToast(`已切换「${(this.vpPvTemplates.find(t=>t.id===tid)||{}).label||''}」台词，${d.stale_n} 个试听待重生成`,'success');
      }catch(e){ this.showToast('台词切换失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // P5: 自定义台词——客户贴自己的直播话术（8~120 字），试听=真实开播的那句话
    async vpApplyCustomText(){
      const t=(this.vpPvDraft||'').trim();
      if(t.length<8||t.length>120){ this.showToast('台词需 8~120 字','error'); return; }
      if(t===this.vpPvText){ this.vpPvEdit=false; return; }   // 没改内容不打陈旧标记
      this.vpBusy='text';
      try{
        const r=await fetch(HUB+'/api/voicepack/preview_text',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:t})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'保存失败');
        this.vpPvTpl='custom'; this.vpPvText=d.text||''; this.vpPvStaleN=d.stale_n||0; this.vpPvEdit=false;
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
        if(d.stale_n) this.showToast(`已用自定义台词，${d.stale_n} 个试听待重生成`,'success');
      }catch(e){ this.showToast('台词保存失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // 补齐/重生成精选克隆试听：缺预览或台词陈旧时触发（Fish 零样本），通话中会被 503 让路
    async vpGenPreviews(){
      this.vpBusy='gen';
      try{
        // 后端 gen_previews 已按 pv_stale 逐条重生成；force 会无视陈旧标记全量重做，换台词时只该打 stale 的
        const r=await fetch(HUB+'/api/voicepack/gen_previews',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'生成失败');
        const [vp,pt]=await Promise.all([
          fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null),
          fetch(HUB+'/api/voicepack/preview_text').then(r=>r.json()).catch(()=>null),
        ]);
        if(vp&&vp.rows) this.vpRows=vp.rows;
        if(pt&&pt.ok){ this.vpPvStaleN=pt.stale_n||0; }
        const n=d.generated?.length||0;
        this.showToast(n?`克隆试听已按新台词生成 ${n} 个`+(d.failed?.length?`，失败 ${d.failed.length}`:'')
          :'试听已是最新，无需重生成', n?'success':'');
      }catch(e){ this.showToast('生成失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // P6 上新入库：多段干声 → 后端体检/拼参考音/分析/转写 → EXT 编号进库
    async vpIngest(){
      const inp=this.$refs.vpUpFile;
      const fs=(inp&&inp.files)?[...inp.files]:[];
      if(!fs.length){ this.showToast('先选择音频文件（同一个人的干声）','error'); return; }
      if(fs.length>8){ this.showToast('一次最多 8 个文件','error'); return; }
      const ok=await this._vpSendIngest(fs);
      if(ok&&inp) inp.value='';
    },
    // P9: 上新发送通道（文件选择与麦克风试录共用；返回是否成功）
    async _vpSendIngest(files){
      this.vpUpBusy=true; this.vpUpWarns=[];
      try{
        const fd=new FormData();
        files.forEach(f=>fd.append('files',f));
        fd.append('label',(this.vpUpLabel||'').trim());
        fd.append('gender',this.vpUpGender);
        const r=await fetch(HUB+'/api/voicepack/ingest',{method:'POST',body:fd});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'入库失败');
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
        this.vpUpLabel=''; this.vpUpWarns=d.warns||[];
        const nm=(d.row&&d.row.name)||d.spk;
        this.showToast(`「${nm}」已入库，克隆试听后台生成中（稍后刷新可听）`,'success');
        return true;
      }catch(e){ this.showToast('上新失败: '+((e&&e.message)||e),'error'); return false; }
      finally{ this.vpUpBusy=false; }
    },
    // ── P9 麦克风现场试录：MediaRecorder 录 → 浏览器内解码转 WAV（后端 soundfile 不识 webm/opus）→ 走既有 ingest ──
    async vpRecStart(){
      if(!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia)){
        this.showToast('浏览器拿不到麦克风：需 HTTPS 或本机 127.0.0.1 访问。可改用「选文件」上传','error'); return;
      }
      try{
        // 关掉浏览器三件套（回声消除/降噪/自动增益）：克隆要的是未染色的原始音色
        const stream=await navigator.mediaDevices.getUserMedia({audio:{
          echoCancellation:false, noiseSuppression:false, autoGainControl:false, channelCount:1}});
        this._recStream=stream;
        const mr=new MediaRecorder(stream);
        this._recChunks=[]; this._recMr=mr;
        mr.ondataavailable=e=>{ if(e.data&&e.data.size) this._recChunks.push(e.data); };
        mr.onstop=()=>this._vpRecFinish();
        mr.start();
        this.vpRec='rec'; this.vpRecSec=0; this.vpRecLevel=0;
        this._recTimer=setInterval(()=>{ this.vpRecSec++; if(this.vpRecSec>=30) this.vpRecStop(); },1000);
        // 实时电平条：太小声/爆音当场看得见，不用等后端体检打回
        const ctx=new (window.AudioContext||window.webkitAudioContext)();
        this._recCtx=ctx;
        const an=ctx.createAnalyser(); an.fftSize=512;
        ctx.createMediaStreamSource(stream).connect(an);
        const buf=new Uint8Array(an.frequencyBinCount);
        const tick=()=>{
          if(this.vpRec!=='rec') return;
          an.getByteTimeDomainData(buf);
          let s=0; for(let i=0;i<buf.length;i++){ const v=(buf[i]-128)/128; s+=v*v; }
          this.vpRecLevel=Math.min(100,Math.round(Math.sqrt(s/buf.length)*300));
          requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      }catch(e){ this.showToast('麦克风打开失败: '+((e&&e.message)||e),'error'); }
    },
    vpRecStop(){
      clearInterval(this._recTimer);
      try{ if(this._recMr&&this._recMr.state!=='inactive') this._recMr.stop(); }catch(_){}
    },
    async _vpRecFinish(){
      clearInterval(this._recTimer);
      try{ (this._recStream?this._recStream.getTracks():[]).forEach(t=>t.stop()); }catch(_){}
      this._recStream=null;
      try{
        const blob=new Blob(this._recChunks,{type:(this._recMr&&this._recMr.mimeType)||'audio/webm'});
        const ctx=this._recCtx||new (window.AudioContext||window.webkitAudioContext)();
        const au=await ctx.decodeAudioData(await blob.arrayBuffer());
        // P10 多段累积：录一段存一段（最多 8 段=后端上限），几段短句比一口气念 30 秒更自然
        this.vpRecTakes.push({blob:this._wavEncode(au), sec:Math.max(1,Math.round(au.duration))});
        this.vpRec='idle';
      }catch(e){ this.vpRec='idle'; this.showToast('录音处理失败: '+((e&&e.message)||e),'error'); }
      finally{ try{ if(this._recCtx) this._recCtx.close(); }catch(_){} this._recCtx=null; this._recMr=null; this._recChunks=[]; }
    },
    get vpRecTotal(){ return this.vpRecTakes.reduce((a,t)=>a+t.sec,0); },
    // AudioBuffer → 单声道 16bit PCM WAV（30s@48k ≈ 2.9MB，远低于后端 30MB 上限）
    _wavEncode(au){
      const n=au.length, chs=au.numberOfChannels, sr=au.sampleRate;
      const mono=new Float32Array(n);
      for(let c=0;c<chs;c++){ const d=au.getChannelData(c); for(let i=0;i<n;i++) mono[i]+=d[i]/chs; }
      const buf=new ArrayBuffer(44+n*2), v=new DataView(buf);
      const ws=(o,s)=>{ for(let i=0;i<s.length;i++) v.setUint8(o+i,s.charCodeAt(i)); };
      ws(0,'RIFF'); v.setUint32(4,36+n*2,true); ws(8,'WAVE'); ws(12,'fmt ');
      v.setUint32(16,16,true); v.setUint16(20,1,true); v.setUint16(22,1,true);
      v.setUint32(24,sr,true); v.setUint32(28,sr*2,true); v.setUint16(32,2,true); v.setUint16(34,16,true);
      ws(36,'data'); v.setUint32(40,n*2,true);
      for(let i=0;i<n;i++){ const s=Math.max(-1,Math.min(1,mono[i])); v.setInt16(44+i*2, s<0?s*0x8000:s*0x7FFF, true); }
      return new Blob([buf],{type:'audio/wav'});
    },
    vpRecPlayTake(i){
      const t=this.vpRecTakes[i]; if(!t) return;
      const u=URL.createObjectURL(t.blob), a=new Audio(u);
      a.onended=()=>URL.revokeObjectURL(u); a.play();
    },
    vpRecDelTake(i){ this.vpRecTakes.splice(i,1); },
    vpRecDiscard(){ this.vpRecTakes=[]; this.vpRec='idle'; this.vpRecSec=0; },
    async vpRecSubmit(){
      if(!this.vpRecTakes.length) return;
      if(this.vpRecTotal<5&&!confirm(`一共只录了 ${this.vpRecTotal} 秒，克隆质量可能打折（建议 10~30 秒）。仍要上传吗？`)) return;
      const ok=await this._vpSendIngest(this.vpRecTakes.map((t,i)=>new File([t.blob],`现场试录${i+1}.wav`,{type:'audio/wav'})));
      if(ok) this.vpRecDiscard();
    },
    // ── P10 远程分发：粘贴 URL——zip 直接入库；分发清单展开列表勾选导入 ──
    async vpUrlImport(){
      const u=prompt('粘贴音色包 zip 或分发清单的 URL\n（对方机器清单地址形如 http://对方IP:9000/api/voicepack/catalog）', this.vpCat.url||'');
      if(u===null) return;
      const url=u.trim(); if(!url) return;
      this.vpCat.url=url; this.vpUpBusy=true;
      try{
        const r=await fetch(HUB+'/api/voicepack/import_url',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'导入失败');
        if(d.catalog){
          this.vpCat.items=d.catalog.map(x=>({...x,st:''}));
          this.vpCat.open=true;
          if(!d.catalog.length) this.showToast('清单是空的（对方还没有上新音色）','info');
        }else{
          const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
          if(vp&&vp.rows) this.vpRows=vp.rows;
          if(!this._vpBundleToast(d)){
            const nm=(d.row&&d.row.name)||d.spk;
            this.showToast(d.already?`「${nm}」已在库（同内容不重复导入）`:`已从 URL 导入「${nm}」`,'success');
          }
        }
      }catch(e){ this.showToast('URL 导入失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpUpBusy=false; }
    },
    async vpCatImport(it){
      it.st='busy';
      try{
        const r=await fetch(HUB+'/api/voicepack/import_url',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:it.url})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'失败');
        it.st=d.already?'already':'done';
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
      }catch(e){ it.st='err'; this.showToast(`「${it.name}」导入失败: `+((e&&e.message)||e),'error'); }
    },
    async vpCatImportAll(){
      for(const it of this.vpCat.items){ if(it.st===''||it.st==='err') await this.vpCatImport(it); }
      this.showToast('清单导入完成','success');
    },
    // ── P13 运营小报表 ──
    get vpRepMax(){
      if(!this.vpRep||!this.vpRep.series) return 1;
      return Math.max(1, ...this.vpRep.series.map(s=>Math.max(s.pv+s.au, s.use)));
    },
    async vpRepLoad(){
      this.vpRepBusy=true;
      try{
        const d=await fetch(HUB+'/api/voicepack/report').then(r=>r.json());
        if(d&&d.ok) this.vpRep=d;
      }catch(e){ this.showToast('报表加载失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpRepBusy=false; }
    },
    // ── P12 订阅式自动同步：贴一次清单地址，之后新上新自动进库（账本保证删过的不回填） ──
    async vpSubsLoad(){
      const d=await fetch(HUB+'/api/voicepack/subs').then(r=>r.json()).catch(()=>null);
      if(d&&d.ok){ this.vpSubs.list=d.subs||[]; this.vpSubs.autoHours=d.auto_hours||6; this.vpSubs.n=(d.subs||[]).length; }
    },
    _vpSyncToast(res,prefix){
      if(!res) { this.showToast((prefix||'')+'已登记，稍后自动同步','info'); return; }
      if(res.error){ this.showToast((prefix||'')+'首轮同步没成（'+res.error+'），定时会自动重试','error'); return; }
      this.showToast((prefix||'')+`同步完成：新入库 ${res.new}、已在库 ${res.already}、跳过 ${res.skip}`+(res.fail?`、失败 ${res.fail}`:''),
                     res.fail?'error':'success');
    },
    async vpSubAdd(url){
      const u=(url||this.vpSubs.newUrl||'').trim();
      if(!u){ this.showToast('先粘贴清单地址（http://对方IP:9000/api/voicepack/catalog）','error'); return; }
      this.vpSubs.busy=true;
      try{
        const r=await fetch(HUB+'/api/voicepack/subs',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:u})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'订阅失败');
        this.vpSubs.newUrl='';
        this._vpSyncToast(d.first_sync,'已订阅。');
        await this.vpSubsLoad();
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
      }catch(e){ this.showToast('订阅失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpSubs.busy=false; }
    },
    async vpSubRemove(s){
      if(!confirm(`退订「${s.label||s.url}」？\n\n已同步进库的音色保留；同步账本也保留（重新订阅不会把删过的塞回来）。`)) return;
      try{
        const r=await fetch(HUB+'/api/voicepack/subs/remove',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({url:s.url})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'退订失败');
        await this.vpSubsLoad();
        this.showToast('已退订','success');
      }catch(e){ this.showToast('退订失败: '+((e&&e.message)||e),'error'); }
    },
    async vpSubSync(s){
      this.vpSubs.busy=true;
      try{
        const r=await fetch(HUB+'/api/voicepack/subs/sync',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(s?{url:s.url}:{})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'同步失败');
        for(const res of (d.results||[])){ this._vpSyncToast(res.ok?res:{error:res.error}); }
        await this.vpSubsLoad();
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
      }catch(e){ this.showToast('同步失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpSubs.busy=false; }
    },
    // ── P9/P11 音色包分发：EXT 打包下载（口令感知）/ 整库打包 / 导入他机导出的包 ──
    async _vpDistFetch(path){
      let tok='';
      if(this.vpDistProtected){
        try{ tok=localStorage.getItem('vp_dist_token')||''; }catch(_){}
        if(!tok){
          const t=prompt('本机设置了分发口令（VP_DIST_TOKEN），输入口令以导出：');
          if(t===null) return null;
          tok=t.trim();
        }
      }
      const r=await fetch(HUB+path+(tok?((path.includes('?')?'&':'?')+'token='+encodeURIComponent(tok)):''));
      if(r.status===401){ try{ localStorage.removeItem('vp_dist_token'); }catch(_){} throw new Error('分发口令不对（再点一次重新输入）'); }
      if(!r.ok) throw new Error('HTTP '+r.status);
      if(tok){ try{ localStorage.setItem('vp_dist_token',tok); }catch(_){} }   // 验证通过才缓存
      return r;
    },
    async _vpDownload(path, fallbackName){
      try{
        const r=await this._vpDistFetch(path);
        if(!r) return;
        const blob=await r.blob();
        const cd=r.headers.get('content-disposition')||'';
        const m=/filename\*=UTF-8''([^;]+)/.exec(cd), m2=/filename="([^"]+)"/.exec(cd);
        const name=m?decodeURIComponent(m[1]):(m2?m2[1]:fallbackName);
        const u=URL.createObjectURL(blob);
        const a=document.createElement('a'); a.href=u; a.download=name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(()=>URL.revokeObjectURL(u),3000);
      }catch(e){ this.showToast('导出失败: '+((e&&e.message)||e),'error'); }
    },
    vpExport(s){ this._vpDownload('/api/voicepack/'+encodeURIComponent(s.spk)+'/export', s.spk+'.zip'); },
    vpExportAll(){ this._vpDownload('/api/voicepack/export_all','voicebundle.zip'); },
    // P11 整库 bundle 导入结果汇总（单包结果返回 false 走原有 toast）
    _vpBundleToast(d){
      if(!d.bundle) return false;
      const okN=d.bundle.filter(x=>x.ok&&!x.already).length;
      const oldN=d.bundle.filter(x=>x.ok&&x.already).length;
      const bad=d.bundle.filter(x=>!x.ok);
      this.showToast(`整库包导入完成：新入库 ${okN}、已在库 ${oldN}`+(bad.length?`、失败 ${bad.length}（${bad[0].error||''}）`:''),
                     bad.length?'error':'success');
      return true;
    },
    async vpImportPack(){
      const inp=this.$refs.vpPackFile;
      const f=inp&&inp.files&&inp.files[0];
      if(!f){ this.showToast('先选择音色包 zip 文件','error'); return; }
      this.vpUpBusy=true;
      try{
        const fd=new FormData();
        fd.append('file',f);
        const r=await fetch(HUB+'/api/voicepack/import_pack',{method:'POST',body:fd});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'导入失败');
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows) this.vpRows=vp.rows;
        inp.value='';
        if(!this._vpBundleToast(d)){
          const nm=(d.row&&d.row.name)||d.spk;
          this.showToast(d.already?`「${nm}」已在库（同内容不重复导入）`:`音色包已导入为「${nm}」，试听后台生成中`,'success');
        }
      }catch(e){ this.showToast('音色包导入失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpUpBusy=false; }
    },
    // 删上新（软删；EXT 专属——内置 218 人只读）。导入过的角色内嵌了副本，不受影响
    async vpExtDelete(s){
      if(!confirm(`删除上新音色「${s.name}」？\n\n软删进回收目录可手工找回；`+
                  (s.imported?`已导入的角色「${s.imported}」内嵌了副本，不受影响。`:'未导入过任何角色。'))) return;
      this.vpBusy=s.spk;
      try{
        const r=await fetch(HUB+'/api/voicepack/'+encodeURIComponent(s.spk),{method:'DELETE'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'删除失败');
        this.vpRows=this.vpRows.filter(x=>x.spk!==s.spk);
        delete this.vpFavs[s.spk];
        this.vpTrashN++;
        if(this.vpTrashOpen) this.vpTrashLoad();
        this.showToast(d.imported_profile?`已删除；角色「${d.imported_profile}」不受影响`:'已删除（回收目录可找回）','success');
      }catch(e){ this.showToast('删除失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // P8 设/撤主打：写 featured.json（与内置精选同表）。设为主打的音色导入后在同传下拉升入 ⭐精选组
    async vpExtFeature(s){
      const want=!s.featured;
      let tagline='';
      if(want){
        const t=prompt('一句话卖点（显示在卡片上和同传下拉分组里，留空用默认）', s.scene||'');
        if(t===null) return;                    // 取消=不动
        tagline=t.trim();
      }
      this.vpBusy=s.spk;
      try{
        const r=await fetch(HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/feature',{
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({featured:want, tagline})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'操作失败');
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows){ this.vpRows=vp.rows; this.vpTier=vp.tier||this.vpTier; }
        this.showToast(want?`「${s.name}」已设为主打，同传下拉将进 ⭐精选组`:'已取消主打','success');
      }catch(e){ this.showToast('主打设置失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // P7 补转写：上新时 STT 离线导致参考文本缺失 → STT 恢复后一键回填（顺带同步已导入角色+重出试听）
    async vpTranscribe(s){
      this.vpBusy=s.spk;
      try{
        const r=await fetch(HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/transcribe',{method:'POST'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'转写失败');
        s.has_ref_text=true;
        this.showToast(`已补转写 ${d.text.length} 字`+(d.profile_updated?`，角色「${d.profile_updated}」已同步`:'')+'，试听后台重生成中','success');
      }catch(e){ this.showToast('补转写失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // P7 上新回收目录：还原（文件搬回+索引重建）/ 彻底删除
    async vpTrashLoad(){
      try{
        const d=await fetch(HUB+'/api/voicepack/ext_trash').then(r=>r.json());
        this.vpTrashItems=d.items||[]; this.vpTrashN=this.vpTrashItems.length;
      }catch(_){ this.vpTrashItems=[]; }
    },
    async vpTrashRestore(it){
      this.vpBusy='trash:'+it.spk;
      try{
        const r=await fetch(HUB+'/api/voicepack/ext_trash/restore',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({ts:String(it.ts), spk:it.spk})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'还原失败');
        const vp=await fetch(HUB+'/api/voicepack').then(r=>r.json()).catch(()=>null);
        if(vp&&vp.rows){ this.vpRows=vp.rows; this.vpTrashN=vp.ext_trash_n||0; }
        await this.vpTrashLoad();
        this.showToast(`「${(d.row&&d.row.name)||it.name}」已还原到音色库`,'success');
      }catch(e){ this.showToast('还原失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    async vpTrashPurge(it){
      const msg=it?`彻底删除「${it.name}」（${it.files.length} 个文件）？此操作不可逆。`
                  :`清空上新回收目录（${this.vpTrashItems.length} 项）？此操作不可逆。`;
      if(!confirm(msg)) return;
      this.vpBusy='trash:'+(it?it.spk:'all');
      try{
        const body=it?{ts:String(it.ts), spk:it.spk}:{};
        const r=await fetch(HUB+'/api/voicepack/ext_trash/purge',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清理失败');
        await this.vpTrashLoad();
        this.showToast('已彻底删除','success');
      }catch(e){ this.showToast('清理失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    async vpImport(s){
      this.vpBusy=s.spk;
      try{
        const r=await fetch(HUB+'/api/voicepack/'+encodeURIComponent(s.spk)+'/import',{
          method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'导入失败');
        s.imported=d.name;
        this.showToast(d.already?`「${d.name}」已在角色库`:`已导入「${d.name}」，角色库可直接使用`,'success');
        await this.loadProfiles();   // 角色列表立即出现新角色（不用手动刷新）
      }catch(e){ this.showToast('导入失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vpBusy=''; }
    },
    // ── 批量勾选：勾选集按「当前还存在的条目」求交集，刷新后幽灵键自动失效 ──
    vaSelToggle(k){ if(this.vaSel[k]) delete this.vaSel[k]; else this.vaSel[k]=1; },
    get vaTrashSel(){ return this.vaTrash.filter(it=>this.vaSel['t:'+it.kind+':'+it.name]); },
    get vaOrphanSel(){ return this.vaAssets.filter(a=>a.kind==='clone'&&a.orphan&&this.vaSel[a.id]); },
    // 全选=当前搜索命中的条目；再点一次取消全部（搜索态下只圈中看得见的，避免误伤）
    vaTrashSelAll(){
      const all=this.vaTrashF.length && this.vaTrashF.every(it=>this.vaSel['t:'+it.kind+':'+it.name]);
      if(all){ this.vaSel={}; return; }
      const m={...this.vaSel}; this.vaTrashF.forEach(it=>m['t:'+it.kind+':'+it.name]=1); this.vaSel=m;
    },
    // 批量还原：冲突策略固定「改名共存」——批量流程里逐条问会打断节奏，且改名零风险
    async vaTrashRestoreSel(){
      const sel=this.vaTrashSel; if(!sel.length) return;
      if(!confirm(`还原选中的 ${sel.length} 项？\n\n同名冲突会自动改名共存，不覆盖现有文件。`)) return;
      this.vaBusy='trash:batch';
      let ok=0, renamed=0, fail=0;
      for(const it of sel){
        try{
          const r=await fetch(HUB+'/api/asset_trash/restore',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({kind:it.kind, name:it.name, on_conflict:'rename'})});
          const d=await r.json();
          if(!r.ok||!d.ok) throw 0;
          ok++; if(d.renamed) renamed++;
        }catch(_){ fail++; }
      }
      this.vaBusy=''; this.vaSel={};
      this.showToast(`已还原 ${ok} 项`+(renamed?`（${renamed} 项因同名改名）`:'')+(fail?`，${fail} 项失败`:''), fail?'error':'success');
      await this.vaRefresh();
    },
    async vaTrashPurgeSel(){
      const sel=this.vaTrashSel; if(!sel.length) return;
      const bytes=sel.reduce((s,i)=>s+(i.size||0),0);
      if(!confirm(`彻底删除选中的 ${sel.length} 项（${this.vaFmtSize(bytes)}）？此操作不可逆。`)) return;
      this.vaBusy='trash:batch';
      try{
        const r=await fetch(HUB+'/api/asset_trash/purge',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({items:sel.map(i=>({kind:i.kind, name:i.name}))})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清理失败');
        this.showToast(`已彻底删除 ${d.purged} 项`,'success');
      }catch(e){ this.showToast('批量彻删失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; this.vaSel={}; await this.vaRefresh(); }
    },
    // 回收站克隆音试听：还原/彻删前先听一耳朵（模型不是音频，UI 不给按钮）
    vaTrashPlay(it){
      if(this._vaAudio){ try{ this._vaAudio.pause(); }catch(_){} this._vaAudio=null; }
      const tid='trash:'+it.name;
      if(this.vaPlayId===tid){ this.vaPlayId=''; return; }   // 再点同一条=停止
      const au=new Audio(HUB+'/api/asset_trash/audio?kind='+encodeURIComponent(it.kind)+'&name='+encodeURIComponent(it.name));
      this._vaAudio=au; this.vaPlayId=tid;
      au.onended=()=>{ if(this.vaPlayId===tid) this.vaPlayId=''; };
      au.onerror=()=>{ this.showToast('回收站试听失败','error'); if(this.vaPlayId===tid) this.vaPlayId=''; };
      au.play().catch(()=>{});
    },
    // 自动清理策略：读取（打开面板时）与保存（改动即存；保存即执行一次，cleaned>0 就提示清了多少）
    async trashPolicyLoad(){
      try{
        const d=await fetch(HUB+'/api/trash_policy').then(r=>r.json());
        if(d.ok && d.policy) this.trashPolicy={...this.trashPolicy, ...d.policy};
      }catch(_){}
    },
    async trashPolicySave(){
      if(this.trashPolicyBusy) return;
      this.trashPolicyBusy=true;
      try{
        const p=this.trashPolicy;
        const body={auto_clean:!!p.auto_clean,
                    max_mb:Math.max(1, Math.round(+p.max_mb||500)),
                    older_days:Math.max(0, Math.round(+p.older_days||30))};
        const r=await fetch(HUB+'/api/trash_policy',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'保存失败');
        this.trashPolicy={...this.trashPolicy, ...d.policy};
        if(d.cleaned>0){
          this.showToast(`策略已保存，当场自动清理了 ${d.cleaned} 项旧回收`,'success');
          await this.vaRefresh();
        }else{
          this.showToast(p.auto_clean?'自动清理已开启':'自动清理已关闭','info');
        }
      }catch(e){ this.showToast('策略保存失败: '+((e&&e.message)||e),'error'); await this.trashPolicyLoad(); }
      finally{ this.trashPolicyBusy=false; }
    },
    // 回收站里 ≥30 天前删除的部分（面板按钮据此显隐；横幅走 assetHealth.trash_old_*）
    get vaTrashOld(){
      const cutoff=Date.now()/1000 - 30*86400;
      const old=(this.vaTrash||[]).filter(i=>(i.deleted_at||0)<=cutoff);
      return {n:old.length, bytes:old.reduce((s,i)=>s+(i.size||0),0)};
    },
    // 只清 30 天前删的（近期删除保留当安全网）；横幅/面板共用
    async vaPurgeTrashOld(){
      const h=this.assetHealth||{};
      const n=this.vaShow ? this.vaTrashOld.n : (h.trash_old_n||0);
      const bytes=this.vaShow ? this.vaTrashOld.bytes : (h.trash_old_bytes||0);
      if(!confirm(`彻底删除回收站里 30 天前删除的 ${n} 项（${this.vaFmtSize(bytes)}）？\n\n近 30 天删的会保留，此操作不可逆。`)) return;
      this.vaBusy='trash:old';
      try{
        const r=await fetch(HUB+'/api/asset_trash/purge',{method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify({older_than_days:30})});
        const d=await r.json();
        if(!r.ok||!d.ok) throw new Error(d.detail||'清理失败');
        this.showToast(`已彻底删除 ${d.purged} 项（30 天前删除的）`,'success');
        await this.vaRefresh();   // 末尾顺带刷新 assetHealth → 横幅自动消退
      }catch(e){ this.showToast('清理失败: '+((e&&e.message)||e),'error'); }
      finally{ this.vaBusy=''; }
    },

    // ── RVC ──
    async startRvcApi() {
      const d=await fetch(HUB+'/realtime/start_rvc_api',{method:'POST'}).then(r=>r.json());
      this.rvcMsg=d.ok?'✅ RVC API 已启动':'❌ '+d.detail;
      this.rvcOk=d.ok;
      if(d.ok){
        this.services.rvc = true;
        setTimeout(()=>this.refreshServices(),1000);
        setTimeout(()=>this.rvcRefreshDevices(),3000);
      }
      setTimeout(()=>this.rvcMsg='',4000);
    },

    async rvcRefreshDevices() {
      this._lastDevRefreshTs=Date.now();   // P2-1b 漂移兜底轮询以「最近一次任意来源的枚举」计时
      try {
        const d=await fetch(HUB+'/rvc/devices').then(r=>r.json());
        if(d.ok){
          // P9 双保险去重：后端已按名去重，这里再兜一层（旧 hub / RVC 直连等场景），
          // 重名会撞 x-for :key → Alpine 移动节点时崩 'reading after'，整页交互挂死
          this.rvcInputDevices=[...new Set(d.input_devices||[])];
          this.rvcOutputDevices=[...new Set(d.output_devices||[])];
          this.rvcInputsEx=d.inputs_ex||null;    // P0 人话层（后端 device_enum 单一真相；无则回退旧渲染）
          this.rvcOutputsEx=d.outputs_ex||null;
          this.rvcPrefs=d.prefs||null;           // P2-1 已存偏好 + 在线状态
          this.rvcFreshNote=d.fresh_note||'';    // P4-3 转换中新插设备（🆕）说明行
          const ipick=(this.rvcInputsEx&&this.rvcInputsEx.pick&&this.rvcInputsEx.pick.value)||'';
          const opick=(this.rvcOutputsEx&&this.rvcOutputsEx.pick&&this.rvcOutputsEx.pick.value)||'';
          if(!this.rvc.inputDevice && this.rvcInputDevices.length) {
            // P0: 后端结构化推荐优先（与下拉显示同一真相）；结构化不可用回退旧关键词链(P23-1)
            this.rvc.inputDevice = ipick ||
                                    this.rvcInputDevices.find(x=>x.toLowerCase().includes('droid') && x.includes('MME')) ||
                                    this.rvcInputDevices.find(x=>x.toLowerCase().includes('droid')) ||
                                    this.rvcInputDevices.find(x=>x.includes('Hands-Free') || x.includes('Bluetooth') || x.includes('蓝牙')) ||
                                    this.rvcInputDevices.find(x=>x.includes('麦克风') || x.includes('Mic')) ||
                                    this.rvcInputDevices[0];
          }
          if(!this.rvc.outputDevice && this.rvcOutputDevices.length) {
            this.rvc.outputDevice = opick ||
                                    this.rvcOutputDevices.find(x=>x.includes('CABLE Input') && x.includes('MME')) ||
                                    this.rvcOutputDevices.find(x=>x.includes('CABLE Input')) ||
                                    this.rvcOutputDevices[0];
          }
          // 设备单一真相：自动选好的麦/声卡同步给开播音频源，专家设备选择器与 RVC 共用一套
          if(this.rvc.inputDevice && !this.audioInput) this.audioInput=this.rvc.inputDevice;
          if(this.rvc.outputDevice && !this.audioOutput) this.audioOutput=this.rvc.outputDevice;
          // 归一：历史保存的是其它 hostapi 变体串时 → 映射到合并后的规范值，简化视图才能正确选中
          const ni=this.audioDevCanon('in', this.audioInput);   if(ni!==this.audioInput){ this.audioInput=ni; this.rvc.inputDevice=ni; }
          const no=this.audioDevCanon('out', this.audioOutput); if(no!==this.audioOutput){ this.audioOutput=no; this.rvc.outputDevice=no; }
          this.applyAudioPrefs();                 // P2-1 偏好恢复 / 缺席回退 / 插回自愈（在自动填充之上）
          this._prevAudioIn=this.audioInput; this._prevAudioOut=this.audioOutput;
        } else {
          this.rvcMsg='❌ 设备列表获取失败: '+(d.detail||'unknown');
          this.rvcOk=false;
        }
      } catch(e){ this.rvcMsg='❌ 设备列表获取失败: '+e; this.rvcOk=false; }
    },
    // ── P2-1 偏好套用（每次枚举后跑一遍，幂等）──
    //   语义：偏好=用户最后一次明确选择（粘性）。在线→选中它；不在线→临时用推荐顶上（不改写偏好），
    //   toast 告知一次并驻留警告条；插回来→自动切回偏好 + toast。显式空串偏好=「跟随系统默认」。
    applyAudioPrefs(){
      const pf=this.rvcPrefs; if(!pf) return;
      const sides=[
        {k:'in',  set:pf.input_set,  want:pf.input,  present:pf.input_present,  canon:pf.input_canon,  label:pf.input_label,
         pick:(this.rvcInputsEx&&this.rvcInputsEx.pick)||{}, what:'麦克风',
         get:()=>this.audioInput,  put:v=>{ this.audioInput=v;  this.rvc.inputDevice=v; }},
        {k:'out', set:pf.output_set, want:pf.output, present:pf.output_present, canon:pf.output_canon, label:pf.output_label,
         pick:(this.rvcOutputsEx&&this.rvcOutputsEx.pick)||{}, what:'输出',
         get:()=>this.audioOutput, put:v=>{ this.audioOutput=v; this.rvc.outputDevice=v; }},
      ];
      for(const s of sides){
        // 8 秒手护窗：刚手动改选完，别让「改选前就已在途的刷新」用旧偏好把新选择顶回去
        const justPicked=(Date.now()-(this._userPickTs[s.k]||0))<8000;
        if(!s.set){ this.devLost[s.k]=null; this.devBack[s.k]=null; this._devFlowSeen[s.k]=false; continue; }
        if(!s.want){ this.devLost[s.k]=null; this.devBack[s.k]=null; this._devFlowSeen[s.k]=false; if(s.get()!=='' && !justPicked) s.put(''); continue; }   // 用户明确选了跟随系统默认
        if(s.present){
          const wasLost=!!this.devLost[s.k];
          this.devLost[s.k]=null;
          this._devFlowSeen[s.k]=false;   // P4-2 缺席 episode 结束，下次缺席重新计一次曝光
          const v=s.canon||s.want;
          // P3-1b 下拉恢复了 ≠ 拾音流跟上了：变声若还跑在顶替设备上，驻留「切回首选」CTA。
          //   刻意不自动重启——重启有 ~2s 声音间隙，正在说话时突然切会吓到主播，时机让人挑。
          const run=this._rvcRunDevs[s.k];
          const stillOnSub=this.rvcActive && run && this.audioDevCanon(s.k,run)!==this.audioDevCanon(s.k,v);
          this.devBack[s.k]=stillOnSub ? {value:v, label:s.label||v, run} : null;
          if(s.get()!==v && !justPicked){
            s.put(v);
            if(wasLost) this.showToast('🔌 「'+(s.label||v)+'」插回来了，'+(stillOnSub?'已恢复选择；变声还跑在顶替设备上，可一键切回':'已自动恢复使用'),'success');
          }
        } else {
          this.devBack[s.k]=null;
          const firstMiss=!this.devLost[s.k];
          this.devLost[s.k]={value:s.want, label:s.label||s.want};
          const pickV=(s.pick&&s.pick.value)||'';
          // 只在当前选择「指着缺席设备/为空/已不在枚举里」时才顶替，绝不覆盖用户手动改选的其它在线设备
          const cur=s.get();
          const curAlive=cur && !!this.audioDevMeta(s.k, cur);
          if(pickV && !curAlive) s.put(pickV);
          if(firstMiss){
            const fb=pickV?('，已先用「'+((s.pick&&s.pick.label)||pickV)+'」顶上；插回后自动恢复'):'，请检查连接后点「🔄 刷新设备」';
            this.showToast('⚠ 上次用的'+s.what+'「'+(s.label||s.want)+'」没检测到'+fb, 'error');
          }
          // P4-2 漏斗曝光：热切 CTA 真的亮出来才算（每个缺席 episode 只记一次）
          if(this.devHotOffer(s.k) && !this._devFlowSeen[s.k]){
            this._devFlowSeen[s.k]=true; this._devFlow('expose', s.k, 'strip');
          }
        }
      }
    },
    // 保存设备偏好（用户在下拉里明确选择时调用；''=跟随系统默认也要记）。本地镜像同步，防下次刷新前状态错位。
    // src = 改动来源（进后端 P3-3 审计轨迹：排查「设备怎么自己变了」直接看账）
    async saveAudioPref(side, value, src='ui-pick'){
      const kind = side==='input'?'in':'out';
      if(this.rvcPrefs){
        this.rvcPrefs[side]=value||'';
        this.rvcPrefs[side+'_set']=true;
        this.rvcPrefs[side+'_present']=!!(value && this.audioDevMeta(kind, value));
        this.rvcPrefs[side+'_canon']=value?this.audioDevCanon(kind, value):'';
        this.rvcPrefs[side+'_label']=value?this.audioDevLabel(kind, value):'';
      }
      this.devLost[kind]=null;   // 新选择即新偏好，旧缺席状态随之作废
      try{
        await fetch(HUB+'/api/audio/prefs',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({[side]: value||'', src})});
      }catch(_){/* 保存失败不打断选择；下次选择再试 */}
    },
    // ── P3-1 开播中拔插热处理：一键把在跑的变声切到推荐设备并重启拾音流 ──
    //   只在「偏好设备缺席 + 变声正在跑 + 在跑的正是那只(或在跑设备自己也没了)」时提供：
    //   不在跑时下拉已被自愈顶替，下次开始变声自然用新设备；热切成功后 CTA 退场、警告条保留(纯信息)。
    //   刻意不写偏好（临时顶替语义）：原设备插回后 applyAudioPrefs 仍会自动切回来。
    devHotOffer(kind){
      const l=this.devLost&&this.devLost[kind];
      if(!l || !this.rvcActive) return false;
      const run=this._rvcRunDevs[kind];
      // 在跑设备未知(''=系统默认也算) / 正是缺席那只 / 它自己也已不在枚举里 → 需要热切
      return !run || run===l.value || !this.audioDevMeta(kind, run);
    },
    // P4-2 漏斗埋点（fire-and-forget，丢了不影响功能）：缺席警告条曝光→点击→成败
    _devFlow(ev,kind,src){
      try{
        fetch(HUB+'/api/metrics/devflow',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({ev,kind:(kind==='out'?'out':'in'),src:src||''})}).catch(()=>{});
      }catch(_){}
    },
    async devHotSwitch(src, kind, dev){
      if(this.devHotBusy) return;
      this.devHotBusy=true;
      const k=(kind==='out')?'out':'in';
      const from=src||'strip';
      this._devFlow('click', k, from);
      try{
        // dev 显式指定（P4-3 🆕 设备一键切）时只下发那一侧；否则让后端走 显式>偏好>推荐 链
        const body={src:from}; if(dev){ body[k==='out'?'output':'input']=dev; }
        const d=await fetch(HUB+'/rvc/hot_switch',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(body)}).then(r=>r.json());
        if(d.input){ this.audioInput=d.input; this.rvc.inputDevice=d.input; this._prevAudioIn=d.input; }
        if(d.output){ this.audioOutput=d.output; this.rvc.outputDevice=d.output; this._prevAudioOut=d.output; }
        if(d.was_running) this.rvcActive=!!d.started;   // 重启失败=转换已停，让按钮态如实反映
        if(d.ok){
          if(d.started) this._rvcRunDevs={in:d.input||'', out:d.output||''};
          this.devBack={in:null,out:null};   // 已切到当前最优线路；若仍有错位，下次枚举会重新亮起
          this.autoSwFail=null;              // P8-1 任何一次成功切换都解除自救卡（危机已过）
          this._devFlow('ok', k, from);
          this.showToast(d.was_running
            ? ('⚡ 已切到「'+(d.input_label||d.input)+' → '+(d.output_label||d.output)+'」并重启变声')
            : '线路已切到推荐设备（变声当前未在跑，下次开始即用）','success');
          // P7-2 趁热打铁：刚成功的这次可能正好凑满「建议开启自动热切」的战绩门槛——立刻复查一次
          setTimeout(()=>this.checkAutoSwAdvice(), 800);
        } else {
          this._devFlow('fail', k, from);
          this.showToast('切换失败：'+(d.detail||'未知错误')+(d.step==='start'?'（变声已停止，请手动点「开始变声」）':''),'error');
        }
      }catch(e){ this._devFlow('fail', k, from); this.showToast('切换失败：'+String(e),'error'); }
      finally{ this.devHotBusy=false; }
    },

    // ══ P0/P1 设备人话层 helpers（数据源 rvcInputsEx/rvcOutputsEx，整块删除=回退旧渲染）══
    devToggleShowAll(){
      this.devShowAll=!this.devShowAll;
      try{ localStorage.setItem('hub_dev_show_all', this.devShowAll?'1':'0'); }catch(_){}
    },
    // 按原始名(含任意 hostapi 变体)找结构化条目
    audioDevMeta(kind, value){
      const ex = kind==='in'?this.rvcInputsEx:this.rvcOutputsEx;
      if(!ex || !value) return null;
      for(const d of (ex.devices||[])){
        if(d.value===value || (d.variants||[]).includes(value)) return d;
      }
      return null;
    },
    audioDevLabel(kind, value){ const m=this.audioDevMeta(kind,value); return m?m.label:(value||''); },
    // 变体串 → 合并后的规范 value（历史配置里存的可能是 WASAPI 行）
    audioDevCanon(kind, value){ const m=this.audioDevMeta(kind,value); return m?m.value:value; },
    // 简化视图分组：隐藏组默认不出现；当前选中值即使属于隐藏项也保留（否则下拉显示空白）
    audioGroups(kind){
      const ex = kind==='in'?this.rvcInputsEx:this.rvcOutputsEx;
      if(!ex) return [];
      const cur = kind==='in'?(this.audioInput||this.rvc.inputDevice):(this.audioOutput||this.rvc.outputDevice);
      const groups=[];
      for(const g of (ex.groups||[])){
        const items=(ex.devices||[]).filter(d=>d.group===g.key &&
          (!d.hidden || d.value===cur || (d.variants||[]).includes(cur)));
        if(items.length) groups.push({key:g.key, label:g.label, items});
      }
      return groups;
    },
    // 「常用设备 6 个（已折叠 49 个虚拟/系统项，合并 36 条重复）」——把吓人的 91 变成可信的事实
    audioCountLine(kind){
      const ex = kind==='in'?this.rvcInputsEx:this.rvcOutputsEx;
      const legacy=(kind==='in'?this.rvcInputDevices:this.rvcOutputDevices)||[];
      if(!ex) return legacy.length?('已检测 '+legacy.length+' 个'+(kind==='in'?'输入':'输出')):'';
      if(this.devShowAll) return '原始设备 '+legacy.length+' 条（系统未合并视图，供排障）';
      const devs=ex.devices||[];
      const vis=devs.filter(d=>!d.hidden).length;
      const hid=devs.length-vis;
      const dup=Math.max(0, (ex.raw_count||0)-(ex.merged_count||0));
      let s='常用设备 '+vis+' 个';
      if(hid>0) s+='，折叠 '+hid+' 个虚拟/系统项';
      if(dup>0) s+='，合并 '+dup+' 条重复';
      return s;
    },
    // 推荐行动态化（P0-4）：只推荐真检测到的，附一句为什么
    audioPickHint(kind){
      const ex = kind==='in'?this.rvcInputsEx:this.rvcOutputsEx;
      if(!ex || !ex.pick) return '';
      return ex.pick.value ? ('推荐 '+ex.pick.label+' — '+ex.pick.reason) : ('⚠ '+ex.pick.reason);
    },
    // 选择变更（P0-3 危险确认）：选中"电脑内放/CABLE回收口"这类毒选项 → 讲清后果，确认才放行
    onPickAudioInput(){
      const m=this.audioDevMeta('in', this.audioInput);
      if(m && m.danger && !confirm('「'+m.label+'」\n'+m.danger+'\n\n确定要用它当麦克风吗？')){
        this.audioInput=this._prevAudioIn||''; this.rvc.inputDevice=this.audioInput; return;
      }
      this._prevAudioIn=this.audioInput;
      this.rvc.inputDevice=this.audioInput;
      this.micTest.res=null;   // 换了设备，旧试音结论作废
      this._userPickTs.in=Date.now();
      this.saveAudioPref('input', this.audioInput);   // P2-1 记住这次明确选择（''=跟随系统默认也记）
      // P4-3 选中的是转换启动后新插的设备(🆕)：正在变声时需重启拾音流才真生效——给一键
      if(m && m.fresh && this.rvcActive &&
         confirm('「'+m.label+'」是变声启动后新插的设备，需要重启变声（约 2 秒断声）才能用上。\n现在就切换吗？\n\n点「取消」＝先保存选择，下次开始变声时生效。')){
        this.devHotSwitch('fresh-pick','in', this.audioInput);
      }
    },
    onPickAudioOutput(){
      const m=this.audioDevMeta('out', this.audioOutput);
      if(m && m.danger && !confirm('「'+m.label+'」\n'+m.danger+'\n\n确定继续吗？')){
        this.audioOutput=this._prevAudioOut||''; this.rvc.outputDevice=this.audioOutput; return;
      }
      this._prevAudioOut=this.audioOutput;
      this.rvc.outputDevice=this.audioOutput;
      this.outTest.res=null;
      this._userPickTs.out=Date.now();
      this.saveAudioPref('output', this.audioOutput);   // P2-1
      if(m && m.fresh && this.rvcActive &&
         confirm('「'+m.label+'」是变声启动后新插的设备，需要重启变声（约 2 秒断声）才能用上。\n现在就切换吗？\n\n点「取消」＝先保存选择，下次开始变声时生效。')){
        this.devHotSwitch('fresh-pick','out', this.audioOutput);
      }
    },
    // P2-1b 拔插自愈的丢失警告条（设备卡/声音线路卡驻留展示；toast 只弹一次，这条一直在）
    devLostLine(kind){
      const l=this.devLost&&this.devLost[kind]; if(!l) return '';
      const what=kind==='in'?'麦克风':'输出设备';
      const cur=kind==='in'?(this.audioInput||this.rvc.inputDevice):(this.audioOutput||this.rvc.outputDevice);
      const curLbl=cur?(this.audioDevLabel(kind,cur)||cur):'系统默认';
      return '上次用的'+what+'「'+l.label+'」没检测到，现在用的是「'+curLbl+'」。设备插回后会自动恢复。';
    },
    // P3-1b 首选已插回、但在跑的变声还挂在顶替设备上：驻留提示 + 「切回首选」（时机让主播挑）
    devBackLine(kind){
      const b=this.devBack&&this.devBack[kind]; if(!b) return '';
      const what=kind==='in'?'麦克风':'输出设备';
      const runLbl=this.audioDevLabel(kind,b.run)||b.run;
      return '首选'+what+'「'+b.label+'」已插回，但变声还跑在「'+runLbl+'」上。';
    },
    // 输出没走直播声卡时的常驻软警告（拍板决策#2：不硬拦，讲清"观众听不到"）
    audioOutWarn(){
      const v=this.audioOutput||this.rvc.outputDevice;
      if(!v) return '';
      const m=this.audioDevMeta('out', v);
      if(m ? m.group==='live' : /cable input/i.test(v)) return '';
      const nm=m?m.label:v;
      return '当前输出是「'+nm+'」：只有你自己听得到，观众听不到变声。要让观众听到，请选「直播声卡 (CABLE Input)」。';
    },
    // ── P1-1 试音三件套 ──
    async micTestRun(){
      if(this.micTest.busy) return;
      this.micTest.busy=true; this.micTest.res=null; this.micTest.url='';
      const dev=this.audioInput||this.rvc.inputDevice||'';
      this.showToast('试音中：对着「'+(this.audioDevLabel('in',dev)||'系统默认麦克风')+'」说句话（3 秒）…','info');
      try{
        const d=await fetch(HUB+'/api/audio/mic_test?secs=3&device='+encodeURIComponent(dev)).then(r=>r.json());
        this.micTest.res=d;
        if(d.ok && d.wav_b64) this.micTest.url='data:audio/wav;base64,'+d.wav_b64;
        if(d.ok){   // P2-2 试音摘要持久化：结论进开播就绪度（黄灯不拦），换设备/过期自动失效
          const rec={level:d.level, verdict:d.verdict, ts:Date.now(), device:dev||''};
          this._micTestSaved=rec;
          try{ localStorage.setItem('hub_mic_test', JSON.stringify(rec)); }catch(_){}
        }
      }catch(e){ this.micTest.res={ok:false, detail:String(e)}; }
      finally{ this.micTest.busy=false; }
    },
    // P2-2 就绪度用的「最近一次试音」裁决：必须是"当前这只麦"的记录且 7 天内，否则视为没试过
    micTestJudge(){
      const cur=this.audioDevCanon('in', this.audioInput||this.rvc.inputDevice||'')||'';
      const rec=this._micTestSaved;
      if(!rec || !rec.ts || (Date.now()-rec.ts)>7*86400e3) return {state:'none'};
      const recDev=this.audioDevCanon('in', rec.device||'')||'';
      if(recDev!==cur) return {state:'stale'};   // 换了麦克风，旧结论不算数
      const ago=Math.round((Date.now()-rec.ts)/60000);
      const agoTxt=ago<1?'刚刚':(ago<60?ago+' 分钟前':(ago<1440?Math.round(ago/60)+' 小时前':Math.round(ago/1440)+' 天前'));
      return {state:rec.level==='good'?'good':'warn', verdict:rec.verdict||'', ago:agoTxt};
    },
    micTestPlay(){ try{ if(this.micTest.url) new Audio(this.micTest.url).play(); }catch(_){} },
    // devOverride：就绪度清单的「试听」验证的是直播声卡这一路（观众能否听到），
    // 用户当前监听耳机时也要直打 CABLE，而不是测耳机。
    async outTestRun(devOverride){
      if(this.outTest.busy) return;
      this.outTest.busy=true; this.outTest.res=null;
      const dev=(devOverride!==undefined?devOverride:(this.audioOutput||this.rvc.outputDevice))||'';
      try{
        const d=await fetch(HUB+'/api/audio/output_test?device='+encodeURIComponent(dev)).then(r=>r.json());
        this.outTest.res=d;
        // P3-2 回环结论持久化：只有带 probe 的（CABLE 自证回环）才有机器结论可记；
        // 普通输出靠人耳听「叮」，没有可信的自动判据，不入就绪度。
        if(d.ok && d.probe){
          const rec={heard:!!d.probe.heard, peak:d.probe.peak_dbfs, ts:Date.now(), device:dev||''};
          this._outTestSaved=rec;
          try{ localStorage.setItem('hub_out_test', JSON.stringify(rec)); }catch(_){}
        }
      }catch(e){ this.outTest.res={ok:false, detail:String(e)}; }
      finally{ this.outTest.busy=false; }
    },
    // P3-2 就绪度用的「直播声卡回环」裁决：找当前枚举里的 CABLE，7 天内对同一路的 heard 结论才算数
    _cableOutDev(){
      const devs=(this.rvcOutputsEx&&this.rvcOutputsEx.devices)||[];
      const d=devs.find(x=>x.group==='live');
      if(d) return d.value;
      return (this.rvcOutputDevices||[]).find(x=>/cable input/i.test(x))||'';
    },
    outTestJudge(){
      const cable=this._cableOutDev();
      if(!cable) return {state:'none'};
      const rec=this._outTestSaved;
      if(!rec || !rec.ts || (Date.now()-rec.ts)>7*86400e3) return {state:'none'};
      const recDev=this.audioDevCanon('out', rec.device||'')||'';
      if(recDev!==(this.audioDevCanon('out', cable)||'')) return {state:'stale'};   // 换了直播声卡，旧结论不算数
      const ago=Math.round((Date.now()-rec.ts)/60000);
      const agoTxt=ago<1?'刚刚':(ago<60?ago+' 分钟前':(ago<1440?Math.round(ago/60)+' 小时前':Math.round(ago/1440)+' 天前'));
      return {state:rec.heard?'good':'warn', ago:agoTxt, peak:rec.peak};
    },
    // 试听结论（人话）：CABLE 回环自证=「观众能听到✓」；普通输出=「听到叮了吗」
    outTestText(){
      const r=this.outTest.res; if(!r) return '';
      if(!r.ok) return '试听失败：'+(r.detail||'未知错误');
      if(r.probe){
        if(r.probe.heard) return '✅ 直播声卡收到了提示音（峰值 '+r.probe.peak_dbfs+'dB）——观众能听到这一路';
        return '⚠️ 提示音已发出，但直播声卡没收到回环——检查 VB-Cable 是否被禁用（Windows 声音设置）';
      }
      return '已向「'+(this.audioDevLabel('out', this.audioOutput||this.rvc.outputDevice)||r.device)+'」播放提示音：听到「叮」就是它；没听到就换一个再试';
    },
    outTestGood(){ const r=this.outTest.res; return !!(r&&r.ok&&(!r.probe||r.probe.heard)); },
    // ── P1-2 用户模式「声音线路卡」：链路数据（全部来自既有状态，零新增轮询）──
    audioChain(){
      const inV=this.audioInput||this.rvc.inputDevice||'';
      const outV=this.audioOutput||this.rvc.outputDevice||'';
      const inM=this.audioDevMeta('in', inV);
      const outM=this.audioDevMeta('out', outV);
      const outLive = outM ? outM.group==='live' : /cable input/i.test(outV);
      let talking=false; try{ talking=!!this.audioLive().talking; }catch(_){}
      return {
        mic: inV ? (inM?inM.label:inV) : '开播时自动选择',
        micOk: !!inV, talking,
        out: outV ? (outM?outM.label:outV) : '开播时自动选择',
        outLive, outWarn: !!outV && !outLive,
      };
    },
    // ── P1-3 前置检查修复入口分发 ──
    envFixAct(c){
      const f=(c&&c.fix)||{};
      if(f.type==='url' && f.to){ window.open(f.to,'_blank'); return; }
      if(f.type==='tab' && f.to){ this.goTab(f.to); return; }
      if(f.type==='wizard'){ this.openVbWizard(); return; }
      if(f.type==='support'){ this.showToast('运行环境缺失需重装，请联系交付/技术支持','info'); }
    },
    async openVbWizard(){
      if(this.vbWizardBusy) return;
      if(this.vbWizard){ this.vbWizard=null; return; }   // 再点=收起
      this.vbWizardBusy=true;
      try{ this.vbWizard=await fetch(HUB+'/api/audio/setup_wizard').then(r=>r.json()); }
      catch(e){ this.vbWizard={ok:false, detail:String(e), steps:[]}; }
      finally{ this.vbWizardBusy=false; }
    },
    async vbWizardRecheck(){
      if(this.vbWizardBusy) return;
      this.vbWizardBusy=true;
      try{
        const [w]=await Promise.all([
          fetch(HUB+'/api/audio/setup_wizard').then(r=>r.json()),
          this.checkEnv(), this.rvcRefreshDevices()]);
        this.vbWizard=w;
        this.showToast(w&&w.ready?'✅ 检测到直播声卡已就绪':'仍未检测到 CABLE——装完驱动需要重启电脑','info');
      }catch(e){ this.showToast('重新检测失败：'+e,'error'); }
      finally{ this.vbWizardBusy=false; }
    },

    async rvcRefreshModels() {
      try {
        const d=await fetch(HUB+'/rvc/models').then(r=>r.json());
        this.rvcModels=d.models||[];
        this.rvcAliases=d.aliases||{};
        this.rvcWeightsDir=d.weights_dir||'';
      } catch(e){}
    },
    // P2-RA: 变声模型下拉显示名——有别名给「别名（原文件名）」，让老运营还能按旧编号对上号
    rvcLabel(m){
      const al=this.rvcAliases&&this.rvcAliases[m];
      const base=String(m||'').split('/').pop().replace(/\.pth$/i,'');
      return al ? al+'（'+base+'）' : m;
    },

    // 将当前 RVC 滑块参数保存到激活角色
    async rvcSaveToProfile() {
      if (!this.active) return;
      const settings = {
        pitch: this.rvc.pitch,
        index_rate: this.rvc.indexRate,
        protect: this.rvc.protect,
        f0method: 'rmvpe',
      };
      const body = { rvc_settings: settings };
      if (this.rvc.model) body.rvc_model = this.rvc.model;
      try {
        const d = await fetch(HUB+`/profiles/${enc(this.active)}`, {
          method: 'PATCH', headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body)
        }).then(r=>r.json());
        this.rvcMsg = d.ok ? `✅ 已保存 → ${this.active}` : '❌ 保存失败';
        this.rvcOk = d.ok;
      } catch(e) { this.rvcMsg = '❌ '+e; this.rvcOk = false; }
      setTimeout(()=>this.rvcMsg='', 3000);
    },

    async rvcApplyConfig() {
      const cfg = {
        pth_path: this.rvc.model,
        index_path: '',
        pitch: this.rvc.pitch,
        sg_input_device: this.rvc.inputDevice,
        sg_output_device: this.rvc.outputDevice,
        index_rate: this.rvc.indexRate,
        protect: this.rvc.protect,
        f0method: 'rmvpe',
        threhold: -60,
        rms_mix_rate: 0.25,
        is_half: true,
      };
      try {
        const d=await fetch(HUB+'/rvc/config',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify(cfg)}).then(r=>r.json());
        // RVC 成功={message:...}；hub 侧预检失败={ok:false,detail:...}；RVC 校验失败={detail:...}
        const ok=!!(d&&(d.success||d.ok||d.message));
        // restarted=true：引擎检测到转换在跑，已自动 停→配→起，新模型/变调即时生效
        this.rvcMsg=ok?(d&&d.restarted?'✅ 配置已应用，变声已重启生效':'✅ 配置已应用'):'⚠️ '+((d&&d.detail)||JSON.stringify(d));
        this.rvcOk=ok;
        // RVC-P1 诚实滑块：hub 回传 index_active=false ⇒ 该模型无 .index，「音色贴合度」实际不生效
        if(ok && d && ('index_active' in d)) this.rvcIndexActive = (d.index_active!==false);
      } catch(e){ this.rvcMsg='❌ '+e; this.rvcOk=false; }
      setTimeout(()=>this.rvcMsg='',this.rvcOk?3000:8000);
      return this.rvcOk;
    },

    async rvcStartConversion() {
      // 先把当前滑块值应用下去再起转换——历史坑：只打 /rvc/start，引擎用的是
      // 上次落盘的旧配置，用户「换了模型/调了变调但声音没变」即源于此。
      // 面板没选模型（刷新页面后的自救卡重启等场景）则跳过应用，走引擎落盘配置。
      if(this.rvc.model && !await this.rvcApplyConfig()){
        this.rvcActive=false;
        return;   // 应用失败：rvcApplyConfig 已展示具体原因，不再盲目起转换
      }
      try {
        const d=await fetch(HUB+'/rvc/start',{method:'POST'}).then(r=>r.json());
        // RVC 起转成功={message:"Audio conversion started"}；失败时 hub 会原样转发 {detail:...}
        // （历史上这里无脑置 rvcActive=true，启动失败也显示"已开始"→ 用户以为变声没响应）
        const txt=String((d&&(d.detail||d.message))||'');
        const running=!!(d&&d.message)||/already/i.test(txt);
        if(running){
          this.rvcActive=true;
          // P3-1 记下本次转换实际用的设备（拔插热切 CTA 的判据：在跑的正是缺席那只才提示）
          this._rvcRunDevs={in:this.rvc.inputDevice||'', out:this.rvc.outputDevice||''};
          this.rvcMsg='✅ 变声已开始'; this.rvcOk=true;
        }else{
          this.rvcActive=false;
          this.rvcMsg='❌ 启动失败：'+(txt||'未知错误'); this.rvcOk=false;
        }
      } catch(e){ this.rvcMsg='❌ '+e; this.rvcOk=false; }
      setTimeout(()=>this.rvcMsg='',this.rvcOk?3000:8000);
    },

    // ── RVC-P1: 录 5 秒 AB 试听（原声 vs 变声）────────────────────────────
    //   不碰实时拾音流：走离线 /api/rvc_assets/preview（复用引擎 /convert 分层缓存，
    //   且离线链路 protect 真实生效）。录音复用 vpRec 同款「解码→WAV」管线，独立实例变量互不干扰。
    async rvcAbRecord(){
      if(this.rvcAb.state==='rec') return;
      if(!this.rvc.model){ this.showToast('先在上方选择变声模型','error'); return; }
      if(!(navigator.mediaDevices&&navigator.mediaDevices.getUserMedia&&window.MediaRecorder)){
        this.showToast('浏览器拿不到麦克风：需 HTTPS 或本机 127.0.0.1 访问','error'); return;
      }
      let stream;
      try{
        // 关浏览器三件套：变声要原始人声，回声消除/降噪会染色
        stream=await navigator.mediaDevices.getUserMedia({audio:{
          echoCancellation:false, noiseSuppression:false, autoGainControl:false, channelCount:1}});
      }catch(e){ this.showToast(this.micErrText?this.micErrText(e):('麦克风打开失败: '+e),'error'); return; }
      this._abStream=stream;
      const mr=new MediaRecorder(stream);
      this._abChunks=[]; this._abMr=mr;
      mr.ondataavailable=e=>{ if(e.data&&e.data.size) this._abChunks.push(e.data); };
      mr.onstop=()=>this._rvcAbFinish();
      mr.start();
      this.rvcAb.state='rec'; this.rvcAb.sec=0;
      this._abTimer=setInterval(()=>{ this.rvcAb.sec++; if(this.rvcAb.sec>=5) this.rvcAbStop(); },1000);
    },
    rvcAbStop(){
      clearInterval(this._abTimer);
      try{ if(this._abMr&&this._abMr.state!=='inactive') this._abMr.stop(); }catch(_){}
    },
    async _rvcAbFinish(){
      clearInterval(this._abTimer);
      try{ (this._abStream?this._abStream.getTracks():[]).forEach(t=>t.stop()); }catch(_){}
      this._abStream=null;
      this.rvcAb.state='idle'; this.rvcAb.busy=true; this.rvcAb.outB64='';
      try{
        const blob=new Blob(this._abChunks,{type:(this._abMr&&this._abMr.mimeType)||'audio/webm'});
        const ctx=new (window.AudioContext||window.webkitAudioContext)();
        const au=await ctx.decodeAudioData(await blob.arrayBuffer());
        try{ ctx.close(); }catch(_){}
        const wav=this._wavEncode(au);
        if(this.rvcAb.rawUrl) URL.revokeObjectURL(this.rvcAb.rawUrl);
        this.rvcAb.rawUrl=URL.createObjectURL(wav);
        const b64=await new Promise((res,rej)=>{ const fr=new FileReader();
          fr.onload=()=>res(String(fr.result).split(',')[1]); fr.onerror=rej; fr.readAsDataURL(wav); });
        // 首次冷加载模型可能 10-30s（引擎离线缓存），busy 态撑住
        const d=await fetch(HUB+'/api/rvc_assets/preview',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:this.rvc.model, sample_b64:b64, settings:{
            pitch:Math.round(+this.rvc.pitch||0), index_rate:+this.rvc.indexRate,
            protect:+this.rvc.protect, f0method:'rmvpe'}})}).then(r=>r.json());
        if(!d.ok) throw new Error(d.detail||'转换失败');
        this.rvcAb.outB64=d.audio_base64;
        this.showToast('✅ 试听就绪：点「原声/变声」对比','success');
      }catch(e){ this.showToast('AB 试听失败: '+((e&&e.message)||e),'error'); }
      finally{ this.rvcAb.busy=false; this._abMr=null; this._abChunks=[]; }
    },
    rvcAbPlay(which){
      if(this._abAudio){ try{ this._abAudio.pause(); }catch(_){} this._abAudio=null; }
      if(this.rvcAb.playing===which){ this.rvcAb.playing=''; return; }   // 再点=停
      const src = which==='raw' ? this.rvcAb.rawUrl
                                : (this.rvcAb.outB64?('data:audio/wav;base64,'+this.rvcAb.outB64):'');
      if(!src) return;
      const a=new Audio(src);
      this._abAudio=a; this.rvcAb.playing=which;
      a.onended=()=>{ if(this.rvcAb.playing===which) this.rvcAb.playing=''; };
      a.onerror=()=>{ if(this.rvcAb.playing===which) this.rvcAb.playing=''; };
      a.play().catch(()=>{});
    },

    async rvcStopConversion() {
      try {
        const d=await fetch(HUB+'/rvc/stop',{method:'POST'}).then(r=>r.json());
        this.rvcActive=false;
        this.rvcMsg='⏹ 变声已停止'; this.rvcOk=true;
      } catch(e){ this.rvcMsg='❌ '+e; this.rvcOk=false; }
      setTimeout(()=>this.rvcMsg='',3000);
    },

    // P23-1: 手机音频输入配置（P0: 优先用后端结构化分组=单一真相；不可用回退旧关键词表）
    rvcSetupPhoneAudio() {
      const exPhones=((this.rvcInputsEx&&this.rvcInputsEx.devices)||[]).filter(d=>d.group==='phone').map(d=>d.value);
      // 旧路径兜底：DroidCam(安卓/iOS) / iVCam / Camo / EpocCam / WO Mic 等
      const PHONE_MIC = ['droid','ivcam','e2esoft','camo','epoccam','wo mic','womic','iphone','phone'];
      const isPhoneMic = (d) => { const n=d.toLowerCase(); return PHONE_MIC.some(p=>n.includes(p)); };
      const phoneInputs = exPhones.length ? exPhones : this.rvcInputDevices.filter(isPhoneMic);
      
      if(phoneInputs.length === 0) {
        this.rvcPhoneAudioMsg = '⚠️ 未找到手机虚拟麦克风。安卓:装DroidCam；苹果:装iVCam/Camo/DroidCam(iOS)/EpocCam，并在PC端连接。';
        setTimeout(()=>this.rvcPhoneAudioMsg='', 6000);
        return;
      }
      
      // 选择最佳手机输入（优先MME，采样率更稳）
      let bestInput = phoneInputs.find(d => d.includes('MME')) || phoneInputs[0];
      this.rvc.inputDevice = bestInput;
      
      // 选择最佳VB-Cable输出
      let bestOutput = this.rvcOutputDevices.find(d => d.includes('CABLE Input') && d.includes('MME'));
      if(!bestOutput) {
        bestOutput = this.rvcOutputDevices.find(d => d.toLowerCase().includes('cable'));
      }
      if(bestOutput) this.rvc.outputDevice = bestOutput;
      // 同步到开播音频源（设备单一真相）
      this.audioInput = bestInput;
      if(bestOutput) this.audioOutput = bestOutput;
      this._prevAudioIn=this.audioInput; this._prevAudioOut=this.audioOutput;
      // P2-1 点「自动配手机麦」也是明确选择 → 存偏好（重启/明天开播仍用手机麦）
      this._userPickTs.in=Date.now(); this._userPickTs.out=Date.now();
      this.saveAudioPref('input', bestInput, 'phone-setup');
      if(bestOutput) this.saveAudioPref('output', bestOutput, 'phone-setup');

      const inLbl=this.audioDevLabel('in', bestInput)||bestInput;
      const outLbl=bestOutput?(this.audioDevLabel('out', bestOutput)||bestOutput):'未找到直播声卡(装 VB-Cable)';
      this.rvcPhoneAudioMsg = `✓ 已配置: ${inLbl} → ${outLbl}。点击"应用变声配置"生效。`;
      setTimeout(()=>this.rvcPhoneAudioMsg='', 5000);
    },

    // ── Services & Stats ──
    async refreshServices() {
      try { const d=await fetch(HUB+'/health').then(r=>r.json()); this.services=d.services||{}; if(d.broadcast) this.broadcast=d.broadcast; } catch(e){}
    },

    async loadSysInfo() {
      try { this.sysInfo=await fetch(HUB+'/api/system_info').then(r=>r.json()); } catch(e){}
    },

    // P20-5: 服务 URL 热重载
    async loadConfig() {
      try { this.configData = await fetch(HUB+'/api/config').then(r=>r.json()); this.svcEdits = {...((this.configData&&this.configData.services)||{})}; }
      catch(e) { this.showToast('配置加载失败: '+e,'error'); }
    },
    async patchConfig(key, value) {
      if(!value || !value.trim()) return;
      const cur = this.configData?.services?.[key];
      if(cur === value.trim()) return;  // 无变化
      try {
        const d = await fetch(HUB+'/api/config', {method:'PATCH',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({key, value:value.trim()})}).then(r=>r.json());
        if(d.ok){
          this.configData.services[key] = value.trim();
          this.showToast(`✅ ${key} 已更新`, 'success');
        }
      } catch(e) { this.showToast('配置更新失败: '+e,'error'); }
    },
    // 服务地址：显式保存（带二次确认，避免误改热重载技术项）；svcEdits 为草稿态，仅点「保存」才落库
    saveSvc(key){
      const v = (this.svcEdits[key]||'').trim();
      if(!v){ this.showToast('地址不能为空','error'); return; }
      const cur = this.configData?.services?.[key];
      if(v===cur) return;
      if(!confirm('确认修改服务「'+key+'」地址？\n\n新地址：'+v+'\n\n修改立即生效（热重载），可能影响正在进行的服务。')) return;
      this.patchConfig(key, v);
    },
    svcReset(key){ if(this.configData&&this.configData.services) this.svcEdits[key] = this.configData.services[key]; },

    async pollHealth() {
      try {
        const d = await fetch(HUB+'/health').then(r=>r.json());
        if (d.pressure) this.healthPressure = d.pressure;
        if (d.services) this.services = d.services;
        if (d.broadcast) this.broadcast = d.broadcast;   // 开播模式感知核心服务单一真相
        this.loadQualityAlerts();
      } catch(e){}
      try {                                  // 远端掉线→本机兜底的降级态（不阻塞主健康轮询）
        const cap = await fetch(HUB+'/api/capacity').then(r=>r.json());
        this.degradedServices = (cap?.gpu_pools?.degraded_services) || [];
      } catch(e){ this.degradedServices = []; }
      setTimeout(()=>this.pollHealth(), 30000);
    },

    async checkInterp() {                              // 探测同传服务(7900)是否在线
      try {
        const c = new AbortController(); const t = setTimeout(()=>c.abort(), 2500);
        const r = await fetch(this.interpUrl.split('?')[0]+'health', {signal:c.signal}); clearTimeout(t);
        this.interpUp = r.ok;
      } catch(e){ this.interpUp = false; }
    },

    // [去重·2026-07-16] 同传双 CTA(直播/通话)、外层观测轮询、停止/预载按钮全部退役：
    // 观测(mbar)/停止(主按钮)/预载(角色标签旁)/直播同传/通话向导 全在 iframe 面板内且就地反馈更好；
    // Hub 侧仅保留在线探测 checkInterp + 面板跳转 goTab('interp')（供命令面板/其他页动线引用）。
    startInterp() { this.goTab('interp'); this.checkInterp(); },

    interpOverlayUrl(panel) {                         // Phase B-2：OBS Browser Source URL
      const q = 'panel=' + (panel === 0 ? '0' : '1') + '&pos=bottom&max2=2';
      return HUB + '/interp/subtitle_overlay?' + q;
    },

    async loadLabServices() {                          // C-4: 实验室服务(发型/试衣)就绪态
      try {
        const d = await fetch(HUB + '/api/lab/services').then(r => r.json());
        if (d.ok) this.labSvc = d.services || {};
      } catch (e) {}
      this.loadFittingClothes();
      this.loadHairStyles();
    },
    async loadHairStyles() {                           // 阶段8：发型样式清单（8001 离线则下拉自然隐藏）
      try {
        const d = await fetch(HUB + '/api/hair/styles').then(r => r.json());
        this.hairStyles = d.up ? (d.styles || []) : [];
      } catch (e) { this.hairStyles = []; }
    },

    // ── S2: 可直播 DFM 整脸换角色清单（换脸引擎 8000 的 /model/available 经 Hub 代理）
    //    引擎离线 / 旧版无该接口 → dfmModels 空，编辑抽屉里的下拉自然隐藏（零回归）
    async loadDfmModels() {
      try {
        const d = await fetch(HUB + '/api/dfm/models?live_only=1').then(r => r.json());
        this.dfmEngineUp = !!d.engine_up;
        this.dfmModels = (d.engine_up && Array.isArray(d.models)) ? d.models : [];
      } catch (e) { this.dfmModels = []; this.dfmEngineUp = false; }
    },

    // S2 预热：选中某 DFM 角色的一刻，后台把它加载进换脸引擎 LRU（不改直播画面）。
    //   循证(.104/4070)：热切未缓存 DFM 阻塞 ~4.5s 且卡并发帧 ~2.8s；命中 LRU 仅 ~16ms。
    //   → 编辑时先预热，之后真正激活该角色时上屏切换秒切无感。fire-and-forget，失败静默。
    prewarmDfm(model) {
      const m = (model || '').trim();
      if (!m) return;                                   // 通用换脸(inswapper 基线常驻)无需预热
      fetch(HUB + '/api/dfm/prewarm', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: m })
      }).catch(() => {});
    },

    // S4: 当前换脸角色（供开播区显示「正在换谁」；engine_up=false 时整条隐藏）
    async refreshDfmCur() {
      try {
        const j = await fetch(HUB + '/api/dfm/current').then(r => r.json());
        const on = !!(j && j.failover && j.failover.on_replica);
        if (on && !this.dfmHot._wasOnReplica) {
          this.showToast('换脸主引擎失联，已切容灾副本（画质略降，主引擎恢复后自动回切）', 'warn');
        } else if (!on && this.dfmHot._wasOnReplica) {
          this.showToast('换脸主引擎已恢复，画面已回切生产', 'success');
        }
        this.dfmHot._wasOnReplica = on;
        this.dfmHot.cur = j;
      }
      catch (e) { this.dfmHot.cur = null; }
    },
    // S5: DFM 库对账——绑定指向直播机没有的模型(missing)或仅离线档(offline_only)时亮警示。
    //   引擎离线时保留上次结果（不闪红）；无绑定=天然干净。
    async refreshDfmAudit() {
      try {
        const d = await fetch(HUB + '/api/dfm/audit').then(r => r.json());
        if (d && d.ok) this.dfmAudit = { issues: d.issues || [], n_bindings: d.n_bindings || 0, ts: Date.now() };
      } catch (e) { /* 引擎/Hub 短暂不可达：保留旧账 */ }
    },
    dfmIssueFor(name) {   // 角色卡/编辑器取该角色的对账问题（无=null）
      return (this.dfmAudit.issues || []).find(i => i.profile === name) || null;
    },
    // P2: DFM 文件名 → 中文名。优先可直播清单（抽屉打开即加载），其次对账结果里的 cn，
    //   都查不到回退去掉 .dfm 的文件名——绑定了「仅离线档」模型时 dfmModels(live_only=1) 查不到它。
    dfmCn(model) {
      if (!model) return '';
      const hit = (this.dfmModels || []).find(m => m.model === model);
      if (hit && hit.cn) return hit.cn;
      const iss = (this.dfmAudit.issues || []).find(i => i.model === model);
      if (iss && iss.cn) return iss.cn;
      return model.replace(/\.dfm$/i, '');
    },
    openEditByName(name) {   // S5: 对账横幅「去重绑」→ 直接打开该角色编辑抽屉
      const p = (this.profiles || []).find(x => x.name === name);
      if (p) this.openEdit(p, 'edit');
    },
    // S4: 一键预热本场热角色（活动角色+最常用，引擎 LRU 预算内）。后台串行加载，立即返回排队清单。
    async prewarmHot() {
      if (this.dfmHot.busy) return;
      this.dfmHot.busy = true; this.dfmHot.msg = '';
      try {
        const d = await fetch(HUB + '/api/dfm/prewarm_hot', { method: 'POST' }).then(r => r.json());
        this.dfmHot.msg = d.n ? ('已排队预热 ' + d.n + ' 个角色（后台约 ' + (d.n * 3) + 's 完成，之后场内切换零冻结）')
                              : (d.hint || '没有可预热的角色');
        this.showToast(this.dfmHot.msg, d.n ? 'success' : 'info');
        setTimeout(() => this.refreshDfmCur(), Math.min(20000, 4000 * Math.max(1, d.n)));
      } catch (e) { this.dfmHot.msg = '预热请求失败：换脸引擎不可达'; this.showToast(this.dfmHot.msg, 'error'); }
      this.dfmHot.busy = false;
    },

    // ── C-4 试衣间（FitDiT）────────────────────────────────
    async loadFittingClothes() {                       // 服装库（服务离线→面板降级为提示）
      try {
        const d = await fetch(HUB + '/api/tryon/clothes').then(r => r.json());
        this.fitting.up = !!d.up;
        this.fitting.backend = d.backend || '';
        this.fitting.clothes = d.clothes || [];
        if (!this.fitting.cloth || !this.fitting.clothes.includes(this.fitting.cloth)) {
          this.fitting.cloth = d.active || this.fitting.clothes[0] || '';
        }
      } catch (e) { this.fitting.up = false; }
      this.checkFittingStored();
    },

    async checkFittingStored() {                       // 阶段12：当前作用角色有没有存照（全身照记忆）
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.fitting.stored = false; return; }
      try {
        const d = await fetch(HUB + `/profiles/${encodeURIComponent(prof)}`).then(r => r.json());
        this.fitting.stored = !!d.has_body_photo;
      } catch (e) { this.fitting.stored = false; }
    },

    onFittingPerson(ev) {                              // 全身/半身照就地读入（不落任何库）
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      const fr = new FileReader();
      fr.onload = () => {
        this.fitting.personB64 = String(fr.result).split(',')[1] || '';
        this.fitting.personName = f.name;
        this.fitting.result = '';
      };
      fr.readAsDataURL(f);
      ev.target.value = '';
    },

    async uploadFittingCloth(ev) {                     // 上传新服装（白底平铺图效果最好）
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      const name = (prompt('服装名称（如：黑色西装）', f.name.replace(/\.[^.]+$/, '')) || '').trim();
      ev.target.value = '';
      if (!name) return;
      const fr = new FileReader();
      fr.onload = async () => {
        try {
          const img = String(fr.result).split(',')[1] || '';
          const r = await fetch(HUB + '/api/tryon/upload_cloth', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, image: img})});
          const d = await r.json();
          if (r.ok && d.ok !== false) {
            this.showToast(`服装「${name}」已入库`, 'success');
            await this.loadFittingClothes();
            this.fitting.cloth = name;
          } else this.showToast('上传失败：' + (d.detail || '').slice(0, 100), 'warn');
        } catch (e) { this.showToast('上传失败：' + e, 'error'); }
      };
      fr.readAsDataURL(f);
    },

    fittingFiltered() {                                // 搜索过滤后的服装名列表
      const q = this.fittingSearch.trim().toLowerCase();
      return q ? this.fitting.clothes.filter(c => c.toLowerCase().includes(q)) : this.fitting.clothes;
    },
    pickFittingCloth(c) {                              // 选衣时按演示命名自动切部位（少一次手动）
      this.fitting.cloth = c;
      if (c.includes('裤装') || c.includes('下装')) this.fitting.clothType = 'lower';
      else if (c.includes('连衣裙')) this.fitting.clothType = 'dress';
      else if (c.includes('上衣') || c.includes('上装')) this.fitting.clothType = 'upper';
    },
    fittingPageItems() {                               // 当前页缩略图（库小于一页时不分页）
      const all = this.fittingFiltered();
      if (all.length <= this.fittingPageSize) return all;
      const p = Math.min(this.fittingPage, Math.max(0, Math.ceil(all.length / this.fittingPageSize) - 1));
      return all.slice(p * this.fittingPageSize, (p + 1) * this.fittingPageSize);
    },
    async deleteFittingCloth(name) {                   // 右键缩略图删服装
      if (!confirm(`删除服装「${name}」？`)) return;
      try {
        const r = await fetch(HUB + '/api/tryon/delete_cloth', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name})});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.showToast(`「${name}」已删除`, 'success');
          await this.loadFittingClothes();
          if (this.fitting.cloth === name) this.fitting.cloth = d.active || '';
        } else if (r.status === 404 && !d.detail) {
          this.showToast('删除入口待 Hub 重启激活（下播后跑 激活定妆包.bat）', 'warn');
        } else this.showToast('删除失败：' + (d.detail || '').slice(0, 120), 'warn');
      } catch (e) { this.showToast('删除失败：' + e, 'error'); }
    },
    async extractFittingCloth(ev) {                    // 截图抠衣：穿着照/商品截图→白底服装入库（可多选批量）
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';
      if (!files.length) return;
      // 单张：可自定义名字；批量：直接用文件名当服装名（免 N 次弹窗）
      let firstName = files[0].name.replace(/\.[^.]+$/, '');
      if (files.length === 1) {
        firstName = (prompt('抠出的服装叫什么名字？', firstName) || '').trim();
        if (!firstName) return;
      }
      const readB64 = f => new Promise(res => {
        const fr = new FileReader();
        fr.onload = () => res(String(fr.result).split(',')[1] || '');
        fr.readAsDataURL(f);
      });
      this.fittingExtractBusy = true;
      let okN = 0, lastName = '';
      try {
        for (let i = 0; i < files.length; i++) {
          this.fittingExtractProg = files.length > 1 ? `${i + 1}/${files.length}…` : '抠衣中';
          const name = i === 0 ? firstName : files[i].name.replace(/\.[^.]+$/, '');
          try {
            // 抠衣部位跟随试穿部位选择：上装/下装/连衣裙（穿着照不连带抠出其他部位）
            const r = await fetch(HUB + '/api/tryon/extract_cloth', {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({image: await readB64(files[i]), save_name: name,
                                    part: this.fitting.clothType || 'upper'})});
            const d = await r.json();
            if (r.ok && d.ok) { okN++; lastName = name; }
            else if (files.length === 1)
              this.showToast('抠衣失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
          } catch (e) { if (files.length === 1) this.showToast('抠衣失败：' + e, 'error'); }
        }
        if (okN) {
          this.showToast(files.length > 1 ? `批量抠衣完成：${okN}/${files.length} 件入库` : `「${lastName}」已抠衣入库`, 'success');
          await this.loadFittingClothes();
          this.fitting.cloth = lastName;
        } else if (files.length > 1) {
          this.showToast('批量抠衣全部失败（图太小/无法识别服装？）', 'warn');
        }
      } finally { this.fittingExtractBusy = false; this.fittingExtractProg = ''; }
    },

    async runFittingPreview() {                        // 只出图不落库；写角色用 applyFittingToProfile
      if (this.fitting.busy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!this.fitting.personB64 && !this.fitting.stored) { this.showToast('先上传全身/半身照', 'warn'); return; }
      if (!this.fitting.cloth) { this.showToast('先选择或上传服装', 'warn'); return; }
      this.fitting.busy = true;
      this.fitting.result = '';
      const t0 = Date.now();
      try {
        // 阶段12：本次没传照→带 profile 让 Hub 用存照；传了→Hub 顺手存档下次免传
        const r = await fetch(HUB + '/api/tryon/preview', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({person_image_b64: this.fitting.personB64,
                                profile: prof || '',
                                cloth_name: this.fitting.cloth,
                                cloth_type: this.fitting.clothType,
                                resolution: this.fitting.resolution})});
        const d = await r.json();
        if (r.ok && d.result_image) {
          this.fitting.result = d.result_image;
          this.fitting.elapsed = Math.round((d.elapsed_ms || (Date.now() - t0)) / 100) / 10;
          if (this.fitting.personB64 && prof) this.fitting.stored = true;
        } else {
          this.showToast('试穿失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
        }
      } catch (e) { this.showToast('试穿失败：' + e, 'error'); }
      finally { this.fitting.busy = false; }
    },

    async applyFittingToProfile() {                    // 满意的预览 → tryon_preset 写入角色底片(带 Ditto 微动)
      if (this.fitting.applyBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      if (!this.fitting.personB64 && !this.fitting.stored) { this.showToast('先上传全身/半身照', 'warn'); return; }
      this.fitting.applyBusy = true;
      try {
        // 阶段12：person_image_b64 传空=用该角色存照（Hub 端全身照记忆）
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/tryon_preset`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({person_image_b64: this.fitting.personB64,
                                cloth_name: this.fitting.cloth, field: 'body_video',
                                cloth_type: this.fitting.clothType,
                                resolution: this.fitting.resolution})});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.showToast(`已写入「${prof}」：${d.hint || '试衣底片就绪'}`, 'success');
          if (this.fitting.personB64) this.fitting.stored = true;
          this.refreshLookHistIfOpen();
        } else {
          this.showToast('写入失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
        }
      } catch (e) { this.showToast('写入失败：' + e, 'error'); }
      finally { this.fitting.applyBusy = false; }
    },

    async runIdleMotion() {                            // 待机微动：角色现有照片→Ditto 微动 idle_video
      if (this.idleMotionBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.idleMotionBusy = true;
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/idle_motion`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({source: 'auto', secs: 6})});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.showToast(`「${prof}」${d.hint || '待机微动已生成'}`, 'success');
          this.refreshLookHistIfOpen();
        } else {
          this.showToast('生成失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
        }
      } catch (e) { this.showToast('生成失败：' + e, 'error'); }
      finally { this.idleMotionBusy = false; }
    },

    // ── 阶段15 动态试衣（CatV2TON 视频换装）────────────────────
    // 提交作业（角色待机微动/底视频 + 当前选中服装）→ 后台 2-6 分钟 → 轮询进度。
    // Hub 侧自动腾显存（卸闲置模型+泊闲置引擎，作业完自动放回），前端只管轮询。
    async runVideoTryon() {
      if (this.vtryon.busy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      if (!this.fitting.cloth) { this.showToast('先选择或上传服装', 'warn'); return; }
      this.vtryon = {...this.vtryon, busy: true, state: 'submit', progress: 0,
                     detail: '提交中…', preview: '', applied: false, jobId: '', elapsed: 0};
      try {
        const r = await fetch(HUB + '/api/videotryon/submit', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({profile: prof, cloth_name: this.fitting.cloth,
                                cloth_type: this.fitting.clothType, field: 'idle_video'})});
        const d = await r.json();
        if (!r.ok || !d.job_id) {
          this.vtryon.busy = false; this.vtryon.state = '';
          this.showToast('动态试衣提交失败：' + (d.detail || JSON.stringify(d)).slice(0, 160), 'warn');
          return;
        }
        this.vtryon.jobId = d.job_id;
        this.showToast(d.hint || '动态试衣作业已提交（约 2-6 分钟）', 'success');
        const t0 = Date.now();
        this.vtryon.timer = setInterval(async () => {
          try {
            const j = await fetch(HUB + '/api/videotryon/job/' + this.vtryon.jobId).then(x => x.json());
            this.vtryon.state = j.state || '';
            this.vtryon.progress = j.progress || 0;
            this.vtryon.detail = j.detail || '';
            this.vtryon.elapsed = Math.round((Date.now() - t0) / 1000);
            if (j.state === 'done') {
              this.stopVtryonPoll();
              this.vtryon.preview = HUB + '/api/videotryon/job/' + this.vtryon.jobId
                                        + '/preview?t=' + Date.now();
              this.showToast('动态试衣完成——预览满意就「应用为待机」', 'success');
            } else if (j.state === 'error') {
              this.stopVtryonPoll();
              this.showToast('动态试衣失败：' + (j.detail || '').slice(0, 160), 'warn');
            }
          } catch (e) { /* 服务短暂无响应容忍，下一拍再试 */ }
        }, 5000);
      } catch (e) {
        this.vtryon.busy = false; this.vtryon.state = '';
        this.showToast('动态试衣提交失败：' + e, 'error');
      }
    },
    stopVtryonPoll() {
      if (this.vtryon.timer) { clearInterval(this.vtryon.timer); this.vtryon.timer = null; }
      this.vtryon.busy = false;
    },
    async applyVideoTryon() {                          // 完成的换装视频 → 角色待机循环
      if (this.vtryon.applyBusy || !this.vtryon.jobId) return;
      this.vtryon.applyBusy = true;
      try {
        const r = await fetch(HUB + `/api/videotryon/job/${this.vtryon.jobId}/apply`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.vtryon.applied = true;
          this.showToast(d.hint || '已应用为待机视频', 'success');
          this.refreshLookHistIfOpen();
        } else this.showToast('应用失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
      } catch (e) { this.showToast('应用失败：' + e, 'error'); }
      finally { this.vtryon.applyBusy = false; }
    },

    // ── Phase 12 C-1 虚拟背景 ──────────────────────────────
    async loadBgStatus() {
      try {
        const d = await fetch(HUB + '/realtime/bg').then(r => r.json());
        this.bgCfg = {...this.bgCfg, ...d, image: d.image || this.bgCfg.image};
      } catch (e) { this.bgCfg.up = false; }
    },

    // 图片下拉的候选清单：hub 直读目录(bgImagesAll,不依赖 realtime 存活) ∪ realtime 上报
    // (images_available)。此前虚拟背景下拉只认 realtime 上报，没开播时清单是空的=没法选。
    // kind='img' 时仅静图（离席品牌图不支持视频）；缺省=全部（虚拟背景可播动图/视频）。
    bgImageOptions(kind) {
      let all = [...new Set([...(this.bgImagesAll || []), ...(this.bgCfg.images_available || [])])];
      if (kind === 'img') all = all.filter(n => /\.(jpe?g|png|webp)$/i.test(n));
      return all.sort();
    },
    bgIsVideo(name) { return /\.(gif|mp4|webm|mov|avi|m4v)$/i.test(name || ''); },

    // 上传背景素材到 bg_images 目录并自动选中（target: 'bg'=虚拟背景 / 'away'=离席品牌图）。
    // 静图 jpg/png/webp ≤15MB；动图/视频 gif/mp4/webm/mov/avi ≤200MB（离席品牌图仅静图）。
    async uploadBgImage(ev, target) {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = '';                       // 允许连续上传同一个文件
      if (!f || this.bgUpBusy) return;
      const isVid = this.bgIsVideo(f.name);
      if (!isVid && !/\.(jpe?g|png|webp)$/i.test(f.name)) {
        this.showToast('仅支持 jpg/png/webp 图片，或 gif/mp4/webm/mov/avi 动图视频', 'warn'); return;
      }
      if (isVid && target === 'away') { this.showToast('离席品牌图仅支持静态图片', 'warn'); return; }
      const maxMB = isVid ? 200 : 15;
      if (f.size > maxMB * 1024 * 1024) { this.showToast(`文件过大，请压到 ${maxMB}MB 以内`, 'warn'); return; }
      this.bgUpBusy = true;
      try {
        const fd = new FormData();
        fd.append('file', f);
        const r = await fetch(HUB + '/api/bg_images/upload', {method: 'POST', body: fd});
        const d = await r.json();
        if (r.ok && d && d.ok && d.saved) {
          this.bgImagesAll = d.images || this.bgImagesAll;
          if (target === 'away') { this.awayCfg.image = d.saved; this.applyAway(); }
          else this.bgCfg.image = d.saved;
          this.showToast(`已上传并选中「${d.saved}」` + (d.note ? `（${d.note}）` : ''),
                         d.note ? 'warn' : 'success');
        } else {
          this.showToast('上传失败：' + ((d && (d.detail || d.error)) || ('HTTP ' + r.status)), 'error');
        }
      } catch (e) { this.showToast('上传失败：' + e, 'error'); }
      finally { this.bgUpBusy = false; }
    },

    // ── C-1b 录播增强（MatAnyone 2 离线抠像,任务经 hub 子进程跑）──────────
    maRunning() { return !!this.ma.running; },
    maPct() {
      const j = this.ma.job || {};
      return j.total > 0 ? Math.min(100, Math.round(j.n / j.total * 100)) : 0;
    },
    maEta() {
      const s = (this.ma.job && this.ma.job.eta_s) || 0;
      return s >= 90 ? Math.round(s / 60) + ' 分钟' : s + ' 秒';
    },
    maDl(name) { return HUB + '/api/matting/download?name=' + encodeURIComponent(name); },
    maKind(name) {
      if (/_pha\.mp4$/i.test(name)) return 'alpha 通道';
      if (/_rgba\.mov$/i.test(name)) return 'ProRes 4444';
      return '合成成片';
    },
    async maRefresh(startPollIfRunning = true) {
      try {   // 记住上次的背景/导出选择（本机浏览器级预设）
        const bg = localStorage.getItem('maBg'), pr = localStorage.getItem('maProres');
        if (bg) this.ma.bg = bg;
        if (pr !== null) this.ma.prores = pr === '1';
      } catch (e) {}
      try {
        const d = await fetch(HUB + '/api/matting/status').then(r => r.json());
        this.ma.running = !!d.running;
        this.ma.job = d.job || {};
        this.ma.history = d.history || [];
        this.ma.queue = d.queue || [];
        if (d.disk_free_gb !== undefined) this.ma.diskFree = d.disk_free_gb;
        if (startPollIfRunning && d.running) this.maPoll();
      } catch (e) {}
      try {
        const d2 = await fetch(HUB + '/api/matting/inputs').then(r => r.json());
        this.ma.inputs = (d2 && d2.inputs) || [];
      } catch (e) {}
    },
    maPoll() {
      if (this._maTimer) return;
      this._maTimer = setInterval(async () => {
        try {
          const d = await fetch(HUB + '/api/matting/status').then(r => r.json());
          this.ma.running = !!d.running;
          this.ma.job = d.job || {};
          this.ma.queue = d.queue || [];
          this.ma.history = d.history || [];
          if (d.disk_free_gb !== undefined) this.ma.diskFree = d.disk_free_gb;
          if (!d.running) {
            clearInterval(this._maTimer); this._maTimer = null;
            const st = (d.job || {}).state;
            if (st === 'done') this.showToast('录播增强完成，可下载产物', 'success');
            else if (st === 'error') this.showToast('录播增强失败：' + ((d.job || {}).error || ''), 'error');
          }
        } catch (e) {}
      }, 2000);
    },
    async maUpload(ev) {
      const files = Array.from(ev.target.files || []);
      ev.target.value = '';
      if (!files.length || this.ma.upBusy) return;
      const bad = files.find(f => !/\.(mp4|mov|avi|m4v|webm)$/i.test(f.name));
      if (bad) { this.showToast(`「${bad.name}」不是支持的格式（mp4/mov/avi/webm）`, 'warn'); return; }
      const big = files.find(f => f.size > 2048 * 1024 * 1024);
      if (big) { this.showToast(`「${big.name}」超过 2GB`, 'warn'); return; }
      this.ma.upBusy = true;
      let okN = 0, lastSaved = '';
      try {
        for (const f of files) {          // 串行上传：≤2GB 大文件并行会挤爆带宽/磁盘
          const fd = new FormData();
          fd.append('file', f);
          const r = await fetch(HUB + '/api/matting/upload', {method: 'POST', body: fd});
          const d = await r.json();
          if (r.ok && d && d.ok && d.saved) { okN++; lastSaved = d.saved; this.ma.inputs = d.inputs || this.ma.inputs; }
          else this.showToast(`「${f.name}」上传失败：` + ((d && d.detail) || ('HTTP ' + r.status)), 'error');
        }
        if (okN) {
          this.ma.input = lastSaved;
          this.showToast(okN === 1 ? `已上传「${lastSaved}」` : `已上传 ${okN} 个录播`, 'success');
        }
      } catch (e) { this.showToast('上传失败：' + e, 'error'); }
      finally { this.ma.upBusy = false; }
    },
    async maEnqueueAll() {
      const items = this.ma.inputs || [];
      if (items.length < 2) return;
      if (!confirm(`把列表里的 ${items.length} 个录播全部提交（背景/导出用当前选择）？\n空闲会立即开跑第一个，其余排队依次处理。`)) return;
      this.ma.busy = true;
      try { localStorage.setItem('maBg', this.ma.bg); localStorage.setItem('maProres', this.ma.prores ? '1' : '0'); } catch (e) {}
      let okN = 0, ranNow = 0;
      try {
        for (const it of items) {
          const d = await fetch(HUB + '/api/matting/start', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({input: it.name, bg: this.ma.bg, export: this.ma.prores ? 'prores' : 'mp4'}),
          }).then(r => r.json());
          if (d.ok) { okN++; if (!d.queued) ranNow++; }
        }
        await this.maRefresh();
        this.showToast(`已提交 ${okN} 个任务` + (ranNow ? '，1 个已开跑' : '，停播后自动依次开跑'), 'success');
      } catch (e) { this.showToast('批量入队失败：' + e, 'error'); }
      finally { this.ma.busy = false; }
    },
    async maStart(forceNow) {
      if (!this.ma.input) { this.showToast('先上传/选择一个录播视频', 'warn'); return; }
      this.ma.busy = true;
      try { localStorage.setItem('maBg', this.ma.bg); localStorage.setItem('maProres', this.ma.prores ? '1' : '0'); } catch (e) {}
      try {
        const body = {input: this.ma.input, bg: this.ma.bg, export: this.ma.prores ? 'prores' : 'mp4'};
        if (forceNow) body.force = true;
        const d = await fetch(HUB + '/api/matting/start', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
        }).then(r => r.json());
        if (d.ok && d.queued) {
          this.ma.queue = d.queue || this.ma.queue;
          await this.maRefresh(false);
          this.showToast(d.detail || '已加入队列', 'success');
          return;
        }
        if (d.ok) { this.ma.running = true; this.ma.job = {state: 'loading'}; this.maPoll(); }
        else this.showToast('没跑起来：' + (d.detail || d.reason || '未知原因'), 'warn');
      } catch (e) { this.showToast('启动失败：' + e, 'error'); }
      finally { this.ma.busy = false; }
    },
    async maCancelQueue(idx) {
      try {
        const body = idx === undefined ? {} : {index: idx};
        const d = await fetch(HUB + '/api/matting/cancel_queue', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
        }).then(r => r.json());
        if (d.ok) { this.ma.queue = d.queue || []; this.showToast('队列已更新', 'success'); }
      } catch (e) {}
    },
    async maCancel() {
      try {
        const d = await fetch(HUB + '/api/matting/cancel', {method: 'POST'}).then(r => r.json());
        if (!d.ok) this.showToast(d.detail || '取消失败', 'warn');
      } catch (e) {}
    },

    // ── 06v 离席画面（持久=effect_cfg，在播=即时热切）──────────
    async loadAwayCfg() {
      try {
        const d = await fetch(HUB + '/api/effect_cfg').then(r => r.json());
        const c = (d && d.cfg) || {};
        if (c.awayStyle !== undefined) this.awayCfg.style = c.awayStyle;
        if (c.awayText !== undefined) this.awayCfg.text = c.awayText;
        if (c.awayImage !== undefined) this.awayCfg.image = c.awayImage;
      } catch (e) {}
      try {
        const d = await fetch(HUB + '/api/bg_images').then(r => r.json());
        if (d && d.ok) this.bgImagesAll = d.images || [];
      } catch (e) {}
    },

    async applyAway() {                    // 在播即时热切；没在播只改本地待保存
      if (!this.perf.streaming) return;
      try {
        const q = new URLSearchParams({style: this.awayCfg.style, text: this.awayCfg.text,
                                       image: this.awayCfg.image || ''});
        const d = await fetch(HUB + '/realtime/swap/away?' + q.toString()).then(r => r.json());
        if (d && d.ok) this.showToast('离席画面已热切，即时生效', 'success');
      } catch (e) {}
    },

    async saveAway() {                     // 持久保存(开播自动生效) + 在播同步热切
      if (this.awayBusy) return;
      this.awayBusy = true;
      try {
        if (this.awayCfg.style === 'image' && !this.awayCfg.image) {
          this.showToast('品牌图模式需先选一张图（可点旁边「上传」添加）', 'warn'); return;
        }
        const body = {awayStyle: this.awayCfg.style, awayText: this.awayCfg.text,
                      awayImage: this.awayCfg.image || ''};
        const d = await fetch(HUB + '/api/effect_cfg', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
        if (d && d.ok) {
          this.showToast(this.perf.streaming ? '离席画面已保存（本场已生效，之后开播自动生效）'
                                             : '离席画面已保存（开播自动生效）', 'success');
          this.applyAway();
        } else {
          this.showToast('保存失败：' + (d && d.detail || '未知错误'), 'error');
        }
      } catch (e) { this.showToast('保存失败：' + e, 'error'); }
      finally { this.awayBusy = false; }
    },

    async applyBg() {                                  // 开播中热切背景,立即生效
      if (this.bgBusy) return;
      this.bgBusy = true;
      try {
        const body = {mode: this.bgCfg.mode};
        if (this.bgCfg.mode === 'image') {
          if (!this.bgCfg.image) { this.showToast('先选一张背景图（可点旁边「上传」添加）', 'warn'); return; }
          body.image = this.bgCfg.image;
        }
        const d = await fetch(HUB + '/realtime/bg', {method: 'POST',
          headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(r => r.json());
        if (d.ok) {
          this.showToast(d.mode === 'none' ? '虚拟背景已关闭' : `虚拟背景已切到「${{none:'关',blur:'虚化',image:'图片',green:'绿幕'}[d.mode]||d.mode}」`, 'success');
          this.bgCfg = {...this.bgCfg, ...d, up: true};
          // 抠像引擎是异步预热：显存门禁结论(rvm.hold)/就绪状态要几秒后才有，稍后取一次真实状态
          if (d.mode !== 'none') setTimeout(() => this.loadBgStatus(), 2500);
        } else {
          this.showToast('背景未生效：' + (d.detail || d.hint || d.error || '未知错误'), 'warn');
        }
      } catch (e) { this.showToast('背景设置失败：' + e, 'error'); }
      finally { this.bgBusy = false; }
    },

    // ── Phase 12 C-2 双人换脸 face_map ─────────────────────
    async loadFaceMap() {
      try {
        const d = await fetch(HUB + '/api/face_map').then(r => r.json());
        if (d.ok) this.faceMap = {enabled: !!d.enabled, slots: (d.slots || []).map(s => s.profile || '')};
        while (this.faceMap.slots.length < 2) this.faceMap.slots.push('');
      } catch (e) {}
    },

    async saveFaceMap() {
      if (this.faceMapBusy) return;
      this.faceMapBusy = true;
      try {
        const r = await fetch(HUB + '/api/face_map', {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enabled: this.faceMap.enabled, slots: this.faceMap.slots})});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.showToast(this.faceMap.enabled ? '双人换脸映射已保存并启用（左=槽0 右=槽1；锁主脸自动让位）' : '双人换脸已关闭（回单人模式，锁主脸恢复生效）', 'success');
        } else {
          this.showToast('保存失败：' + (d.detail || JSON.stringify(d)), 'warn');
        }
      } catch (e) { this.showToast('保存失败：' + e, 'error'); }
      finally { this.faceMapBusy = false; }
    },

    // ── Phase 12 C-3 发型场次定妆 ──────────────────────────
    async runHairPreset() {                            // 离线 5-10s：8001 当前发型 → 角色定妆脸
      if (this.hairPresetBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.hairPresetBusy = true;
      this.hairPresetPreview = '';
      this.labSvcColdHint('hair', '发型');            // P0: 服务未起=后端自动拉起，先告知别干等
      try {
        const body = {apply: true};
        if (this.hairStyleSel) body.hair_style = this.hairStyleSel;   // 阶段8：开播页直选样式
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/hair_preset`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.hairPresetPreview = d.preview_image || '';
          this.showToast(`「${prof}」定妆脸已生成并启用（${d.elapsed_ms || '?'}ms）`, 'success');
          this.refreshLookHistIfOpen();
        } else {
          this.showToast('定妆失败：' + this.svcErrText(d), 'warn');
        }
      } catch (e) { this.showToast('定妆失败：' + e, 'error'); }
      finally { this.hairPresetBusy = false; this.loadLabServices(); }
    },

    // ── 换脸 + 发型跟随源照片（离线出片）──────────────────────
    onFaceHairTarget(ev) {                             // 读入目标图（想要的姿势/场景/身体）
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      const fr = new FileReader();
      fr.onload = () => {
        this.faceHair.targetPreview = String(fr.result);
        this.faceHair.targetB64 = this.faceHair.targetPreview.split(',')[1] || '';
        this.faceHair.targetName = f.name;
        this.faceHair.result = '';
      };
      fr.readAsDataURL(f);
    },
    async loadFaceHairAssets() {                       // 目标图资产直选清单（试衣静图/全身照/试衣历史）
      const prof = this.hairPresetProfile || this.active;
      if (!prof || this.faceHair.assetsFor === prof) return;   // 同角色不重复拉
      try {
        const d = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/target_assets`).then(r => r.json());
        this.faceHair.assets = (d.ok && d.items) || [];
        this.faceHair.assetsFor = prof;
        // 当前选项已不在清单里（换了角色）→ 回落上传
        if (this.faceHair.targetSource !== 'upload'
            && !this.faceHair.assets.some(a => a.key === this.faceHair.targetSource))
          this.faceHair.targetSource = 'upload';
      } catch (e) { this.faceHair.assets = []; this.faceHair.assetsFor = ''; }
    },
    faceHairAssetThumb() {                             // 资产缩略 URL（选中非上传项时显示）
      const prof = this.hairPresetProfile || this.active;
      if (!prof || this.faceHair.targetSource === 'upload') return '';
      return HUB + `/api/profiles/${encodeURIComponent(prof)}/target_asset_thumb?key=${encodeURIComponent(this.faceHair.targetSource)}`;
    },
    faceHairTargetPreview() {                          // 结果对比区左图：上传=本地预览，资产=缩略端点
      return this.faceHair.targetSource === 'upload' ? this.faceHair.targetPreview : this.faceHairAssetThumb();
    },
    async runFaceHair() {                              // 二连管线：换脸 → 发型跟随（源照片自身）
      const useUpload = this.faceHair.targetSource === 'upload';
      if (this.faceHair.busy || (useUpload && !this.faceHair.targetB64)) return;
      const prof = this.hairPresetProfile || this.active;
      this.faceHair.busy = true;
      this.faceHair.result = '';
      try {
        // 目标图：资产直选走 target_source（服务端直读磁盘，免 base64 上传）；上传走 target_image
        const body = {carry_hair: this.faceHair.carryHair,
                      paste_back: this.faceHair.pasteBack};
        if (useUpload) body.target_image = this.faceHair.targetB64;
        else body.target_source = this.faceHair.targetSource;
        // profile 交给服务端解析源脸（_profile_swap_face 尊重 use_styled_face 开关）+ 出片存档归属
        if (prof) body.profile = prof;
        const r = await fetch(HUB + '/api/faceswap_with_hair', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (r.ok && d.ok && d.result_image) {
          this.faceHair.result = d.result_image;
          this.faceHair.hairOk = !!d.hair_ok;
          this.faceHair.pastedBack = !!d.pasted_back;
          this.faceHair.histId = d.hist_id || '';
          const t = (d.faceswap_ms || 0) + (d.hair_ms || 0);
          this.showToast(d.hair_ok ? `换脸+发型出片完成（${t}ms）` : `已换脸；发型步跳过/失败`, d.hair_ok ? 'success' : 'warn');
          this.refreshLookHistIfOpen();              // 出片墙即时多一格
        } else {
          this.showToast('出片失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
        }
      } catch (e) { this.showToast('出片失败：' + e, 'error'); }
      finally { this.faceHair.busy = false; }
    },
    async runFaceHairLive() {                           // 全链：出片→底片→Ditto微动(软降级静帧)
      const useUpload = this.faceHair.targetSource === 'upload';
      if (this.faceHair.busy || (useUpload && !this.faceHair.targetB64)) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.faceHair.busy = true;
      this.faceHair.liveStep = '①换脸+发型…';
      this.faceHair.result = '';
      this.faceHair.liveVideo = '';
      try {
        const body = {carry_hair: this.faceHair.carryHair, paste_back: this.faceHair.pasteBack,
                      idle_secs: 6, field: 'idle_video'};
        if (useUpload) body.target_image = this.faceHair.targetB64;
        else body.target_source = this.faceHair.targetSource;
        this.faceHair.liveStep = '①换脸+发型…②写底片…③微动…';
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/facehair_live`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (r.ok && d.ok && d.result_image) {
          this.faceHair.result = d.result_image;
          this.faceHair.hairOk = !!d.hair_ok;
          this.faceHair.pastedBack = !!d.pasted_back;
          this.faceHair.histId = d.hist_id || '';
          this.faceHair.liveVideo = d.video || '';
          this.faceHair.liveAnimated = !!d.animated;
          const st = d.steps || {};
          const part = ['facehair', 'base', 'idle'].map(k => {
            if (!st[k]) return '';
            const zh = {facehair: '出片', base: '底片', idle: '微动'}[k] || k;
            return st[k].ok ? `${zh}✓` : `${zh}✗`;
          }).filter(Boolean).join(' ');
          this.showToast(`「${prof}」数字人底片完成（${part}）——${d.hint || ''}`.slice(0, 200), 'success');
          this.refreshLookHistIfOpen();
        } else {
          this.showToast('全链失败：' + (d.detail || JSON.stringify(d)).slice(0, 140), 'warn');
        }
      } catch (e) { this.showToast('全链失败：' + e, 'error'); }
      finally { this.faceHair.busy = false; this.faceHair.liveStep = ''; }
    },
    async setFaceHairAsBase() {                        // 出片 → 一键设为角色底片(tryon 位)，数字人链吃到发型
      const prof = this.hairPresetProfile || this.active;
      if (!prof || !this.faceHair.histId) return;
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_history/${this.faceHair.histId}/restore`, {method: 'POST'});
        const d = await r.json();
        if (r.ok && d.ok) this.showToast(d.hint || '已设为角色底片', 'success');
        else this.showToast('设底片失败：' + (d.detail || '').slice(0, 120), 'warn');
      } catch (e) { this.showToast('设底片失败：' + e, 'error'); }
    },

    // ── 阶段10 出片历史：缩略图墙 + 一键回滚（服务端每角色滚动 24 版） ──
    async toggleLookHist() {                           // 展开时按当前角色拉最新清单
      this.lookHistOpen = !this.lookHistOpen;
      this.lookHistZoom = '';
      this.lookHistCmp = [];
      if (this.lookHistOpen) await this.loadLookHist();
    },
    lookHistThumbClick(e) {                            // 阶段11：对比开=进对比槽，关=看大图
      if (!this.lookHistCmpMode) {
        this.lookHistZoom = this.lookHistZoom === e.id ? '' : e.id;
        return;
      }
      const i = this.lookHistCmp.indexOf(e.id);
      if (i >= 0) this.lookHistCmp.splice(i, 1);       // 再点取消
      else {
        this.lookHistCmp.push(e.id);
        if (this.lookHistCmp.length > 2) this.lookHistCmp.shift();   // 满2张顶掉最早选的
      }
    },
    lookHistById(id) { return this.lookHist.find(x => x.id === id) || {}; },
    async loadLookHist() {
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.lookHist = []; return; }
      try {
        const d = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_history`).then(r => r.json());
        this.lookHist = d.ok ? (d.entries || []) : [];
      } catch (e) { this.lookHist = []; }
    },
    refreshLookHistIfOpen() {                          // 出片动作成功后墙上即时多一格
      if (this.lookHistOpen) this.loadLookHist();
    },
    lookHistLabel(e) {                                 // 缩略图下一行小字：类型+样式
      const zh = {hair: '发型', makeup: '妆容', lookpack: '定妆包', tryon: '试衣', idle: '微动', facehair: '换脸+发型'};
      return (zh[e.kind] || e.kind) + '·' + ((e.meta || {}).style || '');
    },
    lookHistTitle(e) {                                 // hover 提示：完整信息+时间
      const t = e.ts ? new Date(e.ts * 1000).toLocaleString() : '';
      const v = (e.meta || {}).video ? ((e.meta.video_exists === false) ? '（视频文件已丢失）' : '（含视频）') : '';
      return `${this.lookHistLabel(e)} ${v}\n${t}\n点图看大图`;
    },
    async restoreLookHist(e) {                         // 一键回滚：定妆脸类立即推送生效
      const prof = this.hairPresetProfile || this.active;
      if (!prof || this.lookHistBusy) return;
      this.lookHistBusy = e.id;
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_history/${e.id}/restore`, {method: 'POST'});
        const d = await r.json();
        if (r.ok && d.ok) this.showToast(`已回滚到该版（${(d.applied || []).join('/')}）——${d.hint || ''}`, 'success');
        else this.showToast('回滚失败：' + (d.detail || '').slice(0, 120), 'warn');
      } catch (err) { this.showToast('回滚失败：' + err, 'error'); }
      finally { this.lookHistBusy = ''; }
    },
    async deleteLookHist(e) {                          // 删单条历史（不动角色当前状态）
      const prof = this.hairPresetProfile || this.active;
      if (!prof || this.lookHistBusy) return;
      if (!confirm(`删除这条「${this.lookHistLabel(e)}」历史？`)) return;
      this.lookHistBusy = e.id;
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_history/${e.id}/delete`, {method: 'POST'});
        const d = await r.json();
        if (r.ok && d.ok) {
          if (this.lookHistZoom === e.id) this.lookHistZoom = '';
          const ci = this.lookHistCmp.indexOf(e.id);
          if (ci >= 0) this.lookHistCmp.splice(ci, 1);
          await this.loadLookHist();
        }
        else this.showToast('删除失败：' + (d.detail || '').slice(0, 120), 'warn');
      } catch (err) { this.showToast('删除失败：' + err, 'error'); }
      finally { this.lookHistBusy = ''; }
    },

    async toggleHairPreset() {                         // 原脸 ↔ 定妆脸 切换换脸源
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/hair_preset/toggle`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.showToast(`「${prof}」换脸源 → ${d.use_styled_face ? '定妆脸' : '原始脸'}`, 'success');
        } else {
          this.showToast(d.detail || '切换失败', 'warn');
        }
      } catch (e) { this.showToast('切换失败：' + e, 'error'); }
    },

    // ── C-5 妆容定妆 + Look Pack 一键定妆包 ──────────────────
    async loadMakeupStyles() {                         // 妆容样式清单（服务不在线则为空，UI 自然隐藏）
      try {
        const d = await fetch(HUB + '/api/makeup/styles').then(r => r.json());
        this.makeupStyles = [...(d.presets || []), ...(d.refs || [])];
        this.makeupStylesDetail = d.detail || {};
        if (!this.makeupStyle && this.makeupStyles.length) this.makeupStyle = this.makeupStyles[0];
      } catch (e) { this.makeupStyles = []; }
      this.loadLiveMakeup();
    },

    // ── C-5b 直播妆容层（换脸输出端逐帧上妆；颜色存角色 live_makeup，BGR）──
    bgrToHex(a) {
      if (!Array.isArray(a) || a.length < 3) return '#a51c30';
      const p = v => ('0' + Math.max(0, Math.min(255, v | 0)).toString(16)).slice(-2);
      return '#' + p(a[2]) + p(a[1]) + p(a[0]);
    },
    hexToBgr(h) {
      h = (h || '').replace('#', '');
      if (h.length !== 6) return [48, 28, 165];
      return [parseInt(h.slice(4, 6), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(0, 2), 16)];
    },

    async loadLiveMakeup() {                           // 读激活(或定妆目标)角色的 live_makeup
      const prof = this.hairPresetProfile || this.active;
      if (!prof) return;
      try {
        const d = await fetch(HUB + `/profiles/${encodeURIComponent(prof)}`).then(r => r.json());
        const m = d.live_makeup || {};
        this.liveMakeup = {
          enabled: !!m.enabled,
          lip:   this.bgrToHex(m.lip_color   || [48, 28, 165]),  lipS:   Math.round((m.lip   ?? 0.4)  * 100),
          blush: this.bgrToHex(m.blush_color || [150, 130, 240]), blushS: Math.round((m.blush ?? 0.22) * 100),
          eye:   this.bgrToHex(m.eye_color   || [80, 85, 110]),   eyeS:   Math.round((m.eye   ?? 0.18) * 100),
        };
        this.liveMakeupLoaded = prof;
      } catch (e) {}
    },

    fillLiveMakeupFromStyle() {                        // 从离线妆容预设取色（色彩单一真相）
      const sp = this.makeupStylesDetail[this.makeupStyle];
      if (!sp) { this.showToast('该样式无内置色板（参考图样式请先跑一次妆容定妆，会自动同步色彩）', 'warn'); return; }
      this.liveMakeup.lip = this.bgrToHex(sp.lip_color);     this.liveMakeup.lipS = Math.round((sp.lip || 0.4) * 80);
      this.liveMakeup.blush = this.bgrToHex(sp.blush_color); this.liveMakeup.blushS = Math.round((sp.blush || 0.2) * 80);
      this.liveMakeup.eye = this.bgrToHex(sp.eye_color);     this.liveMakeup.eyeS = Math.round((sp.eye || 0.2) * 80);
      this.showToast(`已取「${this.makeupStyle}」色板（直播层强度自动 8 折，可再微调）`, 'info');
    },

    async saveLiveMakeup() {                           // PATCH 角色 live_makeup；激活角色即时生效(逐帧注入)
      if (this.liveMakeupBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.liveMakeupBusy = true;
      try {
        const m = this.liveMakeup;
        const body = {live_makeup: {
          enabled: !!m.enabled,
          lip_color: this.hexToBgr(m.lip),     lip:   Math.max(0, Math.min(100, +m.lipS   || 0)) / 100,
          blush_color: this.hexToBgr(m.blush), blush: Math.max(0, Math.min(100, +m.blushS || 0)) / 100,
          eye_color: this.hexToBgr(m.eye),     eye:   Math.max(0, Math.min(100, +m.eyeS   || 0)) / 100,
        }};
        const r = await fetch(HUB + `/profiles/${encodeURIComponent(prof)}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (r.ok && d.ok !== false) {
          this.showToast(`「${prof}」直播妆容已${m.enabled ? '开启（下一帧生效）' : '保存（未开启）'}`, 'success');
        } else {
          this.showToast('保存失败：' + (d.detail || JSON.stringify(d)).slice(0, 120), 'warn');
        }
      } catch (e) { this.showToast('保存失败：' + e, 'error'); }
      finally { this.liveMakeupBusy = false; }
    },

    async runMakeupPreset() {                          // 离线 1-3s：妆容烘进定妆脸（发型结果之上）
      if (this.lookPackBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      if (!this.makeupStyle) { this.showToast('先选择妆容样式', 'warn'); return; }
      this.lookPackBusy = true;
      this.lookPackPreview = '';
      this.labSvcColdHint('makeup', '妆容');
      try {
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/makeup_preset`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({style: this.makeupStyle, apply: true})});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.lookPackPreview = d.preview_image || '';
          this.showToast(`「${prof}」妆容「${d.makeup_style}」已烘进定妆脸（${d.elapsed_ms || '?'}ms）`, 'success');
          this.refreshLookHistIfOpen();
        } else {
          this.showToast('妆容定妆失败：' + this.svcErrText(d), 'warn');
        }
      } catch (e) { this.showToast('妆容定妆失败：' + e, 'error'); }
      finally { this.lookPackBusy = false; this.loadLabServices(); }
    },

    async runFullLook() {                              // 阶段9 一键出片：发型→妆容→试衣→微动 全链编排
      if (this.fullLookBusy) return;                   // 复用各块当前选择；哪步没料/服务离线就跳过，绝不断链
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.fullLookBusy = true;
      this.fullLookSteps = [];
      const log = (text, ok = null) => { this.fullLookSteps.push({text, ok}); return this.fullLookSteps.length - 1; };
      const mark = (i, ok, extra) => { const s = this.fullLookSteps[i]; s.ok = ok; if (extra) s.text += ' ' + extra; };
      try {
        // ① 定妆包：发型+妆容 烘进定妆脸（Hub 端逐步软降级）
        const lp = {apply: true};
        if (this.makeupStyle) lp.makeup_style = this.makeupStyle;
        if (this.hairStyleSel) lp.hair_style = this.hairStyleSel;
        else if (this.labSvc.hair && this.labSvc.hair.up) lp.use_hair = true;
        if (lp.makeup_style || lp.hair_style || lp.use_hair) {
          this.fullLookStep = '①定妆包…';
          const i = log('① 定妆包（发型+妆容）…');
          try {
            const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_pack`, {
              method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(lp)});
            const d = await r.json();
            if (r.ok && d.ok) {
              this.lookPackPreview = d.preview_image || '';
              const st = d.steps || {};
              const part = ['hair', 'makeup'].map(k => {
                if (!st[k]) return '';
                const zh = k === 'hair' ? '发型' : '妆容';
                return st[k].ok ? `${zh}✓` : `${zh}✗(${String(st[k].error || '').slice(0, 40)})`;
              }).filter(Boolean).join(' ');
              mark(i, true, `完成（${part}）`);
            } else mark(i, false, '失败：' + (d.detail || '').slice(0, 80));
          } catch (e) { mark(i, false, '异常：' + e); }
        } else log('① 定妆包：未选发型/妆容 → 跳过', false);

        // ② 试衣写底片（需全身照或存照+服装+服务在线；内含 Ditto 待机微动）
        let tryonDone = false;
        if (this.fitting.up && (this.fitting.personB64 || this.fitting.stored) && this.fitting.cloth) {
          this.fullLookStep = '②试衣…';
          const i = log(`② 试衣「${this.fitting.cloth}」写入底片…（~30s）`);
          try {
            const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/tryon_preset`, {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({person_image_b64: this.fitting.personB64,
                                    cloth_name: this.fitting.cloth, field: 'body_video',
                                    cloth_type: this.fitting.clothType,
                                    resolution: this.fitting.resolution})});
            const d = await r.json();
            if (r.ok && d.ok) { tryonDone = true; mark(i, true, `完成（${d.animated ? '带微动' : '静帧'}）`); }
            else mark(i, false, '失败：' + (d.detail || '').slice(0, 80));
          } catch (e) { mark(i, false, '异常：' + e); }
        } else {
          log('② 试衣：' + (!this.fitting.up ? '服务离线'
              : (!this.fitting.personB64 && !this.fitting.stored ? '未传全身照(该角色也无存照)' : '未选服装')) + ' → 跳过', false);
        }

        // ③ 待机微动：试衣已带微动则免；否则用定妆脸/原脸生成 idle_video
        if (!tryonDone) {
          this.fullLookStep = '③微动…';
          const i = log('③ 待机微动（定妆脸/原脸 → idle_video）…（~20s）');
          try {
            const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/idle_motion`, {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({source: 'auto', secs: 6})});
            const d = await r.json();
            if (r.ok && d.ok) mark(i, true, `完成（来源:${d.source}）`);
            else mark(i, false, '失败：' + (d.detail || '').slice(0, 80));
          } catch (e) { mark(i, false, '异常：' + e); }
        } else {
          log('③ 待机微动：试衣底片已带 → 免', true);
        }

        const okN = this.fullLookSteps.filter(s => s.ok === true).length;
        // 阶段13 提示语纠偏：定妆脸/口型底片对激活角色是自动生效的（faces/switch 推送 +
        // body_video 内容寻址下一句自动换新基底），别再误导运营多做一次「重新激活」。
        log(okN ? (prof === this.active
                   ? `🎬 出片完成 ${okN} 步——「${prof}」正在激活中，定妆脸已推送、口型底片下一句自动换新`
                   : `🎬 出片完成 ${okN} 步——激活「${prof}」即全部生效`)
                : '全部步骤未执行/失败，检查各块选择与服务状态', okN > 0);
        this.showToast(okN ? `「${prof}」一键出片完成（${okN} 步）` : '一键出片没有可执行的步骤', okN ? 'success' : 'warn');
        if (okN) this.refreshLookHistIfOpen();
      } finally { this.fullLookBusy = false; this.fullLookStep = ''; }
    },

    async runLookPack() {                              // 一键定妆包：发型(8001 当前样式) + 妆容 链式烘焙
      if (this.lookPackBusy) return;
      const prof = this.hairPresetProfile || this.active;
      if (!prof) { this.showToast('先激活或选择一个角色', 'warn'); return; }
      this.lookPackBusy = true;
      this.lookPackPreview = '';
      try {
        const body = {apply: true};
        if (this.makeupStyle) body.makeup_style = this.makeupStyle;
        // 直选样式优先（服务未起时后端会自动拉起）；未直选则仅在 8001 在线时带「当前激活发型」步
        if (this.hairStyleSel) { body.hair_style = this.hairStyleSel; this.labSvcColdHint('hair', '发型'); }
        else if (this.labSvc.hair && this.labSvc.hair.up) body.use_hair = true;
        const r = await fetch(HUB + `/api/profiles/${encodeURIComponent(prof)}/look_pack`, {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        const d = await r.json();
        if (r.ok && d.ok) {
          this.lookPackPreview = d.preview_image || '';
          const parts = Object.entries(d.steps || {}).map(([k, s]) =>
            `${k === 'hair' ? '发型' : '妆容'}${s.ok ? '✓' : '✗'}`).join(' ');
          this.showToast(`「${prof}」定妆包完成：${parts}（开播即生效）`, 'success');
          this.refreshLookHistIfOpen();
        } else {
          const err = Object.values(d.steps || {}).map(s => s.error).filter(Boolean).join('；');
          this.showToast('定妆包失败：' + (err ? err.slice(0, 140) : this.svcErrText(d)), 'warn');
        }
      } catch (e) { this.showToast('定妆包失败：' + e, 'error'); }
      finally { this.lookPackBusy = false; this.loadLabServices(); }
    },

    async pollCameraStatus() {
      // 每3秒检查摄像头热重载状态，变化时toast提示
      try {
        const d = await fetch(HUB+'/realtime/camera_status').then(r=>r.json());
        if (d.ts && d.ts !== this.camStatusTs && d.state === 'running') {
          this.camStatusTs = d.ts;
          if (this.lastCamIdx !== -1 && d.index !== this.lastCamIdx) {
            this.showToast(`摄像头已热重载至 #${d.index}`, 'info');
          }
          this.lastCamIdx = d.index;
        }
      } catch(e){}
      setTimeout(()=>this.pollCameraStatus(), 3000);
    },

    async loadLogs() {
      this.logsLoading = true;
      try {
        const url = HUB + '/api/logs?limit=100' + (this.logsFilter ? `&level=${this.logsFilter}` : '');
        const d = await fetch(url).then(r=>r.json());
        this.logs = d.lines || [];
      } catch(e) {
        this.logs = ['读取日志失败: ' + e];
      }
      this.logsLoading = false;
    },

    // ── UK1 承接闭环：全站「去启动」的落点必须接得住 ─────────────────────
    async loadSvcCatalog(){
      if(this.svcCatalog.length) return;   // 静态元数据，取一次即可
      try{
        const d=await fetch(HUB+'/api/services/catalog').then(r=>r.json());
        if(d.ok) this.svcCatalog=d.services||[];
      }catch(_){}
    },
    // 各页警示条「去启动」统一入口：跳体检页 + 高亮该服务行（治「说了去启动，启动在哪」）
    goFix(svc){
      this.selfcheckFocus=svc||'';
      this.goTab('selfcheck');
      this.loadSvcCatalog(); this.refreshServices();
      // 定位重试两拍：init 深链(?fix=)场景下目录/行可能尚未渲染，首拍扑空第二拍兜底（id 定位幂等）
      if(svc) [400, 1500].forEach(ms=>setTimeout(()=>{ try{
        document.getElementById('svc-row-'+svc)?.scrollIntoView({behavior:'smooth', block:'center'});
      }catch(_){} }, ms));
    },
    svcCmd(s){ return 'conda activate '+(s.env||'?')+' && python '+(s.script||'?'); },
    async svcStart(name){
      if(this.svcStartBusy) return;   // GPU 服务错峰拉起，一次一个
      const meta=this.svcCatalog.find(s=>s.name===name)||{};
      this.svcStartBusy=name;
      try{
        const r=await fetch(HUB+'/api/engine/start?name='+encodeURIComponent(name),{method:'POST'});
        const d=await r.json().catch(()=>({}));
        if(!r.ok || d.ok===false) throw new Error(d.reason||d.detail||('HTTP '+r.status));
        this.showToast('已发起启动：'+(meta.label||name)+'（模型加载约 '+(meta.delay||15)+' 秒）','info');
        // 就绪轮询到变绿为止（上限=宽限×2，至少 40s）——别让用户自己反复点「重新探测」
        const t0=Date.now(), limit=Math.max((meta.delay||15)*2, 40)*1000;
        while(Date.now()-t0<limit){
          await new Promise(res=>setTimeout(res, 3000));
          await this.refreshServices();
          if(this.services[name]){ this.showToast((meta.label||name)+' 已就绪 ✅','success'); this.svcStartBusy=''; return; }
        }
        this.showToast((meta.label||name)+' 迟迟未就绪——到「日志」页看原因，或按行内命令手动启动','error');
      }catch(e){ this.showToast('启动失败：'+(e.message||e),'error'); }
      this.svcStartBusy='';
    },

    // 交付体检面板：拉最近交付结论 + 现跑联机体检(doctor)
    async runSelfcheck() {
      this.selfcheckLoading = true;
      try {
        const d = await fetch(HUB + '/api/selfcheck?run=1').then(r=>r.json());
        this.selfcheck = {doctor: d.doctor||null, delivery: d.delivery||null};
        if (d.doctor && d.doctor.error) this.showToast('体检执行失败','error');
        else this.showToast('体检完成','success');
      } catch(e) {
        this.showToast('体检请求失败: '+e,'error');
      }
      this.selfcheckLoading = false;
    },
    dcCount(level) {
      const items = (this.selfcheck.doctor && this.selfcheck.doctor.items) || [];
      return items.filter(i=>i.level===level).length;
    },

    startLogRefresh() {
      if (this.logAutoRefresh && this.tab === 'logs') {
        this.loadLogs();
        setTimeout(()=>this.startLogRefresh(), 3000);
      }
    },
    startMetricsRefresh() {
      // P15-1: 每30s递归自刷新指标（与 startLogRefresh 同一模式）
      if(this.metricsAutoRefresh && this.tab==='settings'){
        this.loadMetrics();
        setTimeout(()=>this.startMetricsRefresh(), 30000);
      }
    },

    async checkEnv() {
      try { const d=await fetch(HUB+'/api/env_check').then(r=>r.json()); this.envChecks=d.checks||[]; } catch(e){}
    },

    async kbRefresh() {
      try {
        const d = await fetch(HUB+'/api/converse/kb').then(r=>r.json());
        this.kbCount = d.docs || 0;
      } catch(e) { this.kbCount = 0; }
    },
    async kbImportXi() {
      this.kbLoading = true; this.kbMsg = '';
      try {
        const d = await fetch(HUB+'/api/converse/kb/import_profile/阿习讲话?replace=true',
                             {method:'POST'}).then(r=>r.json());
        if (!d.ok) throw new Error(d.detail || '导入失败');
        this.kbOk = true;
        this.kbCount = d.total || 0;
        this.kbMsg = `✅ 已导入 ${d.added} 条 · 示例：${(d.sample||'').slice(0,40)}…`;
        this.showToast('阿习演讲稿已导入知识库', 'success');
      } catch(e) {
        this.kbOk = false; this.kbMsg = '❌ '+e;
      }
      this.kbLoading = false;
    },
    async kbClear() {
      try {
        await fetch(HUB+'/api/converse/kb', {method:'DELETE'});
        this.kbCount = 0; this.kbMsg = '已清空'; this.kbOk = true;
      } catch(e) { this.kbMsg = '清空失败'; this.kbOk = false; }
    },
    async kbUploadFile(ev) {
      const f = ev.target.files?.[0];
      if (!f) return;
      this.kbLoading = true; this.kbMsg = '';
      try {
        const text = await f.text();
        const d = await fetch(HUB+'/api/converse/kb/import_text', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({text, profile:this.kbUploadProfile||'', replace_profile:!!this.kbUploadProfile})
        }).then(r=>r.json());
        if (!d.ok) throw new Error(d.detail || '导入失败');
        this.kbOk = true;
        this.kbCount = d.total || 0;
        this.kbMsg = `✅ 已切块导入 ${d.added} 条 · 示例：${(d.sample||'').slice(0,40)}…`;
        this.showToast('知识库已更新', 'success');
      } catch(e) {
        this.kbOk = false; this.kbMsg = '❌ '+e;
      }
      this.kbLoading = false;
      ev.target.value = '';
    },
    async kbSearch() {
      const q = (this.kbSearchQ||'').trim();
      if (!q) return;
      this.kbSearchLoading = true;
      try {
        const enc = encodeURIComponent(q);
        const d = await fetch(HUB+`/api/converse/kb/search?query=${enc}&top_k=5`).then(r=>r.json());
        this.kbSearchHits = (d.hits||[]).map(h=>({score:h.score, text:(h.text||'').slice(0,200)}));
      } catch(e) {
        this.kbSearchHits = [];
        this.kbMsg = '检索失败: '+e; this.kbOk = false;
      }
      this.kbSearchLoading = false;
    },

    async exportProfiles() {
      // 选择性导出或全量导出
      const names = this.exportSelected.length > 0 ? this.exportSelected.join(',') : '';
      try {
        const url = HUB + '/api/export_profiles' + (names ? `?names=${encodeURIComponent(names)}` : '');
        const d=await fetch(url).then(r=>r.json());
        const a=document.createElement('a');
        a.href=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));
        a.download='avatarhub_profiles.json'; a.click();
        this.showToast(`已导出 ${d.count} 个角色`, 'info');
        this.exportSelected = []; this.exportSelectAll = false;
      } catch(e){ this.showToast('导出失败：'+((e&&e.message)||e), 'error'); }
    },

    toggleSelectAll() {
      this.exportSelectAll = !this.exportSelectAll;
      this.exportSelected = this.exportSelectAll ? this.profiles.map(p=>p.name) : [];
    },

    // P19-3: 批量删除所选角色
    async deleteSelectedProfiles() {
      if(!this.exportSelected.length) return;
      if(!confirm(`确认删除 ${this.exportSelected.length} 个角色？此操作不可撤销`)) return;
      const names = [...this.exportSelected];
      await Promise.all(names.map(n=>fetch(HUB+`/profiles/${enc(n)}`,{method:'DELETE'})));
      this.exportSelected=[]; this.exportSelectAll=false;
      this.showToast(`已删除 ${names.length} 个角色`, 'success');
      setTimeout(()=>this.loadProfiles(), 300);
    },

    async onImportFile(e) {
      const f = e.target.files[0];
      if (!f) return;
      this.importFile = f;
      try {
        const text = await f.text();
        this.importData = JSON.parse(text);
        // 先请求 preview 查看冲突
        this.importLoading = true;
        const r = await fetch(HUB+'/api/import_profiles', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({profiles: this.importData.profiles||this.importData, mode: 'preview'})
        }).then(r=>r.json());
        this.importPreview = r;
        this.importLoading = false;
      } catch(e) {
        this.importLoading = false;
        this.showToast('文件解析失败：'+((e&&e.message)||e), 'error');
      }
    },

    async confirmImport() {
      if (!this.importData) return;
      this.importLoading = true;
      try {
        const r = await fetch(HUB+'/api/import_profiles', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            profiles: this.importData.profiles||this.importData,
            mode: this.importMode
          })
        }).then(r=>r.json());
        this.importLoading = false;
        this.importShow = false;
        this.showToast(`导入成功: ${r.imported} 个角色`, 'info');
        this.loadProfiles();
      } catch(e) {
        this.importLoading = false;
        this.showToast('导入失败：'+((e&&e.message)||e), 'error');
      }
    },
  };
}

const enc = s => encodeURIComponent(s);
