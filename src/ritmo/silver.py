"""Silver: medições horárias em grade contínua, com status e cobertura (RF07–RF09).

O status de cada hora se refere à temperatura, a variável principal. Umidade e
radiação seguem na tabela com nulo onde faltam; a radiação é nula ou zero à
noite por natureza e não conta como falha (RQ05).
"""

import json
import re
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from ritmo import config
from ritmo.parse import ler_csv
from ritmo.qualidade import FalhaBloqueante, checar_ano, checar_estacoes, dentro_do_brasil

STATUS_USAVEL = ["ok", "interpolada", "duplicada"]


PERIODO_NO_NOME = re.compile(r"_(\d{2}-\d{2}-\d{4})_A_(\d{2}-\d{2}-\d{4})\.CSV$", re.IGNORECASE)


def periodo(nome: str) -> tuple[date, date]:
    inicio, fim = PERIODO_NO_NOME.search(nome).groups()
    return tuple(datetime.strptime(d, "%d-%m-%Y").date() for d in (inicio, fim))


def escolher_arquivos(nomes: list[str]) -> list[str]:
    """No ano corrente o ZIP traz retratos acumulados da mesma estação
    (jan–ago e jan–set). Fica só o mais recente de cada início."""
    mais_recente = {}
    for nome in nomes:
        codigo = Path(nome).name.split("_")[3]
        inicio, fim = periodo(nome)
        chave = (codigo, inicio)
        if chave not in mais_recente or fim > periodo(mais_recente[chave])[1]:
            mais_recente[chave] = nome
    return sorted(mais_recente.values())


def ler_zip(ano: int) -> tuple[pl.DataFrame, pl.DataFrame, date]:
    """Devolve estações, medições e o último dia coberto pelo ZIP."""
    estacoes, medicoes = [], []
    with zipfile.ZipFile(config.BRONZE / f"{ano}.zip") as z:
        nomes = escolher_arquivos([n for n in z.namelist() if n.upper().endswith(".CSV")])
        for nome in nomes:
            meta, df = ler_csv(z.read(nome), nome)
            estacoes.append({**meta, "ano": ano, "arquivo": nome})
            medicoes.append(df)
    ultimo_dia = max(periodo(n)[1] for n in nomes)
    return pl.DataFrame(estacoes), pl.concat(medicoes), ultimo_dia


def anular_fora_da_faixa(medicoes: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, tuple[int, int]]]:
    """RQ04: valor fisicamente impossível vira nulo; a contagem vai para o relatório."""
    contagens, expressoes = {}, []
    for coluna, (minimo, maximo) in config.FAIXAS.items():
        fora = pl.lit(False)
        if minimo is not None:
            fora = fora | (pl.col(coluna) < minimo)
        if maximo is not None:
            fora = fora | (pl.col(coluna) > maximo)
        contagens[coluna] = medicoes.select(fora.sum(), pl.col(coluna).count()).row(0)
        expressoes.append(pl.when(fora).then(None).otherwise(pl.col(coluna)).alias(coluna))
    return medicoes.with_columns(expressoes), contagens


def montar_grade(medicoes: pl.DataFrame) -> pl.DataFrame:
    """Uma linha por estação e hora, do primeiro ao último instante do arquivo."""
    contagem = medicoes.group_by("codigo", "instante_utc").len("vezes")
    unicas = medicoes.unique(["codigo", "instante_utc"], keep="first", maintain_order=True)

    grade = (
        unicas.group_by("codigo")
        .agg(pl.datetime_ranges(pl.col("instante_utc").min(), pl.col("instante_utc").max(), "1h").first())
        .explode("instante_utc")
    )
    return (
        grade.join(unicas, on=["codigo", "instante_utc"], how="left")
        .join(contagem, on=["codigo", "instante_utc"], how="left")
        .sort("codigo", "instante_utc")
    )


