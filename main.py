import os
import re
import time
import hashlib
import json
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw
from io import BytesIO
from telegram import Update, ChatMember, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    ConversationHandler, CallbackQueryHandler, filters, ContextTypes
)
from curl_cffi import requests as curl_requests

# --- VARIÁVEIS DE AMBIENTE ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
SHOPEE_APP_ID = os.getenv("SHOPEE_APP_ID")
SHOPEE_SECRET = os.getenv("SHOPEE_SECRET")

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Flask criado apenas para manter a porta aberta exigida pelo Render (Health Check)
app = Flask(__name__)

# Estados da Conversa Passo a Passo
SELECTING_PLATFORM = 0
ML_ASK_LINK = 1
SHOPEE_ASK_LINK = 2
ASK_IMAGE = 3
ASK_TITLE = 4
ASK_OLD_PRICE = 5
ASK_PRICE = 6
ASK_CHANNEL = 7

# --- INTEGRAÇÃO COM MERCADO LIVRE (100% Automático via Scraping com suporte a meli.la e múltiplos seletores de preço) ---
def get_mercadolibre_product_info(product_url):
    title = None
    image_url = None
    price_str = None
    final_link = product_url

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        # allow_redirects=True permite seguir links encurtados como meli.la até o produto final
        resp = curl_requests.get(product_url, headers=headers, impersonate="chrome120", allow_redirects=True, timeout=10)
        final_link = resp.url
        
        if resp.status_code == 200:
            html = resp.text
            
            # 1. Extrai o Título pelas Meta Tags Open Graph
            match_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if match_title:
                title = match_title.group(1)

            # 2. Extrai a Imagem pelas Meta Tags Open Graph
            match_img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
            if match_img:
                image_url = match_img.group(1)

            # 3. Extrai o Preço (Tentativa A: Meta tag itemprop padrão)
            match_price = re.search(r'<meta itemprop="price" content="([0-9.]+)"', html)
            if match_price:
                p_val = float(match_price.group(1))
                price_str = f"R$ {p_val:.2f}".replace('.', ',')
            
            # Tentativa B: Busca por classes nativas de preço do Mercado Livre (ex: andes-money-amount__fraction)
            if not price_str:
                match_fraction = re.search(r'class="andes-money-amount__fraction"[^>]*>([0-9.]+)</span>', html)
                if match_fraction:
                    fraction_val = match_fraction.group(1).replace('.', '')
                    match_cents = re.search(r'class="andes-money-amount__cents"[^>]*>([0-9]+)</span>', html)
                    cents_val = match_cents.group(1) if match_cents else "00"
                    
                    p_val = float(f"{fraction_val}.{cents_val}")
                    price_str = f"R$ {p_val:.2f}".replace('.', ',')

            # Tentativa C: JSON-LD estruturado da página
            if not price_str:
                match_json_price = re.search(r'"price":\s*"?([0-9.]+)"?', html)
                if match_json_price:
                    p_val = float(match_json_price.group(1))
                    price_str = f"R$ {p_val:.2f}".replace('.', ',')

    except Exception as e:
        print(f"⚠️ Erro ao extrair dados do Mercado Livre: {e}")

    return {
        "title": title,
        "image": image_url,
        "price": price_str,
        "link": final_link
    }

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
def generate_card_image(image_source):
    prod_img = None
    if image_source:
        try:
            if isinstance(image_source, (bytes, bytearray)):
                prod_img = Image.open(BytesIO(bytes(image_source))).convert("RGBA")
            else:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}
                response = curl_requests.get(image_source, headers=headers, impersonate="chrome120", timeout=10)
                if response.status_code == 200:
                    prod_img = Image.open(BytesIO(response.content)).convert("RGBA")
        except Exception as e:
            print(f"⚠️ Erro ao carregar imagem: {e}")

    canvas_width, canvas_height = 900, 900
    card = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
    draw = ImageDraw.Draw(card)

    if prod_img:
        img_w, img_h = prod_img.size
        ratio = max(canvas_width / img_w, canvas_height / img_h)
        new_w = int(img_w * ratio)
        new_h = int(img_h * ratio)
        
        prod_img = prod_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        
        left = (new_w - canvas_width) // 2
        top = (new_h - canvas_height) // 2
        right = left + canvas_width
        bottom = top + canvas_height
        
        prod_img = prod_img.crop((left, top, right, bottom))
        card.paste(prod_img, (0, 0), prod_img if prod_img.mode == 'RGBA' else None)
    else:
        draw.rectangle([(0, 0), (canvas_width, canvas_height)], fill="#FFF0EE")
        draw.text((320, 440), "📦 PRODUTO OFERTA", fill="#EE4D2D")

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
    keyboard = [
        [InlineKeyboardButton("🟡 Mercado Livre (100% Automático)", callback_data="plat_ml")],
        [InlineKeyboardButton("🟠 Shopee (Modo Manual/Misto)", callback_data="plat_shopee")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "✔️ **Seja bem-vindo ao Bot de Afiliados Automatizado!**\n\n"
        "Por favor, escolha abaixo em qual plataforma deseja gerar o anúncio automático:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return SELECTING_PLATFORM

async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "plat_ml":
        context.user_data["platform"] = "mercadolivre"
        await query.message.reply_text(
            "🟡 **Mercado Livre selecionado!**\n\n"
            "Envie o link do produto do Mercado Livre (ou encurtado meli.la) para extrairmos tudo automaticamente:",
            parse_mode="Markdown"
        )
        return ML_ASK_LINK
    else:
        context.user_data["platform"] = "shopee"
        await query.message.reply_text(
            "🟠 **Shopee selecionado (Modo Manual/Misto)!**\n\n"
            "Envie o link do produto da Shopee:",
            parse_mode="Markdown"
        )
        return SHOPEE_ASK_LINK

# Fluxo Mercado Livre atualizado para aceitar meli.la
async def process_ml_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        await update.message.reply_text("⚠️ Por favor, envie um link válido.")
        return ML_ASK_LINK

    termos_aceitos = ["mercadolivre", "mercadopago", "meli.la", "mercadolivre.com", "mercadolivre.com.br"]
    eh_valido = any(termo in text.lower() for termo in termos_aceitos)

    if not eh_valido:
        await update.message.reply_text("⚠️ O bot não reconheceu este como um link válido do Mercado Livre. Tente enviar novamente:")
        return ML_ASK_LINK

    await update.message.reply_text("🔍 Extraindo informações do Mercado Livre automaticamente...")
    info = get_mercadolibre_product_info(text)

    context.user_data["title"] = info["title"] or "Produto Mercado Livre"
    context.user_data["image_source"] = info["image"]
    context.user_data["price"] = info["price"] or "R$ 0,00"
    context.user_data["old_price"] = "R$ 0,00"
    context.user_data["link"] = info["link"]

    if not info["image"]:
        await update.message.reply_text("⚠️ Não conseguimos puxar a foto automaticamente. Envie a foto do produto:", parse_mode="Markdown")
        return ASK_IMAGE

    await update.message.reply_text("📢 Envie o **ID ou Username do canal/grupo** de destino:", parse_mode="Markdown")
    return ASK_CHANNEL

# Fluxo Shopee
async def process_shopee_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or "shopee" not in text.lower():
        await update.message.reply_text("Por favor, envie um link válido da Shopee.")
        return SHOPEE_ASK_LINK

    await update.message.reply_text("🔍 Analisando link da Shopee...")
    product_info = get_shopee_product_info(text)

    context.user_data["link"] = product_info["link"]
    context.user_data["price"] = product_info["price"]
    context.user_data["title"] = product_info["title"]
    context.user_data["image_source"] = product_info["image"]

    if not product_info["image"]:
        await update.message.reply_text("📸 Não foi possível detectar a imagem. Envie a foto do produto:", parse_mode="Markdown")
        return ASK_IMAGE

    if not product_info["title"]:
        await update.message.reply_text("📝 Digite e envie o **título do produto**:", parse_mode="Markdown")
        return ASK_TITLE

    await update.message.reply_text("❌ Digite e envie o **Preço Antigo** (ex: `R$ 49,90`):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Por favor, envie uma foto válida:")
        return ASK_IMAGE

    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    context.user_data["image_source"] = image_bytes

    if context.user_data.get("platform") == "mercadolivre":
        await update.message.reply_text("📢 Envie o **ID ou Username do canal/grupo** de destino:", parse_mode="Markdown")
        return ASK_CHANNEL

    if not context.user_data.get("title"):
        await update.message.reply_text("📝 Agora digite e envie o **título do produto**:", parse_mode="Markdown")
        return ASK_TITLE
    
    await update.message.reply_text("❌ Agora digite e envie o **Preço Antigo** (ex: `R$ 49,90`):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    if not title:
        await update.message.reply_text("⚠️ Por favor, envie um título válido:")
        return ASK_TITLE

    context.user_data["title"] = title
    await update.message.reply_text("❌ Agora digite e envie o **Preço Antigo** (ex: `R$ 49,90`):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_old_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_price = update.message.text.strip()
    if not old_price:
        await update.message.reply_text("⚠️ Por favor, envie um preço antigo válido:")
        return ASK_OLD_PRICE

    context.user_data["old_price"] = old_price

    if not context.user_data.get("price"):
        await update.message.reply_text("💰 Agora digite e envie o **Preço Atual (Por)** (ex: `R$ 12,99`):", parse_mode="Markdown")
        return ASK_PRICE

    await update.message.reply_text("📢 Envie o **ID ou Username do canal/grupo** de destino:", parse_mode="Markdown")
    return ASK_CHANNEL

async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = update.message.text.strip()
    if not price:
        await update.message.reply_text("⚠️ Por favor, envie um preço atual válido:")
        return ASK_PRICE

    context.user_data["price"] = price
    await update.message.reply_text("📢 Envie o **ID ou Username do canal/grupo** de destino:", parse_mode="Markdown")
    return ASK_CHANNEL

async def receive_channel_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_channel = update.message.text.strip()
    
    await update.message.reply_text("🔍 Verificando permissões de Administrador...")
    is_admin = await verify_bot_admin(context.bot, target_channel)

    if not is_admin:
        await update.message.reply_text(
            f"❌ O bot **não é Administrador** no destino `{target_channel}`.\n\n"
            "Adicione o bot como ADM e tente enviar o ID/Username novamente:",
            parse_mode="Markdown"
        )
        return ASK_CHANNEL

    title = context.user_data.get("title", "🔥 Super Oferta")
    old_price = context.user_data.get("old_price", "R$ 0,00")
    price = context.user_data.get("price", "R$ 0,00")
    link = context.user_data.get("link")
    img_src = context.user_data.get("image_source")

    card_img = generate_card_image(img_src)
    
    # Se for Mercado Livre e não houver preço antigo marcado, oculta a linha "De:"
    if context.user_data.get("platform") == "mercadolivre" or old_price == "R$ 0,00":
        caption = (
            f"🛒 *{title}*\n\n"
            f"✅ *Por: {price}*\n\n"
            f"🔥 *Oferta imperdível no Mercado Livre!*"
        )
    else:
        caption = (
            f"🛒 *{title}*\n\n"
            f"❌ De: {old_price}\n\n"
            f"✅ *Por: {price}*\n\n"
            f"🔥 *Oferta por tempo limitado!*"
        )

    keyboard = [[InlineKeyboardButton("COMPRAR AGORA 🔥", url=link)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_photo(
            chat_id=target_channel, 
            photo=card_img, 
            caption=caption, 
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        await update.message.reply_text(f"✅ Postagem criada e enviada com sucesso para `{target_channel}`!", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao enviar postagem: {e}")

    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END

# --- ROTA WEBHOOK DO MERCADO PAGO E HEALTH CHECK ---
@app.route("/", methods=["GET"])
def home():
    return "Bot Render Ativo!", 200

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

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# --- INICIALIZAÇÃO PRINCIPAL DO BOT ---
def main():
    import threading
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SELECTING_PLATFORM: [CallbackQueryHandler(platform_callback)],
            ML_ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_ml_link)],
            SHOPEE_ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_shopee_link)],
            ASK_IMAGE: [MessageHandler(filters.PHOTO, receive_image)],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            ASK_OLD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_old_price)],
            ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
            ASK_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_and_send)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    print("🤖 Bot integrado (Shopee + Mercado Livre) iniciado com sucesso no Render...")
    application.run_polling()

if __name__ == "__main__":
    main()
