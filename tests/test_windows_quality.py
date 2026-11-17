# tests/test_windows_quality.py
"""Штатная PowerShell-проверка различает stderr статуса и настоящий ненулевой exit code."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("exit_code", [0, 7])
def test_native_stderr_redirect_keeps_exit_code_semantics(tmp_path, exit_code):
    """Проверяет настоящую PowerShell-функцию с внешним процессом и перенаправлением 2>&1."""
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("NOT_RUN: проверка Windows PowerShell")
    source = Path("ops/check-quality.ps1").read_text("utf-8")
    function = source.split("function Invoke-Checked", 1)[1].split("\nInvoke-Checked", 1)[0]
    child = tmp_path / "synthetic.py"
    child.write_text(
        "import sys\nsys.stderr.write('SYNTHETIC STATUS\\n')\nsys.exit(int(sys.argv[1]))\n", "utf-8"
    )

    def quoted(value):
        """Экранирует только один литерал PowerShell, без построения shell-подстановок."""
        return "'" + str(value).replace("'", "''") + "'"

    script = tmp_path / "check.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\nfunction Invoke-Checked"
        + function
        + f"\ntry {{ Invoke-Checked {quoted(sys.executable)} "
        + f"@({quoted(child)}, '{exit_code}') 2>&1; exit 0 }} catch {{ exit 9 }}\n",
        "utf-8-sig",
    )
    response = subprocess.run(  # noqa: S603 — только собственный синтетический код теста
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script.read_text("utf-8-sig")],
        capture_output=True,
        timeout=20,
    )
    assert response.returncode == (0 if exit_code == 0 else 9)
