"""
02_tratamento.py — limpa, filtra o setor e unifica as tabelas.

Entrada : dados/intermediario/*.parquet  (saída do 01_ingestao.py)
Saída   : dados/tratados/base_afl.parquet / .csv
          dados/tratados/aberturas_por_ano.csv

Duas saídas, por dois motivos diferentes:

  base_afl            a base unificada, feita com a PARTE 4 de Estabelecimentos e Empresas,
                      que é o que o teste pede, já filtrada para o setor de alimentação e
                      enriquecida com Empresas, Simples e as tabelas de domínio.

  aberturas_por_ano   a série de aberturas ao longo do tempo. Esta usa TODAS as partes
                      disponíveis, porque as 10 partes são fatias temporais: a parte 4 sozinha
                      concentra aberturas até 2020 e quase nada entre 2021 e 2025, então não
                      dá para responder "como evoluiu a abertura" só com ela.

O que este script faz, em ordem:
  1. padroniza os campos de texto e monta o CNPJ completo de 14 dígitos;
  2. converte as datas de texto YYYYMMDD para data de verdade, contando as inválidas;
  3. filtra o setor (CNAE principal entre os 6 códigos de alimentação fora do lar);
  4. traduz os códigos com as tabelas de domínio (CNAE, município, motivo, natureza);
  5. junta com Empresas (razão social, porte, natureza jurídica, capital social);
  6. junta com a base do Simples para descobrir quem é MEI;
  7. monta a série temporal com todas as partes, tirando duplicados entre elas;
  8. grava as saídas e imprime o resumo do que aconteceu.
"""

from pathlib import Path

import duckdb

RAIZ = Path(__file__).resolve().parents[1]
INTERMEDIARIO = RAIZ / "dados" / "intermediario"
TRATADOS = RAIZ / "dados" / "tratados"

# Os 6 CNAEs de bares, restaurantes e alimentação fora do lar (AFL).
# 4721102 padaria e confeitaria com predominância de revenda
# 5611201 restaurantes e similares
# 5611203 lanchonetes, casas de chá, de sucos e similares
# 5611204 bares e outros estabelecimentos especializados em servir bebidas, sem entretenimento
# 5611205 bares e outros estabelecimentos especializados em servir bebidas, com entretenimento
# 5620104 fornecimento de alimentos preparados preponderantemente para consumo domiciliar
CNAES_AFL = ["4721102", "5611201", "5611203", "5611204", "5611205", "5620104"]
LISTA_CNAES = ", ".join(f"'{c}'" for c in CNAES_AFL)

# Situação cadastral vem como código. As descrições estão no metadados da Receita,
# não em arquivo de domínio próprio.
SITUACAO = {
    "01": "Nula", "02": "Ativa", "03": "Suspensa", "04": "Inapta", "08": "Baixada",
}
# Porte da empresa (campo de Empresas). Repare que NÃO existe código de MEI aqui: MEI não
# é um porte, é um regime tributário, e por isso vamos buscá-lo na base do Simples.
PORTE = {"00": "Não informado", "01": "Microempresa", "03": "Empresa de pequeno porte",
         "05": "Demais"}


def view_de_parquet(con, caminho: Path, nome: str) -> None:
    con.execute(f"CREATE OR REPLACE VIEW {nome} AS SELECT * FROM read_parquet('{caminho}')")


def preparar_estabelecimentos(con) -> None:
    """
    Padroniza os campos e monta o CNPJ de 14 dígitos.

    Um campo vazio ("") é ausência de informação, não um valor: vira NULL para não criar
    categorias fantasma nos agrupamentos.
    """
    con.execute("""
        CREATE OR REPLACE VIEW estab_padrao AS
        SELECT
            -- CNPJ completo de 14 dígitos: básico (8) + ordem (4) + dígito verificador (2)
            cnpj_basico || cnpj_ordem || cnpj_dv                       AS cnpj,
            cnpj_basico,
            cnpj_ordem,
            cnpj_dv,
            regexp_extract(arquivo_origem, 'Y([0-9])', 1)              AS parte,
            CASE matriz_filial WHEN '1' THEN 'Matriz' WHEN '2' THEN 'Filial'
                 ELSE 'Não informado' END                              AS matriz_filial,
            nullif(trim(nome_fantasia), '')                            AS nome_fantasia,
            situacao_cadastral,
            try_strptime(nullif(data_situacao_cadastral, '0'), '%Y%m%d')::DATE
                                                                       AS data_situacao,
            motivo_situacao,
            try_strptime(nullif(data_inicio_atividade, '0'), '%Y%m%d')::DATE
                                                                       AS data_inicio,
            cnae_principal,
            nullif(trim(cnae_secundaria), '')                          AS cnae_secundaria,
            nullif(trim(logradouro), '')                               AS logradouro,
            nullif(trim(numero), '')                                   AS numero,
            nullif(trim(bairro), '')                                   AS bairro,
            nullif(trim(cep), '')                                      AS cep,
            municipio                                                  AS municipio_codigo,
            nullif(trim(uf), '')                                       AS uf
        FROM bruto_estab
    """)


