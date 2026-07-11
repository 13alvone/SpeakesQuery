"""
Analyzer Data Models
────────────────────
Dataclasses that define the contract for the Claude API analysis layer.

These have no external dependencies and are imported by both the analyzer
and the integration hooks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AnalyzerConfig:
    """Configuration for the Claude analyzer, populated from global_settings."""

    api_key: str = ""
    model_primary: str = "claude-sonnet-4-6"
    model_triage: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 1024
    max_input_rows: int = 20
    enable_cache: bool = True
    enable_batch: bool = False
    daily_budget_cents: int = 50
    spike_threshold_for_upgrade: float = 10.0
    min_liquidity: float = 5000.0
    mv_truncate_limit: int = 5


@dataclass
class ActionableMarket:
    """A single market identified as actionable by Claude."""

    question: str = ""
    position: str = ""          # "YES" or "NO"
    confidence: float = 0.0     # 0.0 - 1.0
    reasoning: str = ""         # Single sentence
    estimated_roi: float = 0.0  # Estimated ROI percentage


@dataclass
class AnalysisResult:
    """Returned by ClaudeAnalyzer.analyze().  Always returned, never raised."""

    status: str = "skipped"             # "analyzed", "skipped", "error", "budget_exceeded"
    alert_priority: str = "LOW"         # "CRITICAL", "HIGH", "MODERATE", "LOW", "SKIP"
    summary: str = ""                   # One-sentence summary
    actionable_markets: List[ActionableMarket] = field(default_factory=list)  # Max 5
    pattern_detected: str = ""          # e.g. "correlated geopolitical spike"
    cross_reference_needed: List[str] = field(default_factory=list)  # External sources to check
    model_used: str = ""                # Which model actually handled this
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: float = 0.0             # Estimated cost of this call
    error_message: str = ""             # Populated only on status="error"
    raw_response: str = ""              # Raw JSON from Claude (for debugging)
    skip_reason: str = ""               # Populated only on status="skipped"
    filter_passed: bool = True          # True = send alert, False = suppress
    filter_answer: str = ""             # Raw yes/no answer from the filter gate
    batch_id: str = ""                  # Anthropic batch ID (batch mode only)
    batch_custom_id: str = ""           # UUID linking batch request to result


@dataclass
class UsageStats:
    """Cumulative token usage and cost for the current session/day."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_calls: int = 0
    total_cost_cents: float = 0.0
    budget_remaining_cents: float = 0.0
    last_reset_date: str = ""           # ISO date string (YYYY-MM-DD)
