# FireRedTTS3 部署（104 声音机 · 智聊多语克隆节点）

> 决策记录（2026-08-31，老板授权"部署机器你来安排"）：
> **落点 = 104 声音机（4070 12G），分时方案**。
>
> - 全集群显存实测（2026-08-31 10:4x）：117 满（铁律）/ 173 vLLM 预分配 30.2/32.6G /
>   176 出图+视觉+hub 引擎 26.2/32.6G / 104 IndexTTS-2 常驻后余 4.4G / 140 台账"显存最紧" /
>   198 识图+ASR 单点——**没有一台可与现役服务无扰共存**（FireRed bf16 运行时 ~8-10G）。
> - 104 的 IndexTTS-2 自 2026-08-30 hub 主链切换后是**零流量回滚待命位**；本机职能
>   （台账 role_short=智聊语音唯一）不变，服务矩阵变为【IndexTTS-2（冷备可起）+
>   FireRedTTS3（常驻）】**二选一驻显存**。回滚语义从"现成在跑"变为"停 B 起 A
>   （IndexTTS104 开机任务仍在，~2min 冷载）"——回滚位保留，只慢一步。
> - **与现有栈零功能冲突**：独立 venv（uv 管理的 CPython 3.11，不碰系统 Python）、
>   独立端口 :7869、fish `/v1/tts/clone` 契约 wrapper → chengjie 侧零改码
>   （`voice_lang_route.clone_langs` 的 `clone_base_url` 指过来即可）。
> - **IndexTTS-2.5 刻意不升**：ja 已由 117 CosyVoice 路线解决（08-31 开闸）；2.5 增量
>   （es/ar）被 FireRed 24 语完全覆盖；hub 主链 2.5 升级属幻声集群属地（档体系+10 档
>   重验），作为提案移交，不在本线动。

## 语种定位（为什么是 FireRed）

FireRedTTS3-Base（2026-08 发布，Apache 2.0）：24 语零样本克隆，含我们全部缺口
**th/vi/id/es/ar/hi/tr/pt/fr/de/it/ru/el/uk** + 粤语与 21 中文方言；MiniMax-MLS-Test
声纹相似度均分 84.8（开源最高档），th CER 1.87 / vi 0.86 / id 1.42。
Qwen3-1.7B 底座 + RedAE；bf16 权重 4.7+2.5GB、运行时 ~8-10G；需 Ampere+、
flash_attention_2、torch 2.8 cu128；非中英文本归一化（TN）走 LLM 端点
（接 173:8001 vLLM chatx，OpenAI 兼容）。

## 部署步骤

1. **环境安装 + 权重下载**（增置动作，不碰现役服务；~40GB 磁盘、外网走 hf-mirror）：

   ```powershell
   # 在 117 上推送并启动（或直接在 104 上跑 deploy_104.ps1）
   scp -F deploy/ssh_config.boundless deploy/fireredtts3/deploy_104.ps1 lianbei:C:/firered/
   ssh -F deploy/ssh_config.boundless lianbei powershell -ExecutionPolicy Bypass -File C:/firered/deploy_104.ps1
   ```

   脚本幂等可重入（断点续传靠 hf CLI）；日志 `C:\firered\deploy.log`。

2. **服务定稿**（下载完成后）：`firered_server.py` 的合成调用按官方 repo 现场核对
   （标注 `# TODO-现场核对` 处），冒烟一句 zh —— wrapper 与 chengjie 的契约测试在
   `engines/chengjie/tests/test_lang_voice_route.py`（clone_langs 家族）。

3. **切换验收窗口**（生产变更，走值守窗口）：
   - 停 104 IndexTTS 服务（开机任务保留 Disabled 态即可随时回滚）；
   - 起 FireRed wrapper :7869；
   - 逐语种验收（首样本纪律）：
     `python tools/verify_clone_lang.py --profile 林小雨-智聊 --clone-url http://192.168.0.104:7869 --ref <参考音> --cases th:firered,vi:firered,id:firered,es:firered --n 3`
   - 机器 PASS → 人耳抽检（ear_pack 模式）→ 老板拍板 → `clone_langs` overlay 加语种。

4. **观测**：`voice_synth_stats.blocked_by_lang`（客户在要哪些念不了的语种）决定
   开语种优先级；EmotionTTSWatchdog 模式可复制一份盯 :7869（后续）。

## 回滚

- 停 FireRed 服务 → 启用 IndexTTS104 开机任务并手动启动 → `clone_langs` 里指向
  104:7869 的语种条目删除（热更新 ~30s）。chengjie 侧语言闸自动回"拦克隆→edge 兜底"。
