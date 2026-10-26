-- SEC EDGAR submissions (name, SIC) per company, joined to the SIC reference
-- Adapted from Zetaris's zetaris-hackathon repository, open_data/rest_apis/sql/rate_limited/11_edgar_company_profiles_create.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). INNER JOIN became LEFT JOIN so a company whose SIC code is absent from the reference (IBM, 3570) still appears; CACHE TABLE added after each REST table.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/30_edgar_submissions.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE LIGHTNING REST TABLE apple_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0000320193.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.apple_submissions_raw;

CREATE SCHEMASTORE VIEW apple_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.apple_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE ibm_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0000051143.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.ibm_submissions_raw;

CREATE SCHEMASTORE VIEW ibm_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.ibm_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE oracle_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0001341439.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.oracle_submissions_raw;

CREATE SCHEMASTORE VIEW oracle_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.oracle_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE walmart_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0000104169.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.walmart_submissions_raw;

CREATE SCHEMASTORE VIEW walmart_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.walmart_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE target_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0000027419.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.target_submissions_raw;

CREATE SCHEMASTORE VIEW target_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.target_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE ford_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0000037996.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.ford_submissions_raw;

CREATE SCHEMASTORE VIEW ford_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.ford_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE LIGHTNING REST TABLE tesla_submissions_raw FROM SEC_DATA REQUEST(
    endpoint "https://data.sec.gov/submissions/CIK0001318605.json",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE sec_data.tesla_submissions_raw;

CREATE SCHEMASTORE VIEW tesla_profile_table WITH CONTAINER edgar AS
SELECT
    s.cik AS cik,
    s.name AS entity_name,
    s.sic AS sic_code,
    s.`sicDescription` AS sic_description_edgar,
    r.description AS sic_description_reference,
    r.division AS division,
    r.division_desc AS division_desc,
    r.major_group AS major_group,
    r.major_group_desc AS major_group_desc,
    r.industry_group AS industry_group,
    r.industry_group_desc AS industry_group_desc
FROM sec_data.tesla_submissions_raw s
LEFT JOIN company_dns.sic_codes_table r
    ON s.sic = r.sic_code;

CREATE SCHEMASTORE VIEW all_companies_profile_table WITH CONTAINER edgar AS
SELECT * FROM edgar.apple_profile_table
UNION ALL
SELECT * FROM edgar.ibm_profile_table
UNION ALL
SELECT * FROM edgar.oracle_profile_table
UNION ALL
SELECT * FROM edgar.walmart_profile_table
UNION ALL
SELECT * FROM edgar.target_profile_table
UNION ALL
SELECT * FROM edgar.ford_profile_table
UNION ALL
SELECT * FROM edgar.tesla_profile_table;
