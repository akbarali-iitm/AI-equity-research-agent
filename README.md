<!-- README.md -->
# Super Investing · AI Innovator Assignment

This project generates a short, cited research brief from a ticker and a folder of
Markdown source documents. The SRVCABLE company and its source pack are fictional;
the included documents are treated as the complete research universe.

## Assignment brief

## The task

Super Investing helps long-term investors research Indian stocks. Build a small **AI research agent**:

> **Input:** an NSE ticker, plus a set of documents about the company
> **Output:** a short **research brief** in Markdown

The brief should have these sections:

1. **Snapshot:** what the company does and its latest results, in 3–4 lines
2. **Bull case:** the strongest reasons to be positive
3. **Bear case:** the strongest reasons to be cautious
4. **Open questions:** what's unclear, missing or conflicting in the sources
5. **Sources:** every claim in the brief should be traceable to a source

Keep it to about one page. It's written for a retail investor, not an analyst.

## Agent design

The workflow uses three specialist agents in sequence:

1. **Evidence analyst** extracts typed claims, verbatim quotes, periods, units, values,
   and limitations, tied to stable source IDs.
2. **Skeptic** returns structured findings about conflicts, source quality, stale
   information, promotional claims, and same-name entities.
3. **Brief writer** returns claims linked to validated evidence IDs. The application,
   not the writer, attaches citations and includes mandatory skeptic findings in Open
   questions unless an equivalent conflict is already covered there.

The application validates the source manifest, metadata, quote exactness, typed
evidence, and reported numeric values. It detects differing values assigned to the
same metric and reporting period across sources, then includes the unresolved issue
in Open questions unless an equivalent question is already present. It rejects
unknown evidence IDs, unsupported numbers, missing sections, and overlong briefs; it
also requires each statement to link to evidence and generates the Sources list from
the actual cited sources. Invalid agent output
gets up to three repair attempts per stage, and transient Gemini errors receive
bounded retries. Agent instructions are in
[`prompts/research_system_prompt_v2.md`](./prompts/research_system_prompt_v2.md).
The source documents are data, not instructions; this is important because the pack
contains an embedded prompt-injection attempt.

## Run

