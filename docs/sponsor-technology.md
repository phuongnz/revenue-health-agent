# Sponsor technology: how Zetaris, Meterless and NVIDIA are used

The four parts the submission asks for, then what was unique. Every claim names the
file that makes it true; nothing here describes behaviour the code does not have.

## Zetaris: the data layer

**Role.** Every row the agent reasons about comes through Zetaris. The agent never
downloads a file and never calls SEC EDGAR itself.

**What was registered** (`scripts/zetaris/`, six SQL files, run once with
`scripts/register_sources.py`, one REST call per statement):

- SEC EDGAR XBRL company facts for seven companies under two revenue concepts,
  `us-gaap/Revenues` and `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax`,
  as REST tables cached in Zetaris and exposed as views `edgar.<company>_revenue_table`
  and `edgar.<company>_rfcc_table`: 1,816 revenue facts in all.
- SEC EDGAR submissions headers per company (name, SIC code), joined to the US SIC
  reference from company_dns into `edgar.all_companies_profile_table`.
- A Zetaris Unified Semantic Layer, `sic_edgar_usl`, with `company` (primary key `cik`,
  foreign key `sic`) and `sic_code`, and its data-quality rules. The rules found a real
  violation: IBM's SIC code 3570 is absent from the 1,005-code reference.

`data/README.md` records every source, its licence and the counts Zetaris returned.

**How the agent reads it** (`app/adapters/zetaris.py`, `app/agents/retriever.py`). The
Retriever is the only agent that talks to Zetaris, over the REST API
(`POST /sql-editor/sqls/run`, bearer key, organisation and request ids). Per run it
sends `SHOW TABLES IN edgar` to learn which concept tables exist, one `SELECT` per
company and concept with the requested window in the `WHERE` clause, the profile table,
and `SHOW DQ ALL INVALID TABLE ... sic_edgar_usl.company` to pick up the semantic
layer's violations. The API key is an admin key, so the adapter allowlists read-only
statements (`SELECT`, `WITH`, `SHOW`, `DESCRIBE`, `LIST DQ`, `RUN DQ`, `SHOW DQ`) and
refuses anything else before it is sent, with a `refuse_statement` trace line. Cells
come back as strings and are typed by the adapter. Every statement is a `query_zetaris`
trace line with its row count, and every finding in the report cites the table, the
statement and the row ids it rests on, so a judge can rerun the statement in Zetaris
and see the rows.

**Why Zetaris matters here.** The series live in three places the SEC publishes
separately, under two tags, in several forms per period. Zetaris turns that into tables
the agent can query the same way whatever the source, and the semantic layer's rules
caught a reference-data problem the agent then reports.

## Meterless: the Markovian engine and Nightshift

**The engine.** `app/engines/markovian.py` implements the Meterless Markovian engine
from the implementation guide and the upstream `engines/markovian` source (commit
`85c5d41`): the chunk configuration with its ranges, the injected generator, the
`[STATE_CHECKPOINT]` and `[TASK_COMPLETE]` marker protocol with the carry-over cascade
(marker, then model summary, then heuristic, then truncation), overlap, token
accounting labelled as estimated, the deadline and abort checks, and the JSONL run
record in `logs/markovian-<run_id>.jsonl` with the per-chunk records and the projected
and actual savings.

**The feature it powers.** The engine runs every investigation
(`app/orchestration/graph.py`). One investigation step, a company under one revenue
concept, is one chunk. Before each chunk the orchestrator runs the Retriever, the
Analyst and the Evaluator on that step; the chunk's text is the Analyst model's
explanation of it. The carry-over between chunks is written by code, not by the model:
completed steps, the remaining queue, the decisions taken (a retry requested, a
finding sent back) and the context. When the Evaluator asks for the alternate concept
of a truncated series, the step goes to the front of the queue and becomes the next
chunk, so the engine's loop is what makes the retry branch possible. The engine
enforces the chunk cap (`maxChunks` = the number of planned steps plus their possible
retries, at most 32) and the run deadline, and a run cut short reports the remaining
steps as a limitation instead of failing.

