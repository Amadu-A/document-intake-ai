# app/infrastructure/conversion.py
"""Изолированный процесс технической конвертации; семантическая нарезка запрещена."""

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import warnings
import zipfile
from collections.abc import Callable
from pathlib import Path
from xml.etree import ElementTree

import pymupdf
from PIL import Image, ImageSequence

from app.domain.errors import IntakeError

MIMES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}


async def stop_process(process) -> None:
    """Завершает изолированный конвертер и дренирует pipe, включая отмену родителя."""
    if process.returncode is None:
        try:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


def write_ocr_window(pages: list[dict], target: Path) -> None:
    """Собирает PDF OCR-окна на диске в ограниченном дочернем процессе."""
    result, readers = pymupdf.open(), {}
    try:
        for page in pages:
            if page["pdf"] not in readers:
                readers[page["pdf"]] = pymupdf.open(page["pdf"])
            result.insert_pdf(
                readers[page["pdf"]],
                from_page=page["page_number"] - 1,
                to_page=page["page_number"] - 1,
                links=False,
                annots=False,
            )
        result.save(target, garbage=4, deflate=True)
    finally:
        result.close()
        for reader in readers.values():
            reader.close()


def validate_signature(path: Path, suffix: str) -> str:
    """Проверяет сигнатуру, OOXML zip-лимиты и отсутствие активного содержимого."""
    if suffix not in MIMES:
        raise IntakeError("unsupported_file", "Неподдерживаемый формат файла")
    with path.open("rb") as stream:
        header = stream.read(1024)
    signatures = {
        ".pdf": header.lstrip().startswith(b"%PDF-"),
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".tif": header.startswith((b"II*\x00", b"MM\x00*")),
        ".tiff": header.startswith((b"II*\x00", b"MM\x00*")),
        ".doc": header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        ".docx": header.startswith(b"PK\x03\x04"),
        ".txt": b"\x00" not in header or header.startswith((b"\xff\xfe", b"\xfe\xff")),
    }
    if not signatures[suffix]:
        raise IntakeError("invalid_mime", "Сигнатура не соответствует расширению файла")
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > 2000 or sum(i.file_size for i in infos) > 100_000_000:
                    raise IntakeError("upload_limit", "Превышены лимиты DOCX-архива")
                names = {i.filename for i in infos}
                if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                    raise IntakeError("invalid_mime", "Архив не является документом DOCX")
                for info in infos:
                    name = info.filename.lower()
                    if (
                        info.flag_bits & 1
                        or ".." in Path(name).parts
                        or name.startswith("/")
                        or "\\" in name
                        or ":" in name
                        or "vbaproject" in name
                        or "embeddings/" in name
                        or (
                            info.file_size > 1_000_000
                            and info.file_size > 200 * max(info.compress_size, 1)
                        )
                    ):
                        raise IntakeError("corrupt_document", "Небезопасное содержимое DOCX")
                    if name.endswith(".rels"):
                        content = archive.read(info)
                        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                            raise IntakeError("corrupt_document", "DTD в DOCX не разрешены")
                        relations = ElementTree.fromstring(content)  # noqa: S314
                        if any(
                            item.attrib.get("TargetMode", "").lower() == "external"
                            for item in relations.iter()
                        ):
                            raise IntakeError("corrupt_document", "Внешние связи DOCX не разрешены")
        except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise IntakeError("corrupt_document", "Повреждённый DOCX") from exc
    return MIMES[suffix]


def restrict_process(timeout: int) -> None:
    """На Linux ограничивает CPU/память и запрещает сеть конвертеру и его дочерним процессам."""
    if sys.platform != "linux":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 2))
    resource.setrlimit(resource.RLIMIT_AS, (2_500_000_000, 2_500_000_000))
    resource.setrlimit(resource.RLIMIT_FSIZE, (600_000_000, 600_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int

    class ArgumentComparison(ctypes.Structure):
        """Описывает libseccomp-сравнение аргумента семейства socket syscall."""

        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_int),
            ("datum_a", ctypes.c_uint64),
            ("datum_b", ctypes.c_uint64),
        ]

    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(ArgumentComparison),
    ]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)
    try:
        # AF_UNIX нужен LibreOffice для локального IPC; IPv4/IPv6 запрещены всем потомкам.
        for family in (2, 10):
            comparison = ArgumentComparison(0, 4, family, 0)
            if (
                library.seccomp_rule_add_array(
                    context,
                    0x00050001,
                    library.seccomp_syscall_resolve_name(b"socket"),
                    1,
                    ctypes.byref(comparison),
                )
                != 0
            ):
                raise RuntimeError("seccomp rule failed")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("seccomp load failed")
    finally:
        library.seccomp_release(context)


