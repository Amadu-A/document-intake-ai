# tests/test_processing_browser.py
"""Chromium: радиорежимы, фактическая панель, reload/polling и 172 перехода."""

import asyncio
from contextlib import suppress
from pathlib import Path

import pytest
import pytest_asyncio
import uvicorn
from playwright.async_api import async_playwright, expect

from app.main import create_app
from app.worker import process
from tests.support import facts, pdf_bytes
from tests.test_document_processing import classification

pytestmark = pytest.mark.browser


@pytest_asyncio.fixture
async def panel_server(container, unused_tcp_port):
    """Поднимает настоящий HTTP/worker; OCR/VLM остаются явно синтетическими."""
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
        """Заменяет только доставку AMQP, сохраняя durable checkpoint и fencing."""
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
        yield base
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError):
            await delivery
        server.should_exit = True
        await serving


async def test_panel_mixed_result_keeps_export_and_explicit_block_statuses(container, panel_server):
    """Неписьмо извлечено; отказ другого блока виден после reload и не теряет PDF."""
    nonletter = facts()
    nonletter["classification"] = classification(False)["classification"]
    container.vision.answers.extend(
        [
            {"solution": False},
            {"solution": False},
            classification(False),
            nonletter,
            classification(),
            *[{} for _ in range(container.settings.model_attempts)],
            classification(),
            facts(),
        ]
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1680, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            await page.locator('[data-segmentation-mode][value="auto"]').check()
            await page.locator("[data-files]").set_input_files(
                {"name": "mixed.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(3)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=60000)
            await page.reload()
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Обработка завершена", timeout=15000
            )
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Распознано документов: 2. Требуют проверки: 1. PDF всех блоков готовы."
            )
            await expect(page.locator("[data-validation]")).to_have_text(
                "JSON 1.0 · есть блоки без распознанных реквизитов · см. предупреждения"
            )
            await expect(page.locator("[data-facts]")).not_to_contain_text(
                "шаблон реквизитов пока не реализован"
            )
            await expect(page.locator("[data-facts]")).to_contain_text(
                "Реквизиты не подтверждены · PDF сохранён"
            )
            await expect(page.locator("[data-facts]")).to_contain_text("Процессуальные документы")
            links = page.locator("[data-facts] a")
            assert await links.count() == 3
            for link in await links.all():
                response = await page.request.get(await link.get_attribute("href"))
                assert response.status == 200
                assert (await response.body()).startswith(b"%PDF")
            assert "processing--error" not in await page.locator("[data-processing]").get_attribute(
                "class"
            )
            assert not errors
            await asyncio.to_thread(Path("tmp").mkdir, exist_ok=True)
            await page.screenshot(path="tmp/document-mixed-completed.png", full_page=True)
        finally:
            await browser.close()


async def test_panel_default_confirmation_real_counters_and_polling(container, panel_server):
    """OCR и реквизиты показывают собственные счётчики после reload без SSE."""
    ocr_entered, ocr_release = asyncio.Event(), asyncio.Event()
    facts_entered, facts_release = asyncio.Event(), asyncio.Event()
    original_ocr, original_extract = container.analysis.ocr.recognize, container.analysis._extract
    processed = 0

    async def ocr(pages, emit):
        """Явно завершает одну OCR-страницу при трёх готовых превью."""
        await emit("ocr_progress", {"completed": 1, "total": len(pages)})
        ocr_entered.set()
        await ocr_release.wait()
        return await original_ocr(pages, emit)

    async def extract(*args):
        """Пауза второго документа оставляет в БД один завершённый документ из трёх."""
        nonlocal processed
        if processed == 1:
            facts_entered.set()
            await facts_release.wait()
        result = await original_extract(*args)
        processed += 1
        return result

    container.analysis.ocr.recognize, container.analysis._extract = ocr, extract
    container.vision.answers.extend([{"solution": False}, {"solution": False}])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1680, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            await page.goto(panel_server)
            modes = page.locator("[data-segmentation-mode]")
            assert await modes.count() == 3
            await expect(page.locator('[data-segmentation-mode][value="review"]')).to_be_checked()
            for mode in ("auto", "source", "review"):
                await page.locator(f'[data-segmentation-mode][value="{mode}"]').check()
                assert await page.locator("[data-segmentation-mode]:checked").count() == 1
            flags = await page.evaluate(
                "async () => { const {modeFlags} = await import('/static/js/features/modes.js'); "
                "return ['review','auto','source'].map(modeFlags); }"
            )
            assert flags == [
                {"auto_split": False, "skip_segmentation": False},
                {"auto_split": True, "skip_segmentation": False},
                {"auto_split": False, "skip_segmentation": True},
            ]
            await page.locator("[data-files]").set_input_files(
                {"name": "three.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(3)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Документы готовы к анализу"
            )
            await expect(page.locator("[data-structure-status]")).to_contain_text(
                "Границы ещё не определены"
            )
            await expect(page.locator("[data-export]")).to_be_hidden()
            await page.locator("[data-start]").click()
            await asyncio.wait_for(ocr_entered.wait(), 10)
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Распознано 1 из 3 страниц"
            )

            async def unavailable(route):
                """Разрывает SSE; snapshot и резервный polling остаются настоящими."""
                await route.abort()

            await page.route("**/events?**", unavailable)
            await page.reload()
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Распознано 1 из 3 страниц"
            )
            assert await page.locator("[data-progress]").get_attribute("max") == "3"
            assert await page.locator("[data-progress]").get_attribute("value") == "1"
            ocr_release.set()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Требуется ваша проверка"
            )
            await expect(page.locator("[data-start]")).to_be_hidden()
            assert processed == 0
            await page.locator("[data-confirm-plan]").click()
            await asyncio.wait_for(facts_entered.wait(), 10)
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Обработано 1 из 3 документов", timeout=15000
            )
            await page.reload()
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Обработано 1 из 3 документов"
            )
            facts_release.set()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Обработка завершена"
            )
            await expect(page.locator('[data-processing-step="confirmation"]')).to_contain_text("✓")
            assert not errors
            await asyncio.to_thread(Path("tmp").mkdir, exist_ok=True)
            await page.screenshot(path="tmp/processing-completed.png", full_page=True)
            page.once("dialog", lambda dialog: dialog.accept())
            await page.locator("[data-new-analysis]").click()
            await page.locator('[data-segmentation-mode][value="auto"]').check()
            await page.locator("[data-files]").set_input_files(
                {"name": "auto.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes()}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await page.locator("[data-start]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=30000)
            await expect(page.locator('[data-processing-step="confirmation"]')).to_contain_text(
                "пропущено"
            )
            await expect(page.locator("[data-confirm-plan]")).to_be_hidden()
        finally:
            ocr_release.set()
            facts_release.set()
            await browser.close()


async def test_panel_173_pages_172_comparisons_partial_and_new_packet(container, panel_server):
    """173 синтетических страницы дают 172 перехода; неизвестный остаётся проверяемым."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def compare(prompt, context, pages, schema, emit):
        """Пауза сохраняет возможность проверить восстановление активного этапа."""
        assert schema["title"] == "PairAnswer" and pages == []
        entered.set()
        await release.wait()
        return {"solution": "true" if context["right_page"]["page_number"] == 2 else True}

    container.analysis.pairwise.vision.request = compare
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1680, "height": 1100})
        try:
            await page.goto(panel_server)
            await page.locator("[data-files]").set_input_files(
                {"name": "173.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(173)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=120000)
            await expect(page.locator("[data-structure-status]")).to_contain_text(
                "173 страниц. Границы ещё не определены"
            )
            await expect(page.locator("[data-count-documents]")).to_have_text("—")
            await page.locator("[data-start]").click()
            await asyncio.wait_for(entered.wait(), 20)
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Проверено 0 из 172 переходов"
            )
            await page.reload()
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "Проверено 0 из 172 переходов"
            )
            release.set()
            await expect(page.locator("[data-confirm-plan]")).to_be_enabled(timeout=120000)
            await expect(page.locator("[data-processing-description]")).to_contain_text(
                "1 границ требуют проверки"
            )
            await expect(page.locator("[data-export]")).to_be_hidden()
            identifier = await page.evaluate("sessionStorage.getItem('intake_job')")
            job = await container.database.get(identifier)
            assert len(job.comparisons) == 172
            assert sum(p.status == "completed" for p in job.comparisons) == 171
            await page.screenshot(path="tmp/processing-173-partial.png", full_page=True)
            page.once("dialog", lambda dialog: dialog.accept())
            await page.locator("[data-new-analysis]").click()
            await expect(page.locator("[data-processing-title]")).to_have_text("Выберите документы")
            await expect(page.locator('[data-segmentation-mode][value="review"]')).to_be_checked()
            await expect(page.locator("[data-progress]")).to_be_hidden()
            assert await page.evaluate("sessionStorage.getItem('intake_job')") is None
        finally:
            release.set()
            await browser.close()


async def test_panel_unknown_progress_failure_and_skipped_stages(panel_server):
    """Проекция неизвестного прогресса, ошибки и пропусков не создаёт ложных процентов/галочек."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(panel_server)
            data = {
                "status": "running",
                "stage": "docling",
                "work_stage": "docling",
                "progress": 95,
                "pages_ready": 173,
                "pages_total": 173,
                "preview_complete": True,
                "mode": {"auto_split": True, "skip_segmentation": False},
            }

            async def render(snapshot, **options):
                """Проверяет DOM-проекцию отдельного snapshot, без выдачи её за реальный OCR."""
                await page.evaluate(
                    "async ({snapshot,options}) => { const {renderProcessing} = "
                    "await import('/static/js/features/processing.js'); "
                    "renderProcessing(snapshot,options); }",
                    {"snapshot": snapshot, "options": options},
                )

            await render(data)
            await expect(page.locator("[data-progress]")).to_be_hidden()
            assert await page.locator("[data-progress]").get_attribute("value") is None
            await expect(page.locator("[data-processing-title]")).to_have_text(
                "Распознаём текст документов"
            )
            await render(data, cancelling=True)
            await expect(page.locator("[data-processing-title]")).to_have_text("Отменяем обработку")
            await expect(page.locator("[data-cancel]")).to_be_disabled()
            await expect(page.locator("[data-new-analysis]")).to_be_enabled()
            data.update(status="failed", stage="failed", error={"message": "OCR недоступен"})
            await render(data)
            await expect(page.locator("[data-processing-description]")).to_have_text(
                "OCR недоступен"
            )
            assert "processing__step--error" in await page.locator(
                '[data-processing-step="ocr"]'
            ).get_attribute("class")
            for control in ("start", "confirm-plan", "cancel", "export"):
                await expect(page.locator(f"[data-{control}]")).to_be_hidden()
            data.update(
                status="running",
                stage="extraction_vlm",
                work_stage="extraction_vlm",
                plan_locked=True,
                mode={"auto_split": False, "skip_segmentation": True},
            )
            await render(data)
            for step in ("boundaries", "confirmation"):
                node = page.locator(f'[data-processing-step="{step}"]')
                await expect(node).to_contain_text("пропущено")
                assert "✓" not in await node.inner_text()
        finally:
            await browser.close()
