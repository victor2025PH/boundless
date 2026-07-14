#!/usr/bin/env bash
# AvatarHub GPU 服务容器入口：可编辑包补装(幂等,~1s) → exec 服务脚本。
# 用法(compose 的 command)： /entrypoint.sh fish_speech_server.py
set -e

if [ -s /tmp/req.editable.txt ]; then
    # 源码树在运行时挂载卷 /app 内，构建期装不了；--no-deps 因依赖已进镜像。
    # 失败不阻断启动(如卷里缺该源码树)——服务自身会在 import 时给出明确报错。
    pip install --no-deps -q -r /tmp/req.editable.txt 2>/dev/null || \
        echo "[entrypoint] 警告: 可编辑包补装失败(检查 /app 是否挂载了完整项目根)"
fi

exec python "$@"
