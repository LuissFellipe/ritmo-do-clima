"""Gold: baseline harmônica da temperatura por estação (RF12–RF14).

Uma série de Fourier por estação, ajustada por mínimos quadrados em todos os
anos aprovados: média, tendência linear, K_dia harmônicos de 24 h, K_ano de
8.766 h e a interação, em que os harmônicos do dia são modulados pelo primeiro
harmônico do ano para que o ciclo diário possa mudar de amplitude e de forma
ao longo do ano.

K é um só para todas as estações. Cada par (K_dia, K_ano) é ajustado com os
anos anteriores ao de validação e medido nele; fica o par com menos colunas
cuja mediana do erro médio absoluto entre as estações está a até TOLERANCIA_K_C
da melhor. A tendência só entra no ajuste com ANOS_MINIMOS_TENDENCIA anos. O ano de teste, o seguinte, compara a
baseline com a ingênua (mesma hora do dia anterior) sem ter entrado na escolha.

O tempo conta horas desde 1º de janeiro de ANO_INICIAL (UTC), e a tendência sai
em °C por ano a partir dessa data.
"""

import hashlib
import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

import numpy as np
import polars as pl

from ritmo import config
from ritmo.silver import STATUS_USAVEL

EPOCA = datetime(config.ANO_INICIAL, 1, 1, tzinfo=timezone.utc)
W_DIA = 2 * np.pi / config.PERIODO_DIA_H
W_ANO = 2 * np.pi / config.PERIODO_ANO_H
BLOCOS_SEM_FASE = ("media", "tendencia")

ESQUEMA_COEFICIENTES = {
    "execucao": pl.String,
    "codigo": pl.String,
    "bloco": pl.String,
    "k": pl.Int32,
    "a_k": pl.Float64,
    "b_k": pl.Float64,
    "janela_treino": pl.String,
}
ESQUEMA_VALIDACAO = {
    "codigo": pl.String,
    "etapa": pl.String,
    "ano": pl.Int32,
    "k_dia": pl.Int32,
    "k_ano": pl.Int32,
    "n_horas": pl.Int32,
    "mae_c": pl.Float64,
    "mae_ingenua_c": pl.Float64,
}


def termos(k_dia: int, k_ano: int, com_tendencia: bool = True) -> list[tuple[str, int, str]]:
    """Colunas do modelo como (bloco, k, parte): parte "a" multiplica o cosseno, "b" o seno."""
    lista = [("media", 0, "a")] + ([("tendencia", 0, "a")] if com_tendencia else [])
    lista += [("dia", k, p) for k in range(1, k_dia + 1) for p in "ab"]
    lista += [("ano", k, p) for k in range(1, k_ano + 1) for p in "ab"]
    lista += [(b, k, p) for b in ("dia_cos_ano", "dia_sin_ano") for k in range(1, k_dia + 1) for p in "ab"]
    return lista


def coluna(t: np.ndarray, bloco: str, k: int, parte: str) -> np.ndarray:
    if bloco == "media":
        return np.ones_like(t)
    if bloco == "tendencia":
        return t / config.PERIODO_ANO_H
    onda = np.cos if parte == "a" else np.sin
    if bloco == "dia":
        return onda(k * W_DIA * t)
    if bloco == "ano":
        return onda(k * W_ANO * t)
    modulador = np.cos(W_ANO * t) if bloco == "dia_cos_ano" else np.sin(W_ANO * t)
    return onda(k * W_DIA * t) * modulador


def matriz(t: np.ndarray, lista: list[tuple[str, int, str]]) -> np.ndarray:
    return np.column_stack([coluna(t, *termo) for termo in lista])


CANDIDATOS = [(kd, ka) for kd in config.K_DIA_CANDIDATOS for ka in config.K_ANO_CANDIDATOS]
TERMOS_MAX = termos(max(config.K_DIA_CANDIDATOS), max(config.K_ANO_CANDIDATOS))
# Cada candidato é um subconjunto das colunas do maior: a matriz é montada uma vez por estação.
INDICES = {
    (c, com_tendencia): np.array([TERMOS_MAX.index(termo) for termo in termos(*c, com_tendencia)])
    for c in CANDIDATOS for com_tendencia in (True, False)
}


def com_tendencia(anos_treino: np.ndarray) -> bool:
    return len(np.unique(anos_treino)) >= config.ANOS_MINIMOS_TENDENCIA


