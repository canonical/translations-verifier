# Configuration reference

The verifier reads two independent YAML files:

- The **project configuration** describes the trusted GitHub repository, ARB layout,
  deterministic rules, and acquisition limits.
- The optional **AI configuration** describes an OpenAI-compatible provider and bounds paid
  semantic review.

Both schemas are strict. Unknown fields, invalid values, and YAML documents whose root is not
an object are rejected. Validate configuration before running a review:

```bash
uv run translations-verifier config validate \
  --project-config examples/project.yaml \
  --ai-config examples/ai.yaml
```

## Project configuration

### Top-level fields

#### `repository`

- **Required:** yes
- **Type:** string
- **Accepted forms:** `owner/name` or `https://github.com/owner/name`

Identifies the only GitHub repository the verifier may inspect. A `.git` suffix and trailing
slash are normalized away. A pull-request URL for a different repository is rejected.

```yaml
repository: canonical/ubuntu-desktop-provision
```

#### `main_branch`

- **Required:** no
- **Type:** string
- **Default:** `main`

Names the branch pull requests are expected to target. A PR targeting another branch receives
an error finding (`policy.unexpected_base`). Source ARBs are fetched from the PR's immutable
base SHA, not by resolving this branch name during verification.

```yaml
main_branch: main
```

#### `translation_sets`

- **Required:** yes
- **Type:** non-empty list of translation-set objects

Describes every ARB filename family that may be reviewed. Set names must be unique. Two sets
may not have the same `directory` and `filename_pattern`. A changed ARB must match exactly one
set; unmatched or ambiguous ARBs produce policy errors.

A monorepo normally has one entry for each independently generated localization directory.

### Translation-set fields

#### `translation_sets[].name`

- **Required:** yes
- **Type:** string

A unique human-readable identifier for the set. It distinguishes configuration entries; it
does not affect ARB path matching.

```yaml
name: ubuntu-provision
```

#### `translation_sets[].directory`

- **Required:** yes
- **Type:** repository-relative POSIX path

Directory containing this set's ARB files. Absolute paths, `.` components, and `..` traversal
are rejected. A trailing slash is removed. Matching is exact: nested directories do not match.

```yaml
directory: packages/ubuntu_provision/lib/src/l10n
```

#### `translation_sets[].filename_pattern`

- **Required:** yes
- **Type:** filename containing exactly one `{locale}` field

Maps filenames to locale identifiers and constructs source/context paths. It must be a
filename, not a path. Everything before and after `{locale}` is matched literally.

```yaml
filename_pattern: ubuntu_provision_{locale}.arb
```

This pattern maps `ubuntu_provision_de.arb` to locale `de` and constructs the English path as
`ubuntu_provision_en.arb` when `source_locale` is `en`.

#### `translation_sets[].source_locale`

- **Required:** no
- **Type:** string
- **Default:** `en`

Locale inserted into `filename_pattern` to locate the authoritative source ARB at the PR base
SHA. Changes to the source-locale ARB itself are outside version 1 scope and produce
`policy.source_changed`. The source locale cannot also appear in `context_locales`.

#### `translation_sets[].include_locales`

- **Required:** no
- **Type:** list of strings
- **Default:** `[]`

Optional allowlist for target locales. When non-empty, only listed locales from this
translation set are reviewed. An empty list means all locales are eligible unless excluded.
The source file still resolves through `source_locale` independently of this filter.

```yaml
include_locales: [de, es, fr]
```

#### `translation_sets[].exclude_locales`

- **Required:** no
- **Type:** list of strings
- **Default:** `[]`

Optional denylist for target locales. Excluded ARBs no longer match the translation set and
therefore produce `policy.unmatched_arb` if changed. A locale cannot appear in both
`include_locales` and `exclude_locales`.

```yaml
exclude_locales: [en_GB]
```

#### `translation_sets[].context_locales`

- **Required:** no
- **Type:** ordered list of strings
- **Default:** `[]`

Locales whose existing values at the immutable base SHA may be sent to AI as additional
translation context. Order is significant: locales are considered from first to last. The
current target locale, missing/malformed files, missing keys, and values identical to the
source or target are omitted. Context is never used by deterministic checks.

```yaml
context_locales: [de, es, fr]
```

Avoid very large lists: each context ARB is fetched and counts toward repository byte limits.

