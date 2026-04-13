"""Strategy code governance — validate strategy files for dangerous patterns.

Strategies are user-authored Python scripts executed inside NautilusTrader.
This service uses AST analysis to catch dangerous imports and function calls
before a strategy file is loaded into a backtest or live trading process.
"""

from __future__ import annotations

import ast
from pathlib import Path

from msai.core.logging import get_logger

log = get_logger(__name__)


class StrategyGovernanceService:
    """Validates strategy Python files for dangerous imports and patterns."""

    BLOCKED_IMPORTS: frozenset[str] = frozenset({
        "os", "subprocess", "shutil", "socket", "ctypes", "importlib",
        "webbrowser", "http.server", "xmlrpc", "ftplib", "smtplib",
        "telnetlib", "pickle",
    })

    DANGEROUS_CALLS: frozenset[str] = frozenset({
        "eval", "exec", "__import__", "compile", "globals", "locals",
        "getattr", "setattr", "delattr",
    })

    def validate_file(self, file_path: Path) -> list[str]:
        """Return list of violations. Empty list means the file is safe."""
        violations: list[str] = []
        violations.extend(self._check_syntax(file_path))
        if violations:
            return violations  # Can't do AST checks if syntax is broken
        violations.extend(self._check_imports(file_path))
        violations.extend(self._check_dangerous_patterns(file_path))
        return violations

    def _check_syntax(self, file_path: Path) -> list[str]:
        """Verify the file is valid Python."""
        try:
            source = file_path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(file_path))
            return []
        except SyntaxError as exc:
            return [f"Syntax error at line {exc.lineno}: {exc.msg}"]
        except Exception as exc:
            return [f"Cannot read file: {exc}"]

    def _check_imports(self, file_path: Path) -> list[str]:
        """Check for blocked module imports."""
        violations: list[str] = []
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_module = alias.name.split(".")[0]
                    if top_module in self.BLOCKED_IMPORTS:
                        violations.append(
                            f"Blocked import '{alias.name}' at line {node.lineno}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top_module = node.module.split(".")[0]
                    if top_module in self.BLOCKED_IMPORTS:
                        violations.append(
                            f"Blocked import from '{node.module}' at line {node.lineno}"
                        )
        return violations

    def _check_dangerous_patterns(self, file_path: Path) -> list[str]:
        """Check for dangerous function calls like eval(), exec(), __import__()."""
        violations: list[str] = []
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name: str | None = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name and func_name in self.DANGEROUS_CALLS:
                    violations.append(
                        f"Dangerous call '{func_name}()' at line {node.lineno}"
                    )
        return violations
