-- Unified Semantic Layer sic_edgar_usl: company (PK cik, FK sic) and sic_code, with the DQ run
-- Adapted from Zetaris's zetaris-hackathon repository, open_data/usl/sql/02_sic_edgar_usl_create.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). Comments removed, statements unchanged.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/50_sic_edgar_usl.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE NAMESPACE IF NOT EXISTS lightning.metastore;

CREATE NAMESPACE IF NOT EXISTS lightning.metastore.usl_demo;

COMPILE USL IF NOT EXISTS sic_edgar_usl DEPLOY NAMESPACE lightning.metastore.usl_demo DDL
CREATE TABLE sic_code (
    sic_code varchar(4) NOT NULL PRIMARY KEY,
    description varchar(200),
    division varchar(1),
    division_desc varchar(200),
    major_group varchar(2),
    major_group_desc varchar(200),
    industry_group varchar(3),
    industry_group_desc varchar(200)
);
CREATE TABLE company (
    cik varchar(10) NOT NULL PRIMARY KEY,
    entity_name varchar(200) NOT NULL,
    sic varchar(4) NOT NULL FOREIGN KEY REFERENCES sic_code(sic_code),
    sic_description_edgar varchar(200)
);

UPDATE USL lightning.metastore.usl_demo.sic_edgar_usl SET DESCRIPTION 'USL rebuild of the EDGAR plus SIC company profile data product - 7 companies, FK-related to a SIC hierarchy table. Contrast target for edgar.all_companies_profile_table and its manual Virtual Data Mart (open_data/rest_apis/sql/rate_limited/11_edgar_company_profiles_create.sql).';

UPDATE USL lightning.metastore.usl_demo.sic_edgar_usl.company SET TABLE DESCRIPTION 'One row per company - CIK, name, and SIC code as reported by EDGAR''s own submissions endpoint.';

UPDATE USL lightning.metastore.usl_demo.sic_edgar_usl.sic_code SET TABLE DESCRIPTION 'SIC hierarchy reference - same content as sic_usl.sic_code, duplicated here per caveat 1 above pending confirmation of cross-USL foreign keys.';

ACTIVATE USL TABLE lightning.metastore.usl_demo.sic_edgar_usl.sic_code AS
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

ACTIVATE USL TABLE lightning.metastore.usl_demo.sic_edgar_usl.company AS
SELECT
    COALESCE(cik, '') AS cik,
    COALESCE(entity_name, '') AS entity_name,
    COALESCE(sic, '') AS sic,
    sic_description_edgar
FROM (
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.apple_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.ibm_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.oracle_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.walmart_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.target_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.ford_submissions_raw
    UNION ALL
    SELECT cik, name AS entity_name, sic, `sicDescription` AS sic_description_edgar FROM sec_data.tesla_submissions_raw
) AS companies;

RUN DQ TABLE lightning.metastore.usl_demo.sic_edgar_usl.company;
