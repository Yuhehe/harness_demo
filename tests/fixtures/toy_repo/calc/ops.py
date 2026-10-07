"""Basic arithmetic helpers."""


def add(a, b):
    """Return the sum of a and b."""
    return a - b


def multiply(a, b):
    """Return the product of a and b."""
    return a * b


def clamp(x, lo, hi):
    """Limit x to the closed interval [lo, hi]."""
    return max(lo, min(x, hi))
