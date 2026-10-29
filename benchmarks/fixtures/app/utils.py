"""
Utility functions for calculations and formatting.
"""


def calculate_total(items):
    """Calculate the total price of a list of items."""
    total = 0
    for item in items:
        total += item.get("price", 0) * item.get("quantity", 1)
    return total


def format_currency(amount):
    """Format a number as USD currency string."""
    return f"${amount:,.2f}"


def apply_discount(total, discount_percent):
    """Apply a percentage discount to a total."""
    discount = total * (discount_percent / 100)
    return total - discount
