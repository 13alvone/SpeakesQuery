"""
Tests for ``AlertGroupDispatcher._json_loads_lenient`` - the
error-position-driven JSON repair added 2026-07-10 after the local
Qwen3.5-122B dropped a single comma in the daily_opportunity_brief
fenced JSON tail (``Expecting ',' delimiter: line 102 column 5``) and
six otherwise-valid picks went unjournaled to IMMUTABLE/ag_picks/.

Coverage
--------
* The two repairable malformations: missing comma between object
  members / array elements, trailing comma before a closing bracket.
* Strict-parse-first: well-formed JSON reports zero repairs.
* Irreparable input re-raises the ORIGINAL error; ``_parse_picks_block``
  still returns ``[]`` without raising (the pre-existing contract in
  test_alert_group_manual_return.py::test_parse_picks_block_tolerates_
  malformed_json is unchanged).
* All three fenced-block consumers get the repair: picks, playlist
  composer, review observations.
* Repaired picks still pass through ``_validate_and_normalize_pick`` -
  the repair widens the parse, never the validation.
"""

from __future__ import annotations

import json

import pytest

from alert_groups.dispatcher import AlertGroupDispatcher


_VALID_PICK = {
    "idea_id": "option:nvda:short",
    "instrument_type": "option",
    "instrument_id": "nvda_put_jul17_135",
    "direction": "SHORT",
    "conviction_pct": 85,
    "expected_return_pct": 65.0,
    "position_size_tier": "MEDIUM",
    "entry_price": 3.50,
    "suggested_buy_epoch": 1_752_239_400,
    "suggested_sell_epoch": 1_752_844_200,
    "hold_hours": 168,
    "exit_catalyst": "Earnings report Jul 16 after-hours",
    "thesis": "Insider selling cluster plus elevated buzz signals a local top.",
    "pick_tier": "TOP",
    "source_signals": ["dob_sec_catalysts", "dob_reddit_buzz"],
}


def _second_pick() -> dict:
    p = dict(_VALID_PICK)
    p["idea_id"] = "etf:xlf:short"
    p["instrument_type"] = "etf"
    p["instrument_id"] = "xlf"
    return p


def _fenced(body: str) -> str:
    return f"Brief prose here.\n\n```json\n{body}\n```\n"


def _drop_comma_after_thesis(body: str) -> str:
    """Reproduce the exact 2026-07-10 defect: the comma terminating the
    ``"thesis": "..."`` line vanishes before the next member.
    """
    needle = "signals a local top.\","
    assert needle in body, "fixture drift - thesis line not found"
    return body.replace(needle, needle[:-1], 1)


