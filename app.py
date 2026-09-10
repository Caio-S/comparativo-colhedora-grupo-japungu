# -*- coding: utf-8 -*-
"""
App Flask que serve o relatorio Comparativo de Custo -- Colhedoras do Grupo com dados
sempre atualizados, consultando o MariaDB diretamente (sem etapa de commit no git).

O index.html deste repositorio continua sendo a fonte da estrutura/paginas/layout
(mantido por qualquer colaborador). Este app so troca, a cada atualizacao, o bloco
<script>window.MASTER_RECORDS = [...];</script> por dados frescos do banco -- nunca
grava no disco/repositorio, so serve a versao patchada em memoria.

Credenciais do banco vem de variaveis de ambiente (configuradas no proprio Render,
nunca commitadas). Para rodar local, crie um .env (veja .env.example).
"""
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime

import numpy as np
import pandas as pd
import mysql.connector
from dotenv import load_dotenv
from flask import Flask, Response

# Sem isso, print() fica no buffer do processo e so aparece nos logs do Render quando
# o worker e reiniciado -- inutil para acompanhar uma busca em andamento.
sys.stdout.reconfigure(line_buffering=True)

load_dotenv()

BASE = os.path.dirname(os.path.abspath(__file__))
HTML_TEMPLATE_PATH = os.path.join(BASE, "index.html")
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "1800"))

DATA_START = "<script>window.MASTER_RECORDS = "
DATA_END = ";</script>"
DATA_BLOCK_RE = re.compile(r"<script>window\.MASTER_RECORDS\s*=\s*\[.*?\];</script>", re.DOTALL)

UNIT_MAP = {
    1: "Agro Rubiataba", 2: "Agro Rubiataba",
    3: "PFCMO Goiás", 4: "PFCMO Goiás",
    5: "Agro Uruaçu", 6: "Agro Uruaçu",
    7: "PFCMO Minas Gerais", 8: "PFCMO Minas Gerais",
}

FROTA_CODE_RANGES = [
    (12000, 12999, "PFCMO Goiás"),
    (20000, 29999, "Agro Rubiataba"),
    (50000, 59999, "Agro Uruaçu"),
    (60000, 69999, "PFCMO Minas Gerais"),
]

PESAGEM_WINDOW_START = 2020

app = Flask(__name__)

_cache = {"html": None, "updated_at": None, "error": None, "refreshing": False}
_lock = threading.Lock()


def classify_by_code(codigo):
    for lo, hi, unit in FROTA_CODE_RANGES:
        if lo <= codigo <= hi:
            return unit
    return None


def db_connect():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        charset="utf8mb4",
        connection_timeout=20,
    )


