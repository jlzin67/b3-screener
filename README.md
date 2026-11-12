# B3 Screener

Screener fundamentalista independente para ações brasileiras.

## Fontes e atualização

- **CVM Dados Abertos — DFP:** demonstrações financeiras padronizadas consolidadas.
- **GitHub Actions:** coleta agendada em dias úteis e grava `data/indicadores.json`.
- **GitHub Pages:** publica a interface estática.

Fonte oficial: https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp

## Estado desta versão

A integração inicial coleta o cadastro de companhias abertas e tenta obter demonstrações DFP consolidadas. A interface lê o JSON e permite pesquisar empresas e filtrar os campos disponíveis.

**Limitações importantes:** o cadastro CVM identifica emissores por CNPJ, não por ticker. Ainda não há correspondência validada de tickers nem fonte de cotações. Por isso P/L e P/VP ficam indisponíveis. ROE, ROIC, dívida líquida/EBITDA e CAGR também ficam nulos até que o mapeamento de contas e períodos seja validado. Margem líquida só é calculada quando as contas esperadas são encontradas. Campos vazios não significam zero.

## Executar manualmente

1. Abra a aba **Actions**.
2. Selecione **Atualizar dados fundamentalistas**.
3. Clique em **Run workflow**.

O workflow também é agendado de segunda a sexta-feira.

## Próximas etapas

1. Validar o mapeamento das contas CVM por setor e período.
2. Integrar tickers e classes de ações da B3.
3. Integrar cotações com fonte e horário de referência.
4. Implementar e testar os sete indicadores.
