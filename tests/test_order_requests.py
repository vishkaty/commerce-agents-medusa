"""Order actions for the shopper (cancel, return, report a problem) as durable
requests the merchant resolves."""

from __future__ import annotations

from commerce_medusa.order_requests import OrderRequestStore


def test_requests_are_durable_and_scoped_to_their_customer(tmp_path):
    path = tmp_path / "requests.sqlite"
    store = OrderRequestStore(path)
    one = store.create(
        user_id="priya", order_id="order_1", action="cancel", item_ids=[], reason="changed my mind"
    )
    assert one.status == "requested" and one.request_id.startswith("req-")
    again = OrderRequestStore(path)
    assert [r.request_id for r in again.for_user("priya")] == [one.request_id]
    assert again.for_user("someone-else") == []
    assert [r.request_id for r in again.open()] == [one.request_id]


def test_one_open_request_per_order_and_action(tmp_path):
    store = OrderRequestStore(tmp_path / "r.sqlite")
    first = store.create(
        user_id="priya", order_id="order_1", action="return", item_ids=["a"], reason="x"
    )
    second = store.create(
        user_id="priya", order_id="order_1", action="return", item_ids=["a"], reason="y"
    )
    assert second.request_id == first.request_id, "the open request is returned, not duplicated"
    other = store.create(
        user_id="priya", order_id="order_1", action="problem", item_ids=[], reason="z"
    )
    assert other.request_id != first.request_id


def test_resolve_records_decision_and_closes(tmp_path):
    store = OrderRequestStore(tmp_path / "r.sqlite")
    request = store.create(
        user_id="priya", order_id="order_1", action="cancel", item_ids=[], reason="x"
    )
    resolved = store.resolve(request.request_id, decision="approved", by="vishal", note="done")
    assert (
        resolved.status == "approved" and resolved.resolved_by == "vishal" and resolved.resolved_at
    )
    assert store.open() == []
    assert store.get(request.request_id).note == "done"
    assert store.resolve("req-nope", decision="declined", by="v") is None
