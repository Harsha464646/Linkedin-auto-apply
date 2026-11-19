# Verification

Run commands from this directory; configuration is loaded using relative paths.

- Windows offline regression suite: `.venv\Scripts\python.exe -X utf8 -B -m unittest discover -p "test_*.py" -v`
- Tests use the existing Playwright Chromium installation with local HTML fixtures and intercepted requests. They must not submit real applications or use the persistent `user_data` profile.
- Use the repository virtual environment; the global Python installation may not include Playwright.
- Check patch whitespace with `git diff --check`.
- `main.py` is a live, continuously running application bot, not a test command. Do not use it for verification without explicit authorization to submit applications.
- `config.yaml`, `.env`, application history, learned answers, and browser profile data are local user data. Do not commit or print credentials or session contents.
- Start the read-only local dashboard with `.venv\Scripts\python.exe -X utf8 monitor.py`; it binds only to `127.0.0.1:8765` and does not start application automation.
- Live status is written to ignored `bot_status.json` only after `main.py` starts its reporter. Tests must use temporary directories and mocked application actions, not the production status or CSV files.
