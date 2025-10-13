.PHONY: format lint test all
format:
	black .
lint:
	ruff check . --fix
test:
	pytest -q
all: format lint test
