import base64
import io
import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import requests
from PIL import Image
from pypdf import PdfReader

from google import genai

from prompts import SYSTEM_INSTRUCTION

# Cargar las variables de entorno desde el archivo .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
# Zona horaria IANA usada para filtrar y mostrar los partidos de API-Football.
# Sin ella el filtro 'date' se interpreta en UTC y a la noche local se pide el
# día equivocado. Se puede pisar desde el .env (ej. FOOTBALL_TIMEZONE=Europe/Madrid).
FOOTBALL_TIMEZONE = os.getenv("FOOTBALL_TIMEZONE", "America/Argentina/Buenos_Aires")

# Configuración del cliente de Gemini (SDK oficial google.genai)
client = genai.Client(api_key=GEMINI_API_KEY)

# El modelo se lee del .env para poder cambiarlo sin tocar el código.
# Si la variable no existe, se usa el valor por defecto.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Configuración de generación
generation_config = {
    "temperature": 0.7,
}

# --- CONFIGURACIÓN DE SQLITE (HISTORIAL PERSISTENTE) ---
# Ruta absoluta: así la DB queda siempre en la misma carpeta del script,
# aunque el bot se ejecute desde otro directorio.
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bottin_history.db")

# Cantidad máxima de mensajes del historial que se envían a Gemini por consulta
MAX_HISTORY = 15

def init_db():
    """Crea las tablas de usuarios e historial si no existen."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            created_at TEXT,
            last_seen TEXT
        )
    ''')
    conn.commit()
    conn.close()

def register_user(chat_id: int, user_id: int, username: str, first_name: str) -> bool:
    """Crea o actualiza el registro del usuario. Devuelve True si es la primera vez."""
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        ahora = datetime.now().isoformat()
        cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
        es_nuevo = cursor.fetchone() is None
        cursor.execute('''
            INSERT INTO users (chat_id, user_id, username, first_name, created_at, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_id = excluded.user_id,
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
        ''', (chat_id, user_id, username, first_name, ahora, ahora))
        conn.commit()
    finally:
        conn.close()

    if es_nuevo:
        logger.info(f"👤 Nuevo usuario registrado: {first_name} "
                    f"(user_id: {user_id}, username: @{username}, chat: {chat_id})")
    return es_nuevo

def save_interaction(chat_id: int, user_text: str, bot_reply: str):
    """Guarda la consulta y la respuesta en UNA sola transacción, para que el
    historial nunca quede incompleto (solo el mensaje del usuario) si algo
    falla a mitad de camino."""
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        ahora = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, "user", user_text, ahora)
        )
        cursor.execute(
            "INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, "model", bot_reply, ahora)
        )
        conn.commit()
    finally:
        conn.close()

def delete_user(chat_id: int) -> int:
    """Elimina al usuario y todo su historial de la base de datos SQLite.
    Devuelve la cantidad de mensajes eliminados."""
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
        eliminados = cursor.fetchone()[0]
        cursor.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        cursor.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()
    return eliminados

def init_stats_db():
    """Crea una tabla para estadísticas históricas si no existe."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            team TEXT,
            opponent TEXT,
            result TEXT, -- 'G' (Ganado), 'E' (Empatado), 'P' (Perdido)
            goals_for INTEGER,
            goals_against INTEGER,
            date TEXT
        )
    ''')
    conn.commit()
    conn.close()

def analizar_racha_equipo(nombre_equipo: str) -> str:
    """Consulta SQLite, calcula estadísticas recientes y genera un informe de racha."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT result, goals_for, goals_against, opponent, date 
        FROM team_matches 
        WHERE team LIKE ? 
        ORDER BY id DESC LIMIT 5
    ''', (f"%{nombre_equipo}%",))
    
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return f"No encontré registros históricos recientes en la base de datos local para el equipo '{nombre_equipo}'."
    
    total_partidos = len(rows)
    ganados = sum(1 for r in rows if r[0] == 'G')
    empatados = sum(1 for r in rows if r[0] == 'E')
    perdidos = sum(1 for r in rows if r[0] == 'P')
    goles_favor = sum(r[1] for r in rows)
    goles_contra = sum(r[2] for r in rows)
    
    porcentaje_victorias = (ganados / total_partidos) * 100
    
    informe = (
        f"📊 **Informe Estadístico y de Racha ({nombre_equipo.upper()})**:\n"
        f"- Partidos analizados: {total_partidos}\n"
        f"- Victorias: {ganados} | Empates: {empatados} | Derrotas: {perdidos}\n"
        f"- Efectividad de victorias: {porcentaje_victorias:.1f}%\n"
        f"- Goles a favor: {goles_favor} | Goles en contra: {goles_contra}\n"
    )
    return informe

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ESTRATEGIA HÍBRIDA DE DATOS (API-FOOTBALL + CHAMPIONS + PROMIEDOS) ---

