import os
import ast
import yaml
from typing import Dict, Any, List, Optional, Tuple

from pyruns._config import CONFIG_DEFAULT_FILENAME, CONFIG_FILENAME


def safe_filename(name: str) -> str:
    """Sanitize a string to be safe for filenames."""
    safe = "".join([c for c in name if c.isalnum() or c in (" ", "-", "_")]).strip()
    return safe.replace(" ", "_") if safe else "config"


def list_yaml_files(config_dir: str) -> List[str]:
    """List .yaml/.yml files in a directory."""
    if not os.path.isdir(config_dir):
        return []
    files = [f for f in os.listdir(config_dir) if f.endswith((".yaml", ".yml"))]
    files.sort()
    return files


def load_yaml(path: str) -> Dict[str, Any]:
    """Load a YAML file into a dict."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_yaml(path: str, data: Dict[str, Any]) -> None:
    """Save a dict to a YAML file."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def parse_value(val_str: str) -> Any:
    """Parse string input into Python types (int, float, list, bool)."""
    try:
        return ast.literal_eval(val_str)
    except Exception:
        if val_str.lower() == "true":
            return True
        if val_str.lower() == "false":
            return False
        return val_str


def flatten_dict(d: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def unflatten_dict(d: Dict[str, Any], sep: str = '.') -> Dict[str, Any]:
    result = {}
    for k, v in d.items():
        parts = k.split(sep)
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = v
    return result


def list_template_files(tasks_dir: str) -> Dict[str, str]:
    """
    Scan a tasks directory for loadable YAML config files.
    Returns dict of {relative_path: display_name}.
    Optimized: uses directory names directly to avoid disk I/O.
    """
    if not os.path.isdir(tasks_dir):
        return {}

    options: Dict[str, str] = {}

    # config_default.yaml in tasks_dir
    if os.path.exists(os.path.join(tasks_dir, CONFIG_DEFAULT_FILENAME)):
        options[CONFIG_DEFAULT_FILENAME] = "config_default"

    # config.yaml inside each task subfolder (use dir name, skip task_info I/O)
    try:
        for dir_name in sorted(os.listdir(tasks_dir)):
            if dir_name.startswith("."):
                continue
            task_dir = os.path.join(tasks_dir, dir_name)
            if not os.path.isdir(task_dir):
                continue
            cfg_path = os.path.join(task_dir, CONFIG_FILENAME)
            if os.path.exists(cfg_path):
                options[os.path.join(dir_name, CONFIG_FILENAME)] = dir_name
    except OSError:
        pass

    return options


def preview_config_line(cfg: Dict[str, Any], max_items: int = 6, max_len: int = 120) -> str:
    """Build a short preview string from config values (including nested).

    Flattens the dict so nested values like model.name=resnet50 are included.
    Truncates long values and adds ellipsis when exceeding max_items or max_len.
    """
    if not isinstance(cfg, dict):
        return ""
    flat = flatten_dict(cfg)
    items = []
    for k, v in flat.items():
        if k.startswith("_meta"):
            continue
        # Use short key (last part of dotted path) for compactness
        short_key = k.rsplit(".", 1)[-1] if "." in k else k
        # Truncate long values
        v_str = str(v)
        if len(v_str) > 20:
            v_str = v_str[:18] + ".."
        items.append(f"{short_key}={v_str}")
        if len(items) >= max_items:
            break

    result = ", ".join(items)
    remaining = len(flat) - len(items)
    if remaining > 0:
        result += f" …+{remaining}"
    if len(result) > max_len:
        result = result[:max_len - 3] + "..."
    return result

# Backward compatibility: re-export from task_io (canonical location)
from pyruns.utils.task_io import load_task_info, save_task_info  # noqa: F401

# Backward compatibility: re-export from batch_utils (canonical location)
from pyruns.utils.batch_utils import (  # noqa: F401
    _parse_pipe_value,
    generate_batch_configs,
    count_batch_configs,
    strip_batch_pipes,
)
