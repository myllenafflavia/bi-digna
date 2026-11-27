# Sondagem da API RD Conversas

Primeira etapa do sistema de qualificação de leads do Digna. O script confere, com as credenciais da Digna, o que a documentação oficial da API RD Conversas não publica:

- se `GET /v2/customers` e `GET /v4/reports` trazem o ID do contato, o que define se a descoberta de contatos novos pode ser automática;
- quais campos cada mensagem do histórico traz (ID, remetente, data, tipo);
- se o histórico inclui mensagens do bot e cliques em botão quando pedidos;
- se `end_date` é inclusivo.

## Configuração (uma vez)

Em *Settings → Secrets and variables → Actions → New repository secret*, crie:

1. `RD_CONVERSAS_TOKEN`: o token da API do RD Conversas.
2. `RD_CONVERSAS_JWK`: o JSON inteiro da chave privada, gerada no RD Conversas em *Apps e Integrações → API → Gerar Chave*.

São os mesmos valores que hoje estão nos Segredos do Colab.

## Como rodar

1. Em *Actions*, escolha **Sondar API RD Conversas** e clique em *Run workflow*.
2. Abra a rodada, clique no job **sondar** e expanda a etapa **Sondar a API**.
3. Copie o texto da etapa, até "Fim da sondagem", e leve para a análise.

## Segurança

O script só faz requisições GET (cerca de 20) e não grava nada no repositório. O registro mostra a estrutura das respostas, campos de enumeração e de data e contagens. Nome, telefone e texto de mensagem não são impressos.

Este repositório é público, então o registro das rodadas também é. Por isso a saída foi desenhada para não conter dados pessoais. As credenciais ficam só nos segredos do repositório.
