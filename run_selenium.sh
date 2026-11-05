#!/bin/sh
# Build the current workspace and start its guided Chrome walkthrough.
set -eu
cd "$(dirname "$0")"

if ! .venv/bin/python -c 'import selenium, uvicorn' 2>/dev/null; then
    printf '%s\n' 'Install the walkthrough dependencies first:' \
        '  python3 -m venv .venv' \
        '  .venv/bin/python -m pip install -r requirements-selenium.txt' >&2
    exit 1
fi

npm --prefix artos_document_agent_py/web run build
exec .venv/bin/python -u -m artos_document_agent_py.selenium_walkthrough_py "$@"
