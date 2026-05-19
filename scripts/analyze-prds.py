#!/usr/bin/env python3
"""
PRD Quality Analyzer — inx-context

Scans the inx-context git repository for PRD documents across all branches,
evaluates each against the create-prd skill template using Amazon Bedrock
(Claude Opus 4.6), and generates an interactive HTML report.

Usage:
    AWS_PROFILE=inx-dev python3 scripts/analyze-prds.py --repo /path/to/inx-context [--dry-run] [--skip-llm] [--data-file prd-data.json]

Requirements:
    pip install boto3

Prerequisites:
    aws sso login --sso-session inx-org-root
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPO_ROOT: Path  # Set from --repo argument in main()
BEDROCK_MODEL_ID = "eu.anthropic.claude-opus-4-6-v1"
BEDROCK_REGION = "eu-central-1"
MAY12_CUTOFF = "2026-05-12"
MAX_FILE_TOKENS_APPROX = 15000  # ~60K chars, well within 1M context
CONCURRENCY_DELAY = 0.5  # seconds between Bedrock calls to avoid throttling

PRD_PATTERNS = re.compile(
    r"(^|/)prd[^/]*\.md$"     # prd-something.md or PRD.md at start of filename
    r"|/PRD\.md$"              # exactly PRD.md
    r"|/PRD-[^/]+\.md$"       # PRD-something.md
    r"|-prd[.-]"              # ...-prd.md or ...-prd-...
    r"|-prd\d"                # ...-prd1-... or ...-prd2-...
    r"|_prd[.-]",             # ..._prd.md or ..._prd-...
    re.IGNORECASE,
)
SKIP_PATTERNS = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|pdf|ico|woff|ttf|eot)$"
    r"|/node_modules/|/\.git/|/assets/|/images/|\.gitkeep$"
    r"|AGENTS\.md$|CLAUDE\.md$|README\.md$|CHANGELOG\.md$|SKILL\.md$"
    r"|/templates/.*prd-template|^templates/"
    r"|\.cursor/skills/"
    r"|product-operating-system/"
    r"|^inx-context/new-inx-context-structure/roadmap/"
    r"|^roadmap/"
    r"|^docs/PRD-Asynchronous"
    r"|signals-and-explorations"
    r"|-exploration\."
    r"|-exploration-supplement",
    re.IGNORECASE,
)

RESTRUCTURING_BRANCHES = {
    "INX-20513-align-terminology",
    "INX-20522-QA-E2-S6",
    "chore/improve-repo-structure-readability",
    "audit/openviking-context-alignment",
    "chore/integrate-product-operating-system",
    "chore/vendor-product-operating-system",
    "pc-context-7-layers",
}

SCORING_SYSTEM_PROMPT = """You are a PRD quality auditor for IntegrityNext. You evaluate Product Requirements Documents against a standardized template.

## Scoring Rubric (0-5)

**5 — Exemplary**: Full template compliance. Numbered sections 1-12 (or close). US-XX user story format. RULE-XX-Snn/Fnn acceptance criteria format. YAML frontmatter or structured header metadata. Comprehensive Problem Statement (Pain/Cost/Why now). State & Invariants section. Key Decisions table with D# numbering. Tracking Events table. NFRs with measurable targets.

**4 — Strong**: Most template sections present. User stories with persona. Good acceptance criteria. May miss US-XX numbering or exact RULE-XX format but has equivalent structured ACs. Header metadata present. Missing 1-2 sections (e.g., NFRs, rollout, pricing).

**3 — Partial**: Has Overview, Problem, User Stories, Features but doesn't follow exact template format. Missing several required sections. No RULE numbering. Acceptance criteria present but unstructured.

**2 — Minimal**: Some PRD-like content but largely custom format. Missing most template sections. May be a spec/exploration rather than a PRD.

**1 — None**: Free-form document, Confluence dump, or completely custom format. No template adherence whatsoever.

**0 — N/A**: Not a PRD at all — KB ingestion (verbatim Confluence/PDF export into regulatory knowledge base), duplicate of another file, pointer/redirect file, or pure technical spec with no product requirements.

## Document Type Classification

Classify each document as exactly one of:
- **PRD** — an active product requirements document
- **Exploration** — an early-stage exploration or discovery doc that resembles a PRD
- **Spec/Model** — a technical specification, data model, or architecture doc
- **KB Ingestion** — a verbatim Confluence/PDF export placed in a knowledge base directory
- **Duplicate** — a copy of a PRD that exists elsewhere (canonical version in solutions/)
- **Pointer** — a file that just redirects/points to another document

## Location Classification

