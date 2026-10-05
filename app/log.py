"""One JSON object per log line, so the hosting platform can search and count them.

What is never logged: the Ola key (it travels in the URL, so URLs are never
logged: httpx's own request log is switched off below), sign-in tokens, the
text someone searched for, coordinates, user ids, network addresses.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .context import request_id_var

# Extra fields a log call may attach (logger.info("...", extra={...})).
_FIELDS = (
    "event",
    "method",
    "path",
    "status",
    "duration_ms",
    "kind",
    "cache",
    "upstream",
    "attempt",
    "failure",
    "auth",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        for name in _FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # httpx logs every request's full URL at INFO, and the Ola key is a URL
    # parameter: these must never reach the log.
    for noisy in ("httpx", "httpcore", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Our middleware writes the access log (without query strings).
    logging.getLogger("uvicorn.access").disabled = True
    # uvicorn's own messages in the same JSON form, through the root handler.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
