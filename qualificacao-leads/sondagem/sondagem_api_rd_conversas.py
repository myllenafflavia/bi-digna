"""Sondagem da API RD Conversas (Tallos) para o sistema de qualificação de leads Digna.

Roda pelo GitHub Actions (workflow "Sondar API RD Conversas") ou localmente:

    RD_CONVERSAS_TOKEN=... RD_CONVERSAS_JWK='{"kty":"RSA",...}' python sondagem_api_rd_conversas.py

O script não imprime nomes, telefones nem textos de mensagens. Mostra apenas a
estrutura das respostas (chaves e tipos), valores de campos de enumeração e de
data, e contagens. Faz cerca de 20 requisições GET e não altera nada no RD.

As funções de sessão, consulta e descriptografia vêm do notebook
extrair_conversas_rd_station_lote.ipynb, adaptadas para variáveis de ambiente.
"""

import gzip
import json
import os
import re
import sys
import zlib
from datetime import date, timedelta

import requests
from jwcrypto import jwe, jwk
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://api.tallos.com.br/v2"
BASE_V4 = "https://api.tallos.com.br/v4"


# ---------------------------------------------------------------- credenciais

def carregar_credenciais():
    token = (os.environ.get("RD_CONVERSAS_TOKEN") or "").strip()
    jwk_text = os.environ.get("RD_CONVERSAS_JWK") or ""
    if not token:
        sys.exit("Falta o segredo RD_CONVERSAS_TOKEN.")
    try:
        dados = json.loads(jwk_text)
        if dados.get("kty") != "RSA" or not dados.get("d"):
            raise ValueError()
        jwk.JWK(**dados)
    except (ValueError, TypeError, AttributeError):
        sys.exit("Confira o segredo RD_CONVERSAS_JWK: precisa ser o JSON da chave privada RSA.")
    return token, dados


TOKEN, KEY_DATA = carregar_credenciais()


# ------------------------------------------------------- consulta (do notebook)

class ConsultaFalhou(Exception):
    pass


_session = None


def sessao():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
        retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods={"GET"}, raise_on_status=False, respect_retry_after_header=True)
        _session.mount("https://api.tallos.com.br", HTTPAdapter(max_retries=retry))
    return _session


def get(path, params, aceita_404=False, base=BASE):
    try:
        response = sessao().get(base + path, params=params, timeout=(10, 45), allow_redirects=False)
    except requests.RequestException:
        raise ConsultaFalhou("falha_de_conexao") from None
    if response.status_code == 404 and aceita_404:
        return None
    if response.status_code in {401, 403}:
        raise PermissionError(f"HTTP {response.status_code}. Confira credenciais, permissões e plano do Conversas.")
    if response.status_code != 200:
        raise ConsultaFalhou(f"http_{response.status_code}")
    try:
        return response.json()
    except ValueError:
        return response.text.strip()


JWE_COMPACTO = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){4}"


def abrir_payload(payload):
    # Aceita JWE compacto, serialização JSON e envelopes com os dados cifrados.
    if isinstance(payload, str) and re.fullmatch(JWE_COMPACTO, payload.strip()):
        serialized = payload.strip()
    elif isinstance(payload, dict) and {"ciphertext", "iv", "tag"} <= payload.keys():
        serialized = json.dumps(payload)
    else:
        serialized = None
    if serialized is not None:
        try:
            encrypted = jwe.JWE()
            encrypted.deserialize(serialized, key=jwk.JWK(**KEY_DATA))
        except (jwe.InvalidJWEData, jwe.InvalidJWEOperation, ValueError):
            raise ConsultaFalhou("jwe_falha_na_descriptografia") from None

        bruto = encrypted.payload
        try:
            if bruto.startswith(b"\x1f\x8b"):
                bruto = gzip.decompress(bruto)
            elif (len(bruto) >= 2 and bruto[0] & 15 == 8
                  and bruto[0] >> 4 <= 7 and int.from_bytes(bruto[:2], "big") % 31 == 0):
                bruto = zlib.decompress(bruto)
        except (OSError, EOFError, zlib.error):
            raise ConsultaFalhou("jwe_compactacao_invalida") from None

        try:
            descompactador = zlib.decompressobj(-zlib.MAX_WBITS)
            dados = descompactador.decompress(bruto, 10 * 1024 * 1024)
            if descompactador.eof and not descompactador.unused_data:
                candidato = json.loads(dados, strict=False)
                if isinstance(candidato, (dict, list)):
                    return abrir_payload(candidato)
        except (zlib.error, ValueError):
            pass

        if bruto.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            texto = bruto.decode("utf-32")
        elif bruto.startswith((b"\xff\xfe", b"\xfe\xff")):
            texto = bruto.decode("utf-16")
        else:
            texto = bruto.decode("utf-8-sig", errors="surrogateescape")

            def recuperar_byte(match):
                return bytes([ord(match.group()) - 0xDC00]).decode("cp1252", errors="replace")

            texto = re.sub(r"[\udc80-\udcff]", recuperar_byte, texto)
        texto = texto.strip()
        if re.fullmatch(JWE_COMPACTO, texto):
            return abrir_payload(texto)
        try:
            return abrir_payload(json.loads(texto, strict=False))
        except json.JSONDecodeError as erro:
            raise ConsultaFalhou(f"jwe_json_invalido_posicao_{erro.pos}") from None
    if isinstance(payload, dict):
        for field in ("data", "messages", "items", "history", "payload",
                      "encrypted", "encryptedData", "encrypted_data", "jwe", "result"):
            if field in payload and payload[field] is not None:
                return abrir_payload(payload[field])
    return payload


