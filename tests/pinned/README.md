# Pinned sibling contracts

Each directory holds what review-respond's fakes and golden tests know about one sibling, pinned
to the minimum version in the interface Dependencies table:

- `PIN.json`: the tool, its version, the release tag and the commit the files were taken from.
- `schemas/`: a copy of the sibling's `schemas/cli/`, `defs.json` and the top-level schema files
  they reference, at that tag.
- `grammar.json`: the command-line grammar of the commands review-respond may call (flags,
  arity, value patterns, required flags, mutual exclusions, human-only flags), written from the
  sibling's `docs/interface.md` at that tag. The fakes in `tests/fakebin/` refuse a call that
  breaks it.

To move a pin, raise the minimum in the Dependencies table, extract the same paths from the new
tag with `git archive`, update `PIN.json`, then reread the sibling's interface for changed flags
and update `grammar.json`. `tests/test_pinned_schemas.py` fails when the table and a pin differ.
