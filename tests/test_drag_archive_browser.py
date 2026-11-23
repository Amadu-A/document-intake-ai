# tests/test_drag_archive_browser.py
"""Chromium: настоящий мышиный перенос с виртуализацией и архив после утверждения/reload."""

import asyncio
import re
import zipfile

import pymupdf
import pytest
from playwright.async_api import async_playwright, expect

from tests.support import pdf_bytes
from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def read_plan(page):
    """Читает сохранённую ревизию из той же cookie-сессии через штатный JS API."""
    return await page.evaluate(
        "async () => (await import('/static/js/api.js')).api('/api/v1/jobs/' + "
        "sessionStorage.getItem('intake_job') + '/segmentation')"
    )


async def test_mouse_drop_autoscroll_cancel_and_archive_use_server_approved_order(
    container, panel_server
):
    """Лист 2 переносится за 20-й, сохраняется; reload не теряет ни порядок, ни ожидаемый ZIP."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = container.analysis.documents.run

    async def paused(*args, **kwargs):
        """Позволяет проверить блокировку плана и reload во время классификации."""
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    container.analysis.documents.run = paused
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            await page.locator('[data-segmentation-mode][value="source"]').check()
            await page.locator("[data-files]").set_input_files(
                {"name": "drag.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(30)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await page.locator('[data-boundary-action="manual"]').click()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=15000)
            before = await read_plan(page)
            source = page.locator('[data-page-key$=":2"] [data-page-drag]')
            box = await source.bounding_box()
            await page.mouse.move(box["x"] + 40, box["y"] + 50)
            await page.mouse.down()
            await page.mouse.move(box["x"] + 155, box["y"] + 50, steps=8)
            await expect(page.locator("[data-confirm-plan]")).to_be_disabled()
            await page.keyboard.press("Escape")
            await page.mouse.up()
            assert (await read_plan(page))["plan_revision"] == before["plan_revision"]
            box = await source.bounding_box()
            scroll = await page.locator("[data-boundary-scroll]").bounding_box()
            await page.mouse.move(box["x"] + 40, box["y"] + 50)
            await page.mouse.down()
            await page.mouse.move(scroll["x"] + scroll["width"] - 16, box["y"] + 50, steps=15)
            await page.wait_for_function(
                "() => document.querySelector('[data-boundary-scroll]').scrollLeft > 1300",
                timeout=10000,
            )
            target = page.locator('[data-page-key$=":20"]')
            target_box = await target.bounding_box()
            assert target_box
            await page.mouse.move(target_box["x"] + 78, target_box["y"] + 90, steps=5)
            await expect(target).to_have_class(re.compile("drop-after"))
            await page.mouse.up()
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                f"редакция {before['plan_revision'] + 1}", timeout=15000
            )
            saved = await read_plan(page)
            order = [p["page_number"] for p in saved["draft_plan"]["page_order"]]
            assert order == [1, *range(3, 21), 2, *range(21, 31)]
            await page.reload()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=15000)
            assert (await read_plan(page))["draft_plan"]["page_order"] == saved["draft_plan"][
                "page_order"
            ]
            cut = page.locator("[data-cut-download]")
            await expect(cut).to_be_enabled()
            await cut.click()
            await asyncio.wait_for(entered.wait(), 30)
            await expect(page.locator('[data-boundary-action="page-move"]')).to_be_disabled()
            assert (await read_plan(page))["approved_plan"]["page_order"] == saved["draft_plan"][
                "page_order"
            ]
            await page.reload()
            await expect(cut).to_contain_text("Ожидаем документы", timeout=15000)
            async with page.expect_download(timeout=60000) as event:
                release.set()
            download = await event.value
            assert download.suggested_filename == "documents.zip"
            with zipfile.ZipFile(await download.path()) as archive:
                assert archive.namelist() == ["doc_1/Письмо.pdf"]
                with pymupdf.open(stream=archive.read(archive.namelist()[0])) as pdf:
                    assert [p.get_text().strip() for p in pdf] == [
                        f"SYNTHETIC PAGE {n}" for n in order
                    ]
            await expect(cut).to_be_enabled()
            await expect(cut).to_have_text("Разрезать и скачать")
            await page.screenshot(path="tmp/drag-archive-completed.png", full_page=True)
            assert not errors
        finally:
            release.set()
            await browser.close()


async def test_multiselection_drop_joins_target_group_and_archive_retry(panel_server):
    """Перенос меняет диапазон; архив можно повторить без переутверждения."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        try:
            await page.goto(panel_server)
            await page.locator('[data-segmentation-mode][value="source"]').check()
            await page.locator("[data-files]").set_input_files(
                {"name": "many.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(8)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await page.locator("[data-page-jump]").fill("4")
            await page.locator("[data-page-jump]").press("Tab")
            await page.locator('[data-boundary-action="split"]').click()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=15000)
            await page.locator('[data-page-key$=":2"] [data-page-drag]').click()
            await page.locator('[data-page-key$=":4"] [data-page-drag]').click(
                modifiers=["Control"]
            )
            saved = await read_plan(page)
            a = await page.locator('[data-page-key$=":2"] [data-page-drag]').bounding_box()
            b = await page.locator('[data-page-key$=":7"]').bounding_box()
            await page.mouse.move(a["x"] + 40, a["y"] + 50)
            await page.mouse.down()
            await page.mouse.move(b["x"] + 20, b["y"] + 90, steps=15)
            await page.mouse.up()
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                f"редакция {saved['plan_revision'] + 1}", timeout=15000
            )
            current = await read_plan(page)
            assert [
                [p["page_number"] for p in d["pages"]] for d in current["draft_plan"]["documents"]
            ] == [[1, 3], [5, 6, 2, 4, 7, 8]]
            await page.route(
                "**/archive",
                lambda route: route.fulfill(
                    status=503, json={"error": {"message": "Архив временно недоступен"}}
                ),
                times=1,
            )
            await page.locator("[data-cut-download]").click()
            await expect(page.locator("[data-archive-error]")).to_have_text(
                "Архив временно недоступен", timeout=60000
            )
            await expect(page.locator("[data-cut-download]")).to_be_enabled()
            async with page.expect_download(timeout=30000) as event:
                await page.locator("[data-cut-download]").click()
            download = await event.value
            with zipfile.ZipFile(await download.path()) as archive:
                assert archive.namelist() == ["doc_1/Письмо.pdf", "doc_2/Письмо (2).pdf"]
            assert (await read_plan(page))["approved_revision"] == current["plan_revision"]
            page.on("dialog", lambda dialog: dialog.accept())
            await page.locator("[data-new-analysis]").click()
            await expect(page.locator("[data-cut-download]")).to_be_hidden()
            assert await page.evaluate("sessionStorage.getItem('intake_archive_job')") is None
        finally:
            await browser.close()
