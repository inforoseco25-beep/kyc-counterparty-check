#!/usr/bin/env python3
"""Render a structured KYC report JSON to a corporate A4 PDF with Chromium."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "report.html"
STYLE_PATH = ROOT / "templates" / "style.css"
FONT_REGULAR = ROOT / "assets" / "fonts" / "NotoSans-Regular.ttf"
FONT_SEMIBOLD = ROOT / "assets" / "fonts" / "NotoSans-SemiBold.ttf"

REQUIRED_TOP_LEVEL = {
    "metadata",
    "counterparty",
    "deal_profile",
    "executive_summary",
    "sources",
    "source_coverage",
    "general_info",
    "financial_analysis",
    "legal_history",
    "related_parties",
    "specialized_analysis",
    "risk_map",
    "scores",
    "decision_conditions",
    "documents_conditions",
    "control_matrix",
    "enhanced_due_diligence",
    "limitations",
}

EVIDENCE_LABELS = {
    "primary_verified": "Подтверждено первичным источником",
    "document_verified": "Подтверждено документом",
    "secondary_signal": "Вторичный сигнал",
    "conflict": "Расхождение",
    "not_confirmed": "Не подтверждено",
    "not_checked": "Не проверялось",
    "source_unavailable": "Источник недоступен",
    "not_found": "Не выявлено",
}

PRELIMINARY_NOTICE = "Предварительная KYC-оценка. Решение по конкретному договору не принято."
GENERATOR_MARKER = "kyc-counterparty-check/html-chromium-v1.1"


def normalize(value: Any) -> str:
    text = str(value if value is not None else "")
    return re.sub(r"[\u2010-\u2015\u2212]", "-", text).strip()


def esc(value: Any) -> str:
    return html.escape(normalize(value), quote=True)


def evidence_label(value: Any) -> str:
    code = normalize(value)
    return EVIDENCE_LABELS.get(code, "Не подтверждено")


def validate_data(data: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_TOP_LEVEL - set(data))
    if missing:
        raise ValueError(f"Missing required top-level fields: {', '.join(missing)}")

    for group, fields in {
        "metadata": {"report_title", "report_date", "source_cutoff_date", "version", "jurisdiction", "evidence_sufficiency", "document_status"},
        "counterparty": {"name", "inn", "ogrn", "kpp", "role", "purpose"},
        "deal_profile": {"assessment_scope", "completeness", "subject", "price", "advance", "stages", "payment_terms", "acceptance_terms", "open_exposure", "security", "funding_source", "missing_critical", "proposed_limit", "limit_basis", "limit_approval"},
        "executive_summary": {"overall_risk", "risk_label", "decision", "key_question", "reasons", "critical_risks", "preconditions"},
    }.items():
        absent = sorted(fields - set(data[group]))
        if absent:
            raise ValueError(f"Missing fields in {group}: {', '.join(absent)}")

    risk = data["executive_summary"]["overall_risk"]
    if not isinstance(risk, int) or not 1 <= risk <= 10:
        raise ValueError("executive_summary.overall_risk must be an integer from 1 to 10")
    scores = data["scores"]
    if not isinstance(scores, list) or len(scores) != 5:
        raise ValueError("scores must contain exactly five factor scores")
    for item in scores:
        if not isinstance(item.get("score"), int) or not 1 <= item["score"] <= 10:
            raise ValueError("Every score must be an integer from 1 to 10")

    profile = data["deal_profile"]
    if profile["assessment_scope"] not in {"preliminary", "final"}:
        raise ValueError("deal_profile.assessment_scope must be preliminary or final")
    if profile["assessment_scope"] == "preliminary":
        if not profile.get("missing_critical"):
            raise ValueError("preliminary assessment requires missing_critical")
        if data["executive_summary"]["decision"] == "Допустить":
            raise ValueError("preliminary assessment cannot use an unconditional Допустить decision")
    statuses = [item.get("status") for item in data["sources"]]
    statuses += [item.get("evidence_status") for item in data["source_coverage"]]
    unknown = sorted({status for status in statuses if status not in EVIDENCE_LABELS})
    if unknown:
        raise ValueError(f"Unknown evidence statuses: {', '.join(map(str, unknown))}")
    if re.search(r"\d", normalize(profile.get("proposed_limit"))):
        basis = normalize(profile.get("limit_basis")).lower()
        if not basis or "не представлен" in basis or "не установ" in basis:
            raise ValueError("A numeric proposed limit requires a documented basis")


def risk_class(score: int) -> str:
    if score <= 3:
        return "low"
    if score <= 6:
        return "medium"
    return "high"


def level_class(level: str) -> str:
    lowered = normalize(level).lower()
    if "низ" in lowered:
        return "low"
    if "выс" in lowered or "крит" in lowered:
        return "high"
    return "medium"


def bullets(items: Iterable[Any]) -> str:
    values = [f"<li>{esc(item)}</li>" for item in items if normalize(item)]
    return f"<ul>{''.join(values)}</ul>" if values else "<p class=\"muted\">Не установлено по представленным материалам.</p>"


def numbered(items: Iterable[Any]) -> str:
    values = [f"<li>{esc(item)}</li>" for item in items if normalize(item)]
    return f"<ol>{''.join(values)}</ol>" if values else "<p class=\"muted\">Не установлено по представленным материалам.</p>"


def table(headers: list[str], rows: Iterable[Iterable[Any]], classes: list[str] | None = None) -> str:
    classes = classes or [""] * len(headers)
    head = "".join(f"<th class=\"{esc(classes[i])}\">{esc(value)}</th>" for i, value in enumerate(headers))
    body_rows = []
    for row in rows:
        values = list(row)
        cells = "".join(
            f"<td class=\"{esc(classes[i] if i < len(classes) else '')}\">{esc(value)}</td>"
            for i, value in enumerate(values)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows:
        body_rows.append(f"<tr><td colspan=\"{len(headers)}\">Не установлено по представленным материалам.</td></tr>")
    return f"<table class=\"data-table\"><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def findings(items: Iterable[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for item in items:
        tone = item.get("tone", "neutral")
        title = item.get("title", "Аналитический вывод")
        parts = [f"<div class=\"finding {esc(tone)}\"><p><strong>{esc(title)}</strong></p>"]
        for label, key in (("Факт", "fact"), ("Источник", "source"), ("Анализ", "analysis"), ("Вывод", "conclusion")):
            if normalize(item.get(key)):
                parts.append(f"<p><span class=\"finding-label\">{label}:</span> {esc(item[key])}</p>")
        parts.append("</div>")
        rendered.append("".join(parts))
    return "".join(rendered)


def section(number: str, title: str, body: str) -> str:
    return (
        "<section class=\"major-section\">"
        f"<h2 class=\"section-title\"><span class=\"section-number\">{esc(number)}</span>{esc(title)}</h2>"
        f"{body}</section>"
    )


def render_cover(data: dict[str, Any]) -> str:
    meta = data["metadata"]
    cp = data["counterparty"]
    summary = data["executive_summary"]
    deal = data["deal_profile"]
    scope_label = "Предварительная" if deal["assessment_scope"] == "preliminary" else "Финальная"
    preliminary = f'<div class="preliminary-banner">{esc(PRELIMINARY_NOTICE)}</div>' if deal["assessment_scope"] == "preliminary" else ""
    return f"""
