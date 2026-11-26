# BI Comercial · Grupo Digna

Painel de marketing e vendas ligado ao RD Station CRM. Os dados são atualizados automaticamente a cada hora pelo GitHub Actions, e o painel é publicado pelo GitHub Pages.

## Como funciona

1. A ação **Sincronizar RD CRM** (`.github/workflows/sync-rd.yml`) roda de hora em hora.
2. Ela executa `scripts/sync-rd.mjs`, que lê na API do RD todos os funis, etapas e negócios (todas as páginas) e grava:
   - `data/bi.json`: os dados que o painel usa;
   - `data/history.json`: as movimentações de etapa registradas a cada sincronização;
   - `data/status.json`: o resultado da última sincronização.
3. O `index.html` lê esses arquivos e monta o painel.

O token do RD fica guardado como **segredo do repositório**. Ele nunca é gravado em arquivo e não aparece no painel.

Nome, e-mail e telefone dos contatos e o nome do negócio **não** são gravados. Cada contato vira um código irreversível, usado apenas para contar pessoas únicas.

## Configuração (uma vez)

1. **Segredo do token:** em *Settings → Secrets and variables → Actions → New repository secret*, crie `RD_CRM_TOKEN` com o token da API do RD CRM.
2. **Segredo para os códigos de contato (recomendado):** crie também `RD_HASH_SALT` com qualquer texto longo e aleatório. Se mudar depois, a contagem de contatos únicos recomeça.
3. **Permissão de escrita:** em *Settings → Actions → General → Workflow permissions*, marque *Read and write permissions*.
4. **Primeira carga:** em *Actions → Sincronizar RD CRM → Run workflow*. A primeira carga pode levar alguns minutos.
5. **Publicação:** em *Settings → Pages*, escolha *Deploy from a branch*, branch `main`, pasta `/ (root)`.

O endereço do painel aparece em *Settings → Pages*.

## Meta Ads (Ads Insights API)

A ação **Sincronizar Meta Ads** (`.github/workflows/sync-meta.yml`) roda de hora em hora e executa `scripts/sync-meta.mjs`. Ela:

- consulta `/act_{conta}/insights` por dia e por anúncio, com conta, campanha, conjunto, anúncio, investimento, impressões, alcance, frequência, cliques, cliques no link, CTR, CPC, CPM, ações e custos por ação (leads e conversas iniciadas);
- reprocessa os últimos 7 dias a cada rodada, substituindo esses dias inteiros, sem duplicar registros;
- consulta alcance e frequência já no nível e no período exibidos (dia, mês, semana, 7, 30 e 90 dias, ano), porque alcance não pode ser somado;
- trata paginação, limites da API e falha por conta: uma conta com erro mantém os dados anteriores e não interrompe as outras;
- verifica a validade do token a cada rodada e avisa quando faltam 15 dias ou menos para expirar.

Contas permitidas (definidas no workflow, `META_AD_ACCOUNTS`): Plano Digna `1444452782074905`, Amo Flores `2000431566857709`, Grupo Digna `311468502055498`, Parque da Paz `195768907609096`.

**Configuração:**

1. Crie o segredo `META_ACCESS_TOKEN` (*Settings → Secrets and variables → Actions*) com um token que tenha a permissão `ads_read` nas quatro contas.
2. Opcional, em *Variables*: `META_API_VERSION` (padrão `v26.0`) e `META_START` (padrão `2025-01-01`, início da carga histórica).
3. Rode *Actions → Sincronizar Meta Ads → Run workflow*. A primeira carga busca o histórico mês a mês e pode levar várias rodadas se a API limitar as chamadas; o progresso fica salvo.

**Renovação do token:** tokens de usuário duram cerca de 60 dias. Para não ter interrupção, o ideal é um token de **usuário do sistema** do Business Manager (não expira), com acesso às quatro contas e permissão `ads_read`. Ao trocar, basta atualizar o segredo.

O token vai apenas no cabeçalho das chamadas: não aparece em URLs, arquivos, commits, logs nem no navegador.

## Metas e regras

Na tela **Configurações** do painel dá para definir metas mensais, a operação de cada funil (Plano Digna ou Parque da Paz), a etapa que conta como qualificação e as equivalências entre etapas. As mudanças valem na hora para quem editou. Para valer para todo o time, clique em **Copiar configuração** e cole o conteúdo no arquivo `config.json` deste repositório.

## Limitações conhecidas

- **Histórico de etapas:** a API do RD informa só a etapa atual de cada negócio. O painel registra as movimentações a partir da primeira sincronização. A análise de passagem pelas etapas vale para negócios criados depois disso e fica mais completa com o tempo.
- **Mídia:** investimento, CPL e custos ficam como "indisponível" até a integração com o Reportei ou com as contas de anúncio ser feita.
- **Visibilidade:** em repositório público, os arquivos de `data/` podem ser acessados por qualquer pessoa com o endereço. Eles não têm dados pessoais de contatos, mas têm volumes, origens, campanhas e nomes de responsáveis.

## Variáveis opcionais do script

| Variável | Padrão | Uso |
|---|---|---|
| `RD_START` | `2018-01-01` | Data mais antiga buscada na API |
| `RD_PAUSE_MS` | `400` | Pausa entre chamadas, para respeitar o limite da API |
