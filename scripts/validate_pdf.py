#!/usr/bin/env python3
"""Validate structural and textual invariants of a generated KYC PDF."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from pypdf import PdfReader


REQUIRED_HEADINGS = [
    "Отчёт KYC-проверки контрагента",
    "Методика проверки и источники",
    "Покрытие источников",
    "Общие сведения",
    "Финансовый анализ",
    "Юридическая история",
    "Руководители и связанные лица",
    "Специализированный анализ",
    "Профиль сделки",
    "Ключевые риски и итоговая оценка",
    "Документы и условия до заключения договора",
    "Матрица договорных мер",
    "Углублённая проверка",
]

GENERATOR_MARKER = "kyc-counterparty-check/html-chromium-v1.1"
RAW_STATUS_CODES = [
    "primary_verified", "document_verified", "secondary_signal", "conflict",
    "not_confirmed", "not_checked", "source_unavailable", "not_found",
]
PRELIMINARY_NOTICE = "Предварительная KYC-оценка. Решение по конкретному договору не принято."


def embedded_font_status(reader: PdfReader) -> tuple[bool, list[str]]:
    fonts: list[str] = []
    embedded = True
    seen: set[int] = set()
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        font_dict = resources.get("/Font") or {}
        for _, reference in font_dict.items():
            font = reference.get_object()
            identity = id(font)
            if identity in seen:
                continue
            seen.add(identity)
            fonts.append(str(font.get("/BaseFont", "unknown")))
            descriptor_ref = font.get("/FontDescriptor")
            if descriptor_ref is None and font.get("/DescendantFonts"):
                descendant = font["/DescendantFonts"][0].get_object()
                descriptor_ref = descendant.get("/FontDescriptor")
            if descriptor_ref is None:
                embedded = False
                continue
            descriptor = descriptor_ref.get_object()
            if not any(descriptor.get(key) is not None for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                embedded = False
    return embedded, sorted(set(fonts))


def expected_values(data: dict[str, Any]) -> list[str]:
    return [
        data["counterparty"]["name"],
        data["counterparty"]["inn"],
        data["executive_summary"]["decision"],
        f"{data['executive_summary']['overall_risk']} / 10",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expected-json", type=Path)
    parser.add_argument("--min-pages", type=int, default=6)
    parser.add_argument("--max-pages", type=int, default=20)
    args = parser.parse_args()

    if not args.pdf.is_file() or args.pdf.stat().st_size < 10_000:
        raise ValueError("PDF is missing or unexpectedly small")
    reader = PdfReader(str(args.pdf))
    if reader.is_encrypted:
        raise ValueError("PDF must not be encrypted")
    page_count = len(reader.pages)
    if not args.min_pages <= page_count <= args.max_pages:
        raise ValueError(f"Unexpected page count: {page_count}")

    page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
    all_text = "\n".join(page_texts)
    normalized_text = all_text.casefold()
    for index, (page, text) in enumerate(zip(reader.pages, page_texts, strict=True), start=1):
        width_mm = float(page.mediabox.width) * 25.4 / 72
        height_mm = float(page.mediabox.height) * 25.4 / 72
        if abs(width_mm - 210) > 1 or abs(height_mm - 297) > 1:
            raise ValueError(f"Page {index} is not A4 portrait: {width_mm:.1f} x {height_mm:.1f} mm")
        if f"Стр. {index} из {page_count}" not in text:
            raise ValueError(f"Physical page number is missing or inconsistent on page {index}")
    if len(all_text) < 4_000:
        raise ValueError("Too little extractable text; font or rendering may be broken")
    sparse = [index + 1 for index, text in enumerate(page_texts) if len(re.sub(r"\s+", "", text)) < 180]
    if sparse:
        raise ValueError(f"Nearly blank physical pages detected: {sparse}")
    unresolved = re.findall(r"\{\{[^}]+\}\}", all_text)
    if unresolved:
        raise ValueError(f"Unresolved placeholders: {sorted(set(unresolved))}")
    leaked_statuses = [code for code in RAW_STATUS_CODES if code.casefold() in normalized_text]
    if leaked_statuses:
        raise ValueError(f"Raw evidence status codes leaked into PDF: {leaked_statuses}")

    for heading in REQUIRED_HEADINGS:
        if heading.casefold() not in normalized_text:
            raise ValueError(f"Required heading not found: {heading}")

    if args.expected_json:
        data = json.loads(args.expected_json.read_text(encoding="utf-8"))
        for value in expected_values(data):
            if str(value).casefold() not in normalized_text:
                raise ValueError(f"Expected report value not found: {value}")
        profile = data.get("deal_profile") or {}
        if profile.get("assessment_scope") == "preliminary":
            if PRELIMINARY_NOTICE.casefold() not in normalized_text:
                raise ValueError("Preliminary notice is missing")
            if data["executive_summary"]["decision"] == "Допустить":
                raise ValueError("Preliminary report cannot use unconditional Допустить")
            if not profile.get("missing_critical"):
                raise ValueError("Preliminary report requires missing_critical")
        elif profile.get("assessment_scope") != "final":
            raise ValueError("deal_profile.assessment_scope must be preliminary or final")

    embedded, fonts = embedded_font_status(reader)
    if not embedded:
        raise ValueError(f"One or more fonts are not embedded: {fonts}")
    title = (reader.metadata or {}).get("/Title", "")
    if "KYC" not in title:
        raise ValueError("PDF metadata title is missing")
    generator = (reader.metadata or {}).get("/KYCGenerator", "")
    if generator != GENERATOR_MARKER:
        raise ValueError("PDF was not produced by the approved KYC HTML/Chromium generator")

    print(json.dumps({"ok": True, "pages": page_count, "fonts": fonts, "title": title, "generator": generator}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
