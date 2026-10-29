"""Gold: espectro da temperatura por estação e ano (RF10–RF11).

Série sem lacuna vai pela FFT, sem a média e com janela de Hann. Série com
lacuna vai pelo periodograma de Lomb-Scargle, na mesma grade de frequências
k/N, para que os dois métodos deem períodos comparáveis.

O espectro só localiza os picos. Amplitude, fase e fração da variância de cada
período saem de uma senoide ajustada por mínimos quadrados nos pontos válidos:
vale igual para os dois métodos e não sofre a atenuação da janela de Hann.
"""

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np
import polars as pl
from scipy.signal import lombscargle

from ritmo import config
from ritmo.silver import STATUS_USAVEL

ESQUEMA = {
    "codigo": pl.String,
    "ano": pl.Int32,
    "papel": pl.String,
    "periodo_h": pl.Float64,
    "amplitude_c": pl.Float64,
    "fase_h": pl.Float64,
    "fracao_variancia": pl.Float64,
    "metodo": pl.String,
    "n_horas": pl.Int32,
}


def ajustar_senoide(t: np.ndarray, y: np.ndarray, periodo: float) -> tuple[float, float, float]:
    """Amplitude, instante do máximo (horas, módulo o período) e fração da variância explicada."""
    w = 2 * np.pi / periodo
    X = np.column_stack([np.ones_like(t), np.cos(w * t), np.sin(w * t)])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    _, a, b = coef
    variancia = y.var()
    explicada = 1 - (y - X @ coef).var() / variancia if variancia > 0 else 0.0
    # c + a·cos(wt) + b·sin(wt) = c + A·cos(w(t − t_max)), com t_max = atan2(b, a) / w
    return float(np.hypot(a, b)), float((np.arctan2(b, a) / w) % periodo), float(explicada)


def espectro(t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, str]:
    """Potência na grade k/N, k = 1..N/2, com N = horas do primeiro ao último ponto."""
    n = int(t[-1] - t[0]) + 1
    freq = np.arange(1, n // 2 + 1) / n  # ciclos por hora
    if len(t) == n:
        x = (y - y.mean()) * np.hanning(n)
        return freq, np.abs(np.fft.rfft(x)[1 : n // 2 + 1]) ** 2, "fft"
    return freq, lombscargle(t - t[0], y - y.mean(), 2 * np.pi * freq), "lomb-scargle"


def picos(freq: np.ndarray, potencia: np.ndarray, quantos: int) -> np.ndarray:
    """Períodos (h) dos máximos locais de maior potência, do maior para o menor."""
    p = potencia
    maximo_local = np.r_[p[0] > p[1], (p[1:-1] > p[:-2]) & (p[1:-1] >= p[2:]), p[-1] > p[-2]]
    indices = np.flatnonzero(maximo_local)
    indices = indices[np.argsort(-p[indices], kind="stable")][:quantos]
    return 1 / freq[indices]


def analisar(t: np.ndarray, y: np.ndarray) -> list[dict]:
    """t em horas desde 1º de janeiro (UTC); y com NaN onde não há dado usável."""
    validos = ~np.isnan(y)
    t, y = t[validos].astype(float), y[validos]
    freq, potencia, metodo = espectro(t, y)

    periodos = [("ciclo_24h", config.PERIODO_DIA_H)]
    if t[-1] - t[0] + 1 >= config.DIAS_MINIMOS_CICLO_ANUAL * 24:
        periodos.append(("ciclo_anual", config.PERIODO_ANO_H))
    periodos += [(f"pico_{i}", p) for i, p in enumerate(picos(freq, potencia, config.PICOS_POR_SERIE), 1)]

    linhas = []
    for papel, periodo in periodos:
        amplitude, fase, fracao = ajustar_senoide(t, y, periodo)
        linhas.append({
            "papel": papel, "periodo_h": float(periodo), "amplitude_c": amplitude, "fase_h": fase,
            "fracao_variancia": fracao, "metodo": metodo, "n_horas": int(len(t)),
        })
    return linhas


def _analisar_estacao(args) -> list[dict]:
    codigo, ano, t, y = args
    return [{"codigo": codigo, "ano": ano, **linha} for linha in analisar(t, y)]


def series_do_ano(ano: int) -> list[tuple]:
    """Uma série por estação aprovada no ano (RF09): horas desde 1º/jan e temperatura usável."""
    aprovadas = (
        pl.read_parquet(config.SILVER / "cobertura" / f"ano={ano}.parquet")
        .filter(pl.col("aprovada"))["codigo"]
    )
    inicio = datetime(ano, 1, 1, tzinfo=timezone.utc)
    medicoes = (
        pl.read_parquet(config.SILVER / "medicao_horaria" / f"ano={ano}", hive_partitioning=False,
                        columns=["codigo", "instante_utc", "temp_c", "status"])
        .filter(pl.col("codigo").is_in(aprovadas.implode()))
        .select(
            "codigo",
            t=((pl.col("instante_utc") - pl.lit(inicio)).dt.total_hours()),
            y=pl.when(pl.col("status").is_in(STATUS_USAVEL)).then(pl.col("temp_c")).otherwise(None).fill_null(np.nan),
        )
        .sort("codigo", "t")
    )
    return [
        (codigo, ano, parte["t"].to_numpy(), parte["y"].to_numpy())
        for (codigo,), parte in medicoes.partition_by("codigo", as_dict=True, maintain_order=True).items()
    ]


def pico_24h_entre_os_3_maiores(espectro_ano: pl.DataFrame) -> pl.DataFrame:
    """Critério de aceite: o ciclo de 24 h aparece entre os 3 maiores picos."""
    return (
        espectro_ano.group_by("codigo", "ano")
        .agg(
            ok=(pl.col("papel").is_in(["pico_1", "pico_2", "pico_3"]) & ((pl.col("periodo_h") - 24).abs() <= 0.1)).any()
        )
    )


def processar_ano(ano: int, executor: ProcessPoolExecutor) -> dict:
    series = series_do_ano(ano)
    linhas = [linha for resultado in executor.map(_analisar_estacao, series, chunksize=4) for linha in resultado]
    tabela = pl.DataFrame(linhas, schema=ESQUEMA).sort("codigo", "papel")
    destino = config.GOLD / "espectro"
    destino.mkdir(parents=True, exist_ok=True)
    tabela.write_parquet(destino / f"ano={ano}.parquet", statistics=True)

    metodos = tabela.unique(["codigo", "metodo"]).group_by("metodo").len()
    aceite = pico_24h_entre_os_3_maiores(tabela)
    return {
        "ano": ano,
        "series": len(series),
        "por_metodo": dict(metodos.iter_rows()),
        "pico_24h_entre_os_3_maiores": round(float(aceite["ok"].mean()), 4) if aceite.height else None,
    }


def main(anos=None) -> None:
    anos = anos or sorted(int(p.stem.split("=")[1]) for p in (config.SILVER / "cobertura").glob("ano=*.parquet"))
    inicio = datetime.now(timezone.utc)
    resultados = []
    with ProcessPoolExecutor(max_workers=config.WORKERS) as executor:
        for ano in anos:
            t0 = time.perf_counter()
            r = processar_ano(ano, executor)
            resultados.append(r)
            print(
                f"{ano}: {r['series']} séries {r['por_metodo']}, pico de 24 h entre os 3 maiores em "
                f"{r['pico_24h_entre_os_3_maiores']:.1%} ({time.perf_counter() - t0:.0f} s)"
            )
    registro = {
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "etapa": "espectro",
        "anos": resultados,
    }
    with open(config.SILVER / "execucoes.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or None)
