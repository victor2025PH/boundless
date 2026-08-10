# spoken_style — 真人感文本层交付包（AvatarHub → 智聊/ChatX）

> 让数字人「说人话」的全部文本侧手艺，打包成一个零重依赖的 Python 包。
> 只动文本、与具体 LLM/TTS 解耦；智聊侧在回复管线上挂三个函数调用即可接入。
> 冒烟：`python smoke_test.py`（零外网、注入式假 LLM、红绿双向标定，26 项）。
> 先看效果：`docs/AB_SAMPLES.md`（裸 LLM vs 挂包对照，qwen14b 实跑）；
> 想自己试：`python examples/ab_demo.py --ask "你周末干嘛"`（它就是接入三步的实跑版）。

## 这是什么、为什么这么拆

AvatarHub 电话对话线一年的实战结论：**「像真人」的大头不在声学，在文本**——
TTS 只能"演"你给它的稿子，稿子先得是嘴里的话。GPT 语音的"逻辑感"一半来自
内容本身是口语（盲听 v5/v6 实锤：太连贯=假；写进文本的思考声/重复/停顿，
现代 TTS 能 100% 如实渲染）。所以真人感拆成四层，全部只动文本：

| 层 | 干什么 | 入口 | 注入位置 |
|---|---|---|---|
| L1 稳定 system 段 | 人设卡+说话指纹+副语言协议+情绪标记协议 | `system_blocks()` | 会话级 system（不变，KV 缓存友好） |
| L2 轮变尾注 | 说话稿档位（0-3）+ 意图感知篇幅 | `turn_tail_hint()` | 每轮消息尾（**别**进稳定 system） |
| L3 出口清洁/渲染 | 摘情绪标签→TTS 情绪键；剥 [轻笑]/[呼吸]→渲染参数 | `clean_reply()` | LLM 产物 → 字幕/TTS 之间 |
| L4 口语化改写(可选) | 本地小模型整段重写句子架构，事实锁把关 | `colloquial_rewrite.build_rewrite_fn()` | L3 之前，失败原句直通 |

L1+L2 是纯字符串拼接（零依赖、零延迟）；L3 是纯正则（零依赖）；
只有 L4 需要一个 OpenAI 兼容的小模型端点（qwen14b 级即够）+ `httpx`。

## 快速接入（智聊回复管线三挂点）

```python
import sys; sys.path.insert(0, r"D:\boundless\platform")   # 或以包形式安装
import spoken_style as ss

# ① 建会话时：拼稳定 system（人设卡可选、说话指纹可选）
card = ss.load_persona("data/personas/Mizuki.json")        # 或自己的卡（schema 见下）
sys_prompt = 业务system + "\n\n" + "\n\n".join(
    ss.system_blocks(persona_card=card, role="美月", laugh=False, emotion_tags=True))

# ② 每轮：用户输入拖一条轮变尾注（跟输入意图变——附和/续讲/闲聊篇幅不同）
tail = ss.turn_tail_hint(user_text, level=2)   # 0=播音 1=口语流畅 2=日常唠嗑 3=故事陪伴
reply = your_llm(sys_prompt, history, user_text + tail)

# ③ 出口：清洁 + 拿 TTS 渲染参数（字幕和 TTS 都用 clean 后的文本）
r = ss.clean_reply(reply)
# r = {"text": 干净文本, "emotion": "happy", "intensity": "中",
#      "paraling": {"laugh":0,"big_laugh":0,"breath":1}, "pause_extra_ms": 250}
```

纯文字聊天产品只用 ①② 也成立（③ 的情绪键/停顿是给配音用的，
但 `clean_reply` 的剥标记兜底建议保留——协议开着就可能有标记漏出）。

### TTS 侧怎么用 r["emotion"]（117 本机就有现成引擎）

- **CosyVoice3**（117 机 `emotion_tts_standby` :7852）：
  `POST /v1/tts/clone` 带 `emotion=r["emotion"]`，服务端自拼 instruct2 措辞
  （键表与措辞的单一真相见 `emotion_instruct.py`）；自定义演绎走 `/v1/tts/instruct`。
- **注意**：instruct 演绎会轻微牺牲音色相似度——追音色最像时传 `neutral`，
  把情绪交给文本本身（这正是 L2 说话稿的设计：写法即演法）。
- `r["pause_extra_ms"]`：该句句尾停顿加这么多毫秒（[呼吸] 的渲染）；
  `r["paraling"]["laugh"]`：有真笑素材才拼音频，**没有就忽略**（假笑比没笑更毁真实感，
  这也是 `system_blocks(laugh=False)` 缺省用呼吸版提示词的原因）。

### L4 口语化改写（可选，进阶）

