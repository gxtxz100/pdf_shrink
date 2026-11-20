#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF Shrink — 轻量 PDF 压缩工具
支持命令行与图形界面，支持拖入文件/文件夹，在体积与清晰度之间可调。

Copyright © 2026 唐学智 版权所有
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import io
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    import pymupdf
except ImportError:
    print("缺少依赖 pymupdf，请先执行: pip install -r requirements.txt")
    sys.exit(1)

try:
    from PIL import Image
except ImportError:
    print("缺少依赖 Pillow，请先执行: pip install -r requirements.txt")
    sys.exit(1)


APP_NAME = "PDF Shrink"
APP_VERSION = "1.1.1"
APP_EDITION = "广西瀛桂律师事务所专用版"
APP_AUTHOR = "唐学智"
APP_COPYRIGHT = "Copyright © 2026 唐学智 版权所有"

# 预设：名称 -> (jpeg质量, 目标DPI, 说明)
PRESETS = {
    "extreme": (35, 96, "极致压缩，适合仅需可读的存档"),
    "high": (55, 120, "高压缩，日常分享够用"),
    "balanced": (72, 150, "平衡（推荐），清晰度与体积兼顾"),
    "quality": (85, 200, "高质量，适合打印预览"),
    "print": (92, 300, "印刷级，尽量保留细节"),
}

PRESET_LABELS = {
    "extreme": "极致压缩",
    "high": "高压缩",
    "balanced": "平衡（推荐）",
    "quality": "高质量",
    "print": "印刷级",
}

OUT_DIR_PLACEHOLDER = "留空则默认输出到源文件所在目录"


@dataclass
class CompressOptions:
    quality: int = 72
    dpi: int = 150
    mode: str = "smart"  # smart | rebuild
    grayscale: bool = False
    remove_metadata: bool = True
    page_from: Optional[int] = None  # 1-based inclusive
    page_to: Optional[int] = None
    password: str = ""


@dataclass
class CompressResult:
    input_path: Path
    output_path: Path
    pages: int
    original_size: int
    compressed_size: int
    skipped: bool = False
    message: str = ""

    @property
    def ratio(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return 1.0 - (self.compressed_size / self.original_size)

    @property
    def saved(self) -> int:
        return max(0, self.original_size - self.compressed_size)


def human_size(num: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    size = float(num)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num} B"


def apply_preset(name: str) -> tuple[int, int]:
    if name not in PRESETS:
        raise ValueError(f"未知预设: {name}，可选: {', '.join(PRESETS)}")
    quality, dpi, _ = PRESETS[name]
    return quality, dpi


def _open_doc(path: Path, password: str = "") -> pymupdf.Document:
    doc = pymupdf.open(path)
    if doc.is_encrypted:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ValueError(f"PDF 已加密，请提供正确密码: {path.name}")
    return doc


def _page_range(doc: pymupdf.Document, options: CompressOptions) -> range:
    total = doc.page_count
    start = (options.page_from or 1) - 1
    end = options.page_to or total
    start = max(0, min(start, total - 1))
    end = max(start + 1, min(end, total))
    return range(start, end)


def _pil_to_jpeg_bytes(img: Image.Image, quality: int, grayscale: bool) -> bytes:
    if grayscale:
        img = img.convert("L")
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _downsample_size(
    width: int,
    height: int,
    dpi: int,
    page_width_inch: float,
    page_height_inch: float,
) -> tuple[int, int]:
    """按目标 DPI 估算图像最大像素尺寸。"""
    target_w = max(1, int(page_width_inch * dpi))
    target_h = max(1, int(page_height_inch * dpi))
    if width <= target_w and height <= target_h:
        return width, height
    scale = min(target_w / width, target_h / height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def _compress_images_on_page(page: pymupdf.Page, options: CompressOptions) -> int:
    """就地压缩页面中的图像，返回替换次数。"""
    replaced = 0
    seen: set[int] = set()
    rect = page.rect
    page_w_in = rect.width / 72.0
    page_h_in = rect.height / 72.0

    for info in page.get_images(full=True):
        xref = info[0]
        if xref in seen:
            continue
        seen.add(xref)

        try:
            pix = pymupdf.Pixmap(page.parent, xref)
        except Exception:
            continue

        try:
            if pix.n - pix.alpha > 3:  # CMYK 等
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)  # 去 alpha

            mode = "RGB" if pix.n >= 3 else "L"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            new_w, new_h = _downsample_size(
                pix.width, pix.height, options.dpi, page_w_in, page_h_in
            )
            if (new_w, new_h) != (pix.width, pix.height):
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            jpeg_bytes = _pil_to_jpeg_bytes(img, options.quality, options.grayscale)
            try:
                old_size = len(page.parent.xref_stream_raw(xref) or b"")
            except Exception:
                old_size = len(jpeg_bytes) + 1

            if len(jpeg_bytes) < old_size or options.grayscale:
                # replace_image 会正确更新 /Filter 等字典项
                page.replace_image(xref, stream=jpeg_bytes)
                replaced += 1
        except Exception:
            continue
        finally:
            pix = None
    return replaced


def _rebuild_from_pages(
    doc: pymupdf.Document,
    options: CompressOptions,
    progress: Optional[Callable[[float, str], None]],
) -> pymupdf.Document:
    """将页面栅格化后重建 PDF（适合扫描件）。"""
    out = pymupdf.open()
    pages = list(_page_range(doc, options))
    total = len(pages)
    zoom = options.dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)

    for i, page_no in enumerate(pages):
        page = doc[page_no]
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        jpeg_bytes = _pil_to_jpeg_bytes(img, options.quality, options.grayscale)

        # 页面物理尺寸保持原样
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=jpeg_bytes)

        if progress:
            progress((i + 1) / total, f"重绘页面 {i + 1}/{total}")

    return out


def _strip_metadata(doc: pymupdf.Document) -> None:
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:
        pass