def classificar(grade: pl.DataFrame) -> pl.DataFrame:
    por_estacao = lambda e: e.over("codigo")
    falta = pl.col("temp_c").is_null()

    trecho_nulo = por_estacao(falta.rle_id())
    tamanho_lacuna = pl.len().over("codigo", "_trecho_nulo")
    interpolada = por_estacao(pl.col("temp_c").interpolate())

    trecho_igual = por_estacao(pl.col("temp_c").rle_id())
    tamanho_igual = pl.len().over("codigo", "_trecho_igual")

    return (
        grade.with_columns(_trecho_nulo=trecho_nulo, _trecho_igual=trecho_igual, _interp=interpolada)
        .with_columns(
            _lacuna=pl.when(falta).then(tamanho_lacuna),
            _travado=pl.when(~falta).then(tamanho_igual),
        )
        .with_columns(
            status=pl.when(falta & (pl.col("_lacuna") <= config.LACUNA_MAX_INTERPOLADA_H) & pl.col("_interp").is_not_null())
            .then(pl.lit("interpolada"))
            .when(falta)
            .then(pl.lit("faltante"))
            .when(pl.col("_travado") >= config.HORAS_SENSOR_TRAVADO)
            .then(pl.lit("suspeita"))
            .when(pl.col("vezes") > 1)
            .then(pl.lit("duplicada"))
            .otherwise(pl.lit("ok")),
            salto=por_estacao(pl.col("temp_c").diff().abs()) > config.SALTO_TEMPERATURA_C,
        )
        .with_columns(
            temp_c=pl.when(pl.col("status") == "interpolada").then(pl.col("_interp")).otherwise(pl.col("temp_c")),
        )
        .drop("_trecho_igual", "_interp", "_travado", "vezes")
    )


def cobertura(grade: pl.DataFrame, ano: int, ultimo_dia: date | None = None) -> pl.DataFrame:
    """Fração de horas usáveis entre 1º de janeiro e o último dia publicado
    (o ano inteiro, exceto no ano corrente)."""
    ultimo_dia = min(ultimo_dia or date(ano, 12, 31), date(ano, 12, 31))
    horas_no_ano = ((ultimo_dia + timedelta(days=1)) - date(ano, 1, 1)).days * 24
    return (
        grade.group_by("codigo")
        .agg(
            horas_ok=pl.col("status").is_in(STATUS_USAVEL).sum(),
            horas_faltantes=(pl.col("status") == "faltante").sum(),
            horas_interpoladas=(pl.col("status") == "interpolada").sum(),
            horas_suspeitas=(pl.col("status") == "suspeita").sum(),
            horas_duplicadas=(pl.col("status") == "duplicada").sum(),
            lacunas_longas=pl.col("_trecho_nulo").filter(pl.col("status") == "faltante").n_unique(),
            saltos=pl.col("salto").sum(),
        )
        .with_columns(
            ano=pl.lit(ano),
            fracao_ok=pl.col("horas_ok") / horas_no_ano,
        )
        .with_columns(aprovada=pl.col("fracao_ok") >= config.COBERTURA_MINIMA)
        .sort("codigo")
    )


def horas_revisadas(nova: pl.DataFrame, ano: int) -> pl.DataFrame:
    """RQ10: compara com a silver anterior do mesmo ano, se existir."""
    anterior = config.SILVER / "medicao_horaria" / f"ano={ano}"
    vazio = pl.DataFrame(schema={"codigo": pl.String, "horas_revisadas": pl.UInt32})
    if not anterior.exists():
        return vazio
    velha = pl.read_parquet(anterior, hive_partitioning=False).select("codigo", "instante_utc", "temp_c")
    return (
        nova.select("codigo", "instante_utc", "temp_c")
        .join(velha, on=["codigo", "instante_utc"], suffix="_antes")
        .filter(pl.col("temp_c").ne_missing(pl.col("temp_c_antes")))
        .group_by("codigo")
        .len("horas_revisadas")
    )


