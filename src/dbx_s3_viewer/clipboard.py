"""Copy text to the system clipboard.

The function uses pyperclip, which calls the clipboard of the platform:
pbcopy on macOS, the Windows clipboard API, and wl-copy, xclip or xsel on
Linux. If none of them works, it returns False, and the TUI sends the text
to the terminal instead (OSC 52), which many terminals support.
"""

from __future__ import annotations


def copy_text(text: str) -> bool:
    """Copy text. Return True if the system clipboard took it."""
    try:
        import pyperclip

        pyperclip.copy(text)
    except Exception:  # noqa: BLE001 - pyperclip raises several kinds of errors
        return False
    return True
