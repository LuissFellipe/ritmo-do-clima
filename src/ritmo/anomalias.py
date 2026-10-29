"""Gold: horas anômalas e eventos, a partir do resíduo da baseline (RF15–RF17).

z robusto = (resíduo − mediana) / (1,4826 · MAD), com mediana e MAD do resíduo
na janela de 30 dias centrada no dia da hora. O fator 1,4826 faz a MAD de uma
normal valer o desvio padrão. A janela anda de dia em dia: as 24 horas de um dia
usam a mesma mediana e a mesma MAD, calculadas exatamente.

Horas com |z| acima de Z_ANOMALIA viram eventos: horas do mesmo sinal, com até
INTERVALO_MAX_EVENTO_H horas normais entre elas. O evento é regional quando
alguma estação a até RAIO_VIZINHANCA_KM tem evento do mesmo sinal ao mesmo tempo
(com folga de TOLERANCIA_SIMULTANEO_H), isolado quando as vizinhas tinham dado
naquele dia e nenhuma teve evento, e sem_vizinhas quando nenhuma tinha dado.
"""

import json
import time
import warnings
from datetime import datetime, timezone

import numpy as np
import polars as pl
from numpy.lib.stride_tricks import sliding_window_view

from ritmo import config
from ritmo.baseline import EPOCA, horas_desde_a_epoca

JANELA_H = config.JANELA_Z_DIAS * 24
ESCALA_MAD = 1.4826
RAIO_TERRA_KM = 6371.0

ESQUEMA_EVENTOS = {
    "codigo": pl.String,
    "inicio_h": pl.Int64,
    "fim_h": pl.Int64,
    "pico_h": pl.Int64,
    "pico_z": pl.Float64,
    "horas_anomalas": pl.Int32,
}


