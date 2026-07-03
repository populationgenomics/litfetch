# Releasing litfetch to PyPI

Publishing is automated by [`.github/workflows/release.yml`](../.github/workflows/release.yml)
via PyPI **Trusted Publishing** (OIDC): CI mints a short-lived, scoped token at
publish time, so there is no PyPI API token stored in the repo.

## Cut a release

1. Bump `version` in `pyproject.toml` and merge it to `main`.
2. Publish a GitHub Release whose tag is `v<version>` (e.g. `v0.1.1`).

The `release` workflow then checks the tag matches the `pyproject.toml`
version (a published version is irreversible), builds the sdist + wheel with
`uv build`, and publishes them. The `publish` job runs in the `pypi`
environment and holds only `id-token: write`.
