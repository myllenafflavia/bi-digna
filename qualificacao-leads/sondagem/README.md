# Sondagem da API RD Conversas

Primeira etapa do sistema de qualificação de leads do Digna. O script confere, com as credenciais da Digna, o que a documentação oficial da API RD Conversas não publica:

- se `GET /v2/customers` e `GET /v4/reports` trazem o ID do contato, o que define se a descoberta de contatos novos pode ser automática;
- quais campos cada mensagem do histórico traz (ID, remetente, data, tipo);
- se o histórico inclui mensagens do bot e cliques em botão quando pedidos;
- se `end_date` é inclusivo.

## Como rodar

1. Abra o notebook `extrair_conversas_rd_station_lote.ipynb` no Colab.
2. Confira nos Segredos do Colab se `RD_CONVERSAS_TOKEN` e `RD_CONVERSAS_JWK` estão com acesso liberado.
3. Rode só as três primeiras células: instalação, configuração e funções.
4. Crie uma célula nova, cole o conteúdo de `sondagem_api_rd_conversas.py` e rode.
5. Copie a saída inteira, até "Fim da sondagem", e leve para a análise.

## Segurança

O script só faz requisições GET (cerca de 15) e não altera nada no RD. A saída mostra a estrutura das respostas, campos de enumeração e de data e contagens. Nome, telefone e texto de mensagem não são impressos.

Nenhuma credencial fica neste arquivo: ela vem dos Segredos do Colab.
