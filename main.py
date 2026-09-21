import os
import re
import time
import hashlib
import json
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw
from io import BytesIO
from telegram import Update, ChatMember
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    ConversationHandler, filters, ContextTypes
)
from curl_cffi import requests as curl_requests

# --- VARIÁVEIS DE AMBIENTE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Instância Flask de nível superior exigida pela Vercel
app = Flask(__name__)

# Estados da Conversa Passo a Passo
ASK_IMAGE = 1
ASK_TITLE = 2
ASK_CHANNEL = 3

# --- INTEGRAÇÃO COM A API DA SHOPEE ---
def generate_shopee_signature(app_id, secret, payload, timestamp):
    factor = f"{app_id}{timestamp}{payload}{secret}"
    return hashlib.sha256(factor.encode('utf-8')).hexdigest()

def resolve_shopee_url(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = curl_requests.get(url, headers=headers, impersonate="safari15_6", allow_redirects=True, timeout=10)
        return resp.url
    except Exception as e:
        print(f"⚠️ Erro ao resolver URL: {e}")
        return url

def get_shopee_product_info(product_url):
    final_url = resolve_shopee_url(product_url)
    short_link = None
    title = None
    image_url = None
    price_str = None

    if SHOPEE_APP_ID and SHOPEE_SECRET:
        try:
            timestamp = int(time.time())
            
            # 1. Gerar Link de Afiliado Curto
            mutation = 'mutation GenerateLink($originUrl: String!) { generateShortLink(input: { originUrl:$originUrl }) { shortLink } }'
            payload_link = json.dumps({"query": mutation, "variables": {"originUrl": final_url}})
            sig_link = generate_shopee_signature(SHOPEE_APP_ID, SHOPEE_SECRET, payload_link, timestamp)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"SHA256 Credential={SHOPEE_APP_ID}, Timestamp={timestamp}, Signature={sig_link}"
            }
            
            resp_link = curl_requests.post("https://open-api.affiliate.shopee.com.br/graphql", data=payload_link, headers=headers, timeout=8)
            if resp_link.status_code == 200:
                data_link = resp_link.json().get("data", {}).get("generateShortLink", {})
                short_link = data_link.get("shortLink")

            # 2. Obter Detalhes do Produto via API GraphQL
            slug_match = re.search(r'shopee\.com\.br/([^/?#]+)', final_url)
            if slug_match:
                raw_slug = slug_match.group(1)
                keyword = re.sub(r'-i\.\d+\.\d+$', '', raw_slug).replace('-', ' ')
                if len(keyword) > 2:
                    query_prod = f'query {{ productOfferV2(keyword: "{keyword}", limit: 1) {{ nodes {{ productName imageUrl price }} }} }}'
                    payload_prod = json.dumps({"query": query_prod})
                    sig_prod = generate_shopee_signature(SHOPEE_APP_ID, SHOPEE_SECRET, payload_prod, timestamp)
                    
                    resp_prod = curl_requests.post("https://open-api.affiliate.shopee.com.br/graphql", data=payload_prod, headers=headers, timeout=8)
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

    return {
        "title": title,
        "image": image_url,
        "price": price_str,
        "link": short_link or final_url
    }

# --- GERADOR DE CARD / IMAGEM ---
def generate_card_image(image_source, price_str):
    prod_img = None
    if image_source:
        try:
            if isinstance(image_source, bytes):
                prod_img = Image.open(BytesIO(image_source)).convert("RGBA")
            else:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
                response = curl_requests.get(image_source, headers=headers, impersonate="chrome120", timeout=10)
                if response.status_code == 200:
                    prod_img = Image.open(BytesIO(response.content)).convert("RGBA")
        except Exception as e:
            print(f"⚠️ Erro ao carregar imagem: {e}")

    canvas_width, canvas_height = 800, 1000
    card = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(card)

    # Topo Amarelo
    draw.rectangle([(0, 0), (canvas_width, 120)], fill="#FFCC00")
    draw.text((40, 35), "🔥 CORRE! OFERTA IMPERDÍVEL", fill="#000000")

    if prod_img:
        prod_img.thumbnail((700, 600))
        x_pos = (canvas_width - prod_img.width) // 2
        y_pos = 150 + (600 - prod_img.height) // 2
        card.paste(prod_img, (x_pos, y_pos), prod_img if prod_img.mode == 'RGBA' else None)
    else:
        draw.rectangle([(100, 200), (700, 700)], fill="#FFF0EE")
        draw.text((220, 430), "📦 PRODUTO SHOPEE", fill="#EE4D2D")

    # Rodapé Laranja
    draw.rectangle([(0, 820), (canvas_width, canvas_height)], fill="#EE4D2D")
    draw.text((40, 850), f"Por: {price_str or 'Imperdível'}", fill="#FFFFFF")

    output_stream = BytesIO()
    card.convert("RGB").save(output_stream, format="JPEG")
    output_stream.seek(0)
    return output_stream