<section class="cover">
  <div class="report-kicker">{esc(meta['report_title'])}</div>
  <h1 class="company-name">{esc(cp['name'])}</h1>
  <div class="identifiers">ИНН {esc(cp['inn'])} &nbsp;|&nbsp; ОГРН {esc(cp['ogrn'])} &nbsp;|&nbsp; КПП {esc(cp['kpp'])}</div>
  {preliminary}
  <table class="summary-table"><tbody>
    <tr><td>Роль контрагента</td><td>{esc(cp['role'])}</td></tr>
    <tr><td>Цель проверки</td><td>{esc(cp['purpose'])}</td></tr>
    <tr><td>Дата среза источников</td><td>{esc(meta['source_cutoff_date'])} (дата отчёта: {esc(meta['report_date'])})</td></tr>
    <tr><td>Достаточность доказательств</td><td><span class="evidence-chip">{esc(meta['evidence_sufficiency'])}</span></td></tr>
    <tr><td>Статус оценки</td><td>{esc(scope_label)}; полнота параметров сделки: {esc(deal['completeness'])}</td></tr>
    <tr><td>Итоговая оценка</td><td><strong>Общий риск: {summary['overall_risk']}/10 - {esc(summary['risk_label'])}.</strong> {esc(summary.get('one_line_conclusion', 'Решение приведено с учётом доступных доказательств и параметров сделки.'))}</td></tr>
  </tbody></table>
  <div class="risk-hero">
    <div class="risk-number">{summary['overall_risk']} / 10</div>
    <div class="risk-label">{esc(summary['risk_label'])}</div>
    <div class="decision-stamp">Решение: {esc(summary['decision'])}</div>
  </div>
  <div class="key-question">Ключевой вопрос проверки: {esc(summary['key_question'])}</div>
  <div class="grid-2">
    <div class="card"><h3>Основные причины решения</h3>{bullets(summary['reasons'])}</div>
    <div>
      <div class="card"><h3>Критические риски</h3>{bullets(summary['critical_risks'])}</div>
      <div class="card"><h3>Обязательные меры до договора</h3>{bullets(summary['preconditions'])}</div>
    </div>
  </div>
