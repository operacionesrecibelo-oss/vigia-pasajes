"""Avisa por Telegram si una revisión falló (como máximo 1 aviso cada 6 horas)."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Santiago")
STATE_FILE = os.environ.get("STATE_FILE", "state/state.json")

try:
    with open(STATE_FILE, encoding="utf-8") as f:
        st = json.load(f)
except Exception:
    st = {}

chat = os.environ.get("TELEGRAM_CHAT_ID") or st.get("chat_id")
token = os.environ.get("TELEGRAM_TOKEN", "").strip()
ahora = datetime.now(TZ)
ultimo = st.get("aviso_fallo_ts")
if chat and token and (not ultimo or (ahora - datetime.fromisoformat(ultimo)).total_seconds() > 6 * 3600):
    texto = ("⚠️ El vigía de pasajes tuvo un error en la revisión de las "
             f"{ahora:%H:%M}. Sigue intentando cada 15 minutos; si se repite, pide revisarlo.")
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    st["aviso_fallo_ts"] = ahora.isoformat()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
