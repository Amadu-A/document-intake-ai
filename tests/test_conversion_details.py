# tests/test_conversion_details.py
"""Фрагмент мелких реквизитов строится из исходного PDF, без потери страниц или подмены цифр."""

import hashlib
from pathlib import Path
from threading import get_ident

import pymupdf
import pytest
from PIL import Image

from app.infrastructure.conversion import convert
from tests.support import pdf_bytes
from tests.test_conversion import limits


@pytest.mark.parametrize("rotation", [0, 90])
def test_footer_is_rendered_from_pdf_at_higher_resolution(tmp_path, rotation):
    """PNG совпадает с пикселями исходного PDF-фрагмента; UI-превью и оригинал сохраняются."""
    source = tmp_path / "synthetic.pdf"
    with pymupdf.open() as document:
        for color in ((0, 0, 1), (1, 0, 0)):
            page = document.new_page(width=200, height=300)
            page.draw_rect(pymupdf.Rect(0, 0, 200, 100), fill=(0, 1, 0), color=None)
            page.draw_rect(pymupdf.Rect(0, 200, 200, 300), fill=color, color=None)
            page.set_rotation(rotation)
        document.save(source)
    payload = source.read_bytes()
    result = convert(source, tmp_path / "output", limits())
    assert len(result["pages"]) == 2
    assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(payload).digest()
    with pymupdf.open(result["normalized_pdf"]) as document:
        for number, item in enumerate(result["pages"]):
            assert item["page_number"] == number + 1
            with (
                Image.open(item["footer_preview"]) as footer,
                Image.open(item["preview"]) as preview,
            ):
                color = bytes([0, 0, 255] if number == 0 else [255, 0, 0])
                if rotation == 0:
                    assert footer.size == (800, 400)
                    assert footer.tobytes() == color * (800 * 400)
                else:
                    assert footer.size == (1200, 267)
                    assert [footer.getpixel((x, 100)) for x in (200, 600, 1000)] == [
                        tuple(color),
                        (255, 255, 255),
                        (0, 255, 0),
                    ]
                assert footer.width > preview.width
            assert Path(item["thumbnail"]).is_file()
            with Image.open(item["header_preview"]) as header:
                if rotation == 0:
                    assert header.size == (800, 480)
                    assert header.getpixel((400, 200)) == (0, 255, 0)
                    assert header.getpixel((400, 450)) == (255, 255, 255)
                else:
                    assert header.size == (1200, 320)
                    assert [header.getpixel((x, 100)) for x in (200, 600, 1000)] == [
                        tuple(color),
                        (255, 255, 255),
                        (0, 255, 0),
                    ]


def test_header_keeps_full_requisite_crossing_upper_third(tmp_path):
    """Строка на границе верхней трети сохраняется целиком; PDF и обычное превью прежние."""
    source = tmp_path / "synthetic-requisite.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=300)
        # Полоса имитирует строку даты, нижняя часть которой выходила за прежний clip.
        page.draw_rect(pymupdf.Rect(10, 94, 190, 110), fill=(0, 0, 1), color=None)
        document.save(source)
    original = source.read_bytes()
    result = convert(source, tmp_path / "output", limits())
    assert source.read_bytes() == original
    assert len(result["pages"]) == 1
    with Image.open(result["pages"][0]["header_preview"]) as header:
        assert header.height > 110 * 4
        assert all(header.getpixel((400, y * 4)) == (0, 0, 255) for y in (95, 100, 109))


def test_progress_publishes_complete_pngs_and_mupdf_stays_in_main_thread(tmp_path, monkeypatch):
    """Page-ready означает четыре читаемых PNG; PyMuPDF не вызывается потоками сжатия."""
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(pdf_bytes(3))
    main_thread, ready = get_ident(), []
    original = pymupdf.Page.get_pixmap

    def render(page, *args, **kwargs):
        """Отклоняет небезопасное обращение к PyMuPDF из encoder-потока."""
        assert get_ident() == main_thread
        return original(page, *args, **kwargs)

    def progress(kind, data):
        """Проверяет файлы в момент публикации, когда клиент уже вправе их скачать."""
        if kind == "page_ready":
            for key in ("preview", "thumbnail", "header_preview", "footer_preview"):
                with Image.open(data[key]) as image:
                    image.verify()
            ready.append(data["page_number"])

    monkeypatch.setattr(pymupdf.Page, "get_pixmap", render)
    convert(source, tmp_path / "output", limits(), progress)
    assert ready == [1, 2, 3]


def test_encoder_failure_cannot_publish_an_incomplete_page(tmp_path, monkeypatch):
    """Ошибка фонового PNG-кодирования прерывает подготовку до события готовности страницы."""
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(pdf_bytes())
    original, events = Image.Image.save, []

    def save(image, target, *args, **kwargs):
        """Имитирует отказ записи только детального PNG, сохраняя остальные настоящие файлы."""
        if Path(target).name.startswith("header-"):
            raise OSError("Синтетическая ошибка записи PNG")
        return original(image, target, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", save)
    with pytest.raises(OSError, match="Синтетическая ошибка"):
        convert(source, tmp_path / "output", limits(), lambda kind, data: events.append(kind))
    assert "source_discovered" in events and "page_ready" not in events
