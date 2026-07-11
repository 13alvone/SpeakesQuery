# Backup & Recovery

SpeakesQuery is a local-first desktop app - there is no cloud backup, no
managed service, and no remote sync. Every byte of your data lives on your
machine, and the responsibility for backing it up is yours. This guide
catalogues exactly what to preserve so a fresh checkout (or a restored
machine) can pick up where you left off.

## What to back up

### Critical - irrecoverable if lost

| Path | Contents | Why critical |
|---|---|---|
| `~/.speakes-query/master.key` | Fernet master key for the credential vault | Without this key, every encrypted credential in `credentials.sqlite` becomes unreadable. **Back this up first.** Created 0600 on first run. |
| `credentials.sqlite` | Encrypted API keys, secrets, and credentials per ingestion script | Worthless without `master.key`, but together they are how the engine talks to authenticated APIs. |
| `indexes/IMMUTABLE/` | The protected forever-data tree: OEB pick journal (`ag_picks*`), curator telemetry / reflections / playlists / topic snapshots | The decade-horizon trading and viewing record. Never garbage-collected at runtime, and included in the **default** backup set (per-file hashed in `DIR_TARGETS_HASHED`) even though its parent `indexes/` is summary-only. |

### Important - represents user work

| Path | Contents |
|---|---|
| `indexes/` | All ingested Parquet data (the actual search corpus) |
| `lookups/` | CSV/JSON/Parquet/TSV lookup tables for the `lookup` command |
| `saved_searches/` | YAML configs for scheduled searches (recurring queries + email alerts) |
| `macros/` | Saved SPQL macro definitions |
| `alert_groups/` | Alert group YAML configs (multi-search Claude dispatches) |
| `boilerplate_prompts/` | Reusable prompt templates for alert groups |
| `email_groups/` | Mailing-list YAML configs (`@group_name` recipients in saved searches and alert groups). |
| `analyzer_prompts/` | Per-search Claude prompt overrides for the analyzer. |
| `models/` | LLM model registry YAMLs (`models/<id>.yaml` - provider, endpoint, costs, sampling). User edits to pricing / endpoints live here. |
| `notebooks/` | Notebook user data (cell state + reactive cache hashes) - your analysis work. |
| `jobs/` | Saved query result snapshots (Parquet) + `_index.json` metadata |
| `global_settings.yaml` | User overrides of engine defaults (limits, intervals, allowed_api_domains, SMTP, analyzer config) |

### Useful - recoverable but laborious to recreate

| Path | Contents |
|---|---|
| `last_chance.sqlite` | 30-day soft-delete recovery for saved searches, macros, alert groups, and boilerplate prompts. Restoring from this lets you undelete recent destructions. |
| `scheduled_inputs.db` | Ingestion task definitions - easily recreated by re-installing scripts from the library, but loses any one-off custom edits. |
| `scheduled_inputs_history.db` | Execution-history audit trail for ingestion runs. |
| `saved_searches.db` | Index of saved searches - derived from `saved_searches/*.yaml`, mostly. |
| `saved_search_history.db` | Email-alert send history. |
| `alert_group_runs.sqlite` | Audit trail of past alert-group dispatches (estimated/actual tokens, cost, run status). |
| `claude_api_history.sqlite` | Complete Claude API call history: gzip-compressed request + response payloads plus tokens, cost, latency. Lives outside `indexes/` on purpose so cleanup can't touch it - retention is manual. Every Claude call you made (and paid for) is in here. |
| `llm_call_history.sqlite` | Provider-agnostic history for every router-dispatched LLM call (`\| llm` pipes, local-model alert groups) - plus the content-hash cache that makes idempotent re-runs free. Also at the project root, outside `indexes/`, so cleanup never evicts paid-for cache hits. |
| `notebook_cache/` | Notebook reactive-cache payloads. Regenerable by re-running the notebook, so the backup tool records aggregate stats only (summarized tier). |
| `indexes/logs/` | SPQL-queryable Parquet log stream (config changes, search runs, alert groups, Claude metadata, ingestion, system). Has its own `max_logs_size_gb` budget so noisy logging never evicts your ingested data. |
| `.env` | SMTP credentials + optional API keys (Anthropic, etc.). Easy to recreate but contains secrets. |

### Do NOT back up

| Path | Why |
|---|---|
| `.speakesQueryDevEnv/`, `env/`, `.venv/` | Python virtualenv - recreated by `setup.sh` or `pip install -r requirements.txt`. |
| `__pycache__/`, `*.pyc` | Bytecode - regenerated automatically. |
| `executed_scheduled_searches/` | Per-run output artifacts; trimmed by the cleanup job. Keep only if you need an audit trail. |
| `lexers/antlr4_active/` | Generated parser code - rebuild from `lexers/speakesQuery.g4` via `antlr4 -Dlanguage=Python3 speakesQuery.g4 -o antlr4_active`. |
| `frontend/static/temp/` | Transient UI scratch space. |
| `_default_indexes/` (Docker only) | Baked into the image - the entrypoint seeds these into empty volume mounts. |

## Automated backup via `tools/persistence.py`

