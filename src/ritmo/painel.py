"""Painel: agrega a silver nos CSVs que o painel do projeto lê.

Cada arquivo é pequeno o bastante para ser enviado inteiro ao painel
(o maior, o ciclo diário, tem uma linha por estação e hora do dia).
"""

import duckdb

from ritmo import config
from ritmo.silver import STATUS_USAVEL

MEDICOES = f"read_parquet('{config.SILVER}/medicao_horaria/**/*.parquet', hive_partitioning=false)"
COBERTURA = f"read_parquet('{config.SILVER}/cobertura/*.parquet')"
DIM_ESTACAO = f"read_parquet('{config.SILVER}/dim_estacao.parquet')"
USAVEL = "status in (" + ", ".join(f"'{s}'" for s in STATUS_USAVEL) + ")"
# Média em decimal exato: em double, a soma paralela muda de ordem a cada
# execução e o arredondamento da segunda casa oscilava entre rodadas.
MEDIA_TEMP = "round(avg(temp_c::decimal(18, 6)), 2) temp_media"

CONSULTAS = {
    "cobertura_por_ano": f"""
        select ano, count(*) estacoes, sum(aprovada::int) aprovadas,
               round(median(fracao_ok), 4) mediana_cobertura
        from {COBERTURA} group by ano order by ano
    """,
    "horas_por_status": f"""
        select year(instante_utc) ano, status, count(*) horas
        from {MEDICOES} group by all order by ano, status
    """,
    # C891 (Antártida) fica no cadastro sem cobertura: foi excluída da silver.
    "estacoes": f"""
        with cob as (
            select codigo, round(avg(fracao_ok), 4) cobertura_media,
                   sum(aprovada::int) anos_aprovados, count(*) anos
            from {COBERTURA} group by codigo
        )
        select d.codigo, d.nome, d.uf, d.regiao, round(d.lat, 4) lat, round(d.lon, 4) lon, d.altitude_m,
               d.primeiro_ano, d.ultimo_ano, cob.anos, cob.anos_aprovados, cob.cobertura_media,
               len(d.avisos) n_avisos, array_to_string(d.avisos, '; ') avisos
        from {DIM_ESTACAO} d left join cob using (codigo)
        order by codigo
    """,
    "ciclo_diario": f"""
        select codigo, hour(instante_utc) hora_utc, {MEDIA_TEMP}, count(*) horas
        from {MEDICOES} where {USAVEL} group by all order by codigo, hora_utc
    """,
    "ciclo_anual": f"""
        select codigo, month(instante_utc) mes, {MEDIA_TEMP}, count(*) horas
        from {MEDICOES} where {USAVEL} group by all order by codigo, mes
    """,
}


def main() -> None:
    destino = config.DADOS / "painel"
    destino.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    # Hora e mês saem em UTC, como na silver, e não no fuso da máquina.
    con.sql("set TimeZone = 'UTC'")
    for nome, consulta in CONSULTAS.items():
        caminho = destino / f"{nome}.csv"
        con.sql(f"copy ({consulta}) to '{caminho}' (header)")
        linhas = con.execute(f"select count(*) from read_csv('{caminho}')").fetchall()[0][0]
        print(f"{caminho.relative_to(config.RAIZ)}: {linhas:,} linhas")


if __name__ == "__main__":
    main()
