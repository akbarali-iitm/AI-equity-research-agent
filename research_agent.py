"""Source-grounded, multi-agent research brief generator."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REQUIRED_SECTIONS = ("Snapshot", "Bull case", "Bear case", "Open questions")
MAX_BRIEF_WORDS = 650
SOURCE_PATTERN = re.compile(r"\[(S\d+)\]")
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?![A-Za-z0-9])")
VALID_STANCES = {"positive", "negative", "context"}
VALID_FACT_TYPES = {
    "reported_fact",
    "external_assertion",
    "estimate",
    "inference",
    "company_guidance",
}
VALID_FINDING_KINDS = {
    "conflict",
    "stale_information",
    "source_quality",
    "entity_mismatch",
    "unsupported_promotion",
    "other",
}
MAX_API_RETRIES = 3
MAX_REPAIR_ATTEMPTS = 3
MAX_DOCUMENT_BYTES = 100_000
MAX_TOTAL_SOURCE_BYTES = 500_000
MAX_REQUEST_BYTES = 1_000_000
MAX_RESPONSE_BYTES = 100_000


def _numeric_values(text: str) -> set[str]:
    values: set[str] = set()
    for value in NUMBER_PATTERN.findall(text):
        try:
            values.add(format(Decimal(value.replace(",", "")).normalize(), "f"))
        except InvalidOperation:
            values.add(value.replace(",", ""))
    return values


def _metric_family(text: str) -> str | None:
    families = _metric_families(text)
    for family in (
        "ebitda_margin",
        "revenue",
        "profit_after_tax",
        "order_book",
        "receivables",
        "ebitda",
    ):
        if family in families:
            return family
    return None


def _metric_families(text: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()
    families = set()
    if re.search(r"\b(ebitda\s+)?margin\b", normalized):
        families.add("ebitda_margin")
    if re.search(r"\bebitda\b", normalized):
        families.add("ebitda")
    if re.search(r"\b(revenue|sales|turnover)\b", normalized):
        families.add("revenue")
    if re.search(r"\b(profit after tax|net profit|pat)\b", normalized):
        families.add("profit_after_tax")
    if re.search(r"\b(order book|orders)\b", normalized):
        families.add("order_book")
    if re.search(r"\b(receivable days|receivables)\b", normalized):
        families.add("receivables")
    return families


def _period_parts(period: str) -> tuple[int, int | None] | None:
    normalized = period.casefold().replace("–", "-").replace("—", "-")
    quarter_match = re.search(r"\bq\s*([1-4])\b", normalized)
    fiscal_year_match = re.search(r"\bfy\s*(20\d{2}|\d{2})\b", normalized)
    quarter: int | None = int(quarter_match.group(1)) if quarter_match else None
    fiscal_year: int | None = None
    if fiscal_year_match:
        year_text = fiscal_year_match.group(1)
        fiscal_year = int(year_text)
        if fiscal_year < 100:
            fiscal_year += 2000

    if quarter is None:
        month_quarters = (
            (r"\b(apr(?:il)?|may|jun(?:e)?)\b", 1),
            (r"\b(jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?)\b", 2),
            (r"\b(oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", 3),
            (r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?)\b", 4),
        )
        for pattern, inferred_quarter in month_quarters:
            if re.search(pattern, normalized):
                quarter = inferred_quarter
                break

    if fiscal_year is None:
        year_match = re.search(r"\b(20\d{2})\b", normalized)
        if year_match and quarter is not None:
            calendar_year = int(year_match.group(1))
            fiscal_year = calendar_year if quarter == 4 else calendar_year + 1

    return (quarter, fiscal_year) if quarter is not None else None


def _evidence_conflict_keys(
    evidence: list[dict[str, Any]],
    source_ids: set[str] | None = None,
) -> set[tuple[str, str, tuple[str, ...]]]:
    records: list[tuple[str, int, int | None, str, str]] = []
    for item in evidence:
        source_id = item.get("source_id")
        if source_ids is not None and source_id not in source_ids:
            continue
        family = _metric_family(
            f"{item.get('metric', '')} {item.get('claim', '')}"
        )
        period = item.get("reporting_period")
        value = item.get("value")
        if not family or not isinstance(period, str) or not period.strip() or not isinstance(value, str):
            continue
        parts = _period_parts(period)
        values = _numeric_values(value)
        if not parts:
            continue
        if not values:
            continue
        for numeric_value in values:
            records.append((family, parts[0], parts[1], source_id, numeric_value))

    conflicts: set[tuple[str, str, tuple[str, ...]]] = set()
    for index, left in enumerate(records):
        for right in records[index + 1 :]:
            left_family, left_quarter, left_year, left_source, left_value = left
            right_family, right_quarter, right_year, right_source, right_value = right
            if (
                left_family == right_family
                and left_quarter == right_quarter
                and (left_year is None or right_year is None or left_year == right_year)
                and left_source != right_source
                and left_value != right_value
            ):
                fiscal_year = left_year if left_year is not None else right_year
                period_key = f"q{left_quarter}-fy{fiscal_year}" if fiscal_year else f"q{left_quarter}"
                conflicts.add(
                    (left_family, period_key, tuple(sorted({left_source, right_source})))
                )
    return conflicts


class ResearchError(Exception):
    """Raised when the source pack or an agent response is invalid."""


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    filename: str
    text: str
    source: str
    url: str
    published: str
    document_type: str


class CompletionClient(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> str: ...


class GeminiClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = json.dumps(
            {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": {"temperature": 0.1},
            }
        ).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise ResearchError(
                f"Gemini request is {len(payload)} bytes; limit is {MAX_REQUEST_BYTES}"
            )
        request = Request(
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/{self.model}:generateContent",
            data=payload,
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(MAX_API_RETRIES + 1):
            try:
                with urlopen(request, timeout=90) as response:
                    response_body = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(response_body) > MAX_RESPONSE_BYTES:
                        raise ResearchError(
                            f"Gemini response exceeds {MAX_RESPONSE_BYTES} bytes"
                        )
                    result = json.loads(response_body.decode("utf-8"))
                break
            except HTTPError as exc:
                detail = exc.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
                if exc.code not in {429, 500, 502, 503, 504} or attempt == MAX_API_RETRIES:
                    raise ResearchError(
                        f"Gemini API returned HTTP {exc.code}: {detail}"
                    ) from exc
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = min(30.0, max(0.0, float(retry_after))) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(delay)
            except (URLError, TimeoutError) as exc:
                if attempt == MAX_API_RETRIES:
                    raise ResearchError(f"Could not reach the Gemini API: {exc}") from exc
                time.sleep(2**attempt)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ResearchError(f"Gemini API returned invalid JSON: {exc}") from exc
        else:
            raise ResearchError("Gemini API request exhausted its retry limit")

        if not isinstance(result, dict):
            raise ResearchError("Gemini API response must be a JSON object")
        try:
            parts = result["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            feedback = result.get("promptFeedback", {})
            block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
            if block_reason:
                raise ResearchError(
                    f"Gemini blocked the prompt: {block_reason}"
                ) from exc
            raise ResearchError("Gemini API response is missing candidate content") from exc
        if not isinstance(parts, list):
            raise ResearchError("Gemini API response has invalid candidate parts")
        content = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
        if not content.strip():
            raise ResearchError("Gemini returned no text; the response may have been blocked")
        return content.strip()


def _parse_json_response(response: str, agent_name: str) -> dict[str, Any]:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResearchError(f"{agent_name} returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ResearchError(f"{agent_name} must return a JSON object")
    return value


def load_documents(
    directory: Path, manifest_path: Path | None = None
) -> list[SourceDocument]:
    if not directory.is_dir():
        raise ResearchError(f"Document directory does not exist: {directory}")
    manifest_file = manifest_path or Path(__file__).with_name("source_manifest.json")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResearchError(f"Could not read source manifest {manifest_file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ResearchError(f"Source manifest is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest:
        raise ResearchError("Source manifest must be a non-empty filename-to-ID object")
    if any(not isinstance(name, str) or not isinstance(source_id, str) for name, source_id in manifest.items()):
        raise ResearchError("Source manifest entries must map filenames to source IDs")
    if len(set(manifest.values())) != len(manifest):
        raise ResearchError("Source manifest contains duplicate source IDs")
    if any(
        Path(filename).name != filename
        or not filename.endswith(".md")
        or not re.fullmatch(r"S\d+", source_id)
        for filename, source_id in manifest.items()
    ):
        raise ResearchError("Manifest filenames or source IDs are invalid")

    documents: list[SourceDocument] = []
    total_bytes = 0
    for filename, source_id in manifest.items():
        path = directory / filename
        if not path.is_file():
            raise ResearchError(f"Manifest source file is missing: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ResearchError(f"Could not read source file {path}: {exc}") from exc
        source_bytes = len(text.encode("utf-8"))
        if source_bytes > MAX_DOCUMENT_BYTES:
            raise ResearchError(
                f"{path} is {source_bytes} bytes; per-document limit is {MAX_DOCUMENT_BYTES}"
            )
        total_bytes += source_bytes
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ResearchError(
                f"Source pack exceeds the {MAX_TOTAL_SOURCE_BYTES}-byte total limit"
            )
        match = re.match(r"\A---\s*\n(.*?)\n---\s*", text, flags=re.DOTALL)
        if not match:
            raise ResearchError(f"Missing source metadata front matter: {path}")
        metadata: dict[str, str] = {}
        for line in match.group(1).splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip()
        missing = [key for key in ("source", "url", "published", "type") if not metadata.get(key)]
        if missing:
            raise ResearchError(f"{path} is missing metadata fields: {', '.join(missing)}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["published"]):
            raise ResearchError(f"{path} has invalid publication date: {metadata['published']}")
        try:
            time.strptime(metadata["published"], "%Y-%m-%d")
        except ValueError as exc:
            raise ResearchError(
                f"{path} has invalid publication date: {metadata['published']}"
            ) from exc
        if not re.match(r"^https?://\S+$", metadata["url"]):
            raise ResearchError(f"{path} has invalid URL metadata: {metadata['url']}")
        documents.append(
            SourceDocument(
                source_id=source_id,
                filename=path.name,
                text=text,
                source=metadata["source"],
                url=metadata["url"],
                published=metadata["published"],
                document_type=metadata["type"],
            )
        )
    return documents


def _source_bundle(documents: list[SourceDocument]) -> str:
    return json.dumps(
        [
            {
                "source_id": document.source_id,
                "filename": document.filename,
                "published": document.published,
                "source": document.source,
                "url": document.url,
                "type": document.document_type,
                "untrusted_document_text": document.text,
            }
            for document in documents
        ],
        ensure_ascii=False,
    )


def _validate_evidence(
    evidence: dict[str, Any], documents: list[SourceDocument]
) -> list[dict[str, Any]]:
    by_id = {document.source_id: document for document in documents}
    entries = evidence.get("evidence")
    if not isinstance(entries, list) or not entries:
        raise ResearchError("Evidence analyst must return a non-empty 'evidence' array")
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    required_fields = {
        "source_id",
        "claim",
        "quote",
        "stance",
        "metric",
        "category",
        "fact_type",
        "reporting_period",
        "value",
        "unit",
        "limitation",
    }
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise ResearchError("Evidence entries must be JSON objects")
        missing = required_fields - item.keys()
        if missing:
            raise ResearchError(
                f"Evidence entry {index} is missing fields: {', '.join(sorted(missing))}"
            )
        source_id = item.get("source_id")
        quote = item.get("quote")
        claim = item.get("claim")
        if not isinstance(source_id, str) or source_id not in by_id:
            raise ResearchError(f"Evidence analyst cited unknown source: {source_id}")
        if not isinstance(quote, str) or not quote.strip():
            raise ResearchError(f"Evidence from {source_id} must include an exact quote")
        if quote not in by_id[source_id].text:
            raise ResearchError(f"Evidence quote does not appear verbatim in {source_id}")
        if not isinstance(claim, str) or not claim.strip():
            raise ResearchError(f"Evidence from {source_id} must include a claim")
        # if not isinstance(item["stance"], str) or item["stance"] not in VALID_STANCES:
        #     raise ResearchError(f"Evidence from {source_id} has invalid stance")
        stance = item.get("stance")

        if isinstance(stance, str):
            stance = stance.strip().lower()

        if stance not in VALID_STANCES:
            raise ResearchError(
                f"Evidence from {source_id} has invalid stance "
                f"{item.get('stance')!r}. "
                f"Expected one of: {', '.join(sorted(VALID_STANCES))}"
            )

        item["stance"] = stance
        
        if not isinstance(item["fact_type"], str) or item["fact_type"] not in VALID_FACT_TYPES:
            raise ResearchError(f"Evidence from {source_id} has invalid fact_type")
        if not isinstance(item["category"], str) or not item["category"].strip():
            raise ResearchError(f"Evidence from {source_id} must include a category")
        if not isinstance(item["metric"], str) or not item["metric"].strip():
            raise ResearchError(f"Evidence from {source_id} must include a metric name")
        if not isinstance(item["limitation"], str) or not item["limitation"].strip():
            raise ResearchError(f"Evidence from {source_id} must include a limitation")
        for field in ("reporting_period", "value", "unit"):
            if item[field] is not None and not isinstance(item[field], str):
                raise ResearchError(f"Evidence field {field} must be text or null")
        if item["value"]:
            quoted_numbers = _numeric_values(quote)
            value_numbers = _numeric_values(item["value"])
            if not value_numbers or not value_numbers.issubset(quoted_numbers):
                raise ResearchError(
                    f"Numeric value in evidence from {source_id} is not supported by its quote"
                )
        key = (source_id, re.sub(r"\s+", " ", quote).strip().casefold())
        if key in seen:
            raise ResearchError(f"Evidence contains a duplicate quote from {source_id}")
        seen.add(key)
        validated.append(
            {
                **item,
                "evidence_id": f"E{index:03d}",
                "published": by_id[source_id].published,
                "source_type": by_id[source_id].document_type,
            }
        )
    evidence["evidence"] = validated
    return validated


def _detect_numeric_conflicts(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts = []
    for index, (metric, period, source_ids) in enumerate(
        sorted(_evidence_conflict_keys(evidence)), start=1
    ):
        match = re.fullmatch(r"q([1-4])(?:-fy(20\d{2}))?", period)
        if match:
            quarter = match.group(1)
            year = match.group(2)
            period_label = f"Q{quarter} FY{year[-2:]}" if year else f"Q{quarter}"
        else:
            period_label = period
        conflicts.append(
            {
                "finding_id": f"AUTO-NUMERIC-CONFLICT-{index:03d}",
                "kind": "conflict",
                "summary": (
                    f"Sources report different values for {metric.replace('_', ' ')} "
                    f"in {period_label}; confirm the applicable reported figure."
                ),
                "source_ids": list(source_ids),
                "must_appear_in_open_questions": True,
            }
        )
    return conflicts


def _finding_already_covered(
    finding: dict[str, Any],
    open_questions: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> bool:
    if finding["kind"] != "conflict":
        return False
    finding_source_ids = set(finding["source_ids"])
    finding_keys = _evidence_conflict_keys(evidence, finding_source_ids)
    if not finding_keys:
        return False

    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    for statement in open_questions:
        if not isinstance(statement, dict):
            continue
        evidence_ids = statement.get("evidence_ids")
        text = statement.get("text")
        if not isinstance(evidence_ids, list) or not isinstance(text, str):
            continue
        cited_records = [
            evidence_by_id[evidence_id]
            for evidence_id in evidence_ids
            if isinstance(evidence_id, str) and evidence_id in evidence_by_id
        ]
        cited_source_ids = {item["source_id"] for item in cited_records}
        if not finding_source_ids.issubset(cited_source_ids):
            continue
        text_family = _metric_family(text)
        if any(key[0] == text_family for key in finding_keys):
            return True
    return False


def _validate_findings(
    findings: dict[str, Any], documents: list[SourceDocument]
) -> list[dict[str, Any]]:
    entries = findings.get("findings")
    known_source_ids = {document.source_id for document in documents}
    if not isinstance(entries, list):
        raise ResearchError("Skeptic must return a 'findings' array")
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    required = {"finding_id", "kind", "summary", "source_ids", "must_appear_in_open_questions"}
    for entry in entries:
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ResearchError("Each skeptic finding must include all required fields")
        finding_id = entry["finding_id"]
        if not isinstance(finding_id, str) or not finding_id.strip() or finding_id in seen_ids:
            raise ResearchError("Skeptic finding IDs must be non-empty and unique")
        seen_ids.add(finding_id)
        if not isinstance(entry["kind"], str) or entry["kind"] not in VALID_FINDING_KINDS:
            raise ResearchError(f"Skeptic finding {finding_id} has an invalid kind")
        if not isinstance(entry["summary"], str) or not entry["summary"].strip():
            raise ResearchError(f"Skeptic finding {finding_id} needs a summary")
        source_ids = entry["source_ids"]
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(source_id, str) for source_id in source_ids)
            or any(source_id not in known_source_ids for source_id in source_ids)
        ):
            raise ResearchError(f"Skeptic finding {finding_id} has invalid source_ids")
        if not isinstance(entry["must_appear_in_open_questions"], bool):
            raise ResearchError(
                f"Skeptic finding {finding_id} must set must_appear_in_open_questions"
            )
        if entry["kind"] == "conflict" and not entry["must_appear_in_open_questions"]:
            raise ResearchError(
                f"Material conflict {finding_id} must be included under Open questions"
            )
        validated.append(entry)
    findings["findings"] = validated
    return validated


def _finalize_brief(
    report: dict[str, Any],
    documents: list[SourceDocument],
    evidence: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> str:
    if set(report) != set(REQUIRED_SECTIONS):
        raise ResearchError(
            "Writer must return exactly these sections: " + ", ".join(REQUIRED_SECTIONS)
        )
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    rendered: dict[str, list[str]] = {}
    for section_name in REQUIRED_SECTIONS:
        entries = report[section_name]
        if not isinstance(entries, list) or not entries:
            raise ResearchError(f"Writer left the {section_name} section empty or malformed")
        if section_name == "Snapshot" and len(entries) > 4:
            raise ResearchError("Snapshot must contain no more than four concise statements")
        rendered[section_name] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                raise ResearchError(f"Writer returned a malformed statement in {section_name}")
            text = entry["text"].strip()
            evidence_ids = entry.get("evidence_ids")
            if not text or not isinstance(evidence_ids, list) or not evidence_ids:
                raise ResearchError(
                    f"Every statement in {section_name} needs text and evidence_ids"
                )
            if "\n" in text or "\r" in text or SOURCE_PATTERN.search(text):
                raise ResearchError(
                    f"Statement in {section_name} must be one line; citations are attached by the application"
                )
            if any(not isinstance(item_id, str) for item_id in evidence_ids):
                raise ResearchError(f"evidence_ids in {section_name} must be strings")
            unknown_evidence = set(evidence_ids) - evidence_by_id.keys()
            if unknown_evidence:
                raise ResearchError(
                    f"Writer referenced unknown evidence IDs: {', '.join(sorted(unknown_evidence))}"
                )
            cited_evidence = [evidence_by_id[evidence_id] for evidence_id in evidence_ids]
            supported_numbers = {
                number
                for item in cited_evidence
                for number in _numeric_values(f"{item['claim']} {item['quote']}")
            }
            unsupported_numbers = _numeric_values(text) - supported_numbers
            if unsupported_numbers:
                raise ResearchError(
                    f"Unverified statement in {section_name}; unsupported numeric "
                    f"value(s): {', '.join(sorted(unsupported_numbers))}"
                )
            source_ids = {item["source_id"] for item in cited_evidence}
            citation_suffix = " ".join(f"[{source_id}]" for source_id in sorted(source_ids))
            rendered[section_name].append(f"- {text} {citation_suffix}")

    snapshot_categories: set[str] = set()
    for entry in report["Snapshot"]:
        text_categories = _metric_families(entry["text"])
        evidence_categories = {
            category
            for evidence_id in entry["evidence_ids"]
            for item in [evidence_by_id[evidence_id]]
            for category in _metric_families(f"{item['metric']} {item['claim']}")
        }
        snapshot_categories.update(text_categories & evidence_categories)
    available_snapshot_categories = {
        category
        for item in evidence
        if item.get("fact_type") == "reported_fact"
        for category in [_metric_family(f"{item['metric']} {item['claim']}")]
        if category in {"revenue", "ebitda", "ebitda_margin", "profit_after_tax"}
    }
    missing_snapshot_categories = available_snapshot_categories - snapshot_categories
    if missing_snapshot_categories:
        raise ResearchError(
            "Snapshot omitted key reported result categories: "
            + ", ".join(sorted(missing_snapshot_categories))
        )

    for finding in findings:
        if finding["must_appear_in_open_questions"]:
            related_evidence = [
                item for item in evidence if item["source_id"] in finding["source_ids"]
            ]
            supported_numbers = {
                number
                for item in related_evidence
                for number in _numeric_values(f"{item['claim']} {item['quote']}")
            }
            unsupported_numbers = _numeric_values(finding["summary"]) - supported_numbers
            if unsupported_numbers:
                raise ResearchError(
                    f"Skeptic finding {finding['finding_id']} has unsupported numeric values: "
                    + ", ".join(sorted(unsupported_numbers))
                )
            source_citations = " ".join(
                f"[{source_id}]" for source_id in sorted(finding["source_ids"])
            )
            if not _finding_already_covered(
                finding, report["Open questions"], evidence
            ):
                rendered["Open questions"].append(
                    f"- Source review flagged: {finding['summary']} {source_citations}"
                )

    source_lines = [
        f"- **[{document.source_id}] {document.source}** "
        f"({document.published}) — [{document.filename}]({document.url})"
        for document in documents
        if any(
            document.source_id in re.findall(r"\[(S\d+)\]", line)
            for lines in rendered.values()
            for line in lines
        )
    ]
    if not rendered["Open questions"]:
        rendered["Open questions"].append(
            "- No open questions were generated; manually review source coverage."
        )
    
    cleaned_sections = [
        f"## {name}\n" + "\n".join(rendered[name])
        for name in REQUIRED_SECTIONS
    ]
    cleaned_sections.append("## Sources\n" + "\n".join(source_lines))
    result = "\n\n".join(cleaned_sections).strip() + "\n"
    if len(result.split()) > MAX_BRIEF_WORDS:
        raise ResearchError(
            f"Brief is {len(result.split())} words; the limit is {MAX_BRIEF_WORDS}"
        )
    return result

def load_prompt(filename: str) -> str:
    prompt_path = Path(__file__).parent / "prompts" / filename
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ResearchError(
            f"Could not read prompt file {prompt_path}: {exc}"
        ) from exc


class ResearchWorkflow:
    def __init__(self, client: CompletionClient, system_prompt: str) -> None:
        self.client = client
        self.system_prompt = system_prompt

    def _complete_validated(
        self,
        agent_name: str,
        prompt: str,
        validator: Callable[[str], Any],
    ) -> Any:
        response = self.client.complete(self.system_prompt, prompt)

        for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
            try:
                return validator(response)

            except ResearchError as exc:
                if attempt == MAX_REPAIR_ATTEMPTS:
                    raise ResearchError(
                        f"{agent_name} failed validation after repair: {exc}"
                    ) from exc

                repair_prompt = (
                    f"Your previous {agent_name} response failed deterministic validation.\n"
                    f"Validation error: {exc}\n"
                    "Repair the response to satisfy the original request exactly. "
                    "Do not add unsupported facts, source IDs, evidence IDs, or findings. "
                    "Return only the required JSON object.\n\n"
                    f"ORIGINAL TASK:\n{prompt}\n\n"
                    f"INVALID RESPONSE:\n{response}"
                )

                response = self.client.complete(
                    self.system_prompt,
                    repair_prompt,
                )

        raise ResearchError(
            f"{agent_name} repair loop ended unexpectedly"
        )

    def run(
        self,
        ticker: str,
        documents: list[SourceDocument],
        as_of: str,
    ) -> str:
        # Load reusable agent instructions from the prompts folder.
        evidence_instructions = load_prompt(
            "evidence_prompt_v2.md"
        )
        skeptic_instructions = load_prompt(
            "challenge_prompt_v2.md"
        )
        writer_instructions = load_prompt(
            "brief_writer_prompt.md"
        )

        # Serialize the source documents for the agents.
        bundle = _source_bundle(documents)

        # --------------------------------------------------
        # 1. Evidence Analyst
        # --------------------------------------------------
        evidence_prompt = (
            f"{evidence_instructions}\n\n"
            f"Ticker: {ticker}\n"
            f"As-of date: {as_of}\n\n"
            f"UNTRUSTED SOURCE PACK:\n{bundle}"
        )

        evidence = self._complete_validated(
            "Evidence analyst",
            evidence_prompt,
            lambda response: _validate_evidence(
                _parse_json_response(response, "Evidence analyst"),
                documents,
            ),
        )

        # --------------------------------------------------
        # 2. Skeptic / Source-quality reviewer
        # --------------------------------------------------
        challenge_prompt = (
            f"{skeptic_instructions}\n\n"
            f"Ticker: {ticker}\n"
            f"As-of date: {as_of}\n\n"
            f"VALIDATED EVIDENCE:\n"
            f"{json.dumps(evidence, ensure_ascii=False)}\n\n"
            f"UNTRUSTED SOURCE PACK:\n{bundle}"
        )

        findings = self._complete_validated(
            "Skeptic agent",
            challenge_prompt,
            lambda response: _validate_findings(
                _parse_json_response(response, "Skeptic agent"),
                documents,
            ),
        )

        # Add deterministic numeric conflict findings when the
        # Skeptic has not already flagged the same source combination.
        for automatic_finding in _detect_numeric_conflicts(evidence):
            already_flagged = any(
                finding["kind"] == "conflict"
                and set(automatic_finding["source_ids"]).issubset(
                    finding["source_ids"]
                )
                for finding in findings
            )

            if not already_flagged:
                findings.append(automatic_finding)

        # --------------------------------------------------
        # 3. Brief Writer
        # --------------------------------------------------
        writer_prompt = (
            f"{writer_instructions}\n\n"
            f"Ticker: {ticker}\n"
            f"As-of date: {as_of}\n\n"
            f"VALIDATED EVIDENCE:\n"
            f"{json.dumps(evidence, ensure_ascii=False)}\n\n"
            f"STRUCTURED SKEPTIC FINDINGS:\n"
            f"{json.dumps(findings, ensure_ascii=False)}"
        )

        return self._complete_validated(
            "Brief writer",
            writer_prompt,
            lambda response: _finalize_brief(
                _parse_json_response(response, "Brief writer"),
                documents,
                evidence,
                findings,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a cited, source-grounded research brief from Markdown documents."
    )
    parser.add_argument("ticker", help="NSE ticker, for example SRVCABLE")
    parser.add_argument("--documents", type=Path, default=Path(__file__).parent)
    parser.add_argument("--as-of", default="2026-09-23")
    parser.add_argument("--output", type=Path, help="Write the brief to this path instead of stdout")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[A-Za-z0-9&.-]{1,20}", args.ticker):
        parser.error("ticker must be a short NSE symbol containing letters, digits, '.', '&' or '-'")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: set GEMINI_API_KEY before running the research agent.", file=sys.stderr)
        return 2
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    try:
        documents = load_documents(args.documents)
        prompt_path = Path(__file__).parent / "prompts" / "research_system_prompt_v2.md"
        system_prompt = prompt_path.read_text(encoding="utf-8")
        workflow = ResearchWorkflow(
            GeminiClient(api_key, model), system_prompt
        )
        brief = workflow.run(args.ticker.upper(), documents, args.as_of)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(brief, encoding="utf-8")
        else:
            sys.stdout.write(brief)
    except (OSError, ResearchError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
