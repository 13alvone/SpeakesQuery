# Debugging SpeakesQuery Desktop

## Two modes

| Mode | What | When |
|------|------|------|
| **Docker** (`docker compose up`) | Runs everything in a container - zero local deps | Normal use, demo, testing |
| **Local** (`server.py`) | Runs directly on your machine with a venv | Debugging with PyCharm CE |

---

## Local setup (one-time)

```bash
# From the project root (recommended - handles everything):
./setup.sh

# Or manually:
python3.12 -m venv env
source env/bin/activate
pip install -r requirements.txt
python build_custom_components.py
```

> **Requires:** Python 3.12.x, cmake, a C++ compiler (Xcode CLT on macOS).

---

## PyCharm CE configuration

1. **Open** the project root in PyCharm.

2. **Set the interpreter:**
   Settings → Project → Python Interpreter → Add Interpreter → Existing → select `env/bin/python`

3. **Create a Run/Debug configuration:**
   Run → Edit Configurations → **+** → Python

   | Field | Value |
   |-------|-------|
   | Script | `desktop_app/server.py` |
   | Working directory | Project root |
   | Python interpreter | `env/` |

4. **Set breakpoints** anywhere - `server.py`, `handlers/`, `lexers/speakesQueryListener.py`, `query_engine/`, etc.

5. **Click Debug** ▶

6. **Open** `http://localhost:5111` in your browser and run a query. Your breakpoints will hit.

---

## Optional: `.env` file

```bash
cp .env.example .env
```

Edit `.env` to change the port, configure SMTP, or add any env vars. The server loads this file automatically via `python-dotenv` on startup. Alternatively, configure settings through the in-app Settings page.

---

## Quick reference

```bash
# Activate venv
source env/bin/activate

# Run server (terminal, no debugger)
python desktop_app/server.py

# Run all services (server + query engine + ingestion engine)
./run_all.sh

# Run via Docker instead
docker compose up --build -d

# Rebuild C++ extensions after changing cpp source
python build_custom_components.py --rebuild
```
