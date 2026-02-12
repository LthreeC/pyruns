import os
import ast
import json
import itertools
import yaml
from typing import Dict, Any, List

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
    except:
        # Fallback for simple strings that look like literals but aren't
        if val_str.lower() == "true": return True
        if val_str.lower() == "false": return False
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
    """
    if not os.path.isdir(tasks_dir):
        return {}

    options: Dict[str, str] = {}

    # config_default.yaml in tasks_dir
    if os.path.exists(os.path.join(tasks_dir, "config_default.yaml")):
        options["config_default.yaml"] = "config_default"

    # config.yaml inside each task subfolder
    for dir_name in sorted(os.listdir(tasks_dir)):
        task_dir = os.path.join(tasks_dir, dir_name)
        task_info = load_task_info(task_dir)
        task_name = task_info.get("name", dir_name)
        
        if not os.path.isdir(task_dir):
            continue
        cfg_path = os.path.join(task_dir, "config.yaml")
        if os.path.exists(cfg_path):
            options[os.path.join(dir_name, "config.yaml")] = task_name

    return options


def preview_config_line(cfg: Dict[str, Any], max_items: int = 4) -> str:
    """Build a short preview string from config top-level simple values."""
    if not isinstance(cfg, dict):
        return ""
    items = []
    for k, v in cfg.items():
        if k.startswith("_") or isinstance(v, dict):
            continue
        items.append(f"{k}={v}")
        if len(items) >= max_items:
            break
    return ", ".join(items)


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


def generate_batch_configs(base_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate multiple configs from list-based parameters."""
    flat = flatten_dict(base_config)
    list_params = {k: v for k, v in flat.items() if isinstance(v, list) and not isinstance(v, str)}
    
    if not list_params: 
        return [base_config]

    keys, values = list(list_params.keys()), list(list_params.values())
    configs = []
    for combination in itertools.product(*values):
        temp_flat = flat.copy()
        desc_parts = []
        for k, v in zip(keys, combination):
            temp_flat[k] = v
            # Add metadata about what changed
            desc_parts.append(f"{k.split('.')[-1]}={v}")
            
        config = unflatten_dict(temp_flat)
        # Store metadata description for UI display
        config['_meta_desc'] = ", ".join(desc_parts)
        configs.append(config)
        
    return configs
