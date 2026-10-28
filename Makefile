# O make daqui roda com -j16; as etapas dependem uma da outra.
.NOTPARALLEL:
.PHONY: all download silver painel test clean

all: test download silver painel

download:
	uv run python -m ritmo.download

silver:
	uv run python -m ritmo.silver

painel:
	uv run python -m ritmo.painel

test:
	uv run pytest -q

clean:
	rm -rf data/silver
