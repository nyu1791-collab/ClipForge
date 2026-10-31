# 制作の入口。詳細は README.md と docs/QUICK_START.md。
PY := uv run --no-sync python
ARGS ?=

.PHONY: setup dev produce resume collect test lint fmt

setup:            ## 制作用の最小依存
	uv sync --locked --no-dev --inexact

dev:              ## 開発用（FFmpeg・日本語フォント・テスト依存）
	bash tools/dev_setup.sh

produce:          ## make produce URL=... OUT=... [ARGS='--clips 3']
	$(PY) tools/produce_with_gemini.py '$(URL)' --output '$(OUT)' $(ARGS)

resume:           ## make resume OUT=...
	$(PY) tools/produce_with_gemini.py --resume --output '$(OUT)' $(ARGS)

collect:          ## make collect OUT=... CLIP=1
	$(PY) tools/collect_production_clip.py --output '$(OUT)' --clip $(CLIP)

test:
	$(PY) -m pytest -q -p no:cacheprovider

lint:
	uv run --no-sync ruff check .

fmt:
	uv run --no-sync ruff format . && uv run --no-sync ruff check --fix .