#### `translation_sets[].max_context_chars`

- **Required:** no
- **Type:** integer from `0` to `50,000`
- **Default:** `4,000`

Maximum sum of message characters added from `context_locales` for one AI-reviewed key. The
verifier stops adding locales when the next value would exceed the limit. Set it to `0` to
disable cross-locale text while retaining the configured locale list.

### `policy`

- **Required:** no
- **Type:** deterministic-policy object
- **Default:** all policy field defaults below

Controls deterministic comparison between the English source and changed target. Built-in
ARB, ICU, prompt-injection, unsafe-control, ANSI, zero-width, bidi-override, and empty-string
checks remain active regardless of these options.

#### `policy.variable_patterns`

- **Required:** no
- **Type:** list of Python regular-expression strings
- **Default:** `[]`

Defines project-specific variable syntaxes that must appear with exactly the same matched
values and multiplicities in source and target. Every pattern is compiled during config
loading. Use YAML quoting carefully so backslashes reach the regular-expression engine.

For each configured pattern, the verifier uses Python's regular-expression engine to find all
non-overlapping matches in the English source and target. It compares the complete matched
text (`group(0)`) as a multiset:

- Match order does not matter.
- Reordering variables is allowed.
- Renaming, removing, adding, or duplicating a matched variable is rejected.
- Matching is case-sensitive unless the pattern explicitly enables a flag such as `(?i)`.
- Capture groups do not change comparison behavior; the whole match is always compared.

For this configuration:

```yaml
variable_patterns:
  - '\$\{[A-Za-z_][A-Za-z0-9_]*\}'
```

the pattern recognizes shell-style variables such as `${HOME}` and `${user_name}`. Given the
English source `Copy ${SOURCE} to ${DESTINATION}`:

| Target text | Result | Reason |
|---|---|---|
| `Copiar ${SOURCE} a ${DESTINATION}` | Pass | Both exact variables occur once. |
| `Copiar a ${DESTINATION} desde ${SOURCE}` | Pass | Variable order may change. |
| `Copiar ${SOURCE} a ${TARGET}` | Fail | `${DESTINATION}` was renamed to `${TARGET}`. |
| `Copiar a ${DESTINATION}` | Fail | `${SOURCE}` is missing. |
| `Copiar ${SOURCE} a ${DESTINATION} (${SOURCE})` | Fail | `${SOURCE}` was duplicated. |

Add one list item per variable syntax used by the project. Each pattern is evaluated
independently:

```yaml
variable_patterns:
  - '\$\{[A-Za-z_][A-Za-z0-9_]*\}' # ${VARIABLE}
  - '\{\{[A-Za-z_][A-Za-z0-9_]*\}\}' # {{variable}}
  - '@[A-Z][A-Z0-9_]*@'                 # @VARIABLE@
```

Use single-quoted YAML strings for regexes where practical: YAML then preserves backslashes
literally. Keep patterns narrow enough to match only immutable variable tokens. A broad
pattern such as `\S+` would treat ordinary words as variables and reject normal translation.
Avoid patterns that can match an empty string or use expensive nested repetition, because
they add noise or unnecessary processing to every changed message.

This setting does not transform text or tell the AI how to translate a variable; it is a
deterministic equality check performed before AI review. ICU placeholders such as `{name}`
and printf tokens such as `%s` or `%(name)s` are checked separately without configuration, so
do not add patterns for those unless the project uses a distinct syntax not covered by the
built-in checks.

#### `policy.immutable_tokens`

- **Required:** no
- **Type:** list of strings
- **Default:** `[]`

Case-sensitive literal substrings whose occurrence count must be identical in source and
target. Use this for product names, trademarks, protocol names, or terms translators must not
alter.

```yaml
immutable_tokens: [Ubuntu, AppArmor]
```

#### `policy.preserve_urls`

- **Required:** no
- **Type:** boolean
- **Default:** `true`

When enabled, exact HTTP and HTTPS URL values and multiplicities must match the English
source. Disable only when localized URLs are deliberately supported and separately reviewed.

#### `policy.preserve_command_flags`

- **Required:** no
- **Type:** boolean
- **Default:** `true`

When enabled, command options such as `--dry-run`, `--output=file`, and `-v` must match source
values and multiplicities exactly.

#### `policy.preserve_whitespace`

