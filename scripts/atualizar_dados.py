#!/usr/bin/env python3
"""Gera indicadores fundamentalistas com dados públicos da CVM e cotações da B3."""
from __future__ import annotations

import io
import json
import re
import zipfile
import unicodedata
import time
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


def normalizar_texto(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", texto.upper())


def numero(valor):
    try:
        x = float(valor)
        return x if pd.notna(x) else None
    except (TypeError, ValueError):
        return None


def baixar_cotahist(ano: int) -> bytes:
    urls = [
        f"https://bvmf.bmfbovespa.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
        f"https://www.b3.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
        f"https://b3.com.br/InstDados/SerHist/COTAHIST_A{ano}.ZIP",
    ]
    falhas = []
    # O COTAHIST anual pode ultrapassar 90 MB e a B3 ocasionalmente encerra
    # a conexão antes de concluir a transferência. Repetimos o download e
    # só aceitamos o arquivo depois de validar a estrutura e a integridade ZIP.
    for url in urls:
        for tentativa in range(1, 5):
            try:
                response = requests.get(
                    url,
                    headers={**HEADERS, "Accept-Encoding": "identity"},
                    timeout=(30, 240),
                )
                response.raise_for_status()
                conteudo = response.content
                with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
                    membro_corrompido = zf.testzip()
                    if membro_corrompido:
                        raise zipfile.BadZipFile(
                            f"membro corrompido: {membro_corrompido}"
                        )
                return conteudo
            except (requests.RequestException, zipfile.BadZipFile, OSError) as exc:
                falhas.append(f"{url} (tentativa {tentativa}/4): {exc}")
                if tentativa < 4:
                    time.sleep(3 * tentativa)
    raise RuntimeError("Não foi possível baixar um COTAHIST íntegro da B3. " + " | ".join(falhas))


def carregar_cotacoes_b3() -> tuple[dict[str, dict], str]:
    """Último fechamento à vista por ticker; o BDI 96 é o mercado fracionário."""
    hoje = datetime.now(timezone.utc).date()
    corte = hoje - timedelta(days=365)
    cotacoes: dict[str, dict] = {}

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
                    if linha[10:12].strip() not in {"02", "96"} or linha[24:27].strip() != "010":
                        continue
                    ticker = linha[12:24].strip().upper()
                    if not ticker:
                        continue
                    try:
                        data_pregao = datetime.strptime(linha[2:10], "%Y%m%d").date()
                        preco = int(linha[108:121].strip() or "0") / 100
                        negocios = int(linha[147:152].strip() or "0")
                    except (ValueError, TypeError):
                        continue
                    if data_pregao < corte or data_pregao > hoje or preco <= 0:
                        continue
                    atual = cotacoes.get(ticker)
                    if atual is None or data_pregao > atual["data"]:
                        cotacoes[ticker] = {
                            "preco": preco,
                            "data": data_pregao,
                            "especie": linha[39:49].strip().upper(),
                            "negocios": negocios,
                        }

    if not cotacoes:
        raise RuntimeError("COTAHIST da B3 não retornou cotações à vista recentes.")
    data_maxima = max(item["data"] for item in cotacoes.values()).isoformat()
    return cotacoes, data_maxima


def carregar_mapa_tickers(cotacoes: dict[str, dict]) -> tuple[dict[str, list[str]], int]:
    csv_bytes = baixar(TICKERS_URL)
    ativos = pd.read_csv(io.BytesIO(csv_bytes), dtype=str).fillna("")
    obrigatorias = {"ticker", "cnpj", "classe"}
    if not obrigatorias.issubset(ativos.columns):
        raise RuntimeError(f"CSV de tickers sem colunas esperadas: {sorted(ativos.columns)}")
    ativos["classe_norm"] = ativos["classe"].str.strip().str.casefold()
    ativos["ticker"] = ativos["ticker"].str.strip().str.upper()
    # O COTAHIST anual inclui códigos que já trocaram de ticker. Consideramos
    # negociável apenas o código com pregão recente, usando a data mais recente
    # encontrada no próprio arquivo como referência (não o relógio da máquina).
    data_referencia = max(item["data"] for item in cotacoes.values())
    limite_recencia = data_referencia - timedelta(days=60)
    tickers_recentes = {
        ticker for ticker, cotacao in cotacoes.items()
        if cotacao["data"] >= limite_recencia and cotacao["negocios"] > 0
    }
    acoes = ativos[
        ativos["classe_norm"].isin(["acao", "ações", "acoes"])
        & ativos["ticker"].isin(tickers_recentes)
    ]
    mapa: dict[str, set[str]] = {}
    for _, linha in acoes.iterrows():
        cnpj = cnpj_limpo(linha["cnpj"])
        ticker = linha["ticker"]
        if len(cnpj) == 14 and ticker:
            mapa.setdefault(cnpj, set()).add(ticker)
    return {cnpj: sorted(tickers) for cnpj, tickers in mapa.items()}, len(acoes)


def ler_csv_zip(conteudo: bytes, nome: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        candidatos = [n for n in zf.namelist() if n.lower().endswith(".csv") and nome.lower() in n.lower()]
        if not candidatos:
            raise RuntimeError(f"Arquivo {nome} não encontrado no ZIP.")
        with zf.open(candidatos[0]) as arquivo:
            return pd.read_csv(
                arquivo, sep=";", encoding="latin1",
                dtype={"CNPJ_CIA": str}, low_memory=False
            )


def indexar_por_cnpj(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if df.empty or "CNPJ_CIA" not in df.columns:
        return {}
    return {cnpj_limpo(cnpj): grupo for cnpj, grupo in df.groupby("CNPJ_CIA", sort=False)}


def valor_escalado(linha) -> float | None:
    valor = numero(linha.get("VL_CONTA"))
    if valor is None:
        return None
    escala = normalizar_texto(linha.get("ESCALA_MOEDA", ""))
    if escala in {"MIL", "MILHAR", "MILHARES"}:
        valor *= 1000
    elif escala in {"MILHAO", "MILHOES"}:
        valor *= 1_000_000
    return valor


def localizar_conta(df: pd.DataFrame | None, codigos=(), termos=()) -> float | None:
    if df is None or df.empty:
        return None
    dados = df
    if "ORDEM_EXERC" in dados.columns:
        ultimo = dados[dados["ORDEM_EXERC"].astype(str).str.normalize("NFKD").str.encode("ascii", errors="ignore").str.decode("ascii").str.upper().str.contains("ULTIMO", na=False)]
        if not ultimo.empty:
            dados = ultimo
    if "VERSAO" in dados.columns:
        dados = dados.assign(_versao=pd.to_numeric(dados["VERSAO"], errors="coerce").fillna(0))
    if "CD_CONTA" in dados.columns:
        codigos = [str(c) for c in codigos]
        for codigo in codigos:
            achados = dados[dados["CD_CONTA"].astype(str).str.strip() == codigo]
            if not achados.empty:
                if "_versao" in achados.columns:
                    achados = achados.sort_values("_versao")
                return valor_escalado(achados.iloc[-1])
    if termos and "DS_CONTA" in dados.columns:
        descricoes = dados["DS_CONTA"].fillna("").astype(str).map(normalizar_texto)
        for termo in termos:
            alvo = normalizar_texto(termo)
            achados = dados[descricoes == alvo]
            if not achados.empty:
                if "_versao" in achados.columns:
                    achados = achados.sort_values("_versao")
                return valor_escalado(achados.iloc[-1])
    return None


def localizar_depreciacao(df: pd.DataFrame | None) -> float | None:
    if df is None or df.empty or "DS_CONTA" not in df.columns:
        return None
    dados = df
    if "ORDEM_EXERC" in dados.columns:
        ordem = dados["ORDEM_EXERC"].astype(str).map(normalizar_texto)
        ultimo = dados[ordem == "ULTIMO"]
        if not ultimo.empty:
            dados = ultimo
    descricoes = dados["DS_CONTA"].fillna("").astype(str).map(normalizar_texto)
    mascara = descricoes.str.contains("DEPRECIACAO|AMORTIZACAO|EXAUSTAO", regex=True, na=False)
    candidatos = dados[mascara].copy()
    if candidatos.empty:
        return None
    # Mantém apenas contas-folha para não somar uma conta-pai com seus componentes.
    codigos = candidatos.get("CD_CONTA", pd.Series("", index=candidatos.index)).astype(str)
    pais = set()
    for codigo in codigos:
        partes = codigo.split(".")
        pais.update(".".join(partes[:i]) for i in range(1, len(partes)))
    candidatos = candidatos.loc[[idx for idx, codigo in codigos.items() if codigo not in pais]]
    valores = [valor_escalado(linha) for _, linha in candidatos.iterrows()]
    valores = [abs(v) for v in valores if v is not None]
    return sum(valores) if valores else None


def linha_capital(df: pd.DataFrame | None):
    if df is None or df.empty:
        return None
    dados = df.copy()
    if "VERSAO" in dados.columns:
        dados["_versao"] = pd.to_numeric(dados["VERSAO"], errors="coerce").fillna(0)
        dados = dados.sort_values("_versao")
    return dados.iloc[-1]


def capital_coluna(linha, candidatos) -> float:
    if linha is None:
        return 0.0
    mapa = {normalizar_texto(coluna): coluna for coluna in linha.index}
    for candidato in candidatos:
        coluna = mapa.get(normalizar_texto(candidato))
        if coluna:
            return numero(linha.get(coluna)) or 0.0
    # Compatibilidade com nomes abreviados usados em algumas versões do arquivo.
    for chave, coluna in mapa.items():
        if any(normalizar_texto(c) in chave for c in candidatos):
            valor = numero(linha.get(coluna))
            if valor is not None:
                return valor
    return 0.0


def quantidades_acoes(linha) -> dict[str, float]:
    # A CVM permite informar quantidades em unidades ou milhares de ações.
    # A escala vem no próprio formulário de composição do capital.
    multiplicador = 1.0
    if linha is not None:
        # Nos arquivos DFP de composição de capital, a escala pode vir
        # identificada genericamente como ESCALA_MOEDA (sem "ACAO" no nome).
        # Ela se aplica aos valores numéricos do formulário, inclusive às
        # quantidades de ações exportadas em milhares.
        colunas_escala = [coluna for coluna in linha.index if "ESCALA" in normalizar_texto(coluna)]
        for coluna in colunas_escala:
            escala = normalizar_texto(linha.get(coluna))
            if escala in {"MIL", "MILHAR", "MILHARES", "MILUNIDADE", "MILUNIDADES"}:
                multiplicador = 1000.0
                break

    on = capital_coluna(linha, [
        "QT_ACAO_ORDINARIA_CAPITAL_INTEGRALIZADO",
        "QT_ACAO_ORDINARIA_CAPITAL_INTEGR",
        "QT_ACAO_ORDIN_CAP_INTEGR",
    ])
    pn = capital_coluna(linha, [
        "QT_ACAO_PREFERENCIAL_CAPITAL_INTEGRALIZADO",
        "QT_ACAO_PREFERENCIAL_CAPITAL_INTEGR",
        "QT_ACAO_PREF_CAP_INTEGR",
    ])
    total = capital_coluna(linha, [
        "QT_ACAO_TOTAL_CAPITAL_INTEGRALIZADO",
        "QT_ACAO_TOTAL_CAPITAL_INTEGR",
        "QT_ACAO_TOTAL_CAP_INTEGR",
    ])
    on_tes = capital_coluna(linha, ["QT_ACAO_ORDINARIA_TESOURARIA", "QT_ACAO_ORDIN_TESOURO"])
    pn_tes = capital_coluna(linha, ["QT_ACAO_PREFERENCIAL_TESOURARIA", "QT_ACAO_PREF_TESOURO"])
    total_tes = capital_coluna(linha, ["QT_ACAO_TOTAL_TESOURARIA", "QT_ACAO_TOTAL_TESOURO"])
    on = max(0.0, on - on_tes) * multiplicador
    pn = max(0.0, pn - pn_tes) * multiplicador
    total = total * multiplicador
    total_tes *= multiplicador
    if total <= 0:
        total = on + pn
    else:
        total = max(0.0, total - total_tes)
    if on + pn > 0:
        total = on + pn
    return {"on": on, "pn": pn, "total": total}


def calcular_valor_mercado(tickers: list[str], cotacoes: dict[str, dict], capital) -> float | None:
    quantidades = quantidades_acoes(capital)
    cotacoes_validas = [(t, cotacoes[t]) for t in tickers if t in cotacoes]
    if not cotacoes_validas:
        return None

    por_tipo = {"on": [], "pn": [], "outro": []}
    for ticker, cotacao in cotacoes_validas:
        especie = cotacao["especie"]
        if especie.startswith("ON") or ticker.endswith("3"):
            tipo = "on"
        elif especie.startswith("PN") or ticker[-1:] in {"4", "5", "6", "7", "8"}:
            tipo = "pn"
        else:
            tipo = "outro"
        por_tipo[tipo].append((ticker, cotacao))

    # Sem classe identificável, só é seguro usar o total quando há um único ticker.
    if not por_tipo["on"] and not por_tipo["pn"]:
        # Units (por exemplo, sufixo 11) não equivalem a uma ação individual.
        if len(cotacoes_validas) == 1 and not cotacoes_validas[0][0].endswith("11") and quantidades["total"] > 0:
            return cotacoes_validas[0][1]["preco"] * quantidades["total"]
        return None

    valor_mercado = 0.0
    classes_precificadas = 0
    for tipo in ("on", "pn"):
        quantidade = quantidades[tipo]
        if quantidade <= 0:
            continue
        opcoes = por_tipo[tipo]
        if not opcoes:
            return None
        # Se houver mais de um ticker da mesma classe, usa a série mais negociada
        # como referência, pois a CVM informa a quantidade agregada por classe.
        _, cotacao = max(opcoes, key=lambda item: (item[1]["negocios"], item[1]["data"]))
        valor_mercado += cotacao["preco"] * quantidade
        classes_precificadas += 1
    if classes_precificadas == 0 or valor_mercado <= 0:
        return None
    return valor_mercado


def main():
    cadastro_url = f"{BASE}/CAD/DADOS/cad_cia_aberta.csv"
    cadastro = pd.read_csv(
        io.BytesIO(baixar(cadastro_url)), sep=";", encoding="latin1",
        dtype={"CNPJ_CIA": str}
    )
    if "SIT" in cadastro.columns:
        cadastro = cadastro[cadastro["SIT"].astype(str).str.contains("ATIVO", case=False, na=False)]
    cadastro = cadastro.drop_duplicates("CNPJ_CIA", keep="last")
    nomes = {cnpj_limpo(cnpj): nome for cnpj, nome in zip(cadastro["CNPJ_CIA"], cadastro["DENOM_SOCIAL"])}
    cnpjs_ativos_cvm = set(nomes)

    cotacoes, data_cotahist = carregar_cotacoes_b3()
    mapa_tickers, total_ativos_b3 = carregar_mapa_tickers(cotacoes)
    if not mapa_tickers:
        raise RuntimeError("O mapeamento de tickers retornou vazio; a base anterior será preservada.")

    historico: dict[str, dict[int, dict]] = {}
    capitais: dict[str, object] = {}
    anos_ok = []
    erros = []
    ano_atual = datetime.now(timezone.utc).year
    # 2025 e 2024 atendem aos saldos atuais/médios; 2020 é a base do CAGR de cinco anos.
    anos_dados = [ano_atual - 1, ano_atual - 2, ano_atual - 6]

    for ano in anos_dados:
        try:
            zip_bytes = baixar(f"{BASE}/DOC/DFP/DADOS/dfp_cia_aberta_{ano}.zip")
            dre_idx = indexar_por_cnpj(ler_csv_zip(zip_bytes, "DRE_con"))
            if ano >= ano_atual - 2:
                bpa_idx = indexar_por_cnpj(ler_csv_zip(zip_bytes, "BPA_con"))
                bpp_idx = indexar_por_cnpj(ler_csv_zip(zip_bytes, "BPP_con"))
            else:
                bpa_idx, bpp_idx = {}, {}
            if ano == ano_atual - 1:
                try:
                    dfc_idx = indexar_por_cnpj(ler_csv_zip(zip_bytes, "DFC_MI_con"))
                except RuntimeError:
                    dfc_idx = {}
                try:
                    capital_idx = indexar_por_cnpj(ler_csv_zip(zip_bytes, "composicao_capital"))
                except RuntimeError:
                    capital_idx = {}
            else:
                dfc_idx, capital_idx = {}, {}
            anos_ok.append(ano)
        except Exception as exc:
            erros.append(f"{ano}: {str(exc)[:180]}")
            continue

        for cnpj_key, dre in dre_idx.items():
            if cnpj_key not in cnpjs_ativos_cvm or cnpj_key not in mapa_tickers:
                continue
            bpa = bpa_idx.get(cnpj_key)
            bpp = bpp_idx.get(cnpj_key)
            dfc = dfc_idx.get(cnpj_key)
            receita = localizar_conta(dre, ["3.01"], ["Receita de Venda de Bens e/ou Serviços"])
            lucro = localizar_conta(dre, ["3.11", "3.11.01"], [
                "Lucro/Prejuízo Consolidado do Período",
                "Lucro/Prejuízo do Período",
            ])
            ebit = localizar_conta(dre, ["3.05"], [
                "Resultado Antes do Resultado Financeiro e dos Tributos",
            ])
            resultado_antes_impostos = localizar_conta(dre, ["3.07"], [
                "Resultado Antes dos Tributos sobre o Lucro",
            ])
            imposto = localizar_conta(dre, ["3.08"], [
                "Imposto de Renda e Contribuição Social sobre o Lucro",
            ])
            caixa = localizar_conta(bpa, ["1.01.01"], [
                "Caixa e Equivalentes de Caixa",
            ])
            divida_cp = localizar_conta(bpp, ["2.01.04"], [
                "Empréstimos e Financiamentos",
            ])
            divida_lp = localizar_conta(bpp, ["2.02.01"], [
                "Empréstimos e Financiamentos",
            ])
            patrimonio = localizar_conta(bpp, ["2.03"], [
                "Patrimônio Líquido Consolidado",
                "Patrimônio Líquido",
            ])
            depreciacao = localizar_depreciacao(dfc)

            capital = linha_capital(capital_idx.get(cnpj_key))
            if capital is not None and cnpj_key not in capitais:
                capitais[cnpj_key] = capital
                if cnpj_key == "07526557000100":
                    quant_debug = quantidades_acoes(capital)
                    escalas_debug = {
                        str(col): str(capital.get(col))
                        for col in capital.index if "ESCALA" in normalizar_texto(col)
                    }
                    print(f"DEBUG ABEV3 composição de capital: escalas={escalas_debug}; quantidades={quant_debug}")

            historico.setdefault(cnpj_key, {})[ano] = {
                "receita_liquida": receita,
                "lucro_liquido": lucro,
                "ebit": ebit,
                "resultado_antes_impostos": resultado_antes_impostos,
                "imposto": imposto,
                "caixa": caixa,
                "divida_cp": divida_cp,
                "divida_lp": divida_lp,
                "patrimonio": patrimonio,
                "depreciacao": depreciacao,
            }

    empresas = []
    for cnpj_key, anos in historico.items():
        if not anos:
            continue
        ano_ref = max(anos)
        atual = anos[ano_ref]
        anterior = anos.get(ano_ref - 1, {})

        receita = atual["receita_liquida"]
        lucro = atual["lucro_liquido"]
        patrimonio = atual["patrimonio"]
        caixa = atual["caixa"]
        divida_bruta = None
        if atual["divida_cp"] is not None or atual["divida_lp"] is not None:
            divida_bruta = (atual["divida_cp"] or 0) + (atual["divida_lp"] or 0)
        divida_liquida = (
            divida_bruta - (caixa or 0)
            if divida_bruta is not None and caixa is not None else None
        )
        ebitda = (
            atual["ebit"] + atual["depreciacao"]
            if atual["ebit"] is not None and atual["depreciacao"] is not None else None
        )

        roe = None
        if lucro is not None and patrimonio is not None:
            pl_anterior = anterior.get("patrimonio")
            pl_medio = (patrimonio + pl_anterior) / 2 if pl_anterior is not None else None
            if pl_medio and pl_medio > 0:
                roe = lucro / pl_medio * 100

        margem = lucro / receita * 100 if lucro is not None and receita not in (None, 0) else None

        roic = None
        if atual["ebit"] is not None and patrimonio is not None and divida_liquida is not None:
            capital_investido = patrimonio + divida_liquida
            ebitda_anterior = anterior.get("ebit")
            patrimonio_anterior = anterior.get("patrimonio")
            caixa_anterior = anterior.get("caixa")
            divida_cp_anterior = anterior.get("divida_cp")
            divida_lp_anterior = anterior.get("divida_lp")
            if (
                patrimonio_anterior is not None
                and divida_cp_anterior is not None
                and divida_lp_anterior is not None
                and caixa_anterior is not None
            ):
                capital_anterior = patrimonio_anterior + divida_cp_anterior + divida_lp_anterior - caixa_anterior
                capital_medio = (capital_investido + capital_anterior) / 2
                ebt = atual["resultado_antes_impostos"]
                tax_rate = min(0.34, max(0.0, abs(atual["imposto"] or 0) / abs(ebt))) if ebt not in (None, 0) else 0.34
                if capital_medio > 0:
                    roic = atual["ebit"] * (1 - tax_rate) / capital_medio * 100

        divida_ebitda = (
            divida_liquida / ebitda
            if divida_liquida is not None and ebitda is not None and ebitda > 0 else None
        )

        lucro_inicio = anos.get(ano_ref - 5, {}).get("lucro_liquido")
        cagr = None
        if lucro_inicio is not None and lucro is not None and lucro_inicio > 0 and lucro > 0:
            cagr = ((lucro / lucro_inicio) ** (1 / 5) - 1) * 100

        tickers = mapa_tickers.get(cnpj_key, [])
        valor_mercado = calcular_valor_mercado(tickers, cotacoes, capitais.get(cnpj_key))
        pl_ratio = valor_mercado / lucro if valor_mercado is not None and lucro is not None and lucro > 0 else None
        pvp = valor_mercado / patrimonio if valor_mercado is not None and patrimonio is not None and patrimonio > 0 else None

        empresa = nomes.get(cnpj_key, cnpj_key)
        empresas.append({
            "cnpj": cnpj_key,
            "empresa": empresa,
            "tickers": tickers,
            "fonte": "CVM - DFP consolidada; B3 - COTAHIST",
            "ano_referencia": ano_ref,
            "receita_liquida": round(receita, 2) if receita is not None else None,
            "lucro_liquido": round(lucro, 2) if lucro is not None else None,
            "pvp": round(pvp, 2) if pvp is not None else None,
            "pl": round(pl_ratio, 2) if pl_ratio is not None else None,
            "roe": round(roe, 2) if roe is not None else None,
            "margem_liquida": round(margem, 2) if margem is not None else None,
            "roic": round(roic, 2) if roic is not None else None,
            "divida_ebitda": round(divida_ebitda, 2) if divida_ebitda is not None else None,
            "cagr_lucros": round(cagr, 2) if cagr is not None else None,
            "valor_mercado": round(valor_mercado, 2) if valor_mercado is not None else None,
        })

    linhas = []
    for emissor in empresas:
        for ticker in emissor["tickers"]:
            linha = dict(emissor)
            linha["ticker"] = ticker
            linha["tickers"] = [ticker]
            linhas.append(linha)
    linhas.sort(key=lambda x: (x["empresa"], x["ticker"]))
    empresas_com_ticker = len({item["cnpj"] for item in linhas})

    if not linhas:
        raise RuntimeError("A coleta não gerou linhas de tickers; a base anterior será preservada.")

    payload = {
        "atualizado_em": datetime.now(timezone.utc).isoformat(),
        "fonte": "CVM - Dados Abertos (DFP consolidada); B3 - COTAHIST; universo de ativos Lummios (CC BY 4.0)",
        "url_fonte": "https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp",
        "url_cotacoes": "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/historico/mercado-a-vista/cotacoes-historicas/",
        "url_mapa_tickers": "https://developers.lummios.com.br/dados-abertos.html",
        "licenca_mapa_tickers": "CC BY 4.0 - atribuição à Lummios",
        "anos_processados": sorted(anos_ok),
        "ano_referencia_fundamentos": max(anos_ok) if anos_ok else None,
        "data_ultimo_pregao_b3": data_cotahist,
        "quantidade_empresas": empresas_com_ticker,
        "quantidade_linhas_ticker": len(linhas),
        "quantidade_emissores_cvm_com_dados": len(empresas),
        "quantidade_ativos_acao_no_mapa": total_ativos_b3,
        "metodologia": {
            "p_l": "Valor de mercado estimado / lucro líquido consolidado anual; só exibido com lucro positivo.",
            "pvp": "Valor de mercado estimado / patrimônio líquido consolidado.",
            "roe": "Lucro líquido consolidado / patrimônio líquido médio entre dois exercícios.",
            "margem_liquida": "Lucro líquido consolidado / receita líquida.",
            "roic": "EBIT após imposto estimado / capital investido médio; taxa tributária limitada a 34%.",
            "divida_ebitda": "Dívida de curto e longo prazo menos caixa, dividida por EBITDA (EBIT + depreciação/amortização).",
            "cagr_lucros": "Crescimento anual composto do lucro líquido em cinco anos; requer lucro positivo nos dois extremos.",
            "valor_mercado": "Preço de fechamento mais recente da B3 multiplicado pelas ações em circulação informadas na composição de capital da CVM, respeitando a escala de quantidade (unidades ou milhares); quando há mais de uma série da mesma classe, usa a mais negociada como referência.",
            "ticker_ativo": "Ticker incluído somente se teve ao menos um negócio nos últimos 60 dias corridos em relação ao pregão mais recente encontrado no COTAHIST.",
        },
        "observacao": "Indicadores contábeis usam o último exercício anual DFP disponível, não valores TTM. P/L e P/VP dependem de preço recente e composição de capital; a escala da quantidade de ações (unidade/milhar) é aplicada conforme o formulário da CVM. Quando os dados necessários não são suficientes, o indicador fica nulo. ROE e ROIC usam médias de saldos entre exercícios. Instituições financeiras podem não ter EBITDA/ROIC comparáveis. Tickers sem negociação nos últimos 60 dias são excluídos para evitar códigos antigos após mudança de ticker. Cada classe/ticker aparece em uma linha; indicadores do emissor se repetem entre classes.",
        "erros": erros,
        "empresas": linhas,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    indicadores = ["pvp", "pl", "roe", "margem_liquida", "roic", "divida_ebitda", "cagr_lucros"]
    cobertura = {chave: sum(1 for item in linhas if item[chave] is not None) for chave in indicadores}
    print(
        f"JSON gerado: {OUT} | empresas={empresas_com_ticker} | tickers={len(linhas)} "
        f"| cotações={len(cotacoes)} | cobertura={cobertura} | anos={anos_ok} | erros={erros}"
    )


if __name__ == "__main__":
    main()
