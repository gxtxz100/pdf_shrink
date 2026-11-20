#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PyInstaller Windows 可执行文件版本与版权信息资源文件。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pdf_shrink


def parse_version_tuple(version_str: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version_str.split(".") if p.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def main() -> None:
    out_dir = ROOT / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "win_version_info.txt"

    v_tuple = parse_version_tuple(pdf_shrink.APP_VERSION)
    v_str = ".".join(str(x) for x in v_tuple)

    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={v_tuple},
    prodvers={v_tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [
            StringStruct('CompanyName', '{pdf_shrink.APP_AUTHOR} / {pdf_shrink.APP_EDITION}'),
            StringStruct('FileDescription', '{pdf_shrink.APP_NAME} — 轻量 PDF 压缩工具（{pdf_shrink.APP_EDITION}）'),
            StringStruct('FileVersion', '{v_str}'),
            StringStruct('InternalName', 'PDF-Shrink'),
            StringStruct('LegalCopyright', '{pdf_shrink.APP_COPYRIGHT}'),
            StringStruct('OriginalFilename', 'PDF-Shrink-Windows-x64.exe'),
            StringStruct('ProductName', '{pdf_shrink.APP_NAME}'),
            StringStruct('ProductVersion', '{pdf_shrink.APP_VERSION}'),
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [0x0804, 1200])])
  ]
)
"""
    out_file.write_text(content, encoding="utf-8")
    print(f"Windows version info generated: {out_file}")


if __name__ == "__main__":
    main()
