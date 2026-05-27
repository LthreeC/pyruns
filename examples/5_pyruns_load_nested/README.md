# Nested YAML and pyruns.load()

This example uses `pyruns.load()` to read a structured YAML config. It records
both scalar final metrics and per-step time series.

Start with:

```bash
pyr train.py configs/base.yaml
```

`configs/batch_grid.yaml` is also a concrete single-run recipe. UI batch syntax
belongs in the Generator form editor, where Pyruns expands it into concrete
task YAML files before the script reads anything.
