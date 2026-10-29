import numpy as np
import pytest

from ritmo.espectro import analisar

HORAS_NO_ANO = 8760
RNG = np.random.default_rng(42)


def por_papel(linhas):
    return {linha["papel"]: linha for linha in linhas}


def senoide(periodo, amplitude, pico_h, n=HORAS_NO_ANO, ruido=0.0):
    t = np.arange(n, dtype=float)
    y = 20 + amplitude * np.cos(2 * np.pi * (t - pico_h) / periodo) + ruido * RNG.standard_normal(n)
    return t, y


def com_lacunas(y, fracao):
    y = y.copy()
    y[RNG.random(len(y)) < fracao] = np.nan
    return y


def test_senoide_de_24h_sem_lacuna_vai_pela_fft():
    t, y = senoide(24, 5.0, pico_h=18, ruido=0.5)
    linhas = por_papel(analisar(t, y))
    assert linhas["pico_1"]["metodo"] == "fft"
    assert linhas["pico_1"]["periodo_h"] == pytest.approx(24, abs=0.1)
    assert linhas["ciclo_24h"]["amplitude_c"] == pytest.approx(5.0, abs=0.05)
    assert linhas["ciclo_24h"]["fase_h"] == pytest.approx(18, abs=0.1)


def test_senoide_de_24h_com_10pct_de_lacunas_vai_por_lomb_scargle():
    t, y = senoide(24, 5.0, pico_h=18, ruido=0.5)
    linhas = por_papel(analisar(t, com_lacunas(y, 0.10)))
    assert linhas["pico_1"]["metodo"] == "lomb-scargle"
    assert linhas["pico_1"]["periodo_h"] == pytest.approx(24, abs=0.1)
    assert linhas["ciclo_24h"]["amplitude_c"] == pytest.approx(5.0, abs=0.05)
    assert linhas["ciclo_24h"]["fase_h"] == pytest.approx(18, abs=0.1)


def test_senoide_pura_explica_toda_a_variancia():
    t, y = senoide(24, 3.0, pico_h=6)
    assert por_papel(analisar(t, y))["ciclo_24h"]["fracao_variancia"] == pytest.approx(1.0, abs=1e-9)


def test_ciclo_do_dia_e_do_ano_juntos():
    # Máximo anual em 15 de janeiro (hora 336) e diário às 18h UTC.
    t, dia = senoide(24, 4.0, pico_h=18)
    _, ano = senoide(8766, 6.0, pico_h=336)
    linhas = por_papel(analisar(t, dia + ano - 20))
    assert linhas["ciclo_24h"]["amplitude_c"] == pytest.approx(4.0, abs=0.05)
    assert linhas["ciclo_anual"]["amplitude_c"] == pytest.approx(6.0, abs=0.1)
    assert linhas["ciclo_anual"]["fase_h"] / 24 == pytest.approx(14, abs=1)
    picos = {round(linhas[f"pico_{i}"]["periodo_h"]) for i in (1, 2)}
    assert picos == {24, 8760}


def test_serie_curta_nao_tem_ciclo_anual():
    t, y = senoide(24, 5.0, pico_h=18, n=24 * 200)
    linhas = por_papel(analisar(t, y))
    assert "ciclo_anual" not in linhas
    assert len([p for p in linhas if p.startswith("pico_")]) == 5
