import os
import re
import sqlite3
import threading
import time
import hashlib
import requests
import json
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

# --- VARIÁVEIS DE AMBIENTE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

# Credenciais da API de Afiliados da Shopee
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Configuração do Banco de Dados SQLite
DB_FILE = "bot_database.db"

# --- SERVIDOR WEB (WEBHOOK MERCADO PAGO) ---
app_web = Flask(__name__)

@app_web.route("/", methods=["GET"])
def home():
    return "Bot Online!", 200

@app_web.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data and data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        if payment_id and sdk:
            payment_info = sdk.payment().get(payment_id).get("response", {})
            if payment_info.get("status") == "approved":
                telegram_id = int(payment_info.get("external_reference"))
                activate_subscription(telegram_id)
                print(f"✅ Assinatura ativada para o usuário Telegram ID: {telegram_id}", flush=True)
    return jsonify({"status": "ok"}), 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_web.run(host="0.0.0.0", port=port)

# --- BANCO DE DADOS ---
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

# --- INTEGRAÇÃO COM A API DA SHOPEE ---

def resolve_shopee_url(url):
    """Resolve URLs encurtadas da Shopee criando uma sessão prévia para contornar bloqueios"""
    print(f"🔄 Expandindo URL curta: {url}", flush=True)
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8"
        })
        # Estabelece sessão na home para receber cookies válidos
        session.get("https://shopee.com.br/", timeout=5)
        
        # Segue o redirecionamento
        resp = session.get(url, allow_redirects=True, timeout=10)
        print(f"🔗 URL Expandida: {resp.url}", flush=True)
        return resp.url
    except Exception as e:
        print(f"⚠️ Erro ao expandir URL: {e}", flush=True)
        return url

