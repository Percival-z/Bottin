import base64
import io
import json
import logging
import os
import re
import sqlite3
import unicodedata
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
    """Crea las tablas de estadísticas locales si no existen: partidos de los
    equipos (racha), goleadores recientes y tablas de posiciones cacheadas."""
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
    # El mismo partido no debe guardarse dos veces (ESPN lo devuelve tanto en el
    # scoreboard como en el calendario del equipo).
    cursor.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_team_matches_unico '
        'ON team_matches(team, opponent, date)'
    )
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_scorers_cache (
            team_key TEXT PRIMARY KEY,
            payload TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS standings_cache (
            league TEXT PRIMARY KEY,
            payload TEXT,
            updated_at TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_loaded (
            team_key TEXT PRIMARY KEY,
            updated_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

# --- HISTORIAL DE PARTIDOS Y RACHA (SQLite + ESPN) ---
# La API-Football del plan gratis solo admite la fecha de hoy (y temporadas
# 2022-2024), así que los últimos partidos de un equipo se cargan desde el
# calendario público de ESPN (sin clave ni cuota) y quedan guardados en SQLite.

_TTL_CACHES_SEGUNDOS = 6 * 3600   # equipos, calendarios, goleadores y resúmenes

_CACHE_LIGAS_EQUIPOS: dict[str, tuple[float, list[tuple[int, str, str]]]] = {}
_CACHE_RESOLUCION: dict[str, tuple[float, tuple[str, int, str]]] = {}
_CACHE_CALENDARIO: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_CACHE_RESUMENES: dict[tuple[int, str], tuple[float, list[str]]] = {}
_CACHE_DOCUMENTOS = {"ts": 0.0, "paginas": None}

_STOPWORDS_EQUIPO = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "al", "en",
    "para", "por", "con", "y", "o", "que", "se", "como", "sobre", "entre", "su",
    "sus", "mi", "mis", "tu", "dame", "decime", "quiero", "ver", "hace", "hazme",
    "haceme", "hacer", "cuál", "cual", "cuando", "cuándo", "es", "son", "esta",
    "está", "este", "esto", "estos", "estas", "me", "lo", "le", "les", "nos",
    "racha", "forma", "estadística", "estadísticas", "estadistica", "estadisticas",
    "rendimiento", "partido", "partidos", "último", "últimos", "última", "últimas",
    "ultimo", "ultimos", "ultima", "ultimas", "reciente", "recientes", "jugados",
    "jugadas", "equipo", "equipos", "resultado", "resultados", "historial",
    "histórico", "historico", "actual", "actualmente", "contra", "favor", "ganados",
    "ganadas", "perdidos", "perdidas", "empatados", "los", "las", "jugó", "jugaron",
    "había", "hizo", "viene", "estuvo", "desde", "hasta", "sus", "cómo", "quién",
    "quien", "dónde", "donde", "qué", "cuyos", "todos", "mismo", "mismos",
}

def _sin_acentos(texto: str) -> str:
    """Quita las tildes conservando el resto del texto."""
    descompuesto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in descompuesto if not unicodedata.combining(c))

def _normalizar_nombre(texto: str) -> str:
    """Minúsculas, sin acentos, sin puntuación y con espacios simples: sirve para
    comparar nombres de equipos aunque el usuario escriba distinto que la fuente."""
    texto = _sin_acentos(texto).lower()
    texto = re.sub(r"[^\w\s]", " ", texto)
    texto = texto.replace("_", " ")
    return re.sub(r"\s+", " ", texto).strip()

def _extraer_equipo(texto: str) -> str:
    """Última palabra que parece nombre de equipo (saca puntuación y palabras de
    relleno): 'Dame las estadísticas de los últimos partidos de Boca.' → 'Boca'."""
    palabras = re.findall(r"[\wáéíóúüñÁÉÍÓÚÜÑ-]+", str(texto))
    candidatos = [
        p for p in palabras
        if len(p) >= 3
        and not p.isdigit()
        and p.lower().strip("-") not in _STOPWORDS_EQUIPO
    ]
    return candidatos[-1] if candidatos else ""

def _resultado_del_partido(a_favor: int, en_contra: int) -> str:
    if a_favor > en_contra:
        return "G"
    return "E" if a_favor == en_contra else "P"

def _consultar_historial(nombre: str) -> tuple[str | None, list[tuple]]:
    """Partidos guardados en SQLite que coinciden con `nombre`.

    Devuelve (equipo elegido, filas), donde las filas son
    (team, opponent, result, goals_for, goals_against, date) ordenadas de la más
    reciente a la más vieja. Si el nombre pega con varios equipos, se queda con
    el que más partidos tiene ("river" → "River Plate")."""
    patron = _normalizar_nombre(nombre).replace("%", "").replace("_", "")
    if not patron:
        return None, []
    conn = sqlite3.connect(DB_NAME)
    try:
        filas = conn.execute(
            """SELECT team, opponent, result, goals_for, goals_against, date
               FROM team_matches WHERE team LIKE ? ORDER BY date DESC""",
            (f"%{patron}%",),
        ).fetchall()
    finally:
        conn.close()
    if not filas:
        return None, []
    por_equipo: dict[str, list[tuple]] = {}
    for fila in filas:
        por_equipo.setdefault(fila[0], []).append(fila)
    elegido = max(por_equipo, key=lambda e: (len(por_equipo[e]), -len(e)))
    return elegido, por_equipo[elegido]

def _equipo_pendiente_de_cargar(clave: str) -> bool:
    """True si los partidos de ese equipo no se cargaron (o se cargaron hace más
    de _TTL_CACHES_SEGUNDOS): evita repetir la descarga en cada consulta."""
    conn = sqlite3.connect(DB_NAME)
    try:
        fila = conn.execute(
            "SELECT updated_at FROM team_loaded WHERE team_key = ?", (clave,)).fetchone()
    finally:
        conn.close()
    if not fila:
        return True
    try:
        transcurrido = (datetime.now() - datetime.fromisoformat(fila[0])).total_seconds()
    except Exception:
        return True
    return transcurrido > _TTL_CACHES_SEGUNDOS

def _marcar_equipo_cargado(clave: str) -> None:
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            """INSERT INTO team_loaded (team_key, updated_at) VALUES (?, ?)
               ON CONFLICT(team_key) DO UPDATE SET updated_at = excluded.updated_at""",
            (clave, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

def _guardar_partidos(partidos: list[tuple]) -> int:
    """Guarda filas (team, opponent, result, gf, ga, date) en SQLite. Devuelve la
    cantidad de registros nuevos (los repetidos se ignoran por el índice único)."""
    if not partidos:
        return 0
    conn = sqlite3.connect(DB_NAME)
    try:
        cambios_antes = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO team_matches
               (team, opponent, result, goals_for, goals_against, date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            partidos,
        )
        conn.commit()
        return conn.total_changes - cambios_antes
    finally:
        conn.close()

def _gole_del_competidor(competidor: dict) -> int | None:
    marcador = competidor.get("score")
    if isinstance(marcador, dict):
        marcador = marcador.get("value")
    if marcador is None:
        return None
    try:
        return int(float(marcador))
    except (TypeError, ValueError):
        return None

def _evento_finalizado(evento: dict) -> tuple[str, str, int, str, int] | None:
    """(fecha, local, goles local, visita, goles visita) si el evento de ESPN terminó."""
    competicion = (evento.get("competitions") or [{}])[0]
    estado = (competicion.get("status") or evento.get("status") or {}).get("type", {}) or {}
    if estado.get("state") != "post" and estado.get("name") != "STATUS_FULL_TIME":
        return None
    competidores = competicion.get("competitors") or []
    local = next((c for c in competidores if c.get("homeAway") == "home"), None)
    visita = next((c for c in competidores if c.get("homeAway") == "away"), None)
    if not local or not visita:
        return None
    nombre_local = ((local.get("team") or {}).get("displayName")
                    or (local.get("team") or {}).get("name") or "")
    nombre_visita = ((visita.get("team") or {}).get("displayName")
                     or (visita.get("team") or {}).get("name") or "")
    goles_local, goles_visita = _gole_del_competidor(local), _gole_del_competidor(visita)
    fecha = (evento.get("date") or "")[:10]
    if not fecha or not nombre_local or not nombre_visita or goles_local is None or goles_visita is None:
        return None
    return fecha, nombre_local, goles_local, nombre_visita, goles_visita

def _guardar_eventos(eventos: list[dict]) -> int:
    """Vuelve eventos de ESPN en filas de team_matches (uno por cada equipo)."""
    filas = []
    for evento in eventos:
        fin = _evento_finalizado(evento)
        if not fin:
            continue
        fecha, local, goles_local, visita, goles_visita = fin
        filas.append((local, visita, _resultado_del_partido(goles_local, goles_visita),
                      goles_local, goles_visita, fecha))
        filas.append((visita, local, _resultado_del_partido(goles_visita, goles_local),
                      goles_visita, goles_local, fecha))
    return _guardar_partidos(filas)

def _equipos_de_liga(slug: str) -> list[tuple[int, str, str]]:
    """Equipos de una liga de ESPN: [(id, nombre, nombre para mostrar)]."""
    ahora = datetime.now().timestamp()
    cache = _CACHE_LIGAS_EQUIPOS.get(slug)
    if cache and ahora - cache[0] < _TTL_CACHES_SEGUNDOS:
        return cache[1]
    url = f"http://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams"
    equipos: list[tuple[int, str, str]] = []
    try:
        respuesta = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if respuesta.status_code == 200:
            datos = respuesta.json()
            ligas = list(datos.get("leagues") or [])
            for deporte in datos.get("sports") or []:
                ligas += deporte.get("leagues") or []
            for liga in ligas:
                for equipo in liga.get("teams") or []:
                    info = equipo.get("team") or {}
                    if info.get("id") and (info.get("displayName") or info.get("name")):
                        equipos.append((
                            int(info["id"]),
                            str(info.get("name") or info.get("displayName")),
                            str(info.get("displayName") or info.get("name")),
                        ))
    except Exception as e:
        logger.warning(f"⚠️ No se pudieron leer los equipos de ESPN ({slug}): {e}")
    if equipos:
        _CACHE_LIGAS_EQUIPOS[slug] = (ahora, equipos)
    return equipos

def _resolver_equipo_espn(nombre: str) -> tuple[str, int, str] | None:
    """Busca un equipo por nombre en las ligas conocidas de ESPN → (slug, id, nombre)."""
    objetivo = _normalizar_nombre(nombre)
    if not objetivo:
        return None
    ahora = datetime.now().timestamp()
    cache = _CACHE_RESOLUCION.get(objetivo)
    if cache and ahora - cache[0] < _TTL_CACHES_SEGUNDOS:
        return cache[1]
    slugs = [slug for slug, _, _ in LIGAS_ESPN] + list(SLUGS_SELECCION_ESPN)
    mejor: tuple[tuple[int, int], tuple[str, int, str]] | None = None
    for slug in slugs:
        for team_id, nombre_api, display in _equipos_de_liga(slug):
            variantes = {_normalizar_nombre(display), _normalizar_nombre(nombre_api)}
            if objetivo in variantes:
                prioridad = 0
            elif any(objetivo in v or v in objetivo for v in variantes if v):
                prioridad = 1
            else:
                continue
            clave = (prioridad, len(display))
            if mejor is None or clave < mejor[0]:
                mejor = (clave, (slug, team_id, display))
        if mejor and mejor[0][0] == 0:
            break
    if not mejor:
        return None
    _CACHE_RESOLUCION[objetivo] = (ahora, mejor[1])
    return mejor[1]

def _calendario_espn(slug: str, team_id: int) -> list[dict]:
    """Partidos de la temporada (y la anterior como respaldo) de un equipo en ESPN."""
    ahora = datetime.now().timestamp()
    clave = (slug, team_id)
    cache = _CACHE_CALENDARIO.get(clave)
    if cache and ahora - cache[0] < _TTL_CACHES_SEGUNDOS:
        return cache[1]
    url = f"http://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams/{team_id}/schedule"
    temporada = datetime.now().year
    eventos: list[dict] = []
    for anio in (temporada, temporada - 1):
        try:
            respuesta = requests.get(url, params={"season": anio},
                                     headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if respuesta.status_code == 200:
                eventos = respuesta.json().get("events") or []
        except Exception as e:
            logger.warning(f"⚠️ Error consultando el calendario de ESPN ({slug}/{team_id}): {e}")
            eventos = []
        if eventos:
            break
    if eventos:
        _CACHE_CALENDARIO[clave] = (ahora, eventos)
    return eventos

def _goleadores_en_partido(event_id: int, nombre_equipo: str, slug: str) -> list[str]:
    """Jugadores del equipo que marcaron gol en un partido. Usa los `keyEvents`
    estructurados de ESPN (scoringPlay) y, si no vienen, el commentary en texto.
    Los vacíos no se cachean: si ESPN no respondió, se reintenta la próxima vez."""
    objetivo = _normalizar_nombre(nombre_equipo)
    clave_cache = (event_id, objetivo)
    ahora = datetime.now().timestamp()
    cache = _CACHE_RESUMENES.get(clave_cache)
    if cache and ahora - cache[0] < _TTL_CACHES_SEGUNDOS:
        return cache[1]
    goleadores: list[str] = []
    try:
        respuesta = requests.get(
            f"http://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/summary",
            params={"event": event_id}, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if respuesta.status_code == 200:
            datos = respuesta.json()
            goles = [e for e in datos.get("keyEvents") or [] if e.get("scoringPlay")]
            for gol in goles:
                equipo = _normalizar_nombre((gol.get("team") or {}).get("displayName") or "")
                if not (objetivo and objetivo in equipo):
                    continue
                participantes = gol.get("participants") or []
                atleta = (participantes[0].get("athlete") or {}) if participantes else {}
                if atleta.get("displayName"):
                    goleadores.append(atleta["displayName"])
            if not goles:
                # Respaldo: "Goal! Boca Juniors 1, River 0. Enner Valencia (Boca Juniors) ..."
                for entrada in datos.get("commentary") or []:
                    texto = (entrada.get("text") or "").strip()
                    bajo = texto.lower()
                    if not (bajo.startswith("goal!") or bajo.startswith("penalty scored")):
                        continue
                    for coincidencia in re.finditer(
                            r"([A-ZÁÉÍÓÚÑ][^()\d.,:]{2,60}?)\s*\(([^)]+)\)", texto):
                        jugador = coincidencia.group(1).strip()
                        equipo = _normalizar_nombre(coincidencia.group(2))
                        if jugador and objetivo and objetivo in equipo:
                            goleadores.append(jugador)
                            break
    except Exception as e:
        logger.warning(f"⚠️ No se pudieron leer los goleadores del partido {event_id}: {e}")
        return []
    if goleadores:
        _CACHE_RESUMENES[clave_cache] = (ahora, goleadores)
    return goleadores

def _leer_goleadores(clave: str) -> list[tuple[str, int]] | None:
    """Goleadores guardados del equipo; None si no hay o vencieron (6 h)."""
    conn = sqlite3.connect(DB_NAME)
    try:
        fila = conn.execute(
            "SELECT payload, updated_at FROM team_scorers_cache WHERE team_key = ?",
            (clave,),
        ).fetchone()
    finally:
        conn.close()
    if not fila:
        return None
    try:
        if (datetime.now() - datetime.fromisoformat(fila[1])).total_seconds() > _TTL_CACHES_SEGUNDOS:
            return None
        datos = json.loads(fila[0])
    except Exception:
        return None
    return [(d["jugador"], int(d["goles"])) for d in datos]

def _actualizar_goleadores(clave: str, nombre_equipo: str, slug: str, eventos: list[dict]) -> None:
    """Calcula los goleadores del equipo en sus últimos 5 partidos y los guarda."""
    if _leer_goleadores(clave) is not None:
        return
    finalizados = [e for e in eventos if _evento_finalizado(e)]
    finalizados.sort(key=lambda e: e.get("date") or "", reverse=True)
    contador: dict[str, int] = {}
    for evento in finalizados[:5]:
        try:
            event_id = int(evento.get("id"))
        except (TypeError, ValueError):
            continue
        # El resumen se pide en la liga del partido (una Copa puede no estar en la liga del equipo)
        slug_partido = (evento.get("league") or {}).get("slug") or slug
        for jugador in _goleadores_en_partido(event_id, nombre_equipo, slug_partido):
            contador[jugador] = contador.get(jugador, 0) + 1
    payload = [
        {"jugador": j, "goles": g}
        for j, g in sorted(contador.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            """INSERT INTO team_scorers_cache (team_key, payload, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(team_key) DO UPDATE SET
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (clave, json.dumps(payload, ensure_ascii=False), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

def poblar_historial_equipo(nombre: str) -> bool:
    """Descarga desde ESPN los últimos partidos y goleadores del equipo y los deja
    guardados en SQLite. Devuelve True si hay datos en la base para ese nombre."""
    resuelto = _resolver_equipo_espn(nombre)
    if resuelto:
        slug, team_id, display = resuelto
        clave = _normalizar_nombre(nombre)
        eventos = _calendario_espn(slug, team_id)
        if eventos and _equipo_pendiente_de_cargar(clave):
            nuevos = _guardar_eventos(eventos)
            _marcar_equipo_cargado(clave)
            logger.info(f"🗂️ Se cargaron {nuevos} registros de partidos de {display} en SQLite.")
        if eventos:
            try:
                _actualizar_goleadores(clave, display, slug, eventos)
            except Exception as e:
                logger.warning(f"⚠️ No se pudieron actualizar los goleadores de {display}: {e}")
    _, filas = _consultar_historial(nombre)
    return bool(filas)

def analizar_racha_equipo(nombre_equipo: str) -> str:
    """Informe de racha con salida lista para armar la respuesta: porcentaje de
    victorias, goles a favor/en contra, forma reciente y goleador del período.
    Los datos viven en SQLite y se refrescan desde ESPN si son viejos."""
    nombre = _extraer_equipo(nombre_equipo)
    if not nombre:
        return ("No pude identificar de qué equipo querés la racha. "
                "Escribime, por ejemplo: 'racha de Boca'.")

    # Si en la base no hay datos frescos, se descargan (si falla la red, se
    # responde igual con lo que ya esté guardado).
    try:
        poblar_historial_equipo(nombre)
    except Exception as e:
        logger.warning(f"⚠️ No se pudo actualizar el historial de {nombre}: {e}")

    equipo, filas = _consultar_historial(nombre)
    if not equipo or not filas:
        return (f"No encontré registros históricos en la base de datos local "
                f"para el equipo '{nombre}'. Probá de nuevo en unos minutos.")

    recientes = filas[:5]
    total = len(recientes)
    ganados = sum(1 for f in recientes if f[2] == "G")
    empatados = sum(1 for f in recientes if f[2] == "E")
    perdidos = sum(1 for f in recientes if f[2] == "P")
    goles_favor = sum(f[3] for f in recientes)
    goles_contra = sum(f[4] for f in recientes)
    porcentaje_victorias = (ganados / total) * 100

    simbolos = {"G": "✅", "E": "➖", "P": "❌"}
    lineas_forma = []
    for _, rival, resultado, a_favor, en_contra, fecha in recientes:
        try:
            fecha = datetime.fromisoformat(fecha).strftime("%d/%m/%Y")
        except Exception:
            pass
        lineas_forma.append(
            f"  {simbolos.get(resultado, '•')} {fecha}: {equipo} {a_favor}-{en_contra} {rival}"
        )

    goleadores = _leer_goleadores(_normalizar_nombre(nombre)) or []
    bloque_goleador = ""
    if goleadores:
        principal = goleadores[0]
        otros = ", ".join(f"{jugador} ({goles})" for jugador, goles in goleadores[1:3])
        bloque_goleador = (f"- Goleador del período: {principal[0]} con {principal[1]} tantos"
                           + (f"; también {otros}." if otros else ".") + "\n")

    return (
        f"📊 Informe de racha ({equipo}) — últimos {total} partidos:\n"
        f"- Victorias: {ganados} | Empates: {empatados} | Derrotas: {perdidos} "
        f"(efectividad de victorias {porcentaje_victorias:.1f}%)\n"
        f"- Goles a favor: {goles_favor} | Goles en contra: {goles_contra} "
        f"(promedio {goles_favor / total:.1f} a favor y {goles_contra / total:.1f} en contra por partido)\n"
        f"- Forma reciente (de más reciente a más viejo):\n"
        + "\n".join(lineas_forma) + "\n"
        + bloque_goleador
    )

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

def obtener_partidos_api(ligas_permitidas: list[int] | None = None,
                         equipos: list[str] | None = None) -> str:
    """Consulta los partidos del día en API-Football para Champions League y
    grandes ligas europeas, con logs de diagnóstico.

    `ligas_permitidas` permite pedir otra lista de competiciones (p. ej. las
    selecciones, con LIGAS_SELECCION_API). Por defecto, las ligas de clubes.
    `equipos` filtra por nombres de selecciones (en el idioma de la fuente)
    antes del corte de líneas, para no perder el partido que se pregunta.

    La fecha se envía junto con `timezone` para que el filtro 'date' se
    interprete en la zona horaria del usuario (sin ella, la API filtra en UTC
    y entre las 21:00 y medianoche local se pediría el día equivocado).
    """
    if not FOOTBALL_API_KEY:
        logger.warning("FOOTBALL_API_KEY no está configurada o está vacía en el .env")
        return ""

    hoy = datetime.now().strftime("%Y-%m-%d")

    # IDs permitidos por defecto: Champions League (2), Premier League (39),
    # La Liga (140), Serie A (135), Bundesliga (78), Ligue 1 (61).
    # Si el pedido es por selecciones se pasa LIGAS_SELECCION_API.
    if ligas_permitidas is None:
        ligas_permitidas = [2, 39, 140, 135, 78, 61]

    # Caché: misma fecha + misma zona horaria + mismas ligas → respuesta ya
    # conocida (la clave incluye las ligas para no mezclar clubes y selecciones)
    cache_key = f"{hoy}|{FOOTBALL_TIMEZONE}|{','.join(str(i) for i in ligas_permitidas)}"
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

                linea = f"- [{liga}] {local} vs {visita} -> {marcador}"
                # Si preguntó por una selección concreta, filtrarla acá (antes
                # del corte de 15 líneas) para no perder su partido
                if equipos and not any(e in linea.lower() for e in equipos):
                    continue
                resumen.append(linea)

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

# --- SELECCIONES (fútbol de selecciones) ---
# Palabras que indican que el usuario pregunta por selecciones y no por clubes
SELECCION_KEYWORDS = [
    "seleccion", "selección", "selecciones", "selecc", "scaloneta",
    "mundial", "mondial", "copa america", "eurocopa", "eliminatoria",
    "eliminatorias", "amistoso", "amistosos", "nations league",
    "liga de naciones", "finalissima",
]

def es_selecciones(texto: str) -> bool:
    """True si la consulta es por selecciones. 'Mundial de clubes', Libertadores
    y Sudamericana quedan excluidos (son competencias de clubes)."""
    t = texto.lower()
    if any(k in t for k in ("club", "libertadores", "sudamericana")):
        return False
    return any(k in t for k in SELECCION_KEYWORDS)

# IDs de API-Football para selecciones (verificados con /leagues?country=World).
# El plan gratis solo cubre la fecha pedida, así que sirven para "hoy".
LIGAS_SELECCION_API = [
    1,    # World Cup
    4,    # Euro Championship
    9,    # Copa América
    10,   # Friendlies
    5,    # UEFA Nations League
    536,  # CONCACAF Nations League
    34,   # World Cup - Qualification South America (CONMEBOL)
    32,   # World Cup - Qualification Europe
    31, 30, 29, 33, 37,  # Eliminatorias CONCACAF / Asia / África / Oceanía / Intercontinental
    6,    # Africa Cup of Nations
    36,   # Africa Cup of Nations - Qualification
    913,  # CONMEBOL - UEFA Finalissima
    1038, # King's Cup (amistoso entre selecciones)
    1222, # FIFA Series (amistosos)
]

# Slugs de ESPN para selecciones (verificados: HTTP 200 con partidos en sus fechas)
SLUGS_SELECCION_ESPN = [
    "fifa.friendly",          # amistosos (ventana FIFA actual)
    "fifa.world",             # Copa del Mundo
    "fifa.worldq.CONMEBOL",   # eliminatorias CONMEBOL
    "fifa.worldq.UEFA",       # eliminatorias Europa
    "conmebol.america",       # Copa América
    "uefa.euro",              # Eurocopa
    "uefa.nations",           # Nations League
]

# Selecciones: como aparece en las fuentes (inglés) → variantes que puede
# escribir el usuario. Sirve para filtrar los partidos al país que pregunta.
EQUIPOS_SELECCION = {
    "argentina": ["argentina"],
    "brazil": ["brasil", "brazil"],
    "uruguay": ["uruguay"],
    "chile": ["chile"],
    "paraguay": ["paraguay"],
    "colombia": ["colombia"],
    "peru": ["perú", "peru"],
    "bolivia": ["bolivia"],
    "venezuela": ["venezuela"],
    "ecuador": ["ecuador"],
    "mexico": ["méxico", "mexico"],
    "usa": ["usa", "estados unidos", "eeuu", "ee. uu."],
    "united states": ["usa", "estados unidos", "eeuu"],
    "spain": ["españa", "espana"],
    "france": ["francia"],
    "italy": ["italia"],
    "germany": ["alemania"],
    "england": ["inglaterra"],
    "portugal": ["portugal"],
    "netherlands": ["países bajos", "paises bajos", "holanda"],
    "belgium": ["bélgica", "belgica"],
    "croatia": ["croacia"],
    "switzerland": ["suiza"],
    "denmark": ["dinamarca"],
    "norway": ["noruega"],
    "sweden": ["suecia"],
    "russia": ["rusia"],
    "turkey": ["turquía", "turquia"],
    "japan": ["japón", "japon"],
    "korea": ["corea"],
    "australia": ["australia"],
    "morocco": ["marruecos"],
    "nigeria": ["nigeria"],
    "senegal": ["senegal"],
    "egypt": ["egipto"],
    "canada": ["canada"],
}

def _equipos_mencionados(texto: str) -> list[str]:
    """Selecciones que el usuario mencionó, con el nombre tal como aparece en
    las fuentes (p. ej. "brasil" → "brazil"). Vacío si no menciona ningún país."""
    t = texto.lower()
    return [
        en_fuente
        for en_fuente, variantes in EQUIPOS_SELECCION.items()
        if any(variante in t for variante in variantes)
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
    """Convierte los eventos de ESPN en líneas con el mismo formato del resto.

    De paso guarda los partidos finalizados en SQLite: cada consulta de partidos
    va nutriendo la base local que después usa el análisis de racha."""
    try:
        nuevos = _guardar_eventos(events)
        if nuevos:
            logger.info(f"🗂️ {nuevos} partidos nuevos de {nombre_liga} guardados en SQLite para rachas.")
    except Exception as e:
        logger.warning(f"⚠️ No se pudieron guardar los partidos de {nombre_liga}: {e}")
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
    nombres_seleccion = {
        "fifa.friendly": "Amistosos de Selecciones",
        "fifa.world": "Copa del Mundo",
        "fifa.worldq.CONMEBOL": "Eliminatorias CONMEBOL",
        "fifa.worldq.UEFA": "Eliminatorias Europa",
        "conmebol.america": "Copa América",
        "uefa.euro": "Eurocopa",
        "uefa.nations": "Nations League",
    }
    if slug in nombres_seleccion:
        return nombres_seleccion[slug]
    return next((nombre for s, nombre, _ in LIGAS_ESPN if s == slug), slug)

def _filtrar_mencionados(cadena: str, equipos: list[str]) -> str:
    """Deja solo las líneas que involucran a las selecciones de `equipos`.
    Sin equipos indicados, devuelve la cadena sin cambios."""
    if not cadena or not equipos:
        return cadena
    filtradas = [l for l in cadena.splitlines()
                 if any(e in l.lower() for e in equipos)]
    return "\n".join(filtradas)

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

def obtener_ultimos_partidos_espn(slugs: list[str], max_dias: int = 7,
                                  max_dias_jornada: int = 3,
                                  equipos: list[str] | None = None) -> tuple[str, str]:
    """Busca la jornada más reciente con partidos y la acumula mientras los días
    consecutivos también tengan partidos (una ronda puede abarcar varios días,
    p. ej. sábado y domingo). Devuelve (resumen, rango_de_fechas).

    `equipos` (nombres en el idioma de la fuente) deja solo los partidos de
    esas selecciones: el barrido sigue hacia atrás hasta encontrarlas, dentro
    del tope de max_dias.
    """
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

        if equipos:
            lineas = [l for l in lineas if any(e in l.lower() for e in equipos)]
                
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
    es_selecc = es_selecciones(texto)

    if es_selecc:
        # Selecciones: fuentes propias (API-Football de selecciones / ESPN NT)
        slugs = SLUGS_SELECCION_ESPN
        liga_especificada = False
        equipos = _equipos_mencionados(texto)
    else:
        ligas = detectar_ligas(texto)
        slugs = [slug for slug, _ in ligas] or [slug for slug, _, _ in LIGAS_ESPN]
        liga_especificada = bool(ligas)
        equipos = []
    
    # Si dice "hoy", manda hoy; si no, decide por las palabras de pasado
    quiere_pasado = (
        "hoy" not in texto
        and any(palabra in texto for palabra in PALABRAS_PASADO)
    )
    # "¿cómo viene?" pide forma reciente: además de los partidos de hoy se
    # trae la última jornada, para que el modelo pueda ver los últimos resultados
    quiere_historial = any(p in texto for p in (
        "cómo viene", "como viene", "en qué forma", "en que forma",
        "esta jugando", "está jugando", "ultima fecha", "última fecha",
    ))
    
    datos_hoy = ""
    fuente_hoy = ""
    
    if not quiere_pasado:
        if es_selecc:
            if FOOTBALL_API_KEY:
                datos_hoy = obtener_partidos_api(LIGAS_SELECCION_API, equipos=equipos or None)
                fuente_hoy = "API-Football (Selecciones)"
        elif es_futbol_argentino:
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
            fuente_hoy = "ESPN" if not es_selecc else "ESPN (Selecciones)"

    # Selecciones: si el usuario nombró un país, quedarse solo con sus partidos
    if es_selecc:
        datos_hoy = _filtrar_mencionados(datos_hoy, equipos)

    # Jornadas anteriores: si se pide el pasado, si hoy no hay nada o si se
    # pregunta por la forma reciente ("¿cómo viene?")
    datos_recientes = ""
    fecha_jornada = ""
    if quiere_pasado or not datos_hoy or quiere_historial:
        # Selecciones: barrido más largo (21 días) y filtrado por el equipo
        # dentro del barrido, para llegar al último partido del país
        datos_recientes, fecha_jornada = obtener_ultimos_partidos_espn(
            slugs,
            max_dias=21 if es_selecc else 7,
            equipos=equipos or None,
        )
        
    bloques = []
    if datos_hoy:
        bloques.append(f"- Fuente de datos: {fuente_hoy}\n- Partidos de hoy:\n{datos_hoy}")
    if datos_recientes:
        if quiere_pasado:
            bloques.append(
                f"- Fuente de datos: ESPN\n"
                f"- Últimos partidos encontrados (jornada: {fecha_jornada}):\n{datos_recientes}"
            )
        elif datos_hoy:
            # Hay partidos de hoy y además se pidió la forma reciente
            bloques.append(
                f"- Fuente de datos: ESPN\n"
                f"- Últimos partidos conocidos (jornada: {fecha_jornada}), forma reciente:\n"
                f"{datos_recientes}"
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

# --- TABLA DE POSICIONES (scrapper de Promiedos) ---
# La página de cada liga en promiedos.com.ar trae las tablas completas dentro
# del JSON embebido (__NEXT_DATA__), igual que los partidos del día, así que
# no hace falta parsear HTML.
# Otras fuentes descartadas: API-Football no sirve porque el plan gratis solo
# admite temporadas 2022-2024 ('Free plans do not have access to this season'),
# y ESPN solo expone las tablas con ~40 peticiones encadenadas (su endpoint
# liviano devuelve `false`).

# (slug ESPN → (url_name, id) de Promiedos, mismos identificadores de LIGAS_ESPN)
PROMIEDOS_LIGAS = {
    "arg.1": ("liga-profesional", "hc"),
    "eng.1": ("premier-league", "h"),
    "esp.1": ("laliga", "bb"),
    "ita.1": ("serie-a", "bh"),
    "ger.1": ("bundesliga", "cf"),
    "fra.1": ("ligue-1", "df"),
    "uefa.champions": ("uefa-champions-league", "fhc"),
}

# Competiciones de selecciones en Promiedos: palabra clave → (url_name, id, nombre).
# (Los amistosos no tienen tabla: su página responde 404)
PROMIEDOS_SELECCION = [
    ("eliminatoria", "conmebol-wc-qualification", "gbd", "Eliminatorias CONMEBOL"),
    ("mundial", "fifa-world-cup", "fjda", "Copa del Mundo"),
    ("copa america", "copa-america", "fjf", "Copa América"),
    ("eurocopa", "euro", "gdbg", "Eurocopa"),
    ("nations league", "uefa-nations-league", "habg", "Nations League"),
    ("liga de naciones", "uefa-nations-league", "habg", "Nations League"),
]

def _destino_seleccion(texto: str) -> tuple[str, str, str] | None:
    """Resuelve qué tabla de selecciones pide el usuario → (url_name, id, nombre)."""
    if "eliminatoria" in texto and any(k in texto for k in ("europa", "uefa", "europea")):
        return ("european-qualifiers", "gahb", "Eliminatorias Europa")
    for clave, url_name, id_liga, nombre in PROMIEDOS_SELECCION:
        if clave in texto:
            return (url_name, id_liga, nombre)
    return None

def _tablas_promiedos(url_name: str, id_liga: str) -> list[dict]:
    """Descarga la página de una liga y devuelve sus grupos de tablas."""
    url = f"https://www.promiedos.com.ar/league/{url_name}/{id_liga}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"⚠️ Promiedos respondió HTTP {response.status_code} en {url_name}")
            return []
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text, re.S
        )
        if not match:
            logger.warning("❌ No se encontró __NEXT_DATA__ en la página de la liga de Promiedos.")
            return []
        data = json.loads(match.group(1))
        return data["props"]["pageProps"]["data"].get("tables_groups", []) or []
    except Exception as e:
        logger.error(f"❌ Error leyendo tablas de Promiedos ({url_name}): {e}")
        return []

def _formatear_tabla(tabla: dict, max_filas: int = 20) -> list[str]:
    """Convierte una tabla de Promiedos en líneas 'pos. equipo | pts | PJ | G E P | goles'."""
    lineas = []
    for fila in tabla.get("rows", [])[:max_filas]:
        equipo = fila.get("entity", {}).get("object", {}).get("name", "?")
        valores = {v.get("key"): v.get("value") for v in fila.get("values", [])}
        pos = fila.get("num", "?")
        pts = valores.get("Points", "-")
        pj = valores.get("GamePlayed", "-")
        g, e, p = valores.get("GamesWon", "-"), valores.get("GamesEven", "-"), valores.get("GamesLost", "-")
        goles = valores.get("Goals", "")
        extra = f" | {goles}" if goles else ""
        lineas.append(f"- {pos}. {equipo} | {pts} pts | PJ {pj} | G {g} E {e} P {p}{extra}")
    return lineas

def _guardar_tabla_cache(clave: str, contenido: str) -> None:
    """Guarda una tabla de posiciones ya formateada en SQLite (respaldo local)."""
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute(
            """INSERT INTO standings_cache (league, payload, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(league) DO UPDATE SET
                   payload = excluded.payload,
                   updated_at = excluded.updated_at""",
            (clave, contenido, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

def _leer_tabla_cache(clave: str) -> str:
    """Última tabla de posiciones guardada de esa competencia (vacío si no hay)."""
    conn = sqlite3.connect(DB_NAME)
    try:
        fila = conn.execute(
            "SELECT payload, updated_at FROM standings_cache WHERE league = ?",
            (clave,),
        ).fetchone()
    finally:
        conn.close()
    if not fila:
        return ""
    try:
        fecha = datetime.fromisoformat(fila[1]).strftime("%d/%m/%Y a las %H:%M")
    except Exception:
        fecha = "una fecha anterior"
    return (fila[0]
            + f"\n- (Nota: no se pudo actualizar la tabla ahora; se muestran los datos "
              f"guardados el {fecha}.)")

def obtener_tabla_posiciones(user_text: str) -> str:
    """Devuelve la tabla de posiciones de la liga que menciona el usuario
    (scrapper de Promiedos). Si no aclara la liga, devuelve un aviso para que
    el modelo pregunte de cuál quiere la tabla."""
    texto = user_text.lower()

    # Selecciones: competencias propias (eliminatorias, mundial, Copa América...)
    if es_selecciones(texto):
        destino = _destino_seleccion(texto)
        if not destino:
            return (
                "[Aviso: el usuario pidió una tabla de selecciones sin aclarar de qué "
                "competencia. Preguntale si quiere eliminatorias (CONMEBOL o Europa), "
                "mundial, Copa América, Eurocopa o Nations League.]"
            )
        url_name, id_liga, nombre_liga = destino
    else:
        ligas = detectar_ligas(texto)
        if not ligas:
            return (
                "[Aviso: el usuario pidió una tabla de posiciones pero no especificó la liga. "
                "Preguntale de cuál quiere la tabla: Argentina (Liga Profesional), Premier League, "
                "LaLiga, Serie A, Bundesliga, Ligue 1 o Champions League.]"
            )

        slug, nombre_liga = ligas[0]
        if slug not in PROMIEDOS_LIGAS:
            return ""
        url_name, id_liga = PROMIEDOS_LIGAS[slug]

    grupos = _tablas_promiedos(url_name, id_liga)
    if not grupos:
        # Si Promiedos no respondió (o volvió a cambiar su web), se responde con
        # la última tabla de esa competencia guardada en SQLite.
        cache = _leer_tabla_cache(nombre_liga)
        if cache:
            logger.warning(f"⚠️ Promiedos sin datos de {nombre_liga}: se usa el caché local de SQLite.")
        return cache

    # Candidatas: (nombre del grupo, nombre de la tabla, tabla con filas)
    candidatas = []
    for grupo in grupos:
        gname = (grupo.get("name") or "").strip()
        for tabla in grupo.get("tables", []):
            tname = (tabla.get("name") or "").strip()
            contenido = tabla.get("table") or {}
            if contenido.get("rows"):
                candidatas.append((gname, tname, contenido))

    if not candidatas:
        return ""

    # Selección según lo que pida el usuario
    if any(k in texto for k in ["promedio", "descens", "releg"]):
        elegidas = [c for c in candidatas
                    if re.search(r"promedio|releg|descens", f"{c[0]} {c[1]}", re.I)]
    elif "anual" in texto:
        elegidas = [c for c in candidatas if "anual" in f"{c[0]} {c[1]}".lower()]
    else:
        # Torneo vigente: el primer grupo con nombre (p. ej. "Clausura");
        # en ligas europeas todos los grupos vienen sin nombre → la primera tabla
        primer_grupo = next((g for g, _, _ in candidatas if g), None)
        elegidas = ([c for c in candidatas if c[0] == primer_grupo]
                    if primer_grupo else candidatas[:1])

    if not elegidas:
        elegidas = candidatas[:1]

    # Tope de tablas: competencias como el mundial tienen 12 grupos y la
    # Nations League 16; no conviene mandar todas al modelo
    truncado = len(elegidas) > 6
    if truncado:
        elegidas = elegidas[:6]

    bloques = [f"[Tabla de posiciones] {nombre_liga} (fuente: Promiedos)"]
    for gname, tname, tabla in elegidas:
        titulo = " - ".join(x for x in (gname, tname) if x) or nombre_liga
        lineas = _formatear_tabla(tabla)
        if lineas:
            bloques.append(f"- {titulo}:\n" + "\n".join(lineas))

    if len(bloques) == 1:
        return ""

    resultado = "\n".join(bloques)
    if truncado:
        resultado += "\n- (Nota: la competencia tiene más grupos; se muestran solo los primeros 6.)"
    _guardar_tabla_cache(nombre_liga, resultado)
    logger.info(f"📊 Tabla de posiciones de {nombre_liga} obtenida de Promiedos "
                f"({len(elegidas)} tabla(s)).")
    return resultado

# --- RAG CON DOCUMENTOS (PDF, TXT y MD en la carpeta 'docs/') ---

def _paginas_de_documento(path: str) -> list[tuple[int, str]]:
    """[(número de página, texto)] de un PDF, o una única 'página' para TXT/MD."""
    if path.lower().endswith(".pdf"):
        paginas = []
        try:
            reader = PdfReader(path)
            for i, page in enumerate(reader.pages):
                texto = page.extract_text() or ""
                if texto.strip():
                    paginas.append((i + 1, texto))
        except Exception as e:
            logger.error(f"Error leyendo PDF {os.path.basename(path)}: {e}")
        return paginas
    try:
        with open(path, encoding="utf-8", errors="ignore") as archivo:
            return [(1, archivo.read())]
    except Exception as e:
        logger.error(f"Error leyendo {os.path.basename(path)}: {e}")
        return []

def _documentos_cargados() -> list[tuple[str, int, str]]:
    """Todos los documentos de 'docs/' como (archivo, página, texto), con caché de
    10 minutos para no releer los PDFs en cada consulta."""
    ahora = datetime.now().timestamp()
    if _CACHE_DOCUMENTOS["paginas"] is not None and ahora - _CACHE_DOCUMENTOS["ts"] < 600:
        return _CACHE_DOCUMENTOS["paginas"]
    docs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    paginas: list[tuple[str, int, str]] = []
    if os.path.isdir(docs_dir):
        for filename in sorted(os.listdir(docs_dir)):
            if filename.lower().endswith((".pdf", ".txt", ".md")):
                for numero, texto in _paginas_de_documento(os.path.join(docs_dir, filename)):
                    paginas.append((filename, numero, texto))
    _CACHE_DOCUMENTOS.update(ts=ahora, paginas=paginas)
    return paginas

def _trocear(texto: str, tamanio: int = 1200) -> list[str]:
    """Troza un texto en fragmentos de ~tamanio caracteres por párrafos, sin cortar
    oraciones: así el RAG manda los fragmentos más manejables al modelo."""
    trozos: list[str] = []
    actual = ""
    for parrafo in texto.split("\n\n"):
        parrafo = re.sub(r"\s+", " ", parrafo).strip()
        if not parrafo:
            continue
        # Párrafos más largos que el trozo se parten primero por espacios
        while len(parrafo) > tamanio:
            corte = parrafo.rfind(" ", 0, tamanio)
            if corte < tamanio // 2:
                corte = tamanio
            trozos.append(parrafo[:corte].strip())
            parrafo = parrafo[corte:].strip()
        if not parrafo:
            continue
        if actual and len(actual) + len(parrafo) + 2 > tamanio:
            trozos.append(actual)
            actual = parrafo
        else:
            actual = f"{actual}\n\n{parrafo}" if actual else parrafo
    if actual:
        trozos.append(actual)
    return trozos

_STOPWORDS_RAG = {
    "cual", "cuales", "cuál", "cuáles", "como", "cómo", "donde", "dónde",
    "cuando", "cuándo", "quien", "quienes", "quién", "quiénes", "sobre",
    "entre", "para", "porque", "desde", "hasta", "puede", "pueden", "hoy",
    "partido", "partidos", "equipo", "equipos", "jugaron", "jugadores",
}

def buscar_en_pdfs(query: str) -> str:
    """Busca los fragmentos más relevantes de los documentos locales para la
    consulta: puntúa cada trozo según cuántas palabras clave repite (con tope por
    palabra, para que una repetida como 'copa' no domine) y cuántas distintas
    aparecen, y devuelve los 3 mejores."""
    claves = [c for c in (_normalizar_nombre(p) for p in query.split())
              if len(c) > 3 and c not in _STOPWORDS_RAG]
    if not claves:
        return ""
    candidatos = []
    for archivo, pagina, texto in _documentos_cargados():
        for trozo in _trocear(texto):
            normalizado = _normalizar_nombre(trozo)
            conteos = [(clave, normalizado.count(clave)) for clave in claves]
            presentes = [n for _, n in conteos if n]
            if not presentes:
                continue
            puntaje = sum(min(n, 3) for n in presentes) + 2 * len(presentes)
            candidatos.append((puntaje, archivo, pagina, trozo))
    if not candidatos:
        return ""
    candidatos.sort(key=lambda c: (-c[0], len(c[3])))
    contexto = ""
    for _, archivo, pagina, trozo in candidatos[:3]:
        contexto += f"\n[Fuente: {archivo} - Pág {pagina}]:\n{trozo[:1400]}\n"
    return contexto

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
        "Podés preguntarme por partidos de hoy (Champions, ligas europeas, selecciones o "
        "fútbol argentino), tablas de posiciones, estadísticas, reglamentos o enviarme "
        "imágenes o notas de voz. "
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


def armar_entrada(historial: list[tuple[str, str]], mensaje_nuevo: str,
                  contenido_extra: list[dict] | None = None) -> list[dict]:
    """Convierte el historial guardado en la lista de pasos (steps) que espera
    la Interactions API y agrega al final el mensaje nuevo del usuario.

    Cada mensaje del usuario es un paso 'user_input' y cada respuesta del
    modelo un paso 'model_output', tal como espera la API.

    `contenido_extra` permite anteponer bloques no de texto (imagen, audio)
    al texto del paso final, para que el modelo reciba ambos juntos.
    """
    tipo_de_paso = {"user": "user_input", "model": "model_output"}
    pasos = [
        {"type": tipo_de_paso[rol], "content": [{"type": "text", "text": texto}]}
        for rol, texto in historial
    ]
    contenido_final = list(contenido_extra or []) + [
        {"type": "text", "text": mensaje_nuevo}
    ]
    pasos.append(
        {"type": "user_input", "content": contenido_final}
    )
    return pasos

async def generar_respuesta(chat_id: int, user_text: str) -> str:
    """Arma el contexto (historial, partidos, RAG y estadísticas), consulta a
    Gemini y guarda la interacción en SQLite. Devuelve la respuesta del modelo.

    Es el camino común de las consultas: lo usan tanto los mensajes de texto
    como los de audio (que primero se transcriben).
    """
    # 1. Recuperar el historial previo de SQLite (ventana acotada a MAX_HISTORY)
    historial = get_chat_history(chat_id)

    try:
        # 2. Tabla de posiciones (scrapper de Promiedos). Se resuelve antes que
        #    los partidos: si el usuario pregunta por la tabla, no hace falta
        #    traer también los partidos del día.
        info_tabla = ""
        palabras_tabla = [
            "tabla de posicion", "tabla de posiciones", "posiciones", "posicion",
            "tabla", "clasificatoria", "ranking", "promedio", "descenso",
            "relegacion", "relegación", "cuantos puntos", "cuántos puntos",
        ]
        if any(p in user_text.lower() for p in palabras_tabla):
            info_tabla = obtener_tabla_posiciones(user_text)
            if info_tabla:
                info_tabla = f"\n{info_tabla}\n"

        # 3. Consulta inteligente de partidos (Incluye Champions League, Promiedos y ESPN)
        info_partidos = ""
        palabras_clave_partidos = [
            "partido", "partidos", "juega", "hoy", "resultado", "resultados", "fecha",
            "tabla", "equipo", "equipos", "vs", "champions", "river", "boca",
            "real madrid", "barcelona", "laliga", "la liga", "premier", "ligue one",
            "ligue 1", "bundesliga", "serie a", "liga", "argentina", "argentino",
            "jornada", "ultim", "últim", "pasad", "recient", "ayer", "fixture",
            "calendario", "ganaron", "gano", "ganó", "clasico", "clásico",
            "seleccion", "selección", "selecciones", "scaloneta", "mundial",
            "copa america", "eurocopa", "eliminatoria", "eliminatorias",
            "amistoso", "amistosos", "nations league", "liga de naciones",
        ]
        if any(palabra in user_text.lower() for palabra in palabras_clave_partidos) and not info_tabla:
            datos_hibridos = obtener_partidos_inteligente(user_text)
            if datos_hibridos:
                info_partidos = f"\n{datos_hibridos}\n"

        # 4. RAG: buscar en los documentos locales (reglamentos, historias y formatos)
        info_rag = ""
        palabras_rag = [
            "reglamento", "reglament", "articulo", "artículo", "ley", "historia",
            "fundacion", "fundación", "fundó", "fundad", "formato", "reglas",
            "normas", "torneo", "campeonato", "sancion", "sanción",
            "como se juega", "participantes",
        ]
        if any(p in user_text.lower() for p in palabras_rag):
            info_rag = buscar_en_pdfs(user_text)
            if info_rag:
                info_rag = f"\n[Información extraída de documentos oficiales/reglamentos]:\n{info_rag}\n"

        # 5. Estadísticas locales (racha del equipo, guardadas en SQLite)
        info_stats = ""
        palabras_stats = [
            "racha", "estadistica", "estadística", "estadístic", "estadistic",
            "rendimiento", "ultimos partidos", "últimos partidos", "como viene",
            "en que forma", "en qué forma", "esta en forma", "está en forma",
        ]
        if any(p in user_text.lower() for p in palabras_stats):
            equipo_buscado = _extraer_equipo(user_text)
            if equipo_buscado:
                info_stats = f"\n{analizar_racha_equipo(equipo_buscado)}\n"

        # 6. Contexto temporal exacto en español
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        now = datetime.now()
        ahora_formateada = f"{dias[now.weekday()]} {now.day} de {meses[now.month]} de {now.year} a las {now.strftime('%H:%M')}"

        # 7. Construir el prompt inyectando el contexto temporal y los datos reales
        prompt_con_contexto = (
            f"[Contexto del sistema: Hoy es estrictamente {ahora_formateada}]. "
            f"{info_tabla}"
            f"{info_partidos}"
            f"{info_rag}"
            f"{info_stats}"
            f"Mensaje del usuario: {user_text}"
        )

        # 8. Consultar a Gemini con la Interactions API (sin estado, store=False).
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

        # 9. Guardar la interacción en SQLite (usuario + respuesta, en una sola transacción)
        save_interaction(chat_id, user_text, bot_reply)
        return bot_reply

    except Exception as e:
        logger.error(f"Error al procesar el mensaje con Gemini: {e}")
        raise

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
        bot_reply = await generar_respuesta(chat_id, user_text)

        # 9. Enviar la respuesta en Telegram
        await update.message.reply_text(bot_reply)

    except Exception as e:
        logger.error(f"Error al procesar el mensaje con Gemini: {e}")
        await update.message.reply_text(
            "⚠️ Tuve un problema técnico procesando tu mensaje. Intentá de nuevo en un momento."
        )

def _mime_de_audio(multimedia) -> str:
    """MIME type de un audio de Telegram. Telegram lo informa casi siempre;
    si falta, se infiere del nombre del archivo (default: OGG, formato de las notas de voz)."""
    mime = getattr(multimedia, "mime_type", None)
    if mime:
        return mime
    nombre = (getattr(multimedia, "file_name", None) or "").lower()
    for extension, tipo in (
        (".mp3", "audio/mpeg"),
        (".m4a", "audio/mp4"),
        (".aac", "audio/aac"),
        (".wav", "audio/wav"),
    ):
        if nombre.endswith(extension):
            return tipo
    return "audio/ogg"

async def transcribir_audio(audio_bytes: bytes, mime_type: str) -> str:
    """Transcribe un clip de audio con Gemini (Interactions API, bloque 'audio')."""
    paso_usuario = {
        "type": "user_input",
        "content": [
            {
                "type": "audio",
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "mime_type": mime_type,
            },
            {
                "type": "text",
                "text": "Transcribí fielmente este mensaje de voz, respetando el idioma "
                        "en el que fue grabado. Devolvé únicamente la transcripción, "
                        "sin agregar comentarios ni formato.",
            },
        ],
    }
    interaccion = await client.aio.interactions.create(
        model=GEMINI_MODEL,
        generation_config={"temperature": 0.2},
        input=[paso_usuario],
        store=False,
    )
    return (interaccion.output_text or "").strip()

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recibe notas de voz o archivos de audio, los transcribe con Gemini y
    responde por el mismo camino que un mensaje de texto (partidos, RAG, etc.)."""
    chat_id = update.effective_chat.id
    usuario = update.effective_user
    register_user(chat_id, usuario.id, usuario.username, usuario.first_name)

    multimedia = update.message.voice or update.message.audio or update.message.document
    if not multimedia:
        await update.message.reply_text("⚠️ No pude leer el audio que enviaste.")
        return

    try:
        archivo = await multimedia.get_file()
        audio_bytes = bytes(await archivo.download_as_bytearray())
        mime_type = _mime_de_audio(multimedia)

        logger.info(
            f"🎙️ Audio recibido | Usuario: {usuario.first_name} "
            f"(id: {usuario.id}, chat: {chat_id}) | {mime_type}, {len(audio_bytes)} bytes"
        )

        transcripcion = await transcribir_audio(audio_bytes, mime_type)

        # Los audios pueden traer un pie escrito: se suma a la consulta
        if update.message.caption:
            transcripcion = f"{transcripcion}\n[Pie escrito junto al audio]: {update.message.caption}"

        if not transcripcion:
            await update.message.reply_text(
                "🎙️ Escuché el audio pero no pude entenderlo. ¿Podés repetirlo o escribirme?"
            )
            return

        logger.info(f"📝 Transcripción: {transcripcion}")
        user_text = f"[Nota de voz]: {transcripcion}"

        bot_reply = await generar_respuesta(chat_id, user_text)
        await update.message.reply_text(bot_reply)

    except Exception as e:
        logger.error(f"Error procesando audio: {e}")
        await update.message.reply_text("⚠️ Tuve un problema procesando el audio. Intentá de nuevo en un momento.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analiza una imagen enviada por el usuario (foto o documento con imagen)
    con Gemini, incluyendo el historial de la conversación."""
    chat_id = update.effective_chat.id
    usuario = update.effective_user
    register_user(chat_id, usuario.id, usuario.username, usuario.first_name)

    # Telegram manda las fotos en Message.photo; los archivos (p. ej. un PNG
    # de pantalla) llegan como Message.document
    multimedia = update.message.photo[-1] if update.message.photo else update.message.document
    if not multimedia:
        await update.message.reply_text("⚠️ No pude leer la imagen que enviaste.")
        return

    try:
        photo_file = await multimedia.get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        mime_type = f"image/{(image.format or 'JPEG').lower()}"

        user_caption = update.message.caption or "Contame qué muestra esta imagen y explicala."

        logger.info(
            f"🖼️ Imagen recibida | Usuario: {usuario.first_name} "
            f"(id: {usuario.id}, chat: {chat_id}) | {mime_type}, {len(photo_bytes)} bytes "
            f"| Pie: {user_caption}"
        )

        # Historial + imagen en base64 dentro del paso de entrada (mismo camino que el texto)
        historial = get_chat_history(chat_id)
        bloque_imagen = {
            "type": "image",
            "data": base64.b64encode(bytes(photo_bytes)).decode("ascii"),
            "mime_type": mime_type,
        }

        interaccion = await client.aio.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=SYSTEM_INSTRUCTION,
            generation_config=generation_config,
            input=armar_entrada(historial, user_caption, contenido_extra=[bloque_imagen]),
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
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_audio))

    print("BotTin v5_2 (Gemini + SQLite) con Champions League, Promiedos, RAG y Estadísticas activo... "
          "Presioná Ctrl+C para detenerlo.")
    app.run_polling()

if __name__ == "__main__":
    main()