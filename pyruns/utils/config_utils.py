import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import date, datetime, time as datetime_time
from typing import Any, Dict, List, Optional, Tuple

import yaml
from omegaconf import DictConfig, ListConfig, OmegaConf
from omegaconf._utils import get_yaml_loader

from pyruns._config import CONFIG_DEFAULT_FILENAME, CONFIG_FILENAME, MAX_CONFIG_FILE_BYTES
from pyruns.utils.info_io import _replace_with_retry, load_task_info
from pyruns.utils.sort_utils import normalize_task_search_text, sort_tasks_for_manager


# OmegaConf's loader inherits YAML 1.1 integer resolution, which would turn
# Pyruns' ``start:stop:step`` batch syntax into a sexagesimal integer.  Build a
# private loader from OmegaConf's own loader and change only its integer
# resolver.  No process-global PyYAML state is modified.
_PYRUNS_INT_PATTERN = re.compile(
    r'''^(?:[-+]?(?:0|[1-9][0-9_]*)
         |[-+]?0b[0-1_]+
         |[-+]?0o[0-7_]+
         |[-+]?0x[0-9a-fA-F_]+)$''',
    re.X,
)


def _get_pyruns_yaml_loader() -> Any:
    loader = get_yaml_loader()
    loader.yaml_implicit_resolvers = {
        key: [
            (tag, regexp)
            for tag, regexp in resolvers
            if tag != "tag:yaml.org,2002:int"
        ]
        for key, resolvers in loader.yaml_implicit_resolvers.items()
    }
    loader.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        _PYRUNS_INT_PATTERN,
        list("-+0123456789"),
    )
    return loader


def load_config_text(text: str) -> DictConfig | ListConfig:
    """Parse YAML text into an OmegaConf container using Pyruns semantics."""

    parsed = yaml.load(text, Loader=_get_pyruns_yaml_loader())
    if parsed is None:
        parsed = {}
    config = OmegaConf.create(parsed)
    if not isinstance(config, (DictConfig, ListConfig)):
        raise ValueError("Configuration root must be a mapping or list")
    return config


def to_container(value: Any, *, resolve: bool = False) -> Any:
    """Convert an OmegaConf value to safe JSON/YAML-compatible containers."""

    if isinstance(value, (DictConfig, ListConfig)):
        return OmegaConf.to_container(value, resolve=resolve, enum_to_str=True)
    return value


def _normalize_yaml_objects(value: Any) -> Any:
    """Convert Python date/time objects to stable ISO scalar strings."""

    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _normalize_yaml_objects(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, ListConfig)):
        return [_normalize_yaml_objects(item) for item in value]
    return value

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


def load_yaml(path: str) -> DictConfig:
    """Load a mapping YAML file into an OmegaConf ``DictConfig``."""
    try:
        data = load_config_text(_read_yaml_text_limited(path))
        if isinstance(data, DictConfig):
            return data
        return OmegaConf.create({})
    except Exception:
        return OmegaConf.create({})


def _read_yaml_text_limited(path: str) -> str:
    with open(path, "rb") as handle:
        raw = handle.read(MAX_CONFIG_FILE_BYTES + 1)
    if len(raw) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"YAML file is too large (max {MAX_CONFIG_FILE_BYTES} bytes): {path}"
        )
    return raw.decode("utf-8-sig")


