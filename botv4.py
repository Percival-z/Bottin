import logging
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import google.generativeai as genai
import requests
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

# Configuración del modelo con un System Prompt enfocado en fútbol
generation_config = {
    "temperature": 0.6,
}

system_instruction = (
    "Sos BotTin, un asistente virtual experto, apasionado y dinámico especializado en fútbol. "
    "Respondés de forma cercana, amigable y directa a los hinchas y usuarios, ayudándoles "
    "con datos, análisis y comentarios deportivos. Sin embargo no abrumes con informacion y limitate a responder con lo que te piden. No comentes nada ademas de eso"
)

# Inicializar el modelo Gemini con sus instrucciones de sistema
model = genai.GenerativeModel(
    model_name="gemini-3.1-flash-lite",
    generation_config=generation_config,
    system_instruction=system_instruction
)

# --- CONFIGURACIÓN DE SQLITE (HISTORIAL PERSISTENTE) ---
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
        f"- Diferencia de gol: {goles_favor - goles_contra}\n"
    )
    return informe

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HANDLERS Y FUNCIONES AUXILIARES ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start con un saludo personalizado."""
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola, {nombre}! ⚽ Soy BotTin, tu asistente de fútbol potenciado con Inteligencia Artificial. "
        f"Escribí /help para más info."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /help."""
    await update.message.reply_text(
        "🤖 **BotTin v4 (Gemini + SQLite + RAG + API)**\n"
        "Podés preguntarme sobre partidos de hoy, estadísticas, reglamentos o enviarme imágenes de formaciones."
    )

def get_chat_history(chat_id: int):
    """Recupera el historial de mensajes de un chat específico desde SQLite."""
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

def obtener_partidos_hoy() -> str:
    """Consulta los partidos del día en API-Football con logs de diagnóstico."""
    if not FOOTBALL_API_KEY:
        print("❌ ERROR: FOOTBALL_API_KEY no está configurada o está vacía en el .env")
        return ""
    
    hoy = datetime.now().strftime("%Y-%m-%d")
    url = "https://v3.football.api-sports.io/fixtures"
    querystring = {"date": hoy}
    headers = {
        'x-apisports-key': FOOTBALL_API_KEY
    }
    
    print(f"🔍 Consultando API-Football para la fecha: {hoy}...")
    
    try:
        response = requests.get(url, headers=headers, params=querystring, timeout=10)
        print(f"📡 Código de estado HTTP de la API: {response.status_code}")
        
        data = response.json()
        
        if "errors" in data and data["errors"]:
            print(f"⚠️ Errores devueltos por la API: {data['errors']}")
            
        matches = data.get("response", [])
        print(f"⚽ Total de partidos devueltos por la API para hoy: {len(matches)}")
        
        if not matches:
            return "No hay partidos programados para hoy según la API."
            
        ligas_permitidas = [128, 39, 140, 135, 78, 61]
        resumen = []
        
        for m in matches:
            league_id = m["league"]["id"]
            if league_id in ligas_permitidas:
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
                
        if not resumen:
            print("ℹ️ Hubo partidos hoy, pero ninguno coincidió con las 5 grandes ligas ni la liga argentina.")
            return "Hay partidos hoy, pero no en las ligas configuradas."
            
        print(f"✅ Se filtraron {len(resumen)} partidos de las ligas de interés.")
        return "\n".join(resumen[:12])
        
    except Exception as e:
        print(f"❌ Excepción crítica al conectar con API-Football: {e}")
        return "No se pudo consultar el fixture en este momento."

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    
    try:
        history = get_chat_history(chat_id)
        
        # 1. API: Consultar partidos en tiempo real si menciona palabras clave
        info_partidos = ""
        palabras_clave_api = ["partido", "juega", "hoy", "resultado", "fecha", "tabla", "equipo", "vs", "partidos", "river", "boca"]
        if any(palabra in user_text.lower() for palabra in palabras_clave_api):
            datos_api = obtener_partidos_hoy()
            if datos_api:
                info_partidos = f"\n[Datos oficiales de partidos para hoy]:\n{datos_api}\n"

        # 2. RAG: Buscar en PDFs si pregunta por reglamentos o historia
        info_rag = ""
        if any(w in user_text.lower() for w in ["reglamento", "articulo", "ley", "historia", "fundacion", "torneo", "formato"]):
            info_rag = buscar_en_pdfs(user_text)
            if info_rag:
                info_rag = f"\n[Información extraída de documentos oficiales/reglamentos]:\n{info_rag}\n"

        # 3. Análisis de Estadísticas si nombra a un equipo pidiendo racha o rendimiento
        info_stats = ""
        if any(w in user_text.lower() for w in ["racha", "estadistica", "rendimiento", "ultimos partidos"]):
            palabras = user_text.split()
            equipo_buscado = palabras[-1] if palabras else ""
            if equipo_buscado:
                info_stats = f"\n{analizar_racha_equipo(equipo_buscado)}\n"

        # 4. Contexto temporal en español
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
    """Maneja imágenes enviadas por el usuario (formaciones, tablas, etc.) y las analiza con Gemini."""
    chat_id = update.effective_chat.id
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        image = Image.open(io.BytesIO(photo_bytes))
        
        user_caption = update.message.caption or "Analizá esta imagen de fútbol (formación, tabla o competición) y dame un resumen detallado."
        
        chat = model.start_chat(history=[])
        response = chat.send_message([image, user_caption])
        bot_reply = response.text
        
        save_message(chat_id, "user", f"[Imagen enviada]: {user_caption}")
        save_message(chat_id, "model", bot_reply)
        
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error procesando imagen: {e}")
        await update.message.reply_text("⚠️ Tuve un problema procesando la imagen. Intentá enviarla nuevamente.")

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

    print("BotTin v4 con RAG, Estadísticas, API en tiempo real y Multimodalidad funcionando...")
    app.run_polling()

if __name__ == "__main__":
    main()