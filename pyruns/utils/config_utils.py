import os
import ast
import json
import itertools
import yaml
from typing import Dict, Any, List, Optional, Tuple


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
    if os.path.exists(os.path.join(tasks_dir, "config_default.yaml")):
        options["config_default.yaml"] = "config_default"

    # config.yaml inside each task subfolder (use dir name, skip task_info I/O)
    try:
        for dir_name in sorted(os.listdir(tasks_dir)):
            if dir_name.startswith("."):
                continue
            task_dir = os.path.join(tasks_dir, dir_name)
            if not os.path.isdir(task_dir):
                continue
            cfg_path = os.path.join(task_dir, "config.yaml")
            if os.path.exists(cfg_path):
                options[os.path.join(dir_name, "config.yaml")] = dir_name
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


def load_task_info(task_dir: str) -> Dict[str, Any]:
    """Load task_info.json from a task directory."""
    info_path = os.path.join(task_dir, "task_info.json")
    if not os.path.exists(info_path):
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_task_info(task_dir: str, info: Dict[str, Any]) -> None:
    """Save task_info.json to a task directory."""
    info_path = os.path.join(task_dir, "task_info.json")
    with open(info_path, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  Batch generation  —  | (product) and (|) (zip) syntax
# ═══════════════════════════════════════════════════════════════

def _parse_pipe_value(value) -> Optional[Tuple[List[str], str]]:
    """Detect pipe syntax and determine mode per-value.

    Returns None if no pipe syntax found.
    Otherwise returns (split_parts, mode):
        "product"  for bare pipes:      value1 | value2 | value3
        "zip"      for parenthesized:   (value1 | value2 | value3)
    """
    if not isinstance(value, str):
        return None
    s = value.strip()

    # Zip syntax: (xxx | yyy | zzz)
    if s.startswith("(") and s.endswith(")") and "|" in s:
        inner = s[1:-1]
        parts = [p.strip() for p in inner.split("|") if p.strip()]
        if len(parts) > 1:
            return (parts, "zip")
        return None

    # Product syntax: xxx | yyy
    if "|" in s:
        parts = [p.strip() for p in s.split("|") if p.strip()]
        if len(parts) > 1:
            return (parts, "product")

    return None


def generate_batch_configs(base_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate multiple configs with mixed product + zip params.

    Syntax (in YAML string values):
        param: val1 | val2 | val3        →  product (cartesian)
        param: (val1 | val2 | val3)      →  zip (paired, all same length)

    Total configs = product_of_product_counts × zip_length

    Example:
        lr: 0.001 | 0.01 | 0.1     →  product, 3 values
        bs: 32 | 64                 →  product, 2 values
        seed: (1 | 2 | 3)          →  zip, 3 values
        name: (a | b | c)          →  zip, 3 values (must match seed length)
        → total = 3 × 2 × 3 = 18

    Each split value is parsed back to its original type (int/float/bool/str).
    Non-pipe values are kept fixed in every config.
    A "_meta_desc" key is added to each config with a human-readable description.
    """
    flat = flatten_dict(base_config)

    product_params: Dict[str, List] = {}  # key → [typed values]
    zip_params: Dict[str, List] = {}      # key → [typed values]
    fixed: Dict[str, Any] = {}            # key → value

    for k, v in flat.items():
        parsed = _parse_pipe_value(v)
        if parsed is not None:
            values, mode = parsed
            typed_values = [parse_value(p) for p in values]
            if mode == "product":
                product_params[k] = typed_values
            else:
                zip_params[k] = typed_values
        else:
            fixed[k] = v

    if not product_params and not zip_params:
        return [base_config]

    # Validate: all zip params must have the same length
    if zip_params:
        lengths = {k: len(v) for k, v in zip_params.items()}
        unique_lens = set(lengths.values())
        if len(unique_lens) > 1:
            detail = ", ".join(f"{k}={n}" for k, n in lengths.items())
            raise ValueError(
                f"All (zip) parameters must have equal length. Got: {detail}"
            )

    # Build product combos
    if product_params:
        p_keys = list(product_params.keys())
        p_combos = list(itertools.product(*[product_params[k] for k in p_keys]))
    else:
        p_keys = []
        p_combos = [()]

    # Build zip combos
    if zip_params:
        z_keys = list(zip_params.keys())
        z_combos = list(zip(*[zip_params[k] for k in z_keys]))
    else:
        z_keys = []
        z_combos = [()]

    # Cross-join: every product combo × every zip combo
    configs: List[Dict[str, Any]] = []
    for p_combo in p_combos:
        for z_combo in z_combos:
            temp_flat = fixed.copy()
            desc_parts = []
            for k, v in zip(p_keys, p_combo):
                temp_flat[k] = v
                desc_parts.append(f"{k.split('.')[-1]}={v}")
            for k, v in zip(z_keys, z_combo):
                temp_flat[k] = v
                desc_parts.append(f"{k.split('.')[-1]}={v}")
            config = unflatten_dict(temp_flat)
            config["_meta_desc"] = ", ".join(desc_parts)
            configs.append(config)

    return configs


def count_batch_configs(base_config: Dict[str, Any]) -> int:
    """Preview how many configs would be generated (without building them).

    Returns 0 if zip params have mismatched lengths (invalid).
    """
    flat = flatten_dict(base_config)
    product_counts: List[int] = []
    zip_counts: List[int] = []

    for v in flat.values():
        parsed = _parse_pipe_value(v)
        if parsed is None:
            continue
        values, mode = parsed
        if mode == "product":
            product_counts.append(len(values))
        else:
            zip_counts.append(len(values))

    # Product total
    product_total = 1
    for c in product_counts:
        product_total *= c

    # Zip total
    zip_total = 1
    if zip_counts:
        if len(set(zip_counts)) > 1:
            return 0  # mismatched zip lengths
        zip_total = zip_counts[0]

    return product_total * zip_total


def strip_batch_pipes(config: Dict[str, Any]) -> Dict[str, Any]:
    """Strip pipe syntax, keeping only the first value from each pipe-separated field.

    Used when generating a single task — ensures config.yaml has clean typed values
    (not raw pipe strings like "0.001 | 0.01").
    """
    flat = flatten_dict(config)
    result: Dict[str, Any] = {}
    for k, v in flat.items():
        parsed = _parse_pipe_value(v)
        if parsed is not None:
            values, _ = parsed
            result[k] = parse_value(values[0])
        else:
            result[k] = v
    return unflatten_dict(result)