LLM 对口语指令按下限执行（云模型尤甚），L2 只能贴句尾膏药；L4 用本地小模型
整段重写句子架构，四道保险（详见 `colloquial_rewrite.py` 模块头）：
热旗标灰度 / **事实锁**（数字·英文·否定·问句·标记逐项校验，不过即弃）/
超时铡刀（超时原句直通，绝不拖慢）/ 打断让路（调用方 cancel 竞速）。

```python
os.environ["CONV_COLLOQ_LLM"] = "http://<你的小模型>/v1/chat/completions"  # OpenAI 兼容
os.environ["CONV_COLLOQ_MODEL"] = "qwen14b"
# 开关：data/conv_colloquial.flag 写 "on"（或环境变量 CONV_COLLOQUIAL=1）
fn = ss.colloquial_rewrite.build_rewrite_fn("美月")        # 角色须在 speech_prints.json
out = await fn(reply_segment, first=True)                  # None=直通原句
```

## 数据文件（schema 带样例）

- `data/personas/*.json` — 人设卡。字段白名单：`identity/style/greeting_habit`
  (str≤200) + `catchphrases/boundaries/tone_words`(list≤10×50)。样例 `Mizuki.json`。
  硬规则（boundaries+听不清澄清）组装时优先预算、**不会被截断**（源仓 P1 教训）。
- `data/speech_prints.json` — 角色说话指纹：`print`(一句话画像) `catch`(口头禅池)
  `example`(语感范文) `guide`(可选，覆盖默认「碎句+想词」指引——话少句短/播报
  定位的角色必须自带，否则反人设；样例「秦震」就是 guide 反例样板)。
  mtime 热加载，改了即时生效。

## 环境变量（全部可缺省）

| 变量 | 缺省 | 说明 |
|---|---|---|
| `CONV_COLLOQ_LLM` | `.173` ollama | L4 改写后端（OpenAI 兼容 chat/completions） |
| `CONV_COLLOQ_MODEL` | `qwen14b-fallback` | L4 模型名 |
| `CONV_COLLOQ_T1` / `T2` | 2.8 / 5.0 | 首段/后续段超时铡刀（秒） |
| `CONV_COLLOQ_MIN_HAN` | 6 | 短于此汉字数直通不改 |
| `CONV_COLLOQUIAL` | 0 | =1 等效旗标 "on"（旗标优先） |

## 出处与单一真相（同步纪律）

本包是 **engines/avatarhub**（当前仓 `C:\模仿音色`，2026-08-10 快照）的抽取交付：

| 本包文件 | 源 | 抽取方式 |
|---|---|---|
| `colloquial_rewrite.py` | 同名文件 | **字节一致拷贝**（改进直接 diff 回灌） |
| `emo_tag.py` | 同名文件 | **字节一致拷贝** |
| `naturalness.py` | `avatar_hub.py` `_spoken_style_hint`/`_PARALING_*` + `conversation.py` `_reply_style_hint` | 模板逐字一致，拼装逻辑去 env 化 |
| `persona.py` | `avatar_hub.py` `_persona_sanitize/_persona_prompt` | 逐字一致，HTTPException→PersonaError |
| `emotion_instruct.py` | `emotion_tts_server.py` `EMOTION_INSTRUCT/_fmt_instruct` | 逐字一致 |

在单仓合并（avatarhub 并入 engines/）之前，**avatarhub 侧是单一真相**：
提示词/判据的改进先落 avatarhub 实战验证，再同步到本包（字节拷贝件直接覆盖 +
跑 `smoke_test.py`；抽取件对照上表锚点搬文本）。智聊侧**不要**在本包里改
模板文本——要改请提回 avatarhub 线，避免两处口径漂移。

## 分工

- **AvatarHub 线**：维护本包（模板迭代、事实锁判据、同步）；提供盲听对照样本。
- **智聊线**：管线三挂点接入（①②③）、人设卡与说话指纹的角色化内容、
  自己产品的自然度档位缺省值（对话产品建议 2，客服/播报场景 1 或 flavor<0.5）。
- 有问题先跑 `smoke_test.py` 定位是包坏了还是接入姿势不对。

### chengjie（智聊引擎）桥接已就位（L1–L4 全线）

`engines/chengjie/src/ai/spoken_style_bridge.py` 已把四层接进 `ai_client`
（**默认关**，`config.yaml` 的 `ai.spoken_style.enabled: true` 一键试点；
中文消息才注入尾注、不与自有 persona/spoken_variant 叠加；L4 改写另有
`rewrite: true` 独立开关——事实锁把关、失败原句直通，与 voice_colloquial_llm
只开一个。详见桥接文件头与 config.example.yaml 注释）。
契约测试 `tests/test_spoken_style_bridge.py`（七案红绿双向，可独立直跑）。