# ------------------------------------------------------------ saída sem dados

CAMPOS_VISIVEIS = {
    "sent_by", "sender_type", "type", "status", "channel", "channels",
    "is_template_message", "page", "pages", "limit", "total",
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
    if isinstance(obj, bool) or obj is None:
        return obj
    if chave and (chave in CAMPOS_VISIVEIS or any(p in chave for p in PALAVRAS_DATA)):
        return obj if isinstance(obj, (int, float)) or len(str(obj)) <= 40 else f"<texto, {len(str(obj))} caracteres>"
    if isinstance(obj, str):
        return f"<texto, {len(obj)} caracteres>"
    return f"<{type(obj).__name__}>"


def mostrar(titulo, obj):
    print(f"\n===== {titulo} =====")
    print(json.dumps(estrutura(obj), ensure_ascii=False, indent=2, default=str)[:6000])


def tentar(titulo, path, params, base=BASE):
    try:
        bruto = get(path, params, aceita_404=True, base=base)
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
        # Envelope antes de abrir, para ver metadados de paginação.
        envelope = {k: (f"lista com {len(v)} itens" if isinstance(v, list) else v) for k, v in bruto.items()}
        mostrar(f"{titulo} | envelope", envelope)
    try:
        aberto = abrir_payload(bruto)
    except ConsultaFalhou as erro:
        print(f"\n===== {titulo} =====\nRESPOSTA RECEBIDA, MAS NÃO ABRIU: {erro}")
        return None
    mostrar(titulo, aberto)
    return aberto


def primeiro_id(resposta):
    """Procura o primeiro ID de contato em uma lista de itens."""
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


# -------------------------------------------------------------------- testes

def main():
    hoje = date.today()
    ontem = hoje - timedelta(days=1)

    # Teste 1: listagem de contatos (candidata à descoberta, rota 2)
    clientes = tentar("1. GET /v2/customers (página 1, limit 5)",
                      "/customers", {"limit": 5, "page": 1, "channels": "whatsapp"})
    tentar("1b. GET /v2/customers (página 2, limit 5)",
           "/customers", {"limit": 5, "page": 2, "channels": "whatsapp"})

    # Teste 2: relatório de atendimentos (candidata à descoberta, rota 1), API v4
    relatorio = tentar("2. GET /v4/reports type=customers (ontem a hoje)", "/reports", {
        "start_date": ontem.isoformat(), "end_date": hoje.isoformat(),
        "type": "customers", "channel": "whatsapp", "page": 1, "limit": 5,
    }, base=BASE_V4)

    # Teste 3: histórico de um contato, com bot e respostas de botão incluídos
    customer_id = primeiro_id(relatorio) or primeiro_id(clientes)
    print(f"\nContato usado nos testes 3 e 4: {'encontrado' if customer_id else 'nenhum ID encontrado'}")
    if not customer_id:
        return

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
        ("cliente, texto e botões", {"type": "text,button_reply,quick_reply,list_reply",
                                     "sent_by": "customer"}),
    ]:
        print(f"{rotulo}: {contar_historico(customer_id, extra)}")

    # Teste 4: semântica de datas. Compara um dia isolado com o intervalo até o dia seguinte.
    print("\n===== 4. Semântica de start_date/end_date =====")
    for dias_atras in (1, 2, 3, 7):
        dia = hoje - timedelta(days=dias_atras)
        todos = {"sent_by": "bot,customer,operator"}
        mesmo_dia = contar_historico(customer_id, {"start_date": dia.isoformat(),
                                                   "end_date": dia.isoformat(), **todos})
        ate_seguinte = contar_historico(customer_id, {"start_date": dia.isoformat(),
                                                      "end_date": (dia + timedelta(days=1)).isoformat(), **todos})
        print(f"{dia}: só o dia = {mesmo_dia} | dia até o seguinte = {ate_seguinte}")


if __name__ == "__main__":
    main()
    print("\nFim da sondagem.")
