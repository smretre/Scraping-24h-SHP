import os
import sqlite3
import threading
from flask import Flask, request
import mercadopago
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Configuração do Banco de Dados SQLite
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            free_credits INTEGER DEFAULT 3,
            is_subscribed INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def activate_subscription(telegram_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_subscribed = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

def generate_pix_payment(telegram_id):
    """Gera cobrança PIX via Mercado Pago API"""
    if not sdk:
        return None, None
    
    payment_data = {
        "transaction_amount": 19.90,  # Valor da assinatura mensal
        "description": "Assinatura Mensal Bot Afiliados",
        "payment_method_id": "pix",
        "external_reference": str(telegram_id),  # Identifica o ID do usuário no callback
        "payer": {
            "email": f"user_{telegram_id}@bot.com",
            "first_name": "Usuario",
            "last_name": str(telegram_id)
        }
    }
    
    result = sdk.payment().create(payment_data)
    payment = result.get("response", {})
    
    pix_code = payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code")
    qr_code_base64 = payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code_base64")
    
    return pix_code, qr_code_base64

# --- WEBHOOK PARA RECEBER CONFIRMAÇÃO DE PAGAMENTO ---
app_web = Flask(__name__)

@app_web.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if data and data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        if payment_id and sdk:
            payment_info = sdk.payment().get(payment_id).get("response", {})
            if payment_info.get("status") == "approved":
                telegram_id = int(payment_info.get("external_reference"))
                activate_subscription(telegram_id)
                print(f"✅ Assinatura ativada para o usuário Telegram ID: {telegram_id}")
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_web.run(host="0.0.0.0", port=port)

# --- COMANDOS DO TELEGRAM BOT ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Envie o link de um produto para testar o gerador de cards!")

async def process_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT free_credits, is_subscribed FROM users WHERE telegram_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (telegram_id, free_credits, is_subscribed) VALUES (?, 3, 0)", (user_id,))
        conn.commit()
        free_credits, is_subscribed = 3, 0
    else:
        free_credits, is_subscribed = row
    
    conn.close()

    if is_subscribed == 1 or free_credits > 0:
        if is_subscribed == 0:
            conn = sqlite3.connect(DB_FILE)
            conn.cursor().execute("UPDATE users SET free_credits = free_credits - 1 WHERE telegram_id = ?", (user_id,))
            conn.commit()
            conn.close()
        
        await update.message.reply_text("✨ Gerando seu card promocional...")
        # Lógica de geração do card aqui
    else:
        pix_code, _ = generate_pix_payment(user_id)
        if pix_code:
            msg = (
                "🛑 **Seus 3 testes gratuitos acabaram!**\n\n"
                "Para liberar acesso ilimitado por 30 dias, pague via PIX abaixo:\n\n"
                f"`{pix_code}`\n\n"
                "⚡ *Copie o código acima e pague no seu aplicativo do banco. A liberação ocorre em poucos segundos!*"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text("Erro ao gerar chave PIX. Contate o suporte.")

def main():
    # Inicia o servidor Flask em uma thread separada para o Webhook
    threading.Thread(target=run_flask, daemon=True).start()

    # Inicia o Bot do Telegram
    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_link))

    print("Bot e Webhook iniciados...")
    telegram_app.run_polling()

if __name__ == "__main__":
    main()
