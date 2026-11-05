# tests/test_conversion.py
"""Реальная техническая конвертация PDF/изображений без OCR и семантических эвристик."""

from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from PIL import Image

from app.domain.errors import IntakeError
from app.infrastructure.conversion import convert, validate_signature
from tests.support import MemoryUpload, pdf_bytes


def limits(max_pages=100):
    """Ограничивает геометрию и число страниц тестового конвертера."""
    return {"timeout": 20, "max_pages": max_pages, "max_pixels": 4_000_000, "render_side": 800}


@pytest.mark.parametrize(
    "format,suffix,mime",
    [("PNG", ".png", "image/png"), ("JPEG", ".jpg", "image/jpeg"), ("TIFF", ".tiff", "image/tiff")],
)
def test_images_have_real_pdf_and_preview(tmp_path, format, suffix, mime):
    """Каждый обязательный raster-формат превращается в настоящий PDF и PNG-render."""
    source = tmp_path / ("synthetic" + suffix)
    Image.new("RGB", (300, 400), "white").save(source, format=format)
    assert validate_signature(source, suffix) == mime
    result = convert(source, tmp_path / "output", limits())
    with pymupdf.open(result["normalized_pdf"]) as document:
        assert len(document) == 1
    assert Path(result["pages"][0]["preview"]).is_file()


def test_multipage_tiff_preserves_frame_order(tmp_path):
    """Красный, зелёный и синий кадры TIFF сохраняют последовательность страниц PDF."""
    source = tmp_path / "multipage.tiff"
    frames = [Image.new("RGB", (80, 100), color) for color in ("red", "green", "blue")]
    frames[0].save(source, save_all=True, append_images=frames[1:])
    result = convert(source, tmp_path / "output", limits())
    assert len(result["pages"]) == 3
    with pymupdf.open(result["normalized_pdf"]) as document:
        colors = [page.get_pixmap().pixel(20, 20) for page in document]
        assert colors[0][0] > 200 and colors[1][1] > 100 and colors[2][2] > 200


def test_pdf_over_fifty_pages_is_not_rejected_by_docling_limit(tmp_path):
    """Нормализация принимает 53 страницы, сохраняя исходные физические номера."""
    source = tmp_path / "large.pdf"
    source.write_bytes(pdf_bytes(53))
    result = convert(source, tmp_path / "output", limits())
    assert len(result["pages"]) == 53
    assert result["pages"][-1]["page_number"] == 53


def test_password_protected_pdf_is_explicit_error(tmp_path):
    """Зашифрованный PDF возвращает диагностический код, а не пустой успешный результат."""
    source = tmp_path / "encrypted.pdf"
    with pymupdf.open(stream=pdf_bytes()) as document:
        document.save(
            source,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="synthetic",
            owner_pw="synthetic-owner",
        )
    with pytest.raises(IntakeError) as error:
        convert(source, tmp_path / "output", limits())
    assert error.value.code == "password_protected"


def test_pdf_form_values_are_visible_after_safe_normalization(tmp_path):
    """Удаление интерактивности не должно терять видимое значение заполненного поля."""
    source = tmp_path / "filled.pdf"
    document = pymupdf.open()
    page = document.new_page()
    field = pymupdf.Widget()
    field.field_name = "synthetic-number"
    field.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    field.field_value = "FILLED-7429"
    field.rect = pymupdf.Rect(40, 60, 240, 100)
    page.add_widget(field)
    document.save(source)
    document.close()
    original = source.read_bytes()
    result = convert(source, tmp_path / "output", limits())
    with pymupdf.open(result["normalized_pdf"]) as normalized:
        assert "FILLED-7429" in normalized[0].get_text()
        assert not list(normalized[0].widgets() or [])
    assert source.read_bytes() == original


async def test_storage_cannot_escape_workspace(container):
    """Пользовательское имя не становится filesystem path, traversal артефакта запрещён."""
    job_id = str(uuid4())
    sources = await container.storage.ingest(
        job_id, [MemoryUpload(pdf_bytes(), "../../outside.pdf")]
    )
    assert sources[0].file_name == "outside.pdf"
    assert Path(sources[0].path).parent == container.storage.directory(job_id)
    with pytest.raises(IntakeError):
        container.storage.resolve("C:/outside.pdf")


@pytest.mark.parametrize(
    "relation",
    [
        '<Relationship TargetMode="External"/>',
        "<Relationship TargetMode = 'External'/>",
    ],
)
def test_docx_archive_bomb_and_external_links_are_rejected(tmp_path, relation):
    """OOXML не извлекается без лимитов; внешние связи не передаются Office-конвертеру."""
    import zipfile

    source = tmp_path / "external.docx"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("word/_rels/document.xml.rels", relation)
    with pytest.raises(IntakeError):
        validate_signature(source, ".docx")
