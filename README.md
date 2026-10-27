# revenue-health-agent

**Track:** Solving Fragmented Intelligence
**Sponsor technology:** Zetaris (data layer) · Meterless Markovian engine · Meterless Nightshift with NVIDIA Nemotron
**Run command:** `SAMPLE_MODE=true python run.py --input input_examples/example_1.json`

An agent for the equity analyst who asks "can I trust this company's reported revenue
series?" It reads the company's XBRL revenue facts, filing metadata and industry profile
from SEC EDGAR through Zetaris, finds the periods that are missing, duplicated, restated
or reported under a changed tag, has every finding checked and critiqued before it is
accepted, and writes a report that cites the Zetaris rows behind each claim, gives a
confidence per finding and says what to do about it. Built solo for the GenAI.Works Open
Agent Hackathon 2026, 22 to 27 October.

## Problem

A revenue time series pulled from SEC filings is not one series. The same quarter is
reported in the 10-Q, again in the 10-K, again in an amended filing, and again as a
comparative in the next year's filings, sometimes with a different value. In 2018 most
companies moved from the XBRL tag `us-gaap:Revenues` to
`us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`, so one series stops and
another starts, and some companies never moved. Anyone building a long-run growth model
on these numbers has to find out, period by period, which value to trust, and today that
means reading filings by hand. The user is an analyst or a data engineer preparing
fundamentals; the input is a question in plain words plus the companies and window; the
output is a sourced data-quality report, as JSON and Markdown.

## Agents

Five agents, each a Python class with one job. The findings themselves are arithmetic
in code, never a model's opinion; the models explain, critique and resolve names.

| Agent | Role | Model, through Nightshift | Can block or retry? |
|---|---|---|---|
| Planner | Resolves the companies from the request, plans one step per company and concept, and plans the alternate concept up front when the question is about tags | Nemotron 3.5 Lightning, only when code cannot resolve a company | Blocks the run with `invalid_input` when no company is found |
| Retriever | The only agent that talks to Zetaris: lists the tables, reads the revenue facts, the profile table and the data-quality violations, through a read-only statement allowlist | none | Fails a step on a Zetaris error after bounded retries; the run continues with a limitation |
| Analyst | Finds gaps, cross-form duplicates, restatements, early stops, late starts and tag changes from the rows, each finding citing its row ids; writes the explanation of each step | Nemotron 3.5 Lightning for the explanations | Is the target of send-backs: re-derives a finding or rewrites an explanation once |
| Evaluator | Checks every finding against its rows in code, has the set critiqued by the deep model, holds the model's prose to the code findings, and decides whether a truncated series needs the alternate concept | Nemotron 3 Ultra for the critique | Sends a finding back once, marks it unsupported or rejected, requests a retry step or takes the fallback |
| Reporter | Builds the result from accepted findings only: summary, confidence, limitations, recommended actions, the Markdown report | none | Drops any model sentence that contradicts a finding |

## What makes this an agent system

The agents do not run as a fixed chain. Three loops change the path of a run, and all
three appear in the traces of the three shipped examples:

- **Critique and send-back.** The Evaluator rejects a finding, in code or through the
  Nemotron Ultra critique, and sends it back to the Analyst, which re-derives it from
  the rows. A finding that fails twice is recorded as unsupported or rejected and never
  reaches the report (`send_back`, `record_unsupported`).
- **Retry branch.** When a series stops or starts inside the requested window and the
  company has a table under the other revenue tag, the Evaluator asks for that step
  (`request_retry`), the orchestrator puts it at the front of the queue
  (`enqueue_retry`), and the Analyst compares the two series to decide between a tag
  change and a real gap. When no alternate table exists, as for IBM and Ford, the run
  takes the fallback and says so (`retry_fallback`).
- **Prose review.** Every sentence a model writes is checked against the accepted
  findings: a kind of problem that was not found, a count, a date, a dollar figure or a
  window length that does not match is a contradiction. The explanation goes back to
  the Analyst once (`review_explanation`, `send_back`); if it still contradicts, the
  code-written explanation is used (`explain_fallback`). The code findings are the
  truth; the model never gets the last word.

The Markovian engine drives the run step by step inside a bounded context: every step
is one chunk, the state carried between chunks is written by code (completed steps,
remaining queue, decisions, context), and the engine enforces the chunk cap and the
deadline.

## Architecture

