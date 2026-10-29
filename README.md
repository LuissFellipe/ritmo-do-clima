# Ritmo do Clima

Pipeline de dados que transforma as medições horárias das estações automáticas do
INMET em tabelas confiáveis e, a partir delas, decompõe o clima de cada estação em
séries de Fourier: o ciclo do dia, o ciclo do ano e o que foge deles.

**Página:** https://luissfellipe.github.io/ritmo-do-clima/

**Estado:** M1 (leitura confiável), M2 (espectro), M3 (baseline harmônica) e M4 (anomalias) prontos, com uma
página que mostra a cobertura dos dados e o ciclo do dia e do ano de cada estação. Compressão
e painel vêm nos próximos marcos.

## Como rodar

Requer [uv](https://docs.astral.sh/uv/).

```bash
make all        # testes, download (2015 em diante), silver, espectro, baseline, anomalias e painel
make silver     # só reprocessa a silver a partir dos ZIPs já baixados
make espectro   # FFT e Lomb-Scargle por estação e ano
make baseline   # série de Fourier por estação, escolha de K e teste contra a ingênua
make anomalias  # z robusto do resíduo, eventos e classificação regional × isolado
make painel     # só regera os CSVs da página (site/data) a partir da silver
```

A primeira execução baixa cerca de 1,2 GB. As seguintes só baixam os ZIPs cujo
`ETag` mudou. A silver completa leva cerca de 2 minutos num notebook de 16 GB; o espectro dos 12 anos, cerca de
30 minutos; a baseline, 2 minutos; as anomalias, pouco mais de 1 minuto.

O espectro e a baseline rodam em 4 processos (`WORKERS` em `config.py`). Com um processo por núcleo
(16), o Lomb-Scargle passava de 16 GB de RAM mais 16 GB de swap, e o `systemd-oomd` matava o terminal
junto. Com 4, o pico medido foi de 9,1 GB no espectro e 5,9 GB na baseline.

## Camadas

| Camada | Onde | O que tem |
| --- | --- | --- |
| Bronze | `data/bronze/{ano}.zip` + `manifesto.json` | ZIP original, ETag, hash e data do download |
| Silver | `data/silver/medicao_horaria/ano=/uf=/` | Uma linha por estação e hora (UTC): `temp_c`, `umid_pct`, `rad_kj_m2`, `status` |
| Silver | `data/silver/cobertura/`, `qualidade/` | Cobertura e avisos por estação-ano |
| Silver | `data/silver/dim_estacao.parquet` | Estações com coordenadas, altitude e avisos |
| Gold | `data/gold/espectro/ano=` | Amplitude, fase e fração da variância dos ciclos de 24 h e de 1 ano e dos 5 maiores picos, por estação-ano |
| Gold | `data/gold/coeficientes_fourier/execucao=` | Coeficientes da baseline de cada estação, uma versão por execução |
| Gold | `data/gold/baseline/ano=` | Real, esperado e resíduo de cada hora |
| Gold | `data/gold/baseline_validacao.parquet` | Erro de cada K na validação e no teste |
| Gold | `data/gold/z_robusto/ano=` | z robusto do resíduo de cada hora |
| Gold | `data/gold/eventos_anomalia.parquet` | Eventos com início, fim, pico, duração, tipo e quantas vizinhas tinham dado e evento |
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

## A matemática

**Espectro (M2).** Série sem lacuna vai pela FFT, sem a média e com janela de Hann. Série com
lacuna vai pelo periodograma de Lomb-Scargle, na mesma grade de frequências. O espectro só localiza
os picos: amplitude e fase saem de uma senoide ajustada nos pontos válidos. Nos 12 anos, o pico de
24 h está entre os 3 maiores do espectro em 100% das estações-ano aprovadas (o critério pedia 95%).

**Baseline (M3).** Cada estação ganha uma série de Fourier ajustada por mínimos quadrados em todos
os seus anos aprovados:

```
T(t) = c + tendência·t + Σ harmônicos de 24 h (K_dia) + Σ harmônicos de 8.766 h (K_ano)
         + harmônicos de 24 h × (cos, sen) do ciclo anual
```

O último termo deixa o ciclo do dia mudar de amplitude e de forma ao longo do ano.

- **Escolha de K.** Os 24 pares de K_dia ∈ {1, 2, 3, 4, 6, 8} e K_ano ∈ {1, 2, 3, 4} são ajustados com
  os anos até 2023 e medidos em 2024. Fica o par com menos colunas cujo erro mediano está a até
  0,01 °C do melhor: **K_dia = 4, K_ano = 3** (32 colunas, 1,958 °C contra 1,957 °C do melhor, que
  tem 44). Sem a tolerância, a primeira rodada escolheu (8, 4), o maior candidato da grade, por milésimos de grau.
- **Tendência.** Só entra com 3 anos aprovados ou mais. Com dois, a diferença entre um ano e outro
  virava tendência: 25 das 26 estações com mais de 0,2 °C/ano tinham só dois anos (a A449 saía com
  −0,75 °C/ano).
- **Teste em 2025.** O ano de teste não entra na escolha de K: o ajuste usa os anos até 2024.
- **Versões.** O id da execução é um hash da silver e dos parâmetros: os mesmos dados geram a
  mesma versão, e duas execuções seguidas gravam arquivos idênticos.

### A baseline perde para a ingênua, e isso é esperado

O critério de aceite do M3 pedia que, no ano de teste, a baseline errasse menos que a baseline
ingênua (a temperatura da mesma hora do dia anterior). **Ela não erra menos.** Em 2025, nas mesmas
2,8 milhões de horas de 332 estações:

| | Erro médio absoluto |
| --- | --- |
| Baseline de Fourier | 1,90 °C |
| Ingênua (mesma hora de ontem) | 1,73 °C |

A baseline vence em 18% das estações. As duas medem coisas diferentes. A ingênua copia o tempo de
ontem: se hoje faz frio porque chegou uma frente fria anteontem, ontem também fez, e ela acerta. A
baseline descreve o clima esperado para aquela hora daquele dia do ano e não sabe nada do tempo que
está fazendo.

Para o M4 (anomalias), é a baseline que serve. Uma onda de calor de cinco dias tem resíduo grande
contra o clima esperado em todos os cinco dias, mas, contra o dia anterior, só aparece no primeiro.

### Anomalias (M4)

- **z robusto.** É (resíduo − mediana) / (1,4826 · MAD), com mediana e MAD do resíduo na janela de
  30 dias centrada no dia da hora. O fator 1,4826 faz a MAD de uma normal valer o desvio padrão. A
  janela anda de dia em dia: as 24 horas de um dia usam a mesma mediana e a mesma MAD, calculadas
  exatamente. Ele fica numa tabela própria (`z_robusto`), e não na `baseline`, para que uma etapa
  não reescreva a saída da outra.
- **Eventos.** Hora com |z| > 4 é anômala. Horas anômalas do mesmo sinal, com até 3 h normais entre
  elas, formam um evento.
- **Classificação.** O evento é **regional** quando alguma estação a até 150 km tem evento do mesmo
  sinal ao mesmo tempo, com 6 h de folga, porque uma frente leva horas para cruzar essa distância.
  É **isolado** quando as vizinhas tinham dado naquele dia e nenhuma teve evento. Há uma terceira
  classe, **sem_vizinhas**, para quando nenhuma vizinha tinha dado: 21 estações não têm nenhuma outra
  a menos de 150 km, e chamar o evento delas de isolado faria uma estação sozinha na Amazônia parecer
  defeituosa.

São 92.005 eventos em 38,5 milhões de horas com z: 0,65% das horas, contra 0,006% esperados se o
resíduo fosse normal. 58% são regionais, 31% isolados e 11% sem vizinhas.

**A frente fria de julho de 2017.** Em 19/07/2017, uma massa de ar polar levou Bom Jardim da Serra
(SC) a −8,8 °C, deixou 58 municípios catarinenses abaixo de zero e baixou Florianópolis de 25 °C no
domingo, 16, para 2,2 °C
([Estadão Conteúdo, na IstoÉ Dinheiro](https://istoedinheiro.com.br/com-88oc-cidade-de-sc-registra-menor-temperatura-do-ano-no-pais)).
O pipeline marca eventos frios regionais em 37 estações do Sul no dia 18 e em 36 no dia 19, nos
três estados. Florianópolis (A806) tem um evento de 48 h, com z = −6,6. Em Rancho Queimado (A870),
às 9h UTC do dia 19, fazia −1,7 °C, contra 10,2 °C esperados.

**Um defeito de sensor.** O evento isolado mais extremo é de Bragança (PA), em 17/03/2020: 31,8 °C,
−9,0 °C, 18,8 °C, 9,8 °C e 31,7 °C em horas seguidas, com z = −39. O −9 °C passa pela silver
porque fica dentro da faixa física aceita (−10 a 50 °C); é a classificação regional × isolado que
o separa de um evento de verdade.

**A maioria das anomalias é fria e de tarde.** 90% dos eventos têm z negativo, e 70% têm o pico
entre 16h e 20h UTC (13h e 17h no horário de Brasília). A hipótese é tarde nublada ou chuvosa, em que a
máxima do dia fica longe da esperada. Ela não foi verificada com dados de precipitação.

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
