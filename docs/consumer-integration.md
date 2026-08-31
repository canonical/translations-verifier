# Using the verifier from another repository

This repository provides two integration surfaces:

- a composite GitHub Action for pull-request automation; and
- the `translations-verifier` CLI for local development or another CI system.

The consuming project owns its translation layout, deterministic policy, AI provider
configuration, credentials, workflow permissions, and merge rules. The verifier repository
owns the executable Action code.

## Prerequisites

Before another repository can use the Action:

1. Publish this verifier repository with [action.yml](../action.yml) at its root.
2. Make it public, or enable private Action access for the consuming repositories under the
   owning organization's **Settings > Actions > General** policy.
3. Select a reviewed verifier commit and record its full 40-character SHA.
4. Ensure the consumer uses ARB translation files with one source-locale file per configured
   translation set.

A GitHub Marketplace listing and a PyPI release are not required. GitHub downloads composite
Actions directly from the repository named by `uses:`.

## Consumer repository layout

Add the following files to the consumer repository's default branch:

```text
.github/
  translations-verifier/
    project.yaml
    ai.yaml
  workflows/
    translation-verifier.yml
```

The locations are conventional rather than hard-coded. If different paths are used, pass
those paths through the Action's `project-config` and `ai-config` inputs.

These files are trusted policy. Keep them on the base branch and review changes to them like
workflow changes. The workflow must never load policy, dependencies, or executable code from
the pull-request head.

## 1. Configure the project

Create `.github/translations-verifier/project.yaml`. Start from
[examples/project.yaml](../examples/project.yaml), then set the consumer repository and ARB
layout:

```yaml
repository: canonical/example-project
main_branch: main

translation_sets:
  - name: application
    directory: packages/application/lib/l10n
    filename_pattern: application_{locale}.arb
    source_locale: en
    context_locales: [de, es, fr]
    max_context_chars: 4000

policy:
  variable_patterns:
    - '\$\{[A-Za-z_][A-Za-z0-9_]*\}'
  immutable_tokens: [Ubuntu]
  preserve_urls: true
  preserve_command_flags: true
  preserve_whitespace: true
  allow_bidi_isolates: false
  markup:
    mode: none

limits:
  max_changed_files: 100
  max_changed_keys: 1000
  max_file_bytes: 2000000
  max_total_bytes: 20000000
```

Important consumer-specific values are:

- `repository`: must exactly identify the repository in which the workflow runs;
- `main_branch`: must match the branch translation PRs target;
- `directory`: repository-relative location of one ARB family; and
- `filename_pattern`: filename containing exactly one `{locale}` placeholder.

Add one `translation_sets` entry per independent ARB directory or filename family. The
source file is constructed from `directory`, `filename_pattern`, and `source_locale`. For the
example above, it is `packages/application/lib/l10n/application_en.arb`.

See the [configuration reference](configuration.md) for every field and its exact behavior.

## 2. Configure optional AI review

Create `.github/translations-verifier/ai.yaml` only if the consumer wants semantic AI review:

```yaml
endpoint: https://openrouter.ai/api/v1
model: provider/model-name
api_key_env: TRANSLATION_VERIFIER_API_KEY
timeout_seconds: 60
max_retries: 1
temperature: 0
max_output_tokens: 4000
max_input_tokens_per_run: 200000
max_input_chars_per_key: 12000
allow_insecure_http: false
allow_json_object_fallback: true
```

`api_key_env` is the environment-variable name the verifier reads. It is not a secret value.
Never commit an API key to this YAML file.

For deterministic checks only, omit `ai.yaml`, do not configure an AI secret, and set the
Action's `deterministic-only` input to `"true"`.

## 3. Add the GitHub workflow

Copy [examples/translation-verifier.yml](../examples/translation-verifier.yml) to
`.github/workflows/translation-verifier.yml` in the consumer repository.

Replace both occurrences of:

```yaml
uses: canonical/translations-verifier@FULL_40_CHARACTER_COMMIT_SHA
```

with the published Action repository and reviewed full commit SHA:

```yaml
uses: canonical/translations-verifier@0123456789abcdef0123456789abcdef01234567
```

Do not use a branch such as `@main` in production. A mutable tag such as `@v1` is convenient
but provides weaker supply-chain protection than an immutable commit SHA.

The workflow must grant these read-only permissions:

```yaml
permissions:
  contents: read
  pull-requests: read
```

Pass the job token explicitly as `GITHUB_TOKEN` because the verifier calls the GitHub API:

```yaml
env:
  GITHUB_TOKEN: ${{ github.token }}
```

This is required for private repositories and recommended for public repositories to avoid
anonymous API rate limits. The verifier uses it only to read PR metadata and files at captured
immutable SHAs.

## 4. Configure AI approval and credentials

For AI review, create a consumer-repository environment named
`translation-verification` under **Settings > Environments**.

Configure:

- trusted maintainers as required reviewers;
- an environment secret named `TRANSLATION_VERIFIER_API_KEY`; and
- deployment branch restrictions appropriate to the default branch.

The workflow maps that secret to the environment-variable name declared by `api_key_env`:

