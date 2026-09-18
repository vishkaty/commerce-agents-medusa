"""The lab's order-action tools on the Agent SDK path.

Upstream's tool surface is fixed by its registry, so a deployment cannot add an action
tool through the reference; presentation extensions are the only hook. This module is
what the upstream proposal (anthropics/commerce-agents#22) would fold in: two tools, gated
like cart writes, over two optional backend methods (``request_order_action``,
``get_order_requests``).

Gate: the order must have been read this session. Upstream keeps no order provenance
of its own, but ``get_orders`` and ``get_order`` put the order's items into
``state.seen_products``, so an order whose items are all in provenance was read.
"""

from __future__ import annotations

from typing import Any

from commerce_common.execution import ToolOutcome
from shopping_agent import ShoppingSessionState
from shopping_agent.fencing import STOREFRONT_FENCE
from shopping_agent.gates import PROVENANCE_GATE

REQUEST_ORDER_ACTION = "request_order_action"
GET_ORDER_REQUESTS = "get_order_requests"

LAB_TOOL_CONTRACTS: dict[str, dict[str, Any]] = {
    REQUEST_ORDER_ACTION: {
        "description": (
            "Ask the store to cancel an order, return items from it, or look into a problem "
            "with it. Read the order with get_orders or get_order_status first. Nothing "
            "happens until the store approves the request; tell the customer it is submitted "
            "and how they will hear back. A cancellation is only possible before shipping; a "
            "return only after."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order, as returned by get_orders.",
                },
                "action": {"type": "string", "enum": ["cancel", "return", "problem"]},
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": (
                        "For a return: the product ids of the order's items to send back. "
                        "Omit to name the whole order."
                    ),
                },
                "reason": {
                    "type": "string",
                    "maxLength": 300,
                    "description": "The customer's reason, in their words, briefly.",
                },
            },
            "required": ["order_id", "action", "reason"],
        },
    },
    GET_ORDER_REQUESTS: {
        "description": (
            "The customer's open and resolved order requests (cancellations, returns, "
            "problems) with their status. Read it when the customer asks what happened to a "
            "request."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
}
LAB_TOOL_CONTRACTS.update(
    {
        "apply_promo_code": {
            "description": (
                "Apply a promotion code the customer gives you to their cart. The reply says "
                "what it took off; an unknown code is refused, tell the customer so. Never "
                "invent or guess codes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string", "maxLength": 40}},
                "required": ["code"],
            },
        },
        "remove_promo_code": {
            "description": "Remove a promotion code from the cart.",
            "input_schema": {
                "type": "object",
                "properties": {"code": {"type": "string", "maxLength": 40}},
                "required": ["code"],
            },
        },
        "choose_fulfillment": {
            "description": (
                "Set the delivery option for the cart, by the option's name as "
                "get_fulfillment_options listed it (for example 'express'). Read the options "
                "first and let the customer pick."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"option": {"type": "string", "maxLength": 80}},
                "required": ["option"],
            },
        },
        "subscribe_availability": {
            "description": (
                "Put the customer on the list to be told when a sold-out product or variant "
                "is back. Only for an id returned this session that is out of stock."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"product_id": {"type": "string"}},
                "required": ["product_id"],
            },
        },
    }
)
LAB_TOOL_NAMES = list(LAB_TOOL_CONTRACTS)


def _fenced(payload: Any, max_chars: int) -> ToolOutcome:
    return ToolOutcome(STOREFRONT_FENCE.fence_payload(payload, max_chars))


async def execute_lab_tool(
    name: str,
    args: dict[str, Any],
    *,
    backend: Any,
    session: Any,
    state: ShoppingSessionState,
    max_chars: int,
) -> ToolOutcome:
    if name in {
        "apply_promo_code",
        "remove_promo_code",
        "choose_fulfillment",
        "subscribe_availability",
    }:
        return await _cart_side_tool(
            name, args, backend=backend, session=session, state=state, max_chars=max_chars
        )
    request_action = getattr(backend, "request_order_action", None)
    list_requests = getattr(backend, "get_order_requests", None)
    if request_action is None or list_requests is None:
        return ToolOutcome.error("This store does not take order requests through the assistant.")
    if name == GET_ORDER_REQUESTS:
        rows = await list_requests(session)
        return _fenced({"requests": [r.model_dump(mode="json") for r in rows]}, max_chars)
    order_id = str(args.get("order_id") or "").strip()
    action = str(args.get("action") or "").strip()
    reason = str(args.get("reason") or "").strip()
    item_ids = [str(i) for i in (args.get("item_ids") or []) if str(i).strip()]
    if not order_id or action not in {"cancel", "return", "problem"} or not reason:
        return ToolOutcome.error(
            "order_id, action (cancel, return, problem) and reason are required."
        )
    order = await backend.get_order(session, order_id)
    if order is None:
        return ToolOutcome.error(f"{order_id} is not one of the customer's orders.")
    if any(item.product_id not in state.seen_products for item in order.items):
        return ToolOutcome.held(
            PROVENANCE_GATE,
            f"order {order_id} was not read this session. Call get_orders or get_order_status "
            "first, confirm the order with the customer, then request the action.",
        )
    try:
        request = await request_action(session, order_id, action, item_ids, reason)
    except ValueError as refused:
        return ToolOutcome.error(str(refused))
    return _fenced({"request": request.model_dump(mode="json")}, max_chars)


async def _cart_side_tool(
    name: str, args: dict[str, Any], *, backend: Any, session: Any, state: Any, max_chars: int
) -> ToolOutcome:
    """Promo codes, the delivery choice and the waitlist."""
    method = getattr(backend, name, None)
    if method is None:
        return ToolOutcome.error("This store does not offer that through the assistant.")
    try:
        if name == "apply_promo_code":
            applied = await method(session, str(args.get("code") or ""))
            return _fenced(
                {
                    "code": applied.code,
                    "discount": applied.discount,
                    "cart": applied.cart.model_dump(mode="json"),
                },
                max_chars,
            )
        if name == "remove_promo_code":
            cart = await method(session, str(args.get("code") or ""))
            return _fenced({"cart": cart.model_dump(mode="json")}, max_chars)
        if name == "choose_fulfillment":
            option = await method(session, str(args.get("option") or ""))
            return _fenced({"chosen": option.model_dump(mode="json")}, max_chars)
        product_id = str(args.get("product_id") or "").strip()
        if product_id not in state.seen_products:
            return ToolOutcome.held(
                PROVENANCE_GATE,
                f"product_id {product_id} was not returned this session; find it first.",
            )
        entry = await method(session, product_id)
        return _fenced({"waitlist": entry.model_dump(mode="json")}, max_chars)
    except ValueError as refused:
        return ToolOutcome.error(str(refused))
