import logging
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Cargar las variables de entorno desde el archivo .env
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Configurar el registro (logging) para ver qué va pasando en la consola
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HANDLERS (Funciones que responden a los comandos/mensajes) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /start con un saludo personalizado."""
    nombre = update.effective_user.first_name
    await update.message.reply_text(
        f"¡Hola, {nombre}! ⚽ Soy **BotTin**, tu asistente personal de fútbol. "
        f"Estoy listo para ayudarte con partidos, resultados y datos deportivos. "
        f"Escribí /help para ver qué puedo hacer."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde al comando /help."""
    await update.message.reply_text(
        "Por ahora estoy en versión de esqueleto (v1). Próximamente te ayudaré con "
        "calendarios de partidos, estadísticas en tiempo real y mucho más."
    )

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Función de eco temporal para repetir los mensajes de texto comunes del usuario."""
    texto_usuario = update.message.text
    await update.message.reply_text(f"Dijiste: '{texto_usuario}' (¡Pronto tendré inteligencia artificial de Gemini aquí!)")


def main() -> None:
    """Función principal para poner en marcha el bot."""
    if not TOKEN:
        raise ValueError("No se encontró el TELEGRAM_TOKEN en las variables de entorno.")

    # Construir la aplicación del bot con el token
    app = Application.builder().token(TOKEN).build()

    # Registrar los handlers de comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # Registrar un handler para mensajes de texto comunes (excluyendo comandos)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Iniciar el bot mediante Polling (ideal para desarrollo local)
    print("BotTin está andando... Presioná Ctrl+C para detenerlo.")
    app.run_polling()

if __name__ == "__main__":
    main()
