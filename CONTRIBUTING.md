# Contributing

Thanks for your interest! This guide covers setup, the checks your change must
pass, and how releases work.

## Set up

You need Python 3.13 and, for the full stack, Docker.

```bash
git clone https://github.com/MITRAKER/url-shortener.git
cd url-shortener
python -m venv .venv && source .venv/bin/activate
make install        # dependencies + the git pre-commit hook
make up             # optional: run the app with PostgreSQL in Docker
```

`make help` lists every command.

## Make a change

1. Create a branch from `main`, for example `feat/custom-domains`.
2. Write the code **and its tests**. Follow [the coding standard](docs/CODING_STANDARD.md),
   including the NASA/JPL Power of Ten rules.
3. Run `make check`. It runs what CI runs, except the Docker job and the
   PostgreSQL test run: formatting, lint, strict types, the Power of Ten
   checker, workflow and Dockerfile linters, the tests with the coverage gate,
   and the dependency audit. For the PostgreSQL run, set `TEST_DATABASE_URL`
   (see the README) and run `make test`.
4. Add a line to `CHANGELOG.md` under **Unreleased**.
5. Commit using [Conventional Commits](https://www.conventionalcommits.org/):
   `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `ci: ...`, `chore: ...`.
6. Open a pull request and fill in the template. All CI checks must be green
   before merging.

The pre-commit hook runs the fast checks on every commit, so most problems
are caught before they ever reach CI.

## Dependencies

`requirements.in` lists what the app imports, `requirements-dev.in` the
development tools. The `.txt` files are lock files generated from them: every
package, indirect ones included, at an exact version with its hashes. Never
edit them by hand. To add or change a dependency, edit the `.in` file, run
`make lock`, and commit both files. To update one indirect package, run
`pip-compile --upgrade-package <name>` with the options listed at the top of
the `.txt` file.

The pre-commit hooks follow the same rule: each one is a `repo: local` hook
that runs a tool pinned in `requirements-dev.in` or a script in this
repository. Add a new hook the same way,
not as a remote hook repository, which pre-commit would install without
hashes.

## What CI checks

| Job | What it does |
|-----|--------------|
| Lint, format, types, Power of Ten | All pre-commit hooks on every file |
| Tests (SQLite + fake Redis), Tests (PostgreSQL + Redis) | The test suite on both setups; fails below 95% branch coverage |
| Dependency vulnerability audit | `pip-audit` against the Python vulnerability database |
| Docker build and smoke test | Builds the image, starts the stack, runs `scripts/smoke_test.py` |
| CodeQL | Security analysis of the Python code and of the workflows |

## Releasing

Versions follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

1. In a pull request, set `__version__` in `app/__init__.py` and rename the
   **Unreleased** section of `CHANGELOG.md` to the new version and date.
2. After it is merged, tag that commit on `main` and push the tag:
   ```bash
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```
3. The release workflow reruns CI, publishes the Docker image to
   `ghcr.io/mitraker/url-shortener`, and creates a GitHub release with the
   changelog notes. It refuses to run if the tag and `__version__` differ.

## Recommended repository settings

For maintainers, under **Settings**:

- **Branches → Add rule for `main`:** require a pull request, require the CI
  and CodeQL checks to pass, and require branches to be up to date.
- **Code security:** enable Dependabot alerts, secret scanning with push
  protection, and private vulnerability reporting.
- **General → Pull Requests:** enable "Automatically delete head branches".
