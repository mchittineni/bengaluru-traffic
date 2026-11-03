# Contributing

Issues and pull requests are welcome. For anything larger than a small fix, open an issue first
so we can agree on the approach.

## Set up

```bash
git clone https://github.com/mchittineni/bengaluru-traffic.git
cd bengaluru-traffic
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

No API key is needed for development: `demo_data.py` creates synthetic travel times, and the
tests stub every network and AWS call.

## Before opening a pull request

```bash
.venv/bin/ruff check .
.venv/bin/pytest --cov                     # must stay at or above 80% coverage
node --test tests/viewer/*.test.js         # if you changed viewer/
terraform -chdir=aws fmt -check            # if you changed aws/
```

CI runs the same checks on every pull request, plus the 3D simulation and a browser smoke test
when the simulation or viewer changes.

## Conventions

- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(sim3d): ...`, `fix(forecast): ...`, `docs: ...`, `test: ...`, `ci: ...`.
- Keep pull requests focused, and add or update tests for behaviour you change.
- Results from synthetic data are illustrative. Say so when a change affects reported numbers.

## Licence

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
