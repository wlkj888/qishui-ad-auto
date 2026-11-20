#!/bin/bash
# macOS 启动脚本：优先打开带图标的 .app，找不到时回退到直接运行源码
cd "$(dirname "$0")" || exit 1

APP="汽水音乐去广告.app"
if [ -d "$APP" ]; then
  open "$APP"
  exit 0
fi

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x ".venv/bin/python" ]; then
  echo "未找到虚拟环境 .venv，正在重建..."
  /Users/xiaowu/.local/bin/python3.12 -m venv .venv || { echo "创建虚拟环境失败"; read -n 1 -s -r -p "按任意键退出"; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install opencv-python numpy Pillow rapidocr-onnxruntime || { echo "依赖安装失败"; read -n 1 -s -r -p "按任意键退出"; exit 1; }
fi
exec .venv/bin/python qishui_ad_gui.py
