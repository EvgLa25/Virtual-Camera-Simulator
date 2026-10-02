# Contributing

Use Python 3.11+ and install `requirements-dev.txt` in a virtual environment.
Run `python -m pytest -q` and `python -m ruff check vcamsim tests tools`.
For protocol or engine changes, also run the integration suites listed in
README.md. Include focused regression tests for observable bugs.

Use synthetic test video and keep credentials, private IP inventories and
local configuration out of commits and screenshots. Keep dependency notices
when adapting code or artwork. Describe the behavior changed and the checks
you ran in each pull request.
