# Sondagem da API RD Conversas (Tallos) para o sistema de qualificação de leads Digna
#
# Como usar no Colab:
# 1. Abra o notebook extrair_conversas_rd_station_lote.ipynb.
# 2. Rode as três primeiras células (instalação, configuração e funções).
#    Elas definem BASE, get(), abrir_payload() e as credenciais via Segredos do Colab.
# 3. Cole este arquivo inteiro em uma célula nova e rode.
# 4. Copie a saída e envie para análise.
#
# O script não imprime nomes, telefones nem textos de mensagens. Ele mostra apenas
# a estrutura das respostas (chaves e tipos), valores de campos de enumeração e de data,
# e contagens. Faz no máximo cerca de 15 requisições GET e não altera nada no RD.

import json
from datetime import date, timedelta

CAMPOS_VISIVEIS = {
    "sent_by", "sender_type", "type", "status", "channel", "channels",
    "is_template_message", "kty", "page", "pages", "limit", "total",
    "total_pages", "count", "has_more", "next",
}
PALAVRAS_DATA = ("date", "_at", "At", "time", "timestamp")


def estrutura(obj, chave=None, nivel=0, max_nivel=4):
    """Devolve a forma do objeto sem expor dados pessoais."""
    if nivel > max_nivel:
        return "..."
    if isinstance(obj, dict):
        return {k: estrutura(v, k, nivel + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        if not obj:
            return []
        return [f"lista com {len(obj)} itens; primeiro:", estrutura(obj[0], chave, nivel + 1)]
    if chave and (chave in CAMPOS_VISIVEIS or any(p in chave for p in PALAVRAS_DATA)):
        return obj
    if isinstance(obj, str):
        return f"<texto, {len(obj)} caracteres>"
    if isinstance(obj, bool) or obj is None:
        return obj
    return f"<{type(obj).__name__}>"


def mostrar(titulo, obj):
    print(f"\n===== {titulo} =====")
    print(json.dumps(estrutura(obj), ensure_ascii=False, indent=2, default=str)[:6000])


def tentar(titulo, path, params):
    try:
        bruto = get(path, params, aceita_404=True)
    except PermissionError as erro:
        print(f"\n===== {titulo} =====\nSEM PERMISSÃO: {erro}")
        return None
    except ConsultaFalhou as erro:
        print(f"\n===== {titulo} =====\nFALHOU: {erro}")
        return None
    if bruto is None:
        print(f"\n===== {titulo} =====\n404")
        return None
    if isinstance(bruto, dict):
        # Mostra o envelope antes de abrir, para ver metadados de paginação.
        mostrar(f"{titulo} | envelope", {k: v for k, v in bruto.items() if not isinstance(v, list)}
                | {k: f"lista com {len(v)} itens" for k, v in bruto.items() if isinstance(v, list)})
    try:
        aberto = abrir_payload(bruto)
    except ConsultaFalhou as erro:
        print(f"\n===== {titulo} =====\nRESPOSTA RECEBIDA, MAS NÃO ABRIU: {erro}")
        return None
    mostrar(titulo, aberto)
    return aberto


def primeiro_id(resposta):
    """Procura o primeiro _id ou id de contato em uma lista de itens."""
    itens = resposta
    if isinstance(resposta, dict):
        for campo in ("data", "items", "customers", "contacts", "results", "docs"):
            if isinstance(resposta.get(campo), list):
                itens = resposta[campo]
                break
    if not isinstance(itens, list):
        return None
    for item in itens:
        if not isinstance(item, dict):
            continue
        for campo in ("customer_id", "_id", "id"):
            if isinstance(item.get(campo), str):
                return item[campo]
        cliente = item.get("customer")
        if isinstance(cliente, dict):
            for campo in ("_id", "id"):
                if isinstance(cliente.get(campo), str):
                    return cliente[campo]
        if isinstance(cliente, str):
            return cliente
    return None


def contar_historico(customer_id, extra):
    params = {"customer_id": customer_id, "channel": "whatsapp", "limit": 100, "page": 1}
    params.update(extra)
    try:
        lote = abrir_payload(get("/messages/history", params))
    except (ConsultaFalhou, PermissionError) as erro:
        return f"falhou: {erro}"
    if not isinstance(lote, list):
        return f"formato {type(lote).__name__}"
    return len(lote)


hoje = date.today()
ontem = hoje - timedelta(days=1)

# Teste 1: listagem de contatos (candidata à descoberta, rota 2)
clientes = tentar("1. GET /v2/customers (página 1, limit 5)",
                  "/customers", {"limit": 5, "page": 1, "channels": "whatsapp"})
tentar("1b. GET /v2/customers (página 2, limit 5)",
       "/customers", {"limit": 5, "page": 2, "channels": "whatsapp"})

# Teste 2: relatório de atendimentos (candidata à descoberta, rota 1). Usa a API v4.
BASE_V4 = BASE.replace("/v2", "/v4")
try:
    resposta = sessao().get(BASE_V4 + "/reports", timeout=(10, 45), allow_redirects=False, params={
        "start_date": ontem.isoformat(), "end_date": hoje.isoformat(),
        "type": "customers", "channel": "whatsapp", "page": 1, "limit": 5,
    })
    print(f"\n===== 2. GET /v4/reports type=customers (ontem a hoje) =====\nHTTP {resposta.status_code}")
    if resposta.status_code == 200:
        try:
            relatorio = abrir_payload(resposta.json())
        except ValueError:
            relatorio = resposta.text
        mostrar("2. estrutura", relatorio)
    else:
        relatorio = None
except Exception as erro:  # noqa: BLE001
    print(f"\n===== 2. GET /v4/reports =====\nFALHOU: {type(erro).__name__}")
    relatorio = None

# Teste 3: histórico de um contato real, com bot e respostas de botão incluídos
customer_id = primeiro_id(relatorio) or primeiro_id(clientes)
print(f"\nContato usado nos testes 3 e 4: {'encontrado' if customer_id else 'nenhum ID encontrado'}")

if customer_id:
    tentar("3. GET /v2/messages/history com sent_by e type ampliados", "/messages/history", {
        "customer_id": customer_id, "channel": "whatsapp", "limit": 3, "page": 1,
        "sent_by": "bot,customer,operator",
        "type": "text,button_reply,quick_reply,list_reply,interactive",
    })

    print("\n===== 3b. Contagens por remetente (primeira página, até 100) =====")
    for rotulo, extra in [
        ("padrão (sem sent_by)", {}),
        ("sent_by=bot", {"sent_by": "bot"}),
        ("sent_by=customer", {"sent_by": "customer"}),
        ("sent_by=operator", {"sent_by": "operator"}),
        ("sent_by=bot,customer,operator (vírgula)", {"sent_by": "bot,customer,operator"}),
        ("sent_by repetido", {"sent_by": ["bot", "customer", "operator"]}),
        ("type=text,button_reply,quick_reply,list_reply", {"type": "text,button_reply,quick_reply,list_reply",
                                                           "sent_by": "customer"}),
    ]:
        print(f"{rotulo}: {contar_historico(customer_id, extra)}")

    # Teste 4: semântica de datas. Compara um dia isolado com o intervalo até o dia seguinte.
    print("\n===== 4. Semântica de start_date/end_date =====")
    for dias_atras in (1, 2, 3, 7):
        dia = hoje - timedelta(days=dias_atras)
        mesmo_dia = contar_historico(customer_id, {"start_date": dia.isoformat(), "end_date": dia.isoformat(),
                                                   "sent_by": "bot,customer,operator"})
        ate_seguinte = contar_historico(customer_id, {"start_date": dia.isoformat(),
                                                      "end_date": (dia + timedelta(days=1)).isoformat(),
                                                      "sent_by": "bot,customer,operator"})
        print(f"{dia}: só o dia = {mesmo_dia} | dia até o seguinte = {ate_seguinte}")

print("\nFim da sondagem. Copie toda a saída acima.")
