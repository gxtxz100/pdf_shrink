# PDF Shrink

轻量 PDF 压缩工具：兼顾文件体积与清晰度，支持图形界面和命令行。

## 安装

```bash
cd pdf_shrink
pip install -r requirements.txt
```

依赖很少：仅 `pymupdf` 与 `Pillow`（本机已装可直接用）。

## 使用

### 图形界面（推荐）

```bash
python3 pdf_shrink.py
# 或
python3 pdf_shrink.py --gui
```

也可双击 `启动.command`（macOS）。

### 命令行

```bash
# 单文件（默认「平衡」预设）
python3 pdf_shrink.py 报告.pdf

# 指定输出
python3 pdf_shrink.py 报告.pdf -o 报告_小.pdf

# 选择预设
python3 pdf_shrink.py 扫描件.pdf -p high -m rebuild

# 批量压缩目录
python3 pdf_shrink.py ./pdfs -o ./out -r

# 自定义质量与 DPI
python3 pdf_shrink.py a.pdf -q 65 --dpi 140

# 灰度进一步缩小
python3 pdf_shrink.py a.pdf -g
```

## 功能一览

| 功能 | 说明 |
|------|------|
| 拖放添加 | 图形界面直接拖入单个/多个 PDF 文件或文件夹（自动递归扫描） |
| 默认源目录输出 | 未指定输出目录时，默认保存到各源文件所在目录 |
| 五档预设 | 极致 / 高压缩 / 平衡 / 高质量 / 印刷级 |
| 智能压缩 | 压缩内嵌图片，尽量保留可选中文字 |
| 整页重绘 | 适合扫描件、图片型 PDF |
| 批量处理 | 多文件 / 文件夹（可递归） |
| 灰度模式 | 进一步减小体积 |
| 清除元数据 | 去掉标题、作者等信息 |
| 页码范围 | `--from-page` / `--to-page` |
| 加密 PDF | `--password` |
| 体积对比 | 显示压缩前后大小与节省比例 |
| 安全回退 | 智能模式下若压缩后更大，保留原内容 |

## 预设说明

| 预设 | 质量 | DPI | 适用场景 |
|------|------|-----|----------|
| extreme | 35 | 96 | 仅需可读的存档 |
| high | 55 | 120 | 日常微信/邮件分享 |
| balanced | 72 | 150 | 推荐，清晰度与体积兼顾 |
| quality | 85 | 200 | 打印预览 |
| print | 92 | 300 | 尽量保留细节 |

## 模式怎么选？

- **文字版 / 办公文档** → `smart`（智能压缩）
- **扫描件 / 拍照 PDF** → `rebuild`（整页重绘）

## 许可

MIT
