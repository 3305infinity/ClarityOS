"""
Main application entry point.
"""
from utils import calculate_total, format_currency
from models import get_user, create_order


def process_order(user_id, items):
    """Process an order for a user."""
    user = get_user(user_id)
    if not user:
        return {"error": "User not found"}
    
    total = calculate_total(items)
    formatted = format_currency(total)
    
    order = create_order(user_id, items, total)
    return {
        "order_id": order["id"],
        "user": user["name"],
        "total": formatted,
        "status": "confirmed"
    }


def get_order_summary(order_id):
    """Get a summary of an order."""
    total = calculate_total([])
    return {"order_id": order_id, "total": format_currency(total)}
