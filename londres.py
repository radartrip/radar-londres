# Radar Londres - v1.0 (fase de teste)
# Mede os menores precos de ida e volta saindo de LONDRES (todos os aeroportos) para
# 14 cidades do Brasil, em libras, guarda o historico de cada rota e avisa no Telegram
# quando um preco cai bem abaixo do normal. Tambem le o Secret Flying (erros de tarifa)
# e avisa quando aparece Londres -> Brasil.
#
# Sem afiliado: os precos da Travelpayouts servem so de termometro; o botao do alerta
# abre a busca no Google Flights. Nenhum link leva codigo de comissao.
#
# Descobre sozinho, pelo proprio Telegram:
#   - o DONO: a primeira pessoa que mandar mensagem no privado do robo (/start)
#   - o CANAL: o canal em que o robo for colocado como administrador
# Enquanto nao houver canal, os alertas chegam ao dono como "previa da vitrine".

import html
import json
import os
import re
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

VERSAO = "1.0"
TZ_LONDRES = ZoneInfo("Europe/London")
ORIGEM = "LON"

# Para incluir ou tirar um destino, edite esta lista: "CODIGO": "Nome"
DESTINOS = {
    "REC": "Recife",
    "FOR": "Fortaleza",
    "SSA": "Salvador",
    "NAT": "Natal",
    "BSB": "Brasília",
    "FLN": "Florianópolis",
    "SAO": "São Paulo",
    "POA": "Porto Alegre",
    "CWB": "Curitiba",
    "BEL": "Belém",
    "RIO": "Rio de Janeiro",
    "VIX": "Vitória",
    "BHZ": "Belo Horizonte",
    "GYN": "Goiânia",
}

QUEDA_MINIMA = 0.20      # alerta se ficar 20% ou mais abaixo da media
NOVA_QUEDA = 0.05        # dentro do silencio, so repete se cair mais 5%
SILENCIO_HORAS = 48      # nao repete o mesmo destino por 48h
DIAS_MEDIA = 7           # media dos ultimos 7 dias
MIN_LEITURAS = 12        # so compara depois de 12 leituras da rota
INTERVALO_MIN = 50       # consulta os precos no maximo 1 vez por hora
MAX_LEITURAS = 1500      # cerca de 2 meses de historico por rota
MESES_PLANO_C = 6        # busca mes a mes ate 6 meses a frente
HORA_QUADRO = 8          # quadro diario nos bastidores a partir das 8h de Londres
FAMILIA = 4              # "familia de 4 economiza..."
SECRET_FLYING_DIAS = 7   # na estreia, mostra achados dos ultimos 7 dias

PASTA = "dados"
ARQ = os.path.join(PASTA, "londres.json")
API = "https://api.travelpayouts.com"
SECRET_FLYING_RSS = "https://www.secretflying.com/feed/"

CIAS = {"BA": "British Airways", "LA": "LATAM", "JJ": "LATAM", "TP": "TAP", "IB": "Iberia",
        "UX": "Air Europa", "AF": "Air France", "KL": "KLM", "LH": "Lufthansa", "LX": "Swiss",
        "AZ": "ITA", "TK": "Turkish", "EK": "Emirates", "QR": "Qatar", "ET": "Ethiopian",
        "AT": "Royal Air Maroc", "AV": "Avianca", "CM": "Copa", "AD": "Azul", "G3": "Gol",
        "VS": "Virgin Atlantic", "AA": "American", "UA": "United", "DL": "Delta"}

RODAPE = ("<i>Não vendemos passagens nem ganhamos comissão: a compra é feita direto no site da "
          "companhia ou agência, que responde pela venda e pelo voo. Preço de buscas recentes: "
          "confira no link antes de comprar.</i>")

