"""Live check of durable sessions and the order notice against the running lab host
(:8010), the way a shopper's browser would drive it: start a session, add a tent, restart
the host, confirm the session is still there, check out, deliver the paid event for the
real Checkout Session, and ask on the next turn whether the order went through. Three SDK
turns on the Agent SDK's own login.

    .venv/bin/python scripts/e2e_session_notice.py      # reads .env; the host must be up
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from commerce_medusa.settings import LabSettings  # noqa: E402
from commerce_medusa.stripe_checkout import sign_webhook  # noqa: E402

CUSTOMER = os.environ.get("CUSTOMER_ID", "customer")

HOST = os.environ.get("LAB_HOST_URL", "http://127.0.0.1:8010")
STRIPE = os.path.expanduser("~/.local/bin/stripe")


def step(text: str) -> None:
    print(f"\n== {text}", flush=True)


def sse(body: str) -> list[dict]:
    events = []
    for block in body.strip().split("\n\n"):
        kind = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if kind:
            events.append({"type": kind, "data": data})
    return events


def chat(headers: dict[str, str], message: str) -> tuple[str, list[dict]]:
    response = httpx.post(
        f"{HOST}/api/chat", json={"message": message}, headers=headers, timeout=180
    )
    response.raise_for_status()
    events = sse(response.text)
    text = "".join(str(e["data"].get("text", "")) for e in events if e["type"] == "text_delta")
    calls = [e["data"].get("tool") for e in events if e["type"] == "tool_call"]
    print(f"   tools: {calls}\n   reply: {text[:300]}")
    return text, events


def restart_host() -> None:
    pids = subprocess.run(["lsof", "-ti", ":8010"], capture_output=True, text=True).stdout.split()
    for pid in pids:
        os.kill(int(pid), signal.SIGTERM)
    for _ in range(20):
        if not subprocess.run(["lsof", "-ti", ":8010"], capture_output=True, text=True).stdout:
            break
        time.sleep(0.5)
    log = open(Path("data") / "host.log", "ab")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "--factory",
            "commerce_medusa.host.store:default_app",
            "--port",
            "8010",
        ],
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
    )
    for _ in range(40):
        try:
            if httpx.get(f"{HOST}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("host did not come back")


def main() -> int:
    settings = LabSettings.load()
    step("start a session and add a tent")
    started = httpx.post(f"{HOST}/api/session", json={"user_id": CUSTOMER}, timeout=30).json()
    headers = {"X-Session-Id": started["session_id"]}
    print(f"   session {started['session_id'][:12]}… as {started.get('name')}")
    chat(headers, "add the 2-person backpacking tent to my cart")
    cart = httpx.get(f"{HOST}/api/cart", headers=headers, timeout=30).json()
    print(f"   cart items: {cart.get('item_count')}")
    if not cart.get("item_count"):
        print("FAIL: nothing in the cart")
        return 1

    step("restart the host; the session must survive")
    restart_host()
    after = httpx.get(f"{HOST}/api/cart", headers=headers, timeout=30)
    items = after.json().get("item_count") if after.status_code == 200 else "-"
    print(f"   GET /api/cart after restart -> {after.status_code}, items {items}")
    if after.status_code != 200 or not after.json().get("item_count"):
        print("FAIL: the session did not survive the restart")
        return 1

    step("check out through the assistant (a real Checkout Session)")
    _, events = chat(headers, "check out please")
    urls = [
        h.get("url")
        for e in events
        if e["type"] == "ui" and e["data"].get("component") == "checkout"
        for h in (e["data"].get("payload") or {}).get("handoffs", [])
    ]
    if not urls:
        print("FAIL: no checkout handoff on the card")
        return 1
    checkout_id = urls[0].rsplit("/", 1)[-1].split("#")[0]
    retrieved = json.loads(
        subprocess.run(
            [STRIPE, "checkout", "sessions", "retrieve", checkout_id],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    metadata = retrieved.get("metadata") or {}
    print(
        f"   {checkout_id}: amount_total {retrieved.get('amount_total')} "
        f"{retrieved.get('currency')}; metadata lab_session_id matches: "
        f"{metadata.get('lab_session_id') == started['session_id']}"
    )
    if metadata.get("lab_session_id") != started["session_id"]:
        print("FAIL: the Checkout Session does not carry this session's id")
        return 1

    step("deliver the paid event, signed, as Stripe would")
    event = {
        "id": f"evt_lab_{int(time.time())}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": checkout_id,
                "payment_status": "paid",
                "amount_total": retrieved["amount_total"],
                "currency": retrieved["currency"],
                "metadata": metadata,
                "customer_details": {"email": settings.lab_customer_email},
            }
        },
    }
    payload = json.dumps(event).encode()
    secret = settings.webhook_secrets[0]
    response = httpx.post(
        f"{HOST}/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, secret)},
        timeout=120,
    )
    print(f"   webhook -> {response.status_code} {response.text[:160]}")
    if response.status_code != 200 or not response.json().get("order_id"):
        print("FAIL: the order was not placed")
        return 1
    display_id = response.json().get("display_id")

    step("the next turn hears about it")
    text, events = chat(headers, "did my order go through?")
    cards = [
        json.dumps(e["data"].get("payload"))
        for e in events
        if e["type"] == "ui" and e["data"].get("component") == "order_status"
    ]
    for card in cards:
        print(f"   order card: {card[:240]}")
    in_text = str(display_id) in text or "placed" in text.lower()
    in_card = any(str(display_id) in card for card in cards)
    print(f"   order #{display_id} named in the reply: {in_text}; on an order card: {in_card}")
    return 0 if (in_text or in_card) else 1


if __name__ == "__main__":
    sys.exit(main())
