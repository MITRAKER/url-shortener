# Security policy

## Supported versions

Only the latest release gets security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, report
it privately through GitHub: go to the **Security** tab of this repository
and click **Report a vulnerability**.

Include what you found, how to reproduce it, and the impact you expect. You
will get an answer as soon as possible, and credit in the release notes if you
would like it.

## How this project stays secure

- Every pull request is scanned by CodeQL and by Ruff's security rules (Bandit).
- Every dependency, indirect ones included, is locked to an exact version and
  installed only if its hash matches (`requirements*.txt`). The Docker base
  image is pinned by digest. Dependencies are checked daily with `pip-audit`
  and kept up to date by Dependabot.
- GitHub Actions are pinned to exact commit SHAs, and workflows get the least
  permissions they need.
- The Docker image runs as a non-root user. Each release ships an SBOM and a
  signed build provenance attestation.
