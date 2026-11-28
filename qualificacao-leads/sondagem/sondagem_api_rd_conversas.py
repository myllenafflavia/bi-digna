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
#
# Rodada 2. A rodada 1 mostrou que /v4/reports traz o ID do contato e que filtros
# de lista precisam ir como parâmetro repetido (sent_by=bot&sent_by=customer),
# não separados por vírgula. Esta rodada confirma o que cada item representa,
# o formato das tags e dos campos de mensagem, e a semântica das datas.

TODOS_REMETENTES = ["bot", "customer", "operator"]
TIPOS_TEXTO = ["text", "button_reply", "quick_reply", "list_reply", "interactive"]


def itens_relatorio(inicio, fim, extra=None, limit=100, max_paginas=5):
    """Lê até max_paginas do relatório e devolve (total, itens)."""
    itens, total = [], None
    for page in range(1, max_paginas + 1):
        params = {"start_date": inicio.isoformat(), "end_date": fim.isoformat(),
                  "type": "customers", "channel": "whatsapp", "page": page, "limit": limit}
        params.update(extra or {})
        resposta = get("/reports", params, base=BASE_V4)
        if not isinstance(resposta, dict):
            break
        total = resposta.get("total")
        docs = resposta.get("docs") or []
        itens.extend(d for d in docs if isinstance(d, dict))
        if page >= (resposta.get("pages") or 1):
            break
    return total, itens


def data_da_mensagem(item):
    for chave, valor in item.items():
        if any(p in chave for p in ("created", "date", "timestamp", "sent_at")) and valor:
            return chave, str(valor)
    return None, None


def historico_completo(customer_id, extra=None):
    mensagens = []
    for page in range(1, 21):
        params = {"customer_id": customer_id, "channel": "whatsapp", "limit": 100, "page": page,
                  "sent_by": TODOS_REMETENTES, "type": TIPOS_TEXTO}
        params.update(extra or {})
        lote = abrir_payload(get("/messages/history", params))
        if not isinstance(lote, list) or not lote:
            break
        mensagens.extend(abrir_payload(m) for m in lote)
        if len(lote) < 100:
            break
    return mensagens