def compress_pdf(
    input_path: str | Path,
    output_path: str | Path | None = None,
    options: Optional[CompressOptions] = None,
    progress: Optional[Callable[[float, str], None]] = None,
) -> CompressResult:
    """压缩单个 PDF。未指定 output_path 时默认输出到源文件所在目录。"""
    options = options or CompressOptions()
    input_path = Path(input_path).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到文件: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF 文件: {input_path}")

    if output_path is None:
        output_path = input_path.with_name(f"{input_path.stem}_compressed.pdf")
    else:
        output_path = Path(output_path).expanduser().resolve()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    original_size = input_path.stat().st_size

    if progress:
        progress(0.02, "打开文档…")

    src = _open_doc(input_path, options.password)
    try:
        if options.mode == "rebuild":
            if progress:
                progress(0.05, "栅格化重建…")
            out_doc = _rebuild_from_pages(src, options, progress)
        else:
            if progress:
                progress(0.05, "智能压缩图像…")
            pages = list(_page_range(src, options))
            out_doc = pymupdf.open()
            if len(pages) != src.page_count:
                out_doc.insert_pdf(src, from_page=pages[0], to_page=pages[-1])
            else:
                out_doc.insert_pdf(src)

            total = out_doc.page_count
            for i in range(total):
                _compress_images_on_page(out_doc[i], options)
                if progress:
                    progress(
                        0.05 + 0.85 * (i + 1) / total,
                        f"压缩页面 {i + 1}/{total}",
                    )

        if options.remove_metadata:
            _strip_metadata(out_doc)

        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        if progress:
            progress(0.92, "写入文件…")

        out_doc.save(
            tmp_path.as_posix(),
            garbage=4,
            deflate=True,
            clean=True,
            pretty=False,
        )
        out_doc.close()

        compressed_size = tmp_path.stat().st_size
        if compressed_size >= original_size and options.mode == "smart":
            tmp_path.unlink(missing_ok=True)
            if input_path.resolve() != output_path.resolve():
                shutil.copy2(input_path, output_path)
            if progress:
                progress(1.0, "已跳过（压缩后未变小）")
            return CompressResult(
                input_path=input_path,
                output_path=output_path
                if input_path.resolve() != output_path.resolve()
                else input_path,
                pages=src.page_count,
                original_size=original_size,
                compressed_size=original_size,
                skipped=True,
                message="压缩后体积未减小，已保留原文件内容",
            )

        tmp_path.replace(output_path)
        if progress:
            progress(1.0, "完成")

        return CompressResult(
            input_path=input_path,
            output_path=output_path,
            pages=src.page_count,
            original_size=original_size,
            compressed_size=output_path.stat().st_size,
            message="压缩成功",
        )
    finally:
        src.close()


def collect_pdfs(paths: Iterable[str | Path], recursive: bool = False) -> list[Path]:
    """收集路径列表中的所有 PDF 文件（大小写不敏感，跳过隐藏的 ._ 文件）。"""
    files: list[Path] = []
    for p in paths:
        path = Path(p).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".pdf" and not path.name.startswith("._"):
            files.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            matched = sorted(
                item
                for item in iterator
                if item.is_file()
                and item.suffix.lower() == ".pdf"
                and not item.name.startswith("._")
            )
            files.extend(matched)

    seen = set()
    unique: list[Path] = []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def default_output_for(
    input_path: Path,
    output_dir: Optional[Path],
    suffix: str = "_compressed",
    used_outputs: Optional[set[Path]] = None,
) -> Path:
    """计算输出路径：output_dir 为 None 时默认输出到源文件所在目录。"""
    if output_dir:
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        base_dir = output_dir
    else:
        base_dir = input_path.parent

    candidate = base_dir / f"{input_path.stem}{suffix}.pdf"
    if used_outputs is not None:
        idx = 2
        while candidate.resolve() in used_outputs:
            candidate = base_dir / f"{input_path.stem}{suffix}_{idx}.pdf"
            idx += 1
        used_outputs.add(candidate.resolve())
    return candidate


def _open_in_file_manager(path: Path) -> None:
    target = path if path.is_dir() else path.parent
    if not target.exists():
        return
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        elif sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(target)])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# macOS 原生拖放支持（零额外依赖，直接挂接 AppKit TKContentView）
# ---------------------------------------------------------------------------

_MACOS_DND_INSTALLED = False
_MACOS_DND_HANDLERS: dict[int, dict[str, Callable]] = {}
_MACOS_DND_KEEPALIVE: list[object] = []


