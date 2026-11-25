# tests/test_delivery_browser.py
"""Chromium показывает ожидание отдельно от подготовки и не рисует активность после ошибки."""

import pytest
from playwright.async_api import async_playwright, expect

from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def test_waiting_preview_and_queue_timeout_are_honest(panel_server):
    """Снимки до claim, после claim и после queue timeout дают разные доступные состояния UI."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(panel_server)
            snapshot = {
                "status": "uploaded",
                "stage": "upload",
                "preview_complete": False,
                "pages_ready": 0,
                "pages_total": 0,
            }

            async def render():
                """Проверяет DOM-модуль; synthetic snapshot не обозначается реальным OCR."""
                await page.evaluate(
                    "async data => { const {renderProcessing} = await "
                    "import('/static/js/features/processing.js'); "
                    "renderProcessing(data); }",
                    snapshot,
                )

            await render()
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Ожидает начала обработки"
            )
            await expect(page.locator("[data-progress]")).to_be_hidden()
            assert await page.locator(".processing__step--active").count() == 0
            snapshot.update(stage="preparing_preview", work_stage="preparing_preview")
            await render()
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Подготавливаем страницы"
            )
            snapshot.update(
                status="failed",
                stage="failed",
                work_stage=None,
                error={"code": "queue_timeout", "message": "Превышено время ожидания"},
            )
            await render()
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Превышено время ожидания"
            )
            assert await page.locator(".processing__step--active").count() == 0
        finally:
            await browser.close()
