# Dispatcharr plugins

Plugins for [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr), and a plugin repository to install them
from. They need Dispatcharr 0.34.0 or newer.

## Install

In Dispatcharr, add this plugin repository:

```text
https://raw.githubusercontent.com/johnschult/dispatcharr-plugins/releases/manifest.json
```

The plugins then show in Dispatcharr's plugin browser, and updates arrive there too.

The manifests are signed. To have Dispatcharr check them, paste [`signing-key.asc`](signing-key.asc) into the
repository's public key in Dispatcharr's repo settings; it then shows the repository as verified, and a manifest
that doesn't match the key as failing.

## Plugins

### Guide Titles

Rewrites programme titles in the guide by rule after each EPG refresh. Typical use: Schedules Direct titles a game
"College Football" and puts the matchup in the sub-title; Guide Titles swaps them, turns "at" into "vs" and puts a
sport emoji in front, so the guide reads "🏈  Ohio State vs Michigan".

It edits the source's own programmes in place and keeps their original fields, so channels stay mapped to the
real source (Schedules Direct only downloads stations a channel maps to) and every run starts from the
originals. If a rule stops matching, titles fall back to the source's; the guide never empties.

Rules go in its settings, under one `[EPG source name]` line per source:

```text
[Schedules Direct]
swap_subtitle::^College Football$::
prefix_sport::^College Football$::🏈
```

| Rule | Does |
|---|---|
| `swap_subtitle::TITLE_RE::` | swaps title and sub-title when the title matches |
| `swap_description::TITLE_RE::EXTRACT_RE` | takes the title from the description (EXTRACT_RE's first group) |
| `regex::RE::REPLACEMENT` | regex replace on the title; `$1`, `$2` for groups |
| `replace::TEXT::REPLACEMENT` | plain replace on the title |
| `prefix_sport::RE::EMOJI` | puts EMOJI in front when the title or sub-title matches; team sports get "vs" |

The default rules cover the common sports in Schedules Direct and EPG Share titles.

### Recording Piggyback

While a channel records, other channels carrying the same provider stream play through the recording's
connection instead of opening another one: handy when a provider allows few connections. A programme set to
record gets a mark in front of its title, `ʀᴇᴄ` (small capitals) unless set otherwise, and "ʀᴇᴄ · Recording" above
its description, from when it's scheduled until it ends. Changing the mark re-marks every marked programme at the
next sync. It works alongside Guide Titles: when Guide Titles rewrites a marked programme it updates Piggyback's
copy of the original, and Piggyback puts its mark back on its next sync, so neither needs to know the other's
format.

It reads recordings from Dispatcharr's own DVR, and optionally from Sportarr. Sportarr records through
Dispatcharr's proxy like any other viewer, so its recordings never reach Dispatcharr's DVR; the plugin reads them
from Sportarr's API and finds each one's Dispatcharr channel from the stream URL Sportarr has for it. It syncs when
a channel starts or stops (Sportarr tuning in and hanging up) and on every M3U and EPG refresh. If Sportarr can't
be reached, Dispatcharr's own recordings still sync.

To use Sportarr:

1. Sportarr's channels must come from Dispatcharr's M3U, so their stream URLs are Dispatcharr proxy URLs
   (`…/proxy/ts/stream/<uuid>`). A channel from anywhere else can't be matched.
2. Approve "Access external network services" when installing or updating the plugin.
3. Under **Sportarr**, switch on **Use Sportarr's recordings too** and set its address (such as
   `http://192.168.1.10:1867`) and API key (Sportarr's Settings → General).
4. Run **Test Sportarr**. It says whether Sportarr answered and accepted the key, how many recordings it has, how
   many are on Dispatcharr channels, and names any channel that can't be matched and why.

### VOD Category Tidy

Cleans up movie and series categories for Xtream Codes apps: strips prefixes like "Movie-" and "Series-", merges
overlapping categories by rule, and hides categories containing given words. Only what apps see changes;
Dispatcharr keeps the provider's categories, so refreshes are unaffected.

## Development

Each plugin keeps its logic in a module with no Dispatcharr imports (`titles.py`, `piggyback.py`, `tidy.py`) and
the Dispatcharr side in `plugin.py`.

```sh
./test.sh              # unit + integration
./test.sh unit         # each plugin's tests/ (and the publisher's), with uv
./test.sh integration  # each plugin's integration/, inside Dispatcharr (Docker, ~5 minutes)
```

The integration tests start a throwaway container of the Dispatcharr image in `IMAGE` (it bundles Postgres and
Redis), copy the plugins into `/data/plugins` and run pytest-django there. The plugins load through Dispatcharr's
own PluginManager, so discovery, enabling, event wiring and each `plugin.py` run against real models, and each
published ZIP goes through Dispatcharr's own installer. To try another Dispatcharr version:
`DISPATCHARR_IMAGE=ghcr.io/dispatcharr/dispatcharr@sha256:… ./test.sh integration`, or the workflow's Run
workflow button. `CONTAINER=<name>` reuses a running container (started with `-v /data`) for quicker reruns.

## Releasing

Bump `version` in a plugin's `plugin.json` and push to `main`. Once the tests pass, the workflow builds that
plugin's ZIP (`publish/publish.py`), attaches it to a GitHub release tagged `<slug>-<version>`, and updates
`manifest.json` and `metadata/<slug>/manifest.json` on the `releases` branch, which is what Dispatcharr reads.
ZIPs are built byte-for-byte repeatably, and a plugin whose files changed without a version bump stops the
publish rather than replacing a released version.

Every manifest it writes carries a detached GPG signature over the bytes Dispatcharr checks
(`json.dumps(manifest, separators=(",", ":"))` plus a newline), made with the key in the `SIGNING_KEY` Actions
secret. The workflow refuses to publish without that secret, or if it doesn't match `signing-key.asc`. The
private key exists only in the secret; to replace it, generate a new key, put its private half in `SIGNING_KEY`
and its public half in `signing-key.asc`, push, and paste the new public key into Dispatcharr.

## License

MIT