def fetch_data():
    conn = db_connect()
    try:
        today = date.today().isoformat()

        consumo_filter = """
              a.id_empresa IN (1,2,3,4,5,6,7,8)
              AND a.data_lancamento BETWEEN '2020-01-01' AND '{today}'
              AND a.descricao_especialidade_frota LIKE '%COLHEDORA%CANA%'
              AND (a.codigo_tipo_documento LIKE '%RQE%'
                   OR a.descricao_tipo_documento LIKE '%FRO%'
                   OR a.descricao_tipo_documento LIKE '%CA%'
                   OR a.codigo_tipo_documento = 'CA')
        """.format(today=today)

        # Agregado no proprio banco -- evita trazer todas as linhas cruas (centenas de
        # milhares) so para depois somar em pandas, o que era o gargalo em instancias
        # com CPU/memoria mais limitada.
        consumo_custo = pd.read_sql(
            f"""
            SELECT a.codigo_frota AS frota, YEAR(a.data_lancamento) AS ano,
                   SUM(a.valor_totalFLAG) AS custo
            FROM vw_consumo a
            WHERE {consumo_filter}
            GROUP BY a.codigo_frota, YEAR(a.data_lancamento)
            """,
            conn,
        )
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] consumo_custo: {len(consumo_custo)} linhas")

        consumo_marca = pd.read_sql(
            f"""
            SELECT a.codigo_frota AS frota, a.descricao_marca_frota AS marca,
                   a.descricao_modelo_frota AS modelo, COUNT(*) AS n
            FROM vw_consumo a
            WHERE {consumo_filter}
            GROUP BY a.codigo_frota, a.descricao_marca_frota, a.descricao_modelo_frota
            """,
            conn,
        )
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] consumo_marca: {len(consumo_marca)} linhas")

        pesagem = pd.read_sql(
            f"""
            SELECT
                YEAR(capa.data_pesagem) AS ano,
                fro.CodFrota,
                SUM(item.peso_liquido_rateado) AS producao
            FROM pes_pesagem_capa capa
                LEFT JOIN pes_pesagem_item item ON capa.id = item.id_pesagem_capa
                LEFT JOIN vw_bi_fluxo_dFrota fro ON fro.id_frota = item.id_frota
            WHERE capa.data_pesagem BETWEEN '2020-01-01' AND '{today}'
                AND capa.id_empresa IN (2,4,6,8)
                AND fro.descricao_especialidade LIKE '%COLHEDORA%'
            GROUP BY YEAR(capa.data_pesagem), fro.CodFrota
            """,
            conn,
        )
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] pesagem: {len(pesagem)} linhas")

        abast_agg = pd.read_sql(
            f"""
            SELECT
                codigo_frota AS frota,
                YEAR(data_hora_lancamento) AS ano,
                SUM(CASE WHEN km_hr_percorrido BETWEEN 0 AND 100 THEN km_hr_percorrido ELSE 0 END) AS horas,
                SUM(quantidade) AS litros
            FROM vw_abastecimento a
            WHERE a.codigo_empresa IN (2,4,6,8)
              AND a.data_hora_lancamento BETWEEN '2020-01-01' AND '{today}'
              AND a.descricao_especialidade LIKE '%COLHEDORA%CANA%'
            GROUP BY codigo_frota, YEAR(data_hora_lancamento)
            """,
            conn,
        )
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] abast_agg: {len(abast_agg)} linhas")

        fro = pd.read_sql(
            """
            SELECT id_empresa, codigo, descricao, ano_fabricacao, data_aquisicao
            FROM fro_frota
            WHERE id_empresa IN (2,4,6,8) AND descricao LIKE '%COLHEDORA%'
            """,
            conn,
        )
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] fro_frota: {len(fro)} linhas")

        return consumo_custo, consumo_marca, pesagem, abast_agg, fro
    finally:
        conn.close()