`./update.sh` now snapshots and tarballs every user-data target before
the container rebuild and diffs the post-rebuild state to surface any
regression. Default destination is `~/speakesquery-backups/`.

```bash
./update.sh                       # auto: snapshot → backup → rebuild → diff
./update.sh --no-backup           # skip the tarball (snapshot+diff still run)
./update.sh --no-snapshot         # skip pre/post snapshot+diff entirely
./update.sh --backup-dir /mnt/x   # store backups elsewhere
./update.sh --rollback            # restore the most recent backup tarball
```

Behind the scenes, `./update.sh` shells to `tools/persistence.py` -
which is also usable directly for ad-hoc operations (no container
involved):

```bash
# Capture current state to a JSON manifest
python3 -m tools.persistence snapshot --output ~/snap.json

# Tar.gz every user-data target (small files only by default)
python3 -m tools.persistence backup --output ~/userdata.tar.gz
python3 -m tools.persistence backup --include-indexes  # also bundle parquet

# Restore from a backup (refuses to clobber live files unless --force)
python3 -m tools.persistence restore --tarball ~/userdata.tar.gz
python3 -m tools.persistence restore --tarball ~/userdata.tar.gz --force --yes

# Compare two snapshots (exit non-zero if anything regressed)
python3 -m tools.persistence diff --before ~/before.json --after ~/after.json
```

The tool is stdlib-only so it runs on the bare host Python - no
virtualenv activation required.

`indexes/IMMUTABLE/` is in the **default** backup set (per-file hashed
via `DIR_TARGETS_HASHED` in `tools/persistence.py`) - you do NOT need
`--include-indexes` to capture the decade-horizon trading + curator
record, even though the rest of `indexes/` is opt-in. When
`--include-indexes` is also passed, the tool de-dups so IMMUTABLE isn't
bundled twice.

### Health check at runtime

The Flask server logs a Persistence Audit on every boot listing each
user-data target and warning loudly if any are missing. The same
inventory is exposed at `/api/persistence/audit` for scriptable use:

```bash
curl http://localhost:5111/api/persistence/audit | jq '.issues'
```

Empty `issues` array = every target is healthy and bind-mounted
correctly. Non-empty = check `desktop_app/docker-compose.yml` against
the canonical target list in `tools/persistence.py`.

## Quick backup recipes

### Bare-metal install (macOS / Linux)

A single tar archive captures everything that matters:

```bash
cd /path/to/speakesQuery
tar -czf speakesquery-backup-$(date +%Y%m%d).tar.gz \
  ~/.speakes-query \
  credentials.sqlite \
  global_settings.yaml \
  indexes lookups saved_searches macros alert_groups \
  boilerplate_prompts email_groups analyzer_prompts jobs \
  last_chance.sqlite \
  scheduled_inputs.db scheduled_inputs_history.db \
  saved_searches.db saved_search_history.db \
  alert_group_runs.sqlite analyzer_results.sqlite \
  claude_api_history.sqlite \
  .env 2>/dev/null
```

Note: `indexes/` includes the `indexes/logs/` subtree, which will grow up to `max_logs_size_gb` (default 5 GB). If you only want your ingested data and not the log stream, exclude it with `--exclude='indexes/logs/*'`.

Move that archive to a safe location (external drive, encrypted cloud
storage). The `.env` and `master.key` files contain secrets - encrypt the
archive at rest if you store it anywhere outside your laptop.

### Docker install

The bind mounts in `desktop_app/docker-compose.yml` put all stateful files
on the host filesystem alongside the project tree, so the same `tar`
command above works whether you're running bare-metal or Dockerised.
The credential vault is mounted from `${HOME}/.speakes-query` on the host,
so the key is shared between the two install modes.

To inspect what's currently mounted:

```bash
docker inspect speakesquery-desktop --format '{{json .Mounts}}' | jq
```

## Restore

To restore to a fresh machine or a freshly cloned repo:

1. Clone the project and run `./setup.sh` (bare-metal) or `./install.sh`
   (Docker) once so the directory layout exists.
2. Stop the running engine if it's already started (`./install.sh --stop`
   for Docker; Ctrl-C the bare-metal process).
3. Extract the backup archive into the project root:
   ```bash
   tar -xzf speakesquery-backup-20260416.tar.gz -C /path/to/speakesQuery
   ```
   The archive's relative paths place each file back where it belongs.
   The `~/.speakes-query/` directory restores to your home directory.
4. Verify file permissions on the master key:
   ```bash
   chmod 600 ~/.speakes-query/master.key
   ```
   The credential vault auto-corrects loose permissions on load, but
   pre-fixing avoids a startup warning.
5. Start the engine. The first query against any restored Parquet under
   `indexes/` confirms the data side; running a saved search confirms
   the credential vault decrypted successfully.

## Recovery scenarios

### "I deleted a saved search yesterday and want it back"

The 30-day soft-delete in `last_chance.sqlite` covers this. There is no
built-in UI restore yet (tracked separately); the immediate recovery is
to extract the YAML body from `last_chance.sqlite`:

