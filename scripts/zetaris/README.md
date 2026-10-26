# Registering the data sources in Zetaris

The agent reads every row through Zetaris, so the public sources below are registered
there once as SQL tables and views. The files run in numeric order; later ones depend on
earlier ones. Each statement is one REST call. The scripts call rate-limited public
APIs (SEC EDGAR: 10 requests per second with a declared user agent; company_dns:
6,000 per minute with one), so they are run by hand, once, never from a test or a loop.

| File | Registers | Needs |
|---|---|---|
| `10_company_dns_sic.sql` | database `COMPANY_DNS`, the 1,005-code US SIC reference (V3 and V4), cached, views `company_dns.sic_codes_table` and variants | nothing |
| `20_edgar_revenue_facts.sql` | database `SEC_DATA`, container `edgar`, per company the XBRL concept `us-gaap/Revenues`, cached, views `edgar.<co>_revenue_table` | nothing |
| `25_edgar_revenue_rfcc.sql` | per company the post-2018 concept `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax`, cached, views `edgar.<co>_rfcc_table` | 20 |
| `30_edgar_submissions.sql` | per company the EDGAR submissions header (name, SIC), cached, views `edgar.<co>_profile_table` and `edgar.all_companies_profile_table` (LEFT JOIN, so IBM stays) | 10, 20 |
| `40_sic_usl.sql` | namespace `lightning.metastore.usl_demo` and the semantic layer `sic_usl` | 10 |
| `50_sic_edgar_usl.sql` | the semantic layer `sic_edgar_usl` (`company` with PK `cik` and FK `sic` to `sic_code`) and its data-quality run | 10, 30, 40 |

Companies: Apple, IBM, Oracle, Walmart, Target, Ford, Tesla (the seven that Zetaris's
own verified recipes cover).

## Running

Credentials come from the environment, never from a file in this repository. The
maintainer's wrapper injects them from a vault. If you run this yourself, export them in
the shell, or put them in a local `.env` copied from `.env.example` (it is ignored by
git) and run `set -a; . ./.env; set +a` first.

    export ZETARIS_API_KEY=...            # from the Zetaris UI, Settings > API keys
    export ZETARIS_REST_URL=http://localhost:8888/api/v1.0
    export ZETARIS_ORG_ID=1
    export ZETARIS_USER_AGENT="your-app you@example.com"   # SEC and company_dns require it

    python3 scripts/register_sources.py scripts/zetaris/10_company_dns_sic.sql --dry-run
    python3 scripts/register_sources.py scripts/zetaris/10_company_dns_sic.sql --skip-exists
    ... and so on in order.

`--skip-exists` reports and skips an object that is already there, so a file can be
re-run after a partial failure. The cache is per Zetaris session: after a restart of the
platform, run the `CACHE TABLE` statements again (`-e "CACHE TABLE sec_data.apple_revenue_facts"`).

## Checking

    python3 scripts/register_sources.py -e "SELECT COUNT(*) FROM edgar.apple_revenue_table"
    python3 scripts/register_sources.py -e "SHOW DQ ALL INVALID TABLE lightning.metastore.usl_demo.sic_edgar_usl.company"

`data/README.md` records the counts observed at registration and each source's licence.
