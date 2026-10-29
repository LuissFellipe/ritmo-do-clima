import numpy as np
import polars as pl
import pytest

from ritmo import config
from ritmo.anomalias import ESCALA_MAD, ESQUEMA_EVENTOS, classificar, eventos, vizinhanca, z_robusto

RNG = np.random.default_rng(11)
DIAS = 120


def test_z_de_ruido_normal_tem_desvio_1():
    h = np.arange(DIAS * 24)
    z, _ = z_robusto(h, 2.0 + 1.5 * RNG.standard_normal(len(h)))
    validos = z[~np.isnan(z)]
    # Mediana de |z| de uma normal padrão: 0,674.
    assert np.median(np.abs(validos)) == pytest.approx(0.674, abs=0.02)
    assert np.mean(np.abs(validos) > config.Z_ANOMALIA) < 1e-3


def test_z_bate_com_a_conta_direta_da_janela_centrada_no_dia():
    h = np.arange(24 * 10, DIAS * 24)  # começa no dia 10: a janela não depende do início da série
    r = RNG.standard_normal(len(h)) + np.sin(h / 500)
    z, _ = z_robusto(h, r)
    for hora in (24 * 50, 24 * 50 + 23, 24 * 77 + 5):
        meio_dia = hora - hora % 24 + 12
        janela = r[(h >= meio_dia - 360) & (h < meio_dia + 360)]
        mediana = np.median(janela)
        mad = np.median(np.abs(janela - mediana))
        assert z[h == hora][0] == pytest.approx((r[h == hora][0] - mediana) / (ESCALA_MAD * mad))


def test_sem_dado_suficiente_na_janela_nao_ha_z():
    h = np.arange(24 * 10)  # 240 horas, abaixo das 360 exigidas
    z, dias = z_robusto(h, RNG.standard_normal(len(h)))
    assert np.isnan(z).all()
    assert len(dias) == 0


def test_eventos_juntam_intervalo_curto_e_separam_por_sinal_e_intervalo_longo():
    h = np.arange(100)
    z = np.zeros(100)
    z[[10, 11, 15]] = 5     # intervalo de 3 h: um evento só
    z[[16, 17]] = -6        # sinal trocado: outro evento
    z[[40, 45]] = 5         # intervalo de 4 h: dois eventos
    ev = eventos("A", h, z)
    assert [(e["inicio_h"], e["fim_h"], e["horas_anomalas"]) for e in ev] == [
        (10, 15, 3), (16, 17, 2), (40, 40, 1), (45, 45, 1),
    ]
    assert ev[1]["pico_z"] == -6


def test_vizinhanca_pela_distancia_no_circulo_maximo():
    estacoes = pl.DataFrame({"codigo": ["A", "B", "C"], "lat": [-30.0, -31.0, -35.0], "lon": [-51.0, -51.0, -51.0]})
    pares = vizinhanca(estacoes)
    # 1° de latitude ≈ 111,2 km; C fica a 556 km de A.
    assert sorted(zip(pares["codigo"], pares["vizinha"])) == [("A", "B"), ("B", "A")]
    assert pares["distancia_km"][0] == pytest.approx(111.2, abs=0.1)


def test_regional_isolado_e_sem_vizinhas():
    pares = pl.DataFrame({"codigo": ["A", "B"], "vizinha": ["B", "A"], "distancia_km": [50.0, 50.0]})
    ev = pl.DataFrame(
        [
            {"codigo": "A", "inicio_h": 100, "fim_h": 110, "pico_h": 105, "pico_z": -7.0, "horas_anomalas": 8},
            # Começa 5 h depois do fim do de A: dentro da folga de 6 h.
            {"codigo": "B", "inicio_h": 115, "fim_h": 120, "pico_h": 118, "pico_z": -5.0, "horas_anomalas": 6},
            # Mesmo horário, sinal oposto: não conta.
            {"codigo": "B", "inicio_h": 300, "fim_h": 302, "pico_h": 301, "pico_z": 5.0, "horas_anomalas": 3},
            {"codigo": "A", "inicio_h": 300, "fim_h": 305, "pico_h": 301, "pico_z": -5.0, "horas_anomalas": 6},
            {"codigo": "C", "inicio_h": 300, "fim_h": 305, "pico_h": 301, "pico_z": 6.0, "horas_anomalas": 6},
        ],
        schema=ESQUEMA_EVENTOS,
    )
    dias = pl.DataFrame({"codigo": ["A", "B", "C"] * 2, "dia": [4, 4, 4, 12, 12, 12]})
    tipos = {(c, i): t for c, i, t in classificar(ev, pares, dias).select("codigo", "inicio_h", "tipo").iter_rows()}
    assert tipos == {
        ("A", 100): "regional", ("B", 115): "regional",
        ("A", 300): "isolado", ("B", 300): "isolado", ("C", 300): "sem_vizinhas",
    }
