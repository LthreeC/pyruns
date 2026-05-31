# Nested YAML and pyruns.load()

This example uses `pyruns.load()` to read a deeply nested YAML config. It keeps
dataset preprocessing, model blocks, optimizer settings, scheduler knobs, and
runtime choices in separate subtrees, then records both scalar final metrics and
per-step time series.

Start with:

```bash
pyr train.py configs/base.yaml
```

In the UI launcher, choose `train.py` first, then choose `configs/base.yaml`
as the default YAML template. This first selection creates
`_pyruns_/train/config_default.yaml`; later direct launches can reuse it.

`configs/batch_grid.yaml` is also a concrete single-run recipe with a different
deep parameter shape. UI batch syntax belongs in the Generator form editor,
where Pyruns expands it into concrete task YAML files before the script reads
anything.

`accelerate_train.py` shows the same idea in a more realistic training layout:
training parameters stay in `configs/accelerate.yaml`, while multi-GPU launch
controls stay in environment variables such as `CUDA_VISIBLE_DEVICES`,
`ACCEL_NPROC`, `ACCEL_MP`, `ACCEL_PORT`, `ACCEL_OFF`, and `ACCEL_DEBUG`.
It uses `torch` and `accelerate` when they are installed, and falls back to a
small pure-Python loop so the example still runs in lightweight environments.
