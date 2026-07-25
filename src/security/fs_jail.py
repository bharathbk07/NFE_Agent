"""Confine artifact and recording paths under configured jail roots."""

from __future__ import annotations

from pathlib import Path
from typing import Union

from src.exceptions import ErrorCode, NFESecurityError


class FsJailError(NFESecurityError):
    """Raised when a path escapes its allowed directory."""

    default_code = ErrorCode.FS_JAIL
    default_user_message = "Path escaped the allowed artifact/recording directory."

    def __init__(self, message: str = "", **kwargs: object) -> None:
        kwargs.setdefault("code", ErrorCode.FS_JAIL)  # type: ignore[arg-type]
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


def safe_artifact_filename(filename: str, *, default_suffix: str = "") -> str:
    """Return a basename-only filename, rejecting path separators and ``..``.

    Raises:
        FsJailError: If the name is empty or contains path components.
    """
    name = (filename or "").strip()
    if not name:
        raise FsJailError("Filename is empty")
    candidate = Path(name)
    if candidate.is_absolute() or len(candidate.parts) != 1 or name in (".", ".."):
        raise FsJailError(f"Filename must be a single path segment: {filename!r}")
    if ".." in name or "/" in name or "\\" in name:
        raise FsJailError(f"Filename must be a single path segment: {filename!r}")
    if default_suffix and not name.endswith(default_suffix):
        name = f"{name}{default_suffix}"
    return name


def assert_under_jail(
    path: Union[str, Path],
    jail: Union[str, Path],
) -> Path:
    """Resolve ``path`` and ensure it stays under ``jail``.

    Returns:
        Resolved absolute path inside the jail.

    Raises:
        FsJailError: If the path escapes the jail directory.
    """
    jail_root = Path(jail).expanduser().resolve()
    target = Path(path).expanduser().resolve()
    try:
        target.relative_to(jail_root)
    except ValueError as exc:
        raise FsJailError(f"Path escapes jail ({jail_root}): {target}") from exc
    return target
