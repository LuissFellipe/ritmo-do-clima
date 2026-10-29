"""Gold: compressão da temperatura pelos k maiores coeficientes da FFT (RF18–RF19).

A FFT pede a grade inteira de horas, do primeiro ao último dado do ano. As
lacunas são preenchidas por interpolação linear só para a transformada; o erro
é medido apenas nas horas que tinham dado.

Os bytes saem de um codificador de verdade, e a reconstrução passa pelo
decodificador: cabeçalho com a hora inicial e o tamanho da grade, e, para cada
coeficiente guardado, o índice em uint16 e as partes real e imaginária em
float32. O arredondamento para float32 entra no erro medido.

A comparação é com o Parquet da coluna de temperatura da estação-ano, gravado
como a silver grava (float64, zstd), e com o mesmo Parquet em décimos de grau
num Int16, que é a forma sem perda mais enxuta: o INMET publica uma casa decimal.

Roda num processo só. Com o ProcessPoolExecutor do espectro, os filhos criados
por fork travavam ao gravar o Parquet (o pool de threads do Polars não sobrevive
ao fork): 4 processos a 0% de CPU por 10 minutos. Em série, a conta é leve.
"""

import io
import json
import sys
import time
from datetime import datetime, timezone

import numpy as np
import polars as pl

from ritmo import config
from ritmo.espectro import series_do_ano

ESQUEMA = {
    "codigo": pl.String,
    "ano": pl.Int32,
    "k": pl.Int32,
    "n_horas": pl.Int32,
    "bytes": pl.Int64,
    "bytes_parquet": pl.Int64,
    "bytes_parquet_decimos": pl.Int64,
    "razao": pl.Float64,
    "erro_medio_c": pl.Float64,
    "erro_max_c": pl.Float64,
}

CABECALHO = np.dtype([("inicio_h", "<i4"), ("n", "<i4")])
COEFICIENTE = np.dtype([("indice", "<u2"), ("re", "<f4"), ("im", "<f4")])


def codificar(inicio_h: int, x: np.ndarray, k: int) -> bytes:
    """Série completa na grade horária → os k coeficientes de maior módulo da rFFT."""
    X = np.fft.rfft(x)
    maiores = np.sort(np.argsort(-np.abs(X), kind="stable")[:k])
    coef = np.empty(len(maiores), COEFICIENTE)
    coef["indice"], coef["re"], coef["im"] = maiores, X[maiores].real, X[maiores].imag
    return np.array([(inicio_h, len(x))], CABECALHO).tobytes() + coef.tobytes()


def decodificar(dados: bytes) -> tuple[int, np.ndarray]:
    cab = np.frombuffer(dados[: CABECALHO.itemsize], CABECALHO)[0]
    coef = np.frombuffer(dados[CABECALHO.itemsize :], COEFICIENTE)
    n = int(cab["n"])
    X = np.zeros(n // 2 + 1, complex)
    X[coef["indice"]] = coef["re"] + 1j * coef["im"]
    return int(cab["inicio_h"]), np.fft.irfft(X, n)


def bytes_parquet(y: np.ndarray, decimos: bool = False) -> int:
    coluna = pl.Series("temp_c", y).fill_nan(None)
    if decimos:
        coluna = (coluna * 10).round().cast(pl.Int16)
    buffer = io.BytesIO()
    coluna.to_frame().write_parquet(buffer)
    return buffer.tell()


def comprimir(t: np.ndarray, y: np.ndarray, ks=config.K_COMPRESSAO) -> list[dict]:
    """t em horas desde 1º de janeiro (UTC); y com NaN onde não há dado usável."""
    validos = ~np.isnan(y)
    tv, yv = t[validos].astype(int), y[validos]
    grade = np.arange(tv[0], tv[-1] + 1)
    x = np.interp(grade, tv, yv)
    originais = bytes_parquet(y), bytes_parquet(y, decimos=True)

    linhas = []
    for k in ks:
        dados = codificar(int(tv[0]), x, k)
        inicio, reconstruida = decodificar(dados)
        erro = np.abs(reconstruida[tv - inicio] - yv)
        linhas.append({
            "k": k, "n_horas": int(len(yv)), "bytes": len(dados),
            "bytes_parquet": originais[0], "bytes_parquet_decimos": originais[1],
            "razao": originais[0] / len(dados),
            "erro_medio_c": float(erro.mean()), "erro_max_c": float(erro.max()),
        })
    return linhas


def curva(tabela: pl.DataFrame) -> pl.DataFrame:
    """Medianas por k: a curva bytes × erro do critério de aceite."""
    return (
        tabela.group_by("k")
        .agg(
            estacoes=pl.len(),
            bytes=pl.col("bytes").median(),
            bytes_parquet=pl.col("bytes_parquet").median(),
            bytes_parquet_decimos=pl.col("bytes_parquet_decimos").median(),
            razao=pl.col("razao").median(),
            erro_medio_c=pl.col("erro_medio_c").median(),
            erro_max_c=pl.col("erro_max_c").median(),
            abaixo_do_alvo=(pl.col("erro_medio_c") < config.ERRO_ALVO_COMPRESSAO_C).mean(),
        )
        .sort("k")
    )


def processar_ano(ano: int) -> dict:
    series = series_do_ano(ano)
    linhas = [{"codigo": codigo, "ano": ano, **linha} for codigo, ano, t, y in series for linha in comprimir(t, y)]
    tabela = pl.DataFrame(linhas, schema=ESQUEMA).sort("codigo", "k")
    destino = config.GOLD / "compressao"
    destino.mkdir(parents=True, exist_ok=True)
    tabela.write_parquet(destino / f"ano={ano}.parquet", statistics=True)

    por_k = curva(tabela)
    passa = por_k.filter(pl.col("erro_medio_c") < config.ERRO_ALVO_COMPRESSAO_C)
    return {
        "ano": ano,
        "series": len(series),
        "erro_cai_com_k": bool((por_k["erro_medio_c"].diff().drop_nulls() < 0).all()),
        "menor_k_abaixo_do_alvo": int(passa["k"][0]) if passa.height else None,
        "curva": por_k.to_dicts(),
    }


def main(anos=None) -> None:
    anos = anos or sorted(int(p.stem.split("=")[1]) for p in (config.SILVER / "cobertura").glob("ano=*.parquet"))
    inicio = datetime.now(timezone.utc)
    resultados = []
    for ano in anos:
        t0 = time.perf_counter()
        r = processar_ano(ano)
        resultados.append(r)
        print(
            f"{ano}: {r['series']} séries, erro cai com k: {r['erro_cai_com_k']}, menor k abaixo de "
            f"{config.ERRO_ALVO_COMPRESSAO_C} °C: {r['menor_k_abaixo_do_alvo']} ({time.perf_counter() - t0:.0f} s)"
        )
    registro = {
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "etapa": "compressao",
        "anos": resultados,
    }
    with open(config.SILVER / "execucoes.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or None)