</section>"""


def render_sources(data: dict[str, Any]) -> str:
    rows = [[x.get("name", ""), x.get("date", ""), evidence_label(x.get("status", "")), x.get("result", "")] for x in data["sources"]]
    coverage = []
    for item in data["source_coverage"]:
        required = "Обязательный" if item.get("required") else "Дополнительный"
        checked = "Проверен" if item.get("checked") else "Не проверен"
        coverage.append([
            f"{item.get('source', '')} ({required})",
            checked,
            item.get("date", ""),
            evidence_label(item.get("evidence_status", "")),
            item.get("decision_impact", ""),
        ])
    body = (
        "<p>Проверка выполнена по предоставленным документам и доступным источникам. "
        "Идентификация контрагента проведена по ИНН и ОГРН; одноимённые лица не объединялись без подтверждения связи.</p>"
        + table(["Документ / источник", "Дата", "Статус доказательства", "Результат"], rows)
        + "<h3 class=\"subheading\">1.1. Покрытие источников</h3>"
        + table(["Источник", "Проверка", "Дата", "Качество", "Влияние на решение"], coverage)
        + findings(data.get("source_findings", []))
    )
    return section("1", "Методика проверки и источники", body)


def render_general(data: dict[str, Any]) -> str:
    rows = [[x.get("indicator", ""), x.get("data", ""), x.get("assessment", "")] for x in data["general_info"]]
    return section("2", "Общие сведения", table(["Показатель", "Данные", "Оценка"], rows))


def render_financial(data: dict[str, Any]) -> str:
    block = data["financial_analysis"]
    periods = block.get("periods", ["Период 1", "Период 2"])
    rows = []
    for item in block.get("rows", []):
        values = list(item.get("values", []))
        while len(values) < 2:
            values.append("не установлено")
        rows.append([item.get("indicator", ""), values[0], values[1], item.get("comment", "")])
    score = int(block["score"])
    body = (
        table(["Показатель", periods[0], periods[1], "Комментарий"], rows, ["", "num", "num", ""])
        + findings(block.get("findings", []))
        + f"<h3 class=\"subheading\">Оценка финансовой устойчивости</h3>"
        + f"<div class=\"score-card {risk_class(score)} keep-together\"><div class=\"score-value\">{score}/10</div><div class=\"score-name\">{esc(block.get('score_label', 'Финансовый риск'))}</div></div>"
        + f"<p><strong>Вывод:</strong> {esc(block['conclusion'])}</p>"
    )
    return section("3", "Финансовый анализ", body)


def render_legal(data: dict[str, Any]) -> str:
    block = data["legal_history"]
    checks = [[x.get("registry", ""), x.get("result", ""), x.get("assessment", "")] for x in block.get("checks", [])]
    namesakes = [[x.get("entity", ""), x.get("facts", ""), x.get("conclusion", "")] for x in block.get("namesakes", [])]
    body = table(["Проверенный реестр / показатель", "Результат", "Оценка"], checks) + findings(block.get("findings", []))
    if namesakes:
        body += "<h3 class=\"subheading\">Одноимённые, но не связанные организации</h3>" + table(["Организация", "Установленные сведения", "Вывод"], namesakes)
    body += f"<p><strong>Оценка юридической истории: {int(block['score'])}/10.</strong> {esc(block['conclusion'])}</p>"
    return section("4", "Юридическая история", body)


def render_related(data: dict[str, Any]) -> str:
    block = data["related_parties"]
    rows = [[x.get("entity", ""), x.get("role", ""), x.get("status", "")] for x in block.get("rows", [])]
    body = f"<p>{esc(block.get('overview', ''))}</p>" + table(["Организация / лицо", "Роль", "Статус и значение"], rows)
    body += findings(block.get("findings", []))
    body += f"<p><strong>Оценка руководителей и связанных лиц: {int(block['score'])}/10.</strong> {esc(block['conclusion'])}</p>"
    return section("5", "Руководители и связанные лица", body)


def render_specialized(data: dict[str, Any]) -> str:
    block = data["specialized_analysis"]
    deal = data["deal_profile"]
    deal_rows = [
        ["Предмет", deal.get("subject", ""), "Цена", deal.get("price", "")],
        ["Аванс", deal.get("advance", ""), "Этапы", deal.get("stages", "")],
        ["Срок оплаты", deal.get("payment_terms", ""), "Приёмка", deal.get("acceptance_terms", "")],
        ["Открытая экспозиция", deal.get("open_exposure", ""), "Обеспечение", deal.get("security", "")],
        ["Источник финансирования", deal.get("funding_source", ""), "Полнота", deal.get("completeness", "")],
        ["Предлагаемый лимит", deal.get("proposed_limit", ""), "Основание / согласование", f"{deal.get('limit_basis', '')}; {deal.get('limit_approval', '')}"],
    ]
    scope_note = PRELIMINARY_NOTICE if deal["assessment_scope"] == "preliminary" else "Параметры сделки достаточны для финального решения."
    tone = "warning" if deal["assessment_scope"] == "preliminary" else "positive"
    chunks = [
        "<h3 class=\"subheading\">6.1. Профиль сделки</h3>",
        f"<div class=\"callout {tone}\"><strong>Статус:</strong> {esc(scope_note)}</div>",
        table(["Параметр", "Значение", "Параметр", "Значение"], deal_rows),
        f"<p><strong>Критически недостаёт:</strong> {esc('; '.join(deal.get('missing_critical', [])) or 'ничего')}</p>",
        f"<p><strong>Роль:</strong> {esc(block.get('role', data['counterparty']['role']))}</p>",
    ]
    for index, item in enumerate(block.get("subsections", []), start=1):
        chunks.append(f"<h3 class=\"subheading\">6.{index + 1}. {esc(item.get('title', 'Анализ'))}</h3>")
        for paragraph in item.get("paragraphs", []):
            chunks.append(f"<p>{esc(paragraph)}</p>")
        if item.get("rows"):
            chunks.append(table(item.get("headers", ["Показатель", "Комментарий"]), item["rows"]))
        if item.get("callout"):
            chunks.append(f"<div class=\"callout warning\"><strong>Рабочая интерпретация:</strong> {esc(item['callout'])}</div>")
    chunks.append(f"<p><strong>Оценка специализированного фактора: {int(block['score'])}/10.</strong> {esc(block['conclusion'])}</p>")
    return section("6", f"Специализированный анализ - контрагент в роли {data['counterparty']['role'].lower()}", "".join(chunks))


def render_risks(data: dict[str, Any]) -> str:
    risk_rows = []
    for item in data["risk_map"]:
        cls = level_class(item.get("level", ""))
        level = f"<span class=\"risk-tag {cls}\">{esc(item.get('level', ''))}</span>"
        risk_rows.append([esc(item.get("risk", "")), level, esc(item.get("importance", "")), esc(item.get("control", "")), esc(item.get("preventive", ""))])
    head = "".join(f"<th>{esc(x)}</th>" for x in ["Риск", "Уровень", "Почему важно", "Мера контроля", "Превентивная мера в договор"])
    rows_html = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in risk_rows)
    risk_table = f"<table class=\"risk-table\"><thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table>"

    cards = []
    for item in data["scores"]:
        score = int(item["score"])
        cards.append(f"<div class=\"score-card {risk_class(score)}\"><div class=\"score-value\">{score}/10</div><div class=\"score-name\">{esc(item['name'])}</div></div>")
    summary = data["executive_summary"]
    conditions = data["decision_conditions"]
    options = [[x.get("decision", ""), x.get("condition", ""), x.get("status", "")] for x in conditions.get("options", [])]
    body = risk_table
    body += "<h3 class=\"subheading\">7.1. Итоговая оценка</h3><div class=\"score-grid\">" + "".join(cards) + "</div>"
    body += f"<div class=\"risk-hero\"><div class=\"risk-number\">{summary['overall_risk']} / 10</div><div class=\"risk-label\">{esc(summary['risk_label'])}</div><div class=\"decision-stamp\">{esc(summary['decision'])}</div></div>"
    body += f"<div class=\"callout positive\"><strong>Когда договор допустим.</strong> {esc(conditions.get('when_admissible', ''))}</div>"
    body += f"<div class=\"callout critical\"><strong>Когда требуется углублённая проверка или отказ.</strong> {esc(conditions.get('when_escalate', ''))}</div>"
    body += table(["Решение", "Условие", "Статус"], options)
    body += f"<div class=\"status-box\"><h3>Ограничения и дальнейший контроль</h3><p>{esc(conditions.get('monitoring', ''))}</p></div>"
    return section("7", "Ключевые риски и итоговая оценка", body)


def render_documents(data: dict[str, Any]) -> str:
    block = data["documents_conditions"]
    body = "<h3 class=\"subheading\">8.1. Обязательный пакет документов</h3>" + numbered(block.get("required", []))
    body += "<h3 class=\"subheading\">8.2. Дополнительные документы по выявленным фактам</h3>" + numbered(block.get("additional", []))
    body += "<h3 class=\"subheading\">8.3. Рекомендуемые договорные условия</h3>" + numbered(block.get("clauses", []))
    body += "<h3 class=\"subheading\">8.4. Матрица договорных мер</h3>"
    for item in data["control_matrix"]:
        body += (
            "<div class=\"control-card\">"
            f"<h4>{esc(item.get('risk', 'Риск'))}</h4>"
            f"<p><strong>Пункт / контроль:</strong> {esc(item.get('contract_control', ''))}</p>"
            f"<p><strong>Триггер:</strong> {esc(item.get('trigger', ''))}</p>"
            f"<p><strong>Доказательство:</strong> {esc(item.get('required_evidence', ''))}</p>"
            f"<p><strong>Владелец и мониторинг:</strong> {esc(item.get('owner', ''))}; {esc(item.get('monitoring', ''))}</p>"
            f"<p><strong>Последствие:</strong> {esc(item.get('consequence', ''))}</p>"
            "</div>"
        )
    body += f"<div class=\"callout warning\"><strong>Условие допуска к договору:</strong> {esc(block.get('admission_condition', ''))}</div>"
    return section("8", "Документы и условия до заключения договора", body)


def render_enhanced(data: dict[str, Any]) -> str:
    block = data["enhanced_due_diligence"]
    meta = data["metadata"]
    body = numbered(block.get("steps", []))
    document_status = (
        f"{meta['document_status']} Юрисдикция: {meta['jurisdiction']}. "
        f"Достаточность доказательств: {meta['evidence_sufficiency']}. Версия: {meta['version']}."
    )
    limitations = " ".join(data.get("limitations", []))
    body += (
        "<div class=\"status-box\"><h3>Методологическая оговорка и статус документа</h3>"
        f"<p>{esc(document_status)}</p><p>{esc(block.get('disclaimer', ''))}</p>"
        f"<p>{esc(limitations)}</p></div>"
    )
    body += "<p class=\"document-end\">Конец отчёта.</p>"
    return section("9", "Углублённая проверка", body)


def build_html(data: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    css = STYLE_PATH.read_text(encoding="utf-8")
    css = css.replace("{{FONT_REGULAR_DATA}}", base64.b64encode(FONT_REGULAR.read_bytes()).decode("ascii"))
    css = css.replace("{{FONT_SEMIBOLD_DATA}}", base64.b64encode(FONT_SEMIBOLD.read_bytes()).decode("ascii"))
    css = css.replace("{{RUNNING_COMPANY}}", normalize(data["counterparty"]["name"]).replace('"', "'"))
    css = css.replace("{{RUNNING_INN}}", normalize(data["counterparty"]["inn"]))
    css = css.replace("{{REPORT_DATE}}", normalize(data["metadata"]["report_date"]))
    sections = "".join(
        [
            render_sources(data),
            render_general(data),
            render_financial(data),
            render_legal(data),
            render_related(data),
            render_specialized(data),
            render_risks(data),
            render_documents(data),
            render_enhanced(data),
        ]
    )
    return (
        template.replace("{{DOCUMENT_TITLE}}", esc(data["metadata"]["report_title"] + " - " + data["counterparty"]["name"]))
        .replace("{{INLINE_CSS}}", css)
        .replace("{{COVER_PAGE}}", render_cover(data))
        .replace("{{REPORT_SECTIONS}}", sections)
    )


def find_chromium(explicit: str | None = None) -> Path:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("KYC_CHROME_PATH"):
        candidates.append(os.environ["KYC_CHROME_PATH"])
    candidates.extend(
        [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    )
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise RuntimeError("Chromium browser not found. Set KYC_CHROME_PATH to Chrome, Chromium or Edge.")


def add_metadata(pdf_path: Path, data: dict[str, Any]) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        return
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    writer.add_metadata(
        {
            "/Title": normalize(data["metadata"]["report_title"] + " - " + data["counterparty"]["name"]),
            "/Author": "KYC Counterparty Check",
            "/Subject": "Внутренний аналитический KYC-отчёт",
            "/KYCGenerator": GENERATOR_MARKER,
            "/KYCGeneratorVersion": "1.1",
        }
    )
    replacement = pdf_path.with_suffix(".metadata.pdf")
    with replacement.open("wb") as stream:
        writer.write(stream)
    replacement.replace(pdf_path)


def render_pdf(html_text: str, output_pdf: Path, chromium: Path) -> None:
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kyc-render-", dir=str(output_pdf.parent)) as temp_name:
        temp_dir = Path(temp_name)
        html_path = temp_dir / "report.html"
        profile = temp_dir / "browser-profile"
        html_path.write_text(html_text, encoding="utf-8")
        command = [
            str(chromium),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=2000",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={output_pdf.resolve()}",
            html_path.resolve().as_uri(),
        ]
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if result.returncode != 0 or not output_pdf.exists() or output_pdf.stat().st_size < 10_000:
            details = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Chromium failed to create PDF: {details}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_pdf", type=Path)
    parser.add_argument("--chromium", help="Explicit path to Chrome, Chromium or Edge")
    parser.add_argument("--keep-html", type=Path, help="Optional path for the rendered standalone HTML")
    args = parser.parse_args()

    data = json.loads(args.input_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Input JSON must contain one object")
    validate_data(data)
    html_text = build_html(data)
    if "{{" in html_text or "}}" in html_text:
        raise ValueError("Unresolved template placeholder detected")
    if args.keep_html:
        args.keep_html.parent.mkdir(parents=True, exist_ok=True)
        args.keep_html.write_text(html_text, encoding="utf-8")
    browser = find_chromium(args.chromium)
    render_pdf(html_text, args.output_pdf, browser)
    add_metadata(args.output_pdf, data)
    print(f"Created {args.output_pdf.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
