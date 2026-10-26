# Data

Every row the agent reads comes through Zetaris, from the public sources below,
registered once with the scripts in `scripts/zetaris/`. Nothing is downloaded by the
agent itself. Counts are what Zetaris returned on 26 October 2026, right after
registration; they move as companies file.

## Sources

| Source | What | Access and licence | Registered as |
|---|---|---|---|
| SEC EDGAR XBRL company facts, `companyconcept` endpoint | Per company and concept: every reported value with period start and end, value in USD, accession number, fiscal year and period, form type, filing date | Free, no key. SEC asks for a declared `User-Agent` of the form `app contact@email` and allows 10 requests per second. Public domain as US government data; SEC asks that it not be bulk-redistributed, so the agent queries live and `data/sample/` holds only the small subset sample mode needs | `edgar.<co>_revenue_table` (concept `us-gaap/Revenues`, seven companies) and `edgar.<co>_rfcc_table` (concept `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax`, five companies) |
| SEC EDGAR submissions | Per company: name, SIC code and the SEC's own SIC description | As above | `edgar.<co>_profile_table`, `edgar.all_companies_profile_table` |
| company_dns, US SIC reference (V3 and V4) | The 1,005 codes of the 1987 US Standard Industrial Classification with division, major group and industry group | Free, no key, rate limit raised to 6,000 per minute with a declared user agent. Reference data, not redistributed here | `company_dns.sic_codes_table` and variants |
| Zetaris Unified Semantic Layer `sic_edgar_usl` | `company` (primary key `cik`, foreign key `sic` to `sic_code`) and `sic_code`, built from the two sources above, with Zetaris's data-quality rules | Built on the box from the sources above | `lightning.metastore.usl_demo.sic_edgar_usl.company`, `...sic_code` |

Companies: Apple, IBM, Oracle, Walmart, Target, Ford, Tesla, the seven that Zetaris's
verified registration recipes cover.

## What registration observed

Revenue facts under the old concept `us-gaap/Revenues` (`edgar.<co>_revenue_table`):

| Company | Rows | Fiscal years | Accessions |
|---|---|---|---|
| Apple | 11 | 2018 only | 1 |
| IBM | 235 | 2009 to 2026 | 71 |
| Oracle | 141 | 2010 to 2026 | 44 |
| Walmart | 293 | 2009 to 2027 | 70 |
| Target | 101 | 2009 to 2014 | 21 |
| Ford | 231 | 2009 to 2024 | 59 |
| Tesla | 286 | 2011 to 2026 | 63 |

Revenue facts under the post-2018 concept
`us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax` (`edgar.<co>_rfcc_table`):

| Company | Rows | Fiscal years | Accessions |
|---|---|---|---|
| Apple | 117 | 2019 to 2026 | 31 |
| Oracle | 106 | 2019 to 2026 | 33 |
| Walmart | 120 | 2019 to 2027 | 32 |
| Target | 126 | 2018 to 2026 | 34 |
| Tesla | 49 | 2018 to 2026 | 17 |
| IBM | not registered: SEC has no facts for this concept (HTTP 404) | | |
| Ford | not registered: SEC returns the concept with an empty USD list, so no view can be built | | |

These are the anomalies the agent has to find and explain, none of them planted:

- Apple's series under the old concept stops after one 2018 filing and continues under
  the new concept from 2019: a tag change, not missing revenue.
- Target's old-concept series ends in fiscal 2014 while its new-concept series starts
  in 2018: a real gap of three fiscal years between the two tags.
- IBM and Ford never adopted the new concept; their series live only under the old one.
- Walmart's old-concept rows split into 174 10-Q, 4 10-Q/A and 115 10-K forms: the
  same period appears in several filings, and amended forms can restate a value.
- Fiscal years run up to 2027 for Walmart, whose fiscal year ends in January.

SIC reference and semantic layer:

- `company_dns.sic_codes_table`: 1,005 codes; the semantic layer's `sic_code` table
  carries the same 1,005. Primary-key rule: 1,005 of 1,005 valid.
- `sic_edgar_usl.company`: 7 rows. Primary-key rule on `cik`: 7 of 7 valid.
  Foreign-key rule on `sic`: 6 valid, 1 invalid. The invalid row is IBM, SIC 3570
  "Computer & office Equipment", a code absent from the 1,005-code reference.
- `edgar.all_companies_profile_table` shows all seven because the join is a LEFT JOIN;
  IBM's reference columns are empty. Five SEC descriptions differ in wording from the
  reference ("&" against "and"; "Retail-" and "Services-" prefixes).

## Sample mode: the declared subset

`data/sample/` holds what Zetaris and the models answered in three live runs, the three
requests in `input_examples/`, captured on 26 October 2026 by
`scripts/capture_sample.py` (run by hand through `./run.sh`, never from a test). With
`SAMPLE_MODE=true` the agents run unchanged on these answers, with no Zetaris, no
Nightshift and no network.

| Path | Holds |
|---|---|
| `manifest.json` | the capture date (sample mode's "today", so the replayed prompts match), and for each run its input, status, trace and the keys it read |
| `zetaris/<key>.json` | one statement's answer: the statement, headers, rows. Key: sha256 of the statement with whitespace folded, first 16 hex digits |
| `completions/<key>.json` | one model call's answer: text, requested and routed model, Meterless receipt, usage. Key: sha256 of the agent name and the exact messages, first 16 hex digits |

The subset, and nothing more:

| Statement | Rows |
|---|---|
| Apple, `edgar.apple_revenue_table` and `edgar.apple_rfcc_table` | 11 and 117 |
| Oracle, `edgar.oracle_revenue_table`, fiscal 2010 to 2015 | 85 |
| Target, `edgar.target_revenue_table` and `edgar.target_rfcc_table` | 101 and 126 |
| Ford, `edgar.ford_revenue_table` | 231 |
| `edgar.all_companies_profile_table` | 7 |
| `SHOW TABLES IN edgar` | 20 |
| `SHOW DQ ALL INVALID TABLE ...sic_edgar_usl.company` | 1 (IBM's SIC code) |

That is 671 SEC revenue facts of the 1,816 registered, plus 18 model completions.
IBM, Walmart and Tesla are not in the sample; a request for them in sample mode fails
with a clear "not in the sample" error instead of reaching the network.

A changed statement or prompt has a different key, so sample mode fails loudly, naming
the key, instead of replaying a stale answer (the plan's rule). A call that failed live
is stored as its failure and fails the same way on replay. After changing a prompt, run
`./run.sh uv run python scripts/capture_sample.py --fresh` again.
