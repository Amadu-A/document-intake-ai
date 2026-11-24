# tests/test_upd_browser.py
"""Chromium на частном PDF; бизнес-ответы controlled, загрузка/подтверждение/ZIP настоящие."""

import zipfile

import pymupdf
import pytest
from playwright.async_api import async_playwright, expect

from tests.test_processing_browser import panel_server as panel_server
from tests.upd_support import ReferenceVision, fingerprint, reference

pytestmark = [pytest.mark.browser, pytest.mark.private]


async def test_private_invoice_browser_confirm_restore_and_download_zip(container, panel_server):
    """Проверяет обе страницы, одну группу и точное имя скачанного PDF после reload UI."""
    paths, (_, v2, expected, _) = reference()
    before = fingerprint(paths)
    vision = ReferenceVision(v2["documents"][0])
    container.analysis.vision = container.analysis.pairwise.vision = vision
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            await page.locator("[data-files]").set_input_files(paths[0].resolve())
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-page-key]")).to_have_count(2)
            await page.reload()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=15000)
            async with page.expect_download(timeout=60000) as downloading:
                await page.locator("[data-cut-download]").click()
            download = await downloading.value
            with zipfile.ZipFile(await download.path()) as archive:
                assert archive.namelist() == [expected["expected_archive_entry"]]
                with pymupdf.open(stream=archive.read(archive.namelist()[0])) as pdf:
                    assert len(pdf) == 2
            assert [c["schema"] for c in vision.calls] == [
                "PairAnswer",
                "DocumentClassification",
                "DetailedExtraction",
            ]
            assert not errors
        finally:
            await browser.close()
    assert fingerprint(paths) == before
