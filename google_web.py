"""Google Flights abierto en un navegador real (sin ventana).

Carga la página de búsqueda y captura las respuestas internas GetShoppingResults
que la propia página pide, para leer la lista completa de vuelos.
"""

from fli.search._decoders import parse_flight_row
from fli.search._wire import iter_wrb_chunks


def _parse(cuerpos):
    vuelos, vistos = [], set()
    for texto in cuerpos:
        for inner in iter_wrb_chunks(texto):
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
    return vuelos


def buscar(urls):
    """urls: {clave: url de Google Flights}. Devuelve {clave: [FlightResult]}."""
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
        for clave, url in urls.items():
            cuerpos = []

            def capturar(resp):
                if "GetShoppingResults" in resp.url:
                    try:
                        cuerpos.append(resp.text())
                    except Exception:
                        pass

            page.on("response", capturar)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):  # hasta ~20 s esperando la lista completa
                page.wait_for_timeout(1000)
                if cuerpos and len(_parse(cuerpos)) >= 5:
                    page.wait_for_timeout(2000)
                    break
            page.remove_listener("response", capturar)
            resultado[clave] = _parse(cuerpos)
            page.wait_for_timeout(1500)
        browser.close()
    return resultado