# --- VERIFICAÇÃO DE ADMINISTRADOR ---
async def verify_bot_admin(bot, chat_id):
    try:
        bot_member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        if bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            return True
    except Exception as e:
        print(f"⚠️ Erro ao verificar ADM no chat {chat_id}: {e}")
    return False

# --- FLUXO DO BOT ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✔️ **Bem-vindo ao Bot de Afiliados Shopee!**\n\n"
        "Envie o link de um produto da Shopee para começarmos.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def process_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "shopee" not in text.lower():
        await update.message.reply_text("Por favor, envie um link válido da Shopee.")
        return ConversationHandler.END

    await update.message.reply_text("🔍 A analisar o link...")
    product_info = get_shopee_product_info(text)

    # Guarda no contexto
    context.user_data["link"] = product_info["link"]
    context.user_data["price"] = product_info["price"] or "Imperdível"
    context.user_data["title"] = product_info["title"]
    context.user_data["image_source"] = product_info["image"]

    # PASSO 1: Se não encontrou a imagem, pede a imagem isoladamente
    if not product_info["image"]:
        await update.message.reply_text(
            "⚠️ Não foi possível detetar a imagem automaticamente.\n\n"
            "📸 **Passo 1/3:** Envie a foto (JPG/PNG) ou o link da imagem do produto:"
        , parse_mode="Markdown")
        return ASK_IMAGE

    # PASSO 2: Se encontrou a imagem mas não o título, pede o título isoladamente
    if not product_info["title"]:
        await update.message.reply_text(
            "⚠️ Não foi possível detetar o título automaticamente.\n\n"
            "📝 **Passo 2/3:** Digite e envie o **título do produto**:"
        , parse_mode="Markdown")
        return ASK_TITLE

    # Se encontrou tudo, salta direto para o canal
    await update.message.reply_text(
        "📢 **Passo 3/3:** Envie o **ID ou Username do canal/grupo** de destino (ex: `@seu_canal` ou `-100123456789`):",
        parse_mode="Markdown"
    )
    return ASK_CHANNEL

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image_source = None
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        image_source = await photo_file.download_as_bytearray()
    elif update.message.text and update.message.text.startswith("http"):
        image_source = update.message.text

    if not image_source:
        await update.message.reply_text("⚠️ Por favor, envie uma foto válida (JPG/PNG) ou um link de imagem:")
        return ASK_IMAGE

    context.user_data["image_source"] = image_source

    # Se o título também estiver em falta, pede explicitamente o título e avança o estado para ASK_TITLE
    if not context.user_data.get("title"):
        await update.message.reply_text(
            "✅ Imagem guardada com sucesso!\n\n"
            "📝 **Passo 2/3:** Agora digite e envie o **título do produto**:"
        , parse_mode="Markdown")
        return ASK_TITLE

    # Se o título já existia, pede o canal de destino diretamente
    await update.message.reply_text(
        "✅ Imagem guardada com sucesso!\n\n"
        "📢 **Passo 3/3:** Envie o **ID ou Username do canal/grupo** de destino:",
        parse_mode="Markdown"
    )
    return ASK_CHANNEL

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    if not title:
        await update.message.reply_text("⚠️ Por favor, envie um título válido:")
        return ASK_TITLE

    context.user_data["title"] = title

    await update.message.reply_text(
        "✅ Título guardado com sucesso!\n\n"
        "📢 **Passo 3/3:** Envie o **ID ou Username do canal/grupo** de destino (ex: `@seu_canal` ou `-100123456789`):",
        parse_mode="Markdown"
    )
    return ASK_CHANNEL

async def receive_channel_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_channel = update.message.text.strip()
    
    await update.message.reply_text("🔍 A verificar permissões de Administrador...")
    is_admin = await verify_bot_admin(context.bot, target_channel)

    if not is_admin:
        await update.message.reply_text(
            f"❌ O bot **não é Administrador** no destino `{target_channel}`.\n\n"
            "Certifique-se de que adicionou o bot como ADM e tente enviar o ID/Username novamente:",
            parse_mode="Markdown"
        )
        return ASK_CHANNEL

    # Dados finais
    title = context.user_data.get("title", "🔥 Super Achadinho Shopee")
    price = context.user_data.get("price", "Imperdível")
    link = context.user_data.get("link")
    img_src = context.user_data.get("image_source")

    card_img = generate_card_image(img_src, price)
    caption = (
        f"🔥 *{title}*\n\n"
        f"💥 *Por: {price}*\n\n"
        f"🛒 *Link de Compra:* {link}"
    )

    try:
        await context.bot.send_photo(chat_id=target_channel, photo=card_img, caption=caption, parse_mode="Markdown")
        await update.message.reply_text(f"✅ Postagem criada e enviada com sucesso para `{target_channel}`!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao enviar postagem: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END

async def setup_telegram_app():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, process_link)],
        states={
            ASK_IMAGE: [
                MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), receive_image)
            ],
            ASK_TITLE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)
            ],
            ASK_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_and_send)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)
    
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
    return jsonify({"status": "ok"}}, 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
