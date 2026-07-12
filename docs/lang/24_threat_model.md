# Threat Model

This document states plainly what SpeakesQuery's security layers do, what they do not do, and which risks we accept by design. It is written for a skeptical security reviewer. Where a claim is testable, the test name is cited. Where a defense is a hardening layer rather than a boundary, we say so.

The single most important sentence in this document: **RestrictedPython is a hardening layer, not a security boundary.** If you take away nothing else, take that.

## 1. Scope and trust assumptions

SpeakesQuery is a single-operator, local-first application. One person installs it, configures it, authors or reviews its ingestion scripts, and reads its output. The trust model follows from that:

- **The operator is trusted.** There is no privilege separation between "the user" and "the admin" because they are the same person. Anything the operator can do through the UI, they can already do with a shell on the same machine.
- **The machine is trusted.** SpeakesQuery runs as an ordinary process (or a Docker container bind-mounting the project tree) under the operator's UID. It does not defend against a hostile local user, hostile root, or a compromised OS. No local application can.
- **The public internet is out of bounds.** SpeakesQuery is not designed to be exposed to the internet, and no configuration of it should be treated as internet-safe. The defaults enforce this (section 2.4); the documentation repeats it; this document repeats it again.

Threat actors we actually consider:

1. **A LAN attacker**: another device on the operator's network probing for open ports.
2. **A malicious or compromised ingestion script**: code pasted from the internet, or a legitimate script whose upstream author turned hostile.
3. **Secrets leakage through logs and history stores**: API keys accidentally landing in plaintext in a queryable or backup-able artifact.

Threat actors we deliberately do not consider: nation-state attackers, local privilege escalation, physical access, supply-chain compromise of PyPI dependencies, and anyone with code execution as the app user (see the vault adjacency concession in 2.2).

## 2. Layer by layer

### 2.1 The RestrictedPython sandbox (sandboxed trust tier)

**What it is.** Ingestion scripts default to `trust_level: "sandboxed"`. They are compiled with `compile_restricted` and executed against a curated globals dict (`scheduled_input_engine/executor.py::_build_sandbox_globals`): `safe_builtins` plus hand-picked extras, a module allowlist (`pandas, requests, json, datetime, time, re, math, hashlib, base64, collections, io, bs4, lxml`), a guarded `__import__` (`_safe_import`), and a `_getattr_` guard (`_safe_getattr`) that denies the classic dunder escape vectors (`__subclasses__`, `__globals__`, `__code__`, `__class__`, `__mro__`, and friends in `_BLOCKED_ATTRS`). `hasattr` is wrapped too (`_safe_hasattr`) so existence probes agree with access denials.

**What it does.** It stops honest mistakes and raises the cost of casual attacks. A script that typos an import, wanders into the filesystem, or copy-pastes an `os.system` call fails immediately with a clear error. That is the majority of real-world risk for a tool whose scripts are authored or reviewed by the operator.

**What it does not do.** RestrictedPython is not a security boundary, and we do not pretend it is. Python sandbox escapes have a long public history: gadget chains through allowed objects, C-extension internals reachable via allowed modules (pandas and lxml are large attack surfaces by themselves), and exception-object introspection. **Assume that a determined attacker who can author a sandboxed script can escape it.** We patch specific vectors when we find them (the `_safe_hasattr` probe fix, M-CE-9, is one example in the source), but enumeration-of-badness never converges.

A concrete, known, documented gap, so you can calibrate our honesty: the `BudgetAwareRequests` proxy (section 2.3) guards `requests.get/post/put/patch/delete/head`, but its `__getattr__` passes `requests.Session` through to the real module un-wrapped. A sandboxed script that constructs a `Session()` bypasses the domain allowlist and HTTP budget. This is called out in the source (`scheduled_input_engine/cache.py`, the L-MI-13 note) rather than hidden.

**The consequence we draw.** Because the sandbox is not a boundary, the real control is provenance: all 135 shipped library scripts are author-maintained and reviewed in this repository, every script passes a mandatory test gate before it can be saved (`executor.py::execute_test`), and the operator is told to read anything they paste from elsewhere. The sandbox reduces blast radius and catches accidents; the trust decision happens before the code runs.

### 2.2 The credential vault

