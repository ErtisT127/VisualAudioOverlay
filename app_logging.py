"""Application logging configuration shared by the GUI and capture workers."""

import logging
import os
import sys

LOGGER_NAME = "visual_audio_overlay"
LOG_ENABLED_ENV = "VISUAL_AUDIO_OVERLAY_LOG_ENABLED"
LOG_DEBUG_ENV = "VISUAL_AUDIO_OVERLAY_LOG_DEBUG"
LOG_PATH_ENV = "VISUAL_AUDIO_OVERLAY_LOG_PATH"

_configured = False
_exception_hook_installed = False


def configure_logging(
    path: str | None = None,
    enabled: bool | None = None,
    console_enabled: bool = False,
    debug_enabled: bool = False,
):
    """Configure file logging once; disabled mode performs no file I/O."""
    global _configured, _exception_hook_installed
    if _configured:
        return

    if enabled is None:
        enabled = os.environ.get(LOG_ENABLED_ENV) == "1"
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    log_level = logging.DEBUG if debug_enabled else logging.INFO
    logger.setLevel(log_level)
    warnings_logger = logging.getLogger("py.warnings")
    warnings_logger.propagate = False
    console_logger = logging.getLogger(f"{LOGGER_NAME}.console")
    console_logger.propagate = False
    logging.raiseExceptions = False

    if not enabled:
        # Keep descendants effectively disabled without constructing a handler
        # or touching the filesystem.
        logger.setLevel(logging.CRITICAL + 1)
        warnings_logger.setLevel(logging.CRITICAL + 1)
        console_logger.setLevel(logging.CRITICAL + 1)
        logging.captureWarnings(True)
        _install_exception_hook()
        _configured = True
        return

    try:
        log_path = path or os.environ.get(LOG_PATH_ENV)
        if not log_path:
            raise ValueError("log path is required when logging is enabled")
        os.environ[LOG_PATH_ENV] = log_path
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=0, encoding="utf-8"
        )
        handler.setLevel(log_level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)s "
                "[pid=%(process)d tid=%(thread)d] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        warnings_logger.addHandler(handler)
        warnings_logger.setLevel(logging.WARNING)
        logging.captureWarnings(True)
        if console_enabled:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(logging.Formatter("%(message)s"))
            console_logger.addHandler(console_handler)
            console_logger.setLevel(logging.INFO)
        else:
            console_logger.setLevel(logging.CRITICAL + 1)
    except Exception:
        # Diagnostics must never prevent the application from starting.
        logger.setLevel(logging.CRITICAL + 1)
        warnings_logger.setLevel(logging.CRITICAL + 1)
        console_logger.setLevel(logging.CRITICAL + 1)
        logging.captureWarnings(True)
    _install_exception_hook()
    _configured = True


def _install_exception_hook():
    """Route uncaught Python exceptions through the same logger."""
    global _exception_hook_installed
    if _exception_hook_installed:
        return

    def handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        logging.getLogger(f"{LOGGER_NAME}.unhandled").critical(
            "uncaught exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = handle_exception
    _exception_hook_installed = True


def get_logger(name: str):
    """Return a child logger under the application logger."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
