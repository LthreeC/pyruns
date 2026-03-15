import ast
import functools
import os
import shlex
import yaml
from typing import Dict, Any, List, Optional, Tuple

from .._config import DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME


def _cache_key(filepath: str) -> Tuple[str, int, int]:
    """Return a cache key that invalidates on file content changes."""
    try:
        stat = os.stat(filepath)
        return (os.path.abspath(filepath), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (os.path.abspath(filepath), 0, 0)


@functools.lru_cache(maxsize=128)
def _read_tree_cached(cache_key: Tuple[str, int, int]) -> Optional[ast.AST]:
    path = cache_key[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            return ast.parse(f.read(), filename=path)
    except Exception:
        return None


def detect_config_source_fast(filepath: str) -> Tuple[str, Optional[str]]:
    """Detect how a script reads its config: pyruns_load/argparse/hydra/unknown."""
    tree = _read_tree_cached(_cache_key(filepath))
    if tree is None:
        return ("unknown", None)

    pyruns_aliases = set()
    pyruns_load_aliases = set()
    hydra_aliases = set()
    hydra_main_aliases = set()
    has_pyruns_load = False
    has_argparse = False
    has_hydra = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pyruns":
                    pyruns_aliases.add(alias.asname or alias.name)
                if alias.name == "hydra" or alias.name.startswith("hydra."):
                    hydra_aliases.add(alias.asname or alias.name.split(".")[0])
                    has_hydra = True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "pyruns":
                for alias in node.names:
                    if alias.name == "load":
                        pyruns_load_aliases.add(alias.asname or alias.name)
            if mod == "hydra" or mod.startswith("hydra."):
                for alias in node.names:
                    if alias.name == "main":
                        hydra_main_aliases.add(alias.asname or alias.name)
                has_hydra = True

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in pyruns_load_aliases:
                    has_pyruns_load = True
                if node.func.id in hydra_main_aliases:
                    has_hydra = True
            elif isinstance(node.func, ast.Attribute):
                if getattr(node.func.value, "id", "") in pyruns_aliases and node.func.attr == "load":
                    has_pyruns_load = True

                if node.func.attr == "add_argument":
                    has_argparse = True
                if getattr(node.func.value, "id", "") in hydra_aliases and node.func.attr == "main":
                    has_hydra = True

    if has_pyruns_load:
        return ("pyruns_load", None)

    if has_hydra:
        return ("hydra", None)

    if has_argparse:
        return ("argparse", None)

    return ("unknown", None)


def split_cli_args(args_text: str) -> List[str]:
    """Split a multi-line CLI args string into argv tokens."""
    text = str(args_text or "")
    if not text.strip():
        return []

    normalized_parts = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith("\\"):
            s = s[:-1].rstrip()
        normalized_parts.append(s)
    normalized = " ".join(normalized_parts) if normalized_parts else text.strip()

    if _has_unbalanced_quotes(normalized):
        raise ValueError("Invalid CLI args: unmatched quotes")

    try:
        tokens = shlex.split(normalized, posix=(os.name != "nt"))
    except ValueError as exc:
        raise ValueError(f"Invalid CLI args: {exc}") from exc

    cleaned: List[str] = []
    for tok in tokens:
        s = str(tok)
        if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
            s = s[1:-1]
        cleaned.append(s)
    return cleaned


def _has_unbalanced_quotes(text: str) -> bool:
    """Return True if *text* contains unmatched single or double quotes."""
    quote = None
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch in {"'", '"'}:
            if quote is None:
                quote = ch
            elif quote == ch:
                quote = None
    return quote is not None


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
    tree = _read_tree_cached(_cache_key(filepath))
    if tree is None:
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
                info["flags"] = list(flags)
                long_flags = [f for f in flags if f.startswith("--")]
                info["name"] = long_flags[0] if long_flags else flags[0]

        for kw in node.keywords:
            if kw.arg:
                info[kw.arg] = _extract_value(kw.value)

        explicit_dest = info.get("dest")
        if explicit_dest:
            dest = str(explicit_dest).replace("-", "_")
        else:
            name = info.get("name")
            dest = str(name).lstrip("-").replace("-", "_") if name else ""
        
        if dest:
            action = str(info.get("action", "") or "")
            if "default" not in info:
                if action == "store_true":
                    info["default"] = False
                elif action == "store_false":
                    info["default"] = True
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
