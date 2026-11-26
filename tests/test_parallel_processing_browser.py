# tests/test_parallel_processing_browser.py
"""Chromium: фактическая параллельность пакета, точные счётчики и скрытый блок организации."""

import asyncio

import pytest
from playwright.async_api import async_playwright, expect

from tests.test_boundary_api import KEY
from tests.test_extraction_concurrency import ContextVLM, approved, parallel
from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def test_parallel_panel_shows_real_active_documents_and_keeps_downloads(
    client, container, panel_server
):
    """Через настоящий REST/worker получает active=3; после завершения сохраняет ZIP/XLSX."""
    vlm = ContextVLM(gated=True)
    async with parallel(container, 3, vlm):
        identifier = await approved(client, container, 3)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(extra_http_headers=KEY)
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await page.goto(panel_server)
                await expect(page.locator("[data-organization]")).to_be_hidden()
                await page.evaluate("id => sessionStorage.setItem('intake_job', id)", identifier)
                await page.reload()
                await asyncio.wait_for(vlm.three_active.wait(), 10)
                await expect(page.locator("[data-processing-title]")).to_have_text(
                    "Извлекаем реквизиты", timeout=15000
                )
                await expect(page.locator("[data-processing-description]")).to_have_text(
                    "Обработано 0 из 3 документов"
                )
                await expect(page.locator("[data-extraction-active]")).to_have_text(
                    "Одновременно обрабатывается: 3"
                )
                await page.screenshot(path="tmp/extraction-parallel-active.png", full_page=True)
                vlm.release.set()
                await expect(page.locator("[data-registry-download]")).to_be_enabled(timeout=30000)
                await expect(page.locator("[data-extraction-active]")).to_be_hidden()
                await page.reload()
                await expect(page.locator("[data-registry-download]")).to_be_enabled(timeout=15000)
                await expect(page.locator("[data-cut-download]")).to_be_visible()
                assert not errors
                await page.screenshot(path="tmp/extraction-parallel-completed.png", full_page=True)
            finally:
                vlm.release.set()
                await browser.close()


async def test_panel_counter_fifteen_of_one_hundred_twelve(panel_server):
    """Проверяет точное требуемое отображение snapshot, включая параллельную классификацию."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            # Изолируем DOM-проекцию от начальной перерисовки обычного приложения.
            await page.route("**/static/js/app.js", lambda route: route.abort())
            await page.goto(panel_server)
            await page.evaluate("""async () => {
              const {renderProcessing} = await import('/static/js/features/processing.js');
              renderProcessing({status:'running', preview_complete:true,
                work_stage:'extraction_vlm', extraction_active:3,
                work_progress:{extraction:{completed:15,total:112}},
                document_processing:{x:{status:'processing',stage:'classification'}}});
            }""")
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Извлекаем реквизиты"
            )
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Обработано 15 из 112 документов"
            )
            await expect(page.locator("[data-extraction-active]")).to_have_text(
                "Одновременно обрабатывается: 3"
            )
            await page.screenshot(path="tmp/extraction-parallel-counter.png", full_page=True)
        finally:
            await browser.close()