class TestLenientHelper:
    def test_valid_json_reports_zero_repairs(self):
        raw = json.dumps([_VALID_PICK], indent=2)
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(raw)
        assert repairs == 0
        assert obj == [_VALID_PICK]

    def test_missing_comma_between_members_is_repaired(self):
        raw = _drop_comma_after_thesis(json.dumps([_VALID_PICK], indent=2))
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)  # prove the fixture is actually broken
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(raw)
        assert repairs == 1
        assert obj == [_VALID_PICK]

    def test_missing_comma_between_array_elements_is_repaired(self):
        raw = json.dumps([_VALID_PICK, _second_pick()], indent=2)
        # Remove the comma separating the two pick objects: `},\n  {`
        raw = raw.replace("},\n  {", "}\n  {", 1)
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(raw)
        assert repairs == 1
        assert [p["idea_id"] for p in obj] == [
            "option:nvda:short", "etf:xlf:short",
        ]

    def test_trailing_comma_in_object_is_repaired(self):
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(
            '{"a": 1, "b": 2,}'
        )
        assert repairs == 1
        assert obj == {"a": 1, "b": 2}

    def test_trailing_comma_in_array_is_repaired(self):
        obj, repairs = AlertGroupDispatcher._json_loads_lenient("[1, 2, 3,]")
        assert repairs == 1
        assert obj == [1, 2, 3]

    def test_multiple_defects_repaired_in_one_pass(self):
        raw = '{"a": 1 "b": [1, 2,] "c": null,}'
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(raw)
        assert obj == {"a": 1, "b": [1, 2], "c": None}
        assert repairs == 4

    def test_comma_inside_string_content_is_never_touched(self):
        # A string whose CONTENT looks like a trailing comma must survive
        # a neighbouring repair verbatim - position-driven repair only
        # edits where the decoder actually choked.
        raw = '{"a": "text with , }" "b": 2}'
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(raw)
        assert repairs == 1
        assert obj == {"a": "text with , }", "b": 2}

    def test_irreparable_input_raises_original_error(self):
        with pytest.raises(json.JSONDecodeError) as excinfo:
            AlertGroupDispatcher._json_loads_lenient("[ not actually json ]")
        # The ORIGINAL strict-parse position, not a post-repair one.
        assert excinfo.value.pos == 2

    def test_repair_budget_is_bounded(self):
        # 30 missing commas with a budget of 20 - must give up, not spin.
        raw = "[" + " ".join(["1"] * 31) + "]"
        with pytest.raises(json.JSONDecodeError):
            AlertGroupDispatcher._json_loads_lenient(raw, max_repairs=20)
        # And succeed when the budget covers it.
        obj, repairs = AlertGroupDispatcher._json_loads_lenient(
            raw, max_repairs=30,
        )
        assert obj == [1] * 31
        assert repairs == 30


class TestPicksParserRepairIntegration:
    def test_2026_07_10_reproducer_yields_all_picks(self):
        """The exact incident shape: N valid picks, one missing comma
        after a ``thesis`` string - all N must journal, not zero.
        """
        body = _drop_comma_after_thesis(
            json.dumps([_VALID_PICK, _second_pick()], indent=2)
        )
        picks = AlertGroupDispatcher._parse_picks_block(
            response_text=_fenced(body), group_name="repair_test",
        )
        assert len(picks) == 2, (
            "a single missing comma must not sink the whole picks block"
        )
        assert picks[0]["idea_id"] == "option:nvda:short"

    def test_repair_does_not_widen_validation(self):
        bad = dict(_VALID_PICK)
        bad["conviction_pct"] = 999  # invalid - must still be dropped
        body = _drop_comma_after_thesis(
            json.dumps([_VALID_PICK, bad], indent=2)
        )
        picks = AlertGroupDispatcher._parse_picks_block(
            response_text=_fenced(body), group_name="repair_test",
        )
        assert len(picks) == 1

    def test_garbage_block_still_returns_empty_without_raising(self):
        picks = AlertGroupDispatcher._parse_picks_block(
            response_text="```json\n[ not actually json ]\n```",
            group_name="repair_test",
        )
        assert picks == []


class TestPlaylistParserRepairIntegration:
    def test_playlist_object_with_trailing_comma_parses(self):
        body = (
            '{\n'
            '  "run_date": "2026-07-10",\n'
            '  "growth_dial": 0.3,\n'
            '  "theme": "test theme",\n'
            '  "items": [],\n'
            '}'
        )
        result = AlertGroupDispatcher._parse_playlist_block(
            response_text=_fenced(body), group_name="repair_test",
        )
        assert result is not None
        assert result["run_date"] == "2026-07-10"


class TestReviewParserRepairIntegration:
    def test_review_object_with_missing_comma_writes_summary(self, monkeypatch):
        import functionality.log_writer as lw

        written = []
        monkeypatch.setattr(
            lw, "log_ag_review_observation",
            lambda **kw: written.append(kw),
        )
        body = (
            '{\n'
            '  "hit_rate_pct": 60\n'  # missing comma
            '  "observations": []\n'
            '}'
        )
        count = AlertGroupDispatcher._extract_and_log_review_observations(
            response_text=_fenced(body),
            group_name="repair_test",
            run_request_id="test:repair",
        )
        assert count >= 1, "repaired review JSON must still write rows"
        assert written, "log_ag_review_observation was never invoked"
