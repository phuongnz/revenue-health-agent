# revenue-health-agent

**Track:** Solving Fragmented Intelligence · **Sponsor technology:** Zetaris (data
layer), Meterless Markovian engine, Meterless Nightshift with NVIDIA Nemotron ·
**Run:** `./test.sh`

An agent that helps an equity analyst with unreliable revenue time series. It pulls
each company's XBRL revenue facts, filing metadata and industry profile from SEC EDGAR
through Zetaris, detects gaps, tag changes and restatements, and produces a sourced
data-quality report that says which periods can be trusted and why. Built for the
GenAI.Works Open Agent Hackathon 2026.

## Run

How to run it, and how to test it:

    ./test.sh

## Layout

- `scripts/` — registration of the data sources in Zetaris, run once by hand
- `tests/` — the test suite; nothing in it touches the network
- `docs/` — the design

## Read more

- `docs/design.md` — the design
- `THIRD_PARTY_NOTICES.md` — adapted code and its licences
- `LICENSE` — MIT
