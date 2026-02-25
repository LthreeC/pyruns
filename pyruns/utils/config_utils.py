import os
import ast
import yaml
import re
from typing import Dict, Any, List

# Fix PyYAML parsing of scientific notation without a dot (e.g. 5e-3)
yaml_float_pattern = re.compile(
    r'''^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+]?[0-9]+)?
         |[-+]?(?:[0-9][0-9_]*)(?:[eE][-+]?[0-9]+)
         |\.[0-9_]+(?:[eE][-+]?[0-9]+)?
         |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
         |[-+]?\.(?:inf|Inf|INF)
         |\.(?:nan|NaN|NAN))$''', re.X)
yaml.SafeLoader.add_implicit_resolver(
    'tag:yaml.org,2002:float',
    yaml_float_pattern,
    list('-+0123456789.')
)

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
    """Flatten a nested dict using dotted keys: ``{a: {b: 1}}`` → ``{'a.b': 1}``."""
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def unflatten_dict(d: Dict[str, Any], sep: str = '.') -> Dict[str, Any]:
    """Reverse of ``flatten_dict``: ``{'a.b': 1}`` → ``{a: {b: 1}}``."""
    result = {}
    for k, v in d.items():
        parts = k.split(sep)
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = v
    return result


def get_nested(data: dict, full_key: str):
    """Retrieve parent_dict, key, value for a dotted key path."""
    parts = full_key.split('.')
    d = data
    for p in parts[:-1]:
        if p not in d or not isinstance(d[p], dict):
            return None, None, None
        d = d[p]
    k = parts[-1]
    if k in d:
        return d, k, d[k]
    return None, None, None


def list_template_files(run_root: str) -> Dict[str, str]:
    """
    Scan a Run Root directory for loadable YAML config files.

    Returns dict of ``{relative_path: display_name}``.
    Searches both ``tasks/<name>/config.yaml`` and ``config_default.yaml``.
    Results are sorted by task_sort_key (same order as Manager page).
    """
    if not os.path.isdir(run_root):
        return {}

    options: Dict[str, str] = {}

    from pyruns._config import CONFIG_DEFAULT_FILENAME, CONFIG_FILENAME, TASKS_DIR, TASK_INFO_FILENAME
    from pyruns.utils.info_io import load_task_info
    from pyruns.utils.sort_utils import task_sort_key

    # config.yaml inside each task subfolder
    actual_tasks_dir = os.path.join(run_root, TASKS_DIR)
    
    if os.path.isdir(actual_tasks_dir):
        try:
            task_entries = []
            for dir_name in sorted(os.listdir(actual_tasks_dir)):
                if dir_name.startswith("."):
                    continue
                task_dir = os.path.join(actual_tasks_dir, dir_name)
                if not os.path.isdir(task_dir):
                    continue
                
                cfg_path = os.path.join(task_dir, CONFIG_FILENAME)
                if os.path.exists(cfg_path):
                    display_name = dir_name
                    sort_val = (0, "")
                    try:
                        info = load_task_info(task_dir)
                        if info:
                            display_name = info.get("name", dir_name)
                            sort_val = task_sort_key(info)
                    except Exception:
                        sort_val = (0, str(os.path.getmtime(cfg_path)))
                        
                    rel_path = os.path.join(TASKS_DIR, dir_name, CONFIG_FILENAME).replace("\\", "/")
                    task_entries.append((rel_path, display_name, sort_val))
                    
            # Sort task configs using priority tuples (same as Manager)
            task_entries.sort(key=lambda x: x[2], reverse=True)
            for rel_path, display_name, _ in task_entries:
                options[rel_path] = display_name
        except OSError:
            pass

    # Append config_default.yaml at the very bottom
    default_path = os.path.abspath(os.path.join(run_root, CONFIG_DEFAULT_FILENAME)).replace("\\", "/")
    if os.path.exists(default_path):
        options[default_path] = "config_default.yaml"

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


def validate_config_types_against_template(orig_config: Dict[str, Any], new_configs: List[Dict[str, Any]]) -> str | None:
    """Ensure generated configs match the primitive types of the original template.

    Allows int → float coercion (safe widening), and permits strings
    as wildcards (any type can parse from a string input).  Returns an
    error message string if a mismatch is found, or None if fully valid.
    """
    flat_orig = flatten_dict(orig_config)
    for config in new_configs:
        flat_new = flatten_dict(config)
        for k, v in flat_new.items():
            if k in flat_orig:
                ov = flat_orig[k]
                if ov is None or isinstance(ov, str):
                    continue  # strings or null are untyped wildcards
                
                t_o = type(ov)
                t_n = type(v)
                
                if t_o == float and t_n == int:
                    continue  # safe coercion
                    
                if t_o != t_n:
                    return (
                        f"输入类型错误!\n"
                        f"参数 '{k}' 原本是 {t_o.__name__}，"
                        f"但实际生成了 {t_n.__name__} 类型的 '{v}'。\n"
                        f"请检查并在生成器中重新输入纯{t_o.__name__}内容。"
                    )
    return None
