-- company_dns US SIC reference (1,005 codes), V3 and V4, cached
-- Adapted from Zetaris's zetaris-hackathon repository, open_data/rest_apis/sql/rate_limited/12_company_dns_create.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). Comments removed, statements unchanged.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/10_company_dns_sic.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE LIGHTNING DATABASE COMPANY_DNS DESCRIBE BY "company_dns SIC reference data - V3 and V4 side by side - section or division, group, class";

CREATE LIGHTNING REST TABLE sic_codes_raw FROM COMPANY_DNS REQUEST(
    endpoint "https://company-dns.mediumroast.io/V3.0/na/sic/code/%25",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE company_dns.sic_codes_raw;

CREATE LIGHTNING REST TABLE sic_codes_v4_raw FROM COMPANY_DNS REQUEST(
    endpoint "https://company-dns.mediumroast.io/V4.0/na/sic/code/%25",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "YOUR_APP_NAME YOUR_CONTACT_EMAIL"
) BODY ();

CACHE TABLE company_dns.sic_codes_v4_raw;

CREATE SCHEMASTORE CONTAINER company_dns;

CREATE SCHEMASTORE VIEW sic_codes_table WITH CONTAINER company_dns AS
SELECT
    sic_code,
    sic_val.description AS description,
    sic_val.division AS division,
    sic_val.division_desc AS division_desc,
    sic_val.major_group AS major_group,
    sic_val.major_group_desc AS major_group_desc,
    sic_val.industry_group AS industry_group,
    sic_val.industry_group_desc AS industry_group_desc
FROM company_dns.sic_codes_raw
LATERAL VIEW explode(
    from_json(to_json(data.sics), 'map<string, struct<description:string, division:string, division_desc:string, major_group:string, major_group_desc:string, industry_group:string, industry_group_desc:string>>')
) AS sic_code, sic_val;

CREATE SCHEMASTORE VIEW sic_codes_v4_table WITH CONTAINER company_dns AS
SELECT
    item.section_id AS section_id,
    item.section_desc AS section_desc,
    item.division_id AS division_id,
    item.division_desc AS division_desc,
    item.group_id AS group_id,
    item.group_desc AS group_desc,
    item.class_id AS class_id,
    item.class_desc AS class_desc
FROM company_dns.sic_codes_v4_raw
LATERAL VIEW explode(data) AS item;

CREATE SCHEMASTORE VIEW sic_codes_v4_compat_table WITH CONTAINER company_dns AS
SELECT
    item.class_id AS sic_code,
    item.class_desc AS description,
    item.section_id AS division,
    item.section_desc AS division_desc,
    item.division_id AS major_group,
    item.division_desc AS major_group_desc,
    item.group_id AS industry_group,
    item.group_desc AS industry_group_desc
FROM company_dns.sic_codes_v4_raw
LATERAL VIEW explode(data) AS item;

CREATE SCHEMASTORE VIEW sic_codes_v3_compat_table WITH CONTAINER company_dns AS
SELECT
    sic_val.division AS section_id,
    sic_val.division_desc AS section_desc,
    sic_val.major_group AS division_id,
    sic_val.major_group_desc AS division_desc,
    sic_val.industry_group AS group_id,
    sic_val.industry_group_desc AS group_desc,
    sic_code AS class_id,
    sic_val.description AS class_desc
FROM company_dns.sic_codes_raw
LATERAL VIEW explode(
    from_json(to_json(data.sics), 'map<string, struct<description:string, division:string, division_desc:string, major_group:string, major_group_desc:string, industry_group:string, industry_group_desc:string>>')
) AS sic_code, sic_val;