**The test.** The eight upstream conformance checks are ported to
`tests/test_markovian_conformance.py` under their upstream IDs: eff-001, snap-001,
snap-002, ovl-001, lab-001, ref-001, mrk-001, cfg-001, with the parameters of the
upstream runner, plus a ninth test that every upstream ID is ported.

    tests/test_markovian_conformance.py  9 passed in 0.01s

`tests/test_markovian.py` adds 24 tests on the cascade, the caps, the abort, the
deadline, the JSONL record and the bounded context. All run offline on every commit
through `./test.sh`. This is a port of the upstream checks, not the upstream suite
itself.

**What the engine did and did not buy.** The three shipped runs used one, two and
three chunks, and the run records show `totalTokensSaved: 0`: with steps this short
there is nothing to save. What the engine gives this agent is the bounded state (every
chunk sees the code-written carry-over, never the full history), the marker-driven
completion, the chunk cap and the deadline, and an audit record per step. A request
over all seven companies and both concepts runs up to fourteen chunks and keeps the
same bounded context.

**Nightshift as the provider.** `app/adapters/meterless.py` is the only path to a
model. It calls Nightshift's OpenAI-compatible endpoint (`METERLESS_BASE_URL`, default
`http://127.0.0.1:8088/v1`) with the runtime token, the `meterless-app` and
`meterless-scope` headers, and a fixed model per agent. It keeps the Meterless receipt
from every reply and writes the receipt's `routed_model` into the `model` field of the
`model_call` trace line, with the reduced receipt alongside. The engine's generator is
this adapter; the engine never calls a model itself.

## Cursor

Cursor was not used. The project was built with Claude Code, Anthropic's coding agent,
in planned phases, each phase reviewed in a separate session and merged by the author.
The two kinds of AI were kept apart: the product's Nightshift runtime token and the
NVIDIA key lived in a local vault and were injected into the product process at run
time only, never into the coding tool's model settings. The official checklist lists
the Cursor part as optional; it is stated here rather than left out.

## NVIDIA: where it contributes

Both product models are NVIDIA Nemotron models, served through Nightshift's NVIDIA
route:

- `nvidia/nemotron-3.5-lightning-30b-a3b` writes the Analyst's explanation of each
  step and the engine's end-of-run reflection, and resolves a company name when code
  cannot (thinking off, 2,048 tokens for an explanation).
- `nvidia/nemotron-3-ultra-550b-a55b` is the Evaluator's critic: it reads up to ten
  findings with their evidence and returns a JSON review per finding (reasoning on,
  8,192 tokens, temperature 0.1). Its verdict is averaged with the code's confidence,
  and a rejection sends the finding back to the Analyst.

The adapter requests a named Nemotron model, never `meterless/auto`, and checks the
receipt: a reply routed to any other model is kept but traced `needs_validation`, so
the logs never claim Nemotron for a request Nemotron did not handle. In the three
shipped traces every model call shows one of the two Nemotron ids.

## What was unique

- **The model never decides what is true.** Findings are arithmetic over Zetaris rows;
  the models explain and critique. Every model sentence is checked against the
  findings and sent back once when it contradicts them, so the report cannot say "the
  series is broken" next to a finding that says it is continuous. The prose check is
  deliberately literal and drops more than it should; that trade-off is stated in the
  README.
- **The engine's loop is the collaboration.** The retry branch, the send-back and the
  prose review all happen inside the Markovian chunk loop, with code-written
  carry-over, and all of them are visible in the shipped traces.
- **Honest accounting.** The engine's run records report zero token savings on these
  short runs, and the sponsor explanation says so rather than claiming efficiency the
  logs do not show; the Zetaris data-quality rules found a real violation that the
  agent reports instead of hiding; sample mode fails loudly on a changed prompt
  instead of replaying a stale answer.
