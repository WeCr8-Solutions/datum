"""
ANVIL Core — Logger
"""
import logging

_loggers: dict = {}

def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]
    log = logging.getLogger(f"anvil.{name}")
    if not log.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        )
        handler.setFormatter(fmt)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
    _loggers[name] = log
    return log