# --- Caché en memoria de API-Football ---
# El plan gratis admite 100 requests/día: se cachea el resultado del día
# durante _CACHE_TTL_SEGUNDOS para no gastar cuota con cada consulta.
_CACHE_PARTIDOS = {"key": None, "ts": 0.0, "value": ""}
_CACHE_TTL_SEGUNDOS = 120

def obtener_partidos_api() -> str:
    """Consulta los partidos del día en API-Football para Champions League y
    grandes ligas europeas, con logs de diagnóstico.

    La fecha se envía junto con `timezone` para que el filtro 'date' se
    interprete en la zona horaria del usuario (sin ella, la API filtra en UTC
    y entre las 21:00 y medianoche local se pediría el día equivocado).
    """
    if not FOOTBALL_API_KEY:
        logger.warning("FOOTBALL_API_KEY no está configurada o está vacía en el .env")
        return ""

    hoy = datetime.now().strftime("%Y-%m-%d")

    # Caché: misma fecha + misma zona horaria → respuesta ya conocida.
    cache_key = f"{hoy}|{FOOTBALL_TIMEZONE}"
    if _CACHE_PARTIDOS["key"] == cache_key:
        edad = datetime.now().timestamp() - _CACHE_PARTIDOS["ts"]
        if edad < _CACHE_TTL_SEGUNDOS:
            logger.info(f"♻️ API-Football: usando caché ({int(edad)}s de antigüedad).")
            return _CACHE_PARTIDOS["value"]

    url = "https://v3.football.api-sports.io/fixtures"
    querystring = {"date": hoy, "timezone": FOOTBALL_TIMEZONE}
    headers = {'x-apisports-key': FOOTBALL_API_KEY}

    logger.info(f"🔍 Consultando API-Football para la fecha: {hoy} ({FOOTBALL_TIMEZONE})...")

    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        logger.info(f"📡 Código de estado HTTP de la API: {response.status_code}")

        # Cuota restante del día (header oficial de rate limit)
        restante = response.headers.get("x-ratelimit-requests-remaining")
        if restante is not None:
            logger.info(f"🪙 Requests restantes hoy en API-Football: {restante}")

        if response.status_code != 200:
            logger.error(f"❌ API-Football respondió HTTP {response.status_code} (no se procesa la respuesta).")
            return ""

        data = response.json()

        # Errores que devuelva la API misma (clave inválida, límite superado...)
        if data.get("errors"):
            logger.warning(f"⚠️ Errores devueltos por la API: {data['errors']}")
            return ""

        matches = data.get("response", [])
        logger.info(f"⚽ Total de partidos devueltos por la API para hoy: {len(matches)}")

        if not matches:
            # Respuesta válida pero sin partidos: se cachea igual para no gastar cuota
            _CACHE_PARTIDOS.update(key=cache_key, ts=datetime.now().timestamp(), value="")
            return ""

        # IDs permitidos: Champions League (2), Premier League (39), La Liga (140),
        # Serie A (135), Bundesliga (78), Ligue 1 (61)
        ligas_permitidas = [2, 39, 140, 135, 78, 61]

        # Estados según la doc oficial de API-Football
        estados_finalizados = ["FT", "AET", "PEN"]
        estados_en_juego = ["1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP"]
        estados_parados = {"PST": "Postergado", "CANC": "Cancelado",
                           "ABD": "Abandonado", "AWD": "Resultado oficial",
                           "WO": "Resultado oficial (WO)"}

        # Etiqueta de hora local derivada de la zona horaria (ej. "Buenos Aires")
        zona_label = FOOTBALL_TIMEZONE.split("/")[-1].replace("_", " ")

        resumen = []

        for m in matches:
            if m["league"]["id"] in ligas_permitidas:
                liga = m["league"]["name"]
                local = m["teams"]["home"]["name"]
                visita = m["teams"]["away"]["name"]
                goles_home = m["goals"]["home"]
                goles_away = m["goals"]["away"]
                estado = m["fixture"]["status"]["short"]

                if estado in estados_finalizados:
                    marcador = f"{goles_home} - {goles_away} (Finalizado)"
                elif estado in estados_en_juego:
                    # En algunos estados los goles vienen en None
                    marcador = f"{goles_home or 0} - {goles_away or 0} ¡En juego!"
                elif estado in estados_parados:
                    marcador = estados_parados[estado]
                else:
                    # NS / TBD: la fecha viene ya convertida por el parámetro timezone
                    hora_local = m["fixture"]["date"].split("T")[1][:5]
                    marcador = f"A las {hora_local} ({zona_label})"

                resumen.append(f"- [{liga}] {local} vs {visita} -> {marcador}")

        if not resumen:
            logger.info("ℹ️ Hubo partidos hoy, pero ninguno coincidió con las ligas configuradas.")
            _CACHE_PARTIDOS.update(key=cache_key, ts=datetime.now().timestamp(), value="")
            return ""

        logger.info(f"✅ Se filtraron {len(resumen)} partidos de las ligas de interés.")
        resultado = "\n".join(resumen[:15])

        _CACHE_PARTIDOS.update(key=cache_key, ts=datetime.now().timestamp(), value=resultado)
        return resultado

    except Exception as e:
        logger.error(f"❌ Excepción crítica al conectar con API-Football: {e}")
        return ""

