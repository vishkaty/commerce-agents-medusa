import {
  authenticate,
  defineMiddlewares,
  type AuthenticatedMedusaRequest,
  type MedusaNextFunction,
  type MedusaResponse,
} from "@medusajs/framework/http"
import { Modules } from "@medusajs/framework/utils"

/**
 * Hardening. Medusa leaves `GET /store/orders/:id` unprotected by
 * design and points at this pattern to restrict it: require a signed-in customer and
 * serve only that customer's own orders. Anything else is a 404, so the id reveals
 * nothing either way. The lab's shopping adapter enforces the same rule on its side.
 */
async function requireOwnOrder(
  req: AuthenticatedMedusaRequest,
  res: MedusaResponse,
  next: MedusaNextFunction
) {
  const actorId = req.auth_context?.actor_id
  if (!actorId) {
    res.status(401).json({ type: "unauthorized", message: "Sign in to view an order" })
    return
  }
  const orderModule = req.scope.resolve(Modules.ORDER)
  try {
    const order = await orderModule.retrieveOrder(req.params.id, {
      select: ["id", "customer_id"],
    })
    if (order.customer_id !== actorId) {
      res.status(404).json({ type: "not_found", message: "Order not found" })
      return
    }
  } catch {
    res.status(404).json({ type: "not_found", message: "Order not found" })
    return
  }
  next()
}

export default defineMiddlewares({
  routes: [
    {
      matcher: "/store/orders/:id",
      methods: ["GET"],
      middlewares: [authenticate("customer", ["session", "bearer"]), requireOwnOrder],
    },
  ],
})
