"""
Alert Groups
─────────────
Multi-search Claude API dispatch for SpeakesQuery.

Collects cached results from up to ``alert_group_max_feeders`` saved
searches (default 10, configurable in Settings), serializes them into a
single prompt, dispatches to the Claude API, and delivers the response
via the existing email alert channel.
"""
