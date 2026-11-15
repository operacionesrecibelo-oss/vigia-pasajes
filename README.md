# Vigía de pasajes

Revisa Google Flights cada 15 minutos con GitHub Actions y avisa por Telegram cuando baja el precio.
La configuración del viaje y el token del bot van en *Secrets*; los logs no muestran precios ni rutas.

Secrets requeridos: `TELEGRAM_TOKEN`, `CONFIG_VIAJE` (JSON). Opcional: `TELEGRAM_CHAT_ID`.
