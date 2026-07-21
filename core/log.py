"""StockMinded — colored, timestamped logging for terminal output.

Provides:
  - ColoredFormatter: ANSI-colored log output with timestamps.
  - setup_logging(): wire root logger once at startup.
  - llm_log(): colored [LLM] printer that replaces raw print() calls.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime

# ── ANSI color codes ──────────────────────────────────────────────
_COLORS = {
    "DEBUG":    "\033[36m",   # cyan
    "INFO":     "\033[32m",   # green
    "WARNING":  "\033[33m",   # yellow
    "ERROR":    "\033[31m",   # red
    "CRITICAL": "\033[1;31m", # bold red
    "RESET":    "\033[0m",
    "BOLD":     "\033[1m",
    "DIM":      "\033[2m",
    "LLM":      "\033[35m",   # magenta
    "LLM_OK":   "\033[32m",   # green
    "LLM_ERR":  "\033[31m",   # red
    "LLM_WARN": "\033[33m",   # yellow
}


def _enable_ansi_on_windows() -> None:
    """Enable ANSI escape code processing on Windows 10+."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        # ENABLE_PROCESSED_OUTPUT = 0x0001
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        kernel32.SetConsoleMode(handle, 7)
    except Exception:
        pass
    try:
        # Also enable for stderr
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        kernel32.SetConsoleMode(handle, 7)
    except Exception:
        pass


class ColoredFormatter(logging.Formatter):
    """Formatter that prefixes each line with a colored timestamp + level."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt, datefmt)

    def format(self, record: logging.LogRecord) -> str:
        levelname = record.levelname
        color = _COLORS.get(levelname, _COLORS["RESET"])
        reset = _COLORS["RESET"]
        dim = _COLORS["DIM"]

        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm
        level_tag = f"{color}{levelname:8s}{reset}"
        name_tag = f"{dim}{record.name}{reset}"

        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"

        return f"{dim}{ts}{reset} {level_tag} {name_tag} │ {msg}"


def setup_logging(level: int = logging.INFO) -> None:
    """Wire root logger with ColoredFormatter and enable ANSI on Windows."""
    _enable_ansi_on_windows()

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicate output
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(ColoredFormatter())
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for name in ("urllib3", "requests", "curl_cffi", "apscheduler", "werkzeug"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ── LLM-specific colored printer ──────────────────────────────────

def llm_log(msg: str, level: str = "info") -> None:
    """Print a colored [LLM] line to stderr.

    Replaces the old _print_llm() that used raw print().
    Levels:
      'info'   → magenta [LLM]
      'ok'     → green   [LLM OK]
      'warn'   → yellow  [LLM WARN]
      'error'  → red     [LLM ERR]
    """
    _enable_ansi_on_windows()
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    dim = _COLORS["DIM"]
    reset = _COLORS["RESET"]

    if level == "ok":
        tag_color = _COLORS["LLM_OK"]
        tag = "LLM OK"
    elif level == "warn":
        tag_color = _COLORS["LLM_WARN"]
        tag = "LLM WARN"
    elif level == "error":
        tag_color = _COLORS["LLM_ERR"]
        tag = "LLM ERR"
    else:
        tag_color = _COLORS["LLM"]
        tag = "LLM"

    print(
        f"{dim}{ts}{reset} {tag_color}[{tag}]{reset} {msg}",
        file=sys.stderr,
        flush=True,
    )
