#!/bin/bash
cd "$(dirname "$0")"
echo "正在启动 PDF Shrink…"
if ! python3 -c "import pymupdf, PIL" 2>/dev/null; then
  echo "首次使用，正在安装依赖…"
  python3 -m pip install -r requirements.txt
fi
python3 pdf_shrink.py --gui
