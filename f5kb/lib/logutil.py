"""Structured-logging helpers shared by the Lambda handlers.

Every handler logs one JSON object per line to stderr. These helpers make the
error records self-explanatory for a reader (human or AI) walking the logs
top-to-bottom: exception class + message + trimmed traceback, and an optional
`hint` field telling the reader what to check next.
"""

from __future__ import annotations

import traceback


def exc_fields(e: BaseException, limit: int = 12) -> dict[str, str]:
    """err_type / err_msg / traceback fields for a structured log record.

    The traceback is trimmed to its final 4000 chars — the deepest frames are
    the ones that identify the failing line; CloudWatch charges per byte.
    """
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__, limit=limit))
    return {
        "err_type": type(e).__name__,
        "err_msg": str(e)[:800],
        "traceback": tb[-4000:],
    }
