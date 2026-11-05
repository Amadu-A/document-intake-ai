# tests/test_browser.py
"""Настоящий Chromium и HTTP UI; внешние OCR/VLM явно заменены test doubles."""

import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from app.main import create_app
from tests.support import pdf_bytes

pytestmark = pytest.mark.browser


async def test_real_browser_prompts_upload_sse_export_refresh(container, unused_tcp_port):
    """Проверяет модалку, пачку, страницы, SSE, экспорт и refresh в Chromium."""
    base = f"http://127.0.0.1:{unused_tcp_port}"
    container.settings.app_public_base_url = base
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(container),
            host="127.0.0.1",
            port=unused_tcp_port,
            log_level="warning",
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve())

    async def deliver():
        """Заменяет только доставку AMQP, сохраняя настоящие HTTP, pipeline, storage и PDF."""
        while True:
            for job_id in await container.database.pending():
                job = await container.database.claim(job_id)
                if job:
                    await container.analysis.execute(job)
            await asyncio.sleep(0.1)

    delivery = asyncio.create_task(deliver())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=playwright.chromium.executable_path
            )
            page = await browser.new_page(viewport={"width": 1680, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.locator("[data-prompts-open]").click()
            editor = page.locator("[data-prompt-text]")
            await expect(page.locator("[data-prompts-dialog]")).to_be_visible()
            await expect(editor).not_to_have_value("")
            original = await editor.input_value()
            await editor.fill(original + "\nСинтетическая правка вкладки 1")
            await page.locator('[data-prompt-tab="extraction"]').click()
            second = await editor.input_value()
            await editor.fill(second + "\nСинтетическая правка вкладки 2")
            await page.locator('[data-prompt-tab="segmentation"]').click()
            await expect(editor).to_have_value(original + "\nСинтетическая правка вкладки 1")
            await page.locator("[data-prompts-restore]").click()
            await expect(editor).to_have_value(original)
            await page.locator("[data-prompts-save]").click()
            await expect(page.locator("[data-prompt-status]")).to_contain_text(
                "Оба промпта сохранены"
            )
            await page.keyboard.press("Escape")
            await page.locator("[data-files]").set_input_files(
                [
                    {
                        "name": "synthetic-a.pdf",
                        "mimeType": "application/pdf",
                        "buffer": pdf_bytes(2),
                    },
                    {
                        "name": "synthetic-b.pdf",
                        "mimeType": "application/pdf",
                        "buffer": pdf_bytes(1),
                    },
                ]
            )
            await expect(page.locator("[data-start]")).to_be_enabled()
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-validation]")).to_contain_text("Проверенный результат")
            await page.locator("[data-next]").click()
            await expect(page.locator("[data-page-label]")).to_contain_text("Страница 2 из 3")
            await page.locator("[data-next]").click()
            await expect(page.locator("[data-page-role]")).to_contain_text("synthetic-b.pdf")
            job_id = await page.evaluate("sessionStorage.getItem('intake_job')")
            stored = await container.database.get(job_id)
            assert stored.sources[1].file_id in await page.locator("[data-original]").get_attribute(
                "href"
            )
            await page.locator('[data-result-tab="facts"]').click()
            await expect(page.locator("[data-facts]")).to_contain_text("Тестовое письмо")
            async with page.expect_download() as download:
                await page.locator("[data-export]").click()
            assert (await download.value).suggested_filename.endswith(".json")
            await page.reload()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=10000)
            await page.locator("[data-prompts-open]").click()
            await page.locator('[data-prompt-tab="extraction"]').click()
            await expect(editor).to_have_value(second + "\nСинтетическая правка вкладки 2")
            await page.screenshot(path="tmp/browser-prompts.png", full_page=True)
            await page.keyboard.press("Escape")
            await asyncio.to_thread(Path("tmp").mkdir, exist_ok=True)
            await page.screenshot(path="tmp/browser-desktop.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path="tmp/browser-mobile.png", full_page=True)
            await page.goto(base + "/docs")
            await expect(page.locator(".swagger-ui .info .title")).to_contain_text(
                "Document Intake AI"
            )
            assert not errors, errors
            await browser.close()
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving
