"""Unit tests for ``strategy_hash`` (Phase 1 task 1.12)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from msai.services.nautilus.strategy_hash import (
    StrategyFileNotFoundError,
    compute_strategy_code_hash,
    hashes_match,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestComputeStrategyCodeHash:
    def test_returns_sha256_hex(self, tmp_path: Path) -> None:
        f = tmp_path / "strat.py"
        body = b"# strategy body\nfrom nautilus_trader import Strategy\n"
        f.write_bytes(body)

        expected = hashlib.sha256(body).hexdigest()
        assert compute_strategy_code_hash(f) == expected

    def test_returns_64_lowercase_hex_chars(self, tmp_path: Path) -> None:
        f = tmp_path / "strat.py"
        f.write_bytes(b"x")

        result = compute_strategy_code_hash(f)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_different_bytes_produce_different_hashes(self, tmp_path: Path) -> None:
        """The whole point of the hash: any edit to the file changes
        the identity_signature → cold-start on next restart instead
        of warm-reloading on incompatible state."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_bytes(b"fast = 10")
        f2.write_bytes(b"fast = 20")
        assert compute_strategy_code_hash(f1) != compute_strategy_code_hash(f2)

    def test_identical_bytes_produce_identical_hashes(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        body = b"same content"
        f1.write_bytes(body)
        f2.write_bytes(body)
        assert compute_strategy_code_hash(f1) == compute_strategy_code_hash(f2)

    def test_empty_file_has_known_sha256(self, tmp_path: Path) -> None:
        """sha256 of the empty byte string is a fixed well-known value —
        verifies the chunked-read path handles zero-length files."""
        f = tmp_path / "empty.py"
        f.write_bytes(b"")
        # sha256(b"").hexdigest()
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        assert compute_strategy_code_hash(f) == expected

    def test_large_file_hash_matches_oneshot(self, tmp_path: Path) -> None:
        """Chunked read must produce the same hash as a one-shot read
        for files larger than the 8 KiB chunk size."""
        f = tmp_path / "big.py"
        body = b"abcdefgh" * 10000  # 80 KB
        f.write_bytes(body)

        expected = hashlib.sha256(body).hexdigest()
        assert compute_strategy_code_hash(f) == expected

    def test_missing_path_raises_strategy_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.py"
        with pytest.raises(StrategyFileNotFoundError) as exc_info:
            compute_strategy_code_hash(missing)
        assert exc_info.value.path == missing
        assert "not found on disk" in str(exc_info.value)

    def test_error_is_filenotfounderror_subclass(self, tmp_path: Path) -> None:
        """Existing callers that catch the stdlib exception must still
        catch this one."""
        with pytest.raises(FileNotFoundError):
            compute_strategy_code_hash(tmp_path / "missing.py")

    def test_directory_path_raises(self, tmp_path: Path) -> None:
        """A path that exists but is a directory is not a regular file."""
        with pytest.raises(StrategyFileNotFoundError):
            compute_strategy_code_hash(tmp_path)


class TestHashesMatch:
    def test_identical_strings_match(self) -> None:
        h = "a" * 64
        assert hashes_match(h, h) is True

    def test_different_strings_dont_match(self) -> None:
        assert hashes_match("a" * 64, "b" * 64) is False

    def test_different_lengths_dont_match(self) -> None:
        """Short-circuit on length so hmac.compare_digest doesn't raise."""
        assert hashes_match("abc", "abcdef") is False

    def test_empty_strings_match(self) -> None:
        assert hashes_match("", "") is True
