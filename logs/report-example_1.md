# Revenue series health report: Apple

Run `example_1`. Overall confidence 0.93.

## Summary

Apple: the Revenues series stops at 2018-09-29 and continues under RevenueFromContractWithCustomerExcludingAssessedTax from 2018-09-30, a tag change, not missing revenue; the Revenues series starts at 2015-09-27, where the company began using the tag; earlier revenue sits under a concept this run does not read; 9 periods duplicated across 10-K and 10-Q. The Evaluator reviewed every finding: 13 accepted, 0 rejected, 0 unsupported, 0 sent back to the Analyst. 1 explanation contradicting the findings went back to the Analyst.

## Apple

Apple us-gaap/Revenues: 11 rows covering 2015-09-27..2018-09-29; 2 accepted finding(s). The most important: The Revenues series stops at the period ending 2018-09-29: its last value, $265,595,000,000 for the annual period 2017-10-01 to 2018-09-29, comes from 10-K 0000320193-18-000145 filed 2018-11-05. No later period is reported under this concept, though the request expects one ending on or after 2025-10-26.

The RevenueFromContractWithCustomerExcludingAssessedTax series begins late, at 2016-09-25, because Apple adopted this concept only with its 2019 10-K filings; earlier years sit under another tag, so the series does not span the full 2009–2026 window you requested. Duplicate values appear across multiple quarters in 2017 and 2018, each spanning two or four filings, which means counting rows would inflate the total; the tag_change finding indicates the series continues under the new concept without a gap. Overall, you can trust the series for periods after 2016, but the early years are excluded and duplicates require careful handling.

| Period | Status | Concept | Confidence | Evidence rows | What it means | What to do |
|---|---|---|---|---|---|---|
| 2018-09-29/2018-09-30 | tag change | Revenues -> RevenueFromContractWithCustomerExcludingAssessedTax | 0.93 | `0000320193-18-000145:2017-10-01:2018-09-29`, `0000320193-18-000145:2018-07-01:2018-09-29`, `0000320193-19-000119:2018-09-30:2019-09-28` +2 | Revenue moves from Revenues to RevenueFromContractWithCustomerExcludingAssessedTax: the last Revenues period ends 2018-09-29 (10-K 0000320193-18-000145 filed 2018-11-05) and RevenueFromContractWithCustomerExcludingAssessedTax continues from 2018-09-30 (10-K 0000320193-19-000119 filed 2019-10-31). The series is continuous across the tag change, so the stop under Revenues is not missing revenue. RevenueFromContractWithCustomerExcludingAssessedTax also reports 2016-09-25 to 2018-09-29 (10-K 0000320193-19-000119 filed 2019-10-31), the comparatives of later filings, so both concepts cover those periods. | Join the two concepts into one series: Revenues through 2018-09-29, RevenueFromContractWithCustomerExcludingAssessedTax from 2018-09-30. |
| /2018-09-29 | stops early | Revenues | 0.88 | `0000320193-18-000145:2017-10-01:2018-09-29`, `0000320193-18-000145:2018-07-01:2018-09-29` | The Revenues series stops at the period ending 2018-09-29: its last value, $265,595,000,000 for the annual period 2017-10-01 to 2018-09-29, comes from 10-K 0000320193-18-000145 filed 2018-11-05. No later period is reported under this concept, though the request expects one ending on or after 2025-10-26. Explained by the tag change from Revenues to RevenueFromContractWithCustomerExcludingAssessedTax (2018-09-29 / 2018-09-30). | Check whether the company moved revenue to another XBRL concept after 2018-09-29; until then treat later periods as missing under this tag, not as zero. |
| 2016-09-25/2017-09-30 | starts late | RevenueFromContractWithCustomerExcludingAssessedTax | 0.85 | `0000320193-19-000119:2016-09-25:2017-09-30` | The RevenueFromContractWithCustomerExcludingAssessedTax series starts late, at 2016-09-25, because the company began using this concept late: its earliest period (annual, $229,234,000,000) appears only as a comparative in 10-K 0000320193-19-000119 filed 2019-10-31, 761 days after the period ended. Earlier years sit under another revenue concept or predate the company's XBRL filings, so this marks where the tag starts in the data, not revenue missing inside the series; the request covers fiscal years from 2009. Explained by the tag change from Revenues to RevenueFromContractWithCustomerExcludingAssessedTax (2018-09-29 / 2018-09-30). | For history before 2016-09-25, read the company's earlier revenue concept (for example us-gaap/SalesRevenueNet, retired in the 2018 taxonomy); this series cannot supply it. |
| 2015-09-27/2016-09-24 | starts late | Revenues | 0.85 | `0000320193-18-000145:2015-09-27:2016-09-24` | The Revenues series starts late, at 2015-09-27, because the company began using this concept late: its earliest period (annual, $215,639,000,000) appears only as a comparative in 10-K 0000320193-18-000145 filed 2018-11-05, 772 days after the period ended. Earlier years sit under another revenue concept or predate the company's XBRL filings, so this marks where the tag starts in the data, not revenue missing inside the series; the request covers fiscal years from 2009. | For history before 2015-09-27, read the company's earlier revenue concept (for example us-gaap/SalesRevenueNet, retired in the 2018 taxonomy); this series cannot supply it. |

