"""
01_ingestao.py — traduz os arquivos brutos da Receita Federal para Parquet.

Por que este script existe
--------------------------
Os arquivos do CNPJ não são CSV "amigável":
  * vêm SEM cabeçalho, então os nomes das colunas só existem na documentação oficial;
  * separador é ';' e TODOS os campos vêm entre aspas duplas;
  * alguns campos têm ';' DENTRO das aspas (ex.: "APT 1.404;BLOCO A"), então separar a
    linha por ';' na mão quebra ~1,9% dos registros;
  * alguns campos têm aspas escapadas com aspas dobradas (ex.: loja chamada "A");
  * o encoding é Latin-1 (ISO-8859-1), não UTF-8 — e aparece 1 byte 0x8F, que nem o
    Latin-1 estrito aceita (o DuckDB recusa o arquivo por causa disso).

Estratégia: baixar todos os arquivos 'K*.ESTABELE' / 'K*.EMPRECSV' / Simples / domínios,
converter Latin-1 -> UTF-8 uma vez e gravar Parquet. A conversão é byte a byte, então é
reversível; o que ela revela (bytes de controle) é contado e reportado, não escondido.

Nenhuma regra de negócio acontece aqui: isto é só "traduzir o arquivo para uma tabela de
verdade", com todo campo lido como texto (os códigos têm zero à esquerda e as datas são
texto YYYYMMDD — converter cedo demais destruiria informação).
"""

from pathlib import Path

import duckdb

RAIZ = Path(__file__).resolve().parents[1]
BRUTOS = RAIZ / "dados" / "brutos"
SAIDA = RAIZ / "dados" / "intermediario"

# Bytes de controle C1 (0x80–0x9F): não existem em texto normal e são indício clássico de
# sujeira no arquivo. Contamos para poder afirmar isso na entrega, com número.
FAIXA_CONTROLE = range(0x80, 0xA0)

COLUNAS_ESTABELECIMENTOS = [
    "cnpj_basico", "cnpj_ordem", "cnpj_dv", "matriz_filial", "nome_fantasia",
    "situacao_cadastral", "data_situacao_cadastral", "motivo_situacao", "cidade_exterior",
    "pais", "data_inicio_atividade", "cnae_principal", "cnae_secundaria", "tipo_logradouro",
    "logradouro", "numero", "complemento", "bairro", "cep", "uf", "municipio",
    "ddd_1", "telefone_1", "ddd_2", "telefone_2", "ddd_fax", "fax", "email",
    "situacao_especial", "data_situacao_especial",
]

COLUNAS_EMPRESAS = [
    "cnpj_basico", "razao_social", "natureza_juridica", "qualificacao_responsavel",
    "capital_social", "porte", "ente_federativo",
]

COLUNAS_SIMPLES = [
    "cnpj_basico", "opcao_simples", "data_opcao_simples", "data_exclusao_simples",
    "opcao_mei", "data_opcao_mei", "data_exclusao_mei",
]

# Tabelas de domínio: traduzem códigos em descrições. Só entram as que usamos de fato.
DOMINIOS = {
    "cnaes": ("*CNAECSV", ["codigo", "descricao"]),
    "motivos": ("*MOTICSV", ["codigo", "descricao"]),
    "municipios": ("*MUNICCSV", ["codigo", "descricao"]),
    "naturezas": ("*NATJUCSV", ["codigo", "descricao"]),
    "paises": ("*PAISCSV", ["codigo", "descricao"]),
    "qualificacoes": ("*QUALSCSV", ["codigo", "descricao"]),
}


def converter_para_utf8(caminho: Path, destino: Path) -> dict:
    """
    Converte Latin-1 -> UTF-8 e conta os bytes de controle encontrados.

    A conversão é byte a byte, então nada é adivinhado. Bytes de controle (0x80-0x9F) não
    significam nada em texto e quase sempre são erro de digitação no cadastro.

    Grava primeiro em '.part' e só renomeia no fim: se o processo morrer no meio, não
    sobra um arquivo pela metade sendo reaproveitado como se estivesse bom.
    """
    if destino.exists() and destino.stat().st_size > 0:
        return {"reconvertido": False, "bytes_controle": 0, "linhas_afetadas": 0}

    parcial = destino.with_name(destino.name + ".part")
    bytes_controle = 0
    linhas_afetadas = 0
    with open(caminho, "rb") as entrada, open(parcial, "wb") as saida:
        while pedaco := entrada.read(8_000_000):
            texto = pedaco.decode("latin-1")
            if any(ord(c) in FAIXA_CONTROLE for c in texto):
                bytes_controle += sum(1 for c in texto if ord(c) in FAIXA_CONTROLE)
                linhas_afetadas += sum(
                    1 for linha in texto.splitlines()
                    if any(ord(c) in FAIXA_CONTROLE for c in linha)
                )
            saida.write(texto.encode("utf-8"))

    parcial.replace(destino)
    return {"reconvertido": True, "bytes_controle": bytes_controle,
            "linhas_afetadas": linhas_afetadas}


