"""Testes de qualidade da silver (RQ01–RQ10).

Falha bloqueante interrompe o pipeline. Aviso vai para o relatório, uma linha
por estação-ano, publicado junto com o painel.
"""

import polars as pl

from ritmo import config

# Caixa que contém o território brasileiro, ilhas oceânicas incluídas.
LATITUDE_BR = (-34.0, 6.0)
LONGITUDE_BR = (-74.5, -28.0)
TOLERANCIA_COORDENADA = 0.01


class FalhaBloqueante(Exception):
    """Um teste bloqueante falhou; nada do ano é gravado."""


def dentro_do_brasil() -> pl.Expr:
    # A C891 (Criosfera, na Antártida, lat -84) vem cadastrada como SP.
    return pl.col("lat").is_between(*LATITUDE_BR) & pl.col("lon").is_between(*LONGITUDE_BR)


def checar_ano(
    ano: int,
    grade: pl.DataFrame,
    cobertura: pl.DataFrame,
    fora_da_faixa: dict[str, tuple[int, int]],
    revisadas: pl.DataFrame,
) -> pl.DataFrame:
    # RQ02: chave única.
    duplicadas = grade.height - grade.select("codigo", "instante_utc").n_unique()
    if duplicadas:
        raise FalhaBloqueante(f"{ano}: {duplicadas} chaves (codigo, instante_utc) repetidas na silver")

    # RQ04: acima do limite, o problema é do arquivo, não de um sensor.
    for coluna, (fora, validos) in fora_da_faixa.items():
        if validos and fora / validos > config.FORA_DA_FAIXA_BLOQUEANTE:
            raise FalhaBloqueante(
                f"{ano}: {coluna} com {fora} de {validos} valores fora da faixa {config.FAIXAS[coluna]}"
            )

    # RQ06, RQ07, RQ08 e RQ10 viram colunas do relatório.
    return (
        cobertura.join(revisadas, on="codigo", how="left")
        .with_columns(pl.col("horas_revisadas").fill_null(0))
        .with_columns(
            avisos=pl.concat_list(
                pl.when(pl.col("saltos") > 0).then(pl.lit("RQ06 salto de temperatura")),
                pl.when(pl.col("horas_suspeitas") > 0).then(pl.lit("RQ07 sensor travado")),
                pl.when(~pl.col("aprovada")).then(pl.lit("RQ08 cobertura baixa")),
                pl.when(pl.col("horas_revisadas") > 0).then(pl.lit("RQ10 horas revisadas")),
            ).list.drop_nulls()
        )
    )


def checar_estacoes(estacao_ano: pl.DataFrame) -> pl.DataFrame:
    """RQ09: coordenadas dentro do Brasil e estáveis entre anos. Devolve a dimensão de estações."""
    variacao = estacao_ano.group_by("codigo").agg(
        variacao_lat=pl.col("lat").max() - pl.col("lat").min(),
        variacao_lon=pl.col("lon").max() - pl.col("lon").min(),
        primeiro_ano=pl.col("ano").min(),
        ultimo_ano=pl.col("ano").max(),
    )
    atual = estacao_ano.sort("ano", "arquivo").unique("codigo", keep="last")
    fora = ~dentro_do_brasil()
    mudou = (pl.col("variacao_lat") > TOLERANCIA_COORDENADA) | (pl.col("variacao_lon") > TOLERANCIA_COORDENADA)
    return (
        atual.join(variacao, on="codigo")
        .with_columns(
            avisos=pl.concat_list(
                pl.when(pl.col("lat").is_null() | pl.col("lon").is_null()).then(pl.lit("RQ09 coordenada ausente")),
                pl.when(pl.col("altitude_m").is_null()).then(pl.lit("RQ09 altitude ausente")),
                pl.when(fora).then(pl.lit("RQ09 fora do Brasil, excluída da silver")),
                pl.when(mudou).then(pl.lit("RQ09 coordenada mudou entre anos")),
            ).list.drop_nulls()
        )
        .select(
            "codigo", "nome", "uf", "regiao", "lat", "lon", "altitude_m", "fundacao",
            "layout", "primeiro_ano", "ultimo_ano", "variacao_lat", "variacao_lon", "avisos",
        )
        .sort("codigo")
    )
