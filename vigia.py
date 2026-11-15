"""Vigía de pasajes SCL <-> BOG.

Corre en GitHub Actions cada 15 min. Busca en Google Flights (fast-flights),
filtra por aerolíneas y escalas, y avisa por Telegram solo cuando el precio baja.
No imprime precios ni ruta en los logs (el repositorio es público).
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fast_flights import FlightQuery, Passengers, create_query, get_flights

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
    except Exception:
        pass
    return None


def tg_enviar(token, chat_id, texto):
    http_json(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat_id, "text": texto, "parse_mode": "HTML",
         "disable_web_page_preview": "true"},
    )


# ---------- búsqueda ----------

def aerolinea_ok(f, permitidas):
    nombres = " ".join(f.airlines).lower().replace(" ", "")
    return any(a.lower().replace(" ", "") in nombres for a in permitidas)


def escalas_ok(f):
    # Directo, o todas las escalas dentro de Colombia
    escalas = [s.to_airport.code for s in f.flights[:-1]]
    return all(e in AEROPUERTOS_COL for e in escalas)


def describir(f):
    s0, s1 = f.flights[0], f.flights[-1]
    hh = lambda t: f"{t[0]:02d}:{t[1]:02d}"
    ruta = " → ".join([s0.from_airport.code] + [s.to_airport.code for s in f.flights])
    escalas = "directo" if len(f.flights) == 1 else f"{len(f.flights) - 1} escala"
    return (f"{', '.join(f.airlines)} · {ruta} · {escalas} · "
            f"sale {hh(s0.departure.time)} llega {hh(s1.arrival.time)}")


def buscar_tramo(cfg, fecha, desde, hasta):
    q = create_query(
        flights=[FlightQuery(date=fecha, from_airport=desde, to_airport=hasta,
                             max_stops=1)],
        trip="one-way",
        seat="economy",
        passengers=Passengers(adults=cfg["adultos"], children=cfg["ninos"]),
        language="es",
        currency="CLP",
    )
    res = get_flights(q)
    log(f"Tramo {'ida' if desde == cfg['origen'] else 'vuelta'}: {len(res)} resultados")
    validos = [f for f in res if f.price and aerolinea_ok(f, cfg["aerolineas"])
               and escalas_ok(f)]
    validos.sort(key=lambda f: f.price)
    return (validos[0] if validos else None), q.url()


def mejor_combinacion(cfg):
    ida, url_ida = buscar_tramo(cfg, cfg["ida"], cfg["origen"], cfg["destino"])
    time.sleep(3)
    opciones = []
    for fv in cfg["vueltas"]:
        vuelta, url_v = buscar_tramo(cfg, fv, cfg["destino"], cfg["origen"])
        if ida and vuelta:
            opciones.append({
                "total": ida.price + vuelta.price,
                "fecha_vuelta": fv,
                "ida": describir(ida), "ida_precio": ida.price, "url_ida": url_ida,
                "vuelta": describir(vuelta), "vuelta_precio": vuelta.price,
                "url_vuelta": url_v,
            })
        time.sleep(3)
    return min(opciones, key=lambda o: o["total"]) if opciones else None, opciones


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


def bloque(o, cfg, rate):
    pax = cfg["adultos"] + cfg["ninos"]
    pp = o["total"] / pax
    return (
        f"<b>Total {pax} pasajeros: {clp(o['total'])}</b> ({usd(o['total'], rate)})\n"
        f"Por persona: {clp(pp)} ({usd(pp, rate)})\n\n"
        f"🛫 Ida {fecha_txt(cfg['ida'])}: {clp(o['ida_precio'])}\n{o['ida']}\n"
        f"<a href=\"{o['url_ida']}\">Ver ida en Google Flights</a>\n\n"
        f"🛬 Vuelta {fecha_txt(o['fecha_vuelta'])}: {clp(o['vuelta_precio'])}\n{o['vuelta']}\n"
        f"<a href=\"{o['url_vuelta']}\">Ver vuelta en Google Flights</a>"
    )


# ---------- principal ----------

def main():
    cfg = json.loads(os.environ["CONFIG_VIAJE"])
    token = os.environ["TELEGRAM_TOKEN"]
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

    try:
        mejor, todas = mejor_combinacion(cfg)
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
    pax = cfg["adultos"] + cfg["ninos"]
    pp = mejor["total"] / pax
    umbral = cfg["umbral_por_persona_clp"]
    previo = st.get("ultimo_total")

    # Aviso solo si baja respecto de la última revisión
    if previo is not None and mejor["total"] < previo:
        baja = previo - mejor["total"]
        titulo = ("🔥 <b>¡BAJO TU META!</b>" if pp < umbral else "📉 <b>Bajó el precio</b>")
        tg_enviar(token, chat,
                  f"{titulo} (−{clp(baja)})\n"
                  f"Meta: {clp(umbral)} por persona\n\n{bloque(mejor, cfg, rate)}")
    elif previo is None and pp < umbral:
        tg_enviar(token, chat, f"🔥 <b>¡Ya está bajo tu meta!</b>\n\n{bloque(mejor, cfg, rate)}")

    st["ultimo_total"] = mejor["total"]
    st["minimo"] = min(st.get("minimo", mejor["total"]), mejor["total"])

    # Resumen diario
    if ahora.hour >= cfg.get("resumen_hora", 9) and st.get("ultimo_resumen") != hoy:
        otras = "\n".join(
            f"• Vuelta {fecha_txt(o['fecha_vuelta'])}: {clp(o['total'])} total"
            for o in sorted(todas, key=lambda o: o["total"]))
        tg_enviar(token, chat,
                  f"☀️ <b>Resumen diario</b> · Meta {clp(umbral)} por persona\n"
                  f"Mínimo visto: {clp(st['minimo'])} total\n\n"
                  f"{bloque(mejor, cfg, rate)}\n\n<b>Comparación por fecha de vuelta</b>\n{otras}")
        st["ultimo_resumen"] = hoy

    guardar_estado(st)
    log("Revisión completa.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
