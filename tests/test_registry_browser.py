# tests/test_registry_browser.py
"""Chromium: REST/XLSX, retry/reload, terminal состояния и отмена позднего скачивания."""

import asyncio
from contextlib import closing, suppress
from datetime import timedelta
from io import BytesIO

import pytest
from openpyxl import load_workbook
from playwright.async_api import async_playwright, expect

from app.domain.jobs import utcnow
from app.infrastructure.database import JobRow
from tests.support import pdf_bytes
from tests.test_extractions_v2 import extracted, selected
from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def prepared(page, base, container):
    """Выполняет настоящий upload/prepare/start; только OCR/VLM ответы явно synthetic."""
    await page.goto(base)
    await page.locator('[data-segmentation-mode][value="source"]').check()
    await page.locator("[data-files]").set_input_files(
        {"name": "registry-synthetic.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(2)}
    )
    await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
    await expect(page.locator("[data-registry-download]")).to_be_disabled()
    await expect(page.locator("[data-registry-hint]")).to_have_text("После извлечения реквизитов")
    identifier = await page.evaluate("sessionStorage.getItem('intake_job')")
    job = await container.database.get(identifier)
    selection = selected("Счёт-фактура")
    value = extracted(
        selection,
        number="0007/А",
        date="2025-11-27",
        ref={"file_id": job.sources[0].file_id, "page_number": 1},
        details={
            "profile": "D03",
            "primary_document_type": "Счёт-фактура",
            "primary_document_role": "tax",
        },
    )
    container.vision.answers.extend([selection.model_dump(mode="json"), value])
    await page.locator("[data-start]").click()
    await expect(page.locator("[data-registry-download]")).to_be_enabled(timeout=30000)
    return identifier


@pytest.mark.parametrize("state", ["completed", "completed_with_warnings"])
async def test_registry_download_retry_reload_and_success_states(container, panel_server, state):
    """UI следует валидному terminal snapshot, повторяет только экспорт, не business pipeline."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            identifier = await prepared(page, panel_server, container)
            if state == "completed":
                # Вариант terminal состояния для UI; не выдаётся за качество реального VLM.
                async with container.database.sessions.begin() as session:
                    row = await session.get(JobRow, identifier)
                    data = dict(row.data)
                    data["status"] = state
                    row.data = data
                    row.status = state
            await page.reload()
            button = page.locator("[data-registry-download]")
            await expect(button).to_be_enabled(timeout=15000)
            assert await page.locator("[data-cut-download]").evaluate(
                "node=>node.nextElementSibling.hasAttribute('data-registry-download')"
            )
            calls = len(container.vision.calls)
            original = container.registry.writer.render

            def failed(result):
                """Моделирует отказ XLSX adapter, сохраняя настоящие route/auth/use-case."""
                raise OSError("controlled export error")

            container.registry.writer.render = failed
            await button.click()
            await expect(page.locator("[data-registry-error]")).to_be_visible()
            await expect(button).to_be_enabled()
            container.registry.writer.render = original
            for _ in range(2):
                async with page.expect_download(timeout=30000) as downloading:
                    await button.click()
                download = await downloading.value
                assert download.suggested_filename == "Реестр документов.xlsx"
                payload = await asyncio.to_thread((await download.path()).read_bytes)
                with closing(load_workbook(BytesIO(payload))) as workbook:
                    assert workbook.sheetnames == ["Реестр"]
                    assert workbook.active.max_column == 14 and workbook.active.max_row == 2
                    assert workbook.active["H2"].value == "0007/А от 27.11.2025"
            assert len(container.vision.calls) == calls and not errors
            await page.screenshot(path=f"tmp/registry-{state}.png", full_page=True)
        finally:
            await browser.close()


@pytest.mark.parametrize("ending", ["new_packet", "expiry"])
async def test_new_packet_or_expiry_aborts_late_registry_download(container, panel_server, ending):
    """Новый пакет/настоящий TTL прерывают поздний XLSX прежнего результата."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        entered, release = asyncio.Event(), asyncio.Event()
        downloads = []
        page.on("download", lambda download: downloads.append(download))
        page.on("dialog", lambda dialog: dialog.accept())
        try:
            identifier = await prepared(page, panel_server, container)
            if ending == "expiry":
                async with container.database.sessions.begin() as session:
                    row = await session.get(JobRow, identifier)
                    data = dict(row.data)
                    data["expires_at"] = (utcnow() + timedelta(seconds=5)).isoformat()
                    row.data = data
                await page.reload()
                await expect(page.locator("[data-registry-download]")).to_be_enabled()

            async def delayed(route):
                """Задерживает ответ настоящего авторизованного GET, не подменяя result."""
                response = await route.fetch()
                entered.set()
                await release.wait()
                # Браузер уже мог прервать request своим AbortController.
                with suppress(Exception):
                    await route.fulfill(response=response)

            await page.route(f"**/api/v1/jobs/{identifier}/registry.xlsx", delayed)
            await page.locator("[data-registry-download]").click()
            await asyncio.wait_for(entered.wait(), 10)
            if ending == "new_packet":
                await page.locator("[data-new-analysis]").click()
                await expect(page.locator("[data-registry-download]")).to_be_hidden()
            else:
                await expect(page.locator("[data-notice]")).to_contain_text(
                    "Срок хранения истёк", timeout=10000
                )
                await expect(page.locator("[data-registry-download]")).to_be_disabled()
            release.set()
            await page.wait_for_timeout(500)
            assert not downloads
            if ending == "new_packet":
                assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
        finally:
            release.set()
            await browser.close()


async def test_local_onec_guide_has_no_application_boot_errors(panel_server):
    """Локальная документация читается без внешнего GitHub и без JS контроллера формы загрузки."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            assert await page.locator('a[href="/integration/1c"]').count() == 1
            await page.goto(panel_server + "/integration/1c")
            await expect(
                page.get_by_role("heading", name="Инструкция программисту 1С")
            ).to_be_visible()
            await expect(page.locator(".documentation__text")).to_contain_text("/registry.xlsx")
            assert not await page.locator('script[src="/static/js/app.js"]').count()
            assert not errors
        finally:
            await browser.close()
