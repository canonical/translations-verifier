# WLReviser

WLReviser reviews translation changes in GitHub pull requests with an
OpenAI-compatible model. It writes a JSON report and can apply selected corrections to Weblate.

For `/translations verify` and `/translations apply` in GitHub Actions, see the
[GitHub Actions setup guide](docs/github-actions.md).

## Quick Start

Requirements: Python 3.13+, [uv](https://docs.astral.sh/uv/), access to the target GitHub and
Weblate projects, and an OpenAI-compatible Chat Completions endpoint with strict JSON Schema
support.

```console
$ uv sync
$ uv run wlreviser --help
```

Install the command separately with `uv tool install .`.

## Configuration

Start with [the Ubuntu Desktop Provision example](examples/ubuntu-desktop-provision.yaml).

```yaml
weblate:
  api_url: https://hosted.weblate.org/api/
  project: ubuntu-desktop-translations

github:
  repository: canonical/ubuntu-desktop-provision

ai:
  api_url: http://localhost:11434/v1/
  model: qwen3
  max_concurrency: 4

components:
  - weblate_component: provision-common
    source_locale: en
    context_locales: [de, fr]
    format: arb
    source_file: packages/ubuntu_provision/lib/src/l10n/ubuntu_provision_en.arb
    path: packages/ubuntu_provision/lib/src/l10n/ubuntu_provision_{locale}.arb
    push_after_commit: false
```

Endpoint URLs must use HTTPS, except for `localhost`, `127.0.0.1`, and `::1`. They cannot contain
credentials, query strings, or fragments. `max_concurrency` defaults to `4` and accepts values
from `1` through `32`.

Each component has these fields:

| Field | Meaning |
| --- | --- |
| `weblate_component` | Weblate component slug. It must be unique in the file. |
| `source_locale` | Locale used by the concrete `source_file`. |
| `context_locales` | Optional peer locales supplied to the reviewer for context. |
| `format` | One of `arb`, `po`, or `html`. |
| `source_file` | Normalized repository-relative source path with no placeholders. |
| `path` | Target path template containing exactly one `{locale}` placeholder. |
| `push_after_commit` | Whether WLReviser requests a push after committing. Defaults to `true`; set to `false` when automatic push is enabled for the component in Weblate. |

Locale values refer to filenames, so regional values such as `zh_TW` and `en_US` are preserved.
WLReviser resolves the corresponding Weblate translation by exact normalized filename. Configure
source translation files, not generated outputs such as Dart localization files.

### Supported Formats

| Format | Comparison unit | Automatic apply |
| --- | --- | --- |
| ARB | Each non-metadata string property; `@key` data is review metadata | Yes |
 | PO | Each non-obsolete `msgid` and `msgctxt` identity, including plural forms | Single-form units only |
| HTML | The complete file as one unit | No |

## Credentials

| Variable | Required by | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | `verify` | Required; needs read access to the pull request and repository contents. |
| `WEBLATE_TOKEN` | `verify`, `apply` | Required; verification reads mappings and apply writes units and operates on repositories. |
| `WL_BOT_AI_TOKEN` | `verify` | Optional; omit it for a local endpoint that does not require authentication. |


## Verify

```console
$ uv run wlreviser verify CONFIG.yaml https://github.com/OWNER/REPOSITORY/pull/NUMBER REPORT.json
```

The URL must match `github.repository`. The report includes findings, suggestions, errors, and the
pull request coordinates used for the review. A completed verification exits `0` even when it finds
rejections or item-level errors.

## Apply

Apply specific rejected findings or every applyable finding:

```console
$ uv run wlreviser apply CONFIG.yaml REPORT.json WL-0123456789AB WL-ABCDEF012345
$ uv run wlreviser apply CONFIG.yaml REPORT.json all
```

Apply validates that the report matches the current configuration, updates successful items, then
commits each affected Weblate component and pushes unless `push_after_commit` is `false`.

**Apply does not check whether a Weblate translation changed after the report was generated.** Use
a fresh report when overwriting a newer translation would be unacceptable.

## Development

```console
$ uv run pytest
$ uv run ruff check .
$ uv run pyright
```

Run all three checks before submitting changes.

