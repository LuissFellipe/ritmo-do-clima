"""Leitura de um CSV de estação do INMET para o esquema único (RF04–RF06, RQ01, RQ03).

Cada CSV tem 8 linhas de metadados ("CHAVE:;valor"), a linha de colunas e as
medições horárias. O layout mudou ao longo dos anos; cada versão conhecida está
em LAYOUTS. Um cabeçalho que não bate com nenhuma para o pipeline (RQ01).
"""

import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from ritmo.qualidade import FalhaBloqueante

LINHAS_METADADOS = 8
AUSENTE = -9999.0


class LayoutDesconhecido(FalhaBloqueante):
    """RQ01: o CSV não segue nenhum layout conhecido."""


class TextoNaoNumerico(FalhaBloqueante):
    """RQ03: sobrou texto numa coluna numérica depois da conversão."""


@dataclass(frozen=True)
class Layout:
    nome: str
    coluna_data: str
    coluna_hora: str
    formato_instante: str
    # Converte o texto da hora para "HHMM" antes do strptime.
    limpar_hora: str


LAYOUTS = [
    # Até ~2018: "2010-01-01" e "00:00".
    Layout("iso", "DATA (YYYY-MM-DD)", "HORA (UTC)", "%Y-%m-%d %H%M", r":"),
    # Depois: "2025/01/01" e "0000 UTC".
    Layout("barra_utc", "DATA", "HORA UTC", "%Y/%m/%d %H%M", r" UTC"),
]

# Trecho do cabeçalho normalizado -> coluna do esquema único.
VARIAVEIS = {
    "TEMPERATURA DO AR - BULBO SECO": "temp_c",
    "UMIDADE RELATIVA DO AR, HORARIA": "umid_pct",
    "RADIACAO GLOBAL": "rad_kj_m2",
}

METADADOS = {
    "REGIAO": "regiao",
    "UF": "uf",
    "ESTACAO": "nome",
    "CODIGO (WMO)": "codigo",
    "LATITUDE": "lat",
    "LONGITUDE": "lon",
    "ALTITUDE": "altitude_m",
    "DATA DE FUNDACAO": "fundacao",
}


def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sem_acento).strip().upper()


def numero_br(texto: str) -> float | None:
    """Número com vírgula decimal. Metadado ilegível vira nulo (já houve "ALTITUDE:;F")."""
    try:
        return float(texto.strip().replace(",", "."))
    except ValueError:
        return None


def data_fundacao(texto: str) -> date | None:
    texto = texto.strip()
    for formato in ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            pass
    return None


def ler_metadados(linhas: list[str], arquivo: str) -> dict:
    meta = {}
    for linha in linhas:
        chave, _, valor = linha.partition(";")
        # Em 2019 o "Ã" chegou como "?" (REGI?O, ESTAC?O, FUNDAC?O).
        chave = normalizar(chave).rstrip(":").replace("?", "A")
        # "DATA DE FUNDACAO (YYYY-MM-DD)" e "DATA DE FUNDACAO" são a mesma chave.
        chave = re.sub(r" \(YYYY-MM-DD\)$", "", chave)
        if chave in METADADOS:
            meta[METADADOS[chave]] = valor.strip().rstrip(";")
    faltando = set(METADADOS.values()) - meta.keys()
    if faltando:
        raise LayoutDesconhecido(f"{arquivo}: metadados ausentes {sorted(faltando)}")
    return {
        "codigo": meta["codigo"],
        "nome": meta["nome"],
        "uf": meta["uf"],
        "regiao": meta["regiao"],
        "lat": numero_br(meta["lat"]),
        "lon": numero_br(meta["lon"]),
        "altitude_m": numero_br(meta["altitude_m"]),
        "fundacao": data_fundacao(meta["fundacao"]),
    }


def detectar_layout(colunas: list[str], arquivo: str) -> tuple[Layout, dict[str, str]]:
    normalizadas = [normalizar(c) for c in colunas]
    layout = next(
        (l for l in LAYOUTS if normalizadas[:2] == [l.coluna_data, l.coluna_hora]),
        None,
    )
    if layout is None:
        raise LayoutDesconhecido(f"{arquivo}: colunas de data/hora desconhecidas {colunas[:2]}")
    mapa = {}
    for trecho, destino in VARIAVEIS.items():
        achadas = [original for original, norm in zip(colunas, normalizadas) if norm.startswith(trecho)]
        if len(achadas) != 1:
            raise LayoutDesconhecido(f"{arquivo}: coluna '{trecho}' achada {len(achadas)} vezes")
        mapa[achadas[0]] = destino
    return layout, mapa


def para_float(coluna: str) -> pl.Expr:
    texto = pl.col(coluna).str.strip_chars().str.replace(",", ".", literal=True)
    return pl.when(texto == "").then(None).otherwise(texto).cast(pl.Float64, strict=False)


def ler_csv(conteudo: bytes, arquivo: str) -> tuple[dict, pl.DataFrame]:
    """Devolve (metadados da estação, medições no esquema único)."""
    texto = conteudo.decode("latin-1")
    linhas = texto.splitlines()
    meta = ler_metadados(linhas[:LINHAS_METADADOS], arquivo)

    bruto = pl.read_csv(
        io.StringIO("\n".join(linhas[LINHAS_METADADOS:])),
        separator=";",
        infer_schema=False,
        truncate_ragged_lines=True,
    )
    layout, mapa = detectar_layout(bruto.columns, arquivo)
    meta["layout"] = layout.nome

    coluna_data, coluna_hora = bruto.columns[0], bruto.columns[1]
    medicoes = bruto.select(
        (
            pl.col(coluna_data).str.strip_chars()
            + " "
            + pl.col(coluna_hora).str.strip_chars().str.replace_all(layout.limpar_hora, "")
        )
        .str.strptime(pl.Datetime("us", "UTC"), layout.formato_instante, strict=False)
        .alias("instante_utc"),
        *[para_float(original).alias(destino) for original, destino in mapa.items()],
        # Guarda o texto original para achar o que não virou número (RQ03).
        *[pl.col(original).alias(f"_{destino}_txt") for original, destino in mapa.items()],
    )

    if medicoes["instante_utc"].null_count():
        raise LayoutDesconhecido(f"{arquivo}: data/hora fora do formato {layout.formato_instante}")

    for destino in mapa.values():
        sobrou = medicoes.filter(
            pl.col(destino).is_null() & (pl.col(f"_{destino}_txt").str.strip_chars().fill_null("") != "")
        )
        if sobrou.height:
            raise TextoNaoNumerico(f"{arquivo}: {destino} com texto {sobrou[f'_{destino}_txt'].head(3).to_list()}")

    medicoes = medicoes.select(
        pl.lit(meta["codigo"]).alias("codigo"),
        "instante_utc",
        *[pl.when(pl.col(d) == AUSENTE).then(None).otherwise(pl.col(d)).alias(d) for d in VARIAVEIS.values()],
    )
    return meta, medicoes
