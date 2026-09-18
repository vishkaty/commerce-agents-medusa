"""A durable change ledger (SQLite) with the upstream ChangeLedger's interface plus
the claim and progress records an idempotent apply needs."""

from __future__ import annotations

from merchant_agent import ActorKind, ChangeItem, ChangeKind, ChangeStatus, MerchantAgentConfig

from commerce_medusa.ledger import SqliteLedger


def stage(ledger: SqliteLedger, target: str = "prod_1") -> str:
    change = ledger.stage(
        kind=ChangeKind.INVENTORY_ACTION,
        summary="restock",
        items=[ChangeItem(target=target, field="stock", before=1, after=6)],
        actor="alice",
        actor_kind=ActorKind.AGENT,
    )
    return change.change_id


def test_changes_survive_a_new_ledger_over_the_same_file(tmp_path):
    path = tmp_path / "ledger.sqlite"
    first = SqliteLedger(MerchantAgentConfig(), path)
    change_id = stage(first)
    second = SqliteLedger(MerchantAgentConfig(), path)
    assert [c.change_id for c in second.pending()] == [change_id]
    assert second.get(change_id).created_by == "alice"
    assert stage(second) != change_id, "ids keep counting after a restart"


def test_apply_and_discard_stamp_and_move_out_of_pending(tmp_path):
    ledger = SqliteLedger(MerchantAgentConfig(), tmp_path / "l.sqlite")
    one, two = stage(ledger), stage(ledger, "prod_2")
    applied = ledger.apply(one, actor="bob")
    assert applied.status is ChangeStatus.APPLIED and applied.applied_by == "bob"
    discarded = ledger.discard(two, actor="bob", actor_kind=ActorKind.AGENT)
    assert (
        discarded.status is ChangeStatus.DISCARDED
        and discarded.discarded_by_kind is ActorKind.AGENT
    )
    assert ledger.pending() == [] and {c.change_id for c in ledger.resolved()} == {one, two}
    reopened = SqliteLedger(MerchantAgentConfig(), tmp_path / "l.sqlite")
    assert reopened.get(one).status is ChangeStatus.APPLIED


def test_claim_is_exclusive_until_released(tmp_path):
    path = tmp_path / "l.sqlite"
    ledger, other = (
        SqliteLedger(MerchantAgentConfig(), path),
        SqliteLedger(MerchantAgentConfig(), path),
    )
    change_id = stage(ledger)
    assert ledger.claim(change_id, "host-a") is True
    assert other.claim(change_id, "host-b") is False, "a second process cannot claim it"
    assert ledger.get(change_id).status is ChangeStatus.STAGED, "a claim is not an apply"
    ledger.release(change_id)
    assert other.claim(change_id, "host-b") is True
    other.apply(change_id, actor="bob")
    assert ledger.claim(change_id, "host-a") is False, "an applied change cannot be claimed"


def test_progress_records_survive_and_clear_on_apply(tmp_path):
    path = tmp_path / "l.sqlite"
    ledger = SqliteLedger(MerchantAgentConfig(), path)
    change_id = stage(ledger)
    ledger.record_progress(change_id, "item:0", {"written": True})
    ledger.record_progress(change_id, "promotion", {"id": "promo_9"})
    again = SqliteLedger(MerchantAgentConfig(), path)
    assert again.progress(change_id) == {
        "item:0": {"written": True},
        "promotion": {"id": "promo_9"},
    }
    again.apply(change_id, actor="bob")
    assert again.progress(change_id) == {
        "item:0": {"written": True},
        "promotion": {"id": "promo_9"},
    }, "progress stays as the audit of what was written"


# -- conflicts between staged changes -------------------------------------------------


def test_staging_over_a_pending_change_on_the_same_target_is_flagged(tmp_path):
    ledger = SqliteLedger(MerchantAgentConfig(), tmp_path / "l.sqlite")
    first = stage(ledger)
    second = ledger.stage(
        kind=ChangeKind.PRICE_UPDATE,
        summary="price",
        items=[ChangeItem(target="prod_1", field="price", before=10.0, after=11.0)],
        actor="bob",
    )
    assert not second.guardrail_notes, "a different field on the same target is no conflict"
    third = ledger.stage(
        kind=ChangeKind.INVENTORY_ACTION,
        summary="restock again",
        items=[ChangeItem(target="prod_1", field="stock", before=1, after=3)],
        actor="bob",
    )
    assert any(first in note and "stock" in note for note in third.guardrail_notes), third
    assert ledger.conflicts(third.change_id) == [first]
    ledger.discard(first, actor="bob")
    assert ledger.conflicts(third.change_id) == []


# -- scheduled apply and undo --------------------------------------------------------


def test_schedule_is_durable_and_lists_what_is_due(tmp_path):
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "l.sqlite"
    ledger = SqliteLedger(MerchantAgentConfig(), path)
    soon, later = stage(ledger), stage(ledger, "prod_2")
    now = datetime.now(UTC)
    ledger.schedule(soon, at=now + timedelta(minutes=5), by="alice")
    ledger.schedule(later, at=now + timedelta(days=1), by="alice")
    again = SqliteLedger(MerchantAgentConfig(), path)
    assert again.due(now) == []
    assert again.due(now + timedelta(minutes=6)) == [soon]
    assert again.due(now + timedelta(days=2)) == [soon, later]
    assert again.scheduled_at(soon) == now + timedelta(minutes=5)
    again.apply(soon, actor="scheduler")
    assert again.due(now + timedelta(days=2)) == [later], "an applied change is no longer due"
    again.unschedule(later)
    assert again.due(now + timedelta(days=2)) == []
