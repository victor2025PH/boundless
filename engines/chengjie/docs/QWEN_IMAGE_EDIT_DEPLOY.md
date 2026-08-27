# Qwen-Image-Edit-2511 部署 SOP（工具箱「AI 生成图片」引擎）

> 2026-08-21 · 配套工具箱 `cp-image` 卡的 `qwen_edit` / `z_image` 引擎。
> 代码侧（`tools/comfy_infer.py --engine` + `companion.selfie.provider.manual_ui.engines`）
> 已上线；本文件是**运维在有 GPU 的主机上部署模型**的操作手册。模型未装时前端选
> `qwen_edit`/`z_image` 会收到「生成失败 → 切引擎/联系运维」，不影响 `flux_pulid`（现役）。

---

## 0. 一句话

在某台 5090 的 ComfyUI 里装好 Qwen-Image-Edit-2511 的模型文件 →
在该 ComfyUI 里跑通一次参考图编辑 → 把跑通的工作流导出成 API 格式 JSON →
在 `config.local.yaml` 把 `qwen_edit` 引擎的 `command_args` 指向该 ComfyUI + 该模板 →
坐席工具箱选 Qwen 引擎即可出图。**零代码改动、免重启**（provider 配置热重载）。

---

## 1. GPU 分配决策（按局域网算力实测，2026-08-21）

| 主机 | 卡 / 显存 | 现状 | 适配性 |
|---|---|---|---|
| **176** | 5090 / 32G | 现役 ComfyUI+FLUX、ASR、Ollama VL/MT/14b；稳态余 ~5.7G | **默认落点**：与 FLUX 热切换 |
| **173** | 5090 / 32G | vLLM-27B(AWQ) + IndexTTS-2；Ollama 空 | escape hatch：需先泊车 vLLM/IndexTTS |
| 140 / 198 | 4070 / 12G | 嵌入/CosyVoice / qwen3:8b | ✗ 12G 放不下 20B 图模 |
| 117 | 3060 | 引擎实例 | ✗ 不做图模主机 |

**决策：默认把 `qwen_edit`（及 `z_image`）指向 176 现役 ComfyUI**（`http://192.168.0.176:8188`），与 FLUX **模型热切换**——理由：

1. 手动出图是**低频**动作（坐席按需点），冷切换成本（卸 FLUX 载 Qwen ~30-60s）可接受；
2. FLUX 与 Qwen-Edit **同为出图用途、单次请求只用一个**，天然不并存；
3. `comfy_infer.py` 出图前会**先卸同卡 Ollama 非-VLM 模型**腾显存（`ensure_vram`），
   Qwen 20B GGUF Q4（~12-14G）+ 常驻 VLM(5.5G) 在 32G 卡上够放；
4. 复用 176 现役 ComfyUI = **零新服务部署**。

**何时改用 173**：坐席手动出图用量起来、或热切换抖动明显（FLUX↔Qwen 频繁互卸拖慢）→
在 173 泊车 vLLM/IndexTTS 后起独立 ComfyUI，把 `command_args` 的 `--url` 改成
`http://192.168.0.173:8188`。**只改一行 config，无需改码、无需重启**。

---

## 2. 模型文件（在选定主机的 ComfyUI 上装）

Qwen-Image-Edit-2511（Apache 2.0，可商用）三件套 + 文本编码器：

| 组件 | 放到 ComfyUI 目录 | 备注 |
|---|---|---|
| 扩散模型（GGUF 量化，5090 建议 Q5/Q6） | `models/unet/` 或 `models/diffusion_models/` | 20B；GGUF 需 `ComfyUI-GGUF` 自定义节点 |
| 文本编码器 `qwen_2.5_vl_7b`（fp8） | `models/text_encoders/` 或 `models/clip/` | Qwen-Image 系共用 |
| VAE `qwen_image_vae` | `models/vae/` | |

来源：HuggingFace `Qwen/Qwen-Image-Edit-2511`（GGUF 社区量化：搜 `Qwen-Image-Edit-2511-GGUF`）。
文件名与 `comfy_infer.py` 内置默认（`_QWEN_EDIT_UNET/_CLIP/_VAE`）不一致也没关系——见 §4 用 env 或
模板覆写实际文件名。

**Z-Image-Turbo（可选，速度档）**：6B / Apache 2.0，`models/unet/` 放 `z_image_turbo_*.safetensors`
+ 同一 `qwen_2.5_vl` 文本编码器 + `z_image_vae`。8 步蒸馏，5090 上 ~5-10s。

⚠ 显存纪律（本仓铁律）：**只在 ComfyUI 进程里装模型；引擎实例进程严禁加载 GPU 模型**
（有过 OOM 事故）。`comfy_infer.py` 只做 HTTP 调用。

---

## 3. 在 ComfyUI 里跑通一次（关键步骤，别跳过）

