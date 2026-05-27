# Multi-Script Project

This example is for the launcher flow. It contains two Python entrypoints and
several YAML configs in a project-like layout.

```bash
cd examples/7_multi_script_project
pyr ui
```

Choose `train.py` for an argparse workspace, or choose `evaluate.py` together
with `configs/eval.yaml` for a `pyruns.load()` workspace.
