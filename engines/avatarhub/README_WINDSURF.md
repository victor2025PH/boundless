# AvatarHub 新电脑 Windsurf 部署指南

## 📦 1. 解压文件

将 `avatar_system_v1.zip` 解压到目标目录：
```
C:\模仿音色\
```

## 🖥️ 2. 安装基础环境

### 2.1 安装 Miniconda
下载地址：https://docs.conda.io/en/latest/miniconda.html

安装时勾选：
- ✅ Add Miniconda3 to my PATH environment variable

### 2.2 安装 Git
下载地址：https://git-scm.com/download/win

## 🐍 3. 创建 Conda 环境

### 环境1: facefusion (主控服务)
```powershell
cd C:\模仿音色
conda create -n facefusion python=3.10 -y
conda activate facefusion

# 核心依赖
pip install fastapi uvicorn[standard] websockets httpx aiofiles
pip install numpy pillow opencv-python-headless
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install deep-translator edge-tts
pip install psutil pywin32
pip install sqlalchemy
pip install mediapipe
```

### 环境2: rvc (实时变声)
```powershell
conda create -n rvc python=3.10 -y
conda activate rvc

pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install fairseq
pip install fastapi uvicorn
pip install numpy librosa soundfile sounddevice
pip install gradio==3.38.0
pip install numba
```

### 环境3: cosyvoice (情感TTS)
```powershell
# 先克隆 CosyVoice
cd C:\模仿音色
git clone https://github.com/FunAudioLLM/CosyVoice.git

conda create -n cosyvoice python=3.8 -y
conda activate cosyvoice

# 安装依赖
cd CosyVoice
pip install -r requirements.txt
pip install fastapi uvicorn

# 下载模型 (ModelScope)
modelscope download --model iic/CosyVoice3-0.5B --local_dir pretrained_models/Fun-CosyVoice3-0.5B
```

### 环境4: musethepeak (口型同步)
```powershell
conda create -n musethepeak python=3.10 -y
conda activate musethepeak

pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
pip install diffusers transformers accelerate
pip install fastapi uvicorn
pip install opencv-python numpy pillow
pip install onnxruntime-gpu
```

### 环境5: gptsovits (Singing)
```powershell
# 克隆 GPT-SoVITS (如果目录为空)
cd C:\模仿音色
git clone https://github.com/RVC-Boss/GPT-SoVITS.git

conda create -n gptsovits python=3.9 -y
conda activate gptsovits

pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install fastapi uvicorn
pip install numpy librosa soundfile
# 其他依赖参考 GPT-SoVITS/requirements.txt
```

## 📥 4. 下载模型文件

### 必需模型清单：

| 模型 | 目标路径 | 大小 | 下载方式 |
|------|---------|------|---------|
| CosyVoice3 | `CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B/` | ~3GB | modelscope |
| GFPGAN | `GFPGANv1.4.pth` | ~350MB | 原电脑复制或下载 |
| RVC 模型 | `RVC/assets/weights/*.pth` | ~500MB | 原电脑复制 |
| FaceFusion | `facefusion/.assets/models/` | ~500MB | 自动下载 |
| MuseTalk | `MuseTalk/models/` | ~1.5GB | 原电脑复制 |
| HairFastGAN | `HairFastGAN/pretrained_models/` | ~1GB | 原电脑复制 |
| CodeFormer | `CodeFormer/weights/` | ~100MB | 原电脑复制 |

### 快速复制模型（从原电脑）：
在原电脑上打包模型目录：
```powershell
# 原电脑执行
Compress-Archive -Path "C:\模仿音色\CosyVoice\pretrained_models" -DestinationPath "C:\cosyvoice_models.zip"
Compress-Archive -Path "C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\assets\weights" -DestinationPath "C:\rvc_models.zip"
```

在新电脑解压：
```powershell
# 新电脑执行
Expand-Archive -Path "C:\cosyvoice_models.zip" -DestinationPath "C:\模仿音色\CosyVoice\"
Expand-Archive -Path "C:\rvc_models.zip" -DestinationPath "C:\模仿音色\Retrieval-based-Voice-Conversion-WebUI\assets\"
```