```bash
sqlite3 last_chance.sqlite \
  "SELECT yaml FROM last_chance WHERE name='my_deleted_search' LIMIT 1;" \
  > saved_searches/my_deleted_search.yaml
```

Then restart the engine to pick it up.

### "I lost ~/.speakes-query/master.key but I still have credentials.sqlite"

Without the master key, the encrypted credential blobs in
`credentials.sqlite` are not recoverable - Fernet is authenticated
encryption with no backdoor. You will have to re-enter every API key
through the Settings page after generating a new master key (which the
engine does automatically on first run).

### "My `claude_api_history.sqlite` is too big"

The file is intentionally never auto-pruned so you never lose an expensive call. Manual path:

```bash
# Back up first - prunes are one-way
cp claude_api_history.sqlite claude_api_history.$(date +%F).sqlite

# Delete calls older than 30 days + reclaim space in one shot
curl -X POST http://localhost:5111/api/claude-history/vacuum \
  -H "Content-Type: application/json" \
  -d "{\"older_than_epoch\": $(python -c 'import time; print(int(time.time() - 30*86400))')}"
```

Or set `claude_history_retain_payloads: false` in `global_settings.yaml` to stop storing request/response JSON for *new* calls - metadata still lands, so cost tracking keeps working.

### "I want to migrate from bare-metal to Docker (or vice versa)"

Both modes share the same on-disk layout once the Docker volume mounts
are in place. The credential vault is mounted from your home directory
in Docker, so the same `~/.speakes-query/master.key` works in both modes
without any export/import step. Just back up, switch install modes, and
restore.

## Credential vault master-key rotation

The credential vault at `credentials.sqlite` is symmetrically encrypted
with a Fernet key stored at `~/.speakes-query/master.key`. If that key is
ever compromised - a laptop is lost, a backup of the key file leaks, a
shared server is breached - **every stored API key needs to be
re-encrypted under a new master key**. Back up first: rotation is
cold and one-way.

### When to rotate

- The master-key file has been exposed (published to a git repo,
  attached to an email, copied to a shared machine).
- You're handing the install off to a new operator and want a clean
  cryptographic break.
- Your security policy mandates periodic rotation.

**Do NOT rotate just because you're curious.** Every stored credential
has to survive the re-encrypt, and bugs during rotation are the easiest
way to lose production tokens you can't re-issue.

### Procedure

1. **Stop the SpeakesQuery server** so no ingestion job tries to read
   credentials mid-rotation:

   ```bash
   # Docker
   ./update.sh   # brings the container back up afterwards
   # or bare-metal
   systemctl stop speakesquery   # whatever service name you use
   ```

2. **Back up both files** before touching them:

   ```bash
   cp credentials.sqlite credentials.$(date +%F).sqlite
   cp ~/.speakes-query/master.key ~/.speakes-query/master.$(date +%F).key
   ```

3. **Run the rotation tool.** It reads every row with the old key,
   decrypts, re-encrypts with a freshly-generated new key, writes both
   the new DB and the new master-key file:

   ```bash
   python -m tools.rotate_vault_key \
       --old-key ~/.speakes-query/master.key \
       --new-key ~/.speakes-query/master.new.key \
       --db credentials.sqlite \
       --dry-run     # first pass: shows what would change, writes nothing
   python -m tools.rotate_vault_key \
       --old-key ~/.speakes-query/master.key \
       --new-key ~/.speakes-query/master.new.key \
       --db credentials.sqlite
   ```

4. **Swap keys in place** once the rotation reports success:

   ```bash
   mv ~/.speakes-query/master.key ~/.speakes-query/master.pre-rotate.key
   mv ~/.speakes-query/master.new.key ~/.speakes-query/master.key
   ```

5. **Restart SpeakesQuery.** The analyzer's Fernet cache is
   process-local, so a restart is required - the in-memory key is
   re-read from disk on the next request.

6. **Verify by running one ingestion script** that uses a credential.
   If it succeeds, delete the pre-rotate backups after your standard
   retention window. If it fails, restore from the backup copies
   created in step 2 and file a bug.

### Limitations

- **Rotation is a cold operation.** SpeakesQuery does not support hot
  key rotation - in-flight requests may still hold a reference to the
  old key in memory for the life of the request. Always stop the server
  first.
- **The tool ships as an operator utility in `tools/`**. It is
  deliberately conservative: it reads one row at a time and writes the
  new DB to a sibling file before the swap. See the module docstring
  at `tools/rotate_vault_key.py` for the full argument reference.

## Backup hygiene

- **Cadence**: weekly is fine for most users; nightly if you ingest
  high-velocity data and care about the last day's history.
- **Retention**: keep at least the last 4 weekly backups; the 30-day
  soft-delete already covers in-app restoration of recently-removed
  saved searches and macros.
- **Off-site**: at least one backup should live outside your laptop -
  external drive that lives in a different room, or encrypted cloud
  storage. Local-only backups don't help with theft or hardware failure.
- **Verify**: every few months, do a test restore into a scratch directory
  and confirm at least one query returns expected results. Untested
  backups have a way of being unreadable when you actually need them.
