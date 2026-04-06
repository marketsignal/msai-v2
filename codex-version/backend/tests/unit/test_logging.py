from msai.core.logging import get_logger, setup_logging


def test_setup_logging_and_get_logger() -> None:
    setup_logging("development")
    logger = get_logger("test")
    assert logger is not None