```
 request {query, companies, window}
            │
            ▼
       ┌─────────┐   resolve, plan steps       ┌──────────────────────────────┐
       │ Planner │ ───────────────────────────▶ │ Markovian engine             │
       └─────────┘   (Lightning, fallback only) │ one step = one chunk          │
                                                │ carry-over written by code    │
      ┌────────────────────── per chunk ────────┤ chunk cap, deadline, JSONL    │
      │                                         └──────────────────────────────┘
      ▼
 ┌───────────┐  rows   ┌─────────┐  findings  ┌───────────┐ accepted ┌──────────┐
 │ Retriever │ ──────▶ │ Analyst │ ─────────▶ │ Evaluator │ ───────▶ │ Reporter │
 └───────────┘         └─────────┘            └───────────┘          └──────────┘
      │                    ▲    ▲                 │   │
      │ SQL, read-only     │    └── send-back ────┘   │ request_retry
      ▼                    │        (once)            ▼
 ┌─────────┐               │                    next chunk = alternate concept
 │ Zetaris │               └── explanations (Lightning), critique (Ultra)
 │  REST   │                   every call through Meterless Nightshift,
 └─────────┘                   routed model read from the receipt
      │
 SEC EDGAR XBRL facts, submissions, SIC reference, semantic layer
```

Every arrow is a trace line in `logs/trace-<run_id>.jsonl` and on stdout.

## Tech stack

- **Zetaris** is the data layer. The SEC EDGAR revenue facts, the submissions headers,
  the US SIC reference and a Unified Semantic Layer with data-quality rules are
  registered in Zetaris once (`scripts/zetaris/`), and the agent reads every row through
  the Zetaris REST API (`POST /sql-editor/sqls/run`). Nothing is downloaded by the agent.
- **Meterless Markovian engine**, implemented in `app/engines/markovian.py` from the
  Meterless implementation guide, with the eight upstream conformance checks ported to
  `tests/test_markovian_conformance.py`. It runs the whole investigation.
- **Meterless Nightshift** is the only model provider. `app/adapters/meterless.py` calls
  its OpenAI-compatible endpoint, pins a Nemotron model per agent, keeps the Meterless
  receipt and writes the receipt's `routed_model` into every trace line of a model call.
- **NVIDIA Nemotron**: `nvidia/nemotron-3.5-lightning-30b-a3b` for explanations and
  company resolution, `nvidia/nemotron-3-ultra-550b-a55b` for the critique, both through
  Nightshift's NVIDIA route. A reply routed to any other model is kept but traced
  `needs_validation`.
- Python 3.11+, pydantic 2, httpx, tenacity; the standard library HTTP server; pytest.
  No agent framework.

## Setup

    git clone https://github.com/phuongnz/revenue-health-agent.git
    cd revenue-health-agent
    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt          # or: uv sync
    cp .env.example .env                     # fill in only for live mode

Live mode needs two services and the keys in `.env`:

- A Zetaris instance with the sources registered: run the files in `scripts/zetaris/`
  in order with `scripts/register_sources.py` (see `scripts/zetaris/README.md`), then set
  `ZETARIS_REST_URL`, `ZETARIS_ORG_ID` and `ZETARIS_API_KEY`.
- A Meterless Nightshift gateway with an NVIDIA provider configured: set
  `METERLESS_BASE_URL` (default `http://127.0.0.1:8088/v1`) and `METERLESS_RUNTIME_TOKEN`.

Sample mode needs neither: `SAMPLE_MODE=true` with an empty `.env`.

## Usage

One request from the command line, trace lines on stdout, then the response JSON:

    SAMPLE_MODE=true python run.py --input input_examples/example_1.json
    python run.py --input input_examples/example_3.json --output out.json   # live

The HTTP server, `POST /run` on `0.0.0.0:$PORT` (default 8000), the same request body,
the same response, plus `GET /health`:

    python run.py
    curl -s -X POST localhost:8000/run -H 'content-type: application/json' \
         -d @input_examples/example_1.json

In Docker:

    docker build -t revenue-health-agent .
    docker run --rm -p 8000:8000 -e SAMPLE_MODE=true revenue-health-agent
    docker run --rm -p 8000:8000 --env-file .env revenue-health-agent   # live; on Linux add
                                              # --add-host host.docker.internal:host-gateway

Request:

    {"run_id": "example_1",
     "input": {"query": "Can I trust Apple's reported revenue series from 2009 to today for a long-run growth model?",
               "companies": ["AAPL"]},
     "options": {"sample_mode": true}}

`input` takes `query` (required), `companies` (tickers, CIKs or names), `period_start`,
`period_end` (a fiscal year or an ISO date) and `focus`; `run_id` is optional.

