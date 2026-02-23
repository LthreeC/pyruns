import ast
import os
import yaml
from typing import Dict, Any, Optional, Tuple

from .._config import DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME


def detect_config_source_fast(filepath: str) -> Tuple[str, Optional[str]]:
    """Detect how a script reads its config: 'pyruns_read', 'pyruns_load', 'argparse', or 'unknown'."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
    except Exception:
        return ("unknown", None)

    has_pyruns_load = False
    has_argparse = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if getattr(node.func.value, "id", "") == "pyruns" and node.func.attr == "read":
                    arg_val = None
                    if node.args:
                        arg = node.args[0]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            arg_val = arg.value
                    return ("pyruns_read", arg_val)

                if getattr(node.func.value, "id", "") == "pyruns" and node.func.attr == "load":
                    has_pyruns_load = True

                if node.func.attr == "add_argument":
                    has_argparse = True

    if has_pyruns_load:
        return ("pyruns_load", None)
    
    if has_argparse:
        return ("argparse", None)

    return ("unknown", None)


def _extract_value(node: ast.AST) -> Any:
    """Extract a Python literal value from an AST node."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.List):
        return [_extract_value(x) for x in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_extract_value(x) for x in node.elts)
    if isinstance(node, ast.Dict):
        return {_extract_value(k): _extract_value(v) for k, v in zip(node.keys, node.values)}
    return None


def extract_argparse_params(filepath: str) -> Dict[str, Dict[str, Any]]:
    """Parse a script's AST to extract ``add_argument`` calls and their metadata."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=filepath)
    except Exception:
        return {}

    params: Dict[str, Dict[str, Any]] = {}
    
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue

        info: Dict[str, Any] = {}
        
        if node.args:
            flags = [_extract_value(a) for a in node.args]
            flags = [str(f) for f in flags if isinstance(f, str)]
            if flags:
                long_flags = [f for f in flags if f.startswith("--")]
                info["name"] = long_flags[0] if long_flags else flags[0]

        for kw in node.keywords:
            if kw.arg:
                info[kw.arg] = _extract_value(kw.value)

        name = info.get("name")
        dest = str(name).lstrip("-").replace("-", "_") if name else ""
        
        if dest:
            params[dest] = info

    return params


def argparse_params_to_dict(params: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Convert extracted argparse params to a simple ``{key: default}`` dict."""
    return {key: info.get("default") for key, info in params.items()}


def resolve_config_path(config_path: str, script_dir: str) -> Optional[str]:
    """Resolve a config path against script dir, absolute path, or cwd."""
    script_relative = os.path.join(script_dir, config_path)
    if os.path.exists(script_relative):
        return os.path.abspath(script_relative)
    
    if os.path.isabs(config_path) and os.path.exists(config_path):
        return config_path
    
    cwd_relative = os.path.join(os.getcwd(), config_path)
    if os.path.exists(cwd_relative):
        return os.path.abspath(cwd_relative)
    
    return None


def generate_config_file(pyruns_dir: str, filepath: str, params: Dict[str, Dict[str, Any]]) -> str:
    """Auto-generate ``config_default.yaml`` from argparse params. Returns the _pyruns_ directory."""
    os.makedirs(pyruns_dir, exist_ok=True)

    config_file = os.path.join(pyruns_dir, CONFIG_DEFAULT_FILENAME)
    
    with open(config_file, "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated for {os.path.basename(filepath)}\n\n")

        for key, info in params.items():
            default = info.get("default")
            help_text = info.get("help", "")
            
            line = yaml.safe_dump({key: default}, sort_keys=False).strip()
            
            if help_text:
                f.write(f"{line}  # {help_text}\n")
            else:
                f.write(f"{line}\n")
    
    return pyruns_dir