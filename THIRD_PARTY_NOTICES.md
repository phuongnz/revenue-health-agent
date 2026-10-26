# Third-party code and licences

Code in this repository that was adapted from other projects, and under which licence.
Everything else is original work under the MIT licence in `LICENSE`.

## Zetaris `zetaris-hackathon` (Apache License 2.0)

Source: https://github.com/zetaris (the hackathon "getting-started" repository shared in
the GenAI.Works Resource Center, October 2026).

- `scripts/register_sources.py` adapts `scripts/zetaris_sql.py` and `scripts/run_sql.py`:
  the SQL statement splitter, the user-agent placeholder handling and the REST channel.
  Changes: configuration from the environment only, result headers kept, a single
  wait-and-retry on HTTP 429, pacing between statements, no JDBC channel.
- `scripts/zetaris/*.sql` adapts the registration recipes under `open_data/rest_apis/sql/`
  and `open_data/usl/sql/`: a `CACHE TABLE` per raw table was added, a second XBRL
  concept was registered the same way, and the recipes were reordered.

The Apache License 2.0 text is at https://www.apache.org/licenses/LICENSE-2.0.
