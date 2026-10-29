"""Parâmetros do pipeline. Tudo que muda entre execuções mora aqui."""

from datetime import date
from pathlib import Path

ANO_INICIAL = 2015
ANO_FINAL = date.today().year

URL_ZIP = "https://portal.inmet.gov.br/uploads/dadoshistoricos/{ano}.zip"

# O portal derruba a conexão de user-agents genéricos (python-httpx, curl sem -A).
USER_AGENT = "ritmo-do-clima/0.1 (projeto de portfolio; download semanal)"

RAIZ = Path(__file__).resolve().parents[2]
DADOS = RAIZ / "data"
BRONZE = DADOS / "bronze"
SILVER = DADOS / "silver"
GOLD = DADOS / "gold"
# CSVs da página pública: versionados, porque o GitHub Pages não roda o pipeline.
SITE_DADOS = RAIZ / "site" / "data"

# Lacunas de até este tamanho são interpoladas na silver (RF08).
LACUNA_MAX_INTERPOLADA_H = 3
# Estação-ano abaixo desta cobertura não segue para a gold (RF09).
COBERTURA_MINIMA = 0.80
# Valor idêntico por este número de horas marca o trecho como suspeito (RQ07).
HORAS_SENSOR_TRAVADO = 6
# Salto de temperatura entre horas que gera aviso (RQ06).
SALTO_TEMPERATURA_C = 10.0
# Fração de valores fora da faixa física que bloqueia o pipeline (RQ04).
FORA_DA_FAIXA_BLOQUEANTE = 0.001

# Espectro (RF10–RF11). O ano tropical tem 8.766 h; o ciclo anual só é
# ajustado quando a série cobre quase o ano inteiro.
PERIODO_DIA_H = 24.0
PERIODO_ANO_H = 8766.0
DIAS_MINIMOS_CICLO_ANUAL = 360
PICOS_POR_SERIE = 5

FAIXAS = {
    "temp_c": (-10.0, 50.0),
    "umid_pct": (0.0, 100.0),
    "rad_kj_m2": (0.0, None),
}
