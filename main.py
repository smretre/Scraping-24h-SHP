import os
import re
import sqlite3
import threading
import time
import hashlib
import json
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from curl_cffi import requests as curl_requests

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

def generate_shopee_signature(app_id, secret, payload, timestamp):
    """Gera a assinatura HMAC-SHA256 para a API de Afiliados da Shopee"""
    factor = f"{app_id}{timestamp}{payload}{secret}"
    return hashlib.sha256(factor.encode('utf-8')).hexdigest()

def resolve_shopee_url(url):
    """Resolve URLs encurtadas da Shopee com timeout curto"""
    print(f"🔄 Expandindo URL curta: {url}", flush=True)
    try:
        resp = curl_requests.get(url, impersonate="chrome120", allow_redirects=True, timeout=5)
        print(f"🔗 URL Expandida: {resp.url}", flush=True)
        return resp.url
    except Exception as e:
        print(f"⚠️ Aviso ao expandir URL (usando original): {e}", flush=True)
        return url

def converter_para_afiliado(url):
    """Converte qualquer link da Shopee em link de afiliado oficial via API GraphQL"""
    if not SHOPEE_APP_ID or not SHOPEE_SECRET:
        return url

    timestamp = int(time.time())
    query_obj = {
        "query": "mutation($link: String!){generateShortLink(input:{originUrl:$link}){shortLink}}",
        "variables": {"link": url},
        "operationName": None
    }
    
    payload = json.dumps(query_obj)
    signature = generate_shopee_signature(SHOPEE_APP_ID, SHOPEE_SECRET, payload, timestamp)

    try:
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={signature}'
        }
        res = curl_requests.post("https://open-api.affiliate.shopee.com.br/graphql", data=payload, headers=headers, timeout=8)
        
        if res.status_code == 200:
            res_data = res.json()
            short_link = res_data.get("data", {}).get("generateShortLink", {}).get("shortLink")
            if short_link:
                return short_link
    except Exception as e:
        print(f"⚠️ Erro ao converter link para afiliado: {e}", flush=True)

    return url

def get_shopee_product_info(product_url):
    """Obtém dados completos do produto e gera o link de afiliado"""
    print("🚀 Iniciando busca de informações da Shopee...", flush=True)
    final_url = resolve_shopee_url(product_url)
    
    # 1. Gera o link de afiliado oficial imediatamente
    affiliate_link = converter_para_afiliado(final_url)

    # 2. Extrai ShopID e ItemID da URL
    match = (
        re.search(r'shopee\.com\.br/[^/]+/(\d+)/(\d+)', final_url) or 
        re.search(r'i\.(\d+)\.(\d+)', final_url) or 
        re.search(r'product/(\d+)/(\d+)', final_url) or 
        re.search(r'-i\.(\d+)\.(\d+)', final_url)
    )
    
    shop_id = int(match.group(1)) if match else None
    item_id = int(match.group(2)) if match else None

    title = None
    image_url = None
    price_str = None

    # 3. Consulta a API principal da Shopee para resgatar dados exatos do produto
    if shop_id and item_id:
        print(f"🔍 Buscando dados do item via API Shopee -> ShopID: {shop_id} | ItemID: {item_id}", flush=True)
        try:
            api_url = f"https://shopee.com.br/api/v2/item/get?itemid={item_id}&shopid={shop_id}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": f"https://shopee.com.br/product/{shop_id}/{item_id}"
            }
            resp = curl_requests.get(api_url, headers=headers, impersonate="chrome120", timeout=8)
            if resp.status_code == 200:
                item_data = resp.json().get("item", {})
                title = item_data.get("name")
                
                images = item_data.get("images", [])
                if images:
                    image_url = f"https://down-br.img.susercontent.com/file/{images[0]}"
                elif item_data.get("image"):
                    image_url = f"https://down-br.img.susercontent.com/file/{item_data.get('image')}"
                
                price_raw = item_data.get("price") or item_data.get("price_min")
                if price_raw:
                    price_val = float(price_raw) / 100000.0
                    price_str = f"R$ {price_val:.2f}".replace('.', ',')
        except Exception as e:
            print(f"⚠️ Erro na busca via API da Shopee: {e}", flush=True)

    # 4. Fallback por metatags HTML se a API falhar
    if not image_url or not title:
        print("🌐 Tentando Fallback via HTML Principal...", flush=True)
        try:
            web_link = f"https://shopee.com.br/product/{shop_id}/{item_id}" if shop_id and item_id else final_url
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = curl_requests.get(web_link, headers=headers, impersonate="chrome120", timeout=8)
            soup = BeautifulSoup(resp.text, "html.parser")
            
            og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
            og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
            
            if og_title and not title:
                title = og_title["content"].split(" | ")[0]
            if og_image and not image_url:
                image_url = og_image["content"]
        except Exception as e:
            print(f"⚠️ Erro no scraping HTML: {e}", flush=True)

    title = title or "Produto Shopee"
    price_str = price_str or "Confira no site"

    print(f"🎉 Processamento concluído! Título: {title[:25]}... | Preço: {price_str} | Imagem Encontrada: {image_url is not None}", flush=True)

    return {
        "title": title,
        "image": image_url,
        "price": price_str,
        "link": affiliate_link
    }

# --- GERADOR DE IMAGEM / CARD ---
def generate_card_image(image_url, price_str):
    """Gera o card sobrepondo a imagem do produto no template."""
    prod_img = None
    if image_url:
        try:
            response = curl_requests.get(image_url, impersonate="chrome120", timeout=10)
            if response.status_code == 200:
                prod_img = Image.open(BytesIO(response.content)).convert("RGBA")
        except Exception as e:
            print(f"⚠️ Erro ao carregar imagem do produto: {e}", flush=True)

    if not prod_img:
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