def obtener_partidos_promiedos() -> str:
    """Extrae los partidos y resultados del día de Promiedos leyendo el JSON
    embebido (__NEXT_DATA__) de su nueva web (ya no publica tablas HTML)."""
    url = "https://www.promiedos.com.ar/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"❌ Promiedos respondió HTTP {response.status_code}")
            return ""
            
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text, re.S
        )
        if not match:
            print("❌ No se encontró __NEXT_DATA__ en Promiedos (volvió a cambiar la web).")
            return ""
            
        data = json.loads(match.group(1))
        ligas = data["props"]["pageProps"]["data"]["leagues"]
        resumen = []
        
        for liga in ligas:
            nombre_liga = liga.get("name", "")
            for g in liga.get("games", []):
                equipos = g.get("teams", [])
                if len(equipos) != 2:
                    continue
                local = equipos[0].get("name", "?")
                visita = equipos[1].get("name", "?")
                goles = g.get("scores") or [None, None]
                estado = g.get("status") or {}
                nombre_estado = (estado.get("name") or "").lower()
                ronda = g.get("stage_round_name") or ""
                
                if estado.get("enum") == 3 or "final" in nombre_estado:
                    marcador = f"{goles[0]} - {goles[1]} (Finalizado)"
                elif estado.get("enum") == 2 or "juego" in nombre_estado or "curso" in nombre_estado:
                    marcador = f"{goles[0]} - {goles[1]} ¡En juego!"
                else:
                    hora = (g.get("start_time") or "")[-5:]
                    marcador = f"A las {hora} (hora Argentina)" if hora else "A confirmar"
                    
                etiqueta = f"{nombre_liga} - {ronda}" if ronda else nombre_liga
                resumen.append(f"- [{etiqueta}] {local} vs {visita} -> {marcador}")
                
        print(f"🇦🇷 Promiedos: {len(resumen)} partidos del día leídos del JSON de la web.")
        return "\n".join(resumen[:15]) if resumen else ""
    except Exception as e:
        print(f"❌ Error leyendo Promiedos: {e}")
        return ""

