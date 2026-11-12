# B3 Screener

Screener fundamentalista independente para ações brasileiras.

## Fontes e atualização

- **CVM Dados Abertos — DFP:** demonstrações financeiras padronizadas consolidadas.
- **Lummios — Universo de ativos:** relação ticker/CNPJ das ações da B3, distribuída sob licença CC BY 4.0. A associação é feita pelo CNPJ do emissor; classes diferentes podem aparecer juntas.
- **GitHub Actions:** coleta agendada em dias úteis e grava `data/indicadores.json`.
- **GitHub Pages:** publica a interface estática.

Fontes:
- CVM: https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp
- Lummios: https://developers.lummios.com.br/dados-abertos.html

Atribuição do mapa de ativos: LUMMIOS, “Universo de ativos da Lummios”, versão 2026-10-26, licença CC BY 4.0.

## Estado desta versão

A rotina associa tickers de ações ao cadastro financeiro da CVM por CNPJ. Quando uma companhia possui mais de uma classe negociada, os códigos são exibidos juntos. Companhias sem correspondência permanecem sem ticker; não é feita associação aproximada por nome.

A coleta também obtém demonstrações DFP consolidadas. Ainda não há fonte de cotações integrada. Por isso P/L e P/VP ficam indisponíveis. ROE, ROIC, dívida líquida/EBITDA e CAGR também ficam nulos até que o mapeamento de contas e períodos seja validado. Margem líquida só é calculada quando as contas esperadas são encontradas. Campos vazios não significam zero.

## Executar manualmente

1. Abra a aba **Actions**.
2. Selecione **Atualizar dados fundamentalistas**.
3. Clique em **Run workflow**.

O workflow também é agendado de segunda a sexta-feira.

## Próximas etapas

1. Validar o mapeamento de contas CVM por setor e período.
2. Integrar cotações com fonte e horário de referência.
3. Implementar e testar os indicadores restantes.
