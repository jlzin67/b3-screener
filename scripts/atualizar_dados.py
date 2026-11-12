#!/usr/bin/env python3
"""Baixa dados da CVM e associa tickers de ações da B3 por CNPJ."""
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
TICKERS_URL = "https://developers.lummios.com.br/dados/universo-ativos.csv"
HEADERS = {"User-Agent": "b3-screener-educacional/1.0 (projeto público)"}
TIMEOUT = 90


def baixar(url: str) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def cnpj_limpo(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def carregar_mapa_tickers() -> tuple[dict[str, list[str]], int]:
    """Carrega tickers de ações e agrupa classes negociadas pelo CNPJ do emissor."""
    csv_bytes = baixar(TICKERS_URL)
    ativos = pd.read_csv(io.BytesIO(csv_bytes), dtype=str).fillna("")
    obrigatorias = {"ticker", "cnpj", "classe"}
    if not obrigatorias.issubset(ativos.columns):
        raise RuntimeError(f"CSV de tickers sem colunas esperadas: {sorted(ativos.columns)}")

    ativos["classe_norm"] = ativos["classe"].str.strip().str.casefold()
    acoes = ativos[ativos["classe_norm"].isin(["acao", "ações", "acoes"])]
    mapa: dict[str, set[str]] = {}
    for _, linha in acoes.iterrows():
        cnpj = cnpj_limpo(linha["cnpj"])
        ticker = linha["ticker"].strip().upper()
        if len(cnpj) == 14 and ticker:
            mapa.setdefault(cnpj, set()).add(ticker)
    return {cnpj: sorted(tickers) for cnpj, tickers in mapa.items()}, len(acoes)


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
    dados = df.copy()
    dados["DS_CONTA"] = dados["DS_CONTA"].fillna("").astype(str).str.lower()
    for termo in termos:
        achados = dados[dados["DS_CONTA"].str.fullmatch(termo, case=False, na=False)]
        if not achados.empty:
            linha = achados.sort_values("CD_CONTA").iloc[-1]
            return numero(linha["VL_CONTA"])
    return None


def ano_dfp(ano: int):
    return baixar(f"{BASE}/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip")


def main():
    cadastro_url = f"{BASE}/CAD/DADOS/cad_cia_aberta.csv"
    cadastro = pd.read_csv(io.BytesIO(baixar(cadastro_url)), sep=";", encoding="latin1", dtype={"CNPJ_CIA": str})
    if "SIT" in cadastro.columns:
        cadastro = cadastro[cadastro["SIT"].astype(str).str.contains("ATIVO", case=False, na=False)]
    cadastro = cadastro.drop_duplicates("CNPJ_CIA", keep="last")
    nomes = {cnpj_limpo(cnpj): nome for cnpj, nome in zip(cadastro["CNPJ_CIA"], cadastro["DENOM_SOCIAL"])}

    mapa_tickers = {}
    total_ativos_b3 = 0
    erros = []
    try:
        mapa_tickers, total_ativos_b3 = carregar_mapa_tickers()
    except Exception as exc:
        erros.append(f"Mapeamento de tickers: {str(exc)[:220]}")

    registros = {}
    anos_ok = []
    ano_atual = datetime.now(timezone.utc).year
    for ano in range(ano_atual, ano_atual - 6, -1):
        try:
            zip_bytes = ano_dfp(ano)
            dre = ler_csv_zip(zip_bytes, "DRE_con")
            ler_csv_zip(zip_bytes, "BPA_con")
            ler_csv_zip(zip_bytes, "BPP_con")
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
            cnpj_key = cnpj_limpo(cnpj)
            if cnpj_key not in registros:
                tickers = mapa_tickers.get(cnpj_key, [])
                registros[cnpj_key] = {
                    "cnpj": cnpj,
                    "empresa": nomes.get(cnpj_key, cnpj),
                    "ticker": " / ".join(tickers) if tickers else None,
                    "tickers": tickers,
                    "fonte": "CVM - DFP consolidada",
                    "ano_referencia": ano,
                    "receita_liquida": receita,
                    "lucro_liquido": lucro,
                    "pvp": None, "pl": None, "roe": None, "margem_liquida": None,
                    "roic": None, "divida_ebitda": None, "cagr_lucros": None,
                }

    for item in registros.values():
        receita, lucro = item["receita_liquida"], item["lucro_liquido"]
        if receita not in (None, 0) and lucro is not None:
            item["margem_liquida"] = round(lucro / receita * 100, 2)

    empresas_cvm_com_dados = len(registros)
    # O screener é de ações negociadas na B3: não exibir emissores CVM sem ticker de ação no universo de referência.
    empresas = sorted((item for item in registros.values() if item["tickers"]), key=lambda x: x["empresa"])
    empresas_com_ticker = len(empresas)
    payload = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "fonte": "CVM - Dados Abertos; universo de ativos Lummios (CC BY 4.0)",
        "url_fonte": "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp",
        "url_mapa_tickers": "https://developers.lummios.com.br/dados-abertos.html",
        "licenca_mapa_tickers": "CC BY 4.0 - atribuição à Lummios",
        "anos_processados": sorted(anos_ok),
        "quantidade_empresas": len(empresas),
        "quantidade_emissores_cvm_com_dados": empresas_cvm_com_dados,
        "emissores_sem_ticker_excluidos": empresas_cvm_com_dados - empresas_com_ticker,
        "quantidade_empresas_com_ticker": empresas_com_ticker,
        "quantidade_ativos_acao_no_mapa": total_ativos_b3,
        "observacao": "Exibe somente emissores com ao menos um ticker classificado como ação no universo de referência da B3. Nomes conciliados por CNPJ normalizado. Dados financeiros DFP consolidados; indicadores não calculados permanecem nulos.",
        "erros": erros,
        "empresas": empresas,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON gerado: {OUT} | empresas={len(empresas)} | com ticker={empresas_com_ticker} | ativos B3 no mapa={total_ativos_b3} | anos={anos_ok} | erros={erros}")


if __name__ == "__main__":
    main()