# --- FUENTE ESPN (sin clave): jornadas anteriores y respaldo de hoy ---
# API-Football en el plan gratis solo cubre temporadas recientes y tiene
# 100 requests/día, así que para "últimos partidos" / "jornada pasada" se usa
# el scoreboard público de ESPN (sin clave ni cuota).

# (slug de ESPN, nombre para mostrar, palabras clave que lo activan)
LIGAS_ESPN = [
    ("arg.1", "Liga Profesional Argentina",
     ["argentina", "liga argentina", "liga profesional", "copa argentina", "river", "boca", "racing",
      "independiente", "san lorenzo", "godoy cruz", "talleres", "ascenso", "b nacional"]),
    ("eng.1", "Premier League",
     ["premier", "inglaterra", "manchester", "liverpool", "chelsea", "arsenal", "tottenham",
      "newcastle", "brighton", "aston villa", "sunderland"]),
    ("esp.1", "LaLiga",
     ["laliga", "españa", "espana", "real madrid", "barcelona", "atletico"]),
    ("ita.1", "Serie A",
     ["serie a", "italia", "juventus", "milan", "napoli", "fiorentina", "lazio"]),
    ("ger.1", "Bundesliga",
     ["bundesliga", "alemania", "bayern", "dortmund", "leverkusen"]),
    ("fra.1", "Ligue 1",
     ["ligue one", "ligue 1", "francia", "psg", "marsella"]),
    ("uefa.champions", "Champions League", ["champions"]),
]

# Palabras que indican que el usuario pregunta por partidos ya jugados
PALABRAS_PASADO = [
    "ultim", "últim", "pasad", "recient", "ayer", "jornada", "resultad",
    "ganaron", "gano", "ganó", "fixture", "calendario", "historial",
]

def detectar_ligas(texto: str) -> list[tuple[str, str]]:
    """Detecta qué ligas menciona el usuario. Devuelve tuplas (slug ESPN, nombre)."""
    texto = texto.lower()
    return [
        (slug, nombre) for slug, nombre, claves in LIGAS_ESPN
        if any(clave in texto for clave in claves)
    ]