```yaml
env:
  GITHUB_TOKEN: ${{ github.token }}
  TRANSLATION_VERIFIER_API_KEY: ${{ secrets.TRANSLATION_VERIFIER_API_KEY }}
```

Use a dedicated provider key or project with provider-side request and spending limits. The
verifier's token limits bound one run; they cannot enforce an aggregate monthly budget across
independent GitHub runners.

The deterministic job has no provider credential. The AI job runs only after deterministic
checks succeed and a maintainer approves access to the protected environment.

## Action interface

### Inputs

| Input | Required | Default | Consumer usage |
|---|---:|---|---|
| `project-config` | Yes | None | Workspace-relative path to trusted project YAML. |
| `ai-config` | No | Empty | Workspace-relative path to trusted AI YAML. Omit for deterministic-only runs. |
| `pr` | Yes | None | PR number or full GitHub PR URL; it must match `repository` in project config. |
| `output` | No | `translation-verification.json` | Workspace-relative JSON report path. |
| `deterministic-only` | No | `"false"` | Set to `"true"` to make a clean run complete without AI. |

### Environment variables

| Variable | Required | Purpose |
|---|---:|---|
| `GITHUB_TOKEN` | Private repos: yes; public repos: recommended | Read PR metadata and immutable file contents. |
| Variable named by `api_key_env` | AI runs: yes | Bearer token for the configured AI endpoint. |
| `GITHUB_API_URL` | No | Overrides the GitHub API base URL, primarily for GitHub Enterprise Server. |

### Outputs and artifacts

| Output | Meaning |
|---|---|
| `status` | `acceptable`, `unacceptable`, or `incomplete`. |
| `report` | The path supplied through the `output` input. |

The Action also writes a Markdown summary to `$GITHUB_STEP_SUMMARY` and emits GitHub workflow
annotations. It does not upload the JSON report itself; the consumer workflow should use
`actions/upload-artifact` with `if: always()` as shown in the example.

The Action exits nonzero for `unacceptable` and `incomplete`, so subsequent diagnostic or
artifact steps must use `if: always()` when they should run after a failed verification step.
The step output is written before the Action exits and can be inspected by later steps that
are configured to run.

## Minimal deterministic workflow

A consumer that does not want AI can use a single job:

```yaml
name: Translation verification

on:
  pull_request_target:
    types: [opened, synchronize, reopened]
    paths: ["**/*.arb"]

jobs:
  verify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          ref: ${{ github.event.pull_request.base.sha }}
          persist-credentials: false
      - id: verifier
        uses: canonical/translations-verifier@FULL_40_CHARACTER_COMMIT_SHA
        env:
          GITHUB_TOKEN: ${{ github.token }}
        with:
          project-config: .github/translations-verifier/project.yaml
          pr: ${{ github.event.pull_request.number }}
          deterministic-only: "true"
```

The checkout is the trusted base revision. Do not replace it with the PR head when using
`pull_request_target`.

## Local use in another project

Developers can validate consumer configuration and PR behavior before committing a workflow.
From a checkout of this verifier repository:

```bash
GITHUB_TOKEN=... uv run translations-verifier config validate \
  --project-config /path/to/project/.github/translations-verifier/project.yaml \
  --ai-config /path/to/project/.github/translations-verifier/ai.yaml

GITHUB_TOKEN=... TRANSLATION_VERIFIER_API_KEY=... \
uv run translations-verifier verify \
  --project-config /path/to/project/.github/translations-verifier/project.yaml \
  --ai-config /path/to/project/.github/translations-verifier/ai.yaml \
  --pr https://github.com/canonical/example-project/pull/123 \
  --output translation-verification.json
```

The CLI reads PR content from GitHub by immutable SHA; it does not need to run from inside the
consumer repository. Use `--deterministic-only` to test without spending model credits.

## Verification statuses

- `acceptable` / exit `0`: every required enabled check completed without an error finding.
- `unacceptable` / exit `1`: at least one deterministic or AI error finding exists.
- `incomplete` / exit `2`: required work could not complete because of limits, API failure,
  missing AI configuration, or invalid model output.

The verifier does not approve, reject, merge, comment on, or modify the PR. Repositories may
optionally make the resulting job a required status check through a ruleset after calibrating
it on representative translation PRs.

## Updating consumers

To deploy a verifier update:

1. run CI in the verifier repository;
2. review the new commit;
3. replace the pinned SHA in both consumer workflow jobs; and
4. run the workflow manually against an existing translation PR before merging the update.

Project and AI config changes are independent of verifier releases as long as they remain
valid under the pinned version's schemas.

## Common integration failures

- **`404` from GitHub for a private consumer:** pass `${{ github.token }}` as `GITHUB_TOKEN`
  and verify the action repository is shared with the consumer.
- **`policy.unmatched_arb`:** correct `directory`, `filename_pattern`, or locale filters.
- **`policy.unexpected_base`:** align `main_branch` with the PR's target branch.
- **AI review is incomplete:** verify the protected environment was approved, the secret name
  matches `api_key_env`, and provider/token limits were not exceeded.
- **Workflow does not trigger:** ensure the workflow exists on the default branch and the PR
  changes a path matching `**/*.arb`.
