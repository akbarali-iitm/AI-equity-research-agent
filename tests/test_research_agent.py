import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from research_agent import (
    GeminiClient,
    ResearchError,
    ResearchWorkflow,
    _detect_numeric_conflicts,
    _finalize_brief,
    _validate_evidence,
    load_documents,
)


ROOT = Path(__file__).resolve().parents[1]


def evidence_entry(
    source_id: str,
    claim: str,
    quote: str,
    value: str | None = None,
    evidence_id: str = "E001",
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "claim": claim,
        "quote": quote,
        "stance": "context",
        "metric": "revenue from operations",
        "category": "financial_results",
        "fact_type": "reported_fact",
        "reporting_period": "Q1 FY27" if value else None,
        "value": value,
        "unit": "₹ crore" if value else None,
        "limitation": "Reported figure; does not establish future performance.",
        "evidence_id": evidence_id,
    }


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.prompts.append(user_prompt)
        return next(self.responses)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        result = json.dumps(self.payload).encode("utf-8")
        return result if size < 0 else result[:size]


class ResearchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = load_documents(ROOT)

    def test_manifest_loads_only_intended_sources_with_stable_ids(self) -> None:
        self.assertEqual(len(self.documents), 8)
        self.assertEqual(self.documents[0].source_id, "S1")
        self.assertEqual(self.documents[-1].source_id, "S8")
        self.assertEqual(
            [document.source_id for document in load_documents(ROOT)],
            [document.source_id for document in self.documents],
        )
        self.assertNotIn("candidate.zip", {document.filename for document in self.documents})

    def test_evidence_requires_verbatim_quote_and_supported_value(self) -> None:
        row = evidence_entry(
            "S3",
            "Q1 FY27 revenue was ₹1,248 crore.",
            "Revenue from operations | 1,248 | 1,058 | +18.0%",
            "1,248",
        )
        evidence = {"evidence": [row]}
        validated = _validate_evidence(evidence, self.documents)
        self.assertEqual(validated[0]["evidence_id"], "E001")
        self.assertEqual(validated[0]["published"], "2026-08-08")

        unsupported = dict(row, value="1,428")
        with self.assertRaisesRegex(ResearchError, "not supported by its quote"):
            _validate_evidence({"evidence": [unsupported]}, self.documents)

    def test_numeric_conflicts_are_detected_across_sources_for_same_period(self) -> None:
        entries = [
            evidence_entry(
                "S1",
                "Reported revenue from operations was ₹1,428 crore.",
                "Revenue from operations rose to **₹1,428 crore**, a 35% increase from the year-ago period, the company said in an exchange filing on Friday. Net profit rose about 6% to ₹82 crore.",
                "1,428",
                "E001",
            ),
            evidence_entry(
                "S3",
                "Reported revenue from operations was ₹1,248 crore.",
                "Revenue from operations | 1,248 | 1,058 | +18.0%",
                "1,248",
                "E002",
            ),
        ]
        entries[0]["reporting_period"] = "April–June quarter 2026"
        validated = _validate_evidence({"evidence": entries}, self.documents)

        conflicts = _detect_numeric_conflicts(validated)

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["kind"], "conflict")
        self.assertEqual(conflicts[0]["source_ids"], ["S1", "S3"])
        self.assertTrue(conflicts[0]["must_appear_in_open_questions"])

    def test_finalizer_rejects_unsupported_numbers_and_appends_mandatory_findings(self) -> None:
        evidence = [
            evidence_entry(
                "S3",
                "Revenue was ₹1,248 crore.",
                "Revenue from operations | 1,248 | 1,058 | +18.0%",
                "1,248",
            )
        ]
        finding = {
            "finding_id": "F1",
            "kind": "conflict",
            "summary": "Secondary reporting gives a different revenue figure.",
            "source_ids": ["S1", "S3"],
            "must_appear_in_open_questions": True,
        }
        report = {
            "Snapshot": [{"text": "Q1 FY27 revenue was 1,248 crore.", "evidence_ids": ["E001"]}],
            "Bull case": [{"text": "Growth is promising.", "evidence_ids": ["E001"]}],
            "Bear case": [{"text": "Results do not establish future performance.", "evidence_ids": ["E001"]}],
            "Open questions": [{"text": "What explains the two figures?", "evidence_ids": ["E001"]}],
        }
        result = _finalize_brief(report, self.documents, evidence, [finding])
        self.assertIn("[S1]", result)
        self.assertIn("[S3]", result)
        self.assertIn("Source review flagged: Secondary reporting gives a different revenue figure.", result)
        self.assertIn("## Sources", result)

        with self.assertRaisesRegex(ResearchError, "unsupported numeric value"):
            bad_report = json.loads(json.dumps(report))
            bad_report["Snapshot"][0]["text"] = "Q1 FY27 revenue was 1,428 crore."
            _finalize_brief(bad_report, self.documents, evidence, [finding])

        with self.assertRaisesRegex(ResearchError, "unsupported numeric values"):
            bad_finding = dict(finding, summary="The difference is 1,428 crore.")
            _finalize_brief(report, self.documents, evidence, [bad_finding])

    def test_finalizer_deduplicates_conflict_already_covered_by_writer(self) -> None:
        evidence = [
            evidence_entry(
                "S1",
                "Reported revenue from operations was ₹1,428 crore.",
                "Revenue from operations rose to **₹1,428 crore**, a 35% increase from the year-ago period, the company said in an exchange filing on Friday. Net profit rose about 6% to ₹82 crore.",
                "1,428",
                "E001",
            ),
            evidence_entry(
                "S3",
                "Reported revenue from operations was ₹1,248 crore.",
                "Revenue from operations | 1,248 | 1,058 | +18.0%",
                "1,248",
                "E002",
            ),
        ]
        evidence[0]["reporting_period"] = "April–June quarter 2026"
        finding = {
            "finding_id": "F1",
            "kind": "conflict",
            "summary": "Sources report different values for revenue in Q1 FY27.",
            "source_ids": ["S1", "S3"],
            "must_appear_in_open_questions": True,
        }
        report = {
            "Snapshot": [{"text": "Revenue was 1,248 crore, up 18.0%.", "evidence_ids": ["E002"]}],
            "Bull case": [{"text": "The order outlook is positive.", "evidence_ids": ["E002"]}],
            "Bear case": [{"text": "The two sources disagree on Q1 FY27 revenue.", "evidence_ids": ["E001", "E002"]}],
            "Open questions": [{"text": "Why does Q1 FY27 revenue differ between sources?", "evidence_ids": ["E001", "E002"]}],
        }

        result = _finalize_brief(report, self.documents, evidence, [finding])

        self.assertEqual(result.count("Source review flagged:"), 0)
        self.assertEqual(result.count("Why does Q1 FY27 revenue differ"), 1)
        self.assertIn("up 18.0%", result)

    def test_snapshot_requires_available_key_reported_metrics(self) -> None:
        evidence = [
            evidence_entry(
                "S3",
                "Q1 FY27 revenue from operations was ₹1,248 crore.",
                "Revenue from operations | 1,248 | 1,058 | +18.0%",
                "1,248",
                "E001",
            ),
            {
                **evidence_entry(
                    "S3",
                    "Q1 FY27 EBITDA was ₹140 crore.",
                    "EBITDA | 140 | 136 | +2.6%",
                    "140",
                    "E002",
                ),
                "metric": "EBITDA",
            },
            {
                **evidence_entry(
                    "S3",
                    "Q1 FY27 EBITDA margin was 11.2%.",
                    "EBITDA margin | 11.2% | 12.9% | −170 bps",
                    "11.2",
                    "E003",
                ),
                "metric": "EBITDA margin",
                "unit": "percent",
            },
            {
                **evidence_entry(
                    "S3",
                    "Q1 FY27 profit after tax was ₹82 crore.",
                    "Profit after tax | 82 | 77 | +5.9%",
                    "82",
                    "E004",
                ),
                "metric": "profit after tax",
            },
        ]
        report = {
            "Snapshot": [{"text": "Revenue was ₹1,248 crore.", "evidence_ids": ["E001"]}],
            "Bull case": [{"text": "The operating outlook is positive.", "evidence_ids": ["E001"]}],
            "Bear case": [{"text": "Results carry execution risks.", "evidence_ids": ["E001"]}],
            "Open questions": [{"text": "What remains uncertain?", "evidence_ids": ["E001"]}],
        }

        with self.assertRaisesRegex(ResearchError, "Snapshot omitted key reported result categories"):
            _finalize_brief(report, self.documents, evidence, [])

        report["Snapshot"] = [
            {
                "text": "Revenue was 1,248 crore and EBITDA was 140 crore.",
                "evidence_ids": ["E001", "E002"],
            },
            {
                "text": "EBITDA margin was 11.2%.",
                "evidence_ids": ["E003"],
            },
            {
                "text": "Profit after tax was 82 crore.",
                "evidence_ids": ["E004"],
            },
        ]
        result = _finalize_brief(report, self.documents, evidence, [])
        self.assertIn("EBITDA margin", result)

    def test_workflow_repairs_unsupported_writer_numbers_instead_of_omitting_them(self) -> None:
        evidence = {
            "evidence": [
                {
                    key: value
                    for key, value in evidence_entry(
                        "S3",
                        "Q1 FY27 revenue from operations was ₹1,248 crore, up 18.0%.",
                        "Revenue from operations | 1,248 | 1,058 | +18.0%",
                        "1,248",
                    ).items()
                    if key != "evidence_id"
                }
            ]
        }
        findings = {"findings": []}
        invalid_report = {
            "Snapshot": [{"text": "Revenue was ₹1,428 crore.", "evidence_ids": ["E001"]}],
            "Bull case": [{"text": "Revenue increased year on year.", "evidence_ids": ["E001"]}],
            "Bear case": [{"text": "Revenue is not a forecast.", "evidence_ids": ["E001"]}],
            "Open questions": [{"text": "What remains unclear?", "evidence_ids": ["E001"]}],
        }
        corrected_report = {
            **invalid_report,
            "Snapshot": [{"text": "Revenue was ₹1,248 crore, up 18%.", "evidence_ids": ["E001"]}],
        }
        client = FakeClient(
            [
                json.dumps(evidence),
                json.dumps(findings),
                json.dumps(invalid_report),
                json.dumps(corrected_report),
            ]
        )

        result = ResearchWorkflow(client, "test system prompt").run(
            "SRVCABLE", self.documents, "2026-09-23"
        )

        self.assertEqual(len(client.prompts), 4)
        self.assertIn("unsupported numeric value(s): 1428", client.prompts[-1])
        self.assertIn("Revenue was ₹1,248 crore, up 18%", result)
        self.assertNotIn("Verification limitation", result)

    def test_workflow_retries_one_malformed_agent_output_and_uses_structured_handoffs(self) -> None:
        evidence = {
            "evidence": [
                {
                    key: value
                    for key, value in evidence_entry(
                        "S3",
                        "Q1 FY27 revenue from operations was ₹1,248 crore.",
                        "Revenue from operations | 1,248 | 1,058 | +18.0%",
                        "1,248",
                    ).items()
                    if key != "evidence_id"
                }
            ]
        }
        findings = {
            "findings": [
                {
                    "finding_id": "F1",
                    "kind": "conflict",
                    "summary": "A news article reports a different Q1 revenue figure.",
                    "source_ids": ["S1", "S3"],
                    "must_appear_in_open_questions": True,
                }
            ]
        }
        report = {
            "Snapshot": [{"text": "Q1 FY27 revenue from operations was ₹1,248 crore.", "evidence_ids": ["E001"]}],
            "Bull case": [{"text": "The reported revenue increased year over year.", "evidence_ids": ["E001"]}],
            "Bear case": [{"text": "The reported figure is not a forecast.", "evidence_ids": ["E001"]}],
            "Open questions": [{"text": "Why does a news article give a different revenue figure?", "evidence_ids": ["E001"]}],
        }
        client = FakeClient(
            [
                '{"evidence":[]}',
                json.dumps(evidence),
                json.dumps(findings),
                json.dumps(report),
            ]
        )
        workflow = ResearchWorkflow(client, "test system prompt")

        result = workflow.run("SRVCABLE", self.documents, "2026-09-23")

        self.assertEqual(len(client.prompts), 4)
        self.assertIn("failed deterministic validation", client.prompts[1])
        self.assertIn("VALIDATED EVIDENCE", client.prompts[3])
        self.assertIn("STRUCTURED SKEPTIC FINDINGS", client.prompts[3])
        self.assertIn("Why does a news article", result)
        self.assertIn("Source review flagged:", result)
        self.assertIn("[S1]", result)

    @patch("research_agent.urlopen")
    def test_gemini_client_sends_generate_content_request(self, mocked_urlopen: MagicMock) -> None:
        mocked_urlopen.return_value = FakeResponse(
            {"candidates": [{"content": {"parts": [{"text": "Gemini answer"}]}}]}
        )
        client = GeminiClient("test-key", "gemini-2.5-flash-lite")

        result = client.complete("system rules", "user request")

        self.assertEqual(result, "Gemini answer")
        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/models/gemini-2.5-flash-lite:generateContent"))
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "system rules")
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], "user request")

    @patch("research_agent.time.sleep")
    @patch("research_agent.urlopen")
    def test_gemini_client_retries_rate_limit(
        self, mocked_urlopen: MagicMock, mocked_sleep: MagicMock
    ) -> None:
        from urllib.error import HTTPError

        rate_limit = HTTPError(
            "https://example.invalid",
            429,
            "rate limited",
            {"Retry-After": "0"},
            None,
        )
        mocked_urlopen.side_effect = [
            rate_limit,
            FakeResponse({"candidates": [{"content": {"parts": [{"text": "recovered"}]}}]}),
        ]

        result = GeminiClient("test-key", "gemini-2.5-flash-lite").complete("system", "input")

        self.assertEqual(result, "recovered")
        self.assertEqual(mocked_urlopen.call_count, 2)
        mocked_sleep.assert_called_once_with(0.0)


if __name__ == "__main__":
    unittest.main()
