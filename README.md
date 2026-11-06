# tradeyi

A tiny, dependency-free Python library of trade profit-and-loss helpers.

```sh
python3 -m unittest discover -s tests -v
```

## API

- `position_pnl(entry_price, exit_price, quantity, side="long")` — realised
  profit of a closed position. `side` is `"long"` or `"short"`.
- `weighted_average_cost(fills)` — quantity-weighted average fill price of a
  sequence of `(price, quantity)` pairs.
