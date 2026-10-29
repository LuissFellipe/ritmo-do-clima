# O make daqui roda com -j16; as etapas dependem uma da outra.
.NOTPARALLEL:
.PHONY: all download silver espectro baseline anomalias painel test clean

all: test download silver espectro baseline anomalias painel

download:
	uv run python -m ritmo.download

silver:
	uv run python -m ritmo.silver

espectro:
	uv run python -m ritmo.espectro

baseline:
	uv run python -m ritmo.baseline

anomalias:
	uv run python -m ritmo.anomalias

painel:
	uv run python -m ritmo.painel

test:
	uv run pytest -q

clean:
	rm -rf data/silver data/gold
