import logging
from pathlib import Path


def setup_logging(log_file: str = "logs/arbitrage.log", level: int = logging.INFO) -> logging.Logger:
    """Configure shared logger with file + console handlers."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("arbitrage_bot")
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(level)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger
