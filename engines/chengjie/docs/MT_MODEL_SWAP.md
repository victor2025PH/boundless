# 本地 MT 模型：怎么评、怎么换、怎么补位

> 面向「听说有个新的开源翻译模型更强，我们要不要换」这个反复出现的问题。
> 结论先行：**多数时候不该整体替换，而该当补位层用**——理由见下面的实测。

## 一、为什么不能直接拿新模型跑一遍旧评测

`OllamaMTEngine` 的名字是通用的，但一个 MT 模型的契约有四项，缺一项都会静默失真：

| 契约项 | Hunyuan-MT | MiLMMT-46 |
|---|---|---|
| prompt 格式 | 指令式「把下面的文本翻译成X，不要额外解释」 | 补全式 `Translate this from A to B:\nA: 原文\nB:` |
| 调用模式 | `chat.completions`（套 chat template） | `completions`（裸续写，官方用法） |
| 语种集 | 33 语 | 46 语（**不是超集**） |
| 语种命名 | 「中文」 | `Chinese (Simplified)`（简繁分训） |

这四项曾被焊死在引擎类里。**喂错档不报错**——2026-08-03 实测，MiLMMT 被喂 Hunyuan
prompt 时完全无视输入、胡乱吐客服套话（「我爱机器翻译」→「请告诉我你最喜欢的食物
是什么？」）。若不先解耦就跑横比，会得出「新模型很差」这个 100% 由测试台造成的
假结论。

现在契约集中在 `src/ai/mt_profiles.py`，配置项 `translation.engines.ollama_mt.profile`
（`hunyuan_mt` 默认 / `milmmt46`）。**换 model 必须同时换 profile。**

## 二、怎么评：`scripts/xlate_ab.py`

```bash
python -m scripts.xlate_ab \
  --a "ollama:hy-mt2-7b-official:latest@http://192.168.0.176:11434" \
  --b "ollama:hf.co/mradermacher/MiLMMT-46-4B-v0.1-GGUF:Q4_K_M@http://192.168.0.176:11434" \
  --judge ai \
  --dataset config/eval/translation_samples_hymt.yaml \
  --out-jsonl logs/eval/xlate_ab.jsonl
```

四条设计上的硬要求，都是被真实假结论逼出来的：

1. **两个候选钉在同一台 GPU**。不同卡的延迟数字没有可比性；`--a config` 会取配置
   端点，与手动指定的 B 可能落在不同主机（本仓实测踩过：配置指 140、新模型在 176）。
2. **裁判必须第三方**。候选自己回译自己量的是「复读自己措辞」的自洽度；裁判与候选
   同源时本工具直接 abort。
3. **看置信区间和效应量，不看均值**。10 样本冒烟曾给出「A 更好」，50 样本全量则是
   「无显著差异」——均值差会骗人。
4. **档名写进报告标签**（`ollama_mt:模型名[档名]`），读报告的人一眼能看见这一轮是
   按哪个契约调的。

## 三、2026-08-03 实测：MiLMMT-46-4B vs HY-MT2-7B

50 样本宽语料，DeepSeek 当第三方回译裁判，两者同跑 176（5090）。

| 维度 | HY-MT2-**7B**（生产） | MiLMMT-46-**4B** | 结论 |
|---|---|---|---|
| 语义分 | 0.947 | 0.934 | Δ=−0.0125，95%CI [−0.027, −0.0004] |
| 判词 | — | — | 统计显著但**低于可行动阈 0.02** → 实质等价 |
| 逐样本胜负 | A 胜 22 | B 胜 13 | 平 12 |
| 显存 | 4.91 GB | **2.68 GB** | 省 45% |
| 延迟 median | 233 ms | 247 ms | 略慢（补全式 prompt 前缀更长） |
| 延迟 p90 | 393 ms | **261 ms** | 尾部低 34% |
| 语种 | 33 | 46 | +17，**但少 te/mr/gu/uk** |

论文里 MiLMMT-46-4B 胜过的是 HY-MT **1.5**；本仓生产跑的是 **HY-MT2**（新一代），
所以论文结论不能直接搬——这正是要自己跑横比的原因。

**决策：不整体替换。** 质量略逊，换了在主语料上什么也换不到；但 17 个 HY-MT2 拒收的
语种目前全部漏到付费云端，让 MiLMMT 在本地接住这批才是真收益（代价 2.68 GB 显存）。

## 四、怎么补位：注册第二个本地 MT 实例

`order` 里的未知名，只要同名配置块声明 `type: ollama_mt`，就再装一台本地 MT，
引擎名即该键。两个实例各带自己的语种白名单，router 按 `supports_target` 天然顺移。

```yaml
translation:
  engines:
    order: ["ollama_mt", "milmmt", "ai"]
    ollama_mt:
      model: "hy-mt2-7b-official:latest"
      profile: "hunyuan_mt"
    milmmt:
      type: "ollama_mt"
      base_url: "http://192.168.0.176:11434"
      model: "hf.co/mradermacher/MiLMMT-46-4B-v0.1-GGUF:Q4_K_M"
      profile: "milmmt46"
```

不写这种块 = 行为与单模型部署完全一致（默认关）。引擎名也是 `per_lang_order`、
统计、日志的寻址键——所以必须是自定义名，同名区分不出两个 model。

上线前请确认 te/mr/gu/uk 四语的落点：主力仍收（`ollama_mt` 在前），补位不参与，
语义未变；但若将来把主力换成 MiLMMT，这四语会直接掉到云端。

## 五、门禁

- `tests/test_mt_profiles.py`——默认档 prompt 逐字冻结、两模型语种集非包含关系、
  补全式必须走 `completions` 口（走 chat 口即断言失败）、未知档名回落告警、
  多实例注册与默认名不漂移。
- `tests/test_translation_ab.py`——A/B 裁判台的配对/自举/判词/污染检测。
