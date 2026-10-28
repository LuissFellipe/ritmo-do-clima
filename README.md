# Ritmo do Clima

Pipeline de dados que transforma as medições horárias das estações automáticas do
INMET em tabelas confiáveis e, a partir delas, decompõe o clima de cada estação em
séries de Fourier: o ciclo do dia, o ciclo do ano e o que foge deles.

**Página:** https://luissfellipe.github.io/ritmo-do-clima/

**Estado:** M1 (leitura confiável) pronto, com uma página que mostra a cobertura dos dados
e o ciclo do dia e do ano de cada estação. Espectro, baseline harmônica, anomalias,
compressão e painel vêm nos próximos marcos.

## Como rodar

Requer [uv](https://docs.astral.sh/uv/).

```bash
make all        # testes, download (2015 em diante), silver e painel
make silver     # só reprocessa a silver a partir dos ZIPs já baixados
make painel     # só regera os CSVs da página (site/data) a partir da silver
```

A primeira execução baixa cerca de 1,2 GB. As seguintes só baixam os ZIPs cujo
`ETag` mudou. A silver completa leva cerca de 2 minutos num notebook de 16 GB.

## Camadas

| Camada | Onde | O que tem |
| --- | --- | --- |
| Bronze | `data/bronze/{ano}.zip` + `manifesto.json` | ZIP original, ETag, hash e data do download |
| Silver | `data/silver/medicao_horaria/ano=/uf=/` | Uma linha por estação e hora (UTC): `temp_c`, `umid_pct`, `rad_kj_m2`, `status` |
| Silver | `data/silver/cobertura/`, `qualidade/` | Cobertura e avisos por estação-ano |
| Silver | `data/silver/dim_estacao.parquet` | Estações com coordenadas, altitude e avisos |
| Controle | `data/silver/execucoes.jsonl` | Uma linha por execução |
| Página | `site/data/*.csv` | Cobertura por ano, horas por status, cadastro e ciclos diário e anual por estação (versionados) |

O `status` de cada hora se refere à temperatura: `ok`, `interpolada` (lacuna de
até 3 h), `faltante`, `suspeita` (6 h ou mais com o mesmo valor) ou `duplicada`.
Só estações-ano com pelo menos 80% de horas usáveis seguem para as análises.

## O que os dados do INMET escondem

Cada item abaixo apareceu ao rodar o pipeline nos arquivos reais e virou código e teste:

- **Dois layouts.** Até 2018: `DATA (YYYY-MM-DD)` com `2018-01-01` e `00:00`, e ausente como `-9999`.
  A partir de 2019: `Data` com `2019/01/01` e `0000 UTC`, e ausente como campo vazio.
- **Acentos perdidos na origem.** Em 2019 os metadados vêm como `REGI?O`, `ESTAC?O` e `DATA DE FUNDAC?O`.
- **Metadado ilegível.** A estação S108 (São Bento/MA), em 2016, tem `ALTITUDE:;F`.
- **Uma estação na Antártida.** A C891 (Criosfera), cadastrada como SP, fica a 84° S e registra −51 °C.
  Ela é excluída da silver por estar fora do Brasil.
- **Retratos acumulados no ano corrente.** O ZIP de 2026 traz jan–ago e jan–set de cada estação;
  só o mais recente é lido, e a cobertura é medida até o último dia publicado.
- **Coordenadas que mudam.** 143 estações têm latitude ou longitude diferente entre anos
  (algumas por mais de 1°); isso fica como aviso.
- **Cobertura irregular.** A mediana de horas usáveis por estação cai para 69% em 2021 e 81% em 2022,
  contra 95% a 99% nos outros anos fechados.
- **User-agent.** O portal derruba conexões com user-agent genérico (`python-httpx`);
  o download se identifica.

## Testes de qualidade

Bloqueantes (o pipeline para): layout desconhecido (RQ01), chave repetida (RQ02),
texto em coluna numérica (RQ03) e mais de 0,1% de valores fisicamente impossíveis (RQ04).
Avisos (vão para o relatório): saltos de temperatura (RQ06), sensor travado (RQ07),
cobertura baixa (RQ08), coordenadas (RQ09) e horas revisadas pelo INMET (RQ10).

```bash
make test
```

## Fonte

Dados: [INMET — Dados Históricos](https://portal.inmet.gov.br/dadoshistoricos),
Instituto Nacional de Meteorologia.
