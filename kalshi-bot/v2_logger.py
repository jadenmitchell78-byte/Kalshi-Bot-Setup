"""
v2_logger.py — Logging shim for Replit environment.
Replaces Termux-specific logging by patching builtins.print
so all modules that call v2_logger.inject_v2_logger() get
timestamped, prefixed output without any changes to their code.
"""
import builtins
import sys
from datetime import datetime

_original_print = builtins.print

def _timestamped_print(*args, **kwargs):
    ts = datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]
    file = kwargs.pop("file", sys.stdout)
    _original_print(f"[{ts}]", *args, file=file, **kwargs)

def inject_v2_logger():
    """
    Call once at the top of any module to enable timestamped console output.
    Safe to call multiple times — idempotent.
    """
    builtins.print = _timestamped_print
