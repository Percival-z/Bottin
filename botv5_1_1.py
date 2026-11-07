import logging
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from PIL import Image
import io
from pypdf import PdfReader

# Cargar las variables de entorno desde el archivo .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

# Configurar la API de Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Configuración del modelo con System Prompt ajustado y restricción temporal estricta (Año 2026)
generation_config = {
    "temperature": 0.7,
}

system_instruction = (
    "Sos BotTin, un asistente virtual experto, apasionado y dinámico especializado en fútbol. "
    "REGLA TEMPORAL ABSOLUTA: Te encuentras estrictamente en el año 2026. Bajo ninguna circunstancia debes hablar de partidos, "
    "torneos o temporadas pasadas (como 2024 o 2025) cuando el usuario pregunte por la actualidad o el día de hoy. "
    "Si los datos provistos en el contexto tienen fecha de hoy, cíñete estrictamente a ellos. "
    "REGLA ESTRICTA: Nunca arranques tus respuestas saludando con '¡Hola!' o fórmulas de cortesía repetitivas a menos que el usuario te salude explícitamente en su mensaje actual. "
    "Andá siempre directo al grano con lo que te piden, sin saludos automáticos ni introducciones de relleno."
)

model = genai.GenerativeModel(
    model_name="gemini-3.8-flash",
    generation_config=generation_config,
    system_instruction=system_instruction
)

# --- CONFIGURACIÓN DE SQLITE ---
DB_NAME = "bottin_history.db"

def init_db():
    """Crea la tabla de historial si no existe."""
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
    conn.commit()
    conn.close()

