"""Property tests for compute_priority — the core differentiator function.

Priority (P0–P4) is computed from governance state, never set by humans or LLMs.
These tests lock every branch and boundary, so any refactor that breaks the
priority computation is caught immediately.
"""
from datetime import datetime, timedelta

import pytest

from moss.models import (
    Decision,
    GovernanceGraph,
    Update,
    compute_priority,
)

NOW = datetime(2026, 8, 7, 12, 0, 0)


def _decision(**overrides) -> Decision:
    defaults = dict(id="D1", title="test")
    defaults.update(overrides)
    return Decision(**defaults)


def _graph_with_update(decision_id: str = "D1") -> GovernanceGraph:
    g = GovernanceGraph()
    g.updates = [Update(id="U-1", decision_id=decision_id, content="progress")]
    return g


# ── P4: DONE status short-circuits everything ─────────────────────


class TestDoneShortCircuits:
    def test_done_governs_high_risk_near_deadline_still_p4(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=3),
            risk_level="HIGH",
            status="DONE",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P4"

    def test_done_no_owner_still_p4(self):
        d = _decision(status="DONE")
        assert compute_priority(d, None, NOW) == "P4"


# ── P0: Governed + near deadline + HIGH risk ───────────────────────


class TestP0:
    def test_governed_near_deadline_high_risk(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=5),
            risk_level="HIGH",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P0"

    def test_boundary_deadline_exactly_7_days(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=7),
            risk_level="HIGH",
        )
        # (deadline - now).days == 7, and 7 <= 7 → P0
        assert compute_priority(d, _graph_with_update(), NOW) == "P0"

    def test_overdue_governed_high_risk_is_p0(self):
        d = _decision(
            owner="Alice",
            deadline=NOW - timedelta(days=14),
            risk_level="HIGH",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P0"

    def test_medium_risk_near_deadline_not_p0(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=3),
            risk_level="MEDIUM",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P1"

    def test_low_risk_near_deadline_not_p0(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=3),
            risk_level="LOW",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P1"


# ── P1: Governed (all checks pass) but not P0 ──────────────────────


class TestP1:
    def test_governed_far_deadline_high_risk(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=30),
            risk_level="HIGH",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P1"

    def test_boundary_deadline_8_days_high_risk(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=8),
            risk_level="HIGH",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P1"

    def test_governed_low_risk_near_deadline(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=1),
            risk_level="LOW",
        )
        assert compute_priority(d, _graph_with_update(), NOW) == "P1"


# ── P2: has owner + has deadline, but no update ────────────────────


class TestP2:
    def test_owner_deadline_no_update(self):
        d = _decision(owner="Alice", deadline=NOW + timedelta(days=10))
        assert compute_priority(d, GovernanceGraph(), NOW) == "P2"

    def test_owner_deadline_graph_none(self):
        d = _decision(owner="Alice", deadline=NOW + timedelta(days=10))
        assert compute_priority(d, None, NOW) == "P2"

    def test_owner_deadline_update_for_different_decision(self):
        d = _decision(owner="Alice", deadline=NOW + timedelta(days=10))
        g = GovernanceGraph()
        g.updates = [Update(id="U-1", decision_id="OTHER", content="x")]
        assert compute_priority(d, g, NOW) == "P2"


# ── P3: has owner, missing deadline ────────────────────────────────


class TestP3:
    def test_owner_no_deadline(self):
        d = _decision(owner="Alice")
        assert compute_priority(d, GovernanceGraph(), NOW) == "P3"

    def test_owner_no_deadline_with_update_still_p3(self):
        d = _decision(owner="Alice")
        # Has update but no deadline → not governed → P3
        assert compute_priority(d, _graph_with_update(), NOW) == "P3"


# ── P4: no owner ───────────────────────────────────────────────────


class TestP4:
    def test_no_owner_nothing_else(self):
        d = _decision()
        assert compute_priority(d, None, NOW) == "P4"

    def test_no_owner_with_deadline(self):
        d = _decision(deadline=NOW + timedelta(days=5))
        assert compute_priority(d, None, NOW) == "P4"

    def test_no_owner_with_update(self):
        d = _decision()
        assert compute_priority(d, _graph_with_update(), NOW) == "P4"


# ── Default now parameter ──────────────────────────────────────────


class TestDefaultNow:
    def test_now_defaults_to_current_time(self):
        d = _decision(
            owner="Alice",
            deadline=datetime.now() + timedelta(days=5),
            risk_level="HIGH",
        )
        result = compute_priority(d, _graph_with_update())
        assert result == "P0"


# ── Decision.priority computed_field ───────────────────────────────


class TestComputedField:
    def test_priority_computed_field_p0(self):
        d = _decision(
            owner="Alice",
            deadline=NOW + timedelta(days=3),
            risk_level="HIGH",
        )
        # computed_field uses compute_priority(self) — graph=None → no update → P2
        assert d.priority == "P2"

    def test_priority_computed_field_p4_done(self):
        d = _decision(status="DONE")
        assert d.priority == "P4"
