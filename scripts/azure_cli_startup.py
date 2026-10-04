#!/usr/bin/env python3
"""Complete Requests imports before Azure CLI starts command/poller threads."""
import runpy

if __name__ == "__main__":
    import requests  # noqa: F401 -- preload in this CLI-owned main process

    runpy.run_module("azure.cli", run_name="__main__", alter_sys=True)
