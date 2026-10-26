"""Pure parts of scripts/register_sources.py: splitting, the user agent, result parsing.

Nothing here touches the network or Zetaris; the registration scripts themselves run
once by hand (see the module docstring).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import register_sources as rs  # noqa: E402


def test_splits_on_semicolons_outside_quotes_and_drops_comments():
    sql = """
    -- a line comment; with a semicolon
    CREATE LIGHTNING DATABASE X DESCRIBE BY "desc; not a split";
    /* block; comment */
    SELECT 'a;b' AS v, `c;d` AS w;
    """
    assert rs.split_statements(sql) == [
        'CREATE LIGHTNING DATABASE X DESCRIBE BY "desc; not a split"',
        "SELECT 'a;b' AS v, `c;d` AS w",
    ]


def test_merges_create_table_into_compile_usl():
    sql = """
    COMPILE USL IF NOT EXISTS u DEPLOY NAMESPACE n DDL
    CREATE TABLE a (id int PRIMARY KEY);
    CREATE TABLE b (id int, a_id int FOREIGN KEY REFERENCES a(id));
    ACTIVATE USL TABLE n.u.a AS SELECT 1;
    """
    stmts = rs.split_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("COMPILE USL")
    assert stmts[0].count("CREATE TABLE") == 2
    assert stmts[1].startswith("ACTIVATE USL")


def test_trailing_semicolon_and_blank_statements_are_ignored():
    assert rs.split_statements("SELECT 1;;\n;  ") == ["SELECT 1"]


def test_backslash_escaped_quote_does_not_end_the_string():
    assert rs.split_statements("SELECT 'a\\'b;c' AS v; SELECT 2") == [
        "SELECT 'a\\'b;c' AS v",
        "SELECT 2",
    ]


def test_non_json_reply_is_a_zetaris_error(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>login</html>"

    monkeypatch.setattr(rs, "urlopen", lambda req, timeout: Resp())
    ch = rs.RestChannel(api_key="k", base_url="http://x", org_id="1")
    with pytest.raises(rs.ZetarisError, match="Not a JSON reply"):
        ch.run("SELECT 1")


def test_missing_file_is_reported_not_raised(tmp_path, capsys):
    assert rs.main([str(tmp_path / "nope.sql"), "--dry-run"]) == 2
    assert "Cannot read" in capsys.readouterr().err


def test_files_and_execute_together_are_refused(tmp_path):
    f = tmp_path / "x.sql"
    f.write_text("SELECT 1;")
    with pytest.raises(SystemExit):
        rs.main([str(f), "-e", "SELECT 2", "--dry-run"])


@pytest.mark.parametrize(
    "ua,ok",
    [
        ("my-app me@example.com", True),
        ("my app with spaces me@example.com", True),
        ("my-app", False),
        ("me@example.com", False),
        ("my-app not-an-email", False),
    ],
)
def test_user_agent_must_name_an_app_and_a_contact(ua, ok):
    assert rs.valid_user_agent(ua) is ok


def test_placeholder_is_replaced_only_with_a_valid_agent():
    stmts = [f'HEADER ( user-agent "{rs.PLACEHOLDER_UA}" )', "SELECT 1"]
    out = rs.apply_user_agent(stmts, "my-app me@example.com")
    assert out[0] == 'HEADER ( user-agent "my-app me@example.com" )'
    assert out[1] == "SELECT 1"
    with pytest.raises(rs.ZetarisError):
        rs.apply_user_agent(stmts, None)
    with pytest.raises(rs.ZetarisError):
        rs.apply_user_agent(stmts, 'bad "quoted" me@example.com')
    with pytest.raises(rs.ZetarisError):
        rs.apply_user_agent(stmts, "no-contact")


def test_statements_without_placeholder_need_no_agent():
    assert rs.apply_user_agent(["SELECT 1"], None) == ["SELECT 1"]


def test_result_keeps_headers_rows_total_and_timing():
    r = rs.Result.from_response({"headers": ["a"], "data": [["1"]], "total": 1, "timeUsed": 7})
    assert (r.headers, r.rows, r.total, r.time_used_ms) == (["a"], [["1"]], 1, 7)
    assert rs.Result.from_response(None).rows == []


def test_rest_channel_requires_a_key(monkeypatch):
    monkeypatch.delenv("ZETARIS_API_KEY", raising=False)
    with pytest.raises(rs.ZetarisError):
        rs.RestChannel()


def test_dry_run_prints_statements_and_sends_nothing(tmp_path, capsys, monkeypatch):
    f = tmp_path / "x.sql"
    f.write_text("SELECT 1;\nSELECT 2;")
    monkeypatch.delenv("ZETARIS_API_KEY", raising=False)
    assert rs.main([str(f), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[2/2] x.sql: SELECT 2" in out
    assert "nothing sent" in out
