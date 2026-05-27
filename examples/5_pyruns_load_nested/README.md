# Nested YAML and pyruns.load()

This example uses `pyruns.load()` to read a structured YAML config. It records
both scalar final metrics and per-step time series.

Start with:

```bash
pyr train.py configs/base.yaml
```

In the UI launcher, choose `train.py` first, then choose `configs/base.yaml`
as the default YAML template. This first selection creates
`_pyruns_/train/config_default.yaml`; later direct launches can reuse it.

`configs/batch_grid.yaml` is also a concrete single-run recipe. UI batch syntax
belongs in the Generator form editor, where Pyruns expands it into concrete
task YAML files before the script reads anything.
