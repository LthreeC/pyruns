"""
Batch generation utilities — pipe-based product & zip syntax.

Syntax (in YAML string values):
    param: val1 | val2 | val3        →  product (cartesian)
    param: (val1 | val2 | val3)      →  zip (paired, all same length)
"""
import itertools
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Sequence, Tuple

from omegaconf import DictConfig, OmegaConf

from pyruns.utils.config_utils import ConfigPath, iter_config_fields, parse_value
from pyruns._config import BATCH_SEPARATOR, BATCH_ESCAPE, DEFAULT_BATCH_CONFIG_LIMIT
from pyruns.utils import get_logger

logger = get_logger(__name__)

def _restore_batch_fields(fields: Mapping[ConfigPath, Any]) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for path, value in fields.items():
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value
    return result


# ═══════════════════════════════════════════════════════════════
#  Pipe Parsing
# ═══════════════════════════════════════════════════════════════

def _split_by_pipe(text: str) -> List[str]:
    """Split string by BATCH_SEPARATOR, ignoring escaped instances (BATCH_ESCAPE).

    Escape handling: temporarily swap escaped separators with a null byte
    so they survive the split, then restore them in each part.
    """
    if not text:
        return []
    # Temporarily replace escaped separators with a null byte
    temp_char = "\x00"
    temp = text.replace(BATCH_ESCAPE, temp_char)
    parts = temp.split(BATCH_SEPARATOR)
    # Restore the separator in each part and strip whitespace
    return [p.replace(temp_char, BATCH_SEPARATOR).strip() for p in parts if p.strip()]

def _parse_pipe_value(value) -> Optional[Tuple[Sequence[Any], str]]:

    """Detect pipe syntax and determine expansion mode per-value.

    Returns None if no pipe syntax found.
    Otherwise returns (split_parts, mode):
        "product"  for bare pipes:      ``value1 | value2 | value3``
        "zip"      for parenthesized:   ``(value1 | value2 | value3)``

    Also supports range shorthand:
        ``(start, stop[, step])``   → product expansion
        ``start:stop[:step]``       → product expansion
    """
    if not isinstance(value, str):
        return None
    s = value.strip()

    # Zip syntax: (xxx | yyy | zzz)
    if s.startswith("(") and s.endswith(")") and BATCH_SEPARATOR in s:
        inner = s[1:-1]
        parts = _split_by_pipe(inner)
        if len(parts) > 1:
            return (parts, "zip")
        return None

    # Range syntax 1: (start, stop, step) or (start, stop)
    if s.startswith("(") and s.endswith(")") and "," in s and BATCH_SEPARATOR not in s:
        inner = s[1:-1]
        parts = [p.strip() for p in inner.split(",")]
        try:
            int_parts = [int(p) for p in parts]
            if len(int_parts) in (2, 3):
                start = int_parts[0]
                stop = int_parts[1]
                step = int_parts[2] if len(int_parts) == 3 else 1
                generated = range(start, stop, step)
                if len(generated) > 0:
                    return (generated, "product")
        except ValueError:
            pass

    # Range syntax 2: start:stop:step or start:stop
    # Guard: only match if exactly 2 or 3 colon-separated parts (avoids
    # collision with YAML time strings like "12:30:00" which have 3 parts
    # but typically contain values > 59 in stop position for real ranges).
    if ":" in s and BATCH_SEPARATOR not in s and "{" not in s and "[" not in s:
        parts = [p.strip() for p in s.split(":")]
        if len(parts) in (2, 3):
            try:
                int_parts = [int(p) for p in parts]
                start = int_parts[0]
                stop = int_parts[1]
                step = int_parts[2] if len(int_parts) == 3 else 1
                # Sanity: step must be non-zero and range must produce items
                if step == 0:
                    pass
                else:
                    generated = range(start, stop, step)
                    if len(generated) > 0:
                        logger.debug("Parsed range: %s -> %d items (start=%s)", s, len(generated), start)
                        return (generated, "product")
            except ValueError:
                pass

    # Product syntax: xxx | yyy
    parts = _split_by_pipe(s)
    if len(parts) > 1:
        return (parts, "product")

    return None


