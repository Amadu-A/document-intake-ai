# tests/test_conversion_details.py
"""Фрагмент мелких реквизитов строится из исходного PDF, без потери страниц или подмены цифр."""

import hashlib
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app.infrastructure.conversion import convert
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