**What it is.** API keys are Fernet-encrypted at rest in `credentials.sqlite`; the master key lives at `~/.speakes-query/master.key` with enforced 0600 permissions (`scheduled_input_engine/credentials.py::_verify_permissions` auto-corrects loose modes and refuses to load the key if it cannot). Scripts receive decrypted values as an immutable `MappingProxyType` injected as `CREDENTIALS`, and the reference is dropped after each run. Credential names and values are validated against injection-shaped input at store time (pinned by `tests/test_credentials_vault.py::TestValidateCredentialInput`), values are verified encrypted at rest (`TestStoreRetrieve::test_value_encrypted_at_rest`), and every vault mutation emits an audit event that is never silently swallowed (`_emit_credential_event`). Key rotation is an offline, dry-run-first operator procedure: `python -m tools.rotate_vault_key` re-encrypts every row to a sibling database and refuses to overwrite anything (see `docs/lang/13_backup_recovery.md`).

**What it does.** Encryption at rest protects the credential database as a file: backups, copies synced to other machines, a stolen `credentials.sqlite` on its own, and casual `strings`-level inspection all yield ciphertext.

**What it does not do: vault adjacency, stated plainly.** The Fernet key lives on the same machine as the ciphertext, readable by the same UID that runs the app. Anyone with code execution as the app user can read the key, decrypt the vault, or more simply call the same vault API the app calls. Combine this with 2.1 and the honest conclusion is: **a malicious ingestion script should be assumed capable of exfiltrating every credential in the vault.** The mitigations are upstream of the vault: script provenance, the outbound domain allowlist (a stolen key still needs somewhere to go, and sandboxed scripts can only reach allowlisted hosts through the guarded verbs), and storing only low-blast-radius keys (free-tier data API keys, not bank credentials). If you need credentials protected from the application itself, you need an external secret manager and a different threat model.

### 2.3 Outbound network controls for scripts

**What they are.** Sandboxed scripts get a `BudgetAwareRequests` proxy instead of the real `requests` module. Every guarded call is checked against `allowed_api_domains`, a regex allowlist over hostnames in `global_settings.yaml` (empty list denies by default; over-long patterns are rejected as ReDoS-shaped, `scheduled_input_engine/cache.py`). Per-execution budgets cap HTTP request count, response size, wall-clock time, and output rows, scaled by cron cadence.

**What they do.** They keep honest scripts polite (the budgets exist mostly as API etiquette and runaway protection) and they narrow the exfiltration surface for sandboxed scripts using the guarded verbs.

**What they do not do.** The `Session` bypass in 2.1 applies. For `unrestricted` scripts the allowlist is advisory only: full `__builtins__` means `urllib`, `http.client`, or a raw socket is one import away. The engine-layer budgets (timeout, output-row cap) still apply to both tiers because they are enforced outside the script's globals, but a hostile pro-tier script does not need `requests` at all.

### 2.4 Network exposure: loopback defaults plus a token gate

**What it is.** Two independent controls, added in the 2026-07-12 hardening pass (weakness audit W11):

1. **Loopback by default.** The bare-metal server binds `127.0.0.1`, and the Docker compose file maps the host port as `${BIND_ADDR:-127.0.0.1}:${PORT:-5111}:5111` (`desktop_app/docker-compose.yml`). A default install is not reachable from the LAN at all. LAN exposure requires the explicit `BIND_ADDR` opt-in.
2. **An access-token gate on non-loopback binds.** `desktop_app/access_gate.py` implements the Jupyter model: a single generated token (`~/.speakes-query/access_token`, 0600, same out-of-repo directory as the vault master key), checked with `hmac.compare_digest` on every request via a Flask `before_request` hook. The gate activates automatically whenever the bind address is not loopback, so the Docker container (which binds `0.0.0.0` internally) is always gated; `SPEAKESQUERY_AUTH=on|off` overrides in either direction for operators who front the app with their own authenticating reverse proxy. The token is accepted as a query parameter, an `X-SpeakesQuery-Token` header, a `Bearer` header, or the session cookie set after first use. `/healthz` is the single exempt path and returns liveness only. A misconfigured gate (active but tokenless) fails closed. Pinned by `tests/test_access_gate.py` (`TestResolveAuthRequired`, `TestGateEnforcement`, `test_file_mode_is_0600`, `test_wrong_token_rejected`).

Both settings must be independently loosened before an unauthenticated LAN request can reach a route. That matters because this app contains a credential vault UI and an opt-in arbitrary-code trust tier: "reachable" must never equal "code execution for anyone on the network".

**What it does not do.** The gate is a bearer token over plain HTTP. On a hostile LAN it is sniffable in transit, and the cookie is not `Secure` because there is no TLS yet (the source says so, in `access_gate.py`). It is not multi-user auth, has no roles, no rate limiting, and no lockout. It converts "open to the LAN" into "open to whoever holds the token", nothing more. Do not expose SpeakesQuery to the public internet with or without the token; if you must reach it remotely, use an SSH tunnel or a VPN such as WireGuard or Tailscale and keep the bind on loopback.