def save_message(chat_id: int, role: str, content: str):
    """Guarda un mensaje en la base de datos SQLite."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (chat_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

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

def obtener_partidos_api() -> str:
    """Consulta los partidos del día en API-Football para Champions League y grandes ligas europeas."""
    if not FOOTBALL_API_KEY:
        return ""
    
    hoy = datetime.now().strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    querystring = {"date": hoy}
    headers = {'x-apisports-key': FOOTBALL_API_KEY}
    
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        data = response.json()
        matches = data.get("response", [])
        
        if not matches:
            return ""
            
        # IDs permitidos: Champions League (2), Premier League (39), La Liga (140), Serie A (135), Bundesliga (78), Ligue 1 (61)
        ligas_permitidas = [2, 39, 140, 135, 78, 61] 
        resumen = []
        
        for m in matches:
            if m["league"]["id"] in ligas_permitidas:
                liga = m["league"]["name"]
                local = m["teams"]["home"]["name"]
                visita = m["teams"]["away"]["name"]
                goles_home = m["goals"]["home"]
                goles_away = m["goals"]["away"]
                estado = m["fixture"]["status"]["short"]
                
                if estado in ["FT", "AET", "PEN"]:
                    marcador = f"{goles_home} - {goles_away} (Finalizado)"
                elif estado in ["1H", "2H", "HT", "ET"]:
                    marcador = f"{goles_home} - {goles_away} ¡En juego!"
                else:
                    hora_utc = m["fixture"]["date"].split("T")[1][:5]
                    marcador = f"A las {hora_utc} UTC"
                    
                resumen.append(f"- [{liga}] {local} vs {visita} -> {marcador}")
                
        return "\n".join(resumen[:15]) if resumen else ""
    except Exception as e:
        print(f"❌ Error al consultar API-Football: {e}")
        return ""

def obtener_partidos_promiedos() -> str:
    """Extrae los partidos y resultados del día haciendo web scraping a Promiedos."""
    url = "https://www.promiedos.com.ar/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return ""
            
        soup = BeautifulSoup(response.text, 'html.parser')
        resumen = []
        
        partidos = soup.find_all('tr', class_='promrow') or soup.find_all('div', class_='partido')
        
        if not partidos:
            tablas = soup.find_all('table')
            for tabla in tablas:
                for fila in tabla.find_all('tr'):
                    texto = fila.get_text(separator=" ", strip=True)
                    if texto and ("-" in texto or ":" in texto) and len(texto) < 80:
                        resumen.append(f"- {texto}")
        else:
            for p in partidos:
                resumen.append(f"- {p.get_text(separator=' ', strip=True)}")
                
        return "\n".join(resumen[:15]) if resumen else ""
    except Exception as e:
        print(f"❌ Error haciendo scraping en Promiedos: {e}")
        return ""

def obtener_partidos_inteligente(user_text: str) -> str:
    """Enrutador inteligente con inyección explícita de la fecha actual y Champions League."""
    hoy_str = datetime.now().strftime("%Y-%m-%d")
    
    keywords_argentina = ["argentina", "liga profesional", "copa argentina", "river", "boca", "racing", "independiente", "san lorenzo", "godoy cruz", "talleres", "ascenso", "b nacional"]
    es_futbol_argentino = any(k in user_text.lower() for k in keywords_argentina)
    
    datos_crudos = ""
    fuente = ""
    
    if es_futbol_argentino or not FOOTBALL_API_KEY:
        datos_crudos = obtener_partidos_promiedos()
        fuente = "Promiedos"
    else:
        datos_crudos = obtener_partidos_api()
        fuente = "API-Football (Champions y Ligas Top)"
        if not datos_crudos:
            datos_crudos = obtener_partidos_promiedos()
            fuente = "Promiedos (Respaldo)"
            
    if not datos_crudos:
        return f"[Aviso temporal: Hoy es {hoy_str} y no se encontraron partidos registrados en las fuentes para esta fecha]."
        
    return (
        f"[INFORMACIÓN OBLIGATORIA EN TIEMPO REAL]\n"
        f"- Fecha actual exacta del sistema: {hoy_str}\n"
        f"- Fuente de datos: {fuente}\n"
        f"- Partidos obtenidos:\n{datos_crudos}"
    )

# --- RAG CON PDFS ---

def buscar_en_pdfs(query: str) -> str:
    """Busca fragmentos relevantes dentro de los PDFs en la carpeta 'docs/'."""
    docs_dir = "docs"
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
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola, {nombre}! ⚽ Soy BotTin v5_1_1, tu asistente con Champions League, Promiedos, estadísticas y RAG."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 **BotTin v5_1_1**\n"
        "Podés preguntarme por partidos de hoy (Champions, ligas europeas o fútbol argentino), estadísticas, reglamentos o enviarme imágenes."
    )

def get_chat_history(chat_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC",
        (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    history = []
    for role, content in rows:
        history.append({
            "role": role,
            "parts": [content]
        })
    return history

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    
    try:
        history = get_chat_history(chat_id)
        
        # 1. Consulta inteligente de partidos (Incluye Champions League y Promiedos)
        info_partidos = ""
        palabras_clave_partidos = ["partido", "juega", "hoy", "resultado", "fecha", "tabla", "equipo", "vs", "partidos", "champions", "champions league", "river", "boca", "real madrid", "barcelona", "laliga", "premier league", "ligue one", "bundesliga", "serie A"]
        if any(palabra in user_text.lower() for palabra in palabras_clave_partidos):
            datos_hibridos = obtener_partidos_inteligente(user_text)
            if datos_hibridos:
                info_partidos = f"\n{datos_hibridos}\n"

        # 2. RAG: Buscar en PDFs locales
        info_rag = ""
        if any(w in user_text.lower() for w in ["reglamento", "articulo", "ley", "historia", "fundacion", "torneo", "formato"]):
            info_rag = buscar_en_pdfs(user_text)
            if info_rag:
                info_rag = f"\n[Información extraída de documentos oficiales/reglamentos]:\n{info_rag}\n"

        # 3. Estadísticas locales
        info_stats = ""
        if any(w in user_text.lower() for w in ["racha", "estadistica", "rendimiento", "ultimos partidos"]):
            palabras = user_text.split()
            equipo_buscado = palabras[-1] if palabras else ""
            if equipo_buscado:
                info_stats = f"\n{analizar_racha_equipo(equipo_buscado)}\n"

        # 4. Contexto temporal exacto en español
        dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        meses = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
        now = datetime.now()
        ahora_formateada = f"{dias[now.weekday()]} {now.day} de {meses[now.month]} de {now.year} a las {now.strftime('%H:%M')}"
        
        prompt_con_contexto = (
            f"[Contexto del sistema: Hoy es estrictamente {ahora_formateada}]. "
            f"{info_partidos}"
            f"{info_rag}"
            f"{info_stats}"
            f"Mensaje del usuario: {user_text}"
        )
        
        chat = model.start_chat(history=history)
        response = chat.send_message(prompt_con_contexto)
        bot_reply = response.text
        
        save_message(chat_id, "user", user_text)
        save_message(chat_id, "model", bot_reply)
        
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")
        await update.message.reply_text("⚠️ Tuve un problema técnico procesando tu mensaje.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        
        user_caption = update.message.caption or "Analizá esta imagen de fútbol y dame un resumen detallado."
        
        chat = model.start_chat(history=[])
        response = chat.send_message([image, user_caption])
        bot_reply = response.text
        
        save_message(chat_id, "user", f"[Imagen enviada]: {user_caption}")
        save_message(chat_id, "model", bot_reply)
        
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error procesando imagen: {e}")
        await update.message.reply_text("⚠️ Tuve un problema procesando la imagen.")

def main() -> None:
    init_db()
    init_stats_db()
    
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise ValueError("Faltan credenciales en el archivo .env")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("BotTin v5_1_1 con Champions League, Promiedos, RAG y Estadísticas activo...")
    app.run_polling()

if __name__ == "__main__":
    main()