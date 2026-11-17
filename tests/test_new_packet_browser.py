# tests/test_new_packet_browser.py
"""Chromium: отмена, повтор того же файла, поздний ответ и полный сброс без reload."""

import asyncio
from contextlib import suppress

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from app.main import create_app
from app.worker import process
from tests.support import pdf_bytes

pytestmark = pytest.mark.browser


async def test_new_packet_cancels_resets_and_ignores_old_requests(container, unused_tcp_port):
    """Один экран последовательно принимает несколько пакетов, включая тот же PDF."""
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
        """Заменяет только AMQP; API, worker, DB и PDF остаются настоящими."""
        while True:
            for identifier in await container.database.pending():
                await process(container, identifier)
            await asyncio.sleep(0.05)

    delivery = asyncio.create_task(deliver())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1680, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            messages = []

            async def accept_dialog(dialog):
                """Фиксирует предупреждение и явно подтверждает его в тесте."""
                messages.append(dialog.message)
                await dialog.accept()

            page.on("dialog", accept_dialog)
            await page.goto(base)
            await page.evaluate("window.packetResetProbe = 17")
            payload = {"name": "same.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(4)}
            await expect(page.locator("[data-new-analysis]")).to_be_visible()
            await page.locator("[data-files]").set_input_files(payload)
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=20000)
            first = await page.evaluate("sessionStorage.getItem('intake_job')")
            await page.locator("[data-new-analysis]").click()
            await expect(page.locator("[data-files]")).to_be_enabled()
            assert (await container.database.get(first)).status == "cancelled"
            assert await page.evaluate("window.packetResetProbe") == 17
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
            assert await page.locator("[data-files]").input_value() == ""
            assert await page.locator(".boundary__page").count() == 0
            assert await page.locator("[data-page-image]").get_attribute("src") is None
            assert await page.locator("[data-local-pdf]").get_attribute("data") is None
            await expect(page.locator("[data-json]")).to_have_text("Ожидание анализа…")
            # Ctrl+F5 после сброса не восстанавливает предыдущее задание.
            await page.reload()
            await expect(page.locator("[data-files]")).to_be_enabled()
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
            await page.locator("[data-files]").set_input_files(payload)
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=20000)
            second = await page.evaluate("sessionStorage.getItem('intake_job')")
            assert second != first
            entered, stopped = asyncio.Event(), asyncio.Event()

            async def delayed(*args, **kwargs):
                """Живой активный запрос должен прерваться до следующей загрузки."""
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            original = container.vision.request
            container.vision.request = delayed
            await page.locator("[data-start]").click()
            await asyncio.wait_for(entered.wait(), 10)
            await expect(page.locator("[data-new-analysis]")).to_be_visible()
            await page.locator("[data-cancel]").click()
            await expect(page.locator("[data-job-status]")).to_have_text("Отменено")
            await asyncio.wait_for(stopped.wait(), 2)
            await page.locator("[data-new-analysis]").click()
            container.vision.request = original
            # Поздний ответ загрузки предыдущего поколения не меняет новый экран.
            uploaded, release = asyncio.Event(), asyncio.Event()

            async def late_upload(route):
                """Сервер принял оригинал; браузер ещё не получил job_id."""
                response = await route.fetch()
                uploaded.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route("**/api/v1/uploads", late_upload)
            await page.locator("[data-files]").set_input_files(payload)
            await asyncio.wait_for(uploaded.wait(), 10)
            await page.locator("[data-new-analysis]").click()
            release.set()
            await page.wait_for_timeout(750)
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
            await expect(page.locator("[data-count-files]")).to_have_text("0")
            await expect(page.locator("[data-files]")).to_be_enabled()
            await page.unroute("**/api/v1/uploads", late_upload)

            # Отказ cancel не выдаётся за успех; явное отсоединение остаётся доступно.
            await page.locator("[data-files]").set_input_files(payload)
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=20000)
            detached = await page.evaluate("sessionStorage.getItem('intake_job')")

            async def cancel_failure(route):
                """Отклоняет только собственную команду отмены."""
                await route.fulfill(
                    status=503,
                    content_type="application/json",
                    body='{"error":{"message":"Cancel unavailable"}}',
                )

            await page.route(f"**/api/v1/jobs/{detached}/cancel", cancel_failure)
            await page.locator("[data-cancel]").click()
            await expect(page.locator("[data-notice]")).to_contain_text("Отмена не подтверждена")
            await page.locator("[data-new-analysis]").click()
            await expect(page.locator("[data-files]")).to_be_enabled()
            assert any("Сервер не подтвердил отмену" in message for message in messages)
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
            abandoned = await container.database.get(detached)
            assert abandoned.status == "uploaded"
            await page.unroute(f"**/api/v1/jobs/{detached}/cancel", cancel_failure)
            await container.database.cancel(detached, abandoned.owner)

            # После отказа upload тот же drag/drop снова доступен.
            async def reject(route):
                """Имитирует диагностируемый отказ загрузки."""
                await route.fulfill(
                    status=503,
                    content_type="application/json",
                    body='{"error":{"message":"Синтетический отказ"}}',
                )

            await page.route("**/api/v1/uploads", reject)
            await page.locator("[data-files]").set_input_files(payload)
            await expect(page.locator("[data-notice]")).to_contain_text("Синтетический отказ")
            await expect(page.locator("[data-files]")).to_be_enabled()
            await page.unroute("**/api/v1/uploads", reject)
            await page.locator("[data-dropzone]").evaluate(
                "element => { const data = new DataTransfer(); "
                "data.items.add(new File(['synthetic'], 'invalid.pdf', {type:'application/pdf'})); "
                "element.dispatchEvent(new DragEvent('drop', {dataTransfer:data, bubbles:true})); }"
            )
            await expect(page.locator("[data-notice]")).not_to_contain_text("Синтетический отказ")
            await expect(page.locator("[data-files]")).to_be_enabled()
            assert not errors
            await browser.close()
    finally:
        if "browser" in locals():
            await browser.close()
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving
