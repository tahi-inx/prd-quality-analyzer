# PRD Quality Analyzer

Automated quality analysis for Product Requirements Documents (PRDs) in the [inx-context](https://github.com/IntegrityNextTeam/inx-context) repository.

Scans all branches for PRD files, evaluates each against the standardized PRD template using **Amazon Bedrock (Claude Opus 4.6)**, and generates an interactive HTML dashboard.

**[View the latest report](https://tahi-inx.github.io/prd-quality-analyzer/prd-quality-analysis.html)**

## AWS Account

| Field | Value |
|-------|-------|
| Profile | `inx-dev` |
| Account ID | `590184058999` |
| Region | `eu-central-1` |
| Bedrock Model | `eu.anthropic.claude-opus-4-6-v1` |
| SSO Session | `inx-org-root` |

## Quick Start

```bash
# 1. Install dependencies
pip install boto3

# 2. Log in to AWS SSO
aws sso login --sso-session inx-org-root

# 3. Run the analysis
AWS_PROFILE=inx-dev python3 scripts/analyze-prds.py --repo /path/to/inx-context
```

The report is generated at `reports/prd-quality-report.html` and also deployed to GitHub Pages.

## Usage

```bash
# Full analysis with Bedrock Opus 4.6
AWS_PROFILE=inx-dev python3 scripts/analyze-prds.py --repo /path/to/inx-context

# Regenerate HTML from cached data (no LLM calls, no cost)
python3 scripts/analyze-prds.py --repo /path/to/inx-context --data-file data/prd-data.json

# Heuristic-only scoring (no Bedrock, free, ~80% accuracy)
python3 scripts/analyze-prds.py --repo /path/to/inx-context --skip-llm

# Dry run — just list discovered PRD files
python3 scripts/analyze-prds.py --repo /path/to/inx-context --dry-run

# Custom output path
AWS_PROFILE=inx-dev python3 scripts/analyze-prds.py --repo /path/to/inx-context --output my-report.html

# Use a different Bedrock model
AWS_PROFILE=inx-dev python3 scripts/analyze-prds.py --repo /path/to/inx-context --model anthropic.claude-sonnet-4-20250514-v1:0
```

## How It Works

1. **Scan** — Enumerates all remote branches + main for files matching PRD naming patterns
2. **Read** — Extracts file content via `git show` and creation dates via `git log`
3. **Evaluate** — Sends each file to Bedrock with the scoring rubric as a system prompt
4. **Report** — Generates a self-contained HTML dashboard with Chart.js visualizations

### Scoring Rubric (0–5)

| Score | Level | Criteria |
|-------|-------|----------|
| 5 | Exemplary | Full template: sections 1–12, US-XX, RULE-XX-Snn, YAML frontmatter, State & Invariants, Key Decisions, Tracking Events |
| 4 | Strong | Most sections present, good ACs, may miss US-XX numbering or exact RULE format |
| 3 | Partial | Overview, Problem, Features present but not in template format, no RULE numbering |
| 2 | Minimal | Custom format, missing most template sections |
| 1 | None | Free-form, Confluence dump, no template adherence |
| 0 | N/A | KB ingestion, duplicate, or non-PRD document |

### May 12 Cutoff

PRDs are classified as pre- or post-May 12, 2026, when the standardized template was formally communicated to all teams.

## Project Structure

```
prd-quality-analyzer/
├── scripts/
│   └── analyze-prds.py            # Main analysis script
├── reports/
│   ├── prd-quality-report.html    # Latest auto-generated report
│   └── prd-quality-analysis.html  # Manual baseline report
├── docs/                          # GitHub Pages root
│   └── *.html                     # Published reports
├── data/
│   ├── prd-data.json              # Latest evaluation data (auto-generated)
│   └── prd-data-YYYYMMDD-HHMM.json  # Timestamped archives
├── requirements.txt
└── README.md
```

## Prerequisites

- Python 3.10+
- `boto3` (`pip install boto3`)
- AWS SSO access to the `inx-dev` account (590184058999, eu-central-1)
- Claude Opus 4.6 enabled in Bedrock on `inx-dev`
- Git access to the `inx-context` repository

## Cost

~$3–5 per full run with Claude Opus 4.6 (66 files, ~2–5K tokens each). Use `--data-file` to regenerate reports from cached data at zero cost.
