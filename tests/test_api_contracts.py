# tests/test_api_contracts.py
"""Публичный OpenAPI соответствует delivery-моделям и полному headless workflow."""

import asyncio
import json
from pathlib import Path

from app.domain.extractions import DetailedResult
from app.domain.registry import XLSX_MIME, RegistryResult


async def test_openapi_models_security_defaults_binary_and_local_swagger(client):
    """Swagger описывает авторизацию, параметры review, JSON 1/2, редактор и файлы."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema == json.loads(
        await asyncio.to_thread(Path("docs/openapi.json").read_text, "utf-8")
    )
    models = schema["components"]["schemas"]
    security = schema["components"]["securitySchemes"]
    assert any(v.get("in") == "header" and v.get("name") == "X-API-Key" for v in security.values())
    paths = schema["paths"]
    for path, methods in paths.items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch"}:
                continue
            assert operation.get("description") and operation.get("security"), path
            assert {"401", "403", "404", "409", "410", "422", "429", "503"}.issubset(
                operation["responses"]
            )
    body = paths["/api/v1/jobs"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    body = models[body["$ref"].rsplit("/", 1)[-1]]
    assert body["properties"]["files"]["items"]["format"] == "binary"
    for flag in ("auto_split", "skip_segmentation"):
        assert body["properties"][flag]["default"] is False
    assert models["PlanEdit"]["examples"] and models["DraftBody"]["examples"]
    assert paths["/api/v1/jobs/{job_id}/result"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/BatchResult")
    assert paths["/api/v1/jobs/{job_id}/extractions"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/DetailedResult")
    for path, mime in (
        ("/api/v1/jobs/{job_id}/archive", "application/zip"),
        ("/api/v1/jobs/{job_id}/documents/{document_id}/file", "application/pdf"),
        ("/api/v1/jobs/{job_id}/events", "text/event-stream"),
        ("/api/v1/jobs/{job_id}/registry.xlsx", XLSX_MIME),
    ):
        content = paths[path]["get"]["responses"]["200"]["content"]
        assert set(content) == {mime}
    assert "fingerprint" not in models["PublicComparison"]["properties"]
    for version in ("v1", "v2"):
        assert await asyncio.to_thread(Path(f"docs/samples/result-{version}.json").is_file)
    assert DetailedResult.model_json_schema(mode="serialization") == json.loads(
        await asyncio.to_thread(Path("docs/result-v2.schema.json").read_text, "utf-8")
    )
    assert RegistryResult.model_json_schema(mode="serialization") == json.loads(
        await asyncio.to_thread(Path("docs/registry.schema.json").read_text, "utf-8")
    )
    RegistryResult.model_validate_json(
        await asyncio.to_thread(Path("docs/samples/registry.json").read_text, "utf-8")
    )
    assert paths["/api/v1/jobs/{job_id}/registry"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("/RegistryResult")
    docs = await client.get("/docs")
    assert docs.status_code == 200 and "/swagger-assets/" in docs.text
    assert "cdn.jsdelivr" not in docs.text and "unpkg.com" not in docs.text
    assert (await client.get("/redoc")).status_code == 404
