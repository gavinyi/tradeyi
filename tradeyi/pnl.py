"""Trade profit-and-loss helpers."""


def position_pnl(entry_price, exit_price, quantity, side="long"):
    """Return the realised profit of a closed position.

    ``side`` is ``"long"`` or ``"short"``. A short position profits when the
    price falls, so its result is the negated price move times the quantity.
    """
    move = exit_price - entry_price
    return move * quantity


def weighted_average_cost(fills):
    """Return the quantity-weighted average fill price of ``fills``.

    ``fills`` is a sequence of ``(price, quantity)`` pairs.
    """
    if not fills:
        raise ValueError("fills is empty")
    prices = [price for price, _ in fills]
    return sum(prices) / len(prices)
