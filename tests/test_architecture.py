# tests/test_architecture.py
"""Архитектурные границы, русские docstrings и frontend security contract."""

import ast
import re
from pathlib import Path


def test_dependencies_point_inward():
    """Domain/application не импортируют HTTP, ORM и конкретные внешние адаптеры."""
    for layer in ("domain", "application"):
        for path in Path("app", layer).rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [name.name for name in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    assert not module.startswith(
                        (
                            "fastapi",
                            "sqlalchemy",
                            "aio_pika",
                            "httpx",
                            "app.infrastructure",
                            "app.presentation",
                            "jinja2",
                        )
                    ), (path, module)


def test_source_files_and_definitions_have_russian_documentation():
    """Проверяет относительные пути и содержательные docstrings собственных функций/классов."""
    for path in Path("app").rglob("*.py"):
        source = path.read_text("utf-8")
        assert source.splitlines()[0] == "# " + path.as_posix()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                documentation = ast.get_docstring(node)
                assert documentation and re.search("[А-Яа-я]", documentation), (
                    path,
                    getattr(node, "name", "module"),
                )


def test_frontend_uses_safe_modular_contract():
    """Недоверенный VLM-текст не становится HTML; templates не содержат inline handlers/scripts."""
    templates = "\n".join(p.read_text("utf-8") for p in Path("app/web/templates").rglob("*.html"))
    javascript = "\n".join(p.read_text("utf-8") for p in Path("app/web/static/js").rglob("*.js"))
    assert "innerHTML" not in javascript
    assert not re.search(r"\son\w+=", templates)
    assert not re.search(r"<script(?![^>]*src=)", templates)
    assert templates.count('href="/static/css/style.css"') == 1
    assert 'type="module"' in templates and "<dialog" in templates
    assert 'data-prompt-tab="segmentation"' in templates
    assert 'data-prompt-tab="extraction"' in templates
    assert "getRandomValues" in javascript