### 2.5 The unrestricted `_pro` trust tier

**What it is.** Scripts may declare `trust_level: "unrestricted"`: plain `compile()`, full `__builtins__`, no import filter (`executor.py::_build_unrestricted_globals`). 22 of the 135 shipped scripts use it, for scipy/sklearn/rapidfuzz-grade computation the sandbox cannot express. The `_pro` suffix on the script name and its output subdirectory keeps the tier visible everywhere the script appears.

**What it is, honestly: arbitrary code execution, by design.** We could have kept pretending an ever-larger allowlist was a sandbox. Instead the tier system says the true thing: sandboxed means "hardened against accidents, assume escapable by experts" (2.1), and unrestricted means "this is code running as you, read it first". An honest trust label beats a pretend sandbox, because the pretend sandbox changes operator behavior in exactly the wrong direction. Engine-layer budgets (timeout, HTTP count on guarded verbs, output-row cap) still apply, but they are guardrails against runaway scripts, not containment of hostile ones. The authoring-side guidance, including when to refuse to escalate a script, lives in `docs/lang/09_ingestion_etiquette.md` under Trust Tiers.

### 2.6 Secrets in logs and history stores

**What it is.** Every Claude API call routes through one wrapper (`analyzers/claude_client.py::call_messages_create`), and every request/response body recorded to the forensic history store (`claude_api_history.sqlite`) first passes through `analyzers/_scrub.py::scrub_secrets`, which regex-redacts Anthropic-key-shaped strings (`sk-ant-` plus a broader `sk-` safety net) anywhere in the JSON structure, including nested message content. The batch-submit path shares the same scrubber after an audit found it did not (H-AN-7). Structured Parquet logs carry metadata only, never full payloads. The credential audit trail records key names and actions, never values. Pinned by `tests/test_claude_history.py::test_scrub_secrets_redacts_sk_ant_token` and `test_scrub_secrets_walks_nested_messages`; ingestion-side stderr scrubbing by `tests/test_feeder_fixes.py::test_stderr_env_dump_is_scrubbed_before_recording`.

**What it does not do.** This is targeted redaction, not DLP. It catches the one class of secret that plausibly gets pasted into a prompt (Anthropic keys) and env-dump-shaped leaks in captured stderr. It does not recognize arbitrary third-party token formats, passwords, or PII. If you paste a secret into a prompt or a script, assume it can land in a history store. Extend `_SECRET_PATTERNS` when adding support for new secret-shaped credentials; that is the documented convention.

## 3. Deliberately out of scope

- **Multi-user isolation.** There are no accounts, roles, or per-user data boundaries, and none are planned. The access gate (2.4) is a perimeter, not an authorization system. If two people need isolated instances, run two instances.
- **A third-party script submission pipeline.** None exists, by design. All 135 shipped library scripts are author-maintained in this repository and go through its review and CI (`tests/test_script_library.py` validates schema, sandbox compilation, and mock execution for every one). There is no marketplace, no community upload, no auto-update channel for scripts. This is the load-bearing compensation for 2.1: the sandbox does not need to contain adversaries because the pipeline does not deliver adversaries. If you personally paste untrusted code into the ingestion form, you have taken over that responsibility.
- **High availability and durability guarantees.** Single process, single machine, SQLite and Parquet on local disk. Crash safety is handled with atomic writes everywhere (`functionality/atomic_write.py`, `ParquetWriter.write_atomic`), and backup/restore is a first-class operator tool (`tools/persistence.py`, `docs/lang/13_backup_recovery.md`), but there is no replication, failover, or hosted anything.
- **Telemetry-based detection.** There is no phone-home, so there is also no fleet-wide anomaly detection. All auditing is local: the config/ingestion/system log streams under `indexes/logs/` and the Claude history store are SPQL-queryable by the operator.

## 4. Reporting a vulnerability

Report security issues through GitHub issues on this repository. There is no formal bug bounty and no dedicated security email; this is a single-maintainer project and the issue tracker is the fastest route to the person who can fix it. If a finding is sensitive enough that public disclosure before a fix would put users at risk, say so in the issue without the exploit details and a private channel will be arranged.

What makes a report actionable here: the trust tier involved (sandboxed escape claims are interesting; unrestricted "escapes" are not, per 2.5), whether the default loopback-plus-gate posture is bypassed or an opt-in was required, and a reproduction against a clean install. Findings that reduce to "the operator ran hostile code as themselves" fall inside the accepted model in section 1, but reports that show a shipped script, a default config, or a documented procedure violating this document's claims are exactly what we want to hear about.
