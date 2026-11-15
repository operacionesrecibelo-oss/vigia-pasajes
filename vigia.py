"""Vigía de pasajes SCL <-> BOG.

Corre en GitHub Actions cada 15 min. Busca en Google Flights y, cada 1 hora,
directo en jetsmart.com. Compara todo por persona (ida + vuelta) y avisa por
Telegram solo cuando el precio baja.
No imprime precios ni ruta en los logs (el repositorio es público).
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

import jetsmart

from fast_flights import FlightQuery, Passengers, create_query
from fli.models import Airport as FAirport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo, SeatType, SortBy
from fli.search import SearchFlights
from fli.search._decoders import parse_flight_row
from fli.search._urls import with_locale_params
from fli.search._wire import iter_wrb_chunks
from fli.search.client import get_client

TZ = ZoneInfo("America/Santiago")
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")

# Aeropuertos de Colombia válidos para hacer escala
AEROPUERTOS_COL = {
    "BOG", "MDE", "EOH", "CLO", "CTG", "BAQ", "BGA", "PEI", "SMR", "ADZ",
    "CUC", "AXM", "PSO", "MTR", "LET", "NVA", "MZL", "VUP", "IBE", "RCH",
    "EYP", "PPN", "UIB", "AUC", "APO",
}


# ---------- utilidades ----------

def log(msg):
    # Solo mensajes genéricos: los logs de un repo público son visibles.
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M}] {msg}", flush=True)


def clp(n):
    return "$" + f"{int(round(n)):,}".replace(",", ".")


def usd(n, rate):
    return f"USD {n / rate:,.0f}" if rate else ""


def http_json(url, data=None, timeout=20):
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(url, data=body, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cargar_estado():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_estado(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------- Telegram ----------

def tg_chat_id(token, st):
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return os.environ["TELEGRAM_CHAT_ID"]
    if st.get("chat_id"):
        return st["chat_id"]
    # Autodetección: toma el chat de quien le escribió /start al bot
    try:
        upd = http_json(f"https://api.telegram.org/bot{token}/getUpdates")
        for u in reversed(upd.get("result", [])):
            msg = u.get("message") or u.get("edited_message")
            if msg:
                st["chat_id"] = str(msg["chat"]["id"])
                return st["chat_id"]
        log(f"getUpdates OK, {len(upd.get('result', []))} mensajes recibidos")
    except urllib.error.HTTPError as e:
        log(f"Telegram rechazó el token (HTTP {e.code}). Revisa el secreto TELEGRAM_TOKEN.")
    except Exception as e:
        log(f"Error consultando Telegram: {type(e).__name__}")
    return None


def tg_enviar(token, chat_id, texto):
    http_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": texto, "parse_mode": "HTML",
         "disable_web_page_preview": "true"},
    )



# ---------- búsqueda ----------

def nombres_aerolineas(f):
    vistos = []
    for leg in f.legs:
        n = leg.airline.value
        if n not in vistos:
            vistos.append(n)
    return vistos


def aerolinea_ok(f, permitidas):
    nombres = " ".join(nombres_aerolineas(f)).lower().replace(" ", "")
    return any(a.lower().replace(" ", "") in nombres for a in permitidas)


def escalas_ok(f):
    # Directo, o todas las escalas dentro de Colombia
    escalas = [leg.arrival_airport.name for leg in f.legs[:-1]]
    return all(e in AEROPUERTOS_COL for e in escalas)


def describir(f):
    l0, l1 = f.legs[0], f.legs[-1]
    ruta = " → ".join([l0.departure_airport.name] + [l.arrival_airport.name for l in f.legs])
    escalas = "directo" if len(f.legs) == 1 else f"{len(f.legs) - 1} escala"
    return (f"{', '.join(nombres_aerolineas(f))} · {ruta} · {escalas} · "
            f"sale {l0.departure_datetime:%H:%M} llega {l1.arrival_datetime:%H:%M}")


def buscar_todos(filtros):
    """Llama a GetShoppingResults y lee TODOS los bloques de la respuesta."""
    url = with_locale_params(SearchFlights.BASE_URL, "CLP", "es", "CL")
    r = get_client().post(url=url, data=f"f.req={filtros.encode()}",
                          impersonate="chrome", allow_redirects=True)
    r.raise_for_status()
    vuelos, vistos, n_bloques = [], set(), 0
    for inner in iter_wrb_chunks(r.text):
        n_bloques += 1
        if not isinstance(inner, list):
            continue
        for i in (2, 3):
            if len(inner) > i and isinstance(inner[i], list) and inner[i] and inner[i][0]:
                for row in inner[i][0]:
                    try:
                        f = parse_flight_row(row)
                    except Exception:
                        continue
                    clave = (f.price, tuple((l.flight_number, l.departure_datetime) for l in f.legs))
                    if clave not in vistos:
                        vistos.add(clave)
                        vuelos.append(f)
    log(f"Respuesta: {n_bloques} bloques")
    return vuelos


def buscar_tramo(cfg, fecha, desde, hasta):
    # Enlace para abrir la misma búsqueda en Google Flights
    q = create_query(
        flights=[FlightQuery(date=fecha, from_airport=desde, to_airport=hasta)],
        trip="one-way", seat="economy",
        passengers=Passengers(adults=cfg["adultos"], children=cfg["ninos"]),
        language="es", currency="CLP",
    )
    filtros = FlightSearchFilters(
        passenger_info=PassengerInfo(adults=cfg["adultos"], children=cfg["ninos"]),
        flight_segments=[FlightSegment(
            departure_airport=[[getattr(FAirport, desde), 0]],
            arrival_airport=[[getattr(FAirport, hasta), 0]],
            travel_date=fecha)],
        stops=MaxStops.ONE_STOP_OR_FEWER,
        seat_type=SeatType.ECONOMY,
        sort_by=SortBy.CHEAPEST,
    )
    res = buscar_todos(filtros)
    validos = [f for f in res if f.price and aerolinea_ok(f, cfg["aerolineas"]) and escalas_ok(f)]
    tramo = "ida" if desde == cfg["origen"] else "vuelta"
    log(f"Tramo {tramo}: {len(res)} resultados, {len(validos)} cumplen filtros")
    validos.sort(key=lambda f: f.price)
    return (validos[0] if validos else None), q.url()


def candidato_google(f, url, pax):
    return {"pp": f.price / pax, "fuente": "Google Flights", "desc": describir(f),
            "url": url, "link_txt": "Ver en Google Flights"}


def candidatos_jetsmart(cfg, st, fecha, desde, hasta):
    """Mejor opción de la web de JetSMART para esa fecha (desde la última revisión directa)."""
    js = st.get("jetsmart") or {}
    try:
        edad = (datetime.now(TZ) - datetime.fromisoformat(js["ts"])).total_seconds()
    except Exception:
        return None
    if edad > 3 * 3600:
        return None
    ops = [o for o in js.get("tramos", {}).get(fecha, []) if o["escala_colombia"] and o["tasas_incluidas"]]
    if not ops:
        return None
    o = min(ops, key=lambda x: x["pp"])
    escala = "directo" if o["directo"] else "1 escala (Colombia)"
    desc = (f"JetSMART · {desde} → {hasta} · {escala} · sale {o['sale']} llega {o['llega']}"
            f"{' (+1)' if o['dia_siguiente'] else ''}")
    if o.get("club_pp") and o["club_pp"] < o["pp"]:
        desc += f"\n💳 Con Club de descuentos: {clp(o['club_pp'])} por persona"
    return {"pp": o["pp"], "fuente": "web JetSMART", "desc": desc,
            "url": jetsmart.url_busqueda(cfg, fecha, desde, hasta), "link_txt": "Ver en jetsmart.com"}


def mejor_tramo(cfg, st, fecha, desde, hasta, pax):
    g, url = buscar_tramo(cfg, fecha, desde, hasta)
    cands = []
    if g:
        cands.append(candidato_google(g, url, pax))
    j = candidatos_jetsmart(cfg, st, fecha, desde, hasta)
    if j:
        cands.append(j)
    return min(cands, key=lambda c: c["pp"]) if cands else None


def mejor_combinacion(cfg, st):
    pax = cfg["adultos"] + cfg["ninos"]
    ida = mejor_tramo(cfg, st, cfg["ida"], cfg["origen"], cfg["destino"], pax)
    time.sleep(3)
    opciones = []
    for fv in cfg["vueltas"]:
        vuelta = mejor_tramo(cfg, st, fv, cfg["destino"], cfg["origen"], pax)
        if ida and vuelta:
            opciones.append({"pp": ida["pp"] + vuelta["pp"], "fecha_vuelta": fv,
                             "ida": ida, "vuelta": vuelta})
        time.sleep(3)
    return min(opciones, key=lambda o: o["pp"]) if opciones else None, opciones


def revisar_jetsmart(cfg, st, token, chat):
    tramos = [(cfg["ida"], cfg["origen"], cfg["destino"])] + \
             [(fv, cfg["destino"], cfg["origen"]) for fv in cfg["vueltas"]]
    try:
        res = jetsmart.buscar(cfg, tramos)
        st["jetsmart"] = {"ts": datetime.now(TZ).isoformat(), "tramos": res}
        log("JetSMART directo: " + ", ".join(f"{len(v)} opciones" for v in res.values()))
        if st.get("js_err", 0) >= 3:
            tg_enviar(token, chat, "✅ La revisión directa en jetsmart.com volvió a funcionar.")
        st["js_err"] = 0
    except Exception as e:
        st["js_err"] = st.get("js_err", 0) + 1
        log(f"JetSMART directo falló ({type(e).__name__}); consecutivos: {st['js_err']}")
        if st["js_err"] == 3:
            tg_enviar(token, chat, "⚠️ jetsmart.com está bloqueando la revisión directa. "
                      "Sigo revisando con Google Flights y vuelvo a intentar cada hora.")


def tipo_cambio():
    try:
        return http_json("https://open.er-api.com/v6/latest/USD")["rates"]["CLP"]
    except Exception:
        return None


# ---------- mensajes ----------

def fecha_txt(iso):
    d = date.fromisoformat(iso)
    meses = "ene feb mar abr may jun jul ago sep oct nov dic".split()
    return f"{d.day:02d} {meses[d.month - 1]} {d.year}"


def tramo_txt(icono, titulo, c):
    return (f"{icono} {titulo}: <b>{clp(c['pp'])}</b> por persona · {c['fuente']}\n{c['desc']}\n"
            f"<a href=\"{html.escape(c['url'])}\">{c['link_txt']}</a>")


def bloque(o, cfg, rate):
    pax = cfg["adultos"] + cfg["ninos"]
    total = o["pp"] * pax
    return (
        f"<b>Ida + vuelta: {clp(o['pp'])} por persona</b> ({usd(o['pp'], rate)})\n"
        f"Total {pax} pasajeros: {clp(total)} ({usd(total, rate)})\n\n"
        + tramo_txt("🛫", f"Ida {fecha_txt(cfg['ida'])}", o["ida"]) + "\n\n"
        + tramo_txt("🛬", f"Vuelta {fecha_txt(o['fecha_vuelta'])}", o["vuelta"])
    )


# ---------- principal ----------

def main():
    cfg = json.loads(os.environ["CONFIG_VIAJE"])
    token = os.environ["TELEGRAM_TOKEN"].strip()
    st = cargar_estado()
    ahora = datetime.now(TZ)
    hoy = ahora.date().isoformat()

    chat = tg_chat_id(token, st)
    if not chat:
        log("Falta chat de Telegram: escribe /start a tu bot.")
        guardar_estado(st)
        return 0

    if not st.get("bienvenida"):
        tg_enviar(token, chat, "✅ Vigía de pasajes activo. Revisaré cada 15 minutos "
                  "y te avisaré solo cuando el precio baje.")
        st["bienvenida"] = True

    # Fin del monitoreo
    if hoy > cfg["fin"]:
        if not st.get("finalizado"):
            tg_enviar(token, chat, "🏁 Terminó el periodo de monitoreo. Me detengo.")
            st["finalizado"] = True
        guardar_estado(st)
        print("FINALIZADO")
        return 0

    if os.environ.get("JETSMART_DUE") == "1":
        revisar_jetsmart(cfg, st, token, chat)

    try:
        mejor, todas = mejor_combinacion(cfg, st)
    except Exception as e:
        st["errores"] = st.get("errores", 0) + 1
        log(f"Error de búsqueda ({type(e).__name__}: {str(e)[:150]}); consecutivos: {st['errores']}")
        if st["errores"] == 12:  # ~3 horas fallando
            tg_enviar(token, chat, "⚠️ Llevo 3 horas sin poder consultar Google Flights. "
                      "Sigo intentando; si persiste, hay que revisar el script.")
        guardar_estado(st)
        return 0
    st["errores"] = 0

    if not mejor:
        log(f"Sin opciones que cumplan los filtros ({len(todas)} combinaciones).")
        guardar_estado(st)
        return 0

    rate = tipo_cambio()
    pp = mejor["pp"]
    umbral = cfg["umbral_por_persona_clp"]
    maximo = f"Máximo: {clp(umbral)} por persona (ida + vuelta)"
    previo = st.get("ultimo_pp")

    # Aviso solo si baja respecto de la última revisión
    if previo is not None and pp < previo:
        baja = previo - pp
        titulo = ("🔥 <b>¡BAJO TU MÁXIMO!</b>" if pp <= umbral else "📉 <b>Bajó el precio</b>")
        tg_enviar(token, chat, f"{titulo} (−{clp(baja)} por persona)\n{maximo}\n\n{bloque(mejor, cfg, rate)}")
    elif previo is None and pp <= umbral:
        tg_enviar(token, chat, f"🔥 <b>¡Ya está bajo tu máximo!</b>\n{maximo}\n\n{bloque(mejor, cfg, rate)}")

    st["ultimo_pp"] = pp
    st["minimo_pp"] = min(st.get("minimo_pp", pp), pp)

    # Resumen diario
    forzar = not st.get("formato_v2")  # enviar un resumen con el formato nuevo al actualizar
    if forzar or (ahora.hour >= cfg.get("resumen_hora", 9) and st.get("ultimo_resumen") != hoy):
        st["formato_v2"] = True
        otras = "\n".join(
            f"• Vuelta {fecha_txt(o['fecha_vuelta'])}: {clp(o['pp'])} por persona"
            for o in sorted(todas, key=lambda o: o["pp"]))
        falta = pp - umbral
        estado = "✅ Bajo tu máximo" if falta <= 0 else f"Faltan {clp(falta)} por persona para tu máximo"
        tg_enviar(token, chat,
                  f"☀️ <b>Resumen diario</b>\n{maximo}\n{estado}\n"
                  f"Mínimo visto: {clp(st['minimo_pp'])} por persona\n\n"
                  f"{bloque(mejor, cfg, rate)}\n\n<b>Comparación por fecha de vuelta</b>\n{otras}")
        st["ultimo_resumen"] = hoy

    guardar_estado(st)
    log("Revisión completa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