# Palavras que o Secret Flying usa para Londres e para o Brasil (sem acento, minusculas)
TERMOS_LONDRES = ["london", "londres", "heathrow", "gatwick", "stansted", "luton", "united kingdom", " uk "]
TERMOS_BRASIL = ["brazil", "brasil", "sao paulo", "rio de janeiro", "recife", "fortaleza", "salvador",
                 "natal", "brasilia", "florianopolis", "porto alegre", "curitiba", "belem", "vitoria",
                 "belo horizonte", "goiania", "manaus", "maceio", "joao pessoa", "porto seguro"]


# ---------------- utilidades ----------------

def agora():
    return datetime.now(timezone.utc)


def sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def libras(valor):
    return "£" + f"{valor:,.0f}".replace(",", ".")


def data_br(iso):
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        return "?"


def hora_londres(momento):
    local = momento.astimezone(TZ_LONDRES)
    return local.strftime("%Hh%M"), local.strftime("%d/%m")


def link_google(destino, ida, volta):
    q = f"Flights from {ORIGEM} to {destino}"
    if ida and volta:
        q += f" on {ida} through {volta}"
    return ("https://www.google.com/travel/flights?hl=pt-BR&gl=GB&curr=GBP&q="
            + urllib.parse.quote(q))


def escalas_txt(n):
    if n is None or n == "":
        return ""
    n = int(n)
    return "direto" if n == 0 else ("1 escala" if n == 1 else f"{n} escalas")


def baixar(url, dados=None, cabecalhos=None, tempo=25):
    req = urllib.request.Request(url, data=dados, headers=cabecalhos or {"User-Agent": "RadarLondres/1.0"})
    with urllib.request.urlopen(req, timeout=tempo) as r:
        return r.read()


# ---------------- estado ----------------

def estado_novo():
    return {"versao": VERSAO, "historico": {}, "ultimo_alerta": {}, "contador": 0,
            "dono": None, "canal": None, "canal_nome": "", "offset": 0, "sf_vistos": [],
            "ultimo_quadro": "", "ultima_leitura": "", "estreia": True}


def carregar():
    try:
        with open(ARQ, encoding="utf-8") as f:
            estado = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return estado_novo()
    base = estado_novo()
    base.update(estado)
    return base


