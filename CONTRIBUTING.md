# Contributing

Thanks for your interest. Bug reports and feature requests are welcome via [GitHub Issues](../../issues).

## Local setup

```bash
git clone https://github.com/YOUR_USERNAME/webaudit.git
cd webaudit
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
python3 webaudit.py
```

## Pull requests

- Open an issue first for non-trivial changes so we can agree on direction
- Keep PRs focused — one thing per PR
- `ruff check webaudit.py` should pass before opening a PR

## Code style

- `ruff` for linting and formatting (`ruff check`, `ruff format`)
- Prefer clarity over cleverness — this codebase runs as a single file and the constraint is intentional

## Reporting bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include your OS, Python version, and any relevant log output.
