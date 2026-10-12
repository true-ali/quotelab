.PHONY: install test lint format data data-dry synth backtest backtest-real report report-real real paper paper-compare

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

data-dry:
	uv run quotelab download --dry-run

data:
	uv run quotelab download
	uv run quotelab prepare

synth:
	uv run quotelab synth

backtest: synth
	uv run quotelab backtest --dataset synthetic

backtest-real:
	uv run quotelab backtest --dataset real

report:
	uv run quotelab report --dataset synthetic

report-real:
	uv run quotelab report --dataset real

real: data backtest-real report-real

paper:
	uv run quotelab paper
	uv run quotelab paper-compare

paper-compare:
	uv run quotelab paper-compare
