"""Revisión directa en jetsmart.com, como lo haría una persona (navegador real sin ventana).

Abre la búsqueda en booking.jetsmart.com, activa "Ver precios con tasas e impuestos"
y lee cada opción: horario, si es directo o con escala, tarifa normal y tarifa Club.
Los precios de JetSMART son POR PERSONA.
"""

import re

BASE = "https://booking.jetsmart.com/Flight/InternalSelect"
OPCION = '[data-test-id^="flight-fee-option--j|0-i|"]'


def url_busqueda(cfg, fecha, desde, hasta):
    return (f"{BASE}?o1={desde}&d1={hasta}&dd1={fecha}&ADT={cfg['adultos']}"
            f"&CHD={cfg['ninos']}&inl=0&r=false&s=true&mon=true&cur=CLP&culture=es-CL")


def _monto(patron, texto):
    m = re.search(patron + r"\s*\$([\d.]+)", texto)
    return int(m.group(1).replace(".", "")) if m else None


def _leer_opciones(page):
    textos = page.eval_on_selector_all(
        OPCION, "els => els.map(e => e.textContent.replace(/\\s+/g, ' ').trim())")
    opciones = []
    for t in textos:
        horas = re.findall(r"\b(\d{2}:\d{2})\b", t)
        tarifa = _monto("Tarifa desde", t)
        if not tarifa or len(horas) < 2:
            continue
        directo = t.startswith("Vuelo directo")
        # Las conexiones a Bogotá operadas por JetSMART Colombia (S.A.S.) hacen escala en Colombia
        escala_colombia = directo or "S.A.S" in t
        opciones.append({
            "sale": horas[0], "llega": horas[1],
            "directo": directo,
            "escala_colombia": escala_colombia,
            "pp": tarifa,
            "club_pp": _monto("Club de descuentos", t),
            "tasas_incluidas": "Tasas incluidas" in t,
            "dia_siguiente": "+1" in t,
        })
    return opciones


def buscar(cfg, tramos):
    """tramos: lista de (fecha, desde, hasta). Devuelve {fecha: [opciones]}."""
    from playwright.sync_api import sync_playwright

    resultado = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="es-CL", timezone_id="America/Santiago",
            viewport={"width": 1366, "height": 850},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
        )
        page = ctx.new_page()
        for fecha, desde, hasta in tramos:
            page.goto(url_busqueda(cfg, fecha, desde, hasta), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(OPCION, timeout=45000)
            page.wait_for_timeout(1500)
            # Activar "Ver precios con tasas e impuestos" (el interruptor visible)
            page.evaluate("""() => {
                const t = [...document.querySelectorAll('div.flex.cursor-pointer.items-center')]
                  .filter(e => e.textContent.trim() === 'Ver precios con tasas e impuestos' && e.offsetParent !== null);
                if (t[0]) t[0].click();
            }""")
            page.wait_for_timeout(2500)
            opciones = _leer_opciones(page)
            if opciones and not any(o["tasas_incluidas"] for o in opciones):
                raise RuntimeError("No se pudo activar el precio con tasas")
            resultado[fecha] = opciones
            page.wait_for_timeout(2000)
        browser.close()
    return resultado
