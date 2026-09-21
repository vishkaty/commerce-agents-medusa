"""Sessions survive a host restart. ``SqliteSessionStore`` puts the six storage
methods of upstream's ``SessionStore`` over one SQLite file, keeping the compare-and-set
write that refuses a racing writer. The first five tests mirror upstream's
``demo_common/tests/test_sessions.py`` so the durable store meets the same contract."""

from __future__ import annotations

import json

import pytest
from merchant_agent import AnalysisResult, Listing, MerchantSessionState
from shopping_agent import Product, ShoppingSessionState

from commerce_medusa.sessions import SqliteSessionStore
from demo_common import SessionConflictError, UnknownSessionError


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sessions.sqlite"


def test_start_binds_the_principal_and_mints_a_distinct_token_each_time(path):
    store = SqliteSessionStore(ShoppingSessionState, path)
    first, second = store.start("demo-user"), store.start("demo-user")
    other = store.start("demo-user-2")
    assert first.session_id != second.session_id and len(first.session_id) >= 24
    assert store.require(first.session_id) == first
    assert store.sessions_for_user("demo-user") == [first, second]
    assert store.sessions_for_user("demo-user-2") == [other]
    store.reset(first)
    with pytest.raises(UnknownSessionError):
        store.require(first.session_id)
    store.save(first)  # a reset record writes nothing back
    assert store.sessions_for_user("demo-user") == [second]


def test_save_moves_the_version_when_state_or_transcript_changed(path):
    store = SqliteSessionStore(ShoppingSessionState, path)
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "a kettle"})
    store.save(record)
    assert record.version == 2
    store.save(record)
    assert record.version == 2  # nothing new: no write
    record.state.remember_products([Product(product_id="P-1", title="Kettle", price=39.0)])
    record.messages.append({"role": "assistant", "content": "Here is one."})
    record.pending_app_events.append("Customer tapped add on Kettle (P-1).")
    store.save(record)
    assert record.version == 3
    json.dumps(store.read_state(record.session_id))
    loaded = store.require(record.session_id)
    assert loaded == record and len(loaded.messages) == 2
    loaded.messages.append({"role": "user", "content": "add it"})
    assert len(store.require(record.session_id).messages) == 2  # until saved


def test_a_turn_that_rewrote_earlier_messages_replaces_the_transcript(path):
    store = SqliteSessionStore(ShoppingSessionState, path)
    record = store.start("demo-user")
    record.messages += [
        {"role": "user", "content": "x" * 50},
        {"role": "assistant", "content": "y"},
    ]
    store.save(record)
    record.messages[0]["content"] = "[cleared]"
    record.messages.append({"role": "user", "content": "next"})
    store.save(record)
    assert store.require(record.session_id).messages[0]["content"] == "x" * 50  # appended only
    record.stored_messages = 0
    store.save(record)
    contents = [m["content"] for m in store.require(record.session_id).messages]
    assert contents == ["[cleared]", "y", "next"]


def test_the_second_writer_of_one_version_is_refused_and_writes_nothing(path):
    store = SqliteSessionStore(MerchantSessionState, path)
    started = store.start("acme")
    button, turn = store.require(started.session_id), store.require(started.session_id)
    button.pending_app_events.append("Operator approved change chg-1 from the preview card.")
    store.save(button)
    turn.state.remember_listing_record(Listing(listing_id="L-1", title="Kettle", price=39.0))
    turn.messages.append({"role": "user", "content": "raise it"})
    with pytest.raises(SessionConflictError):
        store.save(turn)
    stored = store.require(started.session_id)
    assert stored.state.read_listings == set() and stored.messages == []


def test_merchant_state_round_trips_with_its_sets_and_counters(path):
    store = SqliteSessionStore(MerchantSessionState, path)
    record = store.start("acme")
    record.state.remember_listing_record(Listing(listing_id="L-1", title="Kettle", price=39.0))
    record.state.remember_analysis(AnalysisResult(question="why flat?", headline="Flat week."))
    store.save(record)
    loaded = store.require(record.session_id)
    assert loaded.state.read_listings == {"L-1"} and loaded.state.analyses_run == 1
    assert (
        loaded.state.remember_analysis(AnalysisResult(question="and now?", headline="Up."))
        == "AN-2"
    )


# -- What the in-memory store cannot do: the point of a durable store.


def test_a_session_survives_a_new_store_over_the_same_file(path):
    before = SqliteSessionStore(ShoppingSessionState, path)
    record = before.start("demo-user")
    record.state.remember_products([Product(product_id="P-1", title="Kettle", price=39.0)])
    record.messages.append({"role": "user", "content": "a kettle"})
    record.pending_app_events.append("Order #1001 was placed.")
    before.save(record)
    del before  # the host process ends

    after = SqliteSessionStore(ShoppingSessionState, path)
    loaded = after.require(record.session_id)
    assert loaded == record
    assert loaded.state.seen_products["P-1"].title == "Kettle"
    assert loaded.messages == [{"role": "user", "content": "a kettle"}]
    assert loaded.pending_app_events == ["Order #1001 was placed."]
    assert after.sessions_for_user("demo-user") == [loaded]


def test_the_version_check_holds_across_two_processes(path):
    """Two hosts over one file: the second writer of a version is refused, exactly as in
    one process, because the check is a conditional UPDATE in the database."""
    host_a, host_b = SqliteSessionStore(ShoppingSessionState, path), None
    started = host_a.start("demo-user")
    host_b = SqliteSessionStore(ShoppingSessionState, path)
    a, b = host_a.require(started.session_id), host_b.require(started.session_id)
    a.pending_app_events.append("from a")
    host_a.save(a)
    b.pending_app_events.append("from b")
    with pytest.raises(SessionConflictError):
        host_b.save(b)
    assert host_b.require(started.session_id).pending_app_events == ["from a"]


def test_reset_removes_state_and_transcript_from_the_file(path):
    store = SqliteSessionStore(ShoppingSessionState, path)
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "hi"})
    store.save(record)
    store.reset(record)
    assert store.read_state(record.session_id) is None
    assert store.read_messages(record.session_id) == []
    assert SqliteSessionStore(ShoppingSessionState, path).session_ids_for_user("demo-user") == []
