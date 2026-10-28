from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from ritmo.qualidade import FalhaBloqueante, checar_ano, checar_estacoes, dentro_do_brasil
from ritmo.silver import classificar, cobertura, escolher_arquivos, montar_grade

INICIO = datetime(2025, 1, 1, tzinfo=timezone.utc)


def serie(temperaturas, codigo="T001"):
    return pl.DataFrame(
        {
            "codigo": codigo,
            "instante_utc": [INICIO + timedelta(hours=h) for h in range(len(temperaturas))],
            "temp_c": temperaturas,
            "umid_pct": [50.0] * len(temperaturas),
            "rad_kj_m2": [None] * len(temperaturas),
        },
        schema_overrides={"instante_utc": pl.Datetime("us", "UTC"), "rad_kj_m2": pl.Float64},
    )


def status(temperaturas):
    return classificar(montar_grade(serie(temperaturas)))["status"].to_list()


def test_lacuna_curta_e_interpolada():
    grade = classificar(montar_grade(serie([10.0, None, None, 13.0])))
    assert grade["status"].to_list() == ["ok", "interpolada", "interpolada", "ok"]
    assert grade["temp_c"].to_list() == pytest.approx([10.0, 11.0, 12.0, 13.0])


def test_lacuna_longa_fica_vazia():
    temperaturas = [10.0, None, None, None, None, 15.0]
    assert status(temperaturas) == ["ok"] + ["faltante"] * 4 + ["ok"]


def test_lacuna_na_borda_nao_e_interpolada():
    assert status([None, 11.0, 12.0]) == ["faltante", "ok", "ok"]


def test_hora_ausente_do_arquivo_entra_na_grade():
    df = serie([10.0, 11.0, 12.0, 13.0]).filter(pl.col("temp_c") != 12.0)
    grade = classificar(montar_grade(df))
    assert grade.height == 4
    assert grade["status"].to_list() == ["ok", "ok", "interpolada", "ok"]


def test_sensor_travado_vira_suspeita():
    temperaturas = [10.0, 11.0] + [12.0] * 6 + [13.0]
    assert status(temperaturas) == ["ok", "ok"] + ["suspeita"] * 6 + ["ok"]


def test_cinco_horas_iguais_nao_sao_suspeitas():
    assert "suspeita" not in status([10.0] + [12.0] * 5 + [13.0])


def test_hora_duplicada_vira_uma_linha():
    df = serie([10.0, 11.0, 12.0])
    grade = classificar(montar_grade(pl.concat([df, df.slice(1, 1)])))
    assert grade.height == 3
    assert grade["status"].to_list() == ["ok", "duplicada", "ok"]


def test_cobertura_conta_sobre_o_ano_inteiro():
    grade = classificar(montar_grade(serie([20.0 + (h % 24) / 10 for h in range(24 * 300)])))
    linha = cobertura(grade, 2025).row(0, named=True)
    assert linha["fracao_ok"] == pytest.approx(300 / 365)
    assert linha["aprovada"]


def test_chave_repetida_bloqueia():
    grade = classificar(montar_grade(serie([10.0, 11.0])))
    repetida = pl.concat([grade, grade.head(1)])
    with pytest.raises(FalhaBloqueante, match="repetidas"):
        checar_ano(2025, repetida, cobertura(grade, 2025), {}, pl.DataFrame({"codigo": [], "horas_revisadas": []}))


def test_muitos_valores_fora_da_faixa_bloqueiam():
    grade = classificar(montar_grade(serie([10.0, 11.0])))
    with pytest.raises(FalhaBloqueante, match="temp_c"):
        checar_ano(2025, grade, cobertura(grade, 2025), {"temp_c": (5, 1000)}, pl.DataFrame({"codigo": [], "horas_revisadas": []}))


def test_estacao_que_mudou_de_lugar_gera_aviso():
    base = {"codigo": "T001", "nome": "X", "uf": "DF", "regiao": "CO", "altitude_m": 1000.0,
            "fundacao": None, "layout": "iso", "arquivo": "a"}
    estacao_ano = pl.DataFrame([
        {**base, "ano": 2024, "lat": -15.78, "lon": -47.92},
        {**base, "ano": 2025, "lat": -15.90, "lon": -47.92},
    ])
    dim = checar_estacoes(estacao_ano)
    assert dim["avisos"][0].to_list() == ["RQ09 coordenada mudou entre anos"]
    assert dim["lat"][0] == pytest.approx(-15.90)


def test_estacao_na_antartida_fica_fora_do_brasil():
    # Caso real: C891 (Criosfera), cadastrada como SP, lat -84.
    estacoes = pl.DataFrame({"codigo": ["A001", "C891"], "lat": [-15.79, -84.0], "lon": [-47.93, -79.49]})
    assert estacoes.filter(dentro_do_brasil())["codigo"].to_list() == ["A001"]


def test_ano_corrente_fica_com_o_retrato_mais_recente():
    # Caso real: o ZIP de 2026 traz jan–ago e jan–set de cada estação.
    nomes = [
        "INMET_CO_DF_A001_BRASILIA_01-01-2026_A_31-08-2026.CSV",
        "INMET_CO_DF_A001_BRASILIA_01-01-2026_A_30-09-2026.CSV",
        "INMET_S_RS_B827_BAGE - CENTRO_24-02-2026_A_31-08-2026.CSV",
        "INMET_S_RS_B827_BAGE - CENTRO_24-02-2026_A_30-09-2026.CSV",
    ]
    assert escolher_arquivos(nomes) == [nomes[1], nomes[3]]


def test_cobertura_do_ano_corrente_vai_ate_o_ultimo_dia_publicado():
    grade = classificar(montar_grade(serie([20.0 + (h % 24) / 10 for h in range(24 * 273)])))
    linha = cobertura(grade, 2026, date(2026, 9, 30)).row(0, named=True)
    assert linha["fracao_ok"] == pytest.approx(1.0)
