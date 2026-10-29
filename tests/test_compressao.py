import numpy as np
import pytest

from ritmo.compressao import codificar, comprimir, decodificar

HORAS_NO_ANO = 8760
RNG = np.random.default_rng(7)


def tres_senoides(n=HORAS_NO_ANO):
    # Períodos que caem exatamente na grade k/N: 24 h (k=365), 12 h (k=730) e o ano (k=1).
    t = np.arange(n, dtype=float)
    y = 22 + 5 * np.cos(2 * np.pi * t / 24) + 1.5 * np.sin(2 * np.pi * t / 12) + 6 * np.cos(2 * np.pi * t / n)
    return t, y


def por_k(linhas):
    return {linha["k"]: linha for linha in linhas}


def test_ida_e_volta_guarda_cabecalho_e_coeficientes():
    _, y = tres_senoides()
    dados = codificar(48, y, 4)
    inicio, reconstruida = decodificar(dados)
    assert inicio == 48
    assert len(dados) == 8 + 4 * 10
    np.testing.assert_allclose(reconstruida, y, atol=1e-4)


def test_tres_senoides_cabem_em_quatro_coeficientes():
    # Média + 3 frequências: com k = 4 o erro é só o arredondamento para float32.
    t, y = tres_senoides()
    linhas = por_k(comprimir(t, y, ks=(3, 4)))
    assert linhas[4]["erro_max_c"] < 1e-4
    assert linhas[3]["erro_medio_c"] > 0.5


def test_erro_cai_com_k_numa_serie_ruidosa():
    t, y = tres_senoides()
    y = y + RNG.standard_normal(len(y))
    erros = [linha["erro_medio_c"] for linha in comprimir(t, y, ks=(10, 50, 200, 1000))]
    assert erros == sorted(erros, reverse=True)


def test_erro_so_conta_as_horas_com_dado():
    # Um valor absurdo numa hora sem dado não pode entrar no erro.
    t, y = tres_senoides()
    y[1000:1100] = np.nan
    linhas = por_k(comprimir(t, y, ks=(4,)))
    assert linhas[4]["n_horas"] == HORAS_NO_ANO - 100
    assert linhas[4]["erro_max_c"] < 0.2


def test_serie_que_comeca_depois_de_janeiro():
    # Começa na hora 2016 com 281 dias: os ciclos de 24 h e 12 h fecham na grade, e o
    # erro só fica zero se a reconstrução voltar para as horas certas.
    t, y = tres_senoides()
    t = t[2016:]
    y = 22 + 5 * np.cos(2 * np.pi * t / 24) + 1.5 * np.sin(2 * np.pi * t / 12)
    linhas = por_k(comprimir(t, y, ks=(3,)))
    assert linhas[3]["erro_max_c"] < 1e-4


def test_razao_compara_com_o_parquet_da_coluna():
    t, y = tres_senoides()
    linha = por_k(comprimir(t, y, ks=(10,)))[10]
    assert linha["bytes"] == 108
    assert linha["razao"] == pytest.approx(linha["bytes_parquet"] / 108)
    assert linha["bytes_parquet_decimos"] < linha["bytes_parquet"]
