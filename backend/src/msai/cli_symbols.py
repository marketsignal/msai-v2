from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from msai.services.symbol_onboarding.manifest import parse_manifest_file

app = typer.Typer(name="symbols", help="Symbol onboarding — manifest-driven universe bootstrap.")
console = Console()


@app.command()
def onboard(
    manifest: Path = typer.Option(..., "--manifest", exists=True, resolve_path=True),
    live_qualify: bool = typer.Option(False, "--live-qualify"),
    cost_ceiling_usd: str | None = typer.Option(
        None,
        "--cost-ceiling-usd",
        help="Hard spend stop in USD; max 2 decimal places.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    from msai.cli import _api_call  # noqa: PLC0415

    parsed = parse_manifest_file(manifest)
    body: dict[str, Any] = {
        "watchlist_name": parsed.watchlist_name,
        "symbols": [
            {
                "symbol": s.symbol,
                "asset_class": s.asset_class,
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
            }
            for s in parsed.symbols
        ],
        "request_live_qualification": live_qualify,
    }
    if cost_ceiling_usd is not None:
        from decimal import Decimal, InvalidOperation  # noqa: PLC0415

        try:
            raw = Decimal(cost_ceiling_usd)
        except InvalidOperation as exc:
            raise typer.BadParameter(
                f"--cost-ceiling-usd must be a decimal number (got {cost_ceiling_usd!r})"
            ) from exc
        if raw < 0:
            raise typer.BadParameter("--cost-ceiling-usd must be non-negative.")
        # Use exponent check (NOT value comparison) — Decimal("123.450") == Decimal("123.45").
        exp = raw.as_tuple().exponent
        if isinstance(exp, int) and exp < -2:
            raise typer.BadParameter(
                "--cost-ceiling-usd supports at most 2 decimal places "
                f"(got {cost_ceiling_usd!r}, exponent={raw.as_tuple().exponent}); use e.g. 123.45."
            )
        quantized = raw.quantize(Decimal("0.01"))
        body["cost_ceiling_usd"] = str(quantized)

    if dry_run:
        response = _api_call("POST", "/api/v1/symbols/onboard/dry-run", json_body=body)
        data = response.json()
        _print_cost_estimate(data)
        raise typer.Exit(code=0)

    response = _api_call("POST", "/api/v1/symbols/onboard", json_body=body)
    data = response.json()
    console.print(f"Run queued: [bold]{data['run_id']}[/bold]")
    console.print(f"Next: [green]msai symbols status {data['run_id']} --watch[/green]")
    raise typer.Exit(code=0)


@app.command()
def status(
    run_id: str = typer.Argument(...),
    watch: bool = typer.Option(False, "--watch"),
) -> None:
    from msai.cli import _api_call  # noqa: PLC0415

    while True:
        response = _api_call("GET", f"/api/v1/symbols/onboard/{run_id}/status")
        data = response.json()
        _render_status_table(data)
        if not watch or data["status"] in {
            "completed",
            "completed_with_failures",
            "failed",
        }:
            break
        time.sleep(5)
    _exit_for_status(data["status"])


@app.command()
def repair(
    run_id: str = typer.Argument(...),
    symbols: str | None = typer.Option(None, "--symbols", help="Comma-separated."),
) -> None:
    from msai.cli import _api_call  # noqa: PLC0415

    body: dict[str, Any] = {}
    if symbols:
        body["symbols"] = [s.strip() for s in symbols.split(",") if s.strip()]
    response = _api_call("POST", f"/api/v1/symbols/onboard/{run_id}/repair", json_body=body)
    data = response.json()
    console.print(f"Repair run queued: [bold]{data['run_id']}[/bold]")


def _render_status_table(data: dict[str, Any]) -> None:
    console.print(
        f"Run [bold]{data['run_id']}[/bold] — watchlist "
        f"[cyan]{data['watchlist_name']}[/cyan] — status [yellow]{data['status']}[/yellow]"
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("symbol", "asset_class", "status", "step", "error", "next_action"):
        table.add_column(col)
    for row in data["per_symbol"]:
        err = (row.get("error") or {}).get("code") or ""
        table.add_row(
            row["symbol"],
            row["asset_class"],
            row["status"],
            row["step"],
            err,
            row.get("next_action") or "",
        )
    console.print(table)


def _print_cost_estimate(data: dict[str, Any]) -> None:
    # Server-side ``estimated_cost_usd`` is ``Decimal``; FastAPI/Pydantic
    # serializes it as a JSON string. Coerce via ``float()`` (handles both
    # str and numeric) before formatting with ``:.2f``.
    raw = data["estimated_cost_usd"]
    estimate_usd = float(raw) if not isinstance(raw, float) else raw
    console.print(
        f"Dry-run estimate: [bold]${estimate_usd:.2f}[/bold] "
        f"({data['estimate_confidence']} confidence) — {data['symbol_count']} symbols"
    )
    console.print(f"Basis: {data['estimate_basis']}")


def _exit_for_status(run_status: str) -> None:
    mapping = {
        "completed": 0,
        "completed_with_failures": 1,
        "failed": 2,
    }
    raise typer.Exit(code=mapping.get(run_status, 3))
