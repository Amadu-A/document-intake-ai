# tests/test_preview_browser.py
"""Chromium: 501 страница не вызывает HTTP-шторм; fallback остаётся редактируемым черновиком."""

import asyncio
from collections import Counter
from contextlib import suppress
from urllib.parse import urlsplit

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from app.main import create_app
from app.worker import process
from tests.support import pdf_bytes

pytestmark = pytest.mark.browser


async def test_browser_501_preview_cache_and_page_fallback(container, unused_tcp_port):
    """Проверяет настоящий HTTP, RAM-кеш, последнюю страницу и ручную правку после отказа VLM."""
    base = f"http://127.0.0.1:{unused_tcp_port}"
    container.settings.app_public_base_url = base
    container.settings.rate_limit_per_minute = 120
    requests, rejected, errors = Counter(), [], []
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
        """Подменяет только AMQP-доставку; нормализация/worker/API остаются настоящими."""
        while True:
            for identifier in await container.database.pending():
                await process(container, identifier)
            await asyncio.sleep(0.05)

    async def invalid(prompt, context, pages, schema, emit):
        """Ошибочный VLM-план не может стать завершённым распознаванием."""
        return {"documents": [], "unassigned_pages": [], "warnings": []}

    delivery = asyncio.create_task(deliver())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1680, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.update([urlsplit(request.url).path]))
            page.on(
                "response",
                lambda response: (
                    rejected.append(response.status)
                    if response.status == 429 and "/api/v1/" in response.url
                    else None
                ),
            )
            await page.goto(base)
            retries = []

            async def throttled(route):
                """Повторяется только чтение; синтетический 429 не затрагивает backend budgets."""
                retries.append(1)
                await route.fulfill(
                    status=429 if len(retries) == 1 else 200,
                    headers={"Retry-After": "0", "Content-Type": "application/json"},
                    body='{"ok":true}',
                )

            await page.route("**/synthetic-throttle", throttled)
            assert await page.evaluate(
                "async () => { const {api} = await import('/static/js/api.js'); "
                "return (await api('/synthetic-throttle')).ok; }"
            )
            assert len(retries) == 2
            rejected.clear()
            await page.unroute("**/synthetic-throttle", throttled)

            async def waiting(route):
                """Длинная пауза должна прерваться AbortSignal без повторного HTTP-запроса."""
                await route.fulfill(status=429, headers={"Retry-After": "60"}, body="{}")

            await page.route("**/synthetic-abort", waiting)
            assert await page.evaluate(
                "async () => { const {request} = await import('/static/js/api.js'); "
                "const controller = new AbortController(); "
                "const work = request('/synthetic-abort', {signal:controller.signal}); "
                "setTimeout(() => controller.abort(), 100); "
                "try { await work; return false; } "
                "catch (error) {return error.name === 'AbortError';} }"
            )
            rejected.clear()
            await page.unroute("**/synthetic-abort", waiting)
            await page.locator("[data-files]").set_input_files(
                {"name": "501.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(501)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-page-image]")).to_be_visible(timeout=10000)
            await page.wait_for_function(
                "() => document.querySelector('[data-page-image]').naturalWidth > 0"
            )
            await page.wait_for_timeout(6500)
            image_requests = sum(
                count
                for path, count in requests.items()
                if path.endswith(("/preview", "/thumbnail"))
            )
            assert image_requests < 60
            assert (
                max(
                    count
                    for path, count in requests.items()
                    if path.endswith(("/preview", "/thumbnail"))
                )
                <= 2
            )
            await page.locator("[data-page-jump]").fill("501")
            await page.locator("[data-page-jump]").press("Tab")
            await expect(page.locator("[data-page-label]")).to_contain_text("Страница 501 из 501")
            await page.wait_for_function(
                "() => document.querySelector('[data-page-image]').alt.includes('501') && "
                "document.querySelector('[data-page-image]').naturalWidth > 0"
            )
            assert await page.locator(".boundary__page").count() <= 24
            container.vision.request = invalid
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-notice]")).to_contain_text("черновик по одному листу")
            await expect(page.locator("[data-export]")).to_be_disabled()
            await expect(page.locator("[data-boundary-status]")).to_contain_text("501 диапазонов")
            await page.locator("[data-page-jump]").fill("1")
            await page.locator("[data-page-jump]").press("Tab")
            await page.locator('[data-boundary-action="merge"]').click()
            await expect(page.locator("[data-boundary-status]")).to_contain_text("500 диапазонов")
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                "Сохранено на сервере"
            )
            await page.reload()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=10000)
            await expect(page.locator("[data-boundary-status]")).to_contain_text("500 диапазонов")
            await page.get_by_role("button", name="Просмотреть страницу 1", exact=True).click()
            await page.get_by_role("button", name="Просмотреть страницу 3", exact=True).click(
                modifiers=["Control"]
            )
            await expect(page.locator("[data-boundary-selection]")).to_contain_text("2")
            await page.locator('[data-boundary-action="create"]').click()
            await expect(
                page.locator("[data-documents] article").first.locator("button").nth(1)
            ).to_have_text("3")
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                "Сохранено на сервере"
            )
            identifier = await page.evaluate("sessionStorage.getItem('intake_job')")
            stored = await container.database.get(identifier)
            assert [[p.page_number for p in d.pages] for d in stored.draft_plan.documents][:2] == [
                [1, 3],
                [2],
            ]
            await page.get_by_role("button", name="Просмотреть страницу 1", exact=True).click()
            await page.locator('[data-boundary-action="split"]').click()
            await expect(page.locator("[data-boundary-status]")).to_contain_text("501 диапазонов")
            stored = await container.database.get(identifier)
            assert [[p.page_number for p in d.pages] for d in stored.draft_plan.documents][:3] == [
                [1],
                [2],
                [3],
            ]
            assert not errors and not rejected
            assert await page.locator("[data-documents]").evaluate(
                "element => element.scrollHeight > element.clientHeight"
            )
            assert await page.evaluate("() => document.documentElement.scrollHeight < 2500")
            await page.screenshot(path="tmp/browser-501-fallback.png", full_page=True)
            await page.locator("[data-cancel]").click()
            await browser.close()
    finally:
        if "browser" in locals():
            await browser.close()
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving
