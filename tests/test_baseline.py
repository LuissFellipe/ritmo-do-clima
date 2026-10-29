import numpy as np
import polars as pl
import pytest

from ritmo import config
from ritmo.baseline import (
    CANDIDATOS, ESQUEMA_VALIDACAO, avaliar_estacao, escolher_k, ingenua, linhas_de_coeficientes,
    matriz, prever, termos,
)

RNG = np.random.default_rng(7)
ANO_H = 8760


def serie_conhecida(k_dia, k_ano, anos, ruido=0.0):
    """Horas desde a época até o fim do ano de teste, gerada por um modelo de coeficientes sorteados."""
    t = np.arange(ANO_H * anos, dtype=float)
    lista = termos(k_dia, k_ano)
    beta = RNG.normal(0, 1, len(lista))
    beta[0], beta[1] = 22.0, 0.03  # média e tendência (°C por ano)
    y = matriz(t, lista) @ beta + ruido * RNG.standard_normal(len(t))
    ano = config.ANO_TESTE_BASELINE - anos + 1 + (t // ANO_H).astype(int)
    return t, ano, y, beta


def test_ajuste_com_todos_os_anos_recupera_os_coeficientes():
    t, ano, y, beta = serie_conhecida(3, 2, anos=3, ruido=0.3)
    _, coeficientes = avaliar_estacao("X000", t, ano, y)
    lista, coef = coeficientes[(3, 2)]
    assert lista == termos(3, 2)
    assert coef == pytest.approx(beta, abs=0.02)


def test_com_dois_anos_a_tendencia_fica_de_fora():
    t, ano, y, _ = serie_conhecida(2, 1, anos=2, ruido=0.3)
    lista, _ = avaliar_estacao("X000", t, ano, y)[1][(2, 1)]
    assert lista == termos(2, 1, com_tendencia=False)


def test_k_menor_que_o_da_serie_erra_mais_na_validacao():
    t, ano, y, _ = serie_conhecida(3, 2, anos=4, ruido=0.3)
    linhas, _ = avaliar_estacao("X000", t, ano, y)
    erro = {(l["k_dia"], l["k_ano"]): l["mae_c"] for l in linhas if l["etapa"] == "validacao"}
    # Ruído normal de desvio 0,3 tem erro médio absoluto de 0,3·√(2/π) ≈ 0,239.
    assert erro[(3, 2)] == pytest.approx(0.3 * np.sqrt(2 / np.pi), rel=0.05)
    assert erro[(1, 1)] > erro[(3, 2)] + 0.1


def test_validacao_e_teste_so_com_anos_suficientes_antes():
    # Dois anos: o de validação tem um ano de treino antes e fica de fora; o teste tem dois.
    t, ano, y, _ = serie_conhecida(1, 1, anos=2)
    linhas, coeficientes = avaliar_estacao("X000", t, ano, y)
    assert {l["etapa"] for l in linhas} == set()
    t, ano, y, _ = serie_conhecida(1, 1, anos=3)
    linhas, _ = avaliar_estacao("X000", t, ano, y)
    assert {l["etapa"] for l in linhas} == {"teste"}
    assert len(coeficientes) == len(CANDIDATOS)


def test_ingenua_usa_a_mesma_hora_do_dia_anterior():
    t = np.array([0.0, 1, 24, 25, 49])
    y = np.array([10.0, 11, 12, 13, 14])
    assert ingenua(t, y, np.array([24.0, 25, 50, 49])) == pytest.approx([10, 11, np.nan, 13], nan_ok=True)


def test_teste_mede_as_duas_baselines_nas_mesmas_horas():
    t, ano, y, _ = serie_conhecida(2, 1, anos=3, ruido=0.5)
    sem_lacuna = avaliar_estacao("X000", t, ano, y)[0]
    manter = ~((ano == config.ANO_TESTE_BASELINE) & (np.arange(len(t)) % 48 < 24))  # dia sim, dia não
    linhas, _ = avaliar_estacao("X000", t[manter], ano[manter], y[manter])
    teste = [l for l in linhas if l["etapa"] == "teste"][0]
    # Sem o dia anterior, nenhuma hora do ano de teste é comparável.
    assert teste["n_horas"] == 0
    assert [l for l in sem_lacuna if l["etapa"] == "teste"][0]["n_horas"] == ANO_H  # o 1º dia usa o 31/12 do ano anterior


def validacao_com(erros):
    return pl.DataFrame(
        [
            {"codigo": c, "etapa": "validacao", "ano": 2024, "k_dia": kd, "k_ano": ka, "n_horas": 100,
             "mae_c": mae, "mae_ingenua_c": None}
            for c in ("A", "B", "C")
            for (kd, ka), mae in erros.items()
        ],
        schema=ESQUEMA_VALIDACAO,
    )


def test_escolher_k_prefere_menos_colunas_no_empate():
    assert escolher_k(validacao_com({(4, 2): 1.0, (3, 2): 1.0, (1, 1): 2.0}))[:2] == (3, 2)


def test_escolher_k_troca_milesimos_de_grau_por_menos_colunas():
    erros = {(8, 4): 1.970, (4, 4): 1.979, (2, 4): 1.987, (1, 1): 2.2}
    assert escolher_k(validacao_com(erros))[:2] == (4, 4)


def test_previsao_pela_tabela_de_coeficientes_e_a_do_modelo():
    t, ano, y, _ = serie_conhecida(4, 3, anos=3, ruido=0.3)
    _, coeficientes = avaliar_estacao("X000", t, ano, y)
    lista, coef = coeficientes[(4, 3)]
    tabela = pl.DataFrame(linhas_de_coeficientes("e1", "X000", "2023–2025", lista, coef))
    assert tabela.filter(pl.col("bloco") == "tendencia")["b_k"].to_list() == [None]
    assert prever(t, tabela) == pytest.approx(matriz(t, lista) @ coef, abs=1e-9)
