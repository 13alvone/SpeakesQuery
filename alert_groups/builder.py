"""
Alert Group Payload Builder
────────────────────────────
Assembles the Claude API messages[] array from serialized search results
and inline prompt text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from alert_groups.models import SerializedResult


class PayloadBuilder:
    """Build a messages[] list for the Claude API from alert group data."""

    def build(
        self,
        group_name: str,
        results: List[SerializedResult],
        prompt_text: str,
    ) -> list:
        """
        Returns a messages[] list ready for the existing Claude API caller.

        The user's prompt_text is placed first as the instruction summary,
        followed by metadata and the serialized search result blocks.

        Raises ValueError if results is empty.
        """
        if not results:
            raise ValueError("PayloadBuilder requires at least one SerializedResult.")

        user_content = self.build_user_content(group_name, results, prompt_text)
        return [{"role": "user", "content": user_content}]

    def build_user_content(
        self,
        group_name: str,
        results: List[SerializedResult],
        prompt_text: str,
    ) -> str:
        """Render the standalone user-content string used inside the Claude
        ``messages[0].content`` slot.

        Extracted so the prompt-only delivery mode can email exactly the
        same string the API path would send, without re-implementing the
        layout. Raises ValueError if results is empty.
        """
        if not results:
            raise ValueError("PayloadBuilder requires at least one SerializedResult.")

        search_blocks = self._render_blocks(results)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        return (
            f"{prompt_text}\n\n"
            f"---\n\n"
            f"**Alert Group:** {group_name}\n"
            f"**Timestamp:** {timestamp}\n"
            f"**Searches included:** {len(results)}\n\n"
            f"{search_blocks}"
        )

    @staticmethod
    def _render_blocks(results: List[SerializedResult]) -> str:
        """Render each result as a labeled markdown block."""
        blocks = []
        for r in results:
            header = f"## Search: {r.search_name} ({r.row_count} rows, {r.format.upper()})"
            blocks.append(f"{header}\n\n```{r.format}\n{r.content}\n```")
        return "\n\n---\n\n".join(blocks)