def resolver(gram: np.ndarray, xty: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Mínimos quadrados pelas equações normais do subconjunto de colunas idx."""
    return np.linalg.lstsq(gram[np.ix_(idx, idx)], xty[idx], rcond=None)[0]


def ingenua(t: np.ndarray, y: np.ndarray, t_alvo: np.ndarray) -> np.ndarray:
    """Temperatura da mesma hora do dia anterior; NaN quando ela não é usável. t precisa estar ordenado."""
    anterior = t_alvo - 24
    pos = np.minimum(np.searchsorted(t, anterior), len(t) - 1)
    return np.where(t[pos] == anterior, y[pos], np.nan)


def avaliar_estacao(codigo: str, t: np.ndarray, ano: np.ndarray, y: np.ndarray) -> tuple[list[dict], dict]:
    """Erro de cada candidato na validação e no teste, e os coeficientes de cada um com todos os anos.

    Os coeficientes vêm como {(k_dia, k_ano): (termos, valores)}, porque a tendência pode ficar de fora.
    """
    X = matriz(t, TERMOS_MAX)
    linhas = []
    for etapa, ano_alvo in (("validacao", config.ANO_TESTE_BASELINE - 1), ("teste", config.ANO_TESTE_BASELINE)):
        treino, alvo = ano < ano_alvo, ano == ano_alvo
        if not alvo.any() or len(np.unique(ano[treino])) < config.ANOS_MINIMOS_BASELINE:
            continue
        gram, xty = X[treino].T @ X[treino], X[treino].T @ y[treino]
        tendencia = com_tendencia(ano[treino])
        y_alvo, X_alvo = y[alvo], X[alvo]
        if etapa == "teste":
            # As duas baselines são medidas nas mesmas horas: as que têm o dia anterior usável.
            y_ingenua = ingenua(t, y, t[alvo])
            comparavel = ~np.isnan(y_ingenua)
            y_alvo, X_alvo, y_ingenua = y_alvo[comparavel], X_alvo[comparavel], y_ingenua[comparavel]
            mae_ingenua = float(np.abs(y_ingenua - y_alvo).mean()) if len(y_alvo) else None
        for k_dia, k_ano in CANDIDATOS:
            idx = INDICES[((k_dia, k_ano), tendencia)]
            erro = np.abs(X_alvo[:, idx] @ resolver(gram, xty, idx) - y_alvo)
            linhas.append({
                "codigo": codigo, "etapa": etapa, "ano": ano_alvo, "k_dia": k_dia, "k_ano": k_ano,
                "n_horas": len(y_alvo), "mae_c": float(erro.mean()) if len(y_alvo) else None,
                "mae_ingenua_c": mae_ingenua if etapa == "teste" else None,
            })
    gram, xty = X.T @ X, X.T @ y
    tendencia = com_tendencia(ano)
    coeficientes = {
        c: (termos(*c, tendencia), resolver(gram, xty, INDICES[(c, tendencia)])) for c in CANDIDATOS
    }
    return linhas, coeficientes


def _avaliar(args) -> tuple:
    codigo, t, ano, y = args
    linhas, coeficientes = avaliar_estacao(codigo, t, ano, y)
    return codigo, f"{ano.min()}–{ano.max()}", linhas, coeficientes


def escolher_k(validacao: pl.DataFrame) -> tuple[int, int, pl.DataFrame]:
    """Par (K_dia, K_ano) com menos colunas a até TOLERANCIA_K_C da menor mediana do erro."""
    ranking = (
        validacao.filter(pl.col("etapa") == "validacao")
        .group_by("k_dia", "k_ano")
        .agg(mediana_mae_c=pl.col("mae_c").median(), estacoes=pl.len())
        # Mesma contagem de termos(): média, tendência, dia, ano e as duas interações.
        .with_columns(colunas=2 + 6 * pl.col("k_dia") + 2 * pl.col("k_ano"))
        .sort("mediana_mae_c", "colunas")
    )
    limite = ranking["mediana_mae_c"].min() + config.TOLERANCIA_K_C
    melhor = ranking.filter(pl.col("mediana_mae_c") <= limite).sort("colunas", "mediana_mae_c").row(0, named=True)
    return melhor["k_dia"], melhor["k_ano"], ranking


def linhas_de_coeficientes(execucao: str, codigo: str, janela: str, lista: list, coef: np.ndarray) -> list[dict]:
    valores = dict(zip(lista, coef.tolist()))
    linhas = []
    for bloco, k, parte in lista:
        if parte == "b":
            continue
        b_k = None if bloco in BLOCOS_SEM_FASE else valores[(bloco, k, "b")]
        linhas.append({
            "execucao": execucao, "codigo": codigo, "bloco": bloco, "k": k,
            "a_k": valores[(bloco, k, "a")], "b_k": b_k, "janela_treino": janela,
        })
    return linhas


def prever(t: np.ndarray, coeficientes: pl.DataFrame) -> np.ndarray:
    """Temperatura esperada só a partir das linhas da tabela de coeficientes de uma estação."""
    lista, valores = [], []
    for linha in coeficientes.iter_rows(named=True):
        lista.append((linha["bloco"], linha["k"], "a"))
        valores.append(linha["a_k"])
        if linha["bloco"] not in BLOCOS_SEM_FASE:
            lista.append((linha["bloco"], linha["k"], "b"))
            valores.append(linha["b_k"])
    return matriz(t, lista) @ np.array(valores)


def horas_desde_a_epoca() -> pl.Expr:
    return (pl.col("instante_utc") - pl.lit(EPOCA)).dt.total_hours().cast(pl.Float64)


def aprovadas() -> pl.DataFrame:
    return (
        pl.read_parquet(config.SILVER / "cobertura" / "*.parquet", hive_partitioning=False)
        .filter(pl.col("aprovada"))
        .select("codigo", "ano")
    )


def series_por_estacao(estacao_ano: pl.DataFrame) -> list[tuple]:
    """Horas usáveis de todos os anos aprovados de cada estação com anos suficientes."""
    medicoes = (
        pl.scan_parquet(config.SILVER / "medicao_horaria" / "**" / "*.parquet", hive_partitioning=False)
        .filter(pl.col("status").is_in(STATUS_USAVEL))
        .select("codigo", ano=pl.col("instante_utc").dt.year(), t=horas_desde_a_epoca(), y=pl.col("temp_c"))
        .join(estacao_ano.lazy(), on=["codigo", "ano"])
        .sort("codigo", "t")
        .collect()
    )
    return [
        (codigo, parte["t"].to_numpy(), parte["ano"].to_numpy(), parte["y"].to_numpy())
        for (codigo,), parte in medicoes.partition_by("codigo", as_dict=True, maintain_order=True).items()
    ]


def id_execucao() -> str:
    """Hash da silver e dos parâmetros: os mesmos dados geram a mesma versão dos coeficientes."""
    parametros = (config.K_DIA_CANDIDATOS, config.K_ANO_CANDIDATOS, config.ANO_TESTE_BASELINE,
                  config.ANOS_MINIMOS_BASELINE, config.ANOS_MINIMOS_TENDENCIA, config.TOLERANCIA_K_C, config.ANO_INICIAL, config.PERIODO_DIA_H, config.PERIODO_ANO_H)
    h = hashlib.sha256(repr(parametros).encode())
    arquivos = sorted((config.SILVER / "medicao_horaria").rglob("*.parquet")) + sorted((config.SILVER / "cobertura").glob("*.parquet"))
    for caminho in arquivos:
        h.update(caminho.relative_to(config.SILVER).as_posix().encode())
        h.update(caminho.read_bytes())
    return h.hexdigest()[:12]


def gravar_baseline(execucao: str, coeficientes: pl.DataFrame, estacao_ano: pl.DataFrame) -> dict[int, int]:
    """Real, esperado e resíduo de cada hora dos anos aprovados das estações com baseline."""
    por_estacao = coeficientes.partition_by("codigo", as_dict=True)
    com_baseline = coeficientes["codigo"].unique().implode()
    destino = config.GOLD / "baseline"
    destino.mkdir(parents=True, exist_ok=True)
    linhas_por_ano = {}
    for ano in sorted(estacao_ano["ano"].unique()):
        codigos = estacao_ano.filter((pl.col("ano") == ano) & pl.col("codigo").is_in(com_baseline))["codigo"]
        medicoes = (
            pl.read_parquet(config.SILVER / "medicao_horaria" / f"ano={ano}", hive_partitioning=False,
                            columns=["codigo", "instante_utc", "temp_c", "status"])
            .filter(pl.col("codigo").is_in(codigos.implode()))
            .sort("codigo", "instante_utc")
        )
        partes = []
        for (codigo,), parte in medicoes.partition_by("codigo", as_dict=True, maintain_order=True).items():
            t = parte.select(horas_desde_a_epoca()).to_series().to_numpy()
            partes.append(parte.with_columns(esperado_c=pl.Series(prever(t, por_estacao[(codigo,)]))))
        tabela = (
            pl.concat(partes)
            .select(
                pl.lit(execucao).alias("execucao"), "codigo", "instante_utc", "status",
                real_c=pl.when(pl.col("status").is_in(STATUS_USAVEL)).then(pl.col("temp_c")),
                esperado_c="esperado_c",
            )
            .with_columns(residuo_c=pl.col("real_c") - pl.col("esperado_c"))
        )
        tabela.write_parquet(destino / f"ano={ano}.parquet", statistics=True)
        linhas_por_ano[int(ano)] = tabela.height
    return linhas_por_ano


def resumo_do_teste(teste: pl.DataFrame) -> dict:
    """Critério de aceite: no ano de teste, a baseline erra menos que a ingênua."""
    pesos = pl.col("n_horas")
    agregado = teste.filter(pl.col("n_horas") > 0).select(
        estacoes=pl.len(),
        horas=pesos.sum(),
        mae_baseline_c=(pl.col("mae_c") * pesos).sum() / pesos.sum(),
        mae_ingenua_c=(pl.col("mae_ingenua_c") * pesos).sum() / pesos.sum(),
        fracao_estacoes_baseline_melhor=(pl.col("mae_c") < pl.col("mae_ingenua_c")).mean(),
    ).row(0, named=True)
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in agregado.items()}


def main() -> None:
    inicio = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    execucao = id_execucao()
    estacao_ano = aprovadas()
    series = [s for s in series_por_estacao(estacao_ano) if len(np.unique(s[2])) >= config.ANOS_MINIMOS_BASELINE]
    print(f"execução {execucao}: {len(series)} estações com pelo menos {config.ANOS_MINIMOS_BASELINE} anos aprovados")

    with ProcessPoolExecutor(max_workers=config.WORKERS) as executor:
        resultados = list(executor.map(_avaliar, series, chunksize=4))
    del series

    validacao = pl.DataFrame([linha for _, _, linhas, _ in resultados for linha in linhas], schema=ESQUEMA_VALIDACAO)
    k_dia, k_ano, ranking = escolher_k(validacao)
    print(ranking.head(6))
    print(f"K escolhido: k_dia={k_dia}, k_ano={k_ano}")

    coeficientes = pl.DataFrame(
        [linha for codigo, janela, _, coef in resultados
         for linha in linhas_de_coeficientes(execucao, codigo, janela, *coef[(k_dia, k_ano)])],
        schema=ESQUEMA_COEFICIENTES,
    )
    (config.GOLD / "coeficientes_fourier").mkdir(parents=True, exist_ok=True)
    coeficientes.write_parquet(config.GOLD / "coeficientes_fourier" / f"execucao={execucao}.parquet", statistics=True)
    validacao.sort("etapa", "codigo", "k_dia", "k_ano").write_parquet(config.GOLD / "baseline_validacao.parquet")

    teste = validacao.filter((pl.col("etapa") == "teste") & (pl.col("k_dia") == k_dia) & (pl.col("k_ano") == k_ano))
    resumo = resumo_do_teste(teste)
    print(f"teste {config.ANO_TESTE_BASELINE}: {resumo}")

    linhas_por_ano = gravar_baseline(execucao, coeficientes, estacao_ano)
    print(f"baseline: {sum(linhas_por_ano.values()):,} horas em {len(linhas_por_ano)} anos ({time.perf_counter() - t0:.0f} s)")

    registro = {
        "inicio": inicio.isoformat(timespec="seconds"),
        "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "etapa": "baseline",
        "execucao": execucao,
        "estacoes": coeficientes["codigo"].n_unique(),
        "estacoes_sem_tendencia": coeficientes["codigo"].n_unique()
        - coeficientes.filter(pl.col("bloco") == "tendencia")["codigo"].n_unique(),
        "k_dia": k_dia,
        "k_ano": k_ano,
        "teste": {"ano": config.ANO_TESTE_BASELINE, **resumo},
        "linhas_por_ano": linhas_por_ano,
    }
    with open(config.SILVER / "execucoes.jsonl", "a") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
