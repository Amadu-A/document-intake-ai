# tests/test_history_profiles_browser.py
"""Chromium: увеличенная загрузка, история без sessionStorage, v2-факты и источники страниц."""

import asyncio
import re

import pytest
from playwright.async_api import async_playwright, expect

from tests.support import pdf_bytes
from tests.test_boundary_api import KEY
from tests.test_extractions_v2 import extracted, selected
from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def test_swagger_authorizes_and_accepts_real_multipart_upload(panel_server):
    """Настоящий Swagger показывает файловый input и выполняет upload с X-API-Key."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(panel_server + "/docs")
            await page.locator(".auth-wrapper .authorize").click()
            await page.locator(".auth-container input").fill(KEY["X-API-Key"])
            await page.locator(".auth-btn-wrapper .authorize").click()
            await page.locator(".auth-btn-wrapper .btn-done").click()
            operation = page.locator(".opblock-post").filter(
                has=page.locator('.opblock-summary-path[data-path="/api/v1/jobs"]')
            )
            await operation.locator(".opblock-summary").click()
            await operation.get_by_role("button", name="Try it out").click()
            await operation.locator('input[type="file"]').set_input_files(
                {"name": "swagger.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(1)}
            )
            async with page.expect_response(
                lambda response: (
                    response.request.method == "POST" and response.url.endswith("/api/v1/jobs")
                )
            ) as pending:
                await operation.get_by_role("button", name="Execute", exact=True).click()
            response = await pending.value
            assert response.status == 202, await response.text()
            assert (await response.json())["job_id"]
        finally:
            await browser.close()


async def test_close_tab_restore_history_and_show_profile_evidence(container, panel_server):
    """Фоновый worker переживает вкладку; новый UI открывает своё задание и корректирует границы."""
    entered, release = asyncio.Event(), asyncio.Event()
    original = container.analysis.ocr.recognize

    async def paused(pages, emit):
        """Даёт закрыть вкладку в реальном OCR-этапе, сохраняя durable server state."""
        entered.set()
        await release.wait()
        return await original(pages, emit)

    container.analysis.ocr.recognize = paused
    container.vision.answers.extend([{"solution": False}, {"solution": False}])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1440, "height": 1100})
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            drop = page.locator("[data-dropzone]")
            assert (await drop.bounding_box())["height"] >= 130
            await drop.dispatch_event("dragenter")
            await expect(drop).to_have_class(re.compile("upload__label--drag"))
            assert "upload--drag" not in await page.locator("section.upload").get_attribute("class")
            await drop.dispatch_event("dragleave")
            await expect(drop).not_to_have_class(re.compile("upload__label--drag"))
            await expect(page.locator('label[for="organization"]')).to_have_text(
                "Организация (необязательно)"
            )
            await page.locator("[data-files]").set_input_files(
                {
                    "name": "history-synthetic.pdf",
                    "mimeType": "application/pdf",
                    "buffer": pdf_bytes(3),
                }
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-processing-status]")).to_have_text("Готово к анализу")
            await page.locator("[data-start]").click()
            await asyncio.wait_for(entered.wait(), 20)
            identifier = await page.evaluate("sessionStorage.getItem('intake_job')")
            await expect(page.locator("[data-processing-status]")).to_have_text("Распознаём")
            await page.close()
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(panel_server)
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
            await page.locator("[data-history-open]").click()
            await page.locator(
                "[data-history-list] button", has_text="history-synthetic.pdf"
            ).click()
            assert await page.evaluate("sessionStorage.getItem('intake_job')") == identifier
            await expect(page.locator("[data-processing-status]")).to_have_text("Распознаём")
            release.set()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=30000)
            await page.locator('[data-boundary-action="merge"]').click()
            await expect(page.locator('[data-processing-step="boundaries"]')).to_contain_text(
                "Скорректировано вручную"
            )
            await expect(page.locator('[data-processing-step="boundaries"]')).not_to_contain_text(
                "пропущено"
            )
            plan = await page.evaluate(
                "async () => (await import('/static/js/api.js')).api('/api/v1/jobs/'+"
                "sessionStorage.getItem('intake_job')+'/segmentation')"
            )
            profile = selected()
            for index, document in enumerate(plan["draft_plan"]["documents"]):
                selection = profile if index == 0 else selected("Договор-заявка", None, "Договоры")
                raw = extracted(
                    selection,
                    ref=document["pages"][0],
                    details=(
                        {
                            "profile": "D03",
                            "primary_document_type": "Счёт на оплату",
                            "primary_document_role": "payment_request",
                            "total_amount": "1200.10",
                            "currency": "RUB",
                        }
                        if index == 0
                        else None
                    ),
                )
                container.vision.answers.extend([selection.model_dump(mode="json"), raw])
            await page.locator("[data-confirm-plan]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-facts]")).to_contain_text(
                "Категория: Закрывающие документы по договору"
            )
            await expect(page.locator("[data-facts]")).to_contain_text("Фактический вид")
            await expect(page.locator("[data-facts]")).to_contain_text("Счёт на оплату")
            await expect(page.locator("[data-facts]")).to_contain_text("1200.10")
            await page.locator('[data-result-tab="facts"]').click()
            await page.locator(
                "[data-facts] button", has_text="Номер · исходная стр. 1"
            ).first.click()
            await expect(page.locator("[data-page-image]")).to_have_attribute(
                "alt", "Страница 1 исходного документа"
            )
            await expect(page.locator("[data-processing-status]")).to_have_text("Результат готов")
            await page.screenshot(path="tmp/profiles-history-desktop.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path="tmp/profiles-history-mobile.png", full_page=True)
            assert not errors
        finally:
            release.set()
            await browser.close()
