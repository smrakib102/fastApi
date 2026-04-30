import logging
from typing import Any


_logger = logging.getLogger(__name__)


def log_event(event: str, **fields: Any) -> None:
    _logger.info("oauth_audit", extra={"event": event, **fields})
