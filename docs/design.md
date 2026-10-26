# revenue-health-agent — design

<!-- The design, stable: what the parts are and how they connect. Sectioned with numbered
     headings so a phase can name a section ("read §3 of docs/design.md") and a worker can
     read it without the file around it. Rationale lives here, not in PLAN.md; the plan
     carries the rule and points at the reason. -->

The entry for the GenAI.Works Open Agent Hackathon 2026, track Solving Fragmented
Intelligence.

## 1. Goal

An equity analyst asks "can I trust the revenue series for company X?" and gets back a
report that lists every reporting period found in the SEC's XBRL facts, marks the
periods that are missing, duplicated, restated or reported under a changed tag, cites
the Zetaris rows behind every claim, states a confidence per period, and says what the
analyst should do about each problem. Today this is a manual job of reading filings.

The entry has to show, in one run, the things the judges look for: several named agents
handing work to each other, a critic that sends weak findings back, a branch taken on
low confidence, a retry with a different query, a fallback when a source fails, and a
trace of all of it with the model that served each request.

## 2. Parts

All code is Python. Frameworks are not scored, so the agents are
plain classes around one Nightshift chat-completion client; no agent framework.

- **Planner.** Reads the request (company or list of companies, period range, optional
  focus), checks which Zetaris tables exist, and emits a work plan: one bounded step per
  company and concept. Delegates by input: a company with a known tag change gets an
  extra "alternate concept" step from the start.
- **Retriever.** The only part that talks to Zetaris. Runs read-only SQL over REST,
  returns rows as typed records, logs the statement and row count. Refuses anything that
  is not `SELECT`, `SHOW` or `DESCRIBE`.
- **Analyst.** Turns rows into findings: gaps between consecutive fiscal periods,
  duplicate values across 10-K and 10-Q forms, restatements (same period, later filing,
  different value), series that stop early, series that start late. Each finding carries
  the row identifiers it rests on. The model writes the explanation; the arithmetic is
  code.
- **Evaluator.** Reads each finding and its evidence rows and either accepts it, rejects
  it with a reason, or asks for a retry. Two branches: a truncated series triggers a
  retry through the Retriever with the alternate revenue concept; a finding whose
  evidence rows do not support it is sent back to the Analyst once, then dropped and
  recorded as unsupported.
- **Reporter.** Writes the report from accepted findings only: per company, per period,
  status, confidence, evidence, recommendation, limitations. Markdown and JSON.
- **Markovian orchestrator.** The Meterless engine. Owns the step loop: takes the
  Planner's steps, runs each with a bounded context made of the compressed carry-over
  from earlier steps plus that step's own rows, compresses the result into the carry-over
  and moves on. Never calls a model itself; the generator is injected. The engine test
  proves that context per step stays bounded while the number of steps grows and that
  the carry-over keeps what later steps need.
- **Nightshift client.** OpenAI-compatible calls to the local gateway. Pins the
  Nemotron lane for every request, stores the compute receipt (requested and routed
  model, provider, timing) with the trace event.
- **Trace log.** One JSONL file per run under `logs/`, mirrored to stdout: trace id,
  agent name, action, target agent, confidence, retry count, status, model receipt.
- **Sample mode.** `SAMPLE_MODE=true` swaps the Retriever's REST channel for cached
  query results and the Nightshift client for recorded completions keyed by prompt
  hash, so the same agent logic runs with an empty `.env`. The cached files are real
  outputs saved from live runs and are documented as such.
- **Entry points.** `run.py --input input_examples/N.json` and `POST /run` on a small
  HTTP server, both returning the same JSON. A Dockerfile runs the same thing.

## 3. Data

Everything the agent reads comes through the Zetaris REST endpoint as SQL tables:

- **SEC EDGAR XBRL company facts.** Per company, the `us-gaap/Revenues` concept: period
  start and end, value in USD, accession number, fiscal year and period, form type,
  filed date. A second concept, the post-2018 revenue tag, is registered for the
  alternate-concept retry. Live queries only; EDGAR data is not redistributed, except
  the small cached sample results that sample mode needs, labelled as such.
- **SEC EDGAR submissions.** Per company: name, SIC code, filing metadata. Joined to the
  SIC reference through the `sic_edgar_usl` semantic layer, which also carries the
  foreign-key data-quality rule whose one failure (IBM) the report mentions.
- **company_dns SIC codes.** The 1,005-code reference with division, major group and
  industry group.

Known anomalies the agent must find on its own, used as the acceptance set: Apple's
series ends at fiscal 2018 under the old tag; Tesla's history is short; duplicate values
across annual and quarterly forms; IBM's SIC code is missing from the reference.

The companies are the seven Zetaris registered scripts for: Apple, IBM, Oracle,
Walmart, Target, Ford, Tesla. The three sample inputs cover one clean company, one with
the tag change, and a multi-company request, so the outputs differ visibly.

## 4. Not in scope

- Any data source outside Zetaris's verified list, and any write to Zetaris from the
  agent.
- A second Meterless engine before the Markovian test passes.
- A web UI. The product surface is the CLI, the HTTP endpoint and the report files.
- Financial advice. The report judges data quality, not companies.
- Running Zetaris locally; the box is on Hetzner and goes away after 27 October.