def gravar_particionado(df: pl.DataFrame, pasta, ano: int) -> None:
    destino = pasta / f"ano={ano}"
    if destino.exists():
        for antigo in sorted(destino.rglob("*.parquet")):
            antigo.unlink()
    for (uf,), parte in df.partition_by("uf", as_dict=True, maintain_order=True).items():
        caminho = destino / f"uf={uf}"
        caminho.mkdir(parents=True, exist_ok=True)
        parte.drop("uf").write_parquet(caminho / "parte.parquet", statistics=True)


def processar_ano(ano: int) -> dict:
    estacoes, medicoes, ultimo_dia = ler_zip(ano)
    # Fora do Brasil não entra na silver (RQ09); coordenada ausente segue, com aviso.
    excluidas = estacoes.filter(dentro_do_brasil().not_())["codigo"].unique().sort().to_list()
    medicoes = medicoes.filter(~pl.col("codigo").is_in(excluidas))
    total = medicoes.height
    medicoes, fora_da_faixa = anular_fora_da_faixa(medicoes)

    grade = classificar(montar_grade(medicoes))
    cob = cobertura(grade, ano, ultimo_dia)
    revisadas = horas_revisadas(grade, ano)
    relatorio = checar_ano(ano, grade, cob, fora_da_faixa, revisadas)

    uf_por_codigo = estacoes.unique("codigo", keep="last").select("codigo", "uf")
    tabela = (
        grade.join(uf_por_codigo, on="codigo", how="left")
        .select("codigo", "instante_utc", "temp_c", "umid_pct", "rad_kj_m2", "status", "uf")
    )
    gravar_particionado(tabela, config.SILVER / "medicao_horaria", ano)

    for nome, df in [("estacao_ano", estacoes), ("cobertura", cob), ("qualidade", relatorio)]:
        pasta = config.SILVER / nome
        pasta.mkdir(parents=True, exist_ok=True)
        df.write_parquet(pasta / f"ano={ano}.parquet")

    return {
        "ano": ano,
        "arquivos": estacoes.height,
        "ultimo_dia": ultimo_dia.isoformat(),
        "linhas_lidas": total,
        "linhas_silver": tabela.height,
        "estacoes_aprovadas": int(cob["aprovada"].sum()),
        "fora_da_faixa": {c: f for c, (f, _) in fora_da_faixa.items()},
        "excluidas_fora_do_brasil": excluidas,
    }


def gravar_dim_estacao() -> pl.DataFrame:
    estacao_ano = pl.read_parquet(config.SILVER / "estacao_ano" / "*.parquet", hive_partitioning=False)
    dim = checar_estacoes(estacao_ano)
    dim.write_parquet(config.SILVER / "dim_estacao.parquet")
    return dim


def main(anos=None) -> None:
    anos = anos or sorted(int(p.stem) for p in config.BRONZE.glob("*.zip"))
    inicio = datetime.now(timezone.utc)
    resultados, erro = [], None
    try:
        for ano in anos:
            r = processar_ano(ano)
            resultados.append(r)
            print(
                f"{ano}: {r['arquivos']} arquivos, {r['linhas_silver']:,} linhas, "
                f"{r['estacoes_aprovadas']} estações aprovadas, fora da faixa {r['fora_da_faixa']}, "
                f"excluídas {r['excluidas_fora_do_brasil']}"
            )
        dim = gravar_dim_estacao()
        print(f"dim_estacao: {dim.height} estações, {dim.filter(pl.col('avisos').list.len() > 0).height} com aviso RQ09")
    except FalhaBloqueante as e:
        erro = str(e)
        raise
    finally:
        registro = {
            "inicio": inicio.isoformat(timespec="seconds"),
            "fim": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "etapa": "silver",
            "anos": resultados,
            "testes_ok": erro is None,
            "erro": erro,
        }
        config.SILVER.mkdir(parents=True, exist_ok=True)
        with open(config.SILVER / "execucoes.jsonl", "a") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main([int(a) for a in sys.argv[1:]] or None)
