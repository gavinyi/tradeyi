# AGENTS.md

Guidance for coding agents working in this repository.

## What this repository is

`tradeyi` is a tiny, pure-standard-library Python library of trade
profit-and-loss helpers. It exists to be small enough that a change can be
reviewed in a minute: no dependencies, no services, no build step.

This repository is also the playground used to exercise an automated coding
agent, so keep changes narrow and verifiable.

## Layout

```
tradeyi/pnl.py       # the library: profit-and-loss helpers
tests/test_pnl.py    # standard-library unittest suite
```

## Running the tests

```sh
python3 -m unittest discover -s tests -v
```

Run the whole suite before you hand work back, and paste the command and its
tail in your report.

## Constraints

- Python 3.7 or later. Standard library only; never add a dependency.
- Keep the public function names and signatures in `tradeyi/pnl.py` stable.
- Every behaviour change needs a test that fails before it and passes after it.
- Do not reformat unrelated code. One logical change per branch.