def _pedir_espn(slug: str, fecha: datetime) -> list[dict]:
    """Pide a ESPN los partidos de una liga en una fecha dada."""
    url = f"http://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
    try:
        response = requests.get(
            url,
            params={"dates": fecha.strftime("%Y%m%d")},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if response.status_code != 200:
            return []
        return response.json().get("events", [])
    except Exception as e:
        print(f"❌ Error consultando ESPN ({slug}): {e}")
        return []

def _formatear_espn(events: list[dict], nombre_liga: str) -> list[str]:
    """Convierte los eventos de ESPN en líneas con el mismo formato del resto."""
    lineas = []
    for e in events:
        competencia = (e.get("competitions") or [{}])[0]
        competidores = competencia.get("competitors") or []
        local = next((c for c in competidores if c.get("homeAway") == "home"), None)
        visita = next((c for c in competidores if c.get("homeAway") == "away"), None)
        if not local or not visita:
            continue
            
        estado = (e.get("status") or {}).get("type", {}).get("state", "")
        if estado == "post":
            marcador = f"{local.get('score', '-')} - {visita.get('score', '-')} (Finalizado)"
        elif estado == "in":
            marcador = f"{local.get('score', '-')} - {visita.get('score', '-')} ¡En juego!"
        else:
            hora_utc = (e.get("date") or "")[11:16]
            marcador = f"A las {hora_utc} UTC"
            
        lineas.append(
            f"- [{nombre_liga}] {local['team']['displayName']} vs {visita['team']['displayName']} -> {marcador}"
        )
    return lineas

def _nombre_liga_espn(slug: str) -> str:
    return next((nombre for s, nombre, _ in LIGAS_ESPN if s == slug), slug)

def obtener_partidos_espn_hoy(slugs: list[str]) -> str:
    """Partidos de HOY en ESPN para las ligas indicadas (respaldo sin clave)."""
    hoy = datetime.now()
    with ThreadPoolExecutor(max_workers=max(len(slugs), 1)) as executor:
        resultados = list(executor.map(lambda s: _pedir_espn(s, hoy), slugs))
        
    lineas = []
    for slug, events in zip(slugs, resultados):
        if events:
            lineas += _formatear_espn(events, _nombre_liga_espn(slug))
    return "\n".join(lineas[:15]) if lineas else ""

def obtener_ultimos_partidos_espn(slugs: list[str], max_dias: int = 7, max_dias_jornada: int = 3) -> tuple[str, str]:
    """Busca la jornada más reciente con partidos y la acumula mientras los días
    consecutivos también tengan partidos (una ronda puede abarcar varios días,
    p. ej. sábado y domingo). Devuelve (resumen, rango_de_fechas)."""
    lineas_acumuladas = []
    fechas = []
    
    for offset in range(1, max_dias + 1):
        fecha = datetime.now() - timedelta(days=offset)
        with ThreadPoolExecutor(max_workers=max(len(slugs), 1)) as executor:
            resultados = list(executor.map(lambda s: _pedir_espn(s, fecha), slugs))
            
        lineas = []
        for slug, events in zip(slugs, resultados):
            if events:
                lineas += _formatear_espn(events, _nombre_liga_espn(slug))
                
        if not lineas:
            if fechas:
                break  # la jornada terminó: apareció un día vacío después de empezar
            continue  # todavía no encontramos la jornada más reciente
            
        lineas_acumuladas += lineas
        fechas.append(fecha)
        if len(fechas) >= max_dias_jornada or len(lineas_acumuladas) >= 15:
            break
            
    if not fechas:
        return "", ""
        
    fechas.sort()
    if len(fechas) == 1:
        rango = fechas[0].strftime("%d/%m/%Y")
    else:
        rango = f"{fechas[0].strftime('%d/%m/%Y')} al {fechas[-1].strftime('%d/%m/%Y')}"
    return "\n".join(lineas_acumuladas[:20]), rango

def obtener_partidos_inteligente(user_text: str) -> str:
    """Enrutador inteligente con inyección explícita de la fecha actual.

    - Pide hoy o no aclara momento → API-Football (europeas) / Promiedos (Argentina),
      con ESPN como respaldo si ninguna tiene partidos.
    - Pide jornadas anteriores (últimos, pasada, resultados...) → ESPN (histórico libre).
    """
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    texto = user_text.lower()
    
    keywords_argentina = ["argentina", "liga profesional", "copa argentina", "river", "boca", "racing", "independiente", "san lorenzo", "godoy cruz", "talleres", "ascenso", "b nacional"]
    es_futbol_argentino = any(k in texto for k in keywords_argentina)
    
    ligas = detectar_ligas(texto)
    slugs = [slug for slug, _ in ligas] or [slug for slug, _, _ in LIGAS_ESPN]
    liga_especificada = bool(ligas)
    
    # Si dice "hoy", manda hoy; si no, decide por las palabras de pasado
    quiere_pasado = (
        "hoy" not in texto
        and any(palabra in texto for palabra in PALABRAS_PASADO)
    )
    
    datos_hoy = ""
    fuente_hoy = ""
    
    if not quiere_pasado:
        if es_futbol_argentino:
            datos_hoy = obtener_partidos_promiedos()
            fuente_hoy = "Promiedos"
        elif FOOTBALL_API_KEY:
            datos_hoy = obtener_partidos_api()
            fuente_hoy = "API-Football (Champions y Ligas Top)"
            if not datos_hoy and not liga_especificada:
                datos_hoy = obtener_partidos_promiedos()
                fuente_hoy = "Promiedos (Respaldo)"
        if not datos_hoy:
            datos_hoy = obtener_partidos_espn_hoy(slugs)
            fuente_hoy = "ESPN"
            
    # Jornadas anteriores: siempre que se pida el pasado o que hoy no haya nada
    datos_recientes = ""
    fecha_jornada = ""
    if quiere_pasado or not datos_hoy:
        datos_recientes, fecha_jornada = obtener_ultimos_partidos_espn(slugs)
        
    bloques = []
    if datos_hoy:
        bloques.append(f"- Fuente de datos: {fuente_hoy}\n- Partidos de hoy:\n{datos_hoy}")
    if datos_recientes:
        if quiere_pasado:
            bloques.append(
                f"- Fuente de datos: ESPN\n"
                f"- Últimos partidos encontrados (jornada: {fecha_jornada}):\n{datos_recientes}"
            )
        else:
            bloques.append(
                f"- Aviso: hoy no hubo partidos en las fuentes para las ligas pedidas.\n"
                f"- Últimos partidos conocidos (jornada: {fecha_jornada}), fuente ESPN:\n{datos_recientes}"
            )
            
    if not bloques:
        return (
            f"[Aviso temporal: Hoy es {hoy_str} y no se encontraron partidos registrados "
            f"en las fuentes para esta fecha ni en los últimos días]."
        )
        
    return (
        f"[INFORMACIÓN OBLIGATORIA EN TIEMPO REAL]\n"
        f"- Fecha actual exacta del sistema: {hoy_str}\n"
        + "\n".join(bloques)
    )

# --- RAG CON PDFS ---

def buscar_en_pdfs(query: str) -> str:
    """Busca fragmentos relevantes dentro de los PDFs en la carpeta 'docs/'."""
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    if not os.path.exists(docs_dir):
        return ""
    
    contexto_encontrado = ""
    palabras_clave = [p.lower() for p in query.split() if len(p) > 3]
    
    for filename in os.listdir(docs_dir):
        if filename.endswith(".pdf"):
            path = os.path.join(docs_dir, filename)
            try:
                reader = PdfReader(path)
                for i, page in enumerate(reader.pages):
                    texto = page.extract_text()
                    if texto:
                        if any(palabra in texto.lower() for palabra in palabras_clave):
                            contexto_encontrado += f"\n[Fuente: {filename} - Pág {i+1}]:\n{texto[:1000]}...\n"
            except Exception as e:
                logger.error(f"Error leyendo PDF {filename}: {e}")
                
    return contexto_encontrado

# --- HANDLERS DE TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start con un saludo personalizado y registra al usuario."""
    usuario = update.effective_user
    register_user(
        update.effective_chat.id, usuario.id, usuario.username, usuario.first_name
    )
    await update.message.reply_text(
        f"¡Hola, {usuario.first_name}! ⚽ Soy BotTin v5_2, tu asistente con Champions League, "
        f"Promiedos, estadísticas y RAG. Escribí /help para más info."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /help mostrando la lista de comandos disponibles."""
    await update.message.reply_text(
        "🤖 **BotTin v5_2 (Gemini + SQLite)**\n"
        "Podés preguntarme por partidos de hoy (Champions, ligas europeas o fútbol argentino), "
        "estadísticas, reglamentos o enviarme imágenes. "
        "Guardo el historial de nuestra charla de forma local.\n\n"
        "**Comandos disponibles:**\n"
        "/start - Inicia la conversación con el bot\n"
        "/help - Muestra este mensaje con los comandos\n"
        "/olvidar - Elimina tu historial de la base de datos"
    )

async def olvidar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Elimina al usuario de la base de datos SQLite (/olvidar)."""
    chat_id = update.effective_chat.id
    usuario = update.effective_user
    eliminados = delete_user(chat_id)
    logger.info(f"🗑️ Usuario {usuario.first_name} (id: {usuario.id}, chat: {chat_id}) ejecutó /olvidar. "
                f"Se eliminaron {eliminados} mensajes de su historial.")
    await update.message.reply_text(
        f"🧹 Listo, {usuario.first_name}! Eliminé tu historial de la base de datos "
        f"({eliminados} mensajes borrados). Empezamos de cero, contame qué fútbol te interesa. ⚽"
    )

def get_chat_history(chat_id: int) -> list[tuple[str, str]]:
    """Recupera los últimos MAX_HISTORY mensajes del chat desde SQLite como
    tuplas (rol, texto), ordenadas del más viejo al más nuevo."""
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT role, content FROM (
                SELECT id, role, content FROM messages
                WHERE chat_id = ? ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (chat_id, MAX_HISTORY)
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    # Si la ventana corta a mitad de un intercambio y arranca con el modelo,
    # descartamos ese mensaje: el historial debe empezar con el usuario.
    if rows and rows[0][0] == "model":
        rows = rows[1:]

    return [(role, content) for role, content in rows]


def armar_entrada(historial: list[tuple[str, str]], mensaje_nuevo: str) -> list[dict]:
    """Convierte el historial guardado en la lista de pasos (steps) que espera
    la Interactions API y agrega al final el mensaje nuevo del usuario.

    Cada mensaje del usuario es un paso 'user_input' y cada respuesta del
    modelo un paso 'model_output', tal como espera la API.
    """
    tipo_de_paso = {"user": "user_input", "model": "model_output"}
    pasos = [
        {"type": tipo_de_paso[rol], "content": [{"type": "text", "text": texto}]}
        for rol, texto in historial
    ]
    pasos.append(
        {"type": "user_input", "content": [{"type": "text", "text": mensaje_nuevo}]}
    )
    return pasos

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa los mensajes usando el historial de SQLite, contexto temporal exacto,
    datos de la API de fútbol (estrategia híbrida), RAG sobre PDFs y estadísticas."""
    chat_id = update.effective_chat.id
    user_text = update.message.text
    usuario = update.effective_user

    # Registro/actualización del usuario en la DB (si nunca mandó /start, igual queda registrado)
    register_user(chat_id, usuario.id, usuario.username, usuario.first_name)

    # Log en terminal: nueva consulta, quién la hizo y qué preguntó
    logger.info(
        f"🆕 Nueva consulta | Usuario: {usuario.first_name} "
        f"(id: {usuario.id}, chat: {chat_id}) | Pregunta: {user_text}"
    )
    
    try:
        # 1. Recuperar el historial previo de SQLite (ventana acotada a MAX_HISTORY)
        historial = get_chat_history(chat_id)
        
        # 2. Consulta inteligente de partidos (Incluye Champions League, Promiedos y ESPN)
        info_partidos = ""
        palabras_clave_partidos = [
            "partido", "partidos", "juega", "hoy", "resultado", "resultados", "fecha",
            "tabla", "equipo", "equipos", "vs", "champions", "river", "boca",
            "real madrid", "barcelona", "laliga", "la liga", "premier", "ligue one",
            "ligue 1", "bundesliga", "serie a", "liga", "argentina", "argentino",
            "jornada", "ultim", "últim", "pasad", "recient", "ayer", "fixture",
            "calendario", "ganaron", "gano", "ganó", "clasico", "clásico",
        ]
        if any(palabra in user_text.lower() for palabra in palabras_clave_partidos):
            datos_hibridos = obtener_partidos_inteligente(user_text)
            if datos_hibridos:
                info_partidos = f"\n{datos_hibridos}\n"

        # 3. RAG: Buscar en PDFs locales
        info_rag = ""
        if any(w in user_text.lower() for w in ["reglamento", "articulo", "ley", "historia", "fundacion", "torneo", "formato"]):
            info_rag = buscar_en_pdfs(user_text)
            if info_rag:
                info_rag = f"\n[Información extraída de documentos oficiales/reglamentos]:\n{info_rag}\n"

        # 4. Estadísticas locales
        info_stats = ""
        if any(w in user_text.lower() for w in ["racha", "estadistica", "rendimiento", "ultimos partidos"]):
            palabras = user_text.split()
            equipo_buscado = palabras[-1] if palabras else ""
            if equipo_buscado:
                info_stats = f"\n{analizar_racha_equipo(equipo_buscado)}\n"

        # 5. Contexto temporal exacto en español
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        now = datetime.now()
        ahora_formateada = f"{dias[now.weekday()]} {now.day} de {meses[now.month]} de {now.year} a las {now.strftime('%H:%M')}"
        
        # 6. Construir el prompt inyectando el contexto temporal y los datos reales
        prompt_con_contexto = (
            f"[Contexto del sistema: Hoy es estrictamente {ahora_formateada}]. "
            f"{info_partidos}"
            f"{info_rag}"
            f"{info_stats}"
            f"Mensaje del usuario: {user_text}"
        )
        
        # 7. Consultar a Gemini con la Interactions API (sin estado, store=False).
        #    Se envía el sistema, el historial completo y el mensaje nuevo.
        interaccion = await client.aio.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config=generation_config,
            input=armar_entrada(historial, prompt_con_contexto),
            store=False,  # Google no guarda la conversación: la memoria es nuestra
        )
        bot_reply = interaccion.output_text
        if not bot_reply:
            raise ValueError("Gemini devolvió una respuesta vacía")
        
        # 8. Guardar la interacción en SQLite (usuario + respuesta, en una sola transacción)
        save_interaction(chat_id, user_text, bot_reply)
        
        # 9. Enviar la respuesta en Telegram
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error al procesar el mensaje con Gemini: {e}")
        await update.message.reply_text(
            "⚠️ Tuve un problema técnico procesando tu mensaje. Intentá de nuevo en un momento."
        )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analiza una imagen enviada por el usuario con Gemini (Interactions API)."""
    chat_id = update.effective_chat.id
    usuario = update.effective_user
    register_user(chat_id, usuario.id, usuario.username, usuario.first_name)
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        mime_type = f"image/{(image.format or 'JPEG').lower()}"
        
        user_caption = update.message.caption or "Analizá esta imagen de fútbol y dame un resumen detallado."
        
        # La Interactions API espera la imagen en base64 dentro del paso de entrada
        paso_usuario = {
            "type": "user_input",
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(bytes(photo_bytes)).decode("ascii"),
                    "mime_type": mime_type,
                },
                {"type": "text", "text": user_caption},
            ],
        }
        
        interaccion = await client.aio.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config=generation_config,
            input=[paso_usuario],
            store=False,
        )
        bot_reply = interaccion.output_text
        if not bot_reply:
            raise ValueError("Gemini devolvió una respuesta vacía")
        
        save_interaction(chat_id, f"[Imagen enviada]: {user_caption}", bot_reply)
        
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error procesando imagen: {e}")
        await update.message.reply_text("⚠️ Tuve un problema procesando la imagen.")

def main() -> None:
    """Función principal para poner en marcha el bot."""
    # Inicializar bases de datos
    init_db()
    init_stats_db()
    
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise ValueError("Faltan credenciales en el archivo .env (TELEGRAM_TOKEN o GEMINI_API_KEY).")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Registrar handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("olvidar", olvidar))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("BotTin v5_2 (Gemini + SQLite) con Champions League, Promiedos, RAG y Estadísticas activo... "
          "Presioná Ctrl+C para detenerlo.")
    app.run_polling()

if __name__ == "__main__":
    main()