- **Required:** no
- **Type:** boolean
- **Default:** `true`

When enabled, leading and trailing whitespace must match the source exactly, and newline, tab,
and backslash counts must be preserved. Internal ordinary spaces may still change naturally
as part of translation.

#### `policy.allow_bidi_isolates`

- **Required:** no
- **Type:** boolean
- **Default:** `false`

Controls whether Unicode bidi isolate characters `U+2066` through `U+2069` are permitted.
Set this to `true` only for projects/locales that intentionally use isolates. Dangerous bidi
overrides and embeddings remain blocked regardless of this setting.

#### `policy.markup`

- **Required:** no
- **Type:** markup-policy object
- **Default:** markup disabled with `href` and `src` marked immutable

Configures deterministic markup parsing, allowlisting, structure comparison, and immutable
attribute comparison.

### Markup-policy fields

#### `policy.markup.mode`

- **Required:** no
- **Type:** one of `none`, `xml`, or `html`
- **Default:** `none`

Selects markup handling:

| Value | Behavior |
|---|---|
| `none` | Rejects tag-like markup in either source or target. |
| `xml` | Parses strict, well-formed XML fragments; tags must be correctly nested and closed. |
| `html` | Parses HTML-like fragments and recognizes standard void elements such as `<br>` and `<img>`. |

In `xml` and `html` modes, target tag names, order, nesting, and multiplicities must match the
source. `script`, `style`, `iframe`, `object`, and `embed` are always rejected.

#### `policy.markup.allowed_tags`

- **Required:** no
- **Type:** list of tag names
- **Default:** `[]`

Allowlist of tags in `xml` or `html` mode. An empty list means **all non-dangerous tags are
allowed**, not that all tags are denied. To constrain markup, list every expected tag.

```yaml
allowed_tags: [a, b, br, code]
```

This field has no effect when `mode` is `none`, because all markup is rejected in that mode.

#### `policy.markup.allowed_attributes`

- **Required:** no
- **Type:** mapping from tag name to a list of attribute names
- **Default:** `{}`

Attributes permitted on each allowed tag. Unlike `allowed_tags`, an empty mapping means **no
attributes are allowed**. Event-handler attributes such as `onclick` are always rejected.
`javascript:` values in `href` and `src` are always rejected.

```yaml
allowed_attributes:
  a: [href, title]
  img: [src, alt]
```

#### `policy.markup.immutable_attributes`

- **Required:** no
- **Type:** list of attribute names
- **Default:** `[href, src]`

Attributes whose complete `(tag, attribute, value)` combinations and multiplicities must be
identical in source and target. Each immutable attribute must also be permitted for its tag in
`allowed_attributes`, or the markup is rejected before comparison.

### `limits`

- **Required:** no
- **Type:** limits object
- **Default:** all limit defaults below

Bounds GitHub acquisition and deterministic workload. Limit exhaustion fails closed as
`incomplete`; if another error finding exists, overall status remains `unacceptable`.

#### `limits.max_changed_files`

- **Required:** no
- **Type:** integer from `1` to `1,000`
- **Default:** `100`

Maximum number of files reported as changed by GitHub for the entire PR, including non-ARB
files. Exceeding it stops changed-file acquisition and makes verification incomplete.

#### `limits.max_changed_keys`

- **Required:** no
- **Type:** integer from `1` to `100,000`
- **Default:** `1,000`

Maximum inserted, changed, or deleted target message operations accumulated across all
matched ARB files. Exceeding it truncates processing to this many operations and marks the
result incomplete.

#### `limits.max_file_bytes`

- **Required:** no
- **Type:** integer from `1` to `20,000,000`
- **Default:** `2,000,000`

Maximum raw size of each fetched ARB file. It applies to English source files, base and head
target files, and cross-locale context files. A larger file is not parsed and makes the result
incomplete.

#### `limits.max_total_bytes`

- **Required:** no
- **Type:** integer from `1` to `100,000,000`
- **Default:** `20,000,000`

Maximum combined bytes of unique ARB files fetched during one run. This includes source,
base-target, head-target, and context files. Files are cached by immutable SHA and path, so a
file reused for multiple keys is counted only once. Exceeding the limit makes affected work
incomplete.

## AI configuration