Response: `run_id`, `trace_id`, `status` (`success`, `partial_success`, `failed`),
`agents_used`, `result` (`summary`, `findings` with `evidence`, `confidence`, `status`
and `sources` naming the table, statement and row ids, `confidence`, `limitations`,
`recommended_actions`), `collaboration` (`feedback_loop_completed`, `revision_count`,
`evaluator_findings`), `log_path`, `execution_time_seconds`, and on failure
`error: {type, message, recoverable}`. The three shipped requests and their real
outputs are in `input_examples/` and `output_examples/`, with their traces, engine
records and Markdown reports in `logs/`. The outputs differ: 13 findings for Apple, none
for Oracle 2010 to 2015, 67 for Target and Ford.

The checkpoint, a secret scan over tracked files plus the test suite (213 tests, no
network):

    ./test.sh

## Sample mode

`SAMPLE_MODE=true`, or `options.sample_mode` in the request, runs the same agents, the
same engine and the same checks on the answers Zetaris and the models gave in three
live runs on 26 October 2026, stored under `data/sample/` keyed by the hash of the
statement or of the exact prompt. Nothing is read from `output_examples/` at runtime; a
test proves it with an audit hook. A request the sample does not cover, or a prompt
that changed since the capture, fails loudly with `sample_miss` instead of replaying a
stale answer. The sample covers Apple, Oracle 2010 to 2015, Target and Ford; the other
registered companies need live mode.

## Failure handling

| Failure | What happens |
|---|---|
| Zetaris timeout, 5xx, 429 or transport error | 3 attempts, exponential wait 1 to 8 s, `Retry-After` honoured up to 30 s; statement timeout 45 s (30 to 60) |
| A revenue read still fails | the step is recorded failed, a limitation is added, the run continues; if no step read any rows the run ends `failed` with `source_unavailable` |
| A statement that is not read-only | refused before it is sent, traced `refuse_statement` |
| Nightshift timeout, 5xx, 429 or transport error | 3 attempts, exponential wait 1 to 8 s; call timeout 60 s (30 to 90) |
| No model reachable, or the critique fails | the run continues code-only: findings stand on the code check, explanations are code-written, the limitation says so, status `partial_success` |
| The model's explanation contradicts a finding | sent back once, then replaced by the code-written explanation |
| A reply routed off Nemotron or without a receipt | kept, traced `needs_validation` |
| A truncated series with no alternate table | `retry_fallback`, the limitation names the company |
| Step deadline (180 s) or run deadline (480 s) | model calls past the deadline are refused and the agent takes its fallback; the engine aborts before the next chunk, the remaining steps become a limitation (`loop_capped`), status `partial_success` |
| Server wall clock (run deadline plus 30 s) | `failed` with `error.type = timeout`, recoverable |
| Bad request | HTTP 400 with `error.type = invalid_input`; duplicate `run_id` in flight 409; body over 1 MB 413 |
| Anything else | `internal_error`, recoverable; the run never raises |

## Data and licensing

All data comes from public sources through Zetaris; `data/README.md` lists each source,
its licence, what was registered and the counts observed.

- **SEC EDGAR** XBRL company facts and submissions: US government data, public domain.
  The SEC asks that it not be bulk-redistributed, so the agent queries live and the
  repository holds only the declared subset in `data/sample/`: 671 revenue facts for
  Apple, Oracle, Target and Ford, the seven profile rows and one data-quality row, out
  of the 1,816 facts registered.
- **US SIC reference** from company_dns: reference data, read through Zetaris, not
  redistributed.
- **Zetaris Unified Semantic Layer** `sic_edgar_usl`, built on the box from the two
  sources above, with its primary-key and foreign-key rules.
- No personal data, no synthetic data. The 18 model completions in the sample are real
  Nemotron answers with their Meterless receipts.

## Limitations

- Seven companies (Apple, IBM, Oracle, Walmart, Target, Ford, Tesla), the ones Zetaris's
  verified registration recipes cover; two revenue tags. Revenue a company reported
  under an earlier tag is not read.
- The prose check is deliberately literal. It drops a correct explanation that rounds a
  figure or uses the word "gap" loosely, so reports lean on code-written text; in the
  shipped examples three of five live explanations were replaced.
- The critique reviews at most ten findings per company and step, at most three
  duplicates and three restatements among them; the rest stand on the code check.
- Sample mode holds only the three examples and is keyed by exact prompt text, so any
  prompt change needs a live recapture (`scripts/capture_sample.py`).