Based on the file path, classify the location:
- **solutions/** — canonical location for PRDs
- **discovery/** — discovery/exploration directory
- **regulations/** — regulatory knowledge base

## Response Format

You MUST respond with ONLY a valid JSON object, no markdown fences, no explanation:

{
  "score": <0-5>,
  "docType": "<PRD|Exploration|Spec/Model|KB Ingestion|Duplicate|Pointer>",
  "notes": "<2-3 sentence assessment explaining the score. Mention specific template elements present or missing. Reference RULE-XX, US-XX, STATE-XX patterns if found.>"
}"""


def git(*args: str, cwd: Optional[Path] = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def git_lines(*args: str) -> list[str]:
    output = git(*args)
    return [line for line in output.splitlines() if line.strip()]


def get_file_creation_date(filepath: str, branch: str) -> str:
    date = git("log", "--format=%ai", "--diff-filter=A", "--follow", "--", filepath)
    if not date:
        date = git("log", branch, "--format=%ai", "--diff-filter=A", "--", filepath)
    if date:
        return date.splitlines()[-1][:10]
    return "unknown"


def get_file_content(filepath: str, branch: str) -> str:
    ref = branch if branch == "main" else f"origin/{branch}"
    content = git("show", f"{ref}:{filepath}")
    if len(content) > MAX_FILE_TOKENS_APPROX * 4:
        content = content[: MAX_FILE_TOKENS_APPROX * 4]
    return content


def find_prd_files() -> list[dict]:
    """Scan main and all remote branches for PRD-like files."""
    print("Fetching remote branches...")
    git("fetch", "--all", "--prune")

    found = {}  # path -> {branch, on_main}

    # Scan main first
    print("Scanning main branch...")
    main_files = git_lines("ls-tree", "-r", "--name-only", "main")
    for f in main_files:
        if SKIP_PATTERNS.search(f):
            continue
        if PRD_PATTERNS.search(f):
            found[f] = {"branch": "main", "on_main": True}

    # Scan remote branches
    branches = git_lines("branch", "-r", "--format=%(refname:short)")
    branches = [
        b.replace("origin/", "")
        for b in branches
        if b.startswith("origin/") and b != "origin/HEAD" and "origin/main" not in b
    ]
    print(f"Scanning {len(branches)} remote branches...")

    for branch in branches:
        if branch in RESTRUCTURING_BRANCHES:
            continue
        try:
            branch_files = git_lines("ls-tree", "-r", "--name-only", f"origin/{branch}")
        except Exception:
            continue
        for f in branch_files:
            if SKIP_PATTERNS.search(f):
                continue
            if PRD_PATTERNS.search(f):
                if f not in found:
                    found[f] = {"branch": branch, "on_main": False}

    print(f"Found {len(found)} PRD candidate files")
    return [{"path": path, **info} for path, info in found.items()]


def normalize_path(filepath: str) -> str:
    """Strip restructuring prefixes to get the canonical path."""
    prefixes = [
        "inx-context/new-inx-context-structure/roadmap/",
        "inx-context/new-inx-context-structure/",
    ]
    for prefix in prefixes:
        if filepath.startswith(prefix):
            return filepath[len(prefix):]
    return filepath


def extract_solution(filepath: str) -> str:
    """Derive the solution name from the file path."""
    fp = normalize_path(filepath)
    parts = fp.split("/")

    if parts[0] == "solutions" and len(parts) > 1:
        name = parts[1]
    elif parts[0] == "discovery" and len(parts) > 2:
        # discovery/ongoing/<solution>/... or discovery/ongoing/platform/VAL-xxx
        name = parts[2]
    elif parts[0] == "regulations" and len(parts) > 2:
        for p in parts:
            if p.upper() in ("CBAM", "CARBON", "EUDR", "ESRS", "SCDD"):
                name = p
                break
        else:
            name = parts[2] if len(parts) > 2 else "unknown"
    else:
        name = parts[0]

    name_map = {
        "scdd": "SCDD", "SCDD": "SCDD",
        "eudr": "EUDR", "EUDR": "EUDR",
        "product-compliance": "Product Compliance",
        "platform": "Platform",
        "carbon": "Carbon", "CARBON": "Carbon",
        "cbam": "CBAM", "CBAM": "CBAM",
        "fe-tech": "FE-tech",
    }
    return name_map.get(name, name.replace("-", " ").title())


def extract_location(filepath: str) -> str:
    fp = normalize_path(filepath)
    if fp.startswith("solutions/"):
        return "solutions/"
    elif fp.startswith("discovery/"):
        return "discovery/"
    elif fp.startswith("regulations/"):
        return "regulations/"
    return "other/"


def evaluate_with_bedrock(filepath: str, content: str, client) -> dict:
    """Send a PRD file to Bedrock for evaluation."""
    user_message = f"""Evaluate this PRD document.

**File path:** `{filepath}`

**Document content:**

{content}"""

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "temperature": 0,
            "system": SCORING_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }
    )

    response = client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )

    response_body = json.loads(response["body"].read())
    text = response_body["content"][0]["text"].strip()

    # Strip markdown fences if the model wraps the JSON
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    return json.loads(text)


def evaluate_heuristic(filepath: str, content: str) -> dict:
    """Fallback heuristic scoring when LLM is skipped."""
    score = 0
    notes_parts = []

    has_yaml = content.startswith("---") or "**Status:**" in content[:500]
    has_rule = bool(re.search(r"RULE-\d+-[SF]\d+", content))
    has_us = bool(re.search(r"US-\d+", content))
    has_state = bool(re.search(r"STATE-\d+", content))
    has_decisions = bool(re.search(r"\bD\d+\b.*decision", content, re.IGNORECASE))
    has_sections = len(re.findall(r"^##\s+\d+\.", content, re.MULTILINE))
    has_problem = bool(re.search(r"problem\s+statement", content, re.IGNORECASE))
    has_nfr = bool(re.search(r"NFR-", content))
    has_tracking = bool(re.search(r"tracking\s+events?", content, re.IGNORECASE))

    if "regulations/regulatory-knowledge-base" in filepath:
        return {"score": 0, "docType": "KB Ingestion", "notes": "File in regulatory KB directory — likely a Confluence/PDF ingestion."}

    signals = sum([has_yaml, has_rule, has_us, has_state, has_decisions, has_sections >= 6, has_problem, has_nfr, has_tracking])
    if signals >= 7:
        score = 5
    elif signals >= 5:
        score = 4
    elif signals >= 3:
        score = 3
    elif signals >= 1:
        score = 2
    else:
        score = 1

    if has_rule:
        notes_parts.append("RULE-XX format present")
    if has_us:
        notes_parts.append("US-XX format present")
    if has_state:
        notes_parts.append("STATE-XX present")
    if not has_problem:
        notes_parts.append("Missing Problem Statement")
    if has_sections < 4:
        notes_parts.append(f"Only {has_sections} numbered sections found")

    doc_type = "PRD"
    if "specification" in filepath.lower() or "data-model" in filepath.lower() or "data-flow" in filepath.lower():
        doc_type = "Spec/Model"
    elif "exploration" in filepath.lower():
        doc_type = "Exploration"

    return {"score": score, "docType": doc_type, "notes": ". ".join(notes_parts) or "Heuristic evaluation — limited accuracy."}


def generate_html(prd_entries: list[dict], output_path: Path):
    """Generate the HTML report from evaluated PRD data."""
    prd_json = json.dumps(prd_entries, indent=2, ensure_ascii=False)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PRD Quality Analysis — inx-context</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0e0e10; --surface: #1a1a1e; --border: #2a2a2e; --text: #e4e4e7;
    --muted: #a1a1aa; --accent: #3b82f6; --success: #22c55e; --warning: #eab308;
    --danger: #ef4444; --info: #3b82f6; --neutral: #71717a;
    --success-bg: #052e16; --warning-bg: #422006; --danger-bg: #450a0a; --info-bg: #172554;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 32px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.35rem; font-weight: 600; margin: 32px 0 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  h3 {{ font-size: 1.1rem; font-weight: 600; margin: 20px 0 8px; }}
  p, .text {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 12px; }}
  .subtitle {{ color: var(--muted); font-size: 0.8rem; margin-bottom: 24px; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 28px 0; }}

  .callout {{ border-radius: 8px; padding: 16px 20px; margin: 16px 0; border-left: 4px solid; }}
  .callout-info {{ background: var(--info-bg); border-color: var(--info); }}
  .callout-warning {{ background: var(--warning-bg); border-color: var(--warning); }}
  .callout-title {{ font-weight: 600; font-size: 0.95rem; margin-bottom: 4px; }}
  .callout-body {{ font-size: 0.85rem; color: var(--muted); }}

  .stats-grid {{ display: grid; gap: 12px; margin: 16px 0; }}
  .stats-4 {{ grid-template-columns: repeat(4, 1fr); }}
  @media (max-width: 768px) {{ .stats-4 {{ grid-template-columns: repeat(2, 1fr); }} }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; text-align: center; }}
  .stat-value {{ font-size: 1.8rem; font-weight: 700; }}
  .stat-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-top: 4px; }}
  .stat-success .stat-value {{ color: var(--success); }}
  .stat-warning .stat-value {{ color: var(--warning); }}
  .stat-danger .stat-value {{ color: var(--danger); }}
  .stat-info .stat-value {{ color: var(--info); }}

  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.82rem; }}
  th {{ background: var(--surface); padding: 10px 12px; text-align: left; font-weight: 600; border-bottom: 2px solid var(--border); position: sticky; top: 0; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }}
  tr:nth-child(even) td {{ background: rgba(255,255,255,0.02); }}
  .row-success td {{ background: rgba(34,197,94,0.08) !important; }}
  .row-info td {{ background: rgba(59,130,246,0.08) !important; }}
  .row-warning td {{ background: rgba(234,179,8,0.06) !important; }}
  .row-danger td {{ background: rgba(239,68,68,0.08) !important; }}
  td.r {{ text-align: right; }}
  td.c {{ text-align: center; }}

  .pill {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.72rem; font-weight: 600; }}
  .pill-success {{ background: var(--success); color: #000; }}
  .pill-info {{ background: var(--info); color: #fff; }}
  .pill-warning {{ background: var(--warning); color: #000; }}
  .pill-danger {{ background: var(--danger); color: #fff; }}
  .pill-neutral {{ background: var(--neutral); color: #fff; }}

  .chart-container {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin: 12px 0; }}
  canvas {{ max-height: 260px; }}

  details {{ margin: 8px 0; }}
  summary {{ cursor: pointer; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; font-weight: 600; font-size: 0.95rem; display: flex; align-items: center; gap: 8px; user-select: none; }}
  summary:hover {{ background: #222226; }}
  summary::marker {{ content: ''; }}
  summary::before {{ content: '\\25B6'; font-size: 0.7rem; transition: transform 0.2s; }}
  details[open] summary::before {{ transform: rotate(90deg); }}
  details .detail-body {{ padding: 12px 0; }}
  .count-badge {{ background: var(--border); color: var(--muted); padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 500; }}
  .summary-pills {{ margin-left: auto; display: flex; gap: 6px; }}

  .findings-block {{ margin-bottom: 16px; }}
  .findings-block p {{ margin-top: 4px; }}

  .score-5 {{ color: var(--success); font-weight: 600; }}
  .score-4 {{ color: var(--info); font-weight: 600; }}
  .score-3 {{ color: var(--warning); font-weight: 600; }}
  .score-2 {{ color: var(--danger); font-weight: 600; }}
  .score-1 {{ color: var(--danger); font-weight: 600; }}
  .score-0 {{ color: var(--muted); }}

  .generated-footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--border); font-size: 0.75rem; color: var(--muted); }}
</style>
</head>
<body>

<h1>PRD Quality Analysis &mdash; inx-context (Full Repository)</h1>
<div class="subtitle">Generated: {timestamp} | Model: Claude Opus 4.6 (Bedrock) | Scope: entire repository across main + all feature branches</div>

<div class="callout callout-info">
  <div class="callout-title">May 12 Cutoff</div>
  <div class="callout-body">On May 12, 2026, a communication went out requiring all teams to use the standardized PRD template from the create-prd skill. PRDs are classified as pre- or post-May 12 based on their git creation date.</div>
</div>

<div class="stats-grid stats-4" id="top-stats"></div>
<div class="stats-grid stats-4" id="secondary-stats"></div>

<hr>
<h2>Location Hygiene</h2>
<div class="callout callout-warning" id="location-callout"></div>
<table id="location-table"></table>

<details>
  <summary>Mislocated &amp; Duplicate Details <span class="count-badge" id="mislocated-count"></span></summary>
  <div class="detail-body"><table id="mislocated-table"></table></div>
</details>

<hr>
<h2>Before vs After May 12</h2>
<div class="chart-container"><canvas id="beforeAfterChart"></canvas></div>
<p class="text" id="beforeAfterCaption"></p>

<hr>
<h2>Average Score by Solution (Pre vs Post May 12)</h2>
<div class="chart-container"><canvas id="solutionChart"></canvas></div>
<p class="text">Grouped by solution showing pre-May 12 (blue) vs post-May 12 (green) averages. KB ingestions and duplicates excluded.</p>

<hr>
<h2>Solution-Level Summary</h2>
<table id="solution-summary"></table>

<hr>
<h2>Scoring Rubric</h2>
<table id="rubric-table"></table>

<hr>
<h2>Detailed PRD Inventory</h2>
<div id="inventory"></div>

<hr>
<h2>Recommendations</h2>
<table id="recommendations"></table>

<div class="generated-footer" id="footer"></div>

<script>
const prdData = {prd_json};

const solutionOrder = [...new Set(prdData.map(p => p.solution))].sort();
const active = prdData.filter(p => !["KB Ingestion","Duplicate","Pointer"].includes(p.docType));
const kb = prdData.filter(p => p.docType === "KB Ingestion");
const dupes = prdData.filter(p => p.docType === "Duplicate");
const mislocated = active.filter(p => p.location !== "solutions/");
const pre = active.filter(p => !p.postMay12);
const post = active.filter(p => p.postMay12);
const avgFn = (arr) => arr.length ? arr.reduce((a,p) => a + p.score, 0) / arr.length : 0;
const scoreLabel = sc => sc >= 5 ? "Exemplary" : sc >= 4 ? "Strong" : sc >= 3 ? "Partial" : sc >= 2 ? "Minimal" : sc >= 1 ? "None" : "N/A";
const rowClass = (p) => {{
  if (["KB Ingestion","Duplicate"].includes(p.docType)) return "";
  return p.score >= 5 ? "row-success" : p.score >= 4 ? "row-info" : p.score >= 3 ? "" : "row-danger";
}};

function makeTable(el, headers, rows, opts = {{}}) {{
  let h = `<thead><tr>${{headers.map((h,i) => `<th${{opts.align && opts.align[i] ? ` style="text-align:${{opts.align[i]}}"` : ""}}>${{h}}</th>`).join("")}}</tr></thead><tbody>`;
  rows.forEach((r, ri) => {{
    const cls = opts.rowClasses ? opts.rowClasses[ri] || "" : "";
    h += `<tr class="${{cls}}">${{r.map((c, ci) => `<td${{opts.align && opts.align[ci] && opts.align[ci]==="right" ? ' class="r"' : opts.align && opts.align[ci]==="center" ? ' class="c"' : ""}}>${{c}}</td>`).join("")}}</tr>`;
  }});
  el.innerHTML = h + "</tbody>";
}}

// Top stats
const overallAvg = avgFn(active).toFixed(1);
const postAvg = avgFn(post).toFixed(1);
document.getElementById("top-stats").innerHTML = [
  {{v: prdData.length, l: "Total Files Scanned", c: ""}},
  {{v: active.length, l: "Active PRD Documents", c: "stat-info"}},
  {{v: overallAvg, l: "Active Avg Score", c: Number(overallAvg) >= 3.5 ? "stat-warning" : "stat-danger"}},
  {{v: postAvg, l: "Post-May 12 Avg", c: "stat-success"}},
].map(s => `<div class="stat ${{s.c}}"><div class="stat-value">${{s.v}}</div><div class="stat-label">${{s.l}}</div></div>`).join("");

document.getElementById("secondary-stats").innerHTML = [
  {{v: pre.length, l: "Pre-May 12", c: ""}},
  {{v: post.length, l: "Post-May 12", c: "stat-success"}},
  {{v: active.filter(p => p.branch !== "main").length, l: "Branch-Only", c: "stat-info"}},
  {{v: mislocated.length, l: "Mislocated (not in solutions/)", c: "stat-warning"}},
].map(s => `<div class="stat ${{s.c}}"><div class="stat-value">${{s.v}}</div><div class="stat-label">${{s.l}}</div></div>`).join("");

// Location hygiene
document.getElementById("location-callout").innerHTML = `<div class="callout-title">PRD files found outside solutions/</div><div class="callout-body">${{mislocated.length}} active PRDs outside solutions/. ${{dupes.length}} duplicates. ${{kb.length}} KB ingestions.</div>`;

makeTable(document.getElementById("location-table"),
  ["Directory","Active PRDs","Duplicates","KB Copies","Issue"],
  [
    ["solutions/", active.filter(p=>p.location==="solutions/").length, "0", "0", "Canonical location"],
    ["discovery/", active.filter(p=>p.location==="discovery/").length, dupes.length, "0", "PRDs should move to solutions/"],
    ["regulations/", "0", "0", kb.length, "KB ingestions, not active PRDs"],
  ],
  {{ rowClasses: ["row-success","row-warning","row-danger"] }}
);

document.getElementById("mislocated-count").textContent = mislocated.length + dupes.length + kb.length;
makeTable(document.getElementById("mislocated-table"),
  ["Name","Type","Directory","Solution","Action Needed"],
  [
    ...mislocated.map(p => [p.name, p.docType, p.location, p.solution, "Move to solutions/"]),
    ...dupes.map(p => [p.name, "Duplicate", p.location, p.solution, "Delete duplicate"]),
    ...kb.map(p => [p.name, "KB Ingestion", p.location, p.solution, "Rename to avoid PRD confusion"]),
  ],
  {{ rowClasses: [...mislocated.map(()=>"row-warning"), ...dupes.map(()=>"row-danger"), ...kb.map(()=>"")] }}
);

// Before/After chart
new Chart(document.getElementById("beforeAfterChart"), {{
  type: "bar",
  data: {{
    labels: ["Pre-May 12", "Post-May 12"],
    datasets: [
      {{label:"Score 1", data:[pre.filter(p=>p.score===1).length, post.filter(p=>p.score===1).length], backgroundColor:"#ef4444"}},
      {{label:"Score 2", data:[pre.filter(p=>p.score===2).length, post.filter(p=>p.score===2).length], backgroundColor:"#eab308"}},
      {{label:"Score 3", data:[pre.filter(p=>p.score===3).length, post.filter(p=>p.score===3).length], backgroundColor:"#71717a"}},
      {{label:"Score 4", data:[pre.filter(p=>p.score===4).length, post.filter(p=>p.score===4).length], backgroundColor:"#3b82f6"}},
      {{label:"Score 5", data:[pre.filter(p=>p.score===5).length, post.filter(p=>p.score===5).length], backgroundColor:"#22c55e"}},
    ]
  }},
  options: {{ responsive:true, plugins:{{legend:{{labels:{{color:"#a1a1aa"}}}}}}, scales:{{x:{{stacked:true,ticks:{{color:"#a1a1aa"}},grid:{{color:"#2a2a2e"}}}},y:{{stacked:true,ticks:{{color:"#a1a1aa"}},grid:{{color:"#2a2a2e"}}}}}}}}
}});
document.getElementById("beforeAfterCaption").textContent = `Pre-May 12: ${{pre.length}} docs | Post-May 12: ${{post.length}} docs`;

// Solution averages chart (pre vs post)
const solAvgs = solutionOrder.map(sol => {{
  const items = active.filter(p=>p.solution===sol);
  const preItems = items.filter(p=>!p.postMay12);
  const postItems = items.filter(p=>p.postMay12);
  return {{
    sol, count: items.length, avg: items.length ? Math.round(avgFn(items)*10)/10 : 0,
    preAvg: preItems.length ? Math.round(avgFn(preItems)*10)/10 : null,
    postAvg: postItems.length ? Math.round(avgFn(postItems)*10)/10 : null,
    preCount: preItems.length, postCount: postItems.length
  }};
}});
new Chart(document.getElementById("solutionChart"), {{
  type: "bar",
  data: {{
    labels: solAvgs.map(s=>s.sol),
    datasets: [
      {{ label: "Pre-May 12 Avg", data: solAvgs.map(s=>s.preAvg), backgroundColor: "#3b82f6", borderRadius: 4 }},
      {{ label: "Post-May 12 Avg", data: solAvgs.map(s=>s.postAvg), backgroundColor: "#22c55e", borderRadius: 4 }}
    ]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ labels: {{ color: "#a1a1aa" }} }},
      tooltip: {{ callbacks: {{ afterLabel: function(ctx) {{
        const sa = solAvgs[ctx.dataIndex];
        return ctx.datasetIndex === 0 ? `${{sa.preCount}} PRDs` : `${{sa.postCount}} PRDs`;
      }} }} }}
    }},
    scales: {{
      x: {{ ticks: {{ color: "#a1a1aa" }}, grid: {{ color: "#2a2a2e" }} }},
      y: {{ min: 0, max: 5, ticks: {{ color: "#a1a1aa" }}, grid: {{ color: "#2a2a2e" }} }}
    }}
  }}
}});

// Solution summary table
makeTable(document.getElementById("solution-summary"),
  ["Solution","Active PRDs","Avg Score","Pre-May 12","Post-May 12","Branch-Only","Maturity"],
  solAvgs.map(sa => {{
    const items = active.filter(p=>p.solution===sa.sol);
    const mat = sa.avg >= 4.5 ? "High" : sa.avg >= 3.5 ? "Medium" : sa.avg >= 2.5 ? "Low" : "Very Low";
    return [sa.sol, sa.count, sa.avg, items.filter(p=>!p.postMay12).length, items.filter(p=>p.postMay12).length, items.filter(p=>p.branch!=="main").length, `<span class="pill ${{sa.avg >= 4.5 ? "pill-success" : sa.avg >= 3.5 ? "pill-info" : sa.avg >= 2.5 ? "pill-warning" : "pill-danger"}}">${{mat}}</span>`];
  }}),
  {{ align:["left","right","right","right","right","right","center"], rowClasses: solAvgs.map(s => s.avg >= 4.5 ? "row-success" : s.avg >= 3.5 ? "row-info" : s.avg >= 2.5 ? "row-warning" : "row-danger") }}
);

// Rubric
makeTable(document.getElementById("rubric-table"),
  ["Score","Level","Criteria"],
  [
    ['<span class="score-5">5</span>',"Exemplary","Full template. Sections 1-12. US-XX. RULE-XX-Snn. YAML frontmatter. State & Invariants. Key Decisions. Tracking Events."],
    ['<span class="score-4">4</span>',"Strong","Most sections. Good ACs. May miss US-XX or exact RULE format."],
    ['<span class="score-3">3</span>',"Partial","Overview, Problem, Features present but not template format. No RULE numbering."],
    ['<span class="score-2">2</span>',"Minimal","Custom format. Missing most template sections."],
    ['<span class="score-1">1</span>',"None","Free-form or Confluence dump. No template adherence."],
    ['<span class="score-0">0</span>',"N/A","KB ingestion, duplicate, or non-PRD."],
  ],
  {{ rowClasses: ["row-success","row-info","row-warning","row-danger","row-danger",""] }}
);

// Detailed inventory
const inv = document.getElementById("inventory");
solutionOrder.forEach(sol => {{
  const all = prdData.filter(p=>p.solution===sol);
  const act = active.filter(p=>p.solution===sol);
  if (!all.length) return;
  const a = act.length ? avgFn(act).toFixed(1) : "0";
  const det = document.createElement("details");
  const sum = document.createElement("summary");
  const nonActive = all.length - act.length;
  sum.innerHTML = `${{sol}} <span class="count-badge">${{all.length}}</span><span class="summary-pills"><span class="pill ${{Number(a) >= 4 ? "pill-success" : Number(a) >= 3 ? "pill-warning" : "pill-danger"}}">Avg: ${{a}}</span>${{nonActive > 0 ? `<span class="pill pill-neutral">+${{nonActive}} non-PRD</span>` : ""}}</span>`;
  det.appendChild(sum);
  const body = document.createElement("div");
  body.className = "detail-body";
  const tbl = document.createElement("table");
  makeTable(tbl,
    ["PRD Name","Score","Date","Directory","Branch","Type","Assessment"],
    all.map(p => [
      p.name,
      p.score === 0 ? '<span class="score-0">N/A</span>' : `<span class="score-${{Math.min(5,p.score)}}">${{p.score}}/5 ${{scoreLabel(p.score)}}</span>`,
      p.date, p.location, p.branch === "main" ? "main" : p.branch, p.docType, p.notes
    ]),
    {{ rowClasses: all.map(p => rowClass(p)) }}
  );
  body.appendChild(tbl);
  det.appendChild(body);
  inv.appendChild(det);
}});

// Recommendations
makeTable(document.getElementById("recommendations"),
  ["Priority","Action","Impact"],
  [
    ["1","Merge branch-only PRDs to main","High-quality PRDs are invisible on main"],
    ["2","Move mislocated discovery/ PRDs to solutions/","Single canonical location"],
    ["3","Delete duplicate PRDs from discovery/","Eliminates version confusion"],
    ["4","Reclassify non-PRD docs (specs, data models)","Cleaner inventory"],
    ["5","Add US-XX identifiers where missing","Formal story numbering"],
    ["6","Retrofit low-scoring PRDs with template sections","Raises overall quality"],
    ["7","Rename KB ingestions to avoid PRD confusion","Cleaner audits"],
  ]
);

document.getElementById("footer").textContent = `Generated {timestamp}. ${{prdData.length}} files scanned, ${{active.length}} active PRDs. Scored by Claude Opus 4.6 via Amazon Bedrock against the create-prd skill template.`;
</script>
</body>
</html>"""
    output_path.write_text(html, encoding="utf-8")
    print(f"HTML report written to {output_path}")


def main():
    global REPO_ROOT

    parser = argparse.ArgumentParser(description="PRD Quality Analyzer for inx-context")
    parser.add_argument("--repo", type=str, required=True, help="Path to the inx-context git repository")
    parser.add_argument("--dry-run", action="store_true", help="Scan files but skip LLM evaluation")
    parser.add_argument("--skip-llm", action="store_true", help="Use heuristic scoring instead of Bedrock")
    parser.add_argument("--data-file", type=str, help="Load previously saved JSON data and regenerate HTML only")
    parser.add_argument("--output", type=str, default=None, help="Output HTML path (default: reports/prd-quality-report.html)")
    parser.add_argument("--model", type=str, default=BEDROCK_MODEL_ID, help="Bedrock model ID")
    parser.add_argument("--region", type=str, default=BEDROCK_REGION, help="AWS region for Bedrock")
    args = parser.parse_args()

    REPO_ROOT = Path(args.repo).resolve()
    if not (REPO_ROOT / ".git").exists():
        print(f"Error: {REPO_ROOT} is not a git repository")
        sys.exit(1)

    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    data_path = Path(args.data_file) if args.data_file else data_dir / "prd-data.json"
    output_path = Path(args.output) if args.output else reports_dir / "prd-quality-report.html"

    # If data file exists and we're not re-scanning, use it
    if args.data_file and data_path.exists() and not args.dry_run:
        print(f"Loading existing data from {data_path}")
        prd_entries = json.loads(data_path.read_text())
        generate_html(prd_entries, output_path)
        return

    # Step 1: Scan for PRD files
    print("=" * 60)
    print("Step 1: Scanning repository for PRD files")
    print("=" * 60)
    candidates = find_prd_files()

    if args.dry_run:
        print(f"\n[DRY RUN] Found {len(candidates)} PRD candidates:")
        for c in candidates:
            print(f"  {'[main]' if c['on_main'] else '[' + c['branch'] + ']':40s} {c['path']}")
        return

    # Step 2: Read contents and get dates
    print("\n" + "=" * 60)
    print("Step 2: Reading file contents and git dates")
    print("=" * 60)
    file_data = []
    for i, c in enumerate(candidates):
        path, branch = c["path"], c["branch"]
        print(f"  [{i+1}/{len(candidates)}] {path} ({branch})")
        try:
            content = get_file_content(path, branch)
            if not content.strip():
                print(f"    [SKIP] Empty file")
                continue
            date = get_file_creation_date(path, branch)
            file_data.append({
                "path": path,
                "branch": branch,
                "on_main": c["on_main"],
                "content": content,
                "date": date,
                "solution": extract_solution(path),
                "location": extract_location(path),
            })
        except Exception as e:
            print(f"    [ERROR] {e}")

    print(f"\nRead {len(file_data)} files successfully")

    # Step 3: Evaluate with LLM or heuristics
    print("\n" + "=" * 60)
    if args.skip_llm:
        print("Step 3: Evaluating with heuristics (--skip-llm)")
    else:
        print(f"Step 3: Evaluating with Bedrock ({args.model})")
    print("=" * 60)

    bedrock_client = None
    if not args.skip_llm:
        try:
            import boto3
            session = boto3.Session(region_name=args.region)
            bedrock_client = session.client("bedrock-runtime")
            print(f"  Bedrock client ready (region: {args.region})")
        except Exception as e:
            print(f"  [ERROR] Cannot create Bedrock client: {e}")
            print("  Falling back to heuristic scoring")
            args.skip_llm = True

    prd_entries = []
    for i, fd in enumerate(file_data):
        name = Path(fd["path"]).stem
        if name.lower() == "prd":
            parent = Path(fd["path"]).parent.name
            name = parent

        post_may12 = fd["date"] >= MAY12_CUTOFF if fd["date"] != "unknown" else False
        print(f"  [{i+1}/{len(file_data)}] {fd['path']}")

        if args.skip_llm:
            result = evaluate_heuristic(fd["path"], fd["content"])
        else:
            try:
                result = evaluate_with_bedrock(fd["path"], fd["content"], bedrock_client)
                time.sleep(CONCURRENCY_DELAY)
            except Exception as e:
                print(f"    [LLM ERROR] {e} — falling back to heuristic")
                result = evaluate_heuristic(fd["path"], fd["content"])

        score = result.get("score", 0)
        print(f"    Score: {score}/5 ({scoreLabel(score)}) | Type: {result.get('docType', 'PRD')}")

        prd_entries.append({
            "solution": fd["solution"],
            "name": name,
            "date": fd["date"],
            "score": score,
            "branch": fd["branch"],
            "postMay12": post_may12,
            "notes": result.get("notes", ""),
            "docType": result.get("docType", "PRD"),
            "location": fd["location"],
            "path": fd["path"],
        })

    # Step 4: Save data
    print(f"\nSaving data to {data_path}")
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(prd_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    # Also save a timestamped copy
    ts = datetime.now().strftime("%Y%m%d-%H%M")
    archive_path = data_dir / f"prd-data-{ts}.json"
    archive_path.write_text(json.dumps(prd_entries, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Archived data to {archive_path}")

    # Step 5: Generate HTML
    print("\n" + "=" * 60)
    print("Step 5: Generating HTML report")
    print("=" * 60)
    generate_html(prd_entries, output_path)

    # Summary
    active_entries = [e for e in prd_entries if e["docType"] not in ("KB Ingestion", "Duplicate", "Pointer")]
    print(f"\n{'=' * 60}")
    print(f"DONE")
    print(f"  Total files: {len(prd_entries)}")
    print(f"  Active PRDs: {len(active_entries)}")
    if active_entries:
        overall = sum(e["score"] for e in active_entries) / len(active_entries)
        print(f"  Overall avg: {overall:.1f}/5")
    print(f"  Data: {data_path}")
    print(f"  Report: {output_path}")
    print(f"{'=' * 60}")


def scoreLabel(sc):
    if sc >= 5: return "Exemplary"
    if sc >= 4: return "Strong"
    if sc >= 3: return "Partial"
    if sc >= 2: return "Minimal"
    if sc >= 1: return "None"
    return "N/A"


if __name__ == "__main__":
    main()
