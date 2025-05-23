#!/usr/bin/env bash


uv run uvx pyrefly check src/
uv run uvx ty check src/
uv run uvx mypy src/
uv run uvx pyright[nodejs] src/
