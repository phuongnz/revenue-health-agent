-- SEC EDGAR XBRL company facts, us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax (the post-2018 revenue tag), seven companies
-- Adapted from Zetaris's zetaris-hackathon repository, the same recipe as 20_edgar_revenue_facts.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). Endpoint concept, table and view names changed (<co>_rfcc_facts, edgar.<co>_rfcc_table); database and container already exist; CACHE TABLE added. IBM is
-- left out: it never reported this concept, so SEC answers 404 and Zetaris refuses the
-- table (verified 26 October 2026); IBM's series lives only under us-gaap/Revenues. Ford is left out
-- too: SEC returns the concept with an empty USD list, so the view over units.USD
-- cannot be built (same date). Five companies remain.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/25_edgar_revenue_rfcc.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE LIGHTNING REST TABLE apple_rfcc_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.apple_rfcc_facts;

CREATE SCHEMASTORE VIEW apple_rfcc_table WITH CONTAINER edgar AS
SELECT
    cik,
    `entityName` AS entity_name,
    tag AS concept,
    fact.`start` AS period_start,
    fact.`end` AS period_end,
    fact.val AS value_usd,
    fact.accn AS accession_number,
    fact.fy AS fiscal_year,
    fact.fp AS fiscal_period,
    fact.frame AS period_frame,
    fact.form AS form_type,
    fact.filed AS filed_date
FROM sec_data.apple_rfcc_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE oracle_rfcc_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0001341439/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.oracle_rfcc_facts;

CREATE SCHEMASTORE VIEW oracle_rfcc_table WITH CONTAINER edgar AS
SELECT
    cik,
    `entityName` AS entity_name,
    tag AS concept,
    fact.`start` AS period_start,
    fact.`end` AS period_end,
    fact.val AS value_usd,
    fact.accn AS accession_number,
    fact.fy AS fiscal_year,
    fact.fp AS fiscal_period,
    fact.frame AS period_frame,
    fact.form AS form_type,
    fact.filed AS filed_date
FROM sec_data.oracle_rfcc_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE walmart_rfcc_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000104169/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.walmart_rfcc_facts;

CREATE SCHEMASTORE VIEW walmart_rfcc_table WITH CONTAINER edgar AS
SELECT
    cik,
    `entityName` AS entity_name,
    tag AS concept,
    fact.`start` AS period_start,
    fact.`end` AS period_end,
    fact.val AS value_usd,
    fact.accn AS accession_number,
    fact.fy AS fiscal_year,
    fact.fp AS fiscal_period,
    fact.frame AS period_frame,
    fact.form AS form_type,
    fact.filed AS filed_date
FROM sec_data.walmart_rfcc_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE target_rfcc_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000027419/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.target_rfcc_facts;

CREATE SCHEMASTORE VIEW target_rfcc_table WITH CONTAINER edgar AS
SELECT
    cik,
    `entityName` AS entity_name,
    tag AS concept,
    fact.`start` AS period_start,
    fact.`end` AS period_end,
    fact.val AS value_usd,
    fact.accn AS accession_number,
    fact.fy AS fiscal_year,
    fact.fp AS fiscal_period,
    fact.frame AS period_frame,
    fact.form AS form_type,
    fact.filed AS filed_date
FROM sec_data.target_rfcc_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE tesla_rfcc_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0001318605/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.tesla_rfcc_facts;

CREATE SCHEMASTORE VIEW tesla_rfcc_table WITH CONTAINER edgar AS
SELECT
    cik,
    `entityName` AS entity_name,
    tag AS concept,
    fact.`start` AS period_start,
    fact.`end` AS period_end,
    fact.val AS value_usd,
    fact.accn AS accession_number,
    fact.fy AS fiscal_year,
    fact.fp AS fiscal_period,
    fact.frame AS period_frame,
    fact.form AS form_type,
    fact.filed AS filed_date
FROM sec_data.tesla_rfcc_facts
LATERAL VIEW explode(units.USD) AS fact;