def montar_base(con) -> None:
    """
    Base unificada do setor, com todas as partes disponíveis.

    As partes não são fatias de tempo nem faixas de CNPJ: são pedaços de um arquivo numa
    ordem interna, e cada uma subamostra os anos de um jeito. Por isso só o conjunto
    inteiro dá a série nacional.
    """
    con.execute(f"""
        CREATE OR REPLACE VIEW base_afl AS
        SELECT
            e.cnpj,
            e.cnpj_basico,
            e.matriz_filial,
            e.nome_fantasia,
            em.razao_social,
            e.situacao_cadastral,
            CASE e.situacao_cadastral
                {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in SITUACAO.items())}
                ELSE 'Não informado' END                               AS situacao,
            e.data_situacao,
            e.data_inicio,
            year(e.data_inicio)                                        AS ano_inicio,
            e.cnae_principal,
            dc.descricao                                               AS cnae_descricao,
            e.cnae_secundaria,
            e.logradouro, e.numero, e.bairro, e.cep,
            e.municipio_codigo,
            dm.descricao                                               AS municipio,
            e.uf,
            dmot.descricao                                             AS motivo_situacao,
            em.natureza_juridica,
            dn.descricao                                               AS natureza_juridica_desc,
            em.capital_social,
            em.porte                                                   AS porte_codigo,
            CASE em.porte
                {' '.join(f"WHEN '{k}' THEN '{v}'" for k, v in PORTE.items())}
                ELSE 'Não informado' END                               AS porte,
            -- MEI vem do Simples: o campo 'porte' de Empresas não tem essa opção.
            CASE WHEN s.opcao_mei = 'S' THEN 'MEI' ELSE 'Não MEI' END   AS mei,
            CASE WHEN s.opcao_simples = 'S' THEN 'Sim' ELSE 'Não' END   AS optante_simples
        FROM estab_padrao e
        LEFT JOIN bruto_empresas em ON em.cnpj_basico = e.cnpj_basico
        LEFT JOIN bruto_simples  s  ON s.cnpj_basico  = e.cnpj_basico
        LEFT JOIN dom_cnaes      dc ON dc.codigo = e.cnae_principal
        LEFT JOIN dom_municipios dm ON dm.codigo = e.municipio_codigo
        LEFT JOIN dom_naturezas  dn ON dn.codigo = em.natureza_juridica
        LEFT JOIN dom_motivos    dmot ON dmot.codigo = e.motivo_situacao
        WHERE e.cnae_principal IN ({LISTA_CNAES})
    """)


def montar_serie(con) -> dict:
    """
    Série de aberturas por ano, com todas as partes.

    Filtramos o setor pelo CNAE principal, igual à base. Conferimos também se o mesmo
    estabelecimento aparece em mais de uma parte: se aparecesse, contaria duas vezes.
    """
    con.execute(f"""
        CREATE OR REPLACE VIEW aberturas AS
        SELECT year(data_inicio) AS ano, parte, count(*) AS estabelecimentos
        FROM estab_padrao
        WHERE cnae_principal IN ({LISTA_CNAES}) AND data_inicio IS NOT NULL
        GROUP BY 1, 2
    """)

    numeros = con.execute(f"""
        SELECT count(*) AS linhas, count(DISTINCT cnpj) AS cnpjs
        FROM estab_padrao
        WHERE cnae_principal IN ({LISTA_CNAES}) AND data_inicio IS NOT NULL
    """).fetchone()
    return {"linhas_do_setor": numeros[0], "cnpjs_distintos": numeros[1]}