def _install_macos_dnd(
    root,
    on_drop: Callable[[list[str]], None],
    on_drag_enter: Optional[Callable[[], None]] = None,
    on_drag_leave: Optional[Callable[[], None]] = None,
) -> bool:
    """为 macOS Tkinter 窗口注册原生文件/文件夹拖放支持。"""
    global _MACOS_DND_INSTALLED
    if sys.platform != "darwin":
        return False

    try:
        objc_path = ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib"
        appkit_path = (
            ctypes.util.find_library("AppKit")
            or "/System/Library/Frameworks/AppKit.framework/AppKit"
        )
        foundation_path = (
            ctypes.util.find_library("Foundation")
            or "/System/Library/Frameworks/Foundation.framework/Foundation"
        )
        objc = ctypes.cdll.LoadLibrary(objc_path)
        appkit = ctypes.cdll.LoadLibrary(appkit_path)
        ctypes.cdll.LoadLibrary(foundation_path)

        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.object_getClassName.argtypes = [ctypes.c_void_p]
        objc.object_getClassName.restype = ctypes.c_char_p
        objc.class_addMethod.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        objc.class_addMethod.restype = ctypes.c_bool

        send_p = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        send_p_p = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        send_p_p_p = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        send_p_s = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
        )(("objc_msgSend", objc))
        send_p_b = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool
        )(("objc_msgSend", objc))
        send_b_p_p = ctypes.CFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        send_s = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        send_l = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        send_p_l = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        )(("objc_msgSend", objc))
        send_v_p = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))

        def sel(name: bytes) -> int:
            return objc.sel_registerName(name)

        def nsstr(text: str) -> int:
            NSString = objc.objc_getClass(b"NSString")
            return send_p_s(NSString, sel(b"stringWithUTF8String:"), text.encode("utf-8"))

        NSURL = objc.objc_getClass(b"NSURL")
        NSArray = objc.objc_getClass(b"NSArray")
        NSMutableArray = objc.objc_getClass(b"NSMutableArray")
        NSDictionary = objc.objc_getClass(b"NSDictionary")
        NSNumber = objc.objc_getClass(b"NSNumber")

        file_url_type = ctypes.c_void_p.in_dll(appkit, "NSPasteboardTypeFileURL").value
        file_only_key = ctypes.c_void_p.in_dll(
            appkit, "NSPasteboardURLReadingFileURLsOnlyKey"
        ).value

        def _pb_has_files(pb: int) -> bool:
            if not pb:
                return False
            try:
                yes_num = send_p_b(NSNumber, sel(b"numberWithBool:"), True)
                opts = send_p_p_p(
                    NSDictionary, sel(b"dictionaryWithObject:forKey:"), yes_num, file_only_key
                )
                cls_arr = send_p_p(NSArray, sel(b"arrayWithObject:"), NSURL)
                if send_b_p_p(pb, sel(b"canReadObjectForClasses:options:"), cls_arr, opts):
                    return True
                legacy = send_p_p(
                    pb, sel(b"propertyListForType:"), nsstr("NSFilenamesPboardType")
                )
                return bool(legacy and send_l(legacy, sel(b"count")) > 0)
            except Exception:
                return False

        def _extract_paths(sender: int) -> list[str]:
            paths: list[str] = []
            try:
                pb = send_p(sender, sel(b"draggingPasteboard"))
                if not pb:
                    return paths
                yes_num = send_p_b(NSNumber, sel(b"numberWithBool:"), True)
                opts = send_p_p_p(
                    NSDictionary, sel(b"dictionaryWithObject:forKey:"), yes_num, file_only_key
                )
                cls_arr = send_p_p(NSArray, sel(b"arrayWithObject:"), NSURL)
                urls = send_p_p_p(
                    pb, sel(b"readObjectsForClasses:options:"), cls_arr, opts
                )
                if urls:
                    count = send_l(urls, sel(b"count"))
                    for i in range(count):
                        url = send_p_l(urls, sel(b"objectAtIndex:"), i)
                        file_url = send_p(url, sel(b"filePathURL")) or url
                        path_ns = send_p(file_url, sel(b"path"))
                        if path_ns:
                            raw = send_s(path_ns, sel(b"UTF8String"))
                            if raw:
                                paths.append(raw.decode("utf-8", errors="surrogateescape"))

                if not paths:
                    legacy = send_p_p(
                        pb, sel(b"propertyListForType:"), nsstr("NSFilenamesPboardType")
                    )
                    if legacy:
                        count = send_l(legacy, sel(b"count"))
                        for i in range(count):
                            item = send_p_l(legacy, sel(b"objectAtIndex:"), i)
                            raw = send_s(item, sel(b"UTF8String"))
                            if raw:
                                paths.append(raw.decode("utf-8", errors="surrogateescape"))
            except Exception:
                pass
            return paths

        if not _MACOS_DND_INSTALLED:
            TKContentView = objc.objc_getClass(b"TKContentView")
            if not TKContentView:
                return False

            DRAG_OP_CB = ctypes.CFUNCTYPE(
                ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )
            BOOL_CB = ctypes.CFUNCTYPE(
                ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )
            VOID_CB = ctypes.CFUNCTYPE(
                None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )

            def _dragging_entered(self_ptr, _cmd, sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    if not handler:
                        return 0
                    pb = send_p(sender, sel(b"draggingPasteboard"))
                    valid = _pb_has_files(pb)
                    handler["drag_valid"] = valid
                    if not valid:
                        return 0
                    # 严禁在 AppKit ctypes 回调里直接调用任何 Tkinter/Tcl 方法（否则会破坏 _tkinter 的 tstate 导致 SIGABRT）
                    # 仅向纯 Python 队列投递事件，由主循环定时器安全消费
                    handler["queue"].put(("enter", None))
                    return 1  # NSDragOperationCopy
                except Exception:
                    return 0

            def _dragging_updated(self_ptr, _cmd, sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    if not handler:
                        return 0
                    return 1 if handler.get("drag_valid") else 0
                except Exception:
                    return 0

            def _dragging_exited(self_ptr, _cmd, _sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    if handler:
                        handler["drag_valid"] = False
                        handler["queue"].put(("leave", None))
                except Exception:
                    pass

            def _dragging_ended(self_ptr, _cmd, _sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    if handler:
                        handler["drag_valid"] = False
                        handler["queue"].put(("leave", None))
                except Exception:
                    pass

            def _prepare_drag(self_ptr, _cmd, _sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    return bool(handler and handler.get("drag_valid"))
                except Exception:
                    return False

            def _perform_drag(self_ptr, _cmd, sender):
                try:
                    handler = _MACOS_DND_HANDLERS.get(int(self_ptr or 0))
                    if not handler:
                        return False
                    handler["drag_valid"] = False
                    handler["queue"].put(("leave", None))
                    dropped = _extract_paths(sender)
                    if not dropped:
                        return False
                    handler["queue"].put(("drop", dropped))
                    return True
                except Exception:
                    return False

            cb_entered = DRAG_OP_CB(_dragging_entered)
            cb_updated = DRAG_OP_CB(_dragging_updated)
            cb_exited = VOID_CB(_dragging_exited)
            cb_ended = VOID_CB(_dragging_ended)
            cb_prepare = BOOL_CB(_prepare_drag)
            cb_perform = BOOL_CB(_perform_drag)
            _MACOS_DND_KEEPALIVE.extend(
                [cb_entered, cb_updated, cb_exited, cb_ended, cb_prepare, cb_perform]
            )

            objc.class_addMethod(
                TKContentView,
                sel(b"draggingEntered:"),
                ctypes.cast(cb_entered, ctypes.c_void_p),
                b"Q@:@",
            )
            objc.class_addMethod(
                TKContentView,
                sel(b"draggingUpdated:"),
                ctypes.cast(cb_updated, ctypes.c_void_p),
                b"Q@:@",
            )
            objc.class_addMethod(
                TKContentView,
                sel(b"draggingExited:"),
                ctypes.cast(cb_exited, ctypes.c_void_p),
                b"v@:@",
            )
            objc.class_addMethod(
                TKContentView,
                sel(b"draggingEnded:"),
                ctypes.cast(cb_ended, ctypes.c_void_p),
                b"v@:@",
            )
            objc.class_addMethod(
                TKContentView,
                sel(b"prepareForDragOperation:"),
                ctypes.cast(cb_prepare, ctypes.c_void_p),
                b"B@:@",
            )
            objc.class_addMethod(
                TKContentView,
                sel(b"performDragOperation:"),
                ctypes.cast(cb_perform, ctypes.c_void_p),
                b"B@:@",
            )
            _MACOS_DND_INSTALLED = True

        # 找到当前 Tk 窗口对应的 TKContentView 并注册拖放类型
        root.update_idletasks()
        root.update()

        NSApplication = objc.objc_getClass(b"NSApplication")
        app = send_p(NSApplication, sel(b"sharedApplication"))
        wins = send_p(app, sel(b"windows"))
        win_count = send_l(wins, sel(b"count")) if wins else 0

        target_title = root.title()
        target_cv = None
        fallback_cv = None
        for i in range(win_count):
            win = send_p_l(wins, sel(b"objectAtIndex:"), i)
            cv = send_p(win, sel(b"contentView"))
            if not cv:
                continue
            if objc.object_getClassName(cv) != b"TKContentView":
                continue
            if fallback_cv is None:
                fallback_cv = cv
            title_ns = send_p(win, sel(b"title"))
            if title_ns:
                raw_title = send_s(title_ns, sel(b"UTF8String"))
                if raw_title and raw_title.decode("utf-8", errors="ignore") == target_title:
                    target_cv = cv
                    break

        cv = target_cv or fallback_cv
        if not cv:
            return False

        types_arr = send_p(NSMutableArray, sel(b"array"))
        send_v_p(types_arr, sel(b"addObject:"), file_url_type)
        send_v_p(types_arr, sel(b"addObject:"), nsstr("NSFilenamesPboardType"))
        send_v_p(cv, sel(b"registerForDraggedTypes:"), types_arr)

        event_queue: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        cv_key = int(cv)
        _MACOS_DND_HANDLERS[cv_key] = {
            "queue": event_queue,
            "drag_valid": False,
        }

        alive = True

        def _poll_dnd_queue() -> None:
            if not alive:
                return
            try:
                while True:
                    kind, payload = event_queue.get_nowait()
                    if kind == "enter" and on_drag_enter:
                        on_drag_enter()
                    elif kind == "leave" and on_drag_leave:
                        on_drag_leave()
                    elif kind == "drop" and isinstance(payload, list):
                        on_drop(payload)
            except queue.Empty:
                pass
            except Exception:
                pass
            try:
                if alive and root.winfo_exists():
                    root.after(40, _poll_dnd_queue)
            except Exception:
                pass

        def _on_destroy(event) -> None:
            nonlocal alive
            if event.widget is root:
                alive = False
                _MACOS_DND_HANDLERS.pop(cv_key, None)

        root.bind("<Destroy>", _on_destroy, add="+")
        root.after(40, _poll_dnd_queue)

        # 同时支持将文件拖到程序坞图标打开
        try:
            root.createcommand(
                "::tk::mac::OpenDocument",
                lambda *args: event_queue.put(("drop", list(args))),
            )
        except Exception:
            pass

        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf_shrink",
        description=f"{APP_NAME} v{APP_VERSION} — 压缩 PDF，兼顾清晰度（{APP_COPYRIGHT}）",
    )
    parser.add_argument("inputs", nargs="*", help="PDF 文件或目录")
    parser.add_argument("-o", "--output", help="输出文件（单文件）或输出目录（批量）；留空则输出到源文件目录")
    parser.add_argument(
        "-p",
        "--preset",
        choices=list(PRESETS.keys()),
        default="balanced",
        help="压缩预设（默认 balanced）",
    )
    parser.add_argument("-q", "--quality", type=int, help="JPEG 质量 1-95（覆盖预设）")
    parser.add_argument("--dpi", type=int, help="目标 DPI（覆盖预设）")
    parser.add_argument(
        "-m",
        "--mode",
        choices=["smart", "rebuild"],
        default="smart",
        help="smart=智能压缩图像（保留文字可选中）；rebuild=整页重绘（扫描件更有效）",
    )
    parser.add_argument("-g", "--grayscale", action="store_true", help="转为灰度以进一步减小体积")
    parser.add_argument("--keep-metadata", action="store_true", help="保留元数据")
    parser.add_argument("--from-page", type=int, dest="page_from", help="起始页（从 1 开始）")
    parser.add_argument("--to-page", type=int, dest="page_to", help="结束页（含）")
    parser.add_argument("--password", default="", help="加密 PDF 的密码")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描目录中的 PDF")
    parser.add_argument("--suffix", default="_compressed", help="批量输出文件名后缀")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION} — {APP_COPYRIGHT}",
    )
    return parser


def run_cli(args: argparse.Namespace) -> int:
    quality, dpi = apply_preset(args.preset)
    if args.quality is not None:
        quality = max(1, min(95, args.quality))
    if args.dpi is not None:
        dpi = max(36, min(600, args.dpi))

    options = CompressOptions(
        quality=quality,
        dpi=dpi,
        mode=args.mode,
        grayscale=args.grayscale,
        remove_metadata=not args.keep_metadata,
        page_from=args.page_from,
        page_to=args.page_to,
        password=args.password or "",
    )

    pdfs = collect_pdfs(args.inputs, recursive=args.recursive)
    if not pdfs:
        print("未找到 PDF 文件。可使用 --gui 打开图形界面，或传入文件路径。")
        return 1

    output_arg = Path(args.output).expanduser() if args.output else None
    results: list[CompressResult] = []
    used_outputs: set[Path] = set()

    for idx, pdf in enumerate(pdfs, 1):
        if len(pdfs) == 1 and output_arg and output_arg.suffix.lower() == ".pdf":
            out = output_arg
        else:
            if output_arg is None:
                out_dir = None
            elif output_arg.suffix.lower() == ".pdf":
                out_dir = output_arg.parent
            else:
                out_dir = output_arg
            out = default_output_for(
                pdf, out_dir, suffix=args.suffix, used_outputs=used_outputs
            )

        print(f"[{idx}/{len(pdfs)}] {pdf.name}")

        def _progress(ratio: float, msg: str) -> None:
            bar_len = 24
            filled = int(bar_len * ratio)
            bar = "█" * filled + "░" * (bar_len - filled)
            print(f"\r  [{bar}] {ratio * 100:5.1f}%  {msg}   ", end="", flush=True)

        try:
            result = compress_pdf(pdf, out, options, progress=_progress)
            print()
            results.append(result)
            if result.skipped:
                print(f"  → 跳过: {result.message}")
            else:
                print(
                    f"  → {human_size(result.original_size)} → {human_size(result.compressed_size)} "
                    f"(减小 {result.ratio * 100:.1f}%) → {result.output_path}"
                )
        except Exception as e:
            print()
            print(f"  ✗ 失败: {e}")
            return 1

    if len(results) > 1:
        total_in = sum(r.original_size for r in results)
        total_out = sum(r.compressed_size for r in results)
        print("-" * 40)
        print(
            f"合计: {len(results)} 个文件，{human_size(total_in)} → {human_size(total_out)} "
            f"(减小 {(1 - total_out / total_in) * 100 if total_in else 0:.1f}%)"
        )
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


def run_gui(initial_files: Optional[list[str]] = None) -> int:
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
            except Exception:
                pass

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    # 非 macOS 若装有 tkinterdnd2 则自动启用跨平台拖放
    tkdnd_mod = None
    if sys.platform != "darwin":
        try:
            import tkinterdnd2 as tkdnd_mod  # type: ignore[import-not-found]
        except Exception:
            tkdnd_mod = None

    root = tkdnd_mod.Tk() if tkdnd_mod else tk.Tk()
    root.title(f"{APP_NAME} v{APP_VERSION} — {APP_EDITION}")
    root.minsize(680, 610)
    root.geometry("780x690")

    def show_about() -> None:
        messagebox.showinfo(
            f"关于 {APP_NAME}",
            f"{APP_NAME} v{APP_VERSION}\n"
            f"{APP_EDITION}\n\n"
            "轻量、高清晰度 PDF 压缩工具，支持直接拖入 PDF 文件或文件夹。\n\n"
            f"{APP_COPYRIGHT}\n"
            f"作者：{APP_AUTHOR}｜保留所有权利。",
            parent=root,
        )

    if sys.platform == "darwin":
        try:
            root.createcommand("tkAboutDialog", show_about)
        except Exception:
            pass

    files: list[Path] = []
    last_output_dir: Optional[Path] = None
    busy = tk.BooleanVar(value=False)

    main = ttk.Frame(root, padding=12)
    main.pack(fill=tk.BOTH, expand=True)

    # =======================================================================
    # 布局策略：先 pack 底部固定区（版权栏 -> 操作栏 -> 进度日志 -> 选项），
    # 再 pack 顶部可伸缩的文件列表区，确保「开始压缩」按钮与版权声明永不被遮挡。
    # =======================================================================

    # ---- 0. 底部版权栏 ----
    footer_wrap = ttk.Frame(main)
    footer_wrap.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
    ttk.Separator(footer_wrap, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 6))

    footer_row = ttk.Frame(footer_wrap)
    footer_row.pack(fill=tk.X)
    ttk.Label(
        footer_row,
        text=f"{APP_EDITION}  v{APP_VERSION}",
        foreground="#888888",
        font=("", 10),
    ).pack(side=tk.LEFT)

    copyright_lbl = ttk.Label(
        footer_row,
        text=f"{APP_COPYRIGHT}（保留所有权利）",
        foreground="#666666",
        font=("", 10),
        cursor="pointinghand" if sys.platform == "darwin" else "hand2",
    )
    copyright_lbl.pack(side=tk.RIGHT)
    copyright_lbl.bind("<Button-1>", lambda _e: show_about())

    # ---- 1. 底部操作栏（永远固定在窗口底部可见）----
    action_row = ttk.Frame(main)
    action_row.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))

    tip_label = ttk.Label(
        action_row,
        text="未设输出目录时，默认保存到源文件所在目录",
        foreground="#666666",
    )
    tip_label.pack(side=tk.LEFT)

    # 先声明回调占位，后面绑定实际函数
    start_btn = ttk.Button(action_row, text="开始压缩", default="active", width=14)
    start_btn.pack(side=tk.RIGHT)

    open_out_btn = ttk.Button(action_row, text="打开输出目录", state=tk.DISABLED)
    open_out_btn.pack(side=tk.RIGHT, padx=(0, 8))

    ttk.Button(action_row, text="退出", command=root.destroy).pack(
        side=tk.RIGHT, padx=(0, 8)
    )

    # ---- 2. 进度与日志 ----
    prog_frame = ttk.Frame(main)
    prog_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

    prog = ttk.Progressbar(prog_frame, mode="determinate", maximum=100)
    prog.pack(fill=tk.X, pady=(0, 4))

    status_var = tk.StringVar(
        value="就绪。拖入 PDF 文件/文件夹，或点击「添加文件」后点击「开始压缩」。"
    )
    ttk.Label(prog_frame, textvariable=status_var).pack(anchor=tk.W)

    log_wrap = ttk.Frame(prog_frame)
    log_wrap.pack(fill=tk.X, pady=(4, 0))
    log = tk.Text(log_wrap, height=5, wrap=tk.WORD, state=tk.DISABLED)
    log_scroll = ttk.Scrollbar(log_wrap, orient=tk.VERTICAL, command=log.yview)
    log.configure(yscrollcommand=log_scroll.set)
    log.pack(side=tk.LEFT, fill=tk.X, expand=True)
    log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ---- 3. 压缩选项 ----
    opt = ttk.LabelFrame(main, text="压缩选项", padding=8)
    opt.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))

    preset_keys = list(PRESETS.keys())
    preset_items = [f"{k} — {PRESET_LABELS[k]}" for k in preset_keys]
    preset_key_var = tk.StringVar(value="balanced")
    preset_display_var = tk.StringVar(value=preset_items[2])
    mode_var = tk.StringVar(value="smart")
    quality_var = tk.IntVar(value=72)
    dpi_var = tk.IntVar(value=150)
    gray_var = tk.BooleanVar(value=False)
    meta_var = tk.BooleanVar(value=True)
    password_var = tk.StringVar(value="")
    out_dir_var = tk.StringVar(value="")
    out_dir_is_placeholder = True

    row1 = ttk.Frame(opt)
    row1.pack(fill=tk.X)
    ttk.Label(row1, text="预设:").pack(side=tk.LEFT)
    preset_combo = ttk.Combobox(
        row1,
        textvariable=preset_display_var,
        values=preset_items,
        state="readonly",
        width=22,
    )
    preset_combo.pack(side=tk.LEFT, padx=(4, 6))

    preset_hint = tk.StringVar(value=PRESETS["balanced"][2])
    ttk.Label(row1, textvariable=preset_hint, foreground="#555555").pack(
        side=tk.LEFT, padx=(4, 0)
    )

    def on_preset_selected(_event=None) -> None:
        idx = preset_combo.current()
        if 0 <= idx < len(preset_keys):
            key = preset_keys[idx]
            preset_key_var.set(key)
            q, d, desc = PRESETS[key]
            quality_var.set(q)
            dpi_var.set(d)
            preset_hint.set(desc)

    preset_combo.bind("<<ComboboxSelected>>", on_preset_selected)
    preset_combo.current(2)

    row2 = ttk.Frame(opt)
    row2.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(row2, text="模式:").pack(side=tk.LEFT)
    ttk.Radiobutton(
        row2, text="智能压缩（保留可选中文字）", variable=mode_var, value="smart"
    ).pack(side=tk.LEFT, padx=(4, 14))
    ttk.Radiobutton(
        row2, text="整页重绘（扫描件更有效）", variable=mode_var, value="rebuild"
    ).pack(side=tk.LEFT)

    row3 = ttk.Frame(opt)
    row3.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(row3, text="JPEG 质量:").pack(side=tk.LEFT)
    ttk.Spinbox(row3, from_=1, to=95, textvariable=quality_var, width=5).pack(
        side=tk.LEFT, padx=(4, 14)
    )
    ttk.Label(row3, text="DPI:").pack(side=tk.LEFT)
    ttk.Spinbox(row3, from_=36, to=600, textvariable=dpi_var, width=5).pack(
        side=tk.LEFT, padx=(4, 14)
    )
    ttk.Checkbutton(row3, text="灰度", variable=gray_var).pack(
        side=tk.LEFT, padx=(0, 12)
    )
    ttk.Checkbutton(row3, text="清除元数据", variable=meta_var).pack(
        side=tk.LEFT, padx=(0, 14)
    )
    ttk.Label(row3, text="密码:").pack(side=tk.LEFT)
    ttk.Entry(row3, textvariable=password_var, show="*", width=11).pack(
        side=tk.LEFT, padx=(4, 0)
    )

    row4 = ttk.Frame(opt)
    row4.pack(fill=tk.X, pady=(6, 0))
    ttk.Label(row4, text="输出目录:").pack(side=tk.LEFT)

    out_entry = tk.Entry(row4, textvariable=out_dir_var)
    out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
    default_entry_fg = out_entry.cget("foreground")

    def show_out_placeholder() -> None:
        nonlocal out_dir_is_placeholder
        out_dir_is_placeholder = True
        out_dir_var.set(OUT_DIR_PLACEHOLDER)
        out_entry.configure(foreground="#888888")

    def clear_out_placeholder() -> None:
        nonlocal out_dir_is_placeholder
        if out_dir_is_placeholder:
            out_dir_is_placeholder = False
            out_dir_var.set("")
            out_entry.configure(foreground=default_entry_fg)

    def get_selected_out_dir() -> Optional[Path]:
        if out_dir_is_placeholder:
            return None
        raw = out_dir_var.get().strip()
        if not raw or raw == OUT_DIR_PLACEHOLDER:
            return None
        return Path(raw).expanduser()

    def on_out_focus_in(_event=None) -> None:
        clear_out_placeholder()

    def on_out_focus_out(_event=None) -> None:
        if not out_dir_var.get().strip():
            show_out_placeholder()

    out_entry.bind("<FocusIn>", on_out_focus_in)
    out_entry.bind("<FocusOut>", on_out_focus_out)
    show_out_placeholder()

    def choose_out_dir() -> None:
        nonlocal out_dir_is_placeholder
        d = filedialog.askdirectory(title="选择输出目录（取消则保持默认：源文件所在目录）")
        if d:
            out_dir_is_placeholder = False
            out_dir_var.set(d)
            out_entry.configure(foreground=default_entry_fg)

    def reset_out_dir() -> None:
        show_out_placeholder()

    ttk.Button(row4, text="浏览…", command=choose_out_dir).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(row4, text="默认(源目录)", command=reset_out_dir).pack(side=tk.LEFT)

    # ---- 4. 顶部：待压缩文件列表（可伸缩，支持直接拖入）----
    file_frame = ttk.LabelFrame(
        main, text="待压缩文件（可直接拖入 PDF 文件或文件夹）", padding=8
    )
    file_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # 拖放高亮边框容器
    drop_border = tk.Frame(file_frame, bg="#C8CDD5", padx=2, pady=2)
    drop_border.pack(fill=tk.BOTH, expand=True)

    list_inner = tk.Frame(drop_border)
    list_inner.pack(fill=tk.BOTH, expand=True)

    listbox = tk.Listbox(
        list_inner,
        selectmode=tk.EXTENDED,
        height=7,
        borderwidth=0,
        highlightthickness=0,
    )
    list_bg = listbox.cget("background")
    list_inner.configure(bg=list_bg)
    scroll = ttk.Scrollbar(list_inner, orient=tk.VERTICAL, command=listbox.yview)
    listbox.configure(yscrollcommand=scroll.set)
    listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # 空列表时的居中拖放引导提示
    empty_hint = tk.Label(
        list_inner,
        text="将 PDF 文件或文件夹拖到此处\n（支持同时拖入多个文件与文件夹，也可点击下方按钮添加）",
        fg="#888888",
        bg=list_bg,
        justify=tk.CENTER,
    )

    # 拖动经过时的醒目提示条
    drag_banner = tk.Label(
        list_inner,
        text="松开鼠标，立即添加到待压缩列表",
        fg="#1D4ED8",
        bg="#DBEAFE",
        font=("", 12, "bold"),
        pady=8,
    )

    btn_row = ttk.Frame(file_frame)
    btn_row.pack(fill=tk.X, pady=(8, 0))

    count_var = tk.StringVar(value="共 0 个文件")

    def log_line(text: str) -> None:
        log.configure(state=tk.NORMAL)
        log.insert(tk.END, text + "\n")
        log.see(tk.END)
        log.configure(state=tk.DISABLED)

    def refresh_list() -> None:
        listbox.delete(0, tk.END)
        total_bytes = 0
        for f in files:
            if f.exists():
                sz = f.stat().st_size
                total_bytes += sz
                size_str = human_size(sz)
            else:
                size_str = "?"
            listbox.insert(tk.END, f"{f.name}  ({size_str})  —  {f.parent}")

        if files:
            empty_hint.place_forget()
            count_var.set(f"共 {len(files)} 个文件（{human_size(total_bytes)}）")
            start_btn.configure(text=f"开始压缩 ({len(files)})")
            if not busy.get():
                open_out_btn.configure(state=tk.NORMAL)
        else:
            empty_hint.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
            count_var.set("共 0 个文件")
            start_btn.configure(text="开始压缩")
            if last_output_dir is None:
                open_out_btn.configure(state=tk.DISABLED)

    def add_paths(raw_paths: Iterable[str | Path], source: str = "添加") -> None:
        if busy.get():
            status_var.set("正在压缩中，请稍后再添加文件。")
            return
        added = 0
        duplicates = 0
        existing = {f.resolve() for f in files}

        for raw in raw_paths:
            p = Path(raw).expanduser()
            if not p.exists():
                continue
            p = p.resolve()
            if p.is_dir():
                # 文件夹仅收集其中的 PDF，其他格式直接静默忽略
                for pdf in collect_pdfs([p], recursive=True):
                    if pdf.resolve() in existing:
                        duplicates += 1
                    else:
                        files.append(pdf)
                        existing.add(pdf.resolve())
                        added += 1
            elif p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("._"):
                if p in existing:
                    duplicates += 1
                else:
                    files.append(p)
                    existing.add(p)
                    added += 1
            # 非 PDF 文件直接静默忽略，不做提示

        refresh_list()
        parts: list[str] = []
        if added:
            parts.append(f"已{source} {added} 个 PDF 文件")
        if duplicates:
            parts.append(f"跳过 {duplicates} 个重复文件")
        if parts:
            msg = "，".join(parts) + "。"
            status_var.set(msg)
            log_line(msg)

    def add_files() -> None:
        paths = filedialog.askopenfilenames(
            title="选择 PDF",
            filetypes=[("PDF 文件", "*.pdf *.PDF"), ("所有文件", "*.*")],
        )
        if paths:
            add_paths(paths, source="添加")

    def add_folder() -> None:
        folder = filedialog.askdirectory(title="选择包含 PDF 的文件夹")
        if folder:
            add_paths([folder], source="扫描添加")

    def remove_selected() -> None:
        selected = list(listbox.curselection())
        for i in reversed(selected):
            del files[i]
        refresh_list()

    def clear_files() -> None:
        files.clear()
        refresh_list()

    def on_drag_enter() -> None:
        drop_border.configure(bg="#2563EB")
        drag_banner.place(relx=0.5, rely=0.14, anchor=tk.CENTER, relwidth=0.92)

    def on_drag_leave() -> None:
        drop_border.configure(bg="#C8CDD5")
        drag_banner.place_forget()

    def on_drop_paths(paths: list[str]) -> None:
        on_drag_leave()
        add_paths(paths, source="拖入")

    listbox.bind("<BackSpace>", lambda _e: remove_selected())
    listbox.bind("<Delete>", lambda _e: remove_selected())

    add_file_btn = ttk.Button(btn_row, text="添加文件", command=add_files)
    add_file_btn.pack(side=tk.LEFT, padx=(0, 6))
    add_dir_btn = ttk.Button(btn_row, text="添加文件夹", command=add_folder)
    add_dir_btn.pack(side=tk.LEFT, padx=(0, 6))
    rm_btn = ttk.Button(btn_row, text="移除选中", command=remove_selected)
    rm_btn.pack(side=tk.LEFT, padx=(0, 6))
    clear_btn = ttk.Button(btn_row, text="清空", command=clear_files)
    clear_btn.pack(side=tk.LEFT)
    ttk.Label(btn_row, textvariable=count_var, foreground="#555555").pack(side=tk.RIGHT)

    def open_output_folder() -> None:
        chosen = get_selected_out_dir()
        if chosen and chosen.exists():
            _open_in_file_manager(chosen)
        elif last_output_dir and last_output_dir.exists():
            _open_in_file_manager(last_output_dir)
        elif files:
            _open_in_file_manager(files[0].parent)

    open_out_btn.configure(command=open_output_folder)

    def set_busy(flag: bool) -> None:
        busy.set(flag)
        state = tk.DISABLED if flag else tk.NORMAL
        for btn in (add_file_btn, add_dir_btn, rm_btn, clear_btn, start_btn):
            btn.configure(state=state)
        if not flag and (last_output_dir or files):
            open_out_btn.configure(state=tk.NORMAL)

    def start_compress() -> None:
        nonlocal last_output_dir
        if busy.get():
            return
        if not files:
            messagebox.showinfo(APP_NAME, "请先拖入或添加待压缩的 PDF 文件。")
            return

        try:
            q_val = max(1, min(95, int(quality_var.get())))
            dpi_val = max(36, min(600, int(dpi_var.get())))
        except Exception:
            messagebox.showerror(APP_NAME, "质量或 DPI 请输入有效整数。")
            return

        options = CompressOptions(
            quality=q_val,
            dpi=dpi_val,
            mode=mode_var.get(),
            grayscale=bool(gray_var.get()),
            remove_metadata=bool(meta_var.get()),
            password=password_var.get().strip(),
        )
        out_dir = get_selected_out_dir()  # 未填则为 None -> 默认源文件所在目录
        work_list = list(files)

        set_busy(True)
        prog.configure(value=0)
        if out_dir is None:
            log_line("输出目录：默认（各源文件所在目录）")
        else:
            log_line(f"输出目录：{out_dir}")

        worker_queue: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()

        def poll_worker_queue() -> None:
            done = False
            try:
                while True:
                    kind, payload = worker_queue.get_nowait()
                    if kind == "progress":
                        val, text = payload  # type: ignore[misc]
                        prog.configure(value=val)
                        status_var.set(text)
                    elif kind == "log":
                        log_line(str(payload))
                    elif kind == "done":
                        summary = str(payload)
                        status_var.set(summary)
                        log_line(summary)
                        prog.configure(value=100)
                        set_busy(False)
                        messagebox.showinfo(APP_NAME, summary)
                        done = True
                    elif kind == "error":
                        log_line(str(payload))
                        set_busy(False)
                        messagebox.showerror(APP_NAME, "发生错误，详见日志。")
                        done = True
            except queue.Empty:
                pass
            if not done:
                try:
                    if root.winfo_exists():
                        root.after(40, poll_worker_queue)
                except Exception:
                    pass

        def worker() -> None:
            nonlocal last_output_dir
            total = len(work_list)
            ok = 0
            total_in = 0
            total_out = 0
            used_outputs: set[Path] = set()
            first_out_dir: Optional[Path] = None

            try:
                for i, pdf in enumerate(work_list):
                    out = default_output_for(pdf, out_dir, used_outputs=used_outputs)
                    if first_out_dir is None:
                        first_out_dir = out.parent

                    def progress(ratio: float, msg: str, index=i, path=pdf) -> None:
                        overall = (index + ratio) / total * 100
                        worker_queue.put(
                            ("progress", (overall, f"[{index + 1}/{total}] {path.name}: {msg}"))
                        )

                    worker_queue.put(("log", f"开始: {pdf.name}"))
                    try:
                        result = compress_pdf(pdf, out, options, progress=progress)
                        total_in += result.original_size
                        total_out += result.compressed_size
                        ok += 1
                        if result.skipped:
                            tip = f"  跳过 — {result.message}\n  输出: {result.output_path}"
                        else:
                            tip = (
                                f"  完成 — {human_size(result.original_size)} → "
                                f"{human_size(result.compressed_size)} "
                                f"(减小 {result.ratio * 100:.1f}%)\n"
                                f"  输出: {result.output_path}"
                            )
                        worker_queue.put(("log", tip))
                    except Exception as e:
                        worker_queue.put(("log", f"  失败 — {e}"))

                last_output_dir = out_dir or first_out_dir
                summary = (
                    f"全部完成: {ok}/{total} 成功。"
                    f"总体积 {human_size(total_in)} → {human_size(total_out)}"
                    + (
                        f"（减小 {(1 - total_out / total_in) * 100:.1f}%）"
                        if total_in
                        else ""
                    )
                )
                worker_queue.put(("done", summary))
            except Exception:
                worker_queue.put(("error", traceback.format_exc()))

        root.after(40, poll_worker_queue)
        threading.Thread(target=worker, daemon=True).start()

    start_btn.configure(command=start_compress)
    root.bind("<Command-Return>", lambda _e: start_compress())
    root.bind("<Control-Return>", lambda _e: start_compress())

    refresh_list()

    # 启用拖放
    dnd_ok = False
    if sys.platform == "darwin":
        dnd_ok = _install_macos_dnd(
            root,
            on_drop=on_drop_paths,
            on_drag_enter=on_drag_enter,
            on_drag_leave=on_drag_leave,
        )
    elif tkdnd_mod is not None:
        try:
            root.drop_target_register(tkdnd_mod.DND_FILES)  # type: ignore[attr-defined]
            root.dnd_bind(  # type: ignore[attr-defined]
                "<<Drop>>",
                lambda e: on_drop_paths(list(root.tk.splitlist(e.data))),
            )
            root.dnd_bind("<<DropEnter>>", lambda _e: on_drag_enter())  # type: ignore[attr-defined]
            root.dnd_bind("<<DropLeave>>", lambda _e: on_drag_leave())  # type: ignore[attr-defined]
            dnd_ok = True
        except Exception:
            dnd_ok = False

    if not dnd_ok:
        empty_hint.configure(
            text="请点击下方「添加文件」或「添加文件夹」添加 PDF"
        )

    if initial_files:
        root.after(50, lambda paths=list(initial_files): add_paths(paths, source="添加"))

    if sys.platform == "darwin":
        def _bring_to_front() -> None:
            try:
                root.deiconify()
                root.lift()
                root.focus_force()
            except Exception:
                pass

        try:
            root.createcommand("::tk::mac::ReopenApplication", _bring_to_front)
        except Exception:
            pass
        root.after(60, _bring_to_front)

    root.mainloop()
    return 0


def _strip_macos_launch_args(argv: list[str]) -> list[str]:
    """过滤 macOS LaunchServices 启动 .app 时可能注入的系统参数。"""
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("-psn_"):
            i += 1
            continue
        if (
            sys.platform == "darwin"
            and getattr(sys, "frozen", False)
            and (arg.startswith("-NS") or arg.startswith("-Apple"))
        ):
            # macOS 用户默认参数通常以 "-Key Value" 成对出现
            i += 2 if (i + 1 < len(argv) and not argv[i + 1].startswith("-")) else 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = _strip_macos_launch_args(list(sys.argv[1:] if argv is None else argv))

    parser = build_parser()
    args = parser.parse_args(raw_argv)

    # 打包版无控制台窗口时（如直接把文件拖到 .exe 图标上），在图形界面中打开这些文件
    if getattr(sys, "frozen", False) and sys.stdout is None:
        return run_gui(initial_files=args.inputs or None)

    if args.gui or not args.inputs:
        return run_gui(initial_files=args.inputs or None)
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
