# Catalogue statistics automation

The organisation profile does not count GitHub repositories directly. The `.github` repository, catalogue hubs, templates and documentation repositories are not labs and must not inflate the public metrics.

The source of truth is the versioned `lab.yaml` manifest stored at the root of each public lab repository.

## Files

```text
.github/
├── .github/workflows/update-catalog-stats.yml
├── docs/catalog-automation.md
├── examples/lab.yaml
├── generated/catalog-stats.json
├── profile/README.md
├── scripts/update_catalog_stats.py
└── tests/test_update_catalog_stats.py
```

## What the workflow does

1. Runs every day and can also be started manually.
2. Lists the public repositories owned by `Innoquo`.
3. Ignores archived repositories, forks and repositories without `lab.yaml`.
4. Validates every public lab manifest against schema version 1.
5. Fails on duplicated IDs or priorities and on evidence inconsistencies.
6. Counts every valid public manifest as a published lab.
7. Counts L3, L4 and L5 manifests as featured only when their evidence flags are coherent.
8. Updates the two metrics in `profile/README.md`.
9. Writes the complete machine-readable catalogue to `generated/catalog-stats.json`.
10. Opens or updates a pull request for human review. It never merges automatically.

The job only changes generated metrics when the underlying manifests change. It does not write a timestamp, so a daily run with identical evidence produces no commit or pull request.

## Organisation setting required once

Open:

```text
Innoquo → Settings → Actions → General → Workflow permissions
```

Enable:

- Read and write permissions for `GITHUB_TOKEN`.
- Allow GitHub Actions to create and approve pull requests.

The workflow creates a pull request but does not approve or merge it. No personal access token is required.

## Install

Copy the complete directory contents into the root of `Innoquo/.github`, preserving the paths. Commit the files to `main`.

Then open:

```text
Innoquo/.github → Actions → Update catalogue statistics → Run workflow
```

The first run creates `automation/catalogue-statistics` and opens a pull request if generated data differs from `main`.

## Manifest contract

Every lab repository must contain a root-level `lab.yaml`:

```yaml
schema_version: 1
id: 061
priority: 1
slug: bedrock-fastapi-runtime
track: aws-bedrock-agentcore
status: L3
visibility: public
languages: [en, fr]
reusable_asset: bedrock-fastapi-service-template
failure_tested: true
cloud_verified: false
last_verified: 2026-08-08
```

Schema version 1 deliberately uses flat top-level scalar fields. Lists may use inline YAML syntax. This allows the validator to avoid runtime dependencies while keeping the manifest readable.

## Evidence validation

- `id` contains exactly three digits.
- `priority` is a positive integer and unique across public labs.
- `slug` must equal the GitHub repository name.
- `track` uses lowercase kebab-case.
- `status` is one of `L0` through `L5`.
- A public repository with a manifest must declare `visibility: public`.
- L3+ requires `failure_tested: true` and an ISO `last_verified` date.
- L4+ additionally requires `cloud_verified: true`.
- Dates in the future are rejected.

A repository without `lab.yaml` is treated as an auxiliary repository and is not counted. A repository with an invalid `lab.yaml` fails the workflow rather than publishing a misleading number.

## Security model

- The workflow is triggered only by schedule or an authorised manual action.
- It does not run untrusted code from lab repositories.
- It reads public metadata and the text of each `lab.yaml` through the GitHub API.
- It uses the repository-scoped `GITHUB_TOKEN`; no PAT is stored.
- It requests only `contents: write` and `pull-requests: write`.
- It uses official GitHub actions for checkout and Python setup.
- It produces a reviewable pull request and never auto-merges.

## Local verification

```bash
python -m unittest discover -s tests -v

GITHUB_TOKEN=github_token_here \
python scripts/update_catalog_stats.py --org Innoquo --dry-run
```

Do not commit a local token. Prefer the automatic `GITHUB_TOKEN` inside GitHub Actions.