# ═══════════════════════════════════════════════════════════════
#  Batch Config Generation
# ═══════════════════════════════════════════════════════════════

def generate_batch_configs(
    base_config: Mapping[Any, Any] | DictConfig,
    *,
    max_configs: int | None = DEFAULT_BATCH_CONFIG_LIMIT,
) -> List[DictConfig]:
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
    if isinstance(base_config, DictConfig):
        normalized_config = base_config
    else:
        normalized_config = OmegaConf.create(dict(base_config))
    if not isinstance(normalized_config, DictConfig):
        raise ValueError("Batch configuration root must be a mapping")

    total_count = count_batch_configs(normalized_config)
    if max_configs is not None and total_count > int(max_configs):
        raise ValueError(
            f"Batch expansion would create {total_count} tasks; limit is {int(max_configs)}. "
            "Narrow the range or split it into smaller batches."
        )

    product_params: Dict[ConfigPath, List] = {}  # path → [typed values]
    zip_params: Dict[ConfigPath, List] = {}      # path → [typed values]
    fixed: Dict[ConfigPath, Any] = {}           # path → value

    for k, v in iter_config_fields(normalized_config, include_empty=True):
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
        return [normalized_config]

    # Validate: all zip params must have the same length
    if zip_params:
        lengths = {k: len(v) for k, v in zip_params.items()}
        unique_lens = set(lengths.values())
        if len(unique_lens) > 1:
            detail = ", ".join(f"{'.'.join(map(str, k))}={n}" for k, n in lengths.items())
            raise ValueError(
                f"All (zip) parameters must have equal length. Got: {detail}"
            )

    # Build product combos
    if product_params:
        p_keys = list(product_params.keys())
        p_combos = itertools.product(*[product_params[k] for k in p_keys])
    else:
        p_keys = []
        p_combos = [()]

    # Build zip combos
    if zip_params:
        z_keys = list(zip_params.keys())
        z_combos = list(zip(*[zip_params[k] for k in z_keys], strict=True))
    else:
        z_keys = []
        z_combos = [()]

    # Cross-join: every product combo × every zip combo
    configs: List[DictConfig] = []
    for p_combo in p_combos:
        for z_combo in z_combos:
            temp_flat = fixed.copy()
            desc_parts = []
            for k, v in zip(p_keys, p_combo, strict=True):
                temp_flat[k] = v
                desc_parts.append(f"{k[-1]}={v}")
            for k, v in zip(z_keys, z_combo, strict=True):
                temp_flat[k] = v
                desc_parts.append(f"{k[-1]}={v}")
            config = OmegaConf.create(_restore_batch_fields(temp_flat))
            if not isinstance(config, DictConfig):
                raise ValueError("Generated batch configuration root must be a mapping")
            config["_meta_desc"] = ", ".join(desc_parts)
            configs.append(config)

    return configs


def count_batch_configs(base_config: Mapping[Any, Any] | DictConfig) -> int:
    """Preview how many configs would be generated (without building them).

    Returns 0 if zip params have mismatched lengths (invalid).
    """
    product_counts: List[int] = []
    zip_counts: List[int] = []

    for _path, v in iter_config_fields(base_config):
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


def strip_batch_pipes(config: Mapping[Any, Any] | DictConfig) -> DictConfig:
    """Strip pipe syntax, keeping only the first value from each pipe-separated field.

    Used when generating a single task — ensures config.yaml has clean typed values
    (not raw pipe strings like "0.001 | 0.01").
    """
    result: Dict[ConfigPath, Any] = {}
    for k, v in iter_config_fields(config, include_empty=True):
        parsed = _parse_pipe_value(v)
        if parsed is not None:
            values, _ = parsed
            result[k] = parse_value(values[0])
        else:
            result[k] = v
    normalized = OmegaConf.create(_restore_batch_fields(result))
    if not isinstance(normalized, DictConfig):
        raise ValueError("Configuration root must be a mapping")
    return normalized
