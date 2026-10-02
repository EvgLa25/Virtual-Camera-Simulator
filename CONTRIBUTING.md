# Contributing to VCamSim

Help is welcome from users, testers and developers. You do not need to write code to contribute.

## Useful ways to help

- Try your own video with a VMS or RTSP client and submit a **compatibility report**, even when it works.
- Report a reproducible bug, improve setup instructions, or explain a confusing part of the UI.
- Propose a feature with the testing problem it would solve.
- Send a focused pull request for a fix, documentation improvement or test.

Use the [issue forms](https://github.com/EvgLa25/Virtual-Camera-Simulator/issues/new/choose) and search existing issues first. Reports are observations for a particular client version and setup, not certification or a promise of compatibility.

## Development setup

Use Python 3.11+ in a virtual environment and install `requirements-dev.txt`. Install FFmpeg and ffprobe separately for media and integration work.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check vcamsim tests tools
```

For protocol or engine changes, also run the relevant integration suites listed in [README.md](README.md#development). Run the GUI suite separately. Add focused regression coverage for observable bugs; documentation-only changes do not need new tests.

## Pull requests

1. Fork the repository and create a branch for one coherent change.
2. Explain the problem and the resulting behavior. Link an existing issue if relevant.
3. Run checks appropriate to the change and list the results in your pull request.
4. For UI changes, include a screenshot using synthetic data and describe the interaction you tested.

Open an issue before substantial new features so the scope and approach can be discussed. Small fixes and documentation improvements can go straight to a pull request.

## Keep examples safe to publish

Use synthetic test video and remove credentials, tokens, private IP inventories, local configuration and real surveillance footage from reports, commits and screenshots. Do not upload your entire configuration or logs without reviewing them.

Keep third-party license notices when adapting code or artwork. Original project code uses the [MIT license](LICENSE); dependency terms remain applicable. Security concerns should follow [SECURITY.md](SECURITY.md).