def load_yaml_strict(path: str) -> DictConfig:
    """Load a mapping YAML file or raise a descriptive error."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    try:
        data = load_config_text(_read_yaml_text_limited(path))
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError(f"Invalid YAML in '{path}': {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"YAML file is not valid UTF-8: {path}") from exc
    if not isinstance(data, DictConfig):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def save_yaml(path: str, data: Any) -> None:
    """Atomically save an OmegaConf-compatible value as YAML."""
    container = _normalize_yaml_objects(to_container(data, resolve=False))
    if not container:
        text = "# empty config\n"
    else:
        text = OmegaConf.to_yaml(
            OmegaConf.create(container, flags={"allow_objects": True}),
            resolve=False,
        )
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_CONFIG_FILE_BYTES:
        raise ValueError(
            f"YAML file is too large (max {MAX_CONFIG_FILE_BYTES} bytes): {path}"
        )

    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_path, path)
        temp_path = ""
    finally:
        if temp_path and os.path.lexists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def parse_value(val_str: Any) -> Any:
    """Parse UI / CLI input into Python values.

    Supports both string and non-string input. Non-string values (e.g. bool
    from a switch widget) are returned as-is.
    """
    if not isinstance(val_str, str):
        return val_str
    try:
        parsed = load_config_text(f"value: {val_str}\n")
        return to_container(parsed, resolve=False)["value"]
    except Exception:
        return val_str


def flatten_dict(d: Mapping[str, Any] | DictConfig, parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
    """Flatten a nested dict using dotted keys: ``{a: {b: 1}}`` → ``{'a.b': 1}``."""
    items = []
    source_items = d.items_ex(resolve=False) if isinstance(d, DictConfig) else d.items()
    for k, v in source_items:
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, Mapping) or isinstance(v, DictConfig):
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


def get_nested(data: Mapping[str, Any], full_key: str):
    """Retrieve parent_dict, key, value for a dotted key path."""
    parts = full_key.split('.')
    d = data
    for p in parts[:-1]:
        if p not in d or not isinstance(d[p], Mapping):
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
    The default template is listed first because it is the workspace parameter source.
    Task configs then follow the same logical order as Manager and Monitor.
    """
    if not os.path.isdir(run_root):
        return {}

    options: Dict[str, str] = {}

    from pyruns._config import (
        TASKS_DIR,
        TASK_INFO_FILENAME,
    )

    # config_default.yaml is the canonical workspace template for the Generator.
    default_path = os.path.abspath(os.path.join(run_root, CONFIG_DEFAULT_FILENAME)).replace("\\", "/")
    if os.path.exists(default_path):
        options[default_path] = "config_default.yaml"

    # config.yaml inside each task subfolder
    actual_tasks_dir = os.path.join(run_root, TASKS_DIR)
    
    if os.path.isdir(actual_tasks_dir):
        try:
            task_entries: List[Dict[str, Any]] = []
            for dir_name in sorted(os.listdir(actual_tasks_dir)):
                if dir_name.startswith("."):
                    continue
                task_dir = os.path.join(actual_tasks_dir, dir_name)
                if not os.path.isdir(task_dir):
                    continue

                cfg_path = os.path.join(task_dir, CONFIG_FILENAME)
                if os.path.exists(cfg_path):
                    info = load_task_info(task_dir)
                    try:
                        fallback_mtime = os.path.getmtime(os.path.join(task_dir, TASK_INFO_FILENAME))
                    except OSError:
                        try:
                            fallback_mtime = os.path.getmtime(cfg_path)
                        except OSError:
                            fallback_mtime = 0.0

                    rel_path = os.path.join(TASKS_DIR, dir_name, CONFIG_FILENAME).replace("\\", "/")
                    task_entries.append(
                        {
                            "name": dir_name,
                            "status": info.get("status", "pending"),
                            "created_at": info.get("created_at")
                            or (
                                time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(fallback_mtime))
                                if fallback_mtime
                                else ""
                            ),
                            "start_times": info.get("start_times", []),
                            "finish_times": info.get("finish_times", []),
                            "pinned": info.get("pinned", False),
                            "task_order": info.get("task_order"),
                            "_template_path": rel_path,
                        }
                    )

            for task in sort_tasks_for_manager(task_entries):
                options[str(task["_template_path"])] = str(task["name"])
        except OSError:
            pass

    return options


def preview_config_line(cfg: Mapping[str, Any], max_items: int = 6, max_len: int = 120) -> str:
    """Build a short preview string from config values (including nested).

    Flattens the dict so nested values like model.name=resnet50 are included.
    Truncates long values and adds ellipsis when exceeding max_items or max_len.
    """
    if not isinstance(cfg, Mapping):
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


def build_config_preview_and_search_text(
    cfg: Mapping[str, Any],
    *,
    task_name: str = "",
    notes: str = "",
    max_items: int = 6,
    max_len: int = 120,
) -> Tuple[str, str]:
    """Build cached preview and normalized search text for a task config."""
    preview = preview_config_line(cfg, max_items=max_items, max_len=max_len)
    if not isinstance(cfg, Mapping):
        cfg = {}

    flat = flatten_dict(cfg)
    search_lines = [str(task_name or ""), str(notes or "")]
    for key, value in flat.items():
        if str(key).startswith("_meta"):
            continue
        search_lines.append(f"{key}: {value}")
        short_key = key.rsplit(".", 1)[-1]
        if short_key != key:
            search_lines.append(f"{short_key}: {value}")

    blob = "\n".join(search_lines)
    normalized_blob = normalize_task_search_text(blob)
    return preview, normalized_blob


def validate_config_types_against_template(
    orig_config: Mapping[str, Any],
    new_configs: List[Mapping[str, Any]],
) -> Optional[str]:
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
                
                if t_o is float and t_n is int:
                    continue  # safe coercion
                    
                if t_o is not t_n:
                    return (
                        f"输入类型错误!\n"
                        f"参数 '{k}' 原本是 {t_o.__name__}，"
                        f"但实际生成了 {t_n.__name__} 类型的 '{v}'。\n"
                        f"请检查并在生成器中重新输入纯{t_o.__name__}内容。"
                    )
    return None