- Token counts and the engine's efficiency figures are estimates (characters over four).
- A server timeout returns a controlled error but cannot stop the worker thread, which
  finishes in the background.
- Live mode depends on a Zetaris instance with the sources registered and a Nightshift
  gateway; the instance used for the hackathon is not public.
- This is a data-quality tool, not financial advice.

## Pre-existing components

None. Everything in this repository was written between 22 and 27 October 2026. The
code adapted from open-source projects is listed under Third-Party Code and Licences.

## Demo Video

https://vimeo.com/1239826516 (3 min 15 s): the problem, the solution and the
architecture from the deck, then a live run against Zetaris and Nightshift and two
filters over its trace.

## AI and Model Usage

**Models, through Meterless Nightshift**, the product's only AI provider:

- `nvidia/nemotron-3.5-lightning-30b-a3b` (NVIDIA Nemotron 3.5 Lightning 30B A3B):
  the Analyst's step explanations and the engine's reflection; the Planner's company
  resolution when code cannot resolve a name.
- `nvidia/nemotron-3-ultra-550b-a55b` (NVIDIA Nemotron 3 Ultra 550B A55B, reasoning
  on): the Evaluator's critique of the findings.

Each call requests a fixed Nemotron model, never `meterless/auto`; the Meterless receipt
is kept and its `routed_model` is the `model` field of the trace line, so the logs say
which model handled each request. Findings are never produced by a model.

**Sponsor technology:** Zetaris as the data layer (`app/adapters/zetaris.py`,
`scripts/zetaris/`); the Meterless Markovian engine (`app/engines/markovian.py`, used by
`app/orchestration/graph.py`, tested by `tests/test_markovian_conformance.py` and
`tests/test_markovian.py`); Nightshift as the provider (`app/adapters/meterless.py`).
`metadata.json` names the same.

**Development tools:** the code and the documents were written with Claude Code
(Anthropic's coding agent) in planned phases, each phase reviewed in a separate session
and merged by the author. The product's Nightshift token and the NVIDIA key were kept in
a local vault and injected into the product at run time only; they were never in the
coding tool's model settings. Generated code was reviewed and tested: 213 tests run
offline on every commit, and the three shipped outputs come from live runs.

**No framework:** the agents, the engine and the orchestration are plain Python.
Libraries: pydantic, httpx, tenacity, pytest.

**Sample mode** replays captured answers so the same logic runs with no keys and no
network; it is documented above and in `data/README.md`, and is never the default.

## Third-Party Code and Licences

`THIRD_PARTY_NOTICES.md` lists the adapted code in full. In short:

- Zetaris `zetaris-hackathon` (Apache License 2.0): the statement splitter, user-agent
  handling and REST request in `scripts/register_sources.py`, and the registration
  recipes in `scripts/zetaris/`.
- Meterless `engines/markovian` (Apache License 2.0, commit `85c5d41`): the engine
  contract that `app/engines/markovian.py` implements and the conformance checks ported
  to `tests/test_markovian_conformance.py`.
- Everything else is original and under the MIT licence in `LICENSE`.

## Run

    SAMPLE_MODE=true python run.py --input input_examples/example_1.json
    ./test.sh

## Layout

- `run.py` — the CLI and the `POST /run` server
- `app/config.py`, `app/schemas.py`, `app/tracing.py`, `app/server.py` — settings from the environment, the request, response and trace schemas, the JSONL trace log
- `app/agents/` — Planner, Retriever, Analyst, Evaluator, Reporter
- `app/orchestration/` — the run through the engine, the step queue, the carry-over state
- `app/engines/markovian.py` — the Markovian engine
- `app/adapters/` — Zetaris (read-only), Nightshift, sample replay and capture
- `data/README.md`, `data/sample/` — sources, licences, counts; the declared sample subset
- `input_examples/`, `output_examples/`, `logs/` — the three requests, their real outputs, traces and reports
- `scripts/` — registration of the sources in Zetaris, run once by hand; the sample capture
- `tests/` — the test suite; nothing in it touches the network
- `Dockerfile`, `.env.example`, `requirements.txt`, `metadata.json`

## Read more

- `docs/design.md` — the design
- `docs/sponsor-technology.md` — how Zetaris, Meterless and NVIDIA are used
- `data/README.md` — the data, its licences and the anomalies it carries
- `THIRD_PARTY_NOTICES.md` — adapted code and its licences
- `LICENSE` — MIT
