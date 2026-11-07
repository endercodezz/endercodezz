# Private Gitea statistics

The API contract was inspected on 2026-11-07 at
`https://git.ywfl.dev/swagger.v1.json`; `/api/v1/version` reported `28.2.0`.
The instance requires the generator's User-Agent and JSON Accept headers.
This inspection is not authenticated verification.

Configure the Actions secret `GITEA_TOKEN` with an API token belonging to the
account whose commits should count. It needs read access to the user and all
repositories to include (scoped tokens: `read:user` and `read:repository`, with
private repositories permitted). No password is used. The token is sent only in
the Authorization header to the fixed instance, and HTTP redirects are refused.
Only the trusted Render panels step receives it.

The collector uses `/user`, paginated `/repos/search` with private access enabled,
each repository's actual default branch in `/repos/{owner}/{repo}/commits`,
`/languages`, `/releases`, `/git/commits/{sha}`, and `/git/commits/{sha}.diff`.
For repository administrators it also reads `/push_mirrors`. The schema's commit
JSON has file names and statuses but no patches, so external contributions use
the raw diff endpoint. Unlinked authors, merge diffs, unavailable patches and
incomplete or ambiguous hunks never produce estimated language bytes.

Mirror identity is resolved from `original_url`, readable push-mirror metadata,
or the optional Actions secret `GITEA_MIRRORS`. This is a JSON object, for example:

```json
{"local-owner/local-copy": "github:public-owner/canonical-project"}
```

Values can also be `gitea:owner/canonical-project`. Names may differ between
providers. Store private mappings in the secret, never in this repository.
The canonical repository must be readable and included in collection. Its
snapshot supplies repository/language/release totals; mirrored copies are
excluded entirely from combined totals, including any divergent mirror-only
commits. Raw provider totals remain available for comparison. Manual copies or
rewritten mirrors with no origin metadata must be mapped explicitly; names and
matching language totals are never used to guess identity. Detected shared
history without a mapping stops generation, as do missing canonical repositories
and mapping cycles. SHA deduplication additionally protects commit-level totals.

Verified public canonical GitHub names and descriptions remain public when a
private mirror uses identical metadata. Only exact fields of the identified
public canonical repository are exempted from the mirror's privacy needles;
private URLs, alternate names and all existing GitHub private guards remain.
The JSON privacy scan checks string values and unexpected keys. Static schema
labels, validated language-counter labels, numeric counts and ISO activity-date
keys are not interpreted as repository metadata; tokens are checked against the
entire output, including keys. Invalid aggregate shapes stop publication.
For README generation, its existing public prose and links must remain byte for
byte unchanged outside the generated numbers block. Private metadata scanning
applies to the new block; token scanning still covers the entire document.

`assets/stats.json` exposes only aggregate provider counters, language byte
totals, dated activity, mirror exclusions and measurement diagnostics. No private
repository records, release tags, paths, URLs, SHAs or code are exported.
Own non-forks supply full language sizes. External repositories supply only
added bytes from commits whose primary author resolves to the authenticated
account. Shared commits are measured once, with full owned-repository sizes
taking precedence over patch bytes.

GitHub's contribution calendar and its PR/issue metrics retain their original
meaning. Commit rhythm, language shares and private release aggregates include
deduplicated Gitea data when authenticated. Gitea commit activity is also exported
separately by date. Card identities and layouts remain unchanged.

When the token is missing, the header, README and JSON explicitly identify a
GitHub-only result; Gitea totals are null, not fabricated zeroes. Authentication,
access, API, or mirror-resolution failures abort before writing public artifacts.
Logs contain status and aggregate counters only. Local tests use synthetic data;
successful authenticated integration must be verified separately in Actions.
