# Teste técnico Abrasel — estágio em dados

Análise dos dados abertos de CNPJ da Receita Federal, filtrada para o setor de bares,
restaurantes e alimentação fora do lar (AFL), com os seis CNAEs do enunciado.

## Respostas

| Pergunta | Resposta |
|---|---|
| Empresas do setor ativas hoje | **1.511.132** empresas, em 1.543.267 estabelecimentos (97,6% matriz) |
| Evolução da abertura nos últimos 5 anos | 2021: 356.849 · 2022: 282.586 · 2023: 268.190 · 2024: 284.904 · 2025: 315.376 (2026, oito meses: 230.057) |
| Porte predominante | **MEI, com 63,1%** das empresas ativas; microempresa 32,2%, EPP 3,9% |

Números de todo o país: foram processados os arquivos completos (72.789.638 estabelecimentos),
não uma amostra.

## Onde está cada coisa

```
scripts/01_ingestao.py     arquivos crus da Receita -> Parquet (encoding, aspas, tipos)
scripts/02_tratamento.py   limpeza, filtro do setor, junções -> base tratada e séries
notebooks/analise_abrasel.ipynb   entrega principal (notebook executado, com os gráficos)
dados/tratados/base_afl.parquet   base unificada e tratada (396 MB)
dados/tratados/base_afl.csv       a mesma base em CSV (1,7 GB)
dados/amostra/amostra_base_afl.csv   20 mil linhas da base tratada, para ver o formato sem baixar tudo
dados/tratados/aberturas_por_ano.csv       aberturas do setor por ano e por parte do arquivo
dados/tratados/aberturas_pais_por_ano.csv  aberturas do país inteiro, todos os CNAEs
docs/layout_rfb_cnpj.pdf          layout oficial das colunas, baixado da Receita
```

## Como reproduzir

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python pandas duckdb matplotlib jupyter pyarrow
```

1. Baixe do portal da Receita, para `dados/brutos/`, a competência mais recente
   (`https://arquivos.receitafederal.gov.br/index.php/s/gn672Ad4CF8N6TK?dir=/Dados/Cadastros/CNPJ`):
   as **10 partes** de `Estabelecimentos`, as **10 de** `Empresas`, `Simples.zip` e as tabelas de
   domínio (`Cnaes`, `Motivos`, `Municipios`, `Naturezas`, `Paises`, `Qualificacoes`). Descompacte
   tudo na mesma pasta.
2. Rode o pipeline:

```bash
.venv/bin/python scripts/01_ingestao.py
.venv/bin/python scripts/02_tratamento.py
```

3. Abra `notebooks/analise_abrasel.ipynb` e rode as células.

Espaço em disco: cerca de 25 GB de arquivos crus e 30 GB de intermediários. Tempo: o download é
o gargalo, o portal entrega a aproximadamente 1 MB/s.

## Descobertas que valem destacar

**As partes não são fatias de tempo nem faixas de CNPJ.** O enunciado sugere baixar a mesma parte
dos dois arquivos, contando que elas se liguem pelo CNPJ básico. Testei: `Empresas4` é faixa
contígua (13.846.068 a 18.421.566) e `Estabelecimentos4` é fatia espalhada pelo cadastro inteiro,
o que deixava só **8,9%** dos estabelecimentos com empresa correspondente. Investigando, as partes
de Estabelecimentos têm exatamente o mesmo tamanho em linhas entre si (4.753.435, nas partes 1 a 9)
e a parte 0 tem 30.008.723. O que muda é a densidade por ano: a parte 4 tem 23.929 aberturas do
setor em 2019 e 1 em 2021; a parte 5 tem 2.733 em 2019 e 35.577 em 2021. Ou seja, somar um
subconjunto de partes produz uma curva com "crescimento" que é só efeito de cobertura.

**A solução foi processar as 10 partes.** Com o conjunto completo o casamento vira **100%** (zero
linhas sem empresa), não há estabelecimento repetido entre partes, e as respostas passam a ser
nacionais. O total lido é 72.789.638 estabelecimentos.

**A parte 4 sozinha não responde a pergunta dos 5 anos.** Ela tem 60 registros em 2021, 46 em 2022,
74 em 2023 e 106 em 2024. E isso não é do setor de alimentação: em todos os CNAEs, 2021 tem 60
registros e 2019 tem 291.773. É característica do arquivo.

**MEI não está no campo `porte`.** Porte só tem não informado, microempresa, EPP e demais. MEI é
regime tributário e só aparece na base do Simples, que foi de onde saiu o número.

**Detalhes técnicos que custaram tempo:** o leitor paralelo de CSV do DuckDB não lê os arquivos de
Estabelecimentos por inteiro (há quebra de linha dentro de campos entre aspas), então a ingestão
usa `parallel = false`; e processar 72 milhões de linhas exige desligar a preservação de ordem de
inserção (`preserve_insertion_order = false`) e limitar a memória, senão o DuckDB morre com erro
de falta de memória ao gravar a base.

## Dados

Competência 2026-08 dos dados abertos de CNPJ da Receita Federal: `Estabelecimentos` 0-9,
`Empresas` 0-9, `Simples` e tabelas de domínio. Todos públicos.