def salvar(estado):
    os.makedirs(PASTA, exist_ok=True)
    with open(ARQ, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


# ---------------- Telegram ----------------

def telegram(metodo, campos, token):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    dados = urllib.parse.urlencode(campos).encode("utf-8")
    resposta = json.loads(baixar(url, dados, {"User-Agent": "RadarLondres/1.0"}).decode("utf-8"))
    if not resposta.get("ok"):
        raise RuntimeError(resposta.get("description") or "Telegram recusou")
    return resposta.get("result")


def enviar(chat, texto, token):
    if not chat:
        print("Sem destino ainda (mande /start para o robô). Mensagem não enviada:\n" + texto)
        return
    try:
        telegram("sendMessage", {"chat_id": chat, "text": texto, "parse_mode": "HTML",
                                 "disable_web_page_preview": "true"}, token)
    except Exception as erro:
        print(f"Telegram falhou: {erro}")


def bastidores(estado, texto, token):
    enviar(estado.get("dono"), texto, token)


def vitrine(estado, texto, token):
    if estado.get("canal"):
        enviar(estado["canal"], texto, token)
    else:
        enviar(estado.get("dono"), "👁️ <b>Prévia da vitrine</b> (canal ainda não conectado)\n\n" + texto, token)


def descobrir(estado, token):
    """Le as novidades do robo: quem mandou /start (dono) e em que canal ele virou administrador."""
    try:
        novidades = telegram("getUpdates", {"offset": estado.get("offset", 0), "timeout": 0,
                                            "allowed_updates": json.dumps(["message", "my_chat_member"])}, token)
    except Exception as erro:
        print(f"getUpdates falhou: {erro}")
        return
    for u in novidades or []:
        estado["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private":
            if not estado.get("dono"):
                estado["dono"] = chat["id"]
                enviar(chat["id"], "🛰️ <b>Radar Londres</b> ligado! Você é o dono: os bastidores "
                       "(quadro diário e avisos técnicos) chegam aqui.\n\nPróximo passo: crie o canal "
                       "e coloque este robô como administrador.", token)
            elif chat["id"] != estado["dono"]:
                enviar(chat["id"], "Oi! Os alertas do Radar Londres saem no canal oficial. 🛰️", token)
        mcm = u.get("my_chat_member") or {}
        canal = mcm.get("chat") or {}
        novo = (mcm.get("new_chat_member") or {}).get("status")
        if canal.get("type") == "channel" and novo == "administrator":
            estado["canal"] = canal["id"]
            estado["canal_nome"] = canal.get("title", "")
            bastidores(estado, f"✅ Canal conectado: <b>{html.escape(estado['canal_nome'])}</b>. "
                       "A partir de agora os alertas saem lá.", token)
        elif canal.get("type") == "channel" and canal.get("id") == estado.get("canal") and novo in ("left", "kicked", "member"):
            estado["canal"] = None
            bastidores(estado, "⚠️ O robô deixou de ser administrador do canal. Os alertas voltam a chegar aqui como prévia.", token)


# ---------------- precos (Travelpayouts, so termometro) ----------------

def chamar(caminho, params, token):
    url = API + caminho + "?" + urllib.parse.urlencode(params)
    corpo = baixar(url, cabecalhos={"X-Access-Token": token, "Accept-Encoding": "identity",
                                    "User-Agent": "RadarLondres/1.0"})
    resposta = json.loads(corpo.decode("utf-8"))
    if resposta.get("success") is False:
        raise RuntimeError(resposta.get("error") or "resposta sem sucesso")
    dados = resposta.get("data") or []
    return list(dados.values()) if isinstance(dados, dict) else list(dados)


def so_ida_e_volta(opcoes):
    return [o for o in opcoes if o.get("price") and (o.get("return_at") or "")]


def meses_a_frente(qtd, base):
    ano, mes, meses = base.year, base.month, []
    for _ in range(qtd + 1):
        meses.append(f"{ano:04d}-{mes:02d}")
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return meses


def busca_datas(destino, token):
    return chamar("/aviasales/v3/prices_for_dates", {
        "origin": ORIGEM, "destination": destino, "currency": "gbp",
        "sorting": "price", "one_way": "false", "limit": 30}, token)


def busca_mes(destino, token):
    meses = meses_a_frente(MESES_PLANO_C, agora())
    opcoes, erros = [], 0
    for i in range(MESES_PLANO_C):
        for volta in (meses[i], meses[i + 1]):
            try:
                opcoes += chamar("/aviasales/v3/prices_for_dates", {
                    "origin": ORIGEM, "destination": destino, "currency": "gbp",
                    "departure_at": meses[i], "return_at": volta,
                    "sorting": "price", "one_way": "false", "limit": 30}, token)
            except Exception:
                erros += 1
    if erros == MESES_PLANO_C * 2:
        raise RuntimeError("todas as buscas mês a mês falharam")
    return opcoes


def busca_semana(destino, token):
    dados = chamar("/aviasales/v3/get_latest_prices", {
        "origin": ORIGEM, "destination": destino, "currency": "gbp", "period_type": "year",
        "one_way": "false", "sorting": "price", "limit": 30, "page": 1}, token)
    return [{"price": o.get("value"), "departure_at": o.get("depart_date") or "",
             "return_at": o.get("return_date") or "", "airline": "",
             "transfers": o.get("number_of_changes")} for o in dados]


BUSCAS = [("datas", busca_datas), ("mes", busca_mes), ("semana", busca_semana)]


def consultar(destino, token):
    erros = []
    for gaveta, busca in BUSCAS:
        try:
            opcoes = so_ida_e_volta(busca(destino, token))
        except Exception as erro:
            erros.append(f"{gaveta}: {erro}")
            continue
        if not opcoes:
            continue
        m = min(opcoes, key=lambda o: o["price"])
        return {"preco": int(round(m["price"])), "ida": (m.get("departure_at") or "")[:10],
                "volta": (m.get("return_at") or "")[:10],
                "cia": CIAS.get(m.get("airline") or "", m.get("airline") or ""),
                "escalas": m.get("transfers"), "gaveta": gaveta}
    if len(erros) == len(BUSCAS):
        raise RuntimeError("; ".join(erros))
    return None


def media_recente(leituras, gaveta, momento):
    limite = momento - timedelta(days=DIAS_MEDIA)
    valores = [l["preco"] for l in leituras
               if l.get("gaveta") == gaveta and datetime.fromisoformat(l["t"]) >= limite]
    return (sum(valores) / len(valores), len(valores)) if valores else (None, 0)


def texto_alerta(destino, achado, media, codigo, momento):
    cidade = DESTINOS[destino]
    pct = round((1 - achado["preco"] / media) * 100)
    economia = int(round((media - achado["preco"]) * FAMILIA, -1))
    hh, dd = hora_londres(momento)
    detalhes = " · ".join(x for x in [f"{data_br(achado['ida'])} → {data_br(achado['volta'])}",
                                      achado.get("cia") or "", escalas_txt(achado.get("escalas"))] if x)
    return (f"🛰️ <b>PREÇO ABAIXO DO NORMAL</b>\n"
            f"<b>Londres → {html.escape(cidade)}</b>\n\n"
            f"💷 <b>{libras(achado['preco'])}</b> ida e volta · 1 adulto · com taxas\n"
            f"📉 {pct}% abaixo do normal da rota ({libras(media)})\n"
            f"👨‍👩‍👧‍👦 Família de {FAMILIA} economiza cerca de {libras(economia)}\n"
            f"🗓️ {html.escape(detalhes)}\n"
            f"🕐 Preço conferido às {hh} de {dd} (hora de Londres)\n\n"
            f"👉 <a href=\"{html.escape(link_google(destino, achado['ida'], achado['volta']))}\">Ver no Google Flights</a>\n"
            f"Código {codigo}\n\n{RODAPE}")


def medir(estado, token, momento):
    """Uma leitura por destino; devolve (alertas enviados, linhas do quadro, falhas)."""
    alertas, quadro, falhas = 0, [], []
    for destino, cidade in DESTINOS.items():
        try:
            achado = consultar(destino, token)
        except Exception as erro:
            falhas.append(f"{cidade}: {erro}")
            continue
        if not achado:
            quadro.append((None, f"• {cidade}: sem preço no cache agora"))
            continue
        leituras = estado["historico"].setdefault(destino, [])
        media, n = media_recente(leituras, achado["gaveta"], momento)
        leituras.append({"t": momento.isoformat(), **achado})
        del leituras[:-MAX_LEITURAS]
        linha = (f"• {cidade}: <b>{libras(achado['preco'])}</b> ({data_br(achado['ida'])} → "
                 f"{data_br(achado['volta'])})")
        if media and n >= MIN_LEITURAS:
            linha += f" · normal {libras(media)}"
        quadro.append((achado["preco"], linha))
        if not media or n < MIN_LEITURAS or achado["preco"] > media * (1 - QUEDA_MINIMA):
            continue
        ultimo = estado["ultimo_alerta"].get(destino)
        if ultimo:
            desde = momento - datetime.fromisoformat(ultimo["t"])
            if desde < timedelta(hours=SILENCIO_HORAS) and achado["preco"] > ultimo["preco"] * (1 - NOVA_QUEDA):
                continue
        estado["contador"] += 1
        codigo = f"RL-{estado['contador']:04d}"
        vitrine(estado, texto_alerta(destino, achado, media, codigo, momento), token)
        estado["ultimo_alerta"][destino] = {"t": momento.isoformat(), "preco": achado["preco"], "codigo": codigo}
        alertas += 1
    quadro.sort(key=lambda x: (x[0] is None, x[0] or 0))
    return alertas, [l for _, l in quadro], falhas


# ---------------- Secret Flying (erros de tarifa) ----------------

def secret_flying(estado, token, momento, estreia):
    try:
        raiz = ET.fromstring(baixar(SECRET_FLYING_RSS))
    except Exception as erro:
        print(f"Secret Flying falhou: {erro}")
        return 0
    vistos = set(estado.get("sf_vistos", []))
    enviados = 0
    for item in raiz.iter("item"):
        titulo = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        chave = link or titulo
        if not chave or chave in vistos:
            continue
        vistos.add(chave)
        alvo = " " + sem_acento(titulo) + " "
        if not (any(t in alvo for t in TERMOS_LONDRES) and any(t in alvo for t in TERMOS_BRASIL)):
            continue
        try:
            quando = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            quando = momento
        if estreia and momento - quando > timedelta(days=SECRET_FLYING_DIAS):
            continue
        hh, dd = hora_londres(momento)
        texto = (f"🚨 <b>ACHADO DE TARIFA</b> (Secret Flying)\n"
                 f"<b>{html.escape(titulo)}</b>\n\n"
                 f"Esse tipo de tarifa some rápido: se fizer sentido para você, confira já.\n"
                 f"🕐 Visto às {hh} de {dd} (hora de Londres)\n"
                 f"👉 <a href=\"{html.escape(link)}\">Ver o achado</a>\n\n{RODAPE}")
        if estreia:
            bastidores(estado, "🧪 Achado recente (estreia, só para você ver o formato):\n\n" + texto, token)
        else:
            vitrine(estado, texto, token)
        enviados += 1
    estado["sf_vistos"] = list(vistos)[-2000:]
    return enviados


# ---------------- principal ----------------

def main():
    token_tg = os.environ.get("TELEGRAM_TOKEN", "").strip()
    token_tp = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
    if not token_tg:
        raise SystemExit("Falta o secret TELEGRAM_TOKEN.")
    estado = carregar()
    momento = agora()
    descobrir(estado, token_tg)
    estreia = estado.get("estreia", True)

    sf = secret_flying(estado, token_tg, momento, estreia)

    ultima = estado.get("ultima_leitura")
    hora_de_medir = (not ultima) or (momento - datetime.fromisoformat(ultima) >= timedelta(minutes=INTERVALO_MIN))
    if token_tp and hora_de_medir:
        alertas, quadro, falhas = medir(estado, token_tp, momento)
        estado["ultima_leitura"] = momento.isoformat()
        local = momento.astimezone(TZ_LONDRES)
        hoje = local.strftime("%Y-%m-%d")
        if estreia or (local.hour >= HORA_QUADRO and estado.get("ultimo_quadro") != hoje):
            hh, dd = hora_londres(momento)
            com_preco = sum(1 for l in quadro if "sem preço" not in l)
            topo = "🛰️ <b>Radar Londres ligado!</b> (v" + VERSAO + ")\n" if estreia else ""
            msg = (f"{topo}📊 <b>Quadro Londres → Brasil</b> · {dd} às {hh} (hora de Londres)\n"
                   f"Ida e volta, 1 adulto, do mais barato ao mais caro · {com_preco} de {len(DESTINOS)} destinos com preço\n\n"
                   + "\n".join(quadro))
            if estreia:
                msg += (f"\n\nAlertas de queda começam depois de {MIN_LEITURAS} leituras por rota "
                        f"(cerca de {MIN_LEITURAS} horas).")
            if falhas:
                msg += "\n\n⚠️ Falhas: " + html.escape("; ".join(falhas))[:800]
            bastidores(estado, msg, token_tg)
            estado["ultimo_quadro"] = hoje
        print(f"Leitura feita: {alertas} alerta(s), {len(falhas)} falha(s).")
    elif not token_tp:
        print("Sem TRAVELPAYOUTS_TOKEN: só o Secret Flying foi lido.")
    estado["estreia"] = False
    estado["versao"] = VERSAO
    salvar(estado)
    print(f"Secret Flying: {sf} achado(s).")


if __name__ == "__main__":
    main()