def get_shopee_product_info(product_url):
    """Extrai informações do produto tratando todos os formatos de URL e chamadas da Shopee"""
    final_url = resolve_shopee_url(product_url)

    # 1. EXTRAÇÃO DE SHOPID E ITEMID
    match = (
        re.search(r'shopee\.com\.br/[^/]+/(\d+)/(\d+)', final_url) or 
        re.search(r'i\.(\d+)\.(\d+)', final_url) or 
        re.search(r'product/(\d+)/(\d+)', final_url) or 
        re.search(r'-i\.(\d+)\.(\d+)', final_url)
    )
    
    if match:
        shop_id, item_id = match.group(1), match.group(2)
        print(f"🎯 IDs Encontrados -> ShopID: {shop_id} | ItemID: {item_id}", flush=True)
        
        # Tentativa 1A: API Microservice V4
        try:
            api_url = f"https://shopee.com.br/api/v4/item/get?itemid={item_id}&shopid={shop_id}"
            headers_api = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
                "Accept": "application/json",
                "Accept-Language": "pt-BR,pt;q=0.9",
                "Referer": f"https://shopee.com.br/product/{shop_id}/{item_id}",
                "X-Shopee-Language": "pt-BR",
                "X-Requested-With": "XMLHttpRequest"
            }
            
            api_resp = requests.get(api_url, headers=headers_api, timeout=8)
            
            if api_resp.status_code == 200:
                data = api_resp.json().get("data") or {}
                title = data.get("name")
                image_id = data.get("image")
                
                price_raw = data.get("price") or data.get("price_min")
                price = f"R$ {price_raw / 100000:.2f}".replace('.', ',') if price_raw else None

                if title and image_id:
                    image_url = f"https://down-br.img.susercontent.com/file/{image_id}"
                    print(f"✅ Sucesso via API Shopee V4: {title[:30]}...", flush=True)
                    return {
                        "title": title,
                        "image": image_url,
                        "price": price or "Confira no site",
                        "link": final_url
                    }
        except Exception as e:
            print(f"⚠️ Erro na API V4: {e}", flush=True)

        # Tentativa 1B: API Open V2 (Fallback de API)
        try:
            api_url_v2 = f"https://shopee.com.br/api/v2/item/get?itemid={item_id}&shopid={shop_id}"
            api_resp2 = requests.get(api_url_v2, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if api_resp2.status_code == 200:
                item = api_resp2.json().get("item") or {}
                title = item.get("name")
                images = item.get("images")
                price_raw = item.get("price")
                
                if title and images:
                    image_url = f"https://down-br.img.susercontent.com/file/{images[0]}"
                    price = f"R$ {price_raw / 100000:.2f}".replace('.', ',') if price_raw else "Confira no site"
                    print(f"✅ Sucesso via API Shopee V2: {title[:30]}...", flush=True)
                    return {
                        "title": title,
                        "image": image_url,
                        "price": price,
                        "link": final_url
                    }
        except Exception as e:
            print(f"⚠️ Erro na API V2: {e}", flush=True)

    # 2. TENTATIVA SECUNDÁRIA VIA METADADOS HTML
    try:
        clean_url = f"https://shopee.com.br/product/{shop_id}/{item_id}" if match else final_url
        headers_html = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        resp = requests.get(clean_url, headers=headers_html, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
        og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        
        if og_title and og_image:
            title_text = og_title["content"].split(" | ")[0]
            print(f"✅ Sucesso via HTML Metatags: {title_text[:30]}...", flush=True)
            return {
                "title": title_text,
                "image": og_image["content"],
                "price": "Confira no site",
                "link": final_url
            }
    except Exception as e:
        print(f"❌ Erro no fallback HTML: {e}", flush=True)

    return None


# --- GERADOR DE IMAGEM / CARD ---
def generate_card_image(image_url, price_str):
    """Gera o card sobrepondo a imagem do produto no template."""
    try:
        response = requests.get(image_url, timeout=10)
        prod_img = Image.open(BytesIO(response.content)).convert("RGBA")
    except Exception:
        prod_img = Image.new("RGBA", (600, 600), (255, 255, 255))

    canvas_width, canvas_height = 800, 1000
    card = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(card)

    # Banner superior ("CORRE!")
    draw.rectangle([(0, 0), (canvas_width, 120)], fill="#FFCC00")
    draw.text((40, 35), "🔥 CORRE! OFERTA IMPERDÍVEL", fill="#000000")

    # Ajusta e centraliza a imagem do produto
    prod_img.thumbnail((700, 600))
    x_pos = (canvas_width - prod_img.width) // 2
    y_pos = 150 + (600 - prod_img.height) // 2
    card.paste(prod_img, (x_pos, y_pos), prod_img if prod_img.mode == 'RGBA' else None)

    # Banner inferior de Preço
    draw.rectangle([(0, 820), (canvas_width, canvas_height)], fill="#EE4D2D")
    draw.text((40, 850), f"Por: {price_str}", fill="#FFFFFF")

    output_stream = BytesIO()
    card.convert("RGB").save(output_stream, format="JPEG")
    output_stream.seek(0)
    return output_stream

# --- GERADOR DE PIX MERCADO PAGO ---
def generate_pix_payment(telegram_id):
    if not sdk:
        return None
    payment_data = {
        "transaction_amount": 19.90,
        "description": "Assinatura Mensal Bot Afiliados",
        "payment_method_id": "pix",
        "external_reference": str(telegram_id),
        "payer": {
            "email": f"user_{telegram_id}@bot.com",
            "first_name": "Usuario",
            "last_name": str(telegram_id)
        }
    }
    result = sdk.payment().create(payment_data)
    payment = result.get("response", {})
    return payment.get("point_of_interaction", {}).get("transaction_data", {}).get("qr_code")

# --- BOT TELEGRAM ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✔️ Envie o link de um produto da Shopee para criar o seu card promocional!")

async def process_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if "shopee" not in text.lower():
        await update.message.reply_text("Por enquanto este bot aceita apenas links da Shopee!")
        return

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
        msg = await update.message.reply_text("🔎 Buscando dados na Shopee e criando o card...")
        
        product_info = get_shopee_product_info(text)

        if not product_info:
            await msg.edit_text("❌ Não foi possível obter os dados desse link da Shopee. Verifique o link e tente novamente.")
            return

        if is_subscribed == 0:
            conn = sqlite3.connect(DB_FILE)
            conn.cursor().execute("UPDATE users SET free_credits = free_credits - 1 WHERE telegram_id = ?", (user_id,))
            conn.commit()
            conn.close()
            remaining_str = f"Resta(m) {free_credits - 1} teste(s) gratuito(s)."
        else:
            remaining_str = "Assinante (Acesso Ilimitado)."

        card_img = generate_card_image(product_info["image"], product_info["price"])

        caption = (
            f"🔥 *{product_info['title']}*\n\n"
            f"💥 *Por: {product_info['price']}*\n\n"
            f"🛒 *Link de Compra:* {product_info['link']}\n\n"
            f"⚡ _{remaining_str}_"
        )

        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=card_img, caption=caption, parse_mode="Markdown")
        await msg.delete()
    else:
        pix_code = generate_pix_payment(user_id)
        if pix_code:
            text_pix = (
                "🛑 *Os seus 3 testes gratuitos acabaram!*\n\n"
                "Para continuar a gerar cards ilimitados da Shopee, assine o plano mensal:\n\n"
                f"`{pix_code}`\n\n"
                "⚡ *Copie o código PIX acima e pague na app do seu banco. A liberação ocorre automaticamente em segundos!*"
            )
            await update.message.reply_text(text_pix, parse_mode="Markdown")
        else:
            await update.message.reply_text("Erro ao gerar o PIX. Tente novamente mais tarde.")

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_link))
    
    print("🤖 Bot iniciado com sucesso!", flush=True)
    telegram_app.run_polling()

if __name__ == "__main__":
    main()
