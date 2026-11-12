#!/usr/bin/env python3
"""Baixa dados da CVM e associa tickers de ações da B3 por CNPJ."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "indicadores.json"
BASE = "https://dados.cvm.gov.br/dados/CIA_ABERTA"
TICKERS_URL = "https://developers.lummios.com.br/dados/universo-ativos.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    "Referer": "https://www.b3.com.br/",
}
TIMEOUT = 90


def baixar(url: str) -> bytes:
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response.content


def cnpj_limpo(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def baixar_cotahist(ano: int) -> bytes:
    """Tenta os domínios oficiais da B3 que distribuem a série histórica."""
    urls = [
        f"https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
        f"https://www.b3.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
        f"https://b3.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
    ]
    falhas = []
    for url in urls:
        try:
            return baixar(url)
        except requests.RequestException as exc:
            falhas.append(f"{url}: {exc}")
    raise RuntimeError("Não foi possível baixar o COTAHIST da B3. " + " | ".join(falhas))


def carregar_tickers_com_negociacao_b3() -> tuple[set[str], str]:
    """Retorna tickers com negociação à vista registrada na B3 nos últimos 365 dias."""
    hoje = datetime.now(timezone.utc).date()
    corte = hoje - timedelta(days=365)
    ultimos_pregoes: dict[str, object] = {}

    # A série anual da B3 é atualizada com os negócios do respectivo ano.
    for ano in (hoje.year - 1, hoje.year):
        zip_bytes = baixar_cotahist(ano)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            candidatos = [nome for nome in zf.namelist() if nome.lower().endswith(".txt")]
            if not candidatos:
                raise RuntimeError(f"COTAHIST {ano}: arquivo TXT não encontrado.")
            with zf.open(candidatos[0]) as arquivo:
                for bruto in arquivo:
                    linha = bruto.decode("latin1").rstrip("\r\n")
                    if len(linha) < 245 or linha[:2] != "01":
                        continue
                    if linha[10:12].strip() not in {"02", "96"}:
                        continue
                    if linha[24:27].strip() not in {"010", "020"}:
                        continue
                    ticker = linha[12:24].strip().upper()
                    if not ticker:
                        continue
                    try:
                        data_pregao = datetime.strptime(linha[2:10], "%Y%m%d").date()
                    except ValueError:
                        continue
                    if data_pregao >= corte and data_pregao <= hoje:
                        if ticker not in ultimos_pregoes or data_pregao > ultimos_pregoes[ticker]:
                            ultimos_pregoes[ticker] = data_pregao

    if not ultimos_pregoes:
        raise RuntimeError("COTAHIST da B3 não retornou negociações recentes.")
    return set(ultimos_pregoes), max(data.isoformat() for data in ultimos_pregoes.values())


def carregar_mapa_tickers() -> tuple[dict[str, list[str]], int, str]:
    """Carrega tickers de ações e agrupa classes negociadas pelo CNPJ do emissor."""
    csv_bytes = baixar(TICKERS_URL)
    ativos = pd.read_csv(io.BytesIO(csv_bytes), dtype=str).fillna("")
    obrigatorias = {"ticker", "cnpj", "classe"}
    if not obrigatorias.issubset(ativos.columns):
        raise RuntimeError(f"CSV de tickers sem colunas esperadas: {sorted(ativos.columns)}")

    tickers_b3_ativos, data_cotahist = carregar_tickers_com_negociacao_b3()
    ativos["classe_norm"] = ativos["classe"].str.strip().str.casefold()
    ativos["ticker"] = ativos["ticker"].str.strip().str.upper()
    acoes = ativos[
        ativos["classe_norm"].isin(["acao", "ações", "acoes"])
        & ativos["ticker"].isin(tickers_b3_ativos)
    ]
    mapa: dict[str, set[str]] = {}
    for _, linha in acoes.iterrows():
        cnpj = cnpj_limpo(linha["cnpj"])
        ticker = linha["ticker"].strip().upper()
        if len(cnpj) == 14 and ticker:
            mapa.setdefault(cnpj, set()).add(ticker)
    return {cnpj: sorted(tickers) for cnpj, tickers in mapa.items()}, len(acoes), data_cotahist


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
    # SIT=ATIVO é o filtro da CVM para emissores com registro ativo.
    cnpjs_ativos_cvm = set(nomes)

    mapa_tickers = {}
    total_ativos_b3 = 0
    data_cotahist = None
    erros = []
    try:
        mapa_tickers, total_ativos_b3, data_cotahist = carregar_mapa_tickers()
    except Exception as exc:
        erros.append(f"Mapeamento de tickers: {str(exc)[:220]}")
        raise RuntimeError("Falha no mapeamento de tickers; a base anterior será preservada.") from exc
    if not mapa_tickers:
        raise RuntimeError("O mapeamento de tickers retornou vazio; a base anterior será preservada.")

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
            # Não basta o ticker existir em um cadastro histórico: exigimos emissor ativo na CVM.
            if cnpj_key not in cnpjs_ativos_cvm:
                continue
            if cnpj_key not in registros:
                tickers = mapa_tickers.get(cnpj_key, [])
                if not tickers:
                    continue
                registros[cnpj_key] = {
                    "cnpj": cnpj,
                    "empresa": nomes.get(cnpj_key) or (str(grupo["DENOM_CIA"].dropna().iloc[0]) if "DENOM_CIA" in grupo.columns and not grupo["DENOM_CIA"].dropna().empty else cnpj),
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
    # Uma linha por ticker: classes diferentes do mesmo emissor ficam individualizadas.
    empresas = []
    for emissor in registros.values():
        for ticker in emissor["tickers"]:
            linha = dict(emissor)
            linha["ticker"] = ticker
            linha["tickers"] = [ticker]
            empresas.append(linha)
    empresas.sort(key=lambda x: (x["empresa"], x["ticker"]))
    empresas_com_ticker = len({item["cnpj"] for item in empresas})
    payload = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "fonte": "CVM - Dados Abertos; universo de ativos Lummios (CC BY 4.0)",
        "url_fonte": "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp",
        "url_mapa_tickers": "https://developers.lummios.com.br/dados-abertos.html",
        "licenca_mapa_tickers": "CC BY 4.0 - atribuição à Lummios",
        "anos_processados": sorted(anos_ok),
        "quantidade_empresas": empresas_com_ticker,
        "quantidade_linhas_ticker": len(empresas),
        "quantidade_emissores_cvm_com_dados": empresas_cvm_com_dados,
        "emissores_sem_ticker_excluidos": empresas_cvm_com_dados - empresas_com_ticker,
        "quantidade_empresas_com_ticker": empresas_com_ticker,
        "quantidade_ativos_acao_no_mapa": total_ativos_b3,
        "data_ultimo_pregao_b3": data_cotahist,
        "criterio_ticker_ativo": "Negociação à vista registrada no COTAHIST da B3 nos últimos 365 dias; considera lote padrão e fracionário.",
        "observacao": "Exibe emissores com registro ativo na CVM e ticker com negociação à vista registrada na B3 nos últimos 365 dias; cada classe/ticker aparece em uma linha separada. Os indicadores contábeis do emissor se repetem entre classes. Nomes conciliados por CNPJ normalizado, com razão social da DFP como alternativa. Dados DFP consolidados; indicadores não calculados permanecem nulos.",
        "erros": erros,
        "empresas": empresas,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON gerado: {OUT} | empresas={len(empresas)} | com ticker={empresas_com_ticker} | ativos B3 recentes no mapa={total_ativos_b3} | anos={anos_ok} | erros={erros}")


if __name__ == "__main__":
    main()
