# Pyruns Examples

These examples are intentionally small and quick to run. They cover the main
ways a research project usually connects to Pyruns.

## 1_argparse_script

Basic `argparse` extraction. Start with:

```bash
pyr examples/1_argparse_script/main.py
```

## 2_pyruns_config

Scripts that read task YAML through `pyruns.load()`.

```bash
pyr examples/2_pyruns_config/main1.py examples/2_pyruns_config/config1.yaml
```

## 3_hydra_script

Hydra is best launched from shell workspace tasks, because Hydra owns its own
CLI override grammar and output directory behavior.

```bash
cd examples/3_hydra_script
pyr
```

Then create a shell task such as:

```bash
python train.py model=small_net optimizer=adam train.epochs=2
```

## 4_advanced_argparse

Advanced `argparse` coverage: positional args, `nargs`, append flags,
`BooleanOptionalAction`, `store_true`, `store_false`, negative defaults,
choices, and `count`.

```bash
pyr examples/4_advanced_argparse/main.py examples/4_advanced_argparse/configs/quick.yaml
```

## 5_pyruns_load_nested

Nested YAML configs with metrics written by `pyruns.record()` and time series
written by `pyruns.track()`.

```bash
pyr examples/5_pyruns_load_nested/train.py examples/5_pyruns_load_nested/configs/base.yaml
```

## 6_shell_workspace

Copyable shell payloads for Bash, PowerShell, and cmd. This is useful when you
want Pyruns to manage commands rather than a single Python entrypoint.

```bash
cd examples/6_shell_workspace
pyr
```

## 7_multi_script_project

A small project with multiple entrypoints and config files, intended for the
launcher flow:

```bash
cd examples/7_multi_script_project
pyr ui
```
