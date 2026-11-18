# tests/test_page_order_browser.py
"""Chromium: перенос листов, сохранённый порядок/PDF и повороты большого превью без обрезки."""

import asyncio
from pathlib import Path

import pymupdf
import pytest
from playwright.async_api import async_playwright, expect

from tests.support import pdf_bytes
from tests.test_processing_browser import panel_server as panel_server

pytestmark = pytest.mark.browser


async def test_page_move_state_preserves_coverage_sources_and_other_operations(panel_server):
    """Чистые операции проверяются в настоящем JS runtime, включая несмежные/межфайловые листы."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(panel_server)
            result = await page.evaluate(
                """async () => {
                  const m = await import('/static/js/features/boundary_state.js');
                  const ref = (n, file='a') => ({file_id:file,page_number:n});
                  const physical = Array.from({length:173},(_,i)=>ref(i+1));
                  const initial = {documents:[{client_id:'one',pages:physical.slice(0,35)},
                    {client_id:'two',pages:physical.slice(35)}],excluded_pages:[]};
                  const moved = m.movePages(initial,[ref(31)],ref(32),'after',physical);
                  const far = m.movePages(moved,[ref(31)],ref(150),'after',
                    m.orderedPages(moved,physical));
                  const restored = m.movePages(far,[ref(31)],ref(32),'before',
                    m.orderedPages(far,physical));
                  const split = m.splitRange(far,ref(150),m.orderedPages(far,physical));
                  const merged = m.mergeRanges(split,ref(150),m.orderedPages(split,physical));
                  const cross = [ref(1),ref(2),ref(3),ref(4),ref(1,'b'),ref(2,'b')];
                  const plan = {documents:[{client_id:'a',pages:cross.slice(0,4)},
                    {client_id:'b',pages:[cross[5]]}],excluded_pages:[cross[4]]};
                  const many = m.movePages(plan,[cross[1],cross[3],cross[4]],
                    cross[5],'before',cross);
                  const all = m.movePages(plan,cross.slice(0,4),cross[5],'after',cross);
                  let self=false, excluded=false;
                  try {m.movePages(plan,[cross[0]],cross[0],'after',cross);} catch {self=true;}
                  try {m.movePages(plan,[cross[0]],cross[4],'after',cross);} catch {excluded=true;}
                  return {adjacent:moved.page_order.slice(30,32).map(p=>p.page_number),
                    farMember:m.groupFor(far,ref(31)).client_id,
                    farAfter:far.page_order.findIndex(p=>p.page_number===31)===
                      far.page_order.findIndex(p=>p.page_number===150)+1,
                    restored:restored.page_order.every((p,i)=>p.page_number===i+1),
                    unchanged:initial.documents[0].pages[30].page_number===31
                      && !initial.page_order,
                    coverage:[moved,far,restored,split,merged].every(p=>m.coversPages(p,physical)),
                    cross:many.page_order.map(m.pageKey),crossGroup:many.documents[1].pages.map(m.pageKey),
                    restoredExcluded:many.excluded_pages.length,oneGroup:all.documents.length,
                    crossCoverage:m.coversPages(many,cross)&&m.coversPages(all,cross),self,excluded};
                }"""
            )
            assert result == {
                "adjacent": [32, 31],
                "farMember": "two",
                "farAfter": True,
                "restored": True,
                "unchanged": True,
                "coverage": True,
                "cross": ["a:1", "a:3", "a:2", "a:4", "b:1", "b:2"],
                "crossGroup": ["a:2", "a:4", "b:1", "b:2"],
                "restoredExcluded": 0,
                "oneGroup": 1,
                "crossCoverage": True,
                "self": True,
                "excluded": True,
            }
        finally:
            await browser.close()


async def test_reorder_rotate_reload_and_final_pdf(container, panel_server):
    """31→32, дальний перенос, metadata offset, поворот/zoom/reload и PDF проходят реальный HTTP."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1680, "height": 1050})
        errors, preview_requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: preview_requests.append(request.url))

        async def jump(number):
            """Выбирает текущую позицию ленты через доступный числовой input."""
            await page.locator("[data-page-jump]").fill(str(number))
            await page.locator("[data-page-jump]").press("Tab")
            await expect(page.locator("[data-page-label]")).to_contain_text(f"Страница {number} из")

        async def plan():
            """Читает реальный авторизованный API из браузерной сессии без сторонних credentials."""
            return await page.evaluate(
                "async () => (await import('/static/js/api.js')).api('/api/v1/jobs/' + "
                "sessionStorage.getItem('intake_job') + '/segmentation')"
            )

        async def saved_after(action, revision):
            """Ждёт ACK ровно следующей редакции, а не локальной перестановки DOM."""
            await action()
            await expect(page.locator("[data-boundary-save]")).to_contain_text(
                f"редакция {revision + 1}", timeout=15000
            )

        try:
            await page.goto(panel_server)
            await page.locator("[data-files]").set_input_files(
                {"name": "eighty.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes(80)}
            )
            await expect(page.locator("[data-start]")).to_be_enabled(timeout=30000)
            await jump(40)
            current = await plan()
            await saved_after(
                page.locator('[data-boundary-action="split"]').click, current["plan_revision"]
            )
            await jump(31)
            current = await plan()
            forward = page.locator('[data-boundary-action="page-forward"]')
            await saved_after(forward.click, current["plan_revision"])
            current = await plan()
            assert [p["page_number"] for p in current["draft_plan"]["page_order"][30:32]] == [
                32,
                31,
            ]
            await saved_after(
                page.locator('[data-boundary-action="undo"]').click, current["plan_revision"]
            )
            current = await plan()
            assert current["draft_plan"]["page_order"] is None
            await saved_after(forward.click, current["plan_revision"])
            await page.locator('[data-boundary-action="page-move"]').click()
            modal = page.locator("[data-page-move-dialog]")
            await expect(modal).to_be_visible()
            await page.locator("[data-page-move-target]").fill("32")
            await page.locator("[data-page-move-submit]").click()
            await expect(page.locator("[data-page-move-error]")).to_contain_text("входит")
            await page.locator("[data-page-move-target]").fill("71")
            await expect(page.locator("[data-page-move-destination]")).to_contain_text(
                "исходная стр. 71"
            )
            current = await plan()
            await saved_after(
                page.locator("[data-page-move-submit]").click, current["plan_revision"]
            )
            await expect(modal).not_to_be_visible()
            current = await plan()
            order = current["draft_plan"]["page_order"]
            numbers = [p["page_number"] for p in order]
            assert numbers.index(31) == numbers.index(71) + 1
            assert 31 in [p["page_number"] for p in current["draft_plan"]["documents"][1]["pages"]]
            await jump(numbers.index(70) + 1)
            await page.locator('[data-boundary-action="page-move"]').click()
            await page.locator("[data-page-move-side]").select_option("before")
            await page.locator("[data-page-move-target]").fill("1")
            await saved_after(
                page.locator("[data-page-move-submit]").click, current["plan_revision"]
            )
            preview_requests.clear()
            await page.reload()
            image = page.locator("[data-page-image]")
            await expect(image).to_be_visible(timeout=30000)
            await expect(image).to_have_attribute("alt", "Страница 70 исходного документа")
            assert any("/70/preview" in url for url in preview_requests)
            await expect(page.locator("[data-rotate-left]")).to_be_enabled()
            current = await plan()
            await page.locator("[data-rotate-left]").click()
            await expect(page.locator("[data-rotation-label]")).to_have_text("270°")
            geometry = await page.evaluate(
                "() => { const a=document.querySelector('[data-page-image]')"
                ".getBoundingClientRect(), "
                "b=document.querySelector('[data-page-surface]').getBoundingClientRect(); "
                "return {width:a.width,height:a.height,dx:Math.abs(a.width-b.width),"
                "dy:Math.abs(a.height-b.height)}; }"
            )
            assert geometry["width"] > geometry["height"]
            assert geometry["dx"] < 1 and geometry["dy"] < 1
            for angle in ("0°", "90°", "180°", "270°", "0°"):
                await page.locator("[data-rotate-right]").click()
                await expect(page.locator("[data-rotation-label]")).to_have_text(angle)
            await page.locator("[data-rotate-left]").click()
            await page.locator("[data-zoom-in]").click()
            await expect(page.locator("[data-fit]")).to_have_text("125%")
            assert (await image.bounding_box())["width"] > geometry["width"]
            await page.locator("[data-next]").click()
            await expect(page.locator("[data-rotation-label]")).to_have_text("0°")
            await page.locator("[data-previous]").click()
            await expect(page.locator("[data-rotation-label]")).to_have_text("270°")
            await page.reload()
            await expect(image).to_be_visible(timeout=15000)
            await expect(page.locator("[data-rotation-label]")).to_have_text("270°")
            assert (await plan())["plan_revision"] == current["plan_revision"]
            await asyncio.to_thread(Path("tmp").mkdir, exist_ok=True)
            await page.screenshot(path="tmp/page-order-rotation-desktop.png", full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path="tmp/page-order-rotation-mobile.png", full_page=True)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.set_viewport_size({"width": 1680, "height": 1050})
            expected = current["draft_plan"]["documents"]
            await page.locator("[data-confirm-plan]").click()
            await expect(page.locator("[data-export]")).to_be_enabled(timeout=60000)
            await expect(forward).to_be_disabled()
            await expect(page.locator("[data-rotate-right]")).to_be_enabled()
            job_id = await page.evaluate("sessionStorage.getItem('intake_job')")
            response = await page.request.get(f"{panel_server}/api/v1/jobs/{job_id}/result")
            documents = (await response.json())["files"][0]["documents"]
            for doc, group in zip(documents, expected, strict=True):
                downloaded = await page.request.get(doc["attachment_file"]["file_url"])
                with pymupdf.open(stream=await downloaded.body()) as pdf:
                    assert [p.get_text().strip() for p in pdf] == [
                        f"SYNTHETIC PAGE {p['page_number']}" for p in group["pages"]
                    ]
            page.once("dialog", lambda dialog: dialog.accept())
            await page.locator("[data-new-analysis]").click()
            assert await page.evaluate("sessionStorage.getItem('intake_view_rotations')") is None
            await expect(page.locator("[data-rotation-label]")).to_have_text("0°")
            await expect(page.locator("[data-rotate-left]")).to_be_disabled()
            assert not errors
        finally:
            await browser.close()