def convert(
    path: Path, target: Path, settings: dict, progress: Callable[[str, dict], None] | None = None
) -> dict:
    """Нормализует один оригинал в PDF и страницы, не меняя порядок или исходник."""
    suffix = path.suffix.lower()
    validate_signature(path, suffix)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = target / "normalized.pdf"
    if suffix in {".doc", ".docx", ".txt"}:
        office_input = path
        if suffix == ".txt":
            with path.open("rb") as stream:
                header = stream.read(4)
            encoding = "utf-16" if header.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            office_input = target / "text-input.txt"
            try:
                with (
                    path.open("r", encoding=encoding) as source,
                    office_input.open("w", encoding="utf-8") as output_text,
                ):
                    shutil.copyfileobj(source, output_text, length=64 * 1024)
            except UnicodeDecodeError:
                with (
                    path.open("r", encoding="cp1251") as source,
                    office_input.open("w", encoding="utf-8") as output_text,
                ):
                    shutil.copyfileobj(source, output_text, length=64 * 1024)
        profile = target / "office-profile"
        profile.mkdir(mode=0o700)
        (profile / "user").mkdir()
        (profile / "user/registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Writer/Content/Update">'
            '<prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item></oor:items>',
            "utf-8",
        )
        try:
            subprocess.run(
                [
                    "libreoffice",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--norestore",
                    f"-env:UserInstallation={profile.resolve().as_uri()}",
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(target),
                    str(office_input),
                ],
                check=True,
                capture_output=True,
                timeout=settings["timeout"],
            )
            generated = target / (office_input.stem + ".pdf")
            generated.replace(output)
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntakeError("file_conversion_failed", "Конвертация Office не выполнена") from exc
        document = pymupdf.open(output)
    elif suffix == ".pdf":
        document = pymupdf.open(path)
        if document.needs_pass:
            raise IntakeError("password_protected", "PDF защищён паролем")
        if len(document) > settings["max_pages"]:
            raise IntakeError("page_limit", "Превышен лимит страниц")
        # Внешний вид заполненных полей и пометок сохраняется, оригинал не перезаписывается.
        document.bake(annots=True, widgets=True)
        # Новый PDF исключает JavaScript, вложенные файлы и интерактивные объекты.
        safe = pymupdf.open()
        safe.insert_pdf(document, links=False, annots=False, widgets=False)
        document.close()
        safe.save(output, garbage=4, deflate=True)
        safe.close()
        document = pymupdf.open(output)
    else:
        document = pymupdf.open()
        Image.MAX_IMAGE_PIXELS = settings["max_pixels"]
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                for index, frame in enumerate(ImageSequence.Iterator(source)):
                    if index >= settings["max_pages"]:
                        raise IntakeError("page_limit", "Превышен лимит кадров изображения")
                    if frame.width * frame.height > settings["max_pixels"]:
                        raise IntakeError("upload_limit", "Превышен лимит пикселей изображения")
                    import io

                    buffer = io.BytesIO()
                    frame.convert("RGB").save(buffer, format="PNG")
                    width = min(frame.width * 0.75, 1440)
                    height = width * frame.height / frame.width
                    page = document.new_page(width=width, height=height)
                    page.insert_image(page.rect, stream=buffer.getvalue())
        document.save(output, garbage=4, deflate=True)
    if not 0 < len(document) <= settings["max_pages"]:
        raise IntakeError("page_limit", "Недопустимое число страниц")
    if progress:
        progress("source_discovered", {"page_count": len(document)})
    pages = []
    for number, page in enumerate(document, 1):
        if shutil.disk_usage(target).free < settings.get("min_free_bytes", 0):
            raise IntakeError(
                "storage_insufficient", "Недостаточно места для подготовки страниц", 507
            )
        rect = page.rect
        if not 1 <= rect.width <= 20000 or not 1 <= rect.height <= 20000:
            raise IntakeError("corrupt_document", "Недопустимая геометрия PDF")
        scale = settings["render_side"] / max(rect.width, rect.height)
        preview = target / f"page-{number}.png"
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        # PNG остаётся без потерь; быстрое сжатие экономит CPU при подготовке больших пачек.
        pixmap.pil_save(preview, format="PNG", compress_level=1)
        thumbnail = target / f"thumb-{number}.png"
        small = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        small.thumbnail((180, 180), Image.Resampling.LANCZOS)
        small.save(thumbnail)
        small.close()
        # Отдельный фрагмент из PDF сохраняет мелкие цифры, потерянные в UI-превью.
        footer_preview = target / f"footer-{number}.png"
        detail_scale = min(settings["render_side"] * 1.5, 2400) / max(rect.width, rect.height)
        footer = page.get_pixmap(
            matrix=pymupdf.Matrix(detail_scale, detail_scale),
            clip=pymupdf.Rect(rect.x0, rect.y0 + rect.height * 2 / 3, rect.x1, rect.y1),
            alpha=False,
        )
        footer.pil_save(footer_preview, format="PNG", compress_level=1)
        header_preview = target / f"header-{number}.png"
        header = page.get_pixmap(
            matrix=pymupdf.Matrix(detail_scale, detail_scale),
            clip=pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height / 3),
            alpha=False,
        )
        header.pil_save(header_preview, format="PNG", compress_level=1)
        item = {
            "page_number": number,
            "preview": str(preview),
            "thumbnail": str(thumbnail),
            "footer_preview": str(footer_preview),
            "header_preview": str(header_preview),
            "pdf": str(output),
        }
        pages.append(item)
        if progress:
            progress("page_ready", item)
    document.close()
    return {"pages": pages, "normalized_pdf": str(output)}


def main() -> int:
    """Обрабатывает приватный manifest; stdout содержит лишь безопасный код ошибки."""
    request = Path(sys.argv[1])
    try:
        data = json.loads(request.read_text("utf-8"))
        restrict_process(data["settings"]["timeout"])
        if data.get("operation") in {"ocr_window", "pdf_pages"}:
            write_ocr_window(data["pages"], Path(data["target"]))
            print(json.dumps({"byte_size": Path(data["target"]).stat().st_size}))
            return 0

        def progress(kind: str, payload: dict) -> None:
            """Передаёт метаданные только родительскому процессу через закрытый stdout pipe."""
            print(json.dumps({"event": kind, "data": payload}), flush=True)

        result = convert(Path(data["path"]), Path(data["target"]), data["settings"], progress)
        request.with_suffix(".result.json").write_text(json.dumps(result), "utf-8")
        return 0
    except IntakeError as exc:
        print(exc.code)
    except Exception:
        print("corrupt_document")
    return 1


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
