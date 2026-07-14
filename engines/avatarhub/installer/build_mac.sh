#!/usr/bin/env bash
# ============================================================
#  AvatarHub macOS 打包脚本：PyInstaller → .app → .dmg
#  必须在 macOS 上运行（PyInstaller 不能跨平台交叉编译）。
#  产物：dist/AvatarHub.app 与 dist/AvatarHub-<ver>.dmg
#
#  依赖（首次）：
#    python3 -m venv .venv_launcher_mac
#    source .venv_launcher_mac/bin/activate
#    pip install pyside6-essentials pyinstaller pillow cryptography zstandard
#    # DMG 打包（二选一）：brew install create-dmg  （无则回退 hdiutil）
#
#  用法：
#    chmod +x installer/build_mac.sh
#    installer/build_mac.sh [版本号]      # 默认 1.0.1
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VER="${1:-1.0.1}"

# 选择 python：优先 mac 专用 venv，回退当前 python3
if [[ -x ".venv_launcher_mac/bin/python" ]]; then
  PY=".venv_launcher_mac/bin/python"
else
  PY="$(command -v python3)"
  echo "[warn] 未找到 .venv_launcher_mac，使用系统 $PY（请确保已装 pyinstaller/pyside6）。"
fi

echo "[1/4] 清理旧产物"
rm -rf build "dist/AvatarHub.app" "dist/AvatarHub-${VER}.dmg" 2>/dev/null || true

echo "[2/4] PyInstaller 构建 .app"
"$PY" -m PyInstaller --noconfirm --clean AvatarHub-mac.spec

if [[ ! -d "dist/AvatarHub.app" ]]; then
  echo "[error] 未生成 dist/AvatarHub.app" >&2
  exit 3
fi

# 可选：本地临时签名（ad-hoc），避免"已损坏"提示；正式分发请用开发者证书 + notarytool 公证。
if command -v codesign >/dev/null 2>&1; then
  echo "[2.5] ad-hoc 签名（正式分发请替换为 Developer ID + 公证）"
  codesign --force --deep --sign - "dist/AvatarHub.app" || echo "[warn] ad-hoc 签名失败，忽略。"
fi

echo "[3/4] 打包 .dmg"
DMG="dist/AvatarHub-${VER}.dmg"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg \
    --volname "AvatarHub ${VER}" \
    --window-size 520 320 \
    --icon "AvatarHub.app" 130 150 \
    --app-drop-link 390 150 \
    "$DMG" "dist/AvatarHub.app" || {
      echo "[warn] create-dmg 失败，回退 hdiutil"; USE_HDIUTIL=1; }
fi
if [[ ! -f "$DMG" ]]; then
  # 回退：hdiutil 直接把 .app 装进只读 dmg
  STAGE="$(mktemp -d)"
  cp -R "dist/AvatarHub.app" "$STAGE/"
  ln -s /Applications "$STAGE/Applications" || true
  hdiutil create -volname "AvatarHub ${VER}" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
  rm -rf "$STAGE"
fi

echo "[4/4] 完成"
shasum -a 256 "$DMG" | tee "dist/AvatarHub-${VER}.dmg.sha256"
echo "[done] 产物：$DMG"
echo "       下一步：python3 gen_download_manifest.py --base-url <你的下载站URL>"
