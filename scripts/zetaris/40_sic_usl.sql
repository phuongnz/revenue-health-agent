-- Unified Semantic Layer sic_usl over the SIC reference (creates the namespace)
-- Adapted from Zetaris's zetaris-hackathon repository, open_data/usl/sql/01_sic_usl_create.sql (Apache-2.0; see
-- THIRD_PARTY_NOTICES.md). Comments removed, statements unchanged.
-- One statement per REST call. Run once, by hand, through ./run.sh:
--   ./run.sh uv run python scripts/register_sources.py scripts/zetaris/40_sic_usl.sql --skip-exists
-- The user-agent placeholder is replaced from ZETARIS_USER_AGENT at run time.

CREATE NAMESPACE IF NOT EXISTS lightning.metastore;

CREATE NAMESPACE IF NOT EXISTS lightning.metastore.usl_demo;

COMPILE USL IF NOT EXISTS sic_usl DEPLOY NAMESPACE lightning.metastore.usl_demo DDL
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

UPDATE USL lightning.metastore.usl_demo.sic_usl SET DESCRIPTION 'Baseline USL model - SIC hierarchy reference, single table, no relationships. Contrast target for company_dns.sic_codes_table (open_data/rest_apis/sql/rate_limited/12_company_dns_create.sql).';

ACTIVATE USL TABLE lightning.metastore.usl_demo.sic_usl.sic_code AS
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

RUN DQ TABLE lightning.metastore.usl_demo.sic_usl.sic_code;
