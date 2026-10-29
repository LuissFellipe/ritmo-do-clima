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

# Baseline (RF12–RF13). K_dia e K_ano saem destas listas por validação temporal:
# o ano de validação escolhe K, e o seguinte, de teste, só mede o resultado.
K_DIA_CANDIDATOS = (1, 2, 3, 4, 6, 8)
K_ANO_CANDIDATOS = (1, 2, 3, 4)
ANO_TESTE_BASELINE = ANO_FINAL - 1  # último ano completo
# Ganhar menos que isto do melhor par não paga colunas a mais: fica o mais simples.
TOLERANCIA_K_C = 0.01
# Com um ano só, a tendência se confunde com o ciclo anual.
ANOS_MINIMOS_BASELINE = 2
# Com dois, a diferença entre um ano e outro vira tendência: 25 das 26 estações
# com mais de 0,2 °C/ano tinham só dois anos aprovados (A449: −0,75 °C/ano).
ANOS_MINIMOS_TENDENCIA = 3

# Processos por etapa paralela. O Lomb-Scargle de um ano ocupa por volta de 2 GB
# por série; com os 16 núcleos a RAM e o swap passam de 90% e o systemd-oomd
# mata o terminal.
WORKERS = 4

FAIXAS = {
    "temp_c": (-10.0, 50.0),
    "umid_pct": (0.0, 100.0),
    "rad_kj_m2": (0.0, None),
}
