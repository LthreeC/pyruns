"""
Batch generation utilities — pipe-based product & zip syntax.

Syntax (in YAML string values):
    param: val1 | val2 | val3        →  product (cartesian)
    param: (val1 | val2 | val3)      →  zip (paired, all same length)
"""
import itertools
from typing import Dict, Any, List, Optional, Tuple

from pyruns.utils.config_utils import flatten_dict, unflatten_dict, parse_value


# ═══════════════════════════════════════════════════════════════
#  Pipe Parsing
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


# ═══════════════════════════════════════════════════════════════
#  Batch Config Generation
# ═══════════════════════════════════════════════════════════════

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