def ler_como_texto(con: duckdb.DuckDBPyConnection, caminho: Path,
                   colunas: list[str], paralelo: bool = True) -> str:
    """
    Cria uma view lendo o CSV com as regras reais do arquivo (aspas, ';', sem header).

    O leitor paralelo do DuckDB não consegue ler alguns arquivos de Estabelecimentos
    inteiros, porque há quebras de linha dentro de campos entre aspas. Nesses casos ele
    mesmo sugere ler com parallel = false, que é mais lento mas lê tudo.
    """
    if not caminho.exists():
        raise FileNotFoundError(f"arquivo bruto não encontrado: {caminho}")

    definicao = ", ".join(f"'{c}': 'VARCHAR'" for c in colunas)
    nome_view = f"v_{caminho.name.replace('.', '_').replace('$', '_')}"
    con.execute(f"""
        CREATE OR REPLACE VIEW {nome_view} AS
        SELECT * FROM read_csv(
            '{caminho}',
            delim = ';',
            quote = '"',
            escape = '"',
            header = false,
            all_varchar = true,
            strict_mode = false,
            parallel = {str(paralelo).lower()},
            columns = {{{definicao}}}
        )
    """)
    return nome_view


def processar_grupo(con, padrao: str, colunas: list[str], nome_saida: str,
                    paralelo: bool = True) -> None:
    """
    Converte, lê e grava um Parquet por arquivo bruto.

    Um Parquet por parte (em vez de um arquivo único gigante) tem três vantagens: dá para
    processar cada parte assim que ela chega, o consumo de memória fica previsível e uma
    parte com problema não derruba o trabalho das outras.
    """
    arquivos = sorted(BRUTOS.glob(padrao))
    if not arquivos:
        print(f"{nome_saida:17s} nenhum arquivo encontrado para o padrão {padrao}")
        return

    destino_dir = SAIDA / nome_saida
    destino_dir.mkdir(exist_ok=True)

    total_linhas = 0
    for caminho in arquivos:
        destino = destino_dir / f"{caminho.name}.parquet"
        convertido = SAIDA / f"{caminho.name}.utf8.csv"

        # se o Parquet desta parte já existe, não refazemos nada
        if not destino.exists():
            info = converter_para_utf8(caminho, convertido)
            if info["reconvertido"] and info["bytes_controle"]:
                print(f"   {caminho.name}: {info['bytes_controle']} byte(s) de controle "
                      f"em {info['linhas_afetadas']} linha(s)")
            view = ler_como_texto(con, convertido, colunas, paralelo)
            con.execute(f"""
                CREATE OR REPLACE VIEW v_partes AS
                SELECT *, '{caminho.name}' AS arquivo_origem FROM {view}
            """)
            con.execute(f"COPY (SELECT * FROM v_partes) TO '{destino}' (FORMAT PARQUET)")

        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{destino}')"
        ).fetchone()[0]
        total_linhas += n
        print(f"   {caminho.name:38s} {n:>12,} linhas")

    print(f"{nome_saida:17s} {total_linhas:>12,} linhas | {len(colunas):>2} colunas | "
          f"{len(arquivos)} arquivo(s)")


def main() -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    (SAIDA / "dominios").mkdir(exist_ok=True)
    con = duckdb.connect()
    # A máquina fica com pouca memória livre quando o download roda junto, então limitamos
    # o uso: o DuckDB prefere escrever em disco a estourar a RAM e ser morto pelo sistema.
    con.execute("SET memory_limit = '3GB'")
    con.execute("SET threads = 4")

    processar_grupo(con, "K*.ESTABELE", COLUNAS_ESTABELECIMENTOS, "estabelecimentos",
                    paralelo=False)
    processar_grupo(con, "K*.EMPRECSV", COLUNAS_EMPRESAS, "empresas")
    processar_grupo(con, "*SIMPLES.CSV*", COLUNAS_SIMPLES, "simples")

    for nome, (padrao, colunas) in DOMINIOS.items():
        arquivos = sorted(BRUTOS.glob(padrao))
        if not arquivos:
            print(f"dominios/{nome:15s} ausente")
            continue
        convertido = SAIDA / "dominios" / f"{arquivos[0].name}.utf8.csv"
        converter_para_utf8(arquivos[0], convertido)
        view = ler_como_texto(con, convertido, colunas)
        destino = SAIDA / "dominios" / f"{nome}.parquet"
        con.execute(f"COPY (SELECT * FROM {view}) TO '{destino}' (FORMAT PARQUET)")

    print("\nOK: arquivos brutos transformados em Parquet.")


if __name__ == "__main__":
    main()
