"""
Data models and database operations.
"""
import json

# In-memory store for testing
_users = {
    1: {"id": 1, "name": "Alice", "email": "alice@example.com"},
    2: {"id": 2, "name": "Bob", "email": "bob@example.com"},
}
_orders = []
_next_order_id = 1000


def get_user(user_id):
    """Fetch a user by ID."""
    return _users.get(user_id)


def create_order(user_id, items, total):
    """Create a new order."""
    global _next_order_id
    order = {
        "id": _next_order_id,
        "user_id": user_id,
        "items": items,
        "total": total,
        "status": "pending"
    }
    _orders.append(order)
    _next_order_id += 1
    return order


def get_order(order_id):
    """Fetch an order by ID."""
    for order in _orders:
        if order["id"] == order_id:
            return order
    return None
