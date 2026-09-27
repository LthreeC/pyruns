"""Temporary same-call file-stat reuse; compile against the real module globals."""
import sys

from pyruns.utils import info_io

original = info_io.validate_workspace_file
source = '''def validate_workspace_file(
    path: str,
    workspace_dir: str,
    *,
    label: str,
    _resolved_paths: dict[str, str | None] | None = None,
) -> None:
    """Reject a workspace file that aliases another path or is not a file."""

    absolute = _workspace_abspath(path)
    root = _workspace_abspath(workspace_dir)
    validate_workspace_directory(root)
    try:
        info = os.lstat(absolute)
    except (OSError, ValueError):
        info = None
    if _stat_is_link_or_reparse(info):
        raise ValueError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if not _path_is_within(absolute, root, _resolved_paths=_resolved_paths):
        raise ValueError(f"{label} resolves outside its workspace boundary: {path}")
    if info is not None and not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file: {path}")
'''
namespace = {}
exec(compile(source, __file__, 'exec'), info_io.__dict__, namespace)
candidate = namespace['validate_workspace_file']


def activate(validator):
    aliases = []
    for name, module in list(sys.modules.items()):
        if name == 'pyruns' or name.startswith('pyruns.'):
            if getattr(module, 'validate_workspace_file', None) in (original, candidate):
                module.validate_workspace_file = validator
                aliases.append(name)
    return aliases
