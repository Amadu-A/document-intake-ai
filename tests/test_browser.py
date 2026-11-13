# tests/test_browser.py
"""Настоящий Chromium и HTTP UI; внешние OCR/VLM явно заменены test doubles."""

import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from app.main import create_app
from app.worker import process
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
            timeout_graceful_shutdown=3,
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
            organization = page.locator("[data-organization]")
            await expect(organization).to_have_value("")
            await organization.fill("ООО «Браузерный тест»")
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
            assert stored.organization_hint == "ООО «Браузерный тест»"
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
            await expect(organization).to_have_value("ООО «Браузерный тест»")
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
        if "browser" in locals():
            await browser.close()
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving


async def test_browser_manual_first_keyboard_exclusion_and_review(container, unused_tcp_port):
    """Ручной-first, серверный ACK, клавиатура, исключение и review проходят настоящий HTTP UI."""
    base = f"http://127.0.0.1:{unused_tcp_port}"
    container.settings.app_public_base_url = base
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(container),
            host="127.0.0.1",
            port=unused_tcp_port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
    )
    serving = asyncio.create_task(server.serve())

    async def deliver():
        """Использует production worker claim/prepare/execute; AMQP доставка заменена тестовой."""
        while True:
            for job_id in await container.database.pending():
                await process(container, job_id)
            await asyncio.sleep(0.05)

    delivery = asyncio.create_task(deliver())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=playwright.chromium.executable_path
            )
            page = await browser.new_page(viewport={"width": 1680, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.locator("[data-files]").set_input_files(
                {"name": "manual.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(5)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled()
            assert not container.vision.calls
            await page.get_by_role("button", name="Просмотреть страницу 2", exact=True).click()
            await page.locator('[data-boundary-action="split"]').click()
            await expect(page.locator("[data-skip-segmentation]")).to_be_checked()
            await expect(page.locator("[data-auto-split]")).not_to_be_checked()
            await expect(page.locator("[data-boundary-status]")).to_contain_text("2 диапазонов")
            handle = page.get_by_role("button", name="Конец документа 1", exact=True)
            await handle.focus()
            await page.keyboard.press("ArrowRight")
            await expect(page.locator("[data-confirm-plan]")).to_be_disabled()
            await page.keyboard.press("Escape")
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled()
            await handle.focus()
            await page.keyboard.press("ArrowRight")
            await page.keyboard.press("Enter")
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                "Сохранено на сервере"
            )
            await expect(
                page.locator("[data-documents] article").first.locator("button")
            ).to_have_count(3)
            handle_box = await page.get_by_role(
                "button", name="Конец документа 1", exact=True
            ).bounding_box()
            x, y = (
                handle_box["x"] + handle_box["width"] / 2,
                handle_box["y"] + handle_box["height"] / 2,
            )
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x + 108, y, steps=5)
            await page.mouse.up()
            await expect(
                page.locator("[data-documents] article").first.locator("button")
            ).to_have_count(4)
            await page.locator('[data-boundary-action="undo"]').click()
            await expect(
                page.locator("[data-documents] article").first.locator("button")
            ).to_have_count(3)
            await page.locator('[data-boundary-action="merge"]').click()
            await expect(page.locator("[data-documents] article")).to_have_count(1)
            await page.locator('[data-boundary-action="undo"]').click()
            await expect(page.locator("[data-documents] article")).to_have_count(2)
            await page.get_by_role("button", name="Просмотреть страницу 5", exact=True).click()
            page.once("dialog", lambda dialog: dialog.accept())
            await page.locator('[data-boundary-action="exclude"]').click()
            await expect(page.locator("[data-page-counts]")).to_contain_text("исключено 1")
            await page.locator('[data-boundary-action="create"]').click()
            await expect(page.locator("[data-page-counts]")).to_contain_text("исключено 0")
            await expect(page.locator("[data-documents] article")).to_have_count(3)
            await page.locator('[data-boundary-action="undo"]').click()
            await expect(page.locator("[data-page-counts]")).to_contain_text("исключено 1")
            page.once("dialog", lambda dialog: dialog.accept())
            await page.locator('[data-boundary-action="reset"]').click()
            await expect(page.locator("[data-documents] article")).to_have_count(1)
            await page.locator('[data-boundary-action="undo"]').click()
            await expect(page.locator("[data-page-counts]")).to_contain_text("исключено 1")
            job_id = await page.evaluate("sessionStorage.getItem('intake_job')")
            job = await container.database.get(job_id)
            assert [len(d.pages) for d in job.draft_plan.documents] == [3, 1]
            await page.reload()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled()
            await expect(page.locator("[data-page-counts]")).to_contain_text("исключено 1")
            await page.locator("[data-confirm-plan]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            assert all(call["schema"]["title"] != "Plan" for call in container.vision.calls)
            assert (await container.database.get(job_id)).plan_locked
            await page.screenshot(path="tmp/browser-boundary-manual.png", full_page=True)
            async with page.expect_navigation(wait_until="load"):
                await page.locator("[data-new-analysis]").click()
            await page.locator("[data-auto-split]").uncheck()
            await page.locator("[data-files]").set_input_files(
                {"name": "review.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(3)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled()
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-export]")).to_be_disabled()
            await expect(page.locator("[data-validation]")).to_contain_text("Черновик состава")
            await page.screenshot(path="tmp/browser-boundary-review.png", full_page=True)
            await page.locator("[data-confirm-plan]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            async with page.expect_navigation(wait_until="load"):
                await page.locator("[data-new-analysis]").click()
            await page.locator("[data-skip-segmentation]").check()
            await expect(page.locator("[data-auto-split]")).to_be_disabled()
            segmentation_calls = len(
                [call for call in container.vision.calls if call["schema"]["title"] == "Plan"]
            )
            await page.locator("[data-files]").set_input_files(
                {"name": "skip.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(2)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled()
            await page.screenshot(path="tmp/browser-boundary-skip.png", full_page=True)
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            assert (
                len([call for call in container.vision.calls if call["schema"]["title"] == "Plan"])
                == segmentation_calls
            )
            async with page.expect_navigation(wait_until="load"):
                await page.locator("[data-new-analysis]").click()
            await page.locator("[data-files]").set_input_files(
                {"name": "thousand.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(1000)}
            )
            await expect(page.locator("[data-page-counts]")).to_contain_text("/1000", timeout=30000)
            assert await page.locator(".boundary__page").count() <= 24
            assert await page.locator(".viewer__thumbnail").count() <= 11
            await page.locator("[data-page-jump]").fill("1000")
            await page.locator("[data-page-jump]").press("Tab")
            await expect(page.locator("[data-page-label]")).to_contain_text("Страница 1000 из 1000")
            await expect(
                page.get_by_role("button", name="Просмотреть страницу 1000", exact=True)
            ).to_be_visible()
            await page.wait_for_timeout(1600)
            await expect(page.locator("[data-page-label]")).to_contain_text("Страница 1000 из 1000")
            assert await page.locator("[data-boundary-scroll]").evaluate(
                "element => element.scrollLeft > 0"
            )
            assert await page.locator(".boundary__page").count() <= 24
            await page.locator("[data-cancel]").click()
            assert not errors, errors
            await browser.close()
    finally:
        if "browser" in locals():
            await browser.close()
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving
