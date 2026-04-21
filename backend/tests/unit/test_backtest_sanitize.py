"""Tests for the backtest failure-message sanitizer."""

from __future__ import annotations

from msai.services.backtests.sanitize import sanitize_public_message


class TestSanitizePublicMessage:
    def test_strips_container_data_root(self):
        raw = "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
        assert "/app/data" not in sanitize_public_message(raw)
        assert "<DATA_ROOT>/parquet/stocks/ES" in sanitize_public_message(raw)

    def test_strips_home_paths(self):
        raw = "File not found: /Users/pablo/.secrets/token"
        assert "/Users/pablo" not in sanitize_public_message(raw)
        assert "<HOME>" in sanitize_public_message(raw)

    def test_strips_stack_trace_file_lines(self):
        raw = (
            "Traceback (most recent call last):\n"
            '  File "/app/src/msai/foo.py", line 42, in bar\n'
            '    raise ValueError("boom")\n'
            "ValueError: boom"
        )
        out = sanitize_public_message(raw)
        # Keep the final exception line; drop the trace bookkeeping.
        assert "ValueError: boom" in out
        assert 'File "/app/src/msai/foo.py", line 42' not in out

    def test_redacts_jwt_shaped_tokens(self):
        raw = "Bad Authorization header: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.abc123def"
        out = sanitize_public_message(raw)
        assert "eyJhbGc" not in out
        assert "<redacted>" in out

    def test_preserves_short_human_messages(self):
        raw = "No bars found for ES.CME in range 2025-01-02..2025-01-15"
        assert sanitize_public_message(raw) == raw  # Nothing to strip.

    def test_truncates_to_1kb(self):
        raw = "x" * 5000
        out = sanitize_public_message(raw)
        assert len(out) <= 1024

    def test_handles_none(self):
        assert sanitize_public_message(None) is None

    def test_handles_empty_string(self):
        assert sanitize_public_message("") == ""

    def test_redacts_postgres_dsn_with_credentials(self):
        # [Phase 5 P1] SQLAlchemy OperationalError often includes the DSN.
        raw = "Connection to postgresql+asyncpg://msai:supersecret@db:5432/msai failed"
        out = sanitize_public_message(raw)
        assert out is not None
        assert "supersecret" not in out
        assert "msai:" not in out
        assert "<redacted>" in out
        # Scheme is preserved so the operator can tell it's a postgres issue.
        assert "postgresql+asyncpg://" in out

    def test_redacts_redis_dsn_with_password_only(self):
        raw = "Redis connection failed: redis://:hunter2@cache:6379/0"
        out = sanitize_public_message(raw)
        assert out is not None
        assert "hunter2" not in out
        assert "<redacted>" in out

    def test_strips_syntax_error_frame_without_in_suffix(self):
        # [Phase 5 P1] SyntaxError frames have no ``in <func>`` suffix,
        # and may have a caret line underneath. Strip both.
        raw = (
            "Traceback (most recent call last):\n"
            '  File "/app/strategies/broken.py", line 12\n'
            "    def bad(:\n"
            "           ^\n"
            "SyntaxError: invalid syntax"
        )
        out = sanitize_public_message(raw)
        assert out is not None
        assert 'File "/app/strategies/broken.py"' not in out
        assert "strategies/broken.py" not in out  # path fully stripped
        assert "def bad(:" not in out  # source line stripped
        assert "SyntaxError: invalid syntax" in out  # error line preserved