def main() -> None:
    TRATADOS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    # 72 milhões de linhas não cabem em memória, e o DuckDB prefere usar disco a estourar:
    # sem estas linhas ele morre com "Out of Memory" na hora de gravar a base.
    con.execute("SET memory_limit = '8GB'")
    con.execute("SET threads = 6")
    con.execute("SET preserve_insertion_order = false")

    view_de_parquet(con, INTERMEDIARIO / "estabelecimentos" / "*.parquet", "bruto_estab")
    view_de_parquet(con, INTERMEDIARIO / "empresas" / "*.parquet", "bruto_empresas")
    view_de_parquet(con, INTERMEDIARIO / "simples" / "*.parquet", "bruto_simples")
    for nome in ["cnaes", "municipios", "motivos", "naturezas"]:
        view_de_parquet(con, INTERMEDIARIO / "dominios" / f"{nome}.parquet", f"dom_{nome}")

    preparar_estabelecimentos(con)
    montar_base(con)
    conferencia = montar_serie(con)

    con.execute(f"COPY (SELECT * FROM base_afl) TO '{TRATADOS / 'base_afl.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY (SELECT * FROM base_afl) TO '{TRATADOS / 'base_afl.csv'}' (HEADER, DELIMITER ',')")
    con.execute(f"COPY (SELECT * FROM aberturas ORDER BY ano, parte) "
                f"TO '{TRATADOS / 'aberturas_por_ano.csv'}' (HEADER, DELIMITER ',')")

    # Série do país inteiro (todos os CNAEs), para comparar o peso do setor no total de
    # aberturas. Fica gravada aqui para o notebook não precisar ler os arquivos brutos.
    con.execute(f"""
        COPY (
            SELECT year(data_inicio) AS ano, count(*) AS aberturas
            FROM estab_padrao
            WHERE data_inicio IS NOT NULL
            GROUP BY 1 ORDER BY 1
        ) TO '{TRATADOS / 'aberturas_pais_por_ano.csv'}' (HEADER, DELIMITER ',')
    """)

    resumo = con.execute("""
        SELECT
            (SELECT count(*) FROM bruto_estab)                                    AS estabelecimentos_lidos,
            (SELECT count(*) FROM estab_padrao WHERE data_inicio IS NULL)         AS sem_data_inicio,
            (SELECT count(DISTINCT parte) FROM estab_padrao)                      AS partes_lidas,
            (SELECT count(*) FROM base_afl)                                       AS base_afl_linhas,
            (SELECT count(DISTINCT cnpj_basico) FROM base_afl)                    AS base_afl_empresas,
            (SELECT count(*) FROM base_afl WHERE situacao = 'Ativa')              AS base_afl_ativos,
            (SELECT count(DISTINCT cnpj_basico) FROM base_afl
              WHERE situacao = 'Ativa')                                           AS base_afl_empresas_ativas,
            (SELECT count(*) FROM base_afl WHERE razao_social IS NULL)            AS base_afl_sem_empresa,
            (SELECT count(*) - count(DISTINCT cnpj) FROM base_afl)                AS cnpjs_repetidos
    """).fetchone()

    rotulos = ["estabelecimentos lidos (todas as partes)", "sem data de início (inválida/vazia)",
               "partes lidas", "linhas da base unificada (setor)",
               "empresas distintas na base", "estabelecimentos ativos",
               "empresas ativas", "linhas sem casar com Empresas",
               "linhas repetidas entre partes"]
    print("\nResumo do tratamento")
    for rotulo, valor in zip(rotulos, resumo):
        print(f"  {rotulo:<45} {valor:>12,}")

    print(f"\n  série de aberturas: {conferencia['linhas_do_setor']:,} linhas do setor, "
          f"{conferencia['cnpjs_distintos']:,} CNPJs distintos")

    print("\nCobertura da série por parte e ano (estabelecimentos):")
    print(con.execute("""
        SELECT ano,
               count(DISTINCT parte)     AS partes_que_contribuem,
               sum(estabelecimentos)     AS aberturas_no_ano
        FROM aberturas WHERE ano >= 2015 GROUP BY 1 ORDER BY 1
    """).df().to_string(index=False))


if __name__ == "__main__":
    main()
