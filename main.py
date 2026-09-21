import os
import re
import time
import hashlib
import json
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from curl_cffi import requests as curl_requests

# --- VARIÁVEIS DE AMBIENTE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

app = Flask(__name__)

# --- INTEGRAÇÃO COM A API DA SHOPEE ---
def generate_shopee_signature(app_id, secret, payload, timestamp):
    factor = f"{app_id}{timestamp}{payload}{secret}"
    return hashlib.sha256(factor.encode('utf-8')).hexdigest()

def resolve_shopee_url(url):
    try:
        resp = curl_requests.get(url, impersonate="chrome120", allow_redirects=True, timeout=5)
        return resp.url
    except Exception as e:
        return url

def get_shopee_product_info(product_url):
    final_url = resolve_shopee_url(product_url)
    short_link = None
    title = None
    image_url = None
    price_str = None

    # 1. Tenta obter o link de afiliado oficial via API GraphQL
    if SHOPEE_APP_ID and SHOPEE_SECRET:
        try:
            timestamp = int(time.time())
            mutation = 'mutation GenerateLink($originUrl: String!) { generateShortLink(input: { originUrl: $originUrl }) { shortLink } }'
            payload_link = json.dumps({"query": mutation, "variables": {"originUrl": final_url}})
            sig_link = generate_shopee_signature(SHOPEE_APP_ID, SHOPEE_SECRET, payload_link, timestamp)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={sig_link}"
            }
            
            resp_link = curl_requests.post("https://open-api.affiliate.shopee.com.br/graphql", data=payload_link, headers=headers, timeout=8)
            if resp_link.status_code == 200:
                short_link = resp_link.json().get("data", {}).get("generateShortLink", {}).get("shortLink")

            # Tenta apanhar dados via OfferV2
            slug_match = re.search(r'shopee\.com\.br/([^/?#]+)', final_url)
            if slug_match:
                raw_slug = slug_match.group(1)
                keyword = re.sub(r'-i\.\d+\.\d+$', '', raw_slug).replace('-', ' ')
                if len(keyword) > 3:
                    query_prod = "query { productOfferV2(keyword: \"" + keyword + "\", limit: 1) { nodes { productName imageUrl price } } }"
                    payload_prod = json.dumps({"query": query_prod, "variables": None, "operationName": None})
                    sig_prod = generate_shopee_signature(SHOPEE_APP_ID, SHOPEE_SECRET, payload_prod, timestamp)
                    
                    headers_prod = {
                        "Content-Type": "application/json",
                        "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={sig_prod}"
                    }
                    
                    resp_prod = curl_requests.post("https://open-api.affiliate.shopee.com.br/graphql", data=payload_prod, headers=headers_prod, timeout=8)
                    if resp_prod.status_code == 200:
                        nodes = resp_prod.json().get("data", {}).get("productOfferV2", {}).get("nodes", [])
                        if nodes:
                            node = nodes[0]
                            title = node.get("productName")
                            image_url = node.get("imageUrl")
                            p_val = node.get("price")
                            if p_val:
                                price_str = f"R$ {float(p_val):.2f}".replace('.', ',')
        except Exception as e:
            print(f"⚠️ Erro na API Shopee: {e}")

    # 2. Se a API não trouxe imagem ou título, faz scraping inteligente direto da página
    try:
        resp_page = curl_requests.get(final_url, impersonate="chrome120", timeout=8)
        if resp_page.status_code == 200:
            html_text = resp_page.text
            soup = BeautifulSoup(html_text, 'html.parser')
            
            # Título OpenGraph ou tag title
            if not title:
                og_title = soup.find("meta", property="og:title")
                if og_title and og_title.get("content"):
                    title = og_title["content"]
                elif soup.title:
                    title = soup.title.string

            # Imagem OpenGraph
            if not image_url:
                og_image = soup.find("meta", property="og:image")
                if og_image and og_image.get("content"):
                    image_url = og_image["content"]

            # Fallback extra: caça URLs de imagens da Susercontent no código bruto se ainda estiver vazio
            if not image_url:
                img_matches = re.findall(r'https?://[^\"\'\s]+\.susercontent\.com/file/[a-z0-9]+', html_text)
                if img_matches:
                    image_url = img_matches[0]
    except Exception as e:
        print(f"⚠️ Erro no scraping da página: {e}")

    return {
        "title": title or "🔥 Super Achadinho Shopee",
        "image": image_url,
        "price": price_str or "Imperdível",
        "link": short_link or final_url
    }

# --- GERADOR DE CARD / IMAGEM ---
def generate_card_image(image_url, price_str):
    prod_img = None
    if image_url:
        try:
            response = curl_requests.get(image_url, impersonate="chrome120", timeout=10)
            if response.status_code == 200:
                prod_img = Image.open(BytesIO(response.content)).convert("RGBA")
        except Exception:
            pass

    canvas_width, canvas_height = 800, 1000
    card = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(card)

    draw.rectangle([(0, 0), (canvas_width, 120)], fill="#FFCC00")
    draw.text((40, 35), "🔥 CORRE! OFERTA IMPERDÍVEL", fill="#000000")

    if prod_img:
        prod_img.thumbnail((700, 600))
        x_pos = (canvas_width - prod_img.width) // 2
        y_pos = 150 + (600 - prod_img.height) // 2
        card.paste(prod_img, (x_pos, y_pos), prod_img if prod_img.mode == 'RGBA' else None)
    else:
        draw.rectangle([(100, 200), (700, 700)], fill="#FFF0EE")
        draw.text((220, 430), "📦 VER PRODUTO NO APP", fill="#EE4D2D")

    draw.rectangle([(0, 820), (canvas_width, canvas_height)], fill="#EE4D2D")
    draw.text((40, 850), f"Por: {price_str}", fill="#FFFFFF")

    output_stream = BytesIO()
    card.convert("RGB").save(output_stream, format="JPEG")
    output_stream.seek(0)
    return output_stream

# --- CONFIGURAÇÃO DO BOT TELEGRAM (MODO WEBHOOK) ---
async def setup_telegram_app():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🚀 Envie o link de um produto da Shopee para criar o seu card promocional!")

    async def process_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if "shopee" not in text.lower():
            await update.message.reply_text("Por enquanto este bot aceita apenas links da Shopee!")
            return

        product_info = get_shopee_product_info(text)
        card_img = generate_card_image(product_info["image"], product_info["price"])

        caption = (
            f"🔥 *{product_info['title']}*\n\n"
            f"💥 *Por: {product_info['price']}*\n\n"
            f"🛒 *Link de Compra:* {product_info['link']}"
        )

        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=card_img, caption=caption, parse_mode="Markdown")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_link))
    await application.initialize()
    return application

# --- ROTAS WEBHOOK DO SERVIDOR ---
@app.route("/", methods=["GET"])
def home():
    return "Bot Serverless Online!", 200

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def process():
        application = await setup_telegram_app()
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.process_update(update)

    loop.run_until_complete(process())
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["POST"])
def mercado_pago_webhook():
    data = request.get_json()
    if data and data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        if payment_id and sdk:
            payment_info = sdk.payment().get(payment_id).get("response", {})
            if payment_info.get("status") == "approved":
                telegram_id = int(payment_info.get("external_reference"))
                print(f"✅ Pagamento aprovado para o ID: {telegram_id}")
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
