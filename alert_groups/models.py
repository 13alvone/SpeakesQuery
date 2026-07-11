"""
Alert Group Data Models
───────────────────────
Dataclasses that define the contract for the alert group dispatch layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional


@dataclass
class SerializedResult:
    """A single search result serialized and ready for prompt embedding."""

    search_name: str
    row_count: int
    estimated_tokens: int
    format: Literal["json", "csv"]
    content: str


@dataclass
class AlertGroupRunResult:
    """Outcome of a single alert group dispatch."""

    group_name: str
    status: str = "pending"             # "success", "error", "skipped"
    searches_used: List[str] = field(default_factory=list)
    estimated_tokens: int = 0
    actual_tokens: int = 0
    cost_usd: float = 0.0
    response_text: str = ""
    error_message: str = ""
    # Per-phase timings - populated by the dispatcher at phase
    # boundaries. None means the phase never ran (rate-limited fires
    # never reach the feeder loop; dry-runs never reach Claude; runs
    # without an email_address skip the email phase). SPQL-queryable
    # via the ``alert_groups`` log category for bottleneck analysis.
    feeder_loop_ms: Optional[int] = None
    claude_call_ms: Optional[int] = None
    email_send_ms: Optional[int] = None