Python 3.10 or later is required. The implementation uses only the Python standard
library. Create a Gemini API key in [Google AI Studio](https://aistudio.google.com/apikey).
From the project directory, set it in PowerShell and run:

```powershell
$env:GEMINI_API_KEY = "your-gemini-api-key"
python research_agent.py SRVCABLE --documents . --as-of 2026-09-23 --output brief.md
```

`--documents` points to a directory containing the filenames in
[`source_manifest.json`](./source_manifest.json). Only files named by this manifest
are read; unrelated Markdown files are ignored. Every source must have metadata
front matter with `source`, `url`, `published` (YYYY-MM-DD), and `type`. The ticker
is required. The default model is `gemini-3.5-flash-lite`; override it with
`GEMINI_MODEL` (for example, `gemini-3.5-flash` if available to your project).
There are three primary Gemini calls per run and up to three repair attempts for an
invalid response at each stage. API errors are reported instead of being silently
converted into a brief.

Gemini API keys have a free tier with model and rate limits; exact availability can
vary by model, region, and Google project. Check the current [Gemini API pricing and
free-tier details](https://ai.google.dev/gemini-api/docs/pricing). Free-tier data
may be used by Google to improve its products, so do not send confidential
documents. The bundled SRVCABLE test sources are fictional.

Run the offline tests without an API key:

```powershell
python -m unittest discover -s tests -v
```

The tests use a fake chat client to check the pipeline, citations, and source loading;
they do not count as live model runs for the assignment. See the experiment log below
for the three saved model outputs and remaining validation work.

## Submission checklist

The assignment also asks for:

1. A shareable build link, plus a separate system prompt (included above).
2. The SRVCABLE brief pasted exactly as generated, with any manual edits disclosed.
3. A test log covering at least three model runs with prompt/design changes, including
   what went wrong and what changed.
4. A 2–3 minute screen recording showing a run, one design decision, and one limitation.

Three model-run briefs are saved under [`outputs/`](./outputs/), satisfying the
assignment's minimum run count. The final conflict-deduplication changes were made
after Run 3, so a post-fix confirmation run is still outstanding, as are the final
reviewed submission brief, shareable repository link, and recording.

## What the pack tests

The input includes a mismatch between a secondary article's Q1 revenue and the
company filing, an unrelated local cable-TV operator, historical promoter-pledge
reporting that differs from the latest exchange filing, a pending GST demand, and a
stock-tips blog containing an instruction aimed at summarizers. The agents should
distinguish primary filings from commentary, identify the issuer correctly, surface
the open issues, and ignore instructions embedded in documents.

## Model and limitations

The default is Gemini 2.5 Flash-Lite, a fast, budget-oriented model for this
structured multi-step workflow. The model is configurable because free-tier
availability and quotas can change. Exact quote matching, numeric checks, citation
linking, section checks, and some conflict handling are deterministic; metric and
period extraction, source interpretation, and factual paraphrase quality still
depend on the model. Review generated briefs before using them. The workflow uses
only supplied documents and does not independently verify them online, fetch live
market data, or give investment recommendations.

## Prompt files and evaluation log

Agent instructions are maintained separately from the workflow code:

- [`evidence_prompt_v2.md`](./prompts/evidence_prompt_v2.md) defines the evidence
  extraction task and structured evidence schema.
- [`challenge_prompt_v2.md`](./prompts/challenge_prompt_v2.md) defines skeptic
  findings and the mandatory-open-question flag.
- [`brief_writer_prompt.md`](./prompts/brief_writer_prompt.md) defines the brief
  format and asks the writer not to repeat findings automatically appended by the
  application.
- [`research_system_prompt_v2.md`](./prompts/research_system_prompt_v2.md) contains
  shared source-grounding and safety instructions.

The workflow loads the evidence, skeptic, and writer prompt files for their
respective stages. The shared system instructions are also kept in a separate file.
Run outputs are retained as generated; they have not been silently edited to hide
validation failures.

### Run 1 — Baseline

**Output:** [`run1_srvcable_brief.md`](./outputs/run1_srvcable_brief.md)

**Observed:** The Snapshot included the reported Q1 FY27 revenue, EBITDA and margin,
profit after tax, order book, and latest promoter pledge figure. The brief also
covered guidance, the Bharuch plant, receivables, and the GST demand. Its Sources list
included only sources cited in the report.

**Problem:** The Q1 revenue discrepancy appeared both as a writer-authored question
and as an automatically appended skeptic finding, so Open questions repeated it.

### Run 2 — Prompt files and structured agent instructions

**Design change:** Moved the evidence analyst, skeptic, and brief writer instructions
into separate files under `prompts/`, retaining the JSON schemas and deterministic
validation.

**Output:** [`run2_srvcable_brief.md`](./outputs/run2_srvcable_brief.md)

**Improvement observed:** The brief was more concise and retained the main positive
and negative considerations, including the GST demand and conflicting Q1 revenue
figures. Its Sources section listed only sources cited by the brief.

**Remaining problems:** The Snapshot omitted reported revenue and EBITDA margin. The
revenue discrepancy appeared twice because the writer included it and the application
also appended the skeptic finding. Numeric validation omitted one draft statement
and added a verification warning to the output.

**Next change:** Run the revised validation design below.

**Status:** Partially improved.

### Run 3 — Results and Evaluation

**Output:** [`run3_srvcable_brief.md`](./outputs/run3_srvcable_brief.md). The saved
output was generated before the final conflict-deduplication changes described
below.

**What improved**

- The Snapshot includes all four key Q1 FY27 metrics: revenue, EBITDA, EBITDA margin,
  and PAT.
- Those four reported financial figures are attributed to the official company
  press release [S3].
- The validation design now rejects unsupported numerical claims and attempts
  bounded repairs instead of silently omitting a statement.
- Conflict deduplication was strengthened in code using metric, reporting period,
  and cited sources.

**Remaining issues**

- The revenue discrepancy between [S1] and [S3] still appears twice in Open
  questions in the saved output. The saved Run 3 therefore does not demonstrate
  successful deduplication.
- The Bull case presents reduced promoter share pledging as a positive indicator.
  That is useful ownership-risk context, but not a direct business-growth driver. The
  output underweights stronger growth evidence: the planned Bharuch capacity
  expansion and data-centre cable opportunity described in [S2] and [S3].
- The Bear case mentions the ₹46.3 crore GST demand but omits the company's stated
  intention to appeal and its view that no material impact is expected. Include that
  response as the company's position, not as an independently established outcome.
- The saved output's source links use standard Markdown link syntax, but point to
  fictional `.example` URLs and have not been checked in a rendered preview. The
  links are not evidence that those URLs resolve.

**Validation status:** A complete brief was saved, and it contains no visible
verification warning. The raw terminal log was not retained, so the Markdown file
alone cannot confirm whether any validation warning was printed during generation.
Offline tests cover the newer reject-and-repair behavior; because those code changes
came after this output, they still need a live post-fix rerun.

**Conclusion:** Run 3 improved Snapshot completeness and the validation design, but
the final output is not fully improved. Duplicate conflict reporting, evidence
classification, balanced regulatory reporting, and rendered-output checks remain
open issues.

## Known limitations

- **Source conflicts:** The agent identifies the two Q1 revenue figures but the
  saved Run 3 repeats the discrepancy in Open questions.
- **Evidence interpretation:** Relevant facts can be placed in the wrong analytical
  category; promoter pledging is ownership context, not a growth driver.
- **Balanced reporting:** The GST demand is included, but the saved Run 3 omits the
  company's stated appeal and its expectation of no material impact.
- **Output formatting:** Source links in the saved brief use standard Markdown
  `[label](target)` syntax; no backslash-escaped brackets or parentheses were
  observed. The `.example` URLs are placeholders, and the report has not been
  checked in a Markdown renderer.
- **Source limitations:** The agent uses only the supplied pack and does not
  independently verify the documents online.
- **Post-fix evidence:** The final deduplication and stricter numeric-repair changes
  were not exercised by the saved Run 3 live output.

## Future improvements

Prioritized next steps:

1. **Deduplicate structured findings before rendering.** The current post-Run-3
   finalizer attempts to suppress an appended conflict when a writer question
   already covers the same metric and source IDs. Promote this to a stable conflict
   key (metric, normalized reporting period, and source IDs) shared by the writer and
   Skeptic, and regression-test it against the duplicate in saved Run 3.
2. **Improve evidence categorization.** Separate business-growth drivers from
   ownership indicators, financial risks, and regulatory developments so accurate
   facts appear in appropriate sections.
3. **Balance regulatory reporting.** Include the GST demand alongside the company's
   stated intention to appeal and its view on material impact, clearly attributed to
   the company.
4. **Validate final Markdown.** Check required headings, bullets, citation format,
   source links, and duplicate questions, then inspect the rendered output.
5. **Run regression tests.** Re-run SRVCABLE after changes and confirm that the four
   key financial metrics remain present and unsupported claims are rejected or
   repaired rather than silently dropped.

When a key is available, preserve the saved Run 3 output and write the rerun to a
different path:

```powershell
$env:GEMINI_API_KEY = "your-gemini-api-key"
$env:GEMINI_MODEL = "gemini-3.5-flash-lite"
python research_agent.py SRVCABLE `
  --documents candidate/research_pack `
  --as-of 2026-09-23 `
  --output ./outputs/run3_validation_rerun_srvcable_brief.md
```

After execution, record whether the revenue-conflict question is singular, the
Snapshot retains the supported revenue, EBITDA, margin and PAT figures, and
unsupported claims cause a repair or explicit failure rather than disappearing.
Do not mark post-fix validation complete until those results are observed.
