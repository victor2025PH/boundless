# 把 frames16/ 的 360 帧合成 12s 无缝循环 4K MP4（H.264, yuv420p, 高码率）
Set-Location $PSScriptRoot
ffmpeg -y -framerate 30 -i "frames16\f_%04d.jpg" `
  -c:v libx264 -preset slow -crf 15 -pix_fmt yuv420p `
  -movflags +faststart -an `
  "boundless-brand-16x9-loop-4k.mp4"
