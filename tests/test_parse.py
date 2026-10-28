from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from ritmo.parse import LayoutDesconhecido, TextoNaoNumerico, ler_csv

AMOSTRAS = Path(__file__).parent / "amostras"
ISO = (AMOSTRAS / "layout_iso_A001_2010.csv").read_bytes()
BARRA = (AMOSTRAS / "layout_barra_utc_A001_2025.csv").read_bytes()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def trocar_linha(conteudo: bytes, indice: int, antes: bytes, depois: bytes) -> bytes:
    linhas = conteudo.split(b"\n")
    linhas[indice] = linhas[indice].replace(antes, depois, 1)
    return b"\n".join(linhas)


def test_os_dois_layouts_viram_o_mesmo_esquema():
    _, antigo = ler_csv(ISO, "iso")
    _, novo = ler_csv(BARRA, "barra")
    assert antigo.schema == novo.schema
    assert antigo.columns == ["codigo", "instante_utc", "temp_c", "umid_pct", "rad_kj_m2"]


def test_layout_iso():
    meta, df = ler_csv(ISO, "iso")
    assert meta["layout"] == "iso"
    assert meta["codigo"] == "A001"
    assert meta["uf"] == "DF"
    assert meta["fundacao"] == date(2000, 5, 7)
    primeira = df.row(0, named=True)
    assert primeira["instante_utc"] == utc(2010, 7, 1, 16)
    assert primeira["temp_c"] == pytest.approx(22.6)
    assert primeira["rad_kj_m2"] == pytest.approx(2743.5)


def test_layout_barra_utc():
    meta, df = ler_csv(BARRA, "barra")
    assert meta["layout"] == "barra_utc"
    assert meta["lat"] == pytest.approx(-15.789, abs=0.01)
    primeira = df.row(0, named=True)
    assert primeira["instante_utc"] == utc(2025, 1, 1, 0)
    assert primeira["temp_c"] == pytest.approx(20.8)
    assert primeira["umid_pct"] == pytest.approx(92)
    # Radiação vem vazia à noite no layout novo.
    assert primeira["rad_kj_m2"] is None


def test_menos_9999_vira_nulo():
    conteudo = trocar_linha(ISO, 9, b";22,6;", b";-9999;")
    _, df = ler_csv(conteudo, "iso")
    assert df["temp_c"][0] is None


def test_cabecalho_desconhecido_para_o_pipeline():
    conteudo = ISO.replace(b"HORA (UTC)", b"HORA (LOCAL)")
    with pytest.raises(LayoutDesconhecido, match="arquivo-teste"):
        ler_csv(conteudo, "arquivo-teste")


def test_texto_em_coluna_numerica_para_o_pipeline():
    conteudo = trocar_linha(BARRA, 9, b";20,8;", b";erro;")
    with pytest.raises(TextoNaoNumerico, match="temp_c"):
        ler_csv(conteudo, "barra")


def test_metadado_ilegivel_vira_nulo():
    # Caso real: S108 (São Bento/MA), 2016, com "ALTITUDE:;F".
    conteudo = trocar_linha(ISO, 6, b"1159,54", b"F")
    meta, _ = ler_csv(conteudo, "iso")
    assert meta["altitude_m"] is None


def test_metadados_com_acento_perdido():
    # Caso real: arquivos de 2019 trazem "REGI?O:", "ESTAC?O:" e "DATA DE FUNDAC?O:".
    conteudo = (
        BARRA.replace(b"REGIAO:", b"REGI?O:")
        .replace(b"ESTACAO:", b"ESTAC?O:")
        .replace(b"DATA DE FUNDACAO:", b"DATA DE FUNDAC?O:")
    )
    meta, _ = ler_csv(conteudo, "barra")
    assert meta["regiao"] == "CO"
    assert meta["nome"] == "BRASILIA"
    assert meta["fundacao"] is not None