The AI configuration is needed only when `verify` runs without `--deterministic-only`. The
verifier sends one request per deterministically clean inserted or changed key. There is no
separate key-count setting; `max_input_tokens_per_run`, repository limits, provider quotas,
and protected-environment approval bound usage.

#### `endpoint`

- **Required:** yes
- **Type:** absolute HTTP(S) URL

Base URL for an OpenAI-compatible API. The client appends `/chat/completions`, so include the
provider's API prefix, commonly `/v1`.

```yaml
endpoint: https://openrouter.ai/api/v1
```

HTTPS is required for non-local hosts unless `allow_insecure_http` is enabled. Plain HTTP is
accepted by default only for `localhost`, `127.0.0.1`, and `::1`.

#### `model`

- **Required:** yes
- **Type:** string

Provider-specific model identifier sent unchanged in every request.

```yaml
model: google/gemini-3.7-flash
```

Choose a model that supports the provider's OpenAI-compatible chat completions and JSON
response format.

#### `api_key_env`

- **Required:** yes
- **Type:** uppercase environment-variable name

Names the environment variable from which the bearer token is read at runtime. It must match
`[A-Z_][A-Z0-9_]*`. This field is a name, not the credential itself; do not put `${...}` or an
API key in YAML.

```yaml
api_key_env: TRANSLATION_VERIFIER_API_KEY
```

#### `timeout_seconds`

- **Required:** no
- **Type:** number greater than `0` and at most `600`
- **Default:** `60.0`

HTTP timeout used by the AI client. A timeout may be retried according to `max_retries`. If all
attempts fail, that key is unreviewed and the overall result becomes incomplete unless another
error already makes it unacceptable.

#### `max_retries`

- **Required:** no
- **Type:** integer from `0` to `3`
- **Default:** `1`

Number of retries for network errors and transient HTTP statuses `429`, `500`, `502`, `503`,
and `504`. A value of `0` makes one request with no retry. The verifier also allows at most one
additional repair attempt for malformed envelopes or schema-invalid verdicts when this value
is at least `1`. JSON Schema fallback is controlled separately.

#### `temperature`

- **Required:** no
- **Type:** number from `0` to `2`
- **Default:** `0.0`

Sampling temperature sent to the provider. Keep `0` for reproducible review. Some compatible
providers may ignore or restrict this parameter.

#### `max_output_tokens`

- **Required:** no
- **Type:** integer from `50` to `10,000`
- **Default:** `4,000`

Value sent as `max_tokens` for each completion. It must be large enough for the strict verdict,
rationale, and any suggested translation. Provider truncation is rejected and makes the key
incomplete.

#### `max_input_tokens_per_run`

- **Required:** no
- **Type:** integer from `100` to `1,000,000`
- **Default:** `200,000`

Maximum estimated input tokens across AI-eligible keys in one verifier run. Estimation is
conservative and tokenizer-independent: one token per four serialized JSON characters,
rounded up. When adding the next key would exceed the cap, that key and all remaining eligible
keys are skipped and the run becomes incomplete.

This is a local abuse/cost bound, not a billing guarantee. Enforce durable monthly limits at
the provider account/project/API-key level.

#### `max_input_chars_per_key`

- **Required:** no
- **Type:** integer from `1,500` to `100,000`
- **Default:** `12,000`

Maximum serialized user-payload characters for one AI request. The count includes the key,
operation, source/target text and metadata, locale identifiers, prompt version, and selected
cross-locale context. Exceeding it leaves that key unreviewed and makes the run incomplete.

#### `allow_insecure_http`

- **Required:** no
- **Type:** boolean
- **Default:** `false`

Allows plain HTTP for a non-local AI endpoint. Leave this disabled in normal operation because
the API key and translation data would otherwise travel without transport encryption. Local
Ollama endpoints are accepted over HTTP without enabling this override.

#### `allow_json_object_fallback`

- **Required:** no
- **Type:** boolean
- **Default:** `true`

If the provider returns HTTP `400` for strict JSON Schema mode, retry once using
`response_format: {type: json_object}`. The returned object is still validated against the
strict local Pydantic verdict contract. Disable this when the provider reliably supports JSON
Schema and you prefer no compatibility fallback.

## Complete examples

See [examples/project.yaml](../examples/project.yaml) and
[examples/ai.yaml](../examples/ai.yaml) for configurations validated by the current CLI.
