import logging
import os
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
import google.generativeai as genai

# Cargar las variables de entorno desde el archivo .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configurar la API de Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Configuración del modelo con un System Prompt enfocado en fútbol
generation_config = {
    "temperature": 0.7,
}

system_instruction = (
    "Sos BotTin, un asistente virtual experto, apasionado y dinámico especializado en fútbol. "
    "Respondés de forma cercana, amigable y directa a los hinchas y usuarios, ayudándoles "
    "con datos, análisis y comentarios deportivos."
)

# Inicializar el modelo Gemini 1.5 Flash con sus instrucciones de sistema
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
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

# --- CONFIGURACIÓN DE LOGS ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start con un saludo personalizado."""
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola, {nombre}! ⚽ Soy **BotTin**, tu asistente de fútbol potenciado con Inteligencia Artificial. "
        f"Ya tengo memoria en SQLite y estoy listo para debatir de fútbol. Escribí /help para más info."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /help."""
    await update.message.reply_text(
        "🤖 **BotTin v3 (Gemini + SQLite)**\n"
        "Podés preguntarme sobre tácticas, historia del fútbol, opiniones de equipos o planteles. "
        "Guardo el historial de nuestra charla de forma local."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Procesa los mensajes de texto del usuario usando Gemini y guardando en SQLite."""
    chat_id = update.effective_chat.id
    user_text = update.message.text
    
    # 1. Guardar el mensaje del usuario en la base de datos
    save_message(chat_id, "user", user_text)
    
    try:
        # 2. Iniciar chat con Gemini (puedes enviar el mensaje directamente)
        chat = model.start_chat(history=[])
        response = chat.send_message(user_text)
        bot_reply = response.text
        
        # 3. Guardar la respuesta del modelo en la base de datos
        save_message(chat_id, "model", bot_reply)
        
        # 4. Enviar la respuesta al usuario en Telegram
        await update.message.reply_text(bot_reply)
        
    except Exception as e:
        logger.error(f"Error al conectar con Gemini: {e}")
        await update.message.reply_text(
            "⚠️ Tuve un problema técnico conectando con la IA de Gemini. Intentá de nuevo en un momento."
        )

def main() -> None:
    """Función principal para poner en marcha el bot."""
    # Inicializar base de datos
    init_db()

    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise ValueError("Faltan credenciales en el archivo .env (TELEGRAM_TOKEN o GEMINI_API_KEY).")

    # Construir la aplicación de Telegram
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Registrar handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Iniciar Polling
    print("BotTin v3 (Gemini + SQLite) está andando... Presioná Ctrl+C para detenerlo.")
    app.run_polling()

if __name__ == "__main__":
    main()