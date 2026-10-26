-- SEC EDGAR XBRL company facts, us-gaap/Revenues, seven companies
-- Adapted from Zetaris's zetaris-hackathon repository, open_data/rest_apis/sql/rate_limited/01_edgar_company_facts_create.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). A CACHE TABLE was added after each REST table so agent queries do not re-hit SEC.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/20_edgar_revenue_facts.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE LIGHTNING DATABASE SEC_DATA DESCRIBE BY "SEC EDGAR XBRL company facts REST source";

CREATE SCHEMASTORE CONTAINER edgar;

CREATE LIGHTNING REST TABLE apple_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.apple_revenue_facts;

CREATE SCHEMASTORE VIEW apple_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.apple_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE ibm_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000051143/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.ibm_revenue_facts;

CREATE SCHEMASTORE VIEW ibm_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.ibm_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE oracle_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0001341439/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.oracle_revenue_facts;

CREATE SCHEMASTORE VIEW oracle_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.oracle_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE walmart_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000104169/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.walmart_revenue_facts;

CREATE SCHEMASTORE VIEW walmart_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.walmart_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE target_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000027419/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.target_revenue_facts;

CREATE SCHEMASTORE VIEW target_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.target_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE ford_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0000037996/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.ford_revenue_facts;

CREATE SCHEMASTORE VIEW ford_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.ford_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;

CREATE LIGHTNING REST TABLE tesla_revenue_facts FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/api/xbrl/companyconcept/CIK0001318605/us-gaap/Revenues.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.tesla_revenue_facts;

CREATE SCHEMASTORE VIEW tesla_revenue_table WITH CONTAINER edgar AS
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
FROM sec_data.tesla_revenue_facts
LATERAL VIEW explode(units.USD) AS fact;
