# Advanced Argparse Example

This example is designed to exercise common training-script argument patterns:

- positional arguments
- `nargs="+"` list values
- `action="append"`
- `argparse.BooleanOptionalAction`
- `store_true` and `store_false`
- `action="count"`
- negative numeric defaults
- choices
- inherited environment variables
- `pyruns.track()` and `pyruns.record()`
- `pyruns.artifact_dir()` writing `artifacts/runN/summary.json`

Start the workspace with:

```bash
pyr main.py configs/quick.yaml
```

`configs/grid.yaml` is a concrete single-run recipe with larger values. UI
batch syntax belongs in the Generator form editor, not in these runnable YAML
example files.
