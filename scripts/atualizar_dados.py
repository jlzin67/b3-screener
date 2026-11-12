#!/usr/bin/env python3
"""Baixa dados oficiais DFP da CVM e publica um JSON conservador.

Nesta primeira versão são publicados cadastro e dados financeiros de origem.
Indicadores só são calculados quando a identificação das contas é inequívoca.
Não há cotação de mercado nesta etapa; P/L e P/VP permanecem indisponíveis.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "indicadores.json"
BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA"
HEADERS = {"User-Agent": "b3-screener-educacional/1.0 contato: github.com/jlzin67/b3-screener"}
TIMEOUT = 90


def baixar(url: str) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def ler_csv_zip(conteudo: bytes, nome: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        candidatos = [n for n in zf.namelist() if n.lower().endswith(".csv") and nome.lower() in n.lower()]
        if not candidatos:
            raise RuntimeError(f"Arquivo {nome} não encontrado no ZIP.")
        with zf.open(candidatos[0]) as f:
            return pd.read_csv(f, sep=";", encoding="latin1", dtype={"CNPJ_CIA": str}, low_memory=False)


def numero(v):
    try:
        x = float(v)
        return x if pd.notna(x) else None
    except (TypeError, ValueError):
        return None


def localizar_conta(df: pd.DataFrame, termos: tuple[str, ...]) -> float | None:
    if df.empty:
        return None
    # DRE consolidada: prioriza contas fixas e a linha de maior nível de detalhe.
    dados = df.copy()
    dados["DS_CONTA"] = dados["DS_CONTA"].fillna("").astype(str).str.lower()
    for termo in termos:
        achados = dados[dados["DS_CONTA"].str.fullmatch(termo, case=False, na=False)]
        if not achados.empty:
            linha = achados.sort_values("CD_CONTA").iloc[-1]
            return numero(linha["VL_CONTA"])
    return None


def ano_dfp(ano: int):
    url = f"{BASE}/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip"
    return baixar(url)


def main():
    cadastro_url = f"{BASE}/CAD/DADOS/cad_cia_aberta.csv"
    cadastro = pd.read_csv(io.BytesIO(baixar(cadastro_url)), sep=";", encoding="latin1", dtype={"CNPJ_CIA": str})
    if "SIT" in cadastro.columns:
        cadastro = cadastro[cadastro["SIT"].astype(str).str.contains("ATIVO", case=False, na=False)]
    cadastro = cadastro.drop_duplicates("CNPJ_CIA", keep="last")
    nomes = cadastro.set_index("CNPJ_CIA")["DENOM_SOCIAL"].to_dict()

    registros = {}
    anos_ok = []
    erros = []
    ano_atual = datetime.now(timezone.utc).year
    for ano in range(ano_atual, ano_atual - 6, -1):
        try:
            zip_bytes = ano_dfp(ano)
            dre = ler_csv_zip(zip_bytes, "DRE_con")
            bpa = ler_csv_zip(zip_bytes, "BPA_con")
            bpp = ler_csv_zip(zip_bytes, "BPP_con")
            anos_ok.append(ano)
        except Exception as exc:
            erros.append(f"{ano}: {str(exc)[:180]}")
            continue

        for cnpj, grupo in dre.groupby("CNPJ_CIA"):
            exercicio = grupo[grupo["ORDEM_EXERC"].astype(str).str.contains("ÚLTIMO|ULTIMO", case=False, na=False)]
            if exercicio.empty:
                exercicio = grupo
            receita = localizar_conta(exercicio, (r"receita de venda de bens e/ou serviços", r"receita líquida de vendas e/ou serviços"))
            lucro = localizar_conta(exercicio, (r"lucro/prejuízo consolidado do período", r"lucro/prejuízo do período"))
            if cnpj not in registros:
                registros[cnpj] = {
                    "cnpj": cnpj,
                    "empresa": nomes.get(cnpj, cnpj),
                    "ticker": None,
                    "fonte": "CVM - DFP consolidada",
                    "ano_referencia": ano,
                    "receita_liquida": receita,
                    "lucro_liquido": lucro,
                    "pvp": None, "pl": None, "roe": None, "margem_liquida": None,
                    "roic": None, "divida_ebitda": None, "cagr_lucros": None,
                }

    # Cálculos que usam linhas padronizadas da DRE e são possíveis sem cotação.
    for item in registros.values():
        receita, lucro = item["receita_liquida"], item["lucro_liquido"]
        if receita not in (None, 0) and lucro is not None:
            item["margem_liquida"] = round(lucro / receita * 100, 2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "fonte": "Comissão de Valores Mobiliários (CVM) - Dados Abertos",
        "url_fonte": "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp",
        "anos_processados": sorted(anos_ok),
        "quantidade_empresas": len(registros),
        "observacao": "Versão inicial: dados consolidados DFP. Ticker e indicadores dependentes de cotação ainda não integrados. Campos sem cálculo validado ficam nulos.",
        "erros": erros,
        "empresas": sorted(registros.values(), key=lambda x: x["empresa"]),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON gerado: {OUT} | empresas={len(registros)} | anos={anos_ok} | erros={erros}")


if __name__ == "__main__":
    main()
