# WST local knowledge collector

`tools/research/collect_wst_sources.py` is a local, read-only tool for the
private WST channel available to the user's Telegram account. It is not a
DenisStock Django feature: it does not enter Docker, production, Celery, cron,
or warehouse business logic.

The tool reuses the existing local Telethon research session and never starts an
interactive login. Do not put a phone number, Telegram code, 2FA password,
session file, or API credentials into Git or a prompt. If the session expires,
authorize it manually using the existing local research workflow, then rerun
`doctor`.

## Safety boundary

Only read-only Telegram calls are used: dialog discovery, message reads,
history iteration, and optional media downloads. Discussion comments are not
read. The collector never sends messages or files, reacts, joins/leaves,
forwards, edits, deletes, invites, or uses browser cookies.

Everything collected stays under ignored `research_inputs/wst/`. Never commit
that directory, `.env.research.local`, `*.session`, local model caches, media,
transcripts, OCR output, or generated SQLite databases. Upload only reviewed
files from `research_inputs/wst/ai_corpus/` to ChatGPT/File Library; never
upload sessions, raw media, state databases, secrets, or unreviewed raw data.

## Install locally

Windows PowerShell:

```powershell
python -m venv .venv-research
.venv-research\Scripts\activate
pip install -r tools/research/requirements-wst.txt
```

Install `ffmpeg` (which also provides `ffprobe`) and Tesseract OCR with `rus`
and `eng` language packs separately. `faster-whisper` runs locally; `large-v3`
is the quality default, while `medium` or `small` can be chosen for a quicker
local experiment. Use `--device cpu`, `--device cuda`, or `--device auto`.
Model caches must remain outside the repository or in ignored directories.

## Safe sequence

```powershell
python tools/research/collect_wst_sources.py doctor

python tools/research/collect_wst_sources.py inventory `
  --channel-id 3278525266 `
  --navigation-message-id 3

python tools/research/collect_wst_sources.py collect-navigation `
  --channel-id 3278525266 `
  --navigation-message-id 3 `
  --download-media

python tools/research/collect_wst_sources.py process `
  --whisper-model large-v3 `
  --device auto

python tools/research/build_wst_corpus.py validate
python tools/research/build_wst_corpus.py build --pack-max-mb 8
python tools/research/build_wst_corpus.py search "целевая аудитория"
```

Only after reviewing the navigation result should a user run:

```powershell
python tools/research/collect_wst_sources.py collect-all `
  --channel-id 3278525266 `
  --download-media
```

`inventory` reads metadata only. It estimates posts, media sizes and durations
without downloading attachments. `collect-navigation` follows only explicit
same-channel links from navigation message `3`; it keeps link order, edge type,
and a cycle-safe navigation path. Links to other channels and external URLs are
recorded as external references and are not fetched.

## Resume, failures and evidence

`research_inputs/wst/state/wst_state.sqlite3` records local checkpoints and
hashes. Valid existing downloads are skipped, `.part` files are atomically
renamed only after a successful download, and one failed media item does not
stop a batch. FloodWait is reported in state rather than bypassed. Rerun the
same command after correcting a local dependency or waiting.

Documents retain page/slide/sheet references. Video transcripts retain segment
timestamps; visual OCR retains frame timestamps and confidence. Low-confidence
OCR and `[НЕРАЗБОРЧИВО]` transcript segments require manual review and are never
silently invented or repaired. `build_wst_corpus.py validate` rejects chunks
without `source_ref`, incomplete transcript locations, duplicate chunks, broken
navigation edges, or an empty corpus.

## Output map

```text
research_inputs/wst/
  raw/                 posts, media manifest, navigation graph
  media/               downloaded source attachments
  extracted/           document blocks, transcripts, keyframes, OCR
  normalized/          evidence chunks and source index
  index/               local SQLite FTS5 search index
  ai_corpus/           reviewable Markdown files and bounded packs
  reports/             inventory, collection, extraction, validation reports
  state/               local checkpoint database
```

Use `doctor` before an actual collection. It checks session authorization,
channel access, navigation message availability, disk space, `ffmpeg`/
`ffprobe`, local Whisper availability, and Tesseract language support without
printing secrets or a sensitive session path.
