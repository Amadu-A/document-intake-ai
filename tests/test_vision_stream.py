# tests/test_vision_stream.py
"""Зависание на JSON-пробелах, сохранность строк и ограниченный повтор настоящего SSE-адаптера."""

import json

import httpx
import pytest

from app.domain.errors import IntakeError
from app.domain.schema import StrictModel
from app.infrastructure.vision import SharedVision
from app.infrastructure.vision_stream import JSONWhitespaceGuard


@pytest.mark.parametrize("character", [" ", "\t", "\r", "\n"])
def test_whitespace_limit_survives_delta_boundaries(character):
    """Первые 512 служебных символов допустимы; следующий отклоняется даже в другом delta."""
    guard = JSONWhitespaceGuard()
    guard.feed('{"value":')
    for _ in range(8):
        guard.feed(character * 64)
    with pytest.raises(IntakeError) as error:
        guard.feed(character)
    assert error.value.code == "model_output_limit"


@pytest.mark.parametrize("chunk_size", [1, 7, 128])
def test_long_string_whitespace_and_escaped_quotes_are_preserved(chunk_size):
    """Пробелы внутри строк, escaped quotes/backslashes и переносы не считаются форматированием."""
    value = {"text": 'начало "' + " " * 1500 + '\\ конец\nновая строка "'}
    encoded = json.dumps(value, ensure_ascii=False)
    guard = JSONWhitespaceGuard()
    for begin in range(0, len(encoded), chunk_size):
        guard.feed(encoded[begin : begin + chunk_size])
    assert json.loads(encoded) == value


def test_json_tokens_reset_the_whitespace_run():
    """Общий объём обычного форматирования не ограничивается, если содержимое продвигается."""
    guard = JSONWhitespaceGuard()
    encoded = '{"values":[' + ",".join("\n  1" for _ in range(1000)) + "]}"
    guard.feed(encoded)
    assert len(json.loads(encoded)["values"]) == 1000


class InterruptedStream(httpx.AsyncByteStream):
    """Проверяет, что HTTP-поток закрыт до дальнейшей генерации пробельного мусора."""

    def __init__(self) -> None:
        """Сохраняет факт закрытия без payload документа."""
        self.closed = False

    async def __aiter__(self):
        """Отдаёт только начало объекта и пробельное зацикливание двумя SSE-событиями."""
        for delta in ('{"value":', "\n" * 513):
            yield sse(delta)
        raise AssertionError("Адаптер продолжил читать зацикленный поток")

    async def aclose(self):
        """Фиксирует закрытие соединения при ранней ошибке."""
        self.closed = True


def sse(delta: str, finish: str | None = None) -> bytes:
    """Кодирует синтетический OpenAI SSE без реального документа."""
    event = {"choices": [{"delta": {"content": delta}, "finish_reason": finish}]}
    return ("data: " + json.dumps(event) + "\n\n").encode()


async def test_stalled_stream_is_closed_before_a_result_can_be_returned(settings):
    """Пробельный мусор прерывает настоящий адаптер и не становится подтверждённым JSON."""
    stream, events = InterruptedStream(), []

    async def transport(request):
        """Подменяет только внешний HTTP, оставляя реальный streaming parser."""
        return httpx.Response(200, stream=stream)

    async def emit(kind, data):
        """Сохраняет только неподтверждённые события в памяти теста."""
        events.append((kind, data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError) as error:
            await SharedVision(settings, client).request("Тест", {}, [], {"type": "object"}, emit)
    assert error.value.code == "model_output_limit" and stream.closed
    assert all(not data.get("validated") for kind, data in events if kind == "text_delta")


async def test_stream_keeps_long_string_values_and_normal_indentation(settings):
    """Настоящий адаптер сохраняет строковые пробелы и escaping при разрыве delta внутри escape."""
    expected = {"value": '" начало ' + " " * 800 + "\\ конец\nновая строка"}
    encoded = json.dumps(expected, ensure_ascii=False, indent=2)

    async def transport(request):
        """Делит синтетический JSON на трёхсимвольные delta, включая escape-последовательности."""
        content = b"".join(sse(encoded[i : i + 3]) for i in range(0, len(encoded), 3))
        return httpx.Response(200, content=content + sse("", "stop") + b"data: [DONE]\n\n")

    async def emit(kind, data):
        """Не публикует синтетическое содержимое в обычные логи."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await SharedVision(settings, client).request(
            "Тест", {}, [], {"type": "object"}, emit
        )
    assert result == expected


@pytest.mark.parametrize("finish", ["length", "content_filter", None])
async def test_complete_json_without_normal_stop_is_still_rejected(settings, finish):
    """Даже закрытый JSON не оправдывает length/filter или оборванный поток без stop."""

    async def transport(request):
        """Имитирует нештатное окончание после синтетического закрытого JSON."""
        return httpx.Response(200, content=sse("{}", finish) + b"data: [DONE]\n\n")

    async def emit(kind, data):
        """Не выводит синтетический stream в журнал."""

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(IntakeError) as error:
            await SharedVision(settings, client).request("Тест", {}, [], {"type": "object"}, emit)
    assert error.value.code == ("model_invalid_json" if finish is None else "model_output_limit")


class SyntheticResult(StrictModel):
    """Маленький типизированный результат для проверки application retry."""

    value: str


@pytest.mark.parametrize("recover", [True, False])
async def test_stalled_answer_has_bounded_retry_and_safe_diagnostics(container, recover):
    """Повтор получает причину и компактный формат; исчерпание попыток остаётся явной ошибкой."""
    payloads, events = [], []

    async def transport(request):
        """Возвращает пробельный цикл, затем по сценарию проверяемый полный ответ."""
        payloads.append(json.loads(request.content))
        delta = (
            json.dumps({"value": "SYNTHETIC"})
            if recover and len(payloads) > 1
            else '{"value":' + "\n" * 513
        )
        return httpx.Response(200, content=sse(delta, "stop") + b"data: [DONE]\n\n")

    async def emit(kind, data):
        """Собирает retry metadata без секретов и документов."""
        events.append((kind, data))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        container.analysis.vision = SharedVision(container.settings, client)
        request = container.analysis._validated(
            "Синтетический prompt",
            {"task": "synthetic_reading"},
            [],
            SyntheticResult,
            lambda value: None,
            emit,
        )
        if recover:
            assert (await request).value == "SYNTHETIC"
        else:
            with pytest.raises(IntakeError) as error:
                await request
            assert error.value.code == "model_output_limit"
    assert len(payloads) == 2 == container.settings.model_attempts
    assert all(p["max_tokens"] == container.settings.vlm_max_tokens for p in payloads)
    assert "Технический формат" not in payloads[0]["messages"][0]["content"]
    assert "компактный JSON" in payloads[1]["messages"][0]["content"]
    feedback = json.loads(payloads[1]["messages"][1]["content"][0]["text"])["validation_errors"]
    assert feedback[0]["code"] == "model_output_limit"
    retries = [data for kind, data in events if kind == "model_retry"]
    assert retries[0]["schema"] == "SyntheticResult"
    assert retries[0]["task"] == "synthetic_reading"
