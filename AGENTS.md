# AGENTS.md

## Conventions

- Everything under `skills/` must stay portable: no references to this
  repository, its dev tooling, the author's accounts, or local paths. Skill docs
  speak in skill-relative paths (`scripts/<name>.py`) and generic accounts
  (`-a google`, `me@example.com`). The repo-level names, tooling, and dev
  commands belong in this file and in `README.md`.
- Run Python with `uv run`, never bare `python`.
- The entry scripts carry PEP 723 dependency blocks so the skill runs standalone;
  keep them in sync with `[project].dependencies` in `pyproject.toml`.
- After editing any `.py` file, run `uv run pyright` and `uvx ruff check skills`.
- Write code, comments, and docs in English. All Python must have type annotations.
