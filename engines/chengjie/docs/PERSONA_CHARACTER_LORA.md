# 每人设「角色 LoRA」管线（真人感根治）

> 目标：让同一个人设的照片**又像本人、又姿态表情自然多样**，摆脱 PuLID 单图锁脸的
> "证件照连拍"感。这是所有 AI 网红号的工业级做法：用一批多样图训一个 per-persona
> LoRA，身份"焙"进权重后，可用变化 prompt + 随机种子自由出图、几乎丢掉 PuLID。
>
> 全程 **SFW**。本管线**不**依赖任何去限制/NSFW 模型——"真人感"靠写实底色 + 姿态多样
> + 皮肤纹理，与内容尺度正交。

## 为什么需要它（根因）

`companion_selfie` 现在的锁脸链是 **PuLID-Flux 从一张正面定妆照零样本注入**。三重叠加把
构图钉死：① PuLID `start_at=0.0` 从去噪第 0 步就锁脸（构图/头位置就是这时定的）；
② 固定种子同底噪；③ prompt 恒「looking at the camera」。

短期缓解已上线（见 `companion.selfie.variety` + `--pulid-start-at`），但**根治**是角色
LoRA——PuLID 是"没学过这个人时的权宜"，LoRA 是"真的学会这个人"。

---

## 全流程（4 步）

### 前置：在 176（RTX 5090）上装训练环境