def main():
    hoje = date.today()
    ontem = hoje - timedelta(days=1)

    # Teste 5: o que cada item do relatório representa
    print("\n===== 5. Relatório type=customers: o que cada item representa =====")
    for rotulo, extra in [("padrão", {}), ("status=opened", {"status": "opened"}),
                          ("status=closed", {"status": "closed"})]:
        try:
            total, _ = itens_relatorio(ontem, hoje, extra, limit=1, max_paginas=1)
            print(f"ontem a hoje, {rotulo}: total = {total}")
        except (ConsultaFalhou, PermissionError) as erro:
            print(f"ontem a hoje, {rotulo}: falhou ({erro})")
    try:
        total, _ = itens_relatorio(ontem, ontem, limit=1, max_paginas=1)
        print(f"só ontem ({ontem} a {ontem}): total = {total}")
    except (ConsultaFalhou, PermissionError) as erro:
        print(f"só ontem: falhou ({erro})")
    for dias in (0, 7, 30):
        try:
            total, _ = itens_relatorio(hoje - timedelta(days=dias), hoje, limit=1, max_paginas=1)
            print(f"últimos {dias} dias até hoje: total = {total}")
        except (ConsultaFalhou, PermissionError) as erro:
            print(f"últimos {dias} dias: falhou ({erro})")

    total, itens = None, []
    for limite in (50, 30, 20, 10, 5):
        try:
            total, itens = itens_relatorio(ontem, hoje, limit=limite, max_paginas=max(1, 100 // limite))
            print(f"maior limit aceito entre os testados: {limite}")
            break
        except ConsultaFalhou as erro:
            print(f"limit={limite}: {erro}")
        except PermissionError as erro:
            print(f"Relatório sem permissão: {erro}")
            return
    if not itens:
        return
    ids = [i.get("id") for i in itens]
    criados = [str(i.get("created_at") or "")[:10] for i in itens]
    print(f"itens lidos: {len(itens)} de {total} | IDs únicos: {len(set(ids))}")
    print(f"created_at dentro da janela ({ontem} a {hoje}): {sum(c >= ontem.isoformat() for c in criados)}")
    print(f"created_at antes da janela: {sum(bool(c) and c < ontem.isoformat() for c in criados)}")
    print(f"created_at mais antigo: {min((c for c in criados if c), default='nenhum')}")
    print(f"itens com contact_origin preenchido: {sum(bool(i.get('contact_origin')) for i in itens)}")
    com_tags = [i for i in itens if i.get("tags")]
    print(f"itens com tags: {len(com_tags)}")
    if com_tags:
        mostrar("5b. formato de tags (primeiro item com tags)", com_tags[0]["tags"])
    extra = next((i.get("extra_data") for i in itens if isinstance(i.get("extra_data"), dict)), {})
    for campo in ("last_message_sent", "last_message_received"):
        valor = str(extra.get(campo) or "")
        print(f"extra_data.{campo}: {len(valor)} caracteres, só dígitos = {valor.isdigit()}")

    # Teste 6: campos de mensagem, com o contato mais antigo da página (mais chance de histórico)
    candidatos = sorted((i for i in itens if i.get("id")), key=lambda i: str(i.get("created_at") or "9"))
    contato = None
    for item in candidatos[:15]:
        try:
            msgs = historico_completo(item["id"])
        except (ConsultaFalhou, PermissionError):
            continue
        if msgs:
            contato, mensagens = item["id"], msgs
            break
    if not contato:
        print("\nNenhum contato da página com mensagens encontradas.")
        return
    print(f"\nContato com histórico encontrado: {len(mensagens)} mensagens (todos os remetentes e tipos)")
    mostrar("6. formato de uma mensagem", mensagens[0])
    contagem = {}
    for m in mensagens:
        if isinstance(m, dict):
            chave = (str(m.get("sent_by") or m.get("sender_type") or "?"), str(m.get("type") or "?"))
            contagem[chave] = contagem.get(chave, 0) + 1
    print("contagem por (remetente, tipo):")
    for (rem, tipo), n in sorted(contagem.items()):
        print(f"  {rem} / {tipo}: {n}")
    exemplo_botao = next((m for m in mensagens if isinstance(m, dict)
                          and m.get("type") in ("button_reply", "quick_reply", "list_reply", "interactive")), None)
    if exemplo_botao:
        mostrar("6b. formato de uma resposta de botão", exemplo_botao)

    # Teste 7: semântica de start_date/end_date com um dia em que há mensagens
    chave_data, _ = data_da_mensagem(mensagens[0]) if isinstance(mensagens[0], dict) else (None, None)
    print(f"\n===== 7. Datas (campo usado: {chave_data}) =====")
    if not chave_data:
        return
    dias = sorted({str(m.get(chave_data))[:10] for m in mensagens if isinstance(m, dict) and m.get(chave_data)})
    print(f"dias com mensagens (UTC, pelo texto do campo): {dias[-5:]}")
    for dia_txt in dias[-2:]:
        dia = date.fromisoformat(dia_txt)
        esperado = sum(str(m.get(chave_data))[:10] == dia_txt for m in mensagens if isinstance(m, dict))
        resultados = {}
        for rotulo, ini, fim in [("só o dia", dia, dia), ("dia até o seguinte", dia, dia + timedelta(days=1)),
                                 ("dia anterior até o dia", dia - timedelta(days=1), dia)]:
            try:
                resultados[rotulo] = len(historico_completo(
                    contato, {"start_date": ini.isoformat(), "end_date": fim.isoformat()}))
            except (ConsultaFalhou, PermissionError) as erro:
                resultados[rotulo] = f"falhou ({erro})"
        print(f"{dia_txt}: mensagens nesse dia UTC = {esperado} | " +
              " | ".join(f"{k} = {v}" for k, v in resultados.items()))
    for formato, valor in [("data e hora ISO", f"{dias[-1]}T00:00:00"),
                           ("data e hora com fuso -03:00", f"{dias[-1]}T00:00:00-03:00")]:
        try:
            n = len(historico_completo(contato, {"start_date": valor, "end_date": valor[:10] + "T23:59:59"}))
            print(f"formato {formato}: aceito, {n} mensagens")
        except (ConsultaFalhou, PermissionError) as erro:
            print(f"formato {formato}: {erro}")


def rodada_4():
    """Contatos antigos que voltam a conversar: testa os outros tipos de relatório."""
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    novos = set()
    try:
        _, itens = itens_relatorio(ontem, hoje, limit=50, max_paginas=4)
        novos = {i.get("id") for i in itens if i.get("id")}
    except (ConsultaFalhou, PermissionError) as erro:
        print(f"type=customers falhou: {erro}")
    print(f"\n===== 8. Referência: contatos novos (type=customers) lidos: {len(novos)} =====")

    for tipo in ("operators", "chatbots", "rejecteds", None):
        rotulo = tipo or "sem type"
        params = {"start_date": ontem.isoformat(), "end_date": hoje.isoformat(),
                  "channel": "whatsapp", "page": 1, "limit": 50}
        if tipo:
            params["type"] = tipo
        try:
            resposta = get("/reports", params, base=BASE_V4)
        except (ConsultaFalhou, PermissionError) as erro:
            print(f"\n===== 8. type={rotulo} =====\nfalhou: {erro}")
            continue
        if isinstance(resposta, dict):
            envelope = {k: (f"lista com {len(v)} itens" if isinstance(v, list) else v) for k, v in resposta.items()}
            mostrar(f"8. type={rotulo} | envelope", envelope)
        docs = resposta.get("docs") if isinstance(resposta, dict) else resposta
        if not isinstance(docs, list) or not docs:
            print(f"type={rotulo}: sem itens")
            continue
        mostrar(f"8. type={rotulo} | primeiro item", docs[0])
        # Procura IDs de contato dentro dos itens e compara com os contatos novos.
        ids = set()
        for d in docs:
            if not isinstance(d, dict):
                continue
            for campo in ("customer_id", "customer", "contact", "contact_id"):
                v = d.get(campo)
                if isinstance(v, dict):
                    v = v.get("_id") or v.get("id")
                if isinstance(v, str):
                    ids.add(v)
        print(f"type={rotulo}: IDs de contato encontrados nos itens = {len(ids)} | "
              f"também na lista de novos = {len(ids & novos)} | fora dela = {len(ids - novos)}")


if __name__ == "__main__":
    rodada_4()
    print("\nFim da sondagem.")
