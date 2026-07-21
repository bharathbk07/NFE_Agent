"""Embed and locate the NFE k6 HTML end-of-test report."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Union

_TEMPLATE_PATH = Path(__file__).with_name("k6_report_template.js")


def load_handle_summary_js() -> str:
    """Return the ``handleSummary`` JavaScript source for embedding in scripts."""
    return _TEMPLATE_PATH.read_text(encoding="utf-8").strip() + "\n"


def report_paths_for_script(script_path: Union[str, Path]) -> Dict[str, str]:
    """Derive HTML / JSON summary paths beside a generated k6 script.

    Primary HTML path is always ``html-report.html`` next to the script
    (user-facing name). A stem-specific copy is also written by handleSummary
    when ``NFE_K6_HTML_REPORT`` points at the stem file — runner sets both via
    env to the canonical ``html-report.html``.

    Args:
        script_path: Path to the ``.js`` script under ``artifacts/k6``.

    Returns:
        Mapping with ``html`` and ``json`` absolute path strings.
    """
    path = Path(script_path).resolve()
    return {
        "html": str(path.with_name("html-report.html")),
        "json": str(path.with_name("summary.json")),
    }


def find_html_report(script_path: Union[str, Path]) -> Optional[str]:
    """Return the HTML report path when the file exists next to the script."""
    path = Path(script_path).resolve()
    paths = report_paths_for_script(path)
    for candidate in (
        Path(paths["html"]),
        path.with_name(f"{path.stem}_html-report.html"),
        Path("html-report.html"),
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    return None
