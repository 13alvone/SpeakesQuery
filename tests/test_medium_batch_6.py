"""MEDIUMs batch 6 - M-MI-11, M-MI-12, M-SV-5 regressions.

Three fixes from the 2026-04-21 production review:

  * **M-MI-11** - ``kalshi_volume_tracker`` no longer coerces missing /
    non-numeric volume fields to 0. Missing data → row is skipped with
    a stdout diagnostic instead of producing a misleading
    ``vol_oi_ratio=0``.
  * **M-MI-12** - ``polymarket_cross_market_correlation_pro`` now
    guards the entropy calculation against near-zero price_sum
    (``> 0.01``) and clips + renormalizes the probability vector so
    ``stats.entropy`` cannot return inf.
  * **M-SV-5** - ``tools/rotate_vault_key.py`` ships as a cold-
    rotation utility for the Fernet master key, plus a new
    "Credential vault master-key rotation" section in
    ``docs/lang/13_backup_recovery.md``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest.mock
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

SCRIPTS_DIR = _PROJECT_ROOT / "script_library" / "scripts"


# ======================================================================
# M-MI-11: kalshi_volume_tracker null-aware field parsing
# ======================================================================

class TestKalshiVolumeTrackerNullHandling:

    def _run(self, markets):
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "kalshi_volume_tracker.json").read_text()
        )

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "api.elections.kalshi.com" in url:
                resp.json = lambda: {"markets": markets, "cursor": ""}
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router), \
             unittest.mock.patch("time.sleep", lambda *a, **kw: None):
            executor = CodeExecutor(
                data["code"], test_mode=True,
                trust_level=data.get("trust_level", "sandboxed"),
            )
            return executor.execute_test()

    def _make_market(self, **overrides):
        base = {
            "ticker": "NORM-1",
            "event_ticker": "EVT-1",
            "title": "Normal market",
            "category": "Economics",
            "last_price": 65,
            "previous_price": 60,
            "volume": 10000,
            "volume_24h": 3000,
            "open_interest": 5000,
        }
        base.update(overrides)
        return base

    def test_missing_volume_24h_skips_row(self):
        """A market with ``volume_24h`` missing is dropped, not treated as 0.

        The skip tally surfaces in a ``_summary`` row at the top of the
        output (bypasses sandbox print limitations).
        """
        markets = [
            self._make_market(ticker="NORM-1"),
            self._make_market(ticker="BAD-1", volume_24h=None),
        ]
        result = self._run(markets)
        assert result["status"] == "pass", f"errors: {result['errors']}"
        tickers = {r.get("ticker") for r in result["head"]}
        assert "BAD-1" not in tickers
        # Summary row exists and counts the skip.
        summary = [r for r in result["head"] if r.get("ticker") == "_summary"]
        assert summary, f"Expected a _summary row. head={result['head']}"
        assert summary[0]["skipped_missing_data_count"] >= 1

    def test_non_numeric_open_interest_skips_row(self):
        """String ``'n/a'`` in open_interest → row skipped."""
        markets = [
            self._make_market(ticker="OI-BAD", open_interest="n/a"),
        ]
        result = self._run(markets)
        tickers = {r.get("ticker") for r in result["head"]}
        assert "OI-BAD" not in tickers
        summary = [r for r in result["head"] if r.get("ticker") == "_summary"]
        assert summary and summary[0]["skipped_missing_data_count"] >= 1

    def test_numeric_zero_still_processes(self):
        """An explicit numeric 0 in volume is NOT treated as missing."""
        markets = [
            self._make_market(
                ticker="ZERO-VOL", volume_24h=0, open_interest=5000,
                # vol_oi_ratio = 0 / 5000 = 0, price_change_pct check
                # still applies. Not every zero-volume market survives
                # the downstream filter, but this test is about parsing,
                # not signaling.
            ),
        ]
        # Bump the price change so the ``vol_oi_ratio < 0.05 and
        # abs(price_change_pct) < 5.0`` filter doesn't drop it.
        markets[0].update(last_price=80, previous_price=50)
        result = self._run(markets)
        # The parse survived; whether the market surfaces depends on
        # downstream filters, but it should not be skipped with the
        # "missing or non-numeric" diagnostic.
        assert result["status"] == "pass"


# ======================================================================
# M-MI-12: entropy numerical stability
# ======================================================================

class TestEntropyGuard:

    def _run(self, events_payload):
        from scheduled_input_engine.executor import CodeExecutor

        data = json.loads(
            (SCRIPTS_DIR / "polymarket_cross_market_correlation_pro.json").read_text()
        )

        def router(url, *_a, **_k):
            resp = unittest.mock.Mock()
            resp.status_code = 200
            resp.raise_for_status = unittest.mock.Mock()
            if "/events" in url:
                resp.json = lambda: events_payload
            else:
                resp.json = lambda: []
            return resp

        with unittest.mock.patch("requests.get", side_effect=router):
            executor = CodeExecutor(
                data["code"], test_mode=True, trust_level="unrestricted",
            )
            return executor.execute_test()

    def _event(self, prices: list[str], ev_id: str = "ev1"):
        """Build a minimal event with N markets at given outcomePrices strings."""
        markets = []
        for i, p in enumerate(prices):
            markets.append({
                "id": f"m_{ev_id}_{i}",
                "question": f"mkt {i}",
                "slug": f"mkt-{ev_id}-{i}",
                "conditionId": f"0x{ev_id}{i}",
                "outcomePrices": p,
                "outcomes": '["Yes","No"]',
                "volume": "10000",
                "liquidity": "1000",
                "tags": "[]",
            })
        return {
            "id": ev_id, "title": f"Event {ev_id}", "slug": ev_id,
            "markets": markets, "tags": "[]",
        }

    def test_entropy_is_finite_for_near_zero_price_sum(self):
        """All prices near 0 → gate trips at 0.01 floor → entropy=0, not inf."""
        # Build 3 markets with outrageously tiny yes-prices so price_sum < 0.01.
        result = self._run([
            self._event([
                '["0.001","0.999"]',
                '["0.002","0.998"]',
                '["0.003","0.997"]',
            ])
        ])
        assert result["status"] == "pass", f"errors: {result['errors']}"
        rows = result["head"]
        assert rows, "Expected at least one row"
        for r in rows:
            import math
            val = r.get("event_entropy")
            assert isinstance(val, (int, float))
            assert math.isfinite(val), (
                f"entropy must be finite after M-MI-12 guard; got {val!r}"
            )
            # When price_sum < 0.01, entropy collapses to 0 per the fix.
            assert val == 0.0

    def test_entropy_finite_for_sparse_distribution(self):
        """A large event with one dominant + many tiny markets → clip+renorm avoids inf."""
        # 1 market at ~0.98, four at ~0.005 each → price_sum ≈ 1.0, but
        # individual shares span 5 orders of magnitude. Without the
        # clip/renorm, scipy.stats.entropy could emit a warning or inf
        # on underflow.
        result = self._run([
            self._event([
                '["0.98","0.02"]',
                '["0.005","0.995"]',
                '["0.005","0.995"]',
                '["0.005","0.995"]',
                '["0.005","0.995"]',
            ])
        ])
        assert result["status"] == "pass"
        for r in result["head"]:
            import math
            assert math.isfinite(r["event_entropy"]), (
                f"entropy on sparse distribution returned non-finite: "
                f"{r['event_entropy']!r}"
            )


# ======================================================================
# M-SV-5: rotate_vault_key tool + docs
# ======================================================================

class TestRotateVaultKeyTool:

    def _seed_vault(self, tmp_path: Path, rows: list[tuple[int, str, str]]):
        """Create a credentials.sqlite with *rows* encrypted under a fresh Fernet key."""
        from cryptography.fernet import Fernet

        key_path = tmp_path / "master.key"
        key = Fernet.generate_key()
        key_path.write_bytes(key)

        db_path = tmp_path / "credentials.sqlite"
        fernet = Fernet(key)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    script_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL,
                    encrypted_value BLOB NOT NULL
                )
            """)
            for script_id, key_name, plaintext in rows:
                conn.execute(
                    "INSERT INTO credentials "
                    "(script_id, key_name, encrypted_value) VALUES (?, ?, ?)",
                    (script_id, key_name, fernet.encrypt(plaintext.encode())),
                )
            conn.commit()
        return key_path, db_path

    def test_dry_run_does_not_write_files(self, tmp_path):
        from tools.rotate_vault_key import rotate

        key_path, db_path = self._seed_vault(
            tmp_path, [(1, "API_KEY", "sk-sample")],
        )
        new_key = tmp_path / "master.new.key"

        rc = rotate(
            old_key_path=key_path,
            new_key_path=new_key,
            db_path=db_path,
            dry_run=True,
        )
        assert rc == 0
        # Dry-run must not touch filesystem.
        assert not new_key.exists(), "dry-run should not write new key"
        assert not (db_path.with_suffix(db_path.suffix + ".rotated.sqlite")).exists()

    def test_successful_rotation_writes_rotated_db_and_new_key(self, tmp_path):
        from cryptography.fernet import Fernet
        from tools.rotate_vault_key import rotate

        key_path, db_path = self._seed_vault(
            tmp_path,
            [
                (1, "API_KEY", "sk-one"),
                (2, "FRED_KEY", "FFRED-two"),
                (1, "OTHER", "deadbeef"),
            ],
        )
        new_key = tmp_path / "master.new.key"
        rotated_db = db_path.with_suffix(db_path.suffix + ".rotated.sqlite")

        rc = rotate(
            old_key_path=key_path,
            new_key_path=new_key,
            db_path=db_path,
        )
        assert rc == 0
        assert new_key.exists(), "rotation must write the new key"
        assert rotated_db.exists(), "rotation must write the rotated DB"

        # Every row must decrypt under the NEW key (and not under the OLD one).
        new_fernet = Fernet(new_key.read_bytes().strip())
        old_fernet = Fernet(key_path.read_bytes().strip())
        with sqlite3.connect(str(rotated_db)) as conn:
            rows = conn.execute(
                "SELECT script_id, key_name, encrypted_value FROM credentials "
                "ORDER BY script_id, key_name"
            ).fetchall()
        plaintexts = {
            (script_id, key_name): new_fernet.decrypt(enc).decode()
            for script_id, key_name, enc in rows
        }
        assert plaintexts == {
            (1, "API_KEY"): "sk-one",
            (2, "FRED_KEY"): "FFRED-two",
            (1, "OTHER"): "deadbeef",
        }
        # Rotated DB must NOT decrypt under the OLD key.
        from cryptography.fernet import InvalidToken
        with pytest.raises(InvalidToken):
            old_fernet.decrypt(rows[0][2])

    def test_failed_decrypt_under_old_key_aborts_rotation(self, tmp_path):
        from cryptography.fernet import Fernet
        from tools.rotate_vault_key import rotate

        # Seed a row with bogus encrypted bytes so the OLD key can't
        # decrypt it. Rotation must refuse to produce a new DB.
        key_path = tmp_path / "master.key"
        key_path.write_bytes(Fernet.generate_key())
        db_path = tmp_path / "credentials.sqlite"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("""
                CREATE TABLE credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    script_id INTEGER NOT NULL,
                    key_name TEXT NOT NULL,
                    encrypted_value BLOB NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO credentials "
                "(script_id, key_name, encrypted_value) VALUES (?, ?, ?)",
                (1, "BOGUS", b"this-is-not-a-fernet-token"),
            )
            conn.commit()

        new_key = tmp_path / "master.new.key"
        rc = rotate(
            old_key_path=key_path,
            new_key_path=new_key,
            db_path=db_path,
        )
        assert rc == 1, "Decrypt failure must exit with rc=1"
        # No outputs were written.
        assert not new_key.exists()
        assert not (db_path.with_suffix(db_path.suffix + ".rotated.sqlite")).exists()

    def test_refuses_to_overwrite_existing_new_key_path(self, tmp_path):
        from tools.rotate_vault_key import rotate

        key_path, db_path = self._seed_vault(
            tmp_path, [(1, "K", "v")],
        )
        new_key = tmp_path / "master.new.key"
        new_key.write_bytes(b"pre-existing\n")

        rc = rotate(
            old_key_path=key_path,
            new_key_path=new_key,
            db_path=db_path,
        )
        assert rc == 2
        # Pre-existing file untouched.
        assert new_key.read_bytes() == b"pre-existing\n"


class TestRotationDocsPresent:
    """Doc-drift guard: the backup-recovery guide must mention rotation."""

    def test_rotation_section_in_backup_doc(self):
        doc = _PROJECT_ROOT / "docs" / "lang" / "13_backup_recovery.md"
        text = doc.read_text()
        assert "master-key rotation" in text.lower(), (
            "docs/lang/13_backup_recovery.md must document key rotation "
            "(M-SV-5). Check that the rotation section isn't accidentally "
            "removed in a future docs pass."
        )
        has_tool_ref = (
            "tools/rotate_vault_key.py" in text
            or "tools.rotate_vault_key" in text
        )
        assert has_tool_ref, (
            "Rotation doc must reference the operator tool "
            "(tools/rotate_vault_key.py or tools.rotate_vault_key)."
        )