1. ComfyUI 里搭 Qwen-Image-Edit-2511 **官方参考图工作流**（社区 2026 已有原生模板；
   节点关键：`UNETLoader`/`UnetLoaderGGUF` → `CLIPLoader(type=qwen_image)` → `VAELoader`
   → `LoadImage(参考脸)` → `TextEncodeQwenImageEditPlus(clip+vae+image+prompt)` → `KSampler`
   → `VAEDecode` → `SaveImage`）。
2. 提示词**只描述场景/动作/衣着，绝不描述长相**（长相由参考图锁定——这正是它比现役
   PuLID 姿态更自由的原因）。
3. 跑通、确认出图是「参考人 + 新场景」。
4. **导出 API 格式**：ComfyUI 菜单 → Save (API Format) → 得到 `qwen_edit_workflow.json`。

> 为什么必须导出模板：Qwen-Image-Edit 的 ComfyUI 节点名随版本演进，`comfy_infer.py` 内置图
> 是「最佳努力」，可能与你装的版本对不上。**你导出的模板 = 一定跑得通的真相**。

---

## 4. 接入 config（两种方式，任选）

### 方式 A（推荐）：模板覆写——最稳

把导出的 `qwen_edit_workflow.json` 里的可变字段替换成占位符：
`%PROMPT%` `%SEED%` `%WIDTH%` `%HEIGHT%` `%STEPS%` `%REF_IMAGE%` `%OUT_PREFIX%`
（`comfy_infer.py::_apply_workflow_template` 会逐字段替换；`%REF_IMAGE%` = 上传后的参考脸文件名，
锁脸时由 `--face-ref {base}` 自动上传得到）。放到引擎可达路径（如
`D:/boundless/engines/chengjie/config/workflows/qwen_edit.json`），然后在
**实例 overlay** `config.local.yaml` 配：

```yaml
companion:
  selfie:
    provider:
      manual_ui:
        engines:
          qwen_edit:
            label: "Qwen 参考图（一致性）"
            command_args: [python, D:/workspace/boundless/engines/chengjie/tools/comfy_infer.py,
              --engine, qwen_edit,
              --url, "http://192.168.0.176:8188",
              --workflow-template, "D:/workspace/boundless/engines/chengjie/config/workflows/qwen_edit.json",
              --min-free-gb, "18",
              --face-ref, "{base}",
              --prompt, "{prompt}", --out, "{out}"]
```

### 方式 B：用内置图 + env 校正文件名

不配 `command_args`（走派生默认：`基础 command_args + --engine qwen_edit --face-ref {base}`），
只在引擎进程环境设实际模型文件名：`COMFY_QWEN_UNET` / `COMFY_QWEN_CLIP` / `COMFY_QWEN_VAE`
（`z_image` 同理 `COMFY_ZIMAGE_*`）。内置节点图与你的 ComfyUI 版本一致时可直接跑；不一致 →
提交 400（`_post_prompt` 会透出 `node_errors`），回方式 A。

> `config.local.yaml` 改动**热重载生效**（~30s），无需重启实例。

---

## 5. 验收

1. **命令行直验**（在引擎主机）：
   ```
   python tools/comfy_infer.py --engine qwen_edit --url http://192.168.0.176:8188 \
     --workflow-template config/workflows/qwen_edit.json \
     --face-ref config/persona_albums/<pid>/face_ref.png \
     --prompt "in a sunny cafe, holding a latte, smiling" --out D:/tmp/qwen_test.png
   ```
   期望：退出码 0 + `qwen_test.png` 是「该人设的脸 + 咖啡馆场景」。
2. **坐席端**：工具箱「AI 生成图片」→ 选人设 → 引擎选 `Qwen 参考图（一致性）` → 生成 →
   出图会经 VLM 体检 → 预览 → 发送/存册。
3. **回退验证**：故意把 `--url` 指向没装模型的 ComfyUI → 前端应显示「生成失败：… 可换引擎或
   联系运维」，且切回 `flux_pulid` 正常出图（证明降级不影响现役链）。

---

## 6. 已知边界 / 注意

- **锁脸 vs 参考图一致性**：`flux_pulid`（PuLID 注入脸特征，姿态偏僵）vs `qwen_edit`（参考图直出，
  姿态自由但极端姿势/手部仍可能漂）。两者并存，坐席按需选。
- **热切换成本**：176 上 FLUX↔Qwen 每次切换有冷载。高频出图请上 173 独立 ComfyUI（§1）。
- **商用许可**：Qwen-Image-Edit-2511 与 Z-Image-Turbo 均 Apache 2.0，可商用（优于 FLUX.2 需授权）。
- **自动链不受影响**：`autosend` 自动出图链走的是 `provider.command_args`（无 `--engine` = flux），
  与工具箱手动引擎完全隔离，部署 Qwen 不动自动链。