角色 LoRA 训练是**独立于本仓库**的外部任务（本进程铁律：不加载 GPU 模型）。推荐
[ai-toolkit (ostris)](https://github.com/ostris/ai-toolkit)（FLUX LoRA 事实标准，配置简单）：

```bash
# 在 176 上（与 ComfyUI 同机，共用 5090）
git clone https://github.com/ostris/ai-toolkit
cd ai-toolkit && git submodule update --init --recursive
python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
# FLUX.1-dev 权重：可复用 ComfyUI 已下载的，或 huggingface-cli 下到本地目录
#   （dev 需同意 HF 上的非商用许可——见文末合规提示）
```

> 备选训练器：`kohya_ss` sd-scripts（flux 分支）、`SimpleTuner`。config schema 不同，
> 本仓库 `build_aitoolkit_config` 只产 ai-toolkit 格式；换训练器则手写 config。

### 第 1 步：批量出多样训练图（本机跑，出图落 176 ComfyUI）

```bash
python tools/persona_lora_dataset.py --persona lin_xiaoyu --n 28
```

- 用 `assets/persona_media/lin_xiaoyu/face_ref.png` 锁脸 + `--pulid-start-at 0.15`（放开姿态）
  + 每张 `variety_salt=i`（穷举景别/角度/表情/视线）→ `datasets/lora/lin_xiaoyu/img_000.png…`
- 缺 face_ref 会警告并无锁脸出图（人脸会漂）——**先跑 `tools/persona_photoshoot.py` 定妆**。
- 先出 `--n 8` 眼看质量，满意再出满 28~40 张。

### 第 2 步：自动清洗（双门）+ 打标

```bash
pip install insightface onnxruntime   # 人脸身份门需要（CPU onnx，一次性）
python tools/persona_lora_dataset.py --persona lin_xiaoyu --n 28 \
    --curate --caption --trigger linxy --subject-class woman --emit-train-config
```

- `--curate`＝**两道正交门，源头治理**（垃圾进=垃圾出，脏样本对 LoRA 毒害远大于少几张）：
  - **① 人脸身份门**（本地嵌入 ~200ms，先跑）：每张脸与 `face_ref` 比 ArcFace 余弦，
    无脸/低于 `--face-min`(默认 0.35) → 剔（"是个人但不是本人"会把 LoRA 带偏）。
  - **② VLM 内容门**（HTTP ~1-3s，对人脸门存活者跑）：判多人/动物/文字/非真人。
  - 先便宜后昂贵，省 VLM 调用；被剔的移到 `_rejected/`。缺 insightface → 仅走 VLM 门（优雅降级）。
- **训练集身份健康度**：清洗时算出的嵌入顺手汇总，manifest.json 记 `identity_health`
  （`ref_mean`/`min`/`self_consistency`）——**训练前就量出数据集有多纯**，预判 LoRA 上限。
- `--caption`：每张产客观描述 → 写 `img_XXX.txt`，形如
  `linxy woman, half-body, looking away, soft smile, cozy dorm, daylight`。
  **trigger（`linxy`）是稀有触发词**——出图时 prompt 带上它才唤起这个人设。
- `--emit-train-config`：生成 `datasets/lora/lin_xiaoyu_flux_lora.yaml`（ai-toolkit 格式）。
- 双门后仍建议人眼扫一遍，确认 20~30 张干净多样即可（多不如精）。

**批量所有人设**（VLM 客户端只建一次跨人设复用）：

```bash
python tools/persona_lora_dataset.py --all-personas --n 28 --curate --caption --emit-train-config
```

各人设出到 `datasets/lora/<pid>/`、各自生成 `<pid>_flux_lora.yaml`。注意 `--trigger` 是全局的，
批量时建议**每人设单独跑**给不同 trigger（或先批量出图、再逐人设 `--caption --trigger`）。

### 第 3 步：在 176 上训练

把数据集目录 + 生成的 yaml 传到 176（或共享盘），改 yaml 里的路径：
- `datasets[0].folder_path` → 176 上数据集绝对路径
- `config.process[0].model.name_or_path` → 176 上 FLUX.1-dev 目录（或 HF repo id）
- `config.process[0].training_folder` → 输出目录

```bash
# 176 ai-toolkit 目录下
python run.py <路径>/lin_xiaoyu_flux_lora.yaml
```

- 5090（32G）：`model.quantize` 可设 `false` 提速；rank 16、2000 步、bf16 通常 20~40 分钟。
- 训练中每 250 步出采样图（yaml `sample.prompts` 用了 trigger）。
- 产物：`training_folder/lin_xiaoyu_flux_lora/*.safetensors`（多个 step 快照）。**别再人眼挑**——
  下一步用保真度分**自动选优**。

### 第 3.5 步：checkpoint 自动选优（客观选最像的那个 step）

把所有 step 快照拷进 **176 ComfyUI 的 `models/loras/`**（或 `extra_model_paths` 指向训练输出），
然后让 `persona_lora_eval` 对每个候选各生成几张、抽脸比 `face_ref`，**按 `ref_mean` 排名选最优**：

```bash
pip install insightface onnxruntime   # 一次性（CPU onnx，不占显存）
python tools/persona_lora_eval.py --persona lin_xiaoyu \
    --checkpoints-dir D:/ComfyUI/models/loras/lin_xiaoyu_steps \
    --trigger linxy --select-n 6 --write-registry
```

- 逐候选评测 → 打印 best-first 排名（verdict → ref_mean → 自一致性 → 无脸率）→ ★ 标出最优。
- `--write-registry`：把最优写回 **`config/persona_lora.json`**（机器独占 JSON，不碰人工 YAML）。
- `--select-n 6`：每候选 6 张省 GPU；候选须已在 ComfyUI `models/loras`（按**文件名**引用）。

### 第 4 步：部署回 ComfyUI 出图链（注册表已自动生效）

**若上一步用了 `--write-registry`，部署已完成**——`resolve_persona_lora` 按「人设字段 > 注册表 >
全局」自动读 `config/persona_lora.json`，出图链即挂上最优 LoRA，**无需改任何 YAML**。只需确认
`command_args` 用了占位符（下），且首次挂 LoRA 需重启一次让配置生效：

1. LoRA `.safetensors` 在 **176 ComfyUI 的 `models/loras/`**。
2. `command_args` 用**占位符**（不写死路径），一条命令服务所有人设：

```yaml
companion:
  selfie:
    variety:
      enabled: true
    stable_seed: false
    provider:
      # {base}=该人设 face_ref 自动解析、{lora}/{lora_weight}=该人设 LoRA 自动注入。
      # 有角色 LoRA 后 PuLID 可降到很轻(--face-weight 0.4、start_at 0.2)甚至删掉 --face-ref。
      command_args: [python, tools/comfy_infer.py, --url, "http://192.168.0.176:8188",
        --prompt, "{prompt}", --out, "{out}", --seed, "{seed}", --face-ref, "{base}",
        --lora, "{lora}", --lora-weight, "{lora_weight}",
        --pulid-start-at, "0.2", --face-weight, "0.4"]
```

3. **每人设的 LoRA 从哪来**（`resolve_persona_lora` 三层优先级，`{lora}`/`{lora_weight}`/触发词
   自动按人设注入）：

   - **① 注册表（推荐，机器写回）**：`config/persona_lora.json`——`persona_lora_eval --write-registry`
     选优后自动写，**无需手改 YAML、不丢注释**。schema：`{"lin_xiaoyu": {"file","trigger","weight"}}`。
   - **② 人设字段（人工钉死，最高优先）**：`profiles_runtime.yaml` 每人设下 `lora_file`/`lora_trigger`/
     `lora_weight`（想覆盖注册表/固定某版时用）。
   - **③ 全局兜底**：`companion.selfie.lora.{file,trigger,weight}`（单人设/统一 LoRA）。

```yaml
# profiles_runtime.yaml —— 仅在想人工钉死/覆盖注册表时才填（否则交给 --write-registry 注册表）
profiles:
  lin_xiaoyu:
    appearance: "a 22-year-old East Asian woman, ..."
    lora_file: "lin_xiaoyu_flux_lora.safetensors"
    lora_trigger: "linxy"
    lora_weight: 0.9
```

   trigger 由 `build_selfie_prompt(lora_trigger=)` 自动领衔，无需手改 appearance。

> - **多 LoRA 叠加**：`{lora}` 支持逗号分隔多个（角色 + 写实皮肤 LoRA 串联，都在 PuLID 前）。
> - **含 TE 的写实/风格 LoRA**：command_args 加 `--lora-clip`（默认只挂 UNet=角色 LoRA 常规）。

---

## 调参速查

| 现象 | 调整 |
|---|---|
| 还是像但姿态僵 | `--face-weight` 再降（0.3）、`--pulid-start-at` 再抬（0.25），或直接去掉 `--face-ref` 纯靠 LoRA |
| 不够像本人 | `--lora-weight` 提到 1.0；或训练加步数/加干净样本；确认 prompt 带了 trigger |
| 皮肤蜡感 | 叠写实 LoRA（`--lora a,b`）；prompt 加 "natural skin texture, pores, film grain" |
| 脸崩/多手 | 数据集里剔崩坏样本重训；出图 `--steps` 提到 24~28 |
| 训练过拟合（每张同背景） | 数据集背景/光线更杂；`caption_dropout_rate` 提到 0.1 |

## GPU 编排（可选，有权衡——务必先读结论）

`command_args_noface` 让**无脸/轻量图**（物体图、无 face_ref 的自拍）走独立命令、可指向
另一台 ComfyUI，把重的锁脸自拍留在主卡（176/5090）：

```yaml
companion:
  selfie:
    provider:
      command_args: [python, tools/comfy_infer.py, --url, "http://192.168.0.176:8188", ...]
      command_args_noface: [python, tools/comfy_infer.py, --url, "http://192.168.0.140:8188",
        --prompt, "{prompt}", --out, "{out}", --seed, "{seed}"]
```

**深思后的诚实结论（默认不建议开）**：
- 140（4070 12G）已承担本管线依赖的 **VLM 出图体检**（`vision.base_urls`）+ 嵌入 + ASR。
  在上面再跑 FLUX schnell（~12G）会与 VLM **抢显存**——生图体检变慢，可能净负收益。
- schnell fp8 在 12G 上本就吃紧，易 OOM。
- 同卡出图由 `comfy_infer` 跨进程锁**串行化**（防显存峰值 OOM），是正确设计；真并行只能靠
  **第二块空闲 GPU**。
- **推荐**：除非有一台**专用空闲 GPU** 主机，否则别分流；宁可靠主卡串行 + `--free-after`
  卸载腾显存。本 `command_args_noface` 是"有富余算力时的开关"，不是默认优化。

## 验收：身份保真度硬指标（人脸版声纹探针）

训完别只靠人眼——用 `tools/persona_lora_eval.py` 出**客观分**（对标语音的
`voice_similarity_probe`）：按生产同款参数生成 N 张 → insightface 抽 ArcFace 脸向量 →
与 `face_ref` 比余弦（**保真度**：像不像本人）+ N 张两两比（**自一致性**：是不是同一个人）
+ **无脸率**（检不到脸=构图崩/狗图，硬失败）→ 出 ok/warn/fail，追加 `logs/lora_fidelity.jsonl`。

```bash
# 启用一次（opt-in，不进 requirements，CPU onnx 不占显存）：
pip install insightface onnxruntime
# 评测（生产同款 LoRA+PuLID+多样 prompt 现生成 12 张）：
python tools/persona_lora_eval.py --persona lin_xiaoyu --n 12
python tools/persona_lora_eval.py --persona lin_xiaoyu --baseline        # A/B：量 LoRA 边际身份增益
python tools/persona_lora_eval.py --from-dir datasets/lora/lin_xiaoyu    # 零 GPU 复评已有图
python tools/persona_lora_eval.py --all-personas
```

- **刻度（provisional）**：ArcFace 余弦真实同人对常 ≥0.5、验证阈 ~0.4、<0.28 判不同人；
  生成脸 vs 参考照通常略低，故 ok 阈 0.4 偏宽。默认目标 `ref_mean≥0.4 且 p10≥0.3 且 无脸率≤15%`。
- **A/B**：`--baseline` 额外跑无 LoRA（仅 PuLID）同 seed 对照，`ΔLoRA` 正值=LoRA 更像本人，
  直接回答"这个 LoRA 值不值得上"。
- **自动收紧**：攒够 15 行历史后 `calibrate_fidelity_floor` 可按你本地实际分布收紧阈值
  （p10-0.05），同语音自然度"攒够再守门"纪律。
- 建议接 **AvatarPrerenderNightly** 顺带跑（`--from-dir` 复评零 GPU，或低峰现生成），
  verdict=fail 退出码 1 可触发告警。
- verdict=fail 场景与处置见上「调参速查」（不像→提 lora_weight/加样本重训；大量无脸→数据集剔崩坏）。

## 观测与回归

- 出图日志已带 `lora=<name>@<weight>` + `pulid(w,start-end)`（`comfy_infer` stderr）。
- 人脸一致性：**客观分**见上（`persona_lora_eval` → `logs/lora_fidelity.jsonl`）+ 现有
  `image_gate` VLM 体检把关多人/性别/水印。
- 纯函数门禁：`tests/test_persona_lora.py`（trigger/标注/VLM 判定/ai-toolkit 配置）、
  `tests/test_face_fidelity.py`（cosine/自一致性/分级/聚合/总判/校准/嵌入器降级）、
  `tests/test_comfy_infer_workflow.py`（LoRA/clip/PuLID 节点路由）。

## ⚠ 合规提示

- **FLUX.1-dev 是非商用许可**。角色 LoRA 基于 dev 训练/推理继承该限制。你现有链路
  已给无脸物体图路由 schnell（Apache 2.0 可商用）——人物自拍这条若要商用，需评估
  改用可商用基座（如 FLUX schnell 训 LoRA，或其它商用许可写实模型）。
- 本管线只产 SFW 图（`build_selfie_prompt` 强制 `safe-for-work, no nudity` 且数据集
  prompt 复用同约束）。请勿把去限制/NSFW 数据混进训练集。
