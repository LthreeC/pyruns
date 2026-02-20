import ast
import os
import re
import yaml
from typing import Dict, Any, Optional, Tuple

from .._config import DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME


# ===================== 快速检测（正则） ===================== #

def detect_config_source_fast(filepath: str) -> Tuple[str, Optional[str]]:
    """
    快速检测配置来源（使用正则，< 1ms）

    Returns:
        ("argparse", None)
        ("pyruns_read", "config.yaml" | None)
        ("pyruns_load", None)          ← script uses pyruns.load() only
        ("unknown", None)
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # pyruns.read("path") or pyruns.read()
    match = re.search(r'pyruns\.read\s*\(\s*(?:["\']([^"\']+)["\']\s*)?\)', content)
    if match:
        return ("pyruns_read", match.group(1))

    # pyruns.load() — auto-read mode (no explicit read required under pyr)
    if re.search(r'pyruns\.load\s*\(', content):
        return ("pyruns_load", None)

    # argparse
    if re.search(r'\.add_argument\s*\(', content):
        return ("argparse", None)

    return ("unknown", None)


# ===================== AST 解析（仅在需要时调用） ===================== #

def _extract_value(node: ast.AST) -> Any:
    """从 AST 节点提取值"""
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
    """解析 argparse 参数（仅 argparse 模式调用）"""
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    params: Dict[str, Dict[str, Any]] = {}
    
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue

        info: Dict[str, Any] = {}
        
        if node.args:
            info["name"] = _extract_value(node.args[0])

        for kw in node.keywords:
            if kw.arg:
                info[kw.arg] = _extract_value(kw.value)

        name = info.get("name")
        if isinstance(name, (list, tuple)):
            name = name[0] if name else ""
        dest = str(name).lstrip("-").replace("-", "_") if name else ""
        
        if dest:
            params[dest] = info

    return params


def argparse_params_to_dict(params: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """argparse 参数转简单字典"""
    return {key: info.get("default") for key, info in params.items()}


# ===================== 文件路径处理 ===================== #

def resolve_config_path(config_path: str, script_dir: str) -> Optional[str]:
    """解析配置文件路径"""
    # 尝试相对于脚本目录
    script_relative = os.path.join(script_dir, config_path)
    if os.path.exists(script_relative):
        return os.path.abspath(script_relative)
    
    # 尝试绝对路径
    if os.path.isabs(config_path) and os.path.exists(config_path):
        return config_path
    
    # 尝试相对于当前目录
    cwd_relative = os.path.join(os.getcwd(), config_path)
    if os.path.exists(cwd_relative):
        return os.path.abspath(cwd_relative)
    
    return None


def generate_config_file(filepath: str, params: Dict[str, Dict[str, Any]]) -> str:
    """生成配置文件"""
    file_dir = os.path.dirname(os.path.abspath(filepath))
    pyruns_dir = os.path.join(file_dir, DEFAULT_ROOT_NAME)
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