## 🚀 5. 启动服务

### 方法1: 一键启动（推荐）
```powershell
cd C:\模仿音色
start_all_services.bat
```

### 方法2: 单独启动（调试使用）
```powershell
# 窗口1: AvatarHub 主控
conda activate facefusion
python avatar_hub.py

# 窗口2: FaceSwap
conda activate facefusion
python faceswap_api.py

# 窗口3: TTS
conda activate facefusion
python tts_api.py

# 窗口4: 情感TTS
conda activate cosyvoice
python emotion_tts_server.py

# 窗口5: RVC
conda activate rvc
cd Retrieval-based-Voice-Conversion-WebUI
python api_240604.py
```

## ✅ 6. 验证安装

### 健康检查
```powershell
curl http://localhost:9000/health
```

应返回 JSON 包含：
```json
{
  "status": "ok",
  "services": {
    "faceswap": true,
    "tts": true,
    "emotion_tts": true,
    "rvc": true
  }
}
```

### Web 界面
浏览器访问：
- 控制面板: http://localhost:9000/ui
- FaceSwap: http://localhost:8000/ui

## 🛠️ 7. Windsurf 开发配置

### 在新电脑的 Windsurf 中：

1. **打开项目文件夹**
   - 打开 Windsurf
   - File → Open Folder → `C:\模仿音色`

2. **配置 Python 解释器**
   - Ctrl+Shift+P → Python: Select Interpreter
   - 选择 `facefusion` 环境：
     `C:\Users\<用户名>\Miniconda3\envs\facefusion\python.exe`

3. **设置调试配置**
   创建 `.vscode/launch.json`：
   ```json
   {
     "version": "0.2.0",
     "configurations": [
       {
         "name": "AvatarHub",
         "type": "python",
         "request": "launch",
         "program": "avatar_hub.py",
         "console": "integratedTerminal",
         "justMyCode": false
       },
       {
         "name": "FaceSwap API",
         "type": "python",
         "request": "launch",
         "program": "faceswap_api.py",
         "console": "integratedTerminal"
       }
     ]
   }
   ```

4. **安装 Python 扩展**
   - 在 Windsurf 扩展商店安装：Python, Pylance

## 🐛 8. 常见问题

### 问题1: CUDA 版本不匹配
**解决**: 重新安装对应版本的 PyTorch
```powershell
pip uninstall torch torchvision torchaudio
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
```

### 问题2: 端口被占用
**解决**: 修改端口或结束占用进程
```powershell
# 查找占用 9000 端口的进程
netstat -ano | findstr :9000
# 结束进程
taskkill /PID <进程ID> /F
```

### 问题3: 模型文件缺失
**解决**: 从原电脑复制或使用 download_rvc_voices.py 重新下载
```powershell
conda activate facefusion
python download_rvc_voices.py
```

### 问题4: 依赖缺失
**解决**: 根据错误信息安装缺失包
```powershell
conda activate <环境名>
pip install <缺失包名>
```

## 📞 9. 支持文档

项目文档：
- `主控机调用API文档.md` - API 接口文档
- `手机音视频输入指南.md` - 手机接入指南
- `实时直播接入指南.md` - OBS 直播配置
- `ROUTING_GUIDE.md` - 服务路由说明

测试脚本：
```powershell
# 运行测试
conda activate facefusion
python test_phase1.py  # 基础测试
python test_phase2.py  # 高级功能测试
python run_all_tests.py  # 全部测试
```

## 🎯 10. 快速开始检查清单

- [ ] 解压 avatar_system_v1.zip 到 C:\模仿音色
- [ ] 安装 Miniconda
- [ ] 创建5个 conda 环境
- [ ] 复制/下载模型文件
- [ ] 修改 env_config.bat 中的路径（如需要）
- [ ] 运行 start_all_services.bat
- [ ] 访问 http://localhost:9000/health 验证
- [ ] 在 Windsurf 中打开项目
- [ ] 配置 Python 解释器
- [ ] 开始开发！

---
**打包时间**: 2025-05-30
**版本**: v1.0
