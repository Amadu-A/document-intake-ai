# app/infrastructure/registry_xlsx.py
"""XLSX adapter: один лист общего реестра, literal text, потоковая запись и удаление temp XML."""

import contextlib
from datetime import date
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.domain.registry import RegistryResult, registry_mapping


class RegistryXlsx:
    """Записывает до 10000 строк текущего сервиса без formulas/macros/hyperlinks."""

    def render(self, result: RegistryResult) -> bytes:
        """Null остаётся пустым, числа целыми, даты датами, строки буквальным текстом.

        Cleanup _writer изолирован здесь для закреплённой openpyxl 3.1.5:
        публичного удаления временного XML при ошибке append/save библиотека не даёт.
        """
        mapping = registry_mapping()
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Реестр")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:N{len(result.items) + 1}"
        sheet.sheet_view.showGridLines = False
        for index, key in enumerate(mapping.column_order, 1):
            sheet.column_dimensions[get_column_letter(index)].width = (
                7
                if key == "number"
                else 34
                if key in {"recipient", "sender", "document_name"}
                else 24
            )
        try:
            header = []
            for key in mapping.column_order:
                cell = WriteOnlyCell(sheet, mapping.columns[key][0])
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="25476B")
                cell.alignment = Alignment(wrap_text=True, vertical="center")
                header.append(cell)
            sheet.row_dimensions[1].height = 42
            sheet.append(header)
            for row in result.items:
                cells = []
                for key in mapping.column_order:
                    value = getattr(row, key)
                    if isinstance(value, str) and len(value) > 32767:
                        raise ValueError("Поле превышает размер Excel cell")
                    cell = WriteOnlyCell(sheet, value)
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                    if isinstance(value, str):
                        cell.data_type, cell.number_format = "s", "@"
                    elif isinstance(value, date):
                        cell.number_format = "DD.MM.YYYY"
                    cells.append(cell)
                sheet.append(cells)
            output = BytesIO()
            workbook.save(output)
            return output.getvalue()
        finally:
            with contextlib.suppress(Exception):
                sheet.close()
            writer = sheet._writer
            if writer is not None and Path(writer.out).exists():
                writer.cleanup()
            workbook.close()
