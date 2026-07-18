# tools/persona_bus · 人设总线只读导出适配器（P5）

把各引擎的人设/角色库导出为集团人设注册表的归一化 JSON（四槽位 face/voice/prompt/knowledge）。
**绝对只读**：不写入/修改 engines/ 下任何文件（SQLite 一律 `mode=ro` 打开）。仅 Python 标准库。
格式与授权/清除协议的完整契约见 `platform/identity/PERSONA_BUS.md`。

另含 **grant 缓存拉取**（v1.2 运行时软门控）：`fetch_grants.py` 从集团
`GET /api/sync/personas/grants` 拉清单写本地 JSON，供 `platform/identity/grant_gate.py` 离线检查。

> ⚠️ 铁律：资产本体（脸模/声纹文件/权重）与任何生物特征数据**绝不进导出文件**；
> `fingerprint` 只能是对资产字节的 sha256 摘要；`raw` 走白名单，不含文件内容。
> 导出文件含显示名/指纹，按内部经营数据处理，勿入 git。

## 用法

```powershell
# avatarhub（默认读 <repo>/engines/avatarhub 下的 avatar_profiles.db、alltalk_tts/voices/、
# avatar_kb.db、声音包/、active_profile.txt、profile_usage.json）
python tools/persona_bus/export_avatarhub_personas.py --out avatarhub_personas.json

# 数据源覆盖（--input 指引擎根目录，便于读备份/异机拷贝）
python tools/persona_bus/export_avatarhub_personas.py --input D:\backup\avatarhub

# 管道联调：不读真实数据，生成 3 条演示数据（四槽齐全 / 部分槽位 / 静置加密行）
python tools/persona_bus/export_avatarhub_personas.py --demo --out demo_personas.json

# 自检（结构 + 值域 + 无本体泄漏启发式；导入前必须过）
python tools/persona_bus/validate_personas.py avatarhub_personas.json
```

## 行为约定

- 数据源缺失/不可解析：stderr 警告 + 输出空 `personas`，**退出码仍为 0**（空导出是合法结果）。
- `--out` 落在被读引擎目录内会被拒绝（退出码 2）——只读纪律护栏。
- 静置加密行（`AVATARHUB_ENCRYPT_PROFILES` 开启后 data 带 `enc:fernet:v1:` 前缀）：
  本脚本无密钥也绝不解密，该行按「存在但槽位未知」导出（四槽 `present=false`、
  `tags` 含 `encrypted`、`raw.encrypted=true`）。
- 槽位 `present` 按资产**实际存在**判断：`voice_name` 引用的 wav 文件丢失 → 回退行内
  `voice_b64`，两者皆无 → `present=false`。
- 大文件指纹流式计算（1 MiB 块）；内嵌 base64 资产的指纹＝**解码后字节**的 sha256，
  与同字节落盘文件的指纹一致（跨存储形态可对账）。
- 导出器出厂自查：写文件前扫一遍疑似 base64 长串，命中即拒绝写出（退出码 3）。
  正常情况下 raw 白名单保证永不命中，这是最后一道闸。

## avatarhub 槽位映射（侦察结论，与 PERSONA_BUS.md §6 一致）

| 槽位 | 数据源 | fingerprint | ref |
|---|---|---|---|
| face | 角色行 `face_b64`（主照） | sha256(解码字节) | `avatar_profiles.db#<名>#face_b64` |
| voice | `voice_name` → `alltalk_tts/voices/<名>.wav`；否则行内 `voice_b64` | sha256(文件流式 / 解码字节) | 相对路径 或 `…#voice_b64` |
| prompt | 角色行 `system_prompt` | sha256(utf-8 文本) | `avatar_profiles.db#<名>#system_prompt` |
| knowledge | `avatar_kb.db` 中 `meta.profile=<名>` 的 kb_docs；否则 `声音包/<名>.txt` | sha256(按 id 排序逐条拼接 / 文件) | `avatar_kb.db#kb_docs?profile=<名>` 或相对路径 |

多资产（照片库/多段参考/情绪参考/话术）只进 `raw` 计数；`source_key`＝profiles 表主键（角色名）。

## 与注册表导入的衔接（website 侧）

导出后交 `website/scripts/ledger-import-personas.mjs`（website 侧同事并行开发）导入集团注册表：

```bash
# 厂商机周期执行：导出 → 校验 → 导入（校验不过不导入）
python tools/persona_bus/export_avatarhub_personas.py --out avatarhub_personas.json
python tools/persona_bus/validate_personas.py avatarhub_personas.json
node website/scripts/ledger-import-personas.mjs avatarhub_personas.json
```

- 幂等键 **`(source_system, source_key)`**：重复导入 upsert 不重登；首见签发 `prs_*`
  内部主键（`platform/identity/ID_SPEC.md` §4.2 遗留键映射三元组）。
- 已 `purge_pending` / `purged` 的键再次出现 → 导入侧**不复活**，标异常人工核查
  （全域清除协议与引擎义务见 PERSONA_BUS.md §5）。
- 同键指纹变化＝人设换了脸/声，正常更新。

## 授权缓存拉取（运行时软门控 v1.2）

```powershell
# 从集团拉本引擎 active persona 的 grants，写入本地缓存（Bearer = EVENT_INGEST_KEY）
python tools/persona_bus/fetch_grants.py --base https://bd2026.cc --key $env:EVENT_INGEST_KEY `
  --system avatarhub --out engines/avatarhub/data/persona_grants_cache.json

# 门控自检
python platform/identity/grant_gate.py --selftest
```

- 失败退出非 0，stderr 含可重试说明；**旧缓存可继续离线用**（默认 warn 放行，不挡业务）。
- 建议挂在 `deploy/cron` export 成功之后；接线指南见各引擎 `grant_check.py` 文件头与 PERSONA_BUS.md §4.1。
- 默认不强制：仅 `PERSONA_GRANT_ENFORCE=1` 时无 grant 才拒绝。

## 后续引擎接入

一引擎一个导出器（`export_<engine>_personas.py`），输出同一 §3 格式即可复用同一校验器
与导入脚本。chengjie（AI 人设/术语库：`config/profiles_runtime.yaml` + `config/voice_refs/`）、
huoke（养号人设：`fb_target_personas`）的 source_key 与槽位映射建议见 PERSONA_BUS.md §6。