**Duplicated across 10-K and 10-Q forms** (9 periods, confidence 0.95-0.95): 2017-10-01/2017-12-30, 2017-12-31/2018-03-31, 2018-04-01/2018-06-30, 2018-09-30/2018-12-29, 2018-12-30/2019-03-30, 2019-03-31/2019-06-29, 2019-09-29/2019-12-28, 2019-12-29/2020-03-28 and 1 more. Example: The quarterly period 2017-10-01 to 2017-12-30 carries the same value, $88,293,000,000, in 2 filings across forms 10-K, 10-Q (first in 10-Q 0000320193-19-000010 filed 2019-01-30 and 10-K 0000320193-19-000119 filed 2019-10-31). Summing or counting rows double-counts it. Rows: `0000320193-19-000010:2017-10-01:2017-12-30`, `0000320193-19-000119:2017-10-01:2017-12-30`.

## Recommended actions

- Apple: Join the two concepts into one series: Revenues through 2018-09-29, RevenueFromContractWithCustomerExcludingAssessedTax from 2018-09-30.
- Apple: For history before 2015-09-27, read the company's earlier revenue concept (for example us-gaap/SalesRevenueNet, retired in the 2018 taxonomy); this series cannot supply it.
- Apple RevenueFromContractWithCustomerExcludingAssessedTax: 9 periods are reported with the same value in both 10-K and 10-Q forms; deduplicate on (period_start, period_end) before summing.

## Limitations

- Only the XBRL concepts us-gaap/Revenues and us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax are read; revenue a company reported under another tag is not seen.
- Facts are as reported to SEC EDGAR's XBRL API and read through Zetaris; the filings' text is not read.
- The report judges the data's reliability, not the companies.

## Review

Agents: Planner, Retriever, Analyst, Evaluator, Reporter. Findings sent back to the Analyst: 0; rejected: 0; unsupported: 0; explanations sent back: 1.

- Apple Revenues, round 1: the critique's summary is left out: it speaks of a gap, but the code found no gap for Apple; the accepted findings are: duplicate, early stop, late start, tag change.
- Apple RevenueFromContractWithCustomerExcludingAssessedTax, round 1: the critique's summary is left out: it counts 3 duplicate finding(s), but the code accepted 9 for Apple.
- The explanation of Apple us-gaap/Revenues contradicted the findings twice (it speaks of a gap, but the code found no gap for Apple; the accepted findings are: early stop, late start); the step is told by the findings alone.

Trace: `logs/trace-example_1.jsonl`