def z_robusto(h: np.ndarray, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """z de cada hora e os dias (horas desde a época // 24) em que ele existe.

    h são horas inteiras desde a época, ordenadas, e r o resíduo nessas horas.
    """
    inicio = h[0] - h[0] % 24
    n_dias = (h[-1] - inicio) // 24 + 1
    meia = JANELA_H // 2
    grade = np.full(meia + 24 * n_dias + meia, np.nan)
    grade[meia + h - inicio] = r
    # A janela k começa na posição 12 + 24k da grade com margem: cobre do meio-dia
    # do dia k menos 15 dias ao meio-dia mais 15 dias.
    janelas = sliding_window_view(grade, JANELA_H)[12::24][:n_dias]
    validas = np.count_nonzero(~np.isnan(janelas), axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # janela sem nenhum dado
        mediana = np.nanmedian(janelas, axis=1)
        escala = ESCALA_MAD * np.nanmedian(np.abs(janelas - mediana[:, None]), axis=1)
    usavel = (validas >= config.HORAS_MINIMAS_JANELA_Z) & (escala > 0)
    dia = (h - inicio) // 24
    z = np.where(usavel[dia], (r - mediana[dia]) / np.where(escala[dia] > 0, escala[dia], 1), np.nan)
    return z, np.unique((inicio // 24 + dia)[~np.isnan(z)])


def eventos(codigo: str, h: np.ndarray, z: np.ndarray) -> list[dict]:
    """Horas com |z| acima do limite, agrupadas por sinal e proximidade no tempo."""
    idx = np.flatnonzero(np.abs(np.nan_to_num(z)) > config.Z_ANOMALIA)
    if not len(idx):
        return []
    sinal = np.sign(z[idx])
    novo = np.r_[True, (np.diff(h[idx]) > config.INTERVALO_MAX_EVENTO_H + 1) | (np.diff(sinal) != 0)]
    linhas = []
    for grupo in np.split(idx, np.flatnonzero(novo)[1:]):
        pico = grupo[np.argmax(np.abs(z[grupo]))]
        linhas.append({
            "codigo": codigo, "inicio_h": int(h[grupo[0]]), "fim_h": int(h[grupo[-1]]),
            "pico_h": int(h[pico]), "pico_z": float(z[pico]), "horas_anomalas": len(grupo),
        })
    return linhas


def vizinhanca(estacoes: pl.DataFrame) -> pl.DataFrame:
    """Pares (codigo, vizinha) a até RAIO_VIZINHANCA_KM, pela distância no círculo máximo."""
    lat, lon = np.radians(estacoes["lat"].to_numpy()), np.radians(estacoes["lon"].to_numpy())
    dlat, dlon = lat[:, None] - lat[None, :], lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:, None]) * np.cos(lat[None, :]) * np.sin(dlon / 2) ** 2
    distancia = 2 * RAIO_TERRA_KM * np.arcsin(np.sqrt(a))
    i, j = np.nonzero((distancia <= config.RAIO_VIZINHANCA_KM) & ~np.eye(len(lat), dtype=bool))
    codigos = estacoes["codigo"].to_numpy()
    return pl.DataFrame({"codigo": codigos[i], "vizinha": codigos[j], "distancia_km": distancia[i, j]})


def classificar(ev: pl.DataFrame, pares: pl.DataFrame, dias: pl.DataFrame) -> pl.DataFrame:
    """Acrescenta n_vizinhas_com_dado, n_vizinhas_com_evento e tipo a cada evento.

    Dois eventos simultâneos (com a folga) têm pelo menos um dia em comum entre os
    dias que cada um toca com a folga: a junção é por dia, e só então pelas horas.
    """
    folga = config.TOLERANCIA_SIMULTANEO_H
    por_dia = ev.with_columns(
        sinal=pl.col("pico_z").sign(),
        dia=pl.int_ranges((pl.col("inicio_h") - folga) // 24, (pl.col("fim_h") + folga) // 24 + 1),
    ).explode("dia")
    vizinhos = pares.select("codigo", "vizinha")
    com_evento = (
        por_dia.join(vizinhos, on="codigo")
        .join(
            por_dia.select(vizinha="codigo", dia="dia", sinal="sinal", v_inicio="inicio_h", v_fim="fim_h"),
            on=["vizinha", "dia", "sinal"],
        )
        .filter((pl.col("v_inicio") <= pl.col("fim_h") + folga) & (pl.col("v_fim") >= pl.col("inicio_h") - folga))
        .group_by("codigo", "inicio_h")
        .agg(n_vizinhas_com_evento=pl.col("vizinha").n_unique())
    )
    com_dado = (
        ev.select("codigo", "inicio_h", dia=pl.col("inicio_h") // 24)
        .join(vizinhos, on="codigo")
        .join(dias.rename({"codigo": "vizinha"}), on=["vizinha", "dia"])
        .group_by("codigo", "inicio_h")
        .agg(n_vizinhas_com_dado=pl.col("vizinha").n_unique())
    )
    return (
        ev.join(com_dado, on=["codigo", "inicio_h"], how="left")
        .join(com_evento, on=["codigo", "inicio_h"], how="left")
        .with_columns(pl.col("n_vizinhas_com_dado", "n_vizinhas_com_evento").fill_null(0).cast(pl.Int32))
        .with_columns(
            tipo=pl.when(pl.col("n_vizinhas_com_evento") > 0).then(pl.lit("regional"))
            .when(pl.col("n_vizinhas_com_dado") > 0).then(pl.lit("isolado"))
            .otherwise(pl.lit("sem_vizinhas"))
        )
        .sort("codigo", "inicio_h")
    )


def instante(coluna: str) -> pl.Expr:
    return pl.lit(EPOCA) + pl.duration(hours=pl.col(coluna))


def main() -> None:
    inicio = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    baseline = (
        pl.scan_parquet(config.GOLD / "baseline" / "*.parquet")
        .filter(pl.col("residuo_c").is_not_null())
        .select("execucao", "codigo", h=horas_desde_a_epoca().cast(pl.Int64), r=pl.col("residuo_c"))
        .sort("codigo", "h")
        .collect()
    )
    execucao = baseline["execucao"].unique().to_list()
    if len(execucao) != 1:
        raise RuntimeError(f"gold/baseline mistura execuções: {execucao}")

    zs, linhas, dias = [], [], []
    for (codigo,), parte in baseline.drop("execucao").partition_by("codigo", as_dict=True, maintain_order=True).items():
        h, r = parte["h"].to_numpy(), parte["r"].to_numpy()
        z, dias_com_z = z_robusto(h, r)
        zs.append(parte.select("codigo", "h").with_columns(z_robusto=pl.Series(z, nan_to_null=True)))
        linhas += eventos(codigo, h, z)
        dias.append(pl.DataFrame({"codigo": codigo, "dia": dias_com_z}))
    del baseline

    z = pl.concat(zs).with_columns(instante_utc=instante("h"))
    destino = config.GOLD / "z_robusto"
    destino.mkdir(parents=True, exist_ok=True)
    z = z.with_columns(ano=pl.col("instante_utc").dt.year())
    for (ano,), parte in z.partition_by("ano", as_dict=True, maintain_order=True).items():
        parte.select("codigo", "instante_utc", "z_robusto").write_parquet(destino / f"ano={ano}.parquet", statistics=True)
    horas_com_z = z["z_robusto"].is_not_null().sum()
    horas_anomalas = (z["z_robusto"].abs() > config.Z_ANOMALIA).sum()
    del z, zs

    estacoes = pl.read_parquet(config.SILVER / "dim_estacao.parquet").filter(
        pl.col("codigo").is_in(pl.concat(dias)["codigo"].unique().implode())
    )
    pares = vizinhanca(estacoes)
    tabela = (
        classificar(pl.DataFrame(linhas, schema=ESQUEMA_EVENTOS), pares, pl.concat(dias))
        .select(
            pl.lit(execucao[0]).alias("execucao_baseline"), "codigo",
            inicio=instante("inicio_h"), fim=instante("fim_h"), pico=instante("pico_h"),
            pico_z="pico_z", duracao_h=(pl.col("fim_h") - pl.col("inicio_h") + 1).cast(pl.Int32),
            horas_anomalas="horas_anomalas", tipo="tipo",
            n_vizinhas_com_dado="n_vizinhas_com_dado", n_vizinhas_com_evento="n_vizinhas_com_evento",
        )
    )
    tabela.write_parquet(config.GOLD / "eventos_anomalia.parquet", statistics=True)

    por_tipo = dict(tabela.group_by("tipo").len().sort("tipo").iter_rows())
    resumo = {
        "horas_com_z": int(horas_com_z),
        "horas_anomalas": int(horas_anomalas),
        "fracao_anomala": round(horas_anomalas / horas_com_z, 6),
        "eventos": tabela.height,
        "por_tipo": por_tipo,
        "eventos_frios": int((tabela["pico_z"] < 0).sum()),
        "estacoes_sem_vizinha_no_raio": estacoes.height - pares["codigo"].n_unique(),
    }
    print(f"{resumo} ({time.perf_counter() - t0:.0f} s)")

    registro = {
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "etapa": "anomalias",
        "execucao_baseline": execucao[0],
        **resumo,
    }
    with open(config.SILVER / "execucoes.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