def build_records(consumo_custo, consumo_marca, pesagem, abast_agg, fro):
    current_year = date.today().year
    years = list(range(2020, current_year + 1))

    horas_frota_ano = abast_agg[["frota", "ano", "horas"]].copy()
    litros_frota_ano = abast_agg[["frota", "ano", "litros"]].copy()

    fro = fro.copy()
    fro["unit_master"] = fro["id_empresa"].map(UNIT_MAP)
    first_year_pesagem_by_frota = pesagem.groupby("CodFrota")["ano"].min()
    fro["first_year_pesagem"] = fro["codigo"].map(first_year_pesagem_by_frota)
    fro["first_year_aquisicao"] = pd.to_datetime(fro["data_aquisicao"], errors="coerce").dt.year

    fro["first_year_real"] = fro["first_year_aquisicao"]
    can_refine = (fro["first_year_aquisicao"] >= PESAGEM_WINDOW_START) & fro["first_year_pesagem"].notna()
    refine_to_pesagem = can_refine & (fro["first_year_pesagem"] > fro["first_year_aquisicao"])
    fro.loc[refine_to_pesagem, "first_year_real"] = fro.loc[refine_to_pesagem, "first_year_pesagem"]
    fro["first_year_real"] = fro["first_year_real"].fillna(fro["first_year_pesagem"])
    fro["first_year_real"] = fro["first_year_real"].fillna(fro["ano_fabricacao"].replace(0, np.nan))
    fro_idx = fro.set_index("codigo")

    # "Moda" de marca/modelo por frota: como consumo_marca ja veio agregado
    # (frota, marca, modelo, contagem) do banco, so pegamos a combinacao mais
    # frequente de cada frota em vez de recalcular sobre linhas cruas.
    best_marca_idx = consumo_marca.groupby("frota")["n"].idxmax()
    best_marca = consumo_marca.loc[best_marca_idx].set_index("frota")
    frota_marca = best_marca["marca"]
    frota_modelo = best_marca["modelo"]

    pes_total_year = (
        pesagem.groupby(["CodFrota", "ano"])["producao"].sum().reset_index().rename(columns={"CodFrota": "frota"})
    )

    py = pes_total_year.rename(columns={"producao": "tonelada"})
    fy = consumo_custo.merge(py, on=["frota", "ano"], how="outer")
    fy["custo"] = fy["custo"].fillna(0.0)
    fy["tonelada"] = fy["tonelada"].fillna(0.0)
    fy = fy.merge(horas_frota_ano, on=["frota", "ano"], how="left")
    fy["horas"] = fy["horas"].fillna(0.0)
    fy = fy.merge(litros_frota_ano, on=["frota", "ano"], how="left")
    fy["litros"] = fy["litros"].fillna(0.0)

    fy["unit_master"] = fy["frota"].map(fro_idx["unit_master"])
    fy["unit_by_code"] = fy["frota"].apply(classify_by_code)
    fy["unit"] = fy["unit_by_code"].where(fy["unit_by_code"].notna(), fy["unit_master"])
    fy = fy.dropna(subset=["unit"])

    fy["marca"] = fy["frota"].map(frota_marca)
    fy["marca"] = fy["marca"].where(
        fy["marca"].notna(),
        fy["frota"].map(fro_idx["descricao"]).apply(
            lambda d: "CASE" if isinstance(d, str) and "CASE" in d.upper() else ("JOHN DEERE" if isinstance(d, str) else None)
        ),
    )
    fy["modelo"] = fy["frota"].map(frota_modelo)
    fy["modelo"] = fy["modelo"].where(fy["modelo"].notna(), fy["frota"].map(fro_idx["descricao"]))

    fy["first_year_real"] = fy["frota"].map(fro_idx["first_year_real"])
    fy["ano_de_uso"] = fy["ano"] - fy["first_year_real"] + 1
    fy.loc[~fy["ano_de_uso"].between(1, 15), "ano_de_uso"] = None

    fy = fy[fy["ano"].isin(years)]

    out_cols = ["frota", "ano", "unit", "unit_master", "marca", "modelo", "ano_de_uso", "custo", "tonelada", "horas", "litros"]
    out = fy[out_cols].copy()
    out["custo"] = out["custo"].round(2)
    out["tonelada"] = out["tonelada"].round(3)
    out["horas"] = out["horas"].round(1)
    out["litros"] = out["litros"].round(1)

    records = out.to_dict("records")
    for r in records:
        r["ano_de_uso"] = int(r["ano_de_uso"]) if pd.notna(r["ano_de_uso"]) else None
    return records


def refresh_once():
    with _lock:
        if _cache["refreshing"]:
            return
        _cache["refreshing"] = True
    try:
        consumo_custo, consumo_marca, pesagem, abast_agg, fro = fetch_data()
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Consultas ao banco concluidas, processando...")
        records = build_records(consumo_custo, consumo_marca, pesagem, abast_agg, fro)
        with open(HTML_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            html = f.read()
        new_block = DATA_START + json.dumps(records, ensure_ascii=False, separators=(",", ":")) + DATA_END
        new_html, n = DATA_BLOCK_RE.subn(lambda _: new_block, html, count=1)
        if n == 0:
            raise RuntimeError("Bloco window.MASTER_RECORDS nao encontrado no index.html")
        with _lock:
            _cache["html"] = new_html
            _cache["updated_at"] = datetime.now()
            _cache["error"] = None
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Dados atualizados: {len(records)} registros.")
    except Exception as e:
        with _lock:
            _cache["error"] = str(e)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ERRO ao atualizar dados: {e}")
    finally:
        with _lock:
            _cache["refreshing"] = False


def refresh_loop():
    while True:
        refresh_once()
        time.sleep(REFRESH_SECONDS)


threading.Thread(target=refresh_loop, daemon=True).start()


@app.route("/")
def index():
    with _lock:
        html = _cache["html"]
        error = _cache["error"]
    if html is None:
        if error:
            return f"Erro ao carregar dados do banco: {error}", 503
        return "Carregando dados do banco pela primeira vez, isso pode levar alguns minutos...", 503
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/health")
def health():
    with _lock:
        return {
            "updated_at": str(_cache["updated_at"]) if _cache["updated_at"] else None,
            "error": _cache["error"],
            "refreshing": _cache["refreshing"],
        }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
