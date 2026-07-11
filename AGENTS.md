# AGENTS.md

## Agent Coding & Contribution Standards

Welcome to the `speakesQuery` project! This document defines the **expectations and requirements for any agent (human or automated, e.g., LLM) contributing code, scripts, or documentation** to this repository.

### 1. General Principles

- All code **must prioritize security, robustness, and maintainability**.
- Code should be written for **clarity, testability, and efficiency**.
- All source, build, and artifact paths should be **explicit and never depend on environmental side effects**.
- **No hard-coded secrets or credentials** are ever to be committed.
- Agents are expected to **log activity, handle errors gracefully, and validate all inputs**.

---

### 2. Coding Style & Formatting

- **Python:**
	- Use `#!/usr/bin/env python3` as the first line of any executable script.
	- Use `argparse` for CLI arguments. All required args should be positional; optional args must use flags.
	- Prefer `sqlite3` for data persistence over CSV/text, unless otherwise required.
	- Always use `logging` (never `print`) for console output:
		- Prefix all messages:  
			[i] info, [!] warning, [x] error, [DEBUG] debug.
	- Provide **in-line comments** for all complex logic or non-trivial operations.
	- Validate all input, handle edge cases, and check error return values.
	- Avoid magic numbers; use named constants.
- **Shell/Bash:**
	- Use `#!/usr/bin/env bash` shebang.
	- All scripts must use `set -euo pipefail` for safety.
	- Always check command return values and handle failure cases.
- **C++/Rust:**
	- Use RAII and smart pointers where applicable.
	- Prefer exception-safe patterns and deterministic cleanup.
	- Avoid undefined behavior; use explicit types and error handling.

---

### 3. Workflow Requirements

- **Branches:**  
	Create a feature or bugfix branch for all work.  
	Never push directly to `main` unless part of a designated ops pipeline.
- **Commits:**  
	- Commit messages must be meaningful:  
		Summarize change, reference issues (if any), and be concise.
- **Pull Requests:**  
	- All code must pass linting and relevant test suites before PR can be merged.
	- All significant logic must include corresponding tests (unit/integration as appropriate).
	- Code reviews are required; self-merges are discouraged unless pre-approved.

---

### 4. Security Standards

- **Credentials & Secrets:**
	- Never commit secrets, tokens, or passwords to the repository.
	- Use environment variables or secret managers for any sensitive config.
- **Data Handling:**
	- All data must be validated, sanitized, and encoded as appropriate.
	- File access must use whitelisting of expected directories - **never allow path traversal.**
- **Dependencies:**
	- Pin all dependencies (in `requirements.txt`, `pyproject.toml`, etc.).
	- External dependencies must be open source and from reputable sources.
	- Periodically scan dependencies for vulnerabilities.

---

### 5. Logging & Error Handling

- **Use logging consistently and verbosely where appropriate.**
	- All logging must include context (function/module names, user input, etc.).
	- Errors should not cause silent failures. Log and exit gracefully with `[x]`-prefixed error.
- **No placeholder code.**
	- All code must be complete, robust, and runnable.
	- Remove dead or unused code before PR review.

---

### 6. Testing & Validation

- **Testing is mandatory for all new features and fixes.**
	- Use `pytest` or standard frameworks where possible.
	- Coverage for edge and failure cases is required.
	- All CI tests must pass before merge.
- **Manual test plans or usage examples** must be included for any major new script or feature.

---

### 7. Documentation

- All modules, scripts, and complex functions must have docstrings or comments explaining intent, usage, and parameters.
- All scripts must support a `--help` flag with clear usage examples.
- Documentation should be succinct but sufficient for another agent to extend or debug code without tribal knowledge.

---

### 8. Operational Safety

- **No scripts or binaries may execute arbitrary shell or system commands without explicit validation and logging.**
- Any process that modifies or deletes data must prompt for confirmation or support a dry-run mode.
- All agents must check for resource leaks, orphaned files, and abnormal exit codes.

---

### Lookup File Upload Limits

- Lookup table uploads are limited to 16 MB by default.
- Adjust this limit via `LOOKUP_MAX_FILESIZE` or `MAX_CONTENT_LENGTH` in `app.py`.

---

### 9. Contribution Checklist

Before submitting code, every agent must:

	- [ ] Confirm all new code and scripts have error handling and logging.
	- [ ] Add/Update relevant tests and ensure all tests pass.
	- [ ] Ensure no secrets or credentials are present in code or history.
	- [ ] Confirm all new scripts use argparse and have proper CLI help.
	- [ ] Update documentation and usage examples as needed.
	- [ ] Perform code linting/formatting per language conventions.
	- [ ] Validate with `python3 -m py_compile` or language-specific checkers.
	- [ ] Remove all debugging or placeholder code.

---

### 10. Communication & Collaboration

- Raise issues for all bugs, vulnerabilities, or unexpected behaviors.
- Use clear, respectful language in all code comments and commit messages.
- Collaborate via PR reviews, not direct pushes or unlogged modifications.

---

## Questions or Suggestions?

Open an issue or discuss in your pull request!  
Your contribution is valued - but must meet these standards for acceptance.

---

End of AGENTS.md
*Copy and modify this as needed. Want a sample PR template or further customization? Just say the word!*

