#!/bin/bash
# -*- coding: utf-8 -*-
# 本机 macOS Developer ID 签名、DMG 打包与 Apple Notarization 公证脚本
# 用法：
#   ./scripts/sign_and_notarize_macos.sh <输入.app或.dmg路径> <输出.dmg路径> [notarytool keychain profile名称]
#
# 示例：
#   ./scripts/sign_and_notarize_macos.sh \
#     /tmp/PDF-Shrink-macOS-AppleSilicon-arm64.dmg \
#     dist/PDF-Shrink-macOS-AppleSilicon-arm64.dmg \
#     pdf-shrink-notary

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "用法: $0 <输入.app或.dmg> <输出.dmg> [notarytool-profile]" >&2
  exit 1
fi

INPUT_PATH="$1"
OUTPUT_DMG="$2"
NOTARY_PROFILE="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="${SCRIPT_DIR}/entitlements.plist"

SIGN_IDENTITY="${SIGN_IDENTITY:-$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -n 1 | sed -E 's/.*"([^"]+)".*/\1/' || true)}"
if [[ -z "$SIGN_IDENTITY" ]]; then
  echo "错误：本机钥匙串未找到 'Developer ID Application' 证书。" >&2
  echo "请在 Xcode -> Settings -> Accounts -> Manage Certificates 中点击 '+' 创建 'Developer ID Application' 证书后重试。" >&2
  exit 1
fi

echo "使用签名证书: $SIGN_IDENTITY"

WORK_DIR="$(mktemp -d /tmp/pdf_shrink_sign.XXXXXX)"
IN_MOUNT=""
VERIFY_MOUNT=""

cleanup() {
  [[ -n "$IN_MOUNT" ]] && hdiutil detach "$IN_MOUNT" -force 2>/dev/null || true
  [[ -n "$VERIFY_MOUNT" ]] && hdiutil detach "$VERIFY_MOUNT" -force 2>/dev/null || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

STAGE="$WORK_DIR/dmg_stage"
mkdir -p "$STAGE"

if [[ "$INPUT_PATH" == *.dmg ]]; then
  IN_MOUNT="$WORK_DIR/in_mount"
  mkdir -p "$IN_MOUNT"
  hdiutil attach -nobrowse -readonly -mountpoint "$IN_MOUNT" "$INPUT_PATH" >/dev/null
  SRC_APP="$(find "$IN_MOUNT" -maxdepth 1 -name "*.app" -print -quit)"
  if [[ -z "$SRC_APP" ]]; then
    echo "错误：输入 DMG 中未找到 .app 文件：$INPUT_PATH" >&2
    exit 1
  fi
  ditto "$SRC_APP" "$STAGE/PDF Shrink.app"
  hdiutil detach "$IN_MOUNT" >/dev/null
  IN_MOUNT=""
elif [[ -d "$INPUT_PATH" && "$INPUT_PATH" == *.app ]]; then
  ditto "$INPUT_PATH" "$STAGE/PDF Shrink.app"
else
  echo "错误：不支持的输入路径：$INPUT_PATH（请传入 .app 或 .dmg）" >&2
  exit 1
fi

APP="$STAGE/PDF Shrink.app"
xattr -cr "$APP"
chmod +x "$APP/Contents/MacOS/PDF Shrink"

sign_item() {
  codesign --force --timestamp --options runtime --entitlements "$ENTITLEMENTS" --sign "$SIGN_IDENTITY" "$1"
}

echo "=== 1. 由内而外正式签名 (Hardened Runtime + Timestamp) ==="
while IFS= read -r -d '' bin; do
  sign_item "$bin"
done < <(find "$APP/Contents/Frameworks" -type f \( -name "*.so" -o -name "*.dylib" \) -print0)

if [[ -d "$APP/Contents/Frameworks/Python.framework" ]]; then
  for ver_dir in "$APP/Contents/Frameworks/Python.framework/Versions"/*; do
    [[ -L "$ver_dir" || ! -d "$ver_dir" ]] && continue
    if [[ -f "$ver_dir/Python" ]]; then
      sign_item "$ver_dir/Python"
    fi
  done
  sign_item "$APP/Contents/Frameworks/Python.framework"
fi

sign_item "$APP/Contents/MacOS/PDF Shrink"
sign_item "$APP"

codesign --verify --deep --strict --verbose=2 "$APP"

echo "=== 2. 打包 DMG ==="
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/使用说明.txt" <<'EOF'
PDF Shrink（广西瀛桂律师事务所专用版）— macOS 安装说明
Copyright © 2026 唐学智 版权所有
================================================================

【安装方式】
将「PDF Shrink.app」拖入旁边的「Applications（应用程序）」文件夹即可使用。

【重要提示：如需转发给同事或存入百度网盘同步空间】
请直接保存或转发本 .dmg 安装镜像单文件，切勿把拖出来后的「PDF Shrink.app」放进
百度网盘同步空间、微信文件夹或 Windows 电脑中转（网盘/Windows 会丢失 macOS 应用
内部的符号链接与可执行权限，导致应用损坏无法打开）。
EOF

mkdir -p "$(dirname "$OUTPUT_DMG")"
rm -f "$OUTPUT_DMG"
hdiutil create \
  -volname "PDF Shrink" \
  -srcfolder "$STAGE" \
  -ov \
  -fs HFS+ \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$OUTPUT_DMG"

echo "=== 3. 签名 DMG ==="
codesign --force --timestamp --sign "$SIGN_IDENTITY" "$OUTPUT_DMG"
codesign --verify --verbose=2 "$OUTPUT_DMG"

if [[ -n "$NOTARY_PROFILE" ]]; then
  echo "=== 4. 提交 Apple 公证 (Notarization) ==="
  xcrun notarytool submit "$OUTPUT_DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  echo "=== 5. 装订公证票据 (Stapler) ==="
  xcrun stapler staple "$OUTPUT_DMG"
  xcrun stapler validate "$OUTPUT_DMG"
else
  echo "提示：未指定 notarytool keychain profile，已跳过 Apple 公证步骤。"
  echo "如需公证，请先运行一次："
  echo "  xcrun notarytool store-credentials pdf-shrink-notary --apple-id 125061761@qq.com --team-id WLU4LGTJW7"
  echo "再将 'pdf-shrink-notary' 作为第 3 个参数传入本脚本。"
fi

echo "=== 6. 挂载验收 ==="
VERIFY_MOUNT="$WORK_DIR/verify_mount"
mkdir -p "$VERIFY_MOUNT"
hdiutil attach -nobrowse -readonly -mountpoint "$VERIFY_MOUNT" "$OUTPUT_DMG" >/dev/null
codesign --verify --deep --strict --verbose=2 "$VERIFY_MOUNT/PDF Shrink.app"
if [[ -n "$NOTARY_PROFILE" ]]; then
  spctl --assess --type execute --verbose=4 "$VERIFY_MOUNT/PDF Shrink.app"
fi
"$VERIFY_MOUNT/PDF Shrink.app/Contents/MacOS/PDF Shrink" --version
hdiutil detach "$VERIFY_MOUNT" >/dev/null
VERIFY_MOUNT=""

echo "✅ 完成：$OUTPUT_DMG"
