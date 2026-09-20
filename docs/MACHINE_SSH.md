# 六机 SSH 台账（中文产品命名）

> 更新：2026-07-23  
> 单一源：`deploy/machines.json`  
> 生成 SSH 配置：`tools/render_ssh_config.ps1` → `deploy/ssh_config.boundless`  
> 网状安装：`tools/setup_machine_mesh.ps1`  
> 探活：`tools/cluster_ping.ps1`

## 分配

| 中文名 | SSH 别名 | IP | 账号 | 角色 | 主产品 | 工作仓 | 开发入口 |
|---|---|---|---|---|---|---|---|
| **幻声** | `huansheng` | 192.168.0.176 | user | 开发 | 幻声/幻颜/通传（avatarhub hub） | `D:\boundless` | `D:\开发\幻声` |
| **幻影** | `huanying` | 192.168.0.173 | admin | 算力 | 幻影（GLM-4-Voice 实时语音，5090） | `C:\boundless` | `C:\开发\幻影` |
| **通译** | `tongyi` | 192.168.0.117 | Administrator | 开发 | 通译 + 智聊（chengjie） | `D:\workspace\boundless`（联接 `D:\boundless`） | `D:\开发\通译` · `D:\开发\智聊` |
| **智拓** | `zhituo` | 192.168.0.198 | Administrator | 开发 | 智拓（huoke） | `D:\boundless` | `D:\开发\智拓` |
| **幻颜节点** | `huanyan-node` | 192.168.0.104 | Administrator | 算力 | 换脸服务 | `C:\boundless`（无 D: 盘） | `C:\开发\幻颜节点` |
| **通传节点** | `tongchuan-node` | 192.168.0.140 | Administrator | 算力 | STT 服务 | `D:\boundless` | `D:\开发\通传节点` |

官网 VPS：`vps-bd2026` → `ubuntu@165.154.233.121`（`hualing_deploy`）

## 壁纸

路径：`brand-assets/05_backgrounds/machines/*-wallpaper.png`  
生成：`python tools/make_machine_wallpapers.py`  
已写入各机 `C:\Users\Public\Pictures\boundless-wallpaper-<id>.png` 并设为当前用户壁纸。

## 算力互调

- Hub：`http://192.168.0.176:9000`（幻声机）
- **编码模式（2026-08-14 起）**：幻影 `.173:8010/v1`（vLLM，model=`coder`＝Qwen3.6-27B
  无审查 AWQ，~80 tok/s / 64k ctx；默认思考档写码，请求带
  `chat_template_kwargs:{"enable_thinking":false}` 为 0.6s 直答档。契约详情：
  `engines/chengjie/deploy/compute/README.md` §编码模式）
- 节点：幻颜 `.104:8000` · 通传 STT `.140:7854` · 通译 TTS `.117:7852/7858` · 幻影 GLM-4-Voice `.173:8888/10011`
- LAN LLM：幻影 `.173:8001/v1`（vLLM ``chatx``＝Qwen3-27B AWQ，智聊主链兜底 + 口语化；08-28 起不再用 ollama 14B）· 通传 `.140:11434`（bge-m3 嵌入 / qwen3-vl 视觉）· 幻声 `.176:11434`（qwen3-vl / hy-mt2 / 嵌入备）
- 拓扑：`engines/avatarhub/cluster_map.json`
- 调用方式：业务经 Hub HTTP + service token（非 CUDA 直连）

## 自检

```powershell
powershell -File tools\cluster_ping.ps1
# 任意机互访
ssh huansheng hostname
ssh huanying hostname
ssh tongyi hostname
ssh zhituo hostname
ssh huanyan-node hostname
ssh tongchuan-node hostname
```
