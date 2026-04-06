"""Local artifact export helpers for meeting delegate sessions."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ArtifactExportError(RuntimeError):
    """Raised when a local meeting artifact cannot be rendered."""


class MeetingArtifactExporter:
    def __init__(self) -> None:
        self._pandoc = self._resolve_binary(
            env_name="DELEGATE_PANDOC_PATH",
            command_name="pandoc",
            fallbacks=[
                r"C:\Users\jung\AppData\Local\Microsoft\WinGet\Packages\JohnMacFarlane.Pandoc_Microsoft.Winget.Source_8wekyb3d8bbwe\pandoc-3.9.0.1\pandoc.exe",
            ],
        )
        self._soffice = self._resolve_binary(
            env_name="DELEGATE_SOFFICE_PATH",
            command_name="soffice",
            fallbacks=[
                r"C:\Program Files\LibreOffice\program\soffice.exe",
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            ],
        )
        self._timeout = float(os.getenv("DELEGATE_ARTIFACT_TIMEOUT_SECONDS", "90"))
        configured_reference = os.getenv("DELEGATE_REFERENCE_DOC_PATH", "").strip()
        default_reference = Path("doc/templates/meeting-summary-reference.docx")
        if configured_reference:
            self._reference_doc = Path(configured_reference)
        elif default_reference.exists():
            self._reference_doc = default_reference
        else:
            self._reference_doc = None

    @property
    def pdf_ready(self) -> bool:
        return bool(self._pandoc and self._soffice)

    def readiness(self) -> dict[str, Any]:
        blocking_reasons: list[str] = []
        quality_notes: list[str] = []
        if not self._pandoc:
            blocking_reasons.append("pandoc is not installed for summary.docx export.")
        if not self._soffice:
            blocking_reasons.append("LibreOffice is not installed for summary.pdf export.")
        docx_polish_available = importlib.util.find_spec("docx") is not None
        if not docx_polish_available:
            quality_notes.append(
                "python-docx is not installed, so the DOCX/PDF styling polish layer will be skipped."
            )
        reference_doc_ready = bool(self._reference_doc and self._reference_doc.exists())
        if self._reference_doc and not reference_doc_ready:
            quality_notes.append(
                f"Configured reference DOCX was not found: {self._reference_doc}"
            )
        return {
            "docx_ready": bool(self._pandoc),
            "pdf_ready": self.pdf_ready,
            "pandoc_path": self._pandoc,
            "soffice_path": self._soffice,
            "reference_doc_path": str(self._reference_doc.resolve()) if self._reference_doc else None,
            "reference_doc_ready": reference_doc_ready,
            "docx_polish_available": docx_polish_available,
            "blocking_reasons": blocking_reasons,
            "quality_notes": quality_notes,
        }

    def export_summary_bundle(self, summary_markdown_path: Path) -> list[dict[str, str]]:
        exports: list[dict[str, str]] = []
        if not summary_markdown_path.exists():
            return exports
        if not self._pandoc:
            return exports

        docx_path = summary_markdown_path.with_suffix(".docx")
        self._run(
            self._pandoc_command(summary_markdown_path, docx_path),
            error_label="Pandoc summary export",
        )
        self._polish_summary_docx(docx_path)
        if docx_path.exists():
            exports.append({"format": "docx", "path": str(docx_path)})

        if not self._soffice or not docx_path.exists():
            return exports

        out_dir = summary_markdown_path.parent
        self._run(
            [
                self._soffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(docx_path),
            ],
            error_label="LibreOffice PDF export",
        )

        generated_pdf = out_dir / f"{docx_path.stem}.pdf"
        target_pdf = summary_markdown_path.with_suffix(".pdf")
        if generated_pdf.exists():
            if generated_pdf != target_pdf:
                generated_pdf.replace(target_pdf)
            exports.append({"format": "pdf", "path": str(target_pdf)})
        return exports

    def _pandoc_command(self, summary_markdown_path: Path, docx_path: Path) -> list[str]:
        command = [
                self._pandoc,
                str(summary_markdown_path),
                "-o",
                str(docx_path),
            ]
        if self._reference_doc and self._reference_doc.exists():
            command.extend(["--reference-doc", str(self._reference_doc)])
        return command

    def _resolve_binary(self, *, env_name: str, command_name: str, fallbacks: list[str]) -> str | None:
        configured = os.getenv(env_name, "").strip()
        if configured and Path(configured).exists():
            return configured

        discovered = shutil.which(command_name)
        if discovered:
            return discovered

        for candidate in fallbacks:
            if Path(candidate).exists():
                return candidate
        return None

    def _run(self, args: list[str], *, error_label: str) -> None:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except OSError as exc:
            raise ArtifactExportError(f"{error_label} could not start: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ArtifactExportError(f"{error_label} timed out after {self._timeout} seconds.") from exc

        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise ArtifactExportError(f"{error_label} failed: {details}")

    def _polish_summary_docx(self, docx_path: Path) -> None:
        if not docx_path.exists():
            return
        try:
            from docx import Document
            from docx.enum.section import WD_SECTION_START
            from docx.enum.table import WD_ALIGN_VERTICAL
            from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn
            from docx.shared import Inches, Pt, RGBColor
        except Exception:
            return

        document = Document(docx_path)

        def set_rfonts(target: Any, *, ascii_name: str, east_asia_name: str | None = None) -> None:
            east_asia_name = east_asia_name or ascii_name
            target.font.name = ascii_name
            element = getattr(target, "_element", None)
            if element is None:
                element = getattr(target, "element", None)
            if element is None:
                return
            rpr = element.get_or_add_rPr()
            rfonts = rpr.rFonts
            if rfonts is None:
                rfonts = OxmlElement("w:rFonts")
                rpr.append(rfonts)
            rfonts.set(qn("w:ascii"), ascii_name)
            rfonts.set(qn("w:hAnsi"), ascii_name)
            rfonts.set(qn("w:cs"), ascii_name)
            rfonts.set(qn("w:eastAsia"), east_asia_name)

        def set_run_font(run: Any, *, ascii_name: str, east_asia_name: str | None = None, size_pt: float | None = None, color_hex: str | None = None, bold: bool | None = None) -> None:
            east_asia_name = east_asia_name or ascii_name
            run.font.name = ascii_name
            if size_pt is not None:
                run.font.size = Pt(size_pt)
            if color_hex:
                run.font.color.rgb = RGBColor.from_string(color_hex)
            if bold is not None:
                run.font.bold = bold
            rpr = run._element.get_or_add_rPr()
            rfonts = rpr.rFonts
            if rfonts is None:
                rfonts = OxmlElement("w:rFonts")
                rpr.append(rfonts)
            rfonts.set(qn("w:ascii"), ascii_name)
            rfonts.set(qn("w:hAnsi"), ascii_name)
            rfonts.set(qn("w:cs"), ascii_name)
            rfonts.set(qn("w:eastAsia"), east_asia_name)

        def set_paragraph_bottom_border(paragraph: Any, *, color_hex: str, size: int = 10, space: int = 6) -> None:
            p_pr = paragraph._element.get_or_add_pPr()
            borders = p_pr.find(qn("w:pBdr"))
            if borders is None:
                borders = OxmlElement("w:pBdr")
                p_pr.append(borders)
            bottom = borders.find(qn("w:bottom"))
            if bottom is None:
                bottom = OxmlElement("w:bottom")
                borders.append(bottom)
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), str(size))
            bottom.set(qn("w:space"), str(space))
            bottom.set(qn("w:color"), color_hex)

        def set_cell_shading(cell: Any, fill_hex: str) -> None:
            tc_pr = cell._tc.get_or_add_tcPr()
            shading = tc_pr.find(qn("w:shd"))
            if shading is None:
                shading = OxmlElement("w:shd")
                tc_pr.append(shading)
            shading.set(qn("w:val"), "clear")
            shading.set(qn("w:color"), "auto")
            shading.set(qn("w:fill"), fill_hex)

        def set_cell_margins(cell: Any, *, top: int = 90, bottom: int = 90, left: int = 110, right: int = 110) -> None:
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_mar = tc_pr.find(qn("w:tcMar"))
            if tc_mar is None:
                tc_mar = OxmlElement("w:tcMar")
                tc_pr.append(tc_mar)
            for side, value in {"top": top, "bottom": bottom, "left": left, "right": right}.items():
                node = tc_mar.find(qn(f"w:{side}"))
                if node is None:
                    node = OxmlElement(f"w:{side}")
                    tc_mar.append(node)
                node.set(qn("w:w"), str(value))
                node.set(qn("w:type"), "dxa")

        for section in document.sections:
            section.start_type = WD_SECTION_START.CONTINUOUS
            section.top_margin = Inches(0.72)
            section.bottom_margin = Inches(0.72)
            section.left_margin = Inches(0.82)
            section.right_margin = Inches(0.82)

        for style_name, font_name, east_font, size_pt, color_hex, bold in [
            ("Heading 1", "Noto Serif KR", "Noto Serif KR", 22.0, "204F3D", True),
            ("Heading 2", "Noto Sans KR", "Noto Sans KR", 14.0, "244E3B", True),
            ("Heading 3", "Noto Sans KR", "Noto Sans KR", 12.3, "355E4B", True),
            ("Normal", "Noto Sans KR", "Noto Sans KR", 10.5, "1F2933", False),
            ("List Paragraph", "Noto Sans KR", "Noto Sans KR", 10.3, "1F2933", False),
            ("Subtitle", "Noto Sans KR", "Noto Sans KR", 9.6, "6B7C74", False),
            ("Title", "Noto Serif KR", "Noto Serif KR", 22.0, "204F3D", True),
            ("Table Grid", "Noto Sans KR", "Noto Sans KR", 10.0, "1F2933", False),
        ]:
            try:
                style = document.styles[style_name]
            except KeyError:
                continue
            set_rfonts(style, ascii_name=font_name, east_asia_name=east_font)
            style.font.size = Pt(size_pt)
            style.font.color.rgb = RGBColor.from_string(color_hex)
            style.font.bold = bold
            pf = style.paragraph_format
            if style_name == "Heading 1":
                pf.alignment = WD_ALIGN_PARAGRAPH.LEFT
                pf.space_before = Pt(0)
                pf.space_after = Pt(14)
                pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
                pf.line_spacing = 1.0
                pf.keep_with_next = True
            elif style_name == "Heading 2":
                pf.alignment = WD_ALIGN_PARAGRAPH.LEFT
                pf.space_before = Pt(16)
                pf.space_after = Pt(7)
                pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
                pf.line_spacing = 1.05
                pf.keep_with_next = True
            elif style_name == "Heading 3":
                pf.alignment = WD_ALIGN_PARAGRAPH.LEFT
                pf.space_before = Pt(11)
                pf.space_after = Pt(4)
                pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
                pf.line_spacing = 1.08
                pf.keep_with_next = True
            elif style_name in {"Normal", "List Paragraph", "Table Grid"}:
                pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
                pf.line_spacing = 1.32
                pf.space_after = Pt(4)

        if document.paragraphs:
            title = document.paragraphs[0]
            title.alignment = WD_ALIGN_PARAGRAPH.LEFT
            title.paragraph_format.space_before = Pt(0)
            title.paragraph_format.space_after = Pt(18)
            title.paragraph_format.keep_with_next = True
            set_paragraph_bottom_border(title, color_hex="C8D8D1", size=10, space=14)
            for run in title.runs:
                set_run_font(run, ascii_name="Noto Serif KR", east_asia_name="Noto Serif KR", size_pt=22.0, color_hex="204F3D", bold=True)

        metadata_labels = ("회의 일시:", "작성 주체:", "세션 ID:", "참석자:")
        metadata_label_names = {label.rstrip(":") for label in metadata_labels}
        within_overview_block = False

        for paragraph in document.paragraphs[1:]:
            text = (paragraph.text or "").strip()
            style_name = paragraph.style.name if paragraph.style is not None else ""
            if style_name == "Heading 2":
                within_overview_block = text == "회의 개요"
                set_paragraph_bottom_border(paragraph, color_hex="D9E7E1", size=6, space=6)
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=14.0, color_hex="244E3B", bold=True)
            elif style_name == "Heading 3":
                within_overview_block = False
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=12.3, color_hex="355E4B", bold=True)
            elif within_overview_block and any(text.startswith(label) for label in metadata_labels):
                paragraph.paragraph_format.space_before = Pt(0)
                paragraph.paragraph_format.space_after = Pt(7)
                paragraph.paragraph_format.left_indent = Inches(0.02)
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=10.7, color_hex="1F2933", bold=run.bold)
            elif text.startswith("제기자:"):
                within_overview_block = False
                paragraph.paragraph_format.left_indent = Inches(0.12)
                paragraph.paragraph_format.space_after = Pt(2)
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=9.8, color_hex="355E4B", bold=True)
            elif text.startswith("타임스탬프:"):
                within_overview_block = False
                paragraph.paragraph_format.left_indent = Inches(0.12)
                paragraph.paragraph_format.space_after = Pt(6)
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=9.6, color_hex="5F6C66", bold=False)
            elif style_name == "Normal":
                if text:
                    within_overview_block = False
                for run in paragraph.runs:
                    set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=10.5, color_hex="1F2933", bold=run.bold)

        if document.tables:
            first_table = document.tables[0]
            first_column_values = [(row.cells[0].text or "").strip() for row in first_table.rows[1:]]
            if first_column_values and metadata_label_names.issuperset(set(first_column_values)):
                first_table._element.getparent().remove(first_table._element)

        for index, table in enumerate(document.tables):
            table.style = "Table Grid"
            for row_index, row in enumerate(table.rows):
                for cell_index, cell in enumerate(row.cells):
                    set_cell_margins(cell)
                    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                    if row_index == 0:
                        set_cell_shading(cell, "244E3B")
                    elif index == 0 and cell_index == 0:
                        set_cell_shading(cell, "EEF4F1")
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_before = Pt(0)
                        paragraph.paragraph_format.space_after = Pt(0)
                        for run in paragraph.runs:
                            if row_index == 0:
                                set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=10.0, color_hex="FFFFFF", bold=True)
                            elif cell_index == 0:
                                set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=10.0, color_hex="244E3B", bold=True)
                            else:
                                set_run_font(run, ascii_name="Noto Sans KR", east_asia_name="Noto Sans KR", size_pt=10.0, color_hex="1F2933", bold=False)

        document.save(docx_path)
