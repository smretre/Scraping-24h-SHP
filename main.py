import os
import re
import time
import hashlib
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import mercadopago
from PIL import Image, ImageDraw, ImageFilter
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

# IDs do Telegram dos Administradores com acesso livre total
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "6063904865,6779689073").split(",") if x.strip()]

# Inicializa SDK do Mercado Pago
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Flask criado apenas para manter a porta aberta exigida pelo Render (Health Check)
app = Flask(__name__)

# --- BANCO DE DADOS EM MEMÓRIA ---
user_db = {}

# Estados da Conversa Passo a Passo
SELECTING_PLATFORM = 0
ML_ASK_LINK = 1
SHOPEE_ASK_LINK = 2
TEMU_ASK_LINK = 3
SHEIN_ASK_LINK = 4
ASK_IMAGE = 5
ASK_TITLE = 6
ASK_OLD_PRICE = 7
ASK_PRICE = 8
ASK_BUTTON_STYLE = 9
ASK_CHANNEL = 10
DUO_SELECT_FIRST = 13   # Escolha da 1ª plataforma do Duo
DUO_SELECT_SECOND = 14  # Escolha da 2ª plataforma do Duo

# --- PREÇOS DOS PLANOS (Em Reais) ---
PLAN_PRICES = {
    "single_shopee": 19.90,
    "single_temu": 19.90,
    "single_ml": 29.90,
    "single_shein": 29.90,
    "duo": 39.90,      
    "pro": 59.90       
}

CHANNELS_FILE = "user_channels.json"

def load_user_channels():
    """Carrega o dicionário de canais salvos do ficheiro JSON."""
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_user_channel(user_id, channel_username):
    """Guarda ou atualiza o canal associado ao ID do utilizador."""
    channels = load_user_channels()
    channels[str(user_id)] = channel_username
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=4)

def get_user_channel(user_id):
    """Retorna o canal salvo do utilizador, ou None se não existir."""
    channels = load_user_channels()
    return channels.get(str(user_id))

# --- FUNÇÕES DE CONTROLE DE ACESSO E PLANOS ---
def check_user_access(user_id, platform):
    if user_id in ADMIN_IDS:
        return True, "admin"

    if user_id not in user_db:
        user_db[user_id] = {
            "tests_left": 5,
            "plan": None,
            "platforms": [],
            "expires_at": None
        }

    data = user_db[user_id]

    if data["plan"] and data["expires_at"] and datetime.now() < data["expires_at"]:
        if data["plan"] == "pro" or platform in data["platforms"]:
            return True, "subscription"

    if data["tests_left"] > 0:
        return True, "test"

    return False, "expired"

# --- INTEGRAÇÃO COM MERCADO LIVRE ---
def get_mercadolibre_product_info(product_url):
    title = None
    image_url = None
    price_str = None
    old_price_str = None
    final_link = product_url

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = curl_requests.get(product_url, headers=headers, impersonate="chrome120", allow_redirects=True, timeout=10)
        final_link = resp.url
        
        if resp.status_code == 200:
            html = resp.text
            
            # Extrair Título
            match_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if match_title:
                title = match_title.group(1)

            # Extrair Imagem
            match_img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
            if match_img:
                image_url = match_img.group(1)

            # 1. TENTATIVA MAIS SEGURA: Extrair do JSON-LD (Dados estruturados oficiais do produto)
            json_ld_match = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            if json_ld_match:
                try:
                    data = json.loads(json_ld_match.group(1))
                    # O JSON-LD pode ser um dicionário ou lista
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("@type") == "Product" or "offers" in item:
                            offers = item.get("offers", {})
                            if isinstance(offers, list):
                                offers = offers[0]
                            
                            # Preço atual
                            p_val = offers.get("price") or offers.get("lowPrice")
                            if p_val:
                                price_str = f"R$ {float(p_val):.2f}".replace('.', ',')
                            
                            # Preço antigo (se houver alta)
                            high_val = offers.get("highPrice")
                            if high_val and float(high_val) > float(p_val or 0):
                                old_price_str = f"R$ {float(high_val):.2f}".replace('.', ',')
                            break
                except Exception:
                    pass

            # 2. SE O JSON-LD FALHAR, procuramos de forma restrita na tag itemprop="price"
            if not price_str:
                meta_price = re.search(r'<meta itemprop="price" content="([0-9.]+)"', html)
                if meta_price:
                    p_val = float(meta_price.group(1))
                    if p_val >= 10.0:
                        price_str = f"R$ {p_val:.2f}".replace('.', ',')

            # 3. VERIFICAR PREÇO ANTIGO ESPECÍFICO (apenas se houver etiqueta de preço riscado real)
            if not old_price_str:
                match_old = re.search(r'<(?:s|span)[^>]*class="[^"]*andes-money-amount--previous[^"]*"[^>]*>.*?<span[^>]*class="andes-money-amount__fraction"[^>]*>([0-9.]+)</span>', html, re.DOTALL)
                if match_old:
                    old_frac = match_old.group(1).replace('.', '')
                    sub_old = html[match_old.start():match_old.end()]
                    match_cents = re.search(r'class="andes-money-amount__cents"[^>]*>([0-9]+)</span>', sub_old)
                    old_cents = match_cents.group(1) if match_cents else "00"
                    old_val = float(f"{old_frac}.{old_cents}")
                    if price_str and old_val > float(price_str.replace('R$ ', '').replace('.', '').replace(',', '.')):
                        old_price_str = f"R$ {old_val:.2f}".replace('.', ',')

    except Exception as e:
        print(f"⚠️ Erro ao extrair dados do Mercado Livre: {e}")

    return {
        "title": title, 
        "image": image_url,
        "price": price_str,  
        "old_price": old_price_str,  
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

    return {"title": title, "image": image_url, "price": price_str, "link": short_link or final_url}

# --- INTEGRAÇÃO COM A TEMU (OBRIGA TÍTULO E IMAGEM MANUAIS) ---
def get_temu_product_info(product_url):
    final_link = product_url
    price_str = None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = curl_requests.get(product_url, headers=headers, impersonate="chrome120", allow_redirects=True, timeout=10)
        final_link = resp.url
        
        if resp.status_code == 200:
            html = resp.text
            prices = re.findall(r'R\$\s*([0-9]+[.,][0-9]{2})', html)
            if prices:
                valid_prices = [float(p.replace('.', '').replace(',', '.')) for p in prices if float(p.replace('.', '').replace(',', '.')) < 1000]
                if valid_prices:
                    p_val = min(valid_prices)
                    price_str = f"R$ {p_val:.2f}".replace('.', ',')
    except Exception as e:
        print(f"⚠️ Erro ao extrair dados da Temu: {e}")

    # Retorna título e imagem como None propositalmente para forçar digitação/envio manual
    return {"title": None, "image": None, "price": price_str, "link": final_link}

# --- INTEGRAÇÃO COM A SHEIN ---
def get_shein_product_info(product_url):
    title = None
    image_url = None
    price_str = None
    final_link = product_url

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        resp = curl_requests.get(product_url, headers=headers, impersonate="chrome120", allow_redirects=True, timeout=10)
        final_link = resp.url
        
        if resp.status_code == 200:
            html = resp.text
            match_title = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if match_title:
                title = match_title.group(1)

            match_img = re.search(r'<meta property="og:image" content="([^"]+)"', html)
            if match_img:
                image_url = match_img.group(1)

            match_price = re.search(r'class="country-price"[^>]*>.*?R\$\s*([0-9.,]+)', html, re.DOTALL)
            if match_price:
                price_str = f"R$ {match_price.group(1)}"
            else:
                match_json_price = re.search(r'"retailPrice":\s*\{\s*"amount":\s*"([0-9.]+)"', html)
                if match_json_price:
                    p_val = float(match_json_price.group(1))
                    price_str = f"R$ {p_val:.2f}".replace('.', ',')
    except Exception as e:
        print(f"⚠️ Erro ao extrair dados da Shein: {e}")

    return {"title": title, "image": image_url, "price": price_str, "link": final_link}

# --- GERADOR DE CARD / IMAGEM ---
def generate_card_image(image_source, platform="mercadolivre"):
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
        if platform in ["temu", "shein"]:
            bg_img = prod_img.copy()
            bg_w, bg_h = bg_img.size
            bg_ratio = max(canvas_width / bg_w, canvas_height / bg_h)
            bg_new_w = int(bg_w * bg_ratio)
            bg_new_h = int(bg_h * bg_ratio)
            bg_img = bg_img.resize((bg_new_w, bg_new_h), Image.Resampling.LANCZOS)
            
            bg_left = (bg_new_w - canvas_width) // 2
            bg_top = (bg_new_h - canvas_height) // 2
            bg_img = bg_img.crop((bg_left, bg_top, bg_left + canvas_width, bg_top + canvas_height))
            
            bg_img = bg_img.filter(ImageFilter.GaussianBlur(15))
            darken = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 90))
            bg_img.alpha_composite(darken)
            card.paste(bg_img, (0, 0))

        img_w, img_h = prod_img.size
        ratio = min(canvas_width / img_w, canvas_height / img_h)
        new_w = int(img_w * ratio)
        new_h = int(img_h * ratio)
        
        prod_img = prod_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (canvas_width - new_w) // 2
        top = (canvas_height - new_h) // 2
        card.paste(prod_img, (left, top), prod_img if prod_img.mode == 'RGBA' else None)
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
    user_id = update.effective_user.id
    
    if user_id not in user_db and user_id not in ADMIN_IDS:
        user_db[user_id] = {"tests_left": 5, "plan": None, "platforms": [], "expires_at": None}

    tests_info = "Acesso Livre (Admin)" if user_id in ADMIN_IDS else f"Testes grátis restantes: {user_db[user_id]['tests_left']}/5"

    keyboard = [
        [InlineKeyboardButton("🟡 Mercado Livre", callback_data="plat_ml"),
         InlineKeyboardButton("🟠 Shopee", callback_data="plat_shopee")],
        [InlineKeyboardButton("🔴 Temu", callback_data="plat_temu"),
         InlineKeyboardButton("🟣 Shein", callback_data="plat_shein")],
        [InlineKeyboardButton("💎 Ver Planos", callback_data="menu_plans")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(
            f"✔️ **Seja bem-vindo ao Bot de Afiliados Automatizado!**\n\n"
            f"ℹ️ `{tests_info}`\n\n"
            "Escolha abaixo em qual plataforma deseja gerar o anúncio ou ver nossos planos:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    elif update.callback_query:
        await update.callback_query.message.edit_text(
            f"✔️ **Seja bem-vindo ao Bot de Afiliados Automatizado!**\n\n"
            f"ℹ️ `{tests_info}`\n\n"
            "Escolha abaixo em qual plataforma deseja gerar o anúncio ou ver nossos planos:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    return SELECTING_PLATFORM

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Substitua "https://t.me/seu_grupo_de_suporte" pelo link real do seu grupo
    keyboard = [[InlineKeyboardButton("💬 Acessar Grupo de Suporte", url="https://t.me/+WsO0zYtwrmUwOGEx")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🛠️ **Central de Suporte**\n\n"
        "Encontrou algum bug, problema ou tem alguma dúvida? "
        "Clique no botão abaixo para entrar no nosso grupo de suporte oficial e falar diretamente com a gente!",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def comando_canal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # context.args já pega tudo o que vem escrito depois do comando /canal de forma limpa!
    if context.args:
        novo_canal = context.args[0]
        save_user_channel(user_id, novo_canal)
        
        await update.message.reply_text(f"✅ Canal `{novo_canal}` guardado com sucesso!", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Usa no formato correto, ex: `/canal @teucanal`", parse_mode="Markdown")
    
async def platform_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "menu_plans":
        keyboard = [
            [InlineKeyboardButton("🟠 Shopee (R$ 19,90/mês)", callback_data="buy_single_shopee")],
            [InlineKeyboardButton("🔴 Temu (R$ 19,90/mês)", callback_data="buy_single_temu")],
            [InlineKeyboardButton("🟡 Mercado Livre (R$ 29,90/mês)", callback_data="buy_single_ml")],
            [InlineKeyboardButton("🟣 Shein (R$ 29,90/mês)", callback_data="buy_single_shein")],
            [InlineKeyboardButton("⭐ Plano Duo - 2 Plataformas (R$ 39,90)", callback_data="buy_duo")],
            [InlineKeyboardButton("🚀 Plano PRO - Todas (R$ 59,90)", callback_data="buy_pro")],
            [InlineKeyboardButton("🔙 Voltar", callback_data="back_start")]
        ]
        await query.message.edit_text(
            "💎 **Planos de Assinatura (Duração de 30 dias):**\n\n"
            "• **Shopee e Temu** possuem planos individuais mais baratos devido ao fluxo assistido.\n"
            "• **Plano Duo**: Escolha 2 plataformas de sua preferência.\n"
            "• **Plano PRO**: Acesso total a todas as 4 plataformas sem limites!\n\n"
            "Escolha o plano desejado para gerar o pagamento via Mercado Pago:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECTING_PLATFORM

    if query.data == "back_start":
        await start(update, context)
        return SELECTING_PLATFORM

    plat_map = {
        "plat_ml": ("mercadolivre", ML_ASK_LINK),
        "plat_shopee": ("shopee", SHOPEE_ASK_LINK),
        "plat_temu": ("temu", TEMU_ASK_LINK),
        "plat_shein": ("shein", SHEIN_ASK_LINK)
    }

    if query.data in plat_map:
        platform_name, next_state = plat_map[query.data]
        
        allowed, reason = check_user_access(user_id, platform_name)
        
        if not allowed:
            keyboard = [[InlineKeyboardButton("🔙 Voltar ao Menu", callback_data="back_start")]]
            await query.message.edit_text(
                "❌ **Seus testes grátis acabaram!**\n\n"
                "Para continuar postando ofertas, por favor escolha um plano de assinatura.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return SELECTING_PLATFORM

        if reason == "test":
            user_db[user_id]["tests_left"] -= 1

        context.user_data["platform"] = platform_name
        
        names = {"mercadolivre": "🟡 Mercado Livre", "shopee": "🟠 Shopee", "temu": "🔴 Temu", "shein": "🟣 Shein"}
        keyboard = [[InlineKeyboardButton("🔙 Voltar", callback_data="back_start")]]
        await query.message.edit_text(
            f"{names[platform_name]} selecionado!\n\nEnvie o link do produto:", 
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return next_state

    if query.data == "buy_duo":
        keyboard = [
            [InlineKeyboardButton("🟡 Mercado Livre", callback_data="duo1_mercadolivre"),
             InlineKeyboardButton("🟠 Shopee", callback_data="duo1_shopee")],
            [InlineKeyboardButton("🔴 Temu", callback_data="duo1_temu"),
             InlineKeyboardButton("🟣 Shein", callback_data="duo1_shein")],
            [InlineKeyboardButton("🔙 Voltar", callback_data="menu_plans")]
        ]
        await query.message.edit_text(
            "⭐ **Plano Duo Selecionado (R$ 39,90)**\n\n"
            "Por favor, escolha a **1ª plataforma** que deseja incluir no seu plano:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return DUO_SELECT_FIRST

    if query.data.startswith("buy_"):
        plan_key = query.data.replace("buy_", "")
        price = PLAN_PRICES.get(plan_key, 39.90)

        if sdk:
            preference_data = {
                "items": [{
                    "title": f"Assinatura Bot Afiliados - {plan_key.upper()} (30 dias)",
                    "quantity": 1,
                    "unit_price": float(price),
                    "currency_id": "BRL"
                }],
                "external_reference": str(user_id),
                "metadata": {"plan_key": plan_key}
            }
            try:
                pref_response = sdk.preference().create(preference_data)
                init_point = pref_response["response"]["init_point"]
                
                keyboard = [
                    [InlineKeyboardButton("💳 Pagar plano", url=init_point)],
                    [InlineKeyboardButton("🔙 Voltar ao Menu", callback_data="menu_plans")]
                ]
                await query.message.edit_text(
                    f"🔗 **Link de pagamento gerado com sucesso!**\n\n"
                    f"Valor: `R$ {price:.2f}`\n"
                    f"Assim que o pagamento for aprovado, seu acesso será liberado automaticamente.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                await query.message.edit_text(f"❌ Erro ao gerar pagamento no Mercado Pago: {e}")
        else:
            await query.message.edit_text("❌ Sistema de pagamento não configurado no momento.")
        return SELECTING_PLATFORM

async def duo_first_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "menu_plans":
        return await platform_callback(update, context)
        
    first_plat = query.data.replace("duo1_", "")
    context.user_data["duo_first"] = first_plat
    
    names = {
        "mercadolivre": "🟡 Mercado Livre", 
        "shopee": "🟠 Shopee", 
        "temu": "🔴 Temu", 
        "shein": "🟣 Shein"
    }
    
    keyboard = []
    for code, name in names.items():
        if code != first_plat:
            keyboard.append([InlineKeyboardButton(name, callback_data=f"duo2_{code}")])
    keyboard.append([InlineKeyboardButton("🔙 Voltar", callback_data="buy_duo")])

    await query.message.edit_text(
        f"✅ 1ª Plataforma escolhida: **{names[first_plat]}**\n\n"
        "Agora, escolha a **2ª plataforma** do seu Plano Duo:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DUO_SELECT_SECOND

async def duo_second_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("duo1_") or query.data == "buy_duo":
        return await duo_first_choice(update, context)
        
    second_plat = query.data.replace("duo2_", "")
    first_plat = context.user_data.get("duo_first")
    selected_platforms = [first_plat, second_plat]
    
    user_id = update.effective_user.id
    price = PLAN_PRICES["duo"]

    if sdk:
        preference_data = {
            "items": [{
                "title": "Assinatura Bot Afiliados - Plano Duo (30 dias)",
                "quantity": 1,
                "unit_price": float(price),
                "currency_id": "BRL"
            }],
            "external_reference": str(user_id),
            "metadata": {
                "plan_key": "duo",
                "platforms": selected_platforms
            }
        }
        try:
            pref_response = sdk.preference().create(preference_data)
            init_point = pref_response["response"]["init_point"]
            
            names = {
                "mercadolivre": "Mercado Livre", 
                "shopee": "Shopee", 
                "temu": "Temu", 
                "shein": "Shein"
            }
            
            keyboard = [
                [InlineKeyboardButton("💳 Pagar Plano Duo", url=init_point)],
                [InlineKeyboardButton("🔙 Voltar", callback_data="buy_duo")]
            ]
            await query.message.edit_text(
                f"💎 **Plano Duo configurado com sucesso!**\n\n"
                f"• Plataformas: `{names.get(first_plat, first_plat)}` e `{names.get(second_plat, second_plat)}`\n"
                f"• Valor: `R$ {price:.2f}`\n\n"
                f"Clique abaixo para realizar o pagamento via Mercado Pago:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            await query.message.edit_text(f"❌ Erro ao gerar pagamento: {e}")
            
    return SELECTING_PLATFORM

async def process_ml_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return ML_ASK_LINK

    await update.message.reply_text("🔍 Extraindo informações do Mercado Livre...")
    info = get_mercadolibre_product_info(text)

    context.user_data["title"] = info["title"] or "Produto Mercado Livre"
    context.user_data["image_source"] = info["image"]
    context.user_data["price"] = info["price"] or ""
    context.user_data["link"] = info["link"]

    if not info["image"]:
        await update.message.reply_text("⚠️ Não conseguimos puxar a foto automaticamente. Envie a foto do produto:")
        return ASK_IMAGE

    if not info["price"]:
        await update.message.reply_text("💰 Digite e envie o **Preço Atual (Por)** do produto:", parse_mode="Markdown")
        return ASK_PRICE

    # Se o bot achou o preço antigo sozinho, ele salva e pode avançar
    if info["old_price"]:
        context.user_data["old_price"] = info["old_price"]
        
        # Defina o teclado de estilos (caso já tenha ele pronto em outra parte do código)
        keyboard = [
            [InlineKeyboardButton("🔵 Azul", callback_data="style_primary"),
             InlineKeyboardButton("🟢 Verde", callback_data="style_success")],
            [InlineKeyboardButton("🔴 Vermelho", callback_data="style_danger"),
             InlineKeyboardButton("⚪ Padrão", callback_data="style_default")]
        ]
        
        await update.message.reply_text(
            f"✅ **Preço Atual:** `{info['price']}`\n"
            f"🏷️ **Preço Antigo detectado:** `{info['old_price']}`\n\n"
            "🎨 **Escolha a cor de destaque do botão da oferta:**",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ASK_BUTTON_STYLE

    else:
        context.user_data["old_price"] = ""
        await update.message.reply_text(
            f"💰 Preço atual detectado: `{info['price']}`\n\n"
            "❌ Digite e envie o **Preço Antigo** (ou `0` se não tiver):", 
            parse_mode="Markdown"
        )
        return ASK_OLD_PRICE
        
async def process_shopee_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return SHOPEE_ASK_LINK

    await update.message.reply_text("🔍 Analisando link da Shopee...")
    product_info = get_shopee_product_info(text)

    context.user_data["link"] = product_info["link"]
    context.user_data["price"] = product_info["price"] or ""
    context.user_data["title"] = product_info["title"]
    context.user_data["image_source"] = product_info["image"]

    if not product_info["image"]:
        await update.message.reply_text("📸 Não foi possível detectar a imagem. Envie a foto do produto:")
        return ASK_IMAGE

    if not product_info["title"]:
        await update.message.reply_text("📝 Digite e envie o **título do produto**:", parse_mode="Markdown")
        return ASK_TITLE

    if not product_info["price"]:
        await update.message.reply_text("💰 Digite e envie o **Preço Atual (Por)** do produto:", parse_mode="Markdown")
        return ASK_PRICE

    await update.message.reply_text(f"💰 Preço detectado: `{product_info['price']}`\n\n❌ Digite e envie o **Preço Antigo** (ou `0` se não tiver):", parse_mode="Markdown")
    return ASK_OLD_PRICE

# --- PROCESSAMENTO TEMU (EXIGE IMAGEM E TÍTULO MANUAIS) ---
async def process_temu_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return TEMU_ASK_LINK

    await update.message.reply_text("🔍 Analisando link da Temu...")
    product_info = get_temu_product_info(text)

    context.user_data["link"] = product_info["link"]
    context.user_data["price"] = product_info["price"] or ""
    context.user_data["title"] = None          # Força manual
    context.user_data["image_source"] = None   # Força manual

    # Como a Temu gera títulos genéricos, obriga o envio da foto primeiro
    await update.message.reply_text("📸 **Temu:** Como os títulos gerados são genéricos, envie a **foto do produto** manualmente:", parse_mode="Markdown")
    return ASK_IMAGE

async def process_shein_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text:
        return SHEIN_ASK_LINK

    await update.message.reply_text("🔍 Extraindo informações da Shein...")
    info = get_shein_product_info(text)

    context.user_data["title"] = info["title"] or "Produto Shein"
    context.user_data["image_source"] = info["image"]
    context.user_data["price"] = info["price"] or ""
    context.user_data["link"] = info["link"]

    if not info["image"]:
        await update.message.reply_text("⚠️ Não conseguimos puxar a foto automaticamente. Envie a foto do produto:")
        return ASK_IMAGE

    if not info["price"]:
        await update.message.reply_text("💰 Digite e envie o **Preço Atual (Por)** do produto:", parse_mode="Markdown")
        return ASK_PRICE

    await update.message.reply_text(f"💰 Preço detectado: `{info['price']}`\n\n❌ Digite e envie o **Preço Antigo** (ou `0` se não tiver):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Por favor, envie uma foto válida:")
        return ASK_IMAGE

    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    context.user_data["image_source"] = image_bytes

    if not context.user_data.get("title"):
        await update.message.reply_text("📝 Agora digite e envie o **título do produto**:", parse_mode="Markdown")
        return ASK_TITLE
    
    if not context.user_data.get("price"):
        await update.message.reply_text("💰 Digite e envie o **Preço Atual (Por)**:", parse_mode="Markdown")
        return ASK_PRICE

    await update.message.reply_text("❌ Agora digite e envie o **Preço Antigo** (ou `0` se não tiver):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text
    if not title:
        await update.message.reply_text("⚠️ Por favor, envie um título válido:")
        return ASK_TITLE

    context.user_data["title"] = title
    
    if not context.user_data.get("price"):
        await update.message.reply_text("💰 Digite e envie o **Preço Atual (Por)**:", parse_mode="Markdown")
        return ASK_PRICE

    await update.message.reply_text("❌ Agora digite e envie o **Preço Antigo** (ou `0` se não tiver):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = update.message.text.strip()
    if not price:
        await update.message.reply_text("⚠️ Por favor, envie um preço atual válido:")
        return ASK_PRICE

    context.user_data["price"] = price
    await update.message.reply_text("❌ Agora digite e envie o **Preço Antigo** (ou `0` se não tiver):", parse_mode="Markdown")
    return ASK_OLD_PRICE

async def receive_old_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    old_price = update.message.text.strip()
    if not old_price:
        await update.message.reply_text("⚠️ Por favor, envie um preço antigo válido ou `0`:")
        return ASK_OLD_PRICE

    context.user_data["old_price"] = old_price

    keyboard = [
        [InlineKeyboardButton("🔵 Azul (Primary)", callback_data="style_primary"),
         InlineKeyboardButton("🟢 Verde (Success)", callback_data="style_success")],
        [InlineKeyboardButton("🔴 Vermelho (Danger)", callback_data="style_danger"),
         InlineKeyboardButton("⚪ Padrão (Sem cor)", callback_data="style_default")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎨 **Escolha a cor de destaque do botão da oferta:**",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    return ASK_BUTTON_STYLE

async def receive_button_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    style_map = {
        "style_primary": "primary",
        "style_success": "success",
        "style_danger": "danger",
        "style_default": None
    }
    
    selected_style = style_map.get(query.data, None)
    context.user_data["button_style"] = selected_style
    
    user_id = query.from_user.id
    canal_salvo = get_user_channel(user_id)

    if canal_salvo:
        # 🚀 Canal salvo detetado! Passamos o controlo diretamente para a função de envio
        context.user_data["target_channel"] = canal_salvo
        await query.message.edit_text(
            f"🚀 Canal salvo detetado (`{canal_salvo}`). A processar a publicação...",
            parse_mode="Markdown"
        )
        
        # Criação de classes auxiliares para simular a mensagem de texto e avançar sem erros
        class FakeMessage:
            def __init__(self, message, user, text):
                self.text = text
                self._message = message
                self.from_user = user
            async def reply_text(self, *args, **kwargs):
                return await self._message.reply_text(*args, **kwargs)

        class FakeUpdate:
            def __init__(self, query_obj, text):
                self.effective_user = query_obj.from_user
                self.message = FakeMessage(query_obj.message, query_obj.from_user, text)

        fake_update = FakeUpdate(query, canal_salvo)
        
        # Chama a tua função de envio original sem repetição de código!
        return await receive_channel_and_send(fake_update, context)
    else:
        # ⚠️ Se não tem canal salvo, pede normalmente pela primeira vez
        await query.message.edit_text(
            "📢 Agora envie o **ID ou Username do canal/grupo** de destino onde a oferta será publicada:\n\n*(Dica: Envie /cancel se quiser desistir)*",
            parse_mode="Markdown"
        )
        return ASK_CHANNEL

async def receive_channel_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_channel = update.message.text.strip()
    
    # 💾 Guarda o canal automaticamente para nunca mais precisar de pedir
    save_user_channel(user_id, target_channel)
    context.user_data["target_channel"] = target_channel
    
    await update.message.reply_text("🔍 Verificando permissões de Administrador...")
    is_admin = await verify_bot_admin(context.bot, target_channel)

    if not is_admin:
        await update.message.reply_text(
            f"❌ O bot **não é Administrador** no destino `{target_channel}`.\n\n"
            "Adicione o bot como ADM e tente enviar o ID/Username novamente:",
            parse_mode="Markdown"
        )
        return ASK_CHANNEL

    # Se for administrador, continua o fluxo normal de envio da oferta...

    title = context.user_data.get("title", "🔥 Super Oferta")
    old_price = context.user_data.get("old_price", "0")
    price = context.user_data.get("price", "R$ 0,00")
    link = context.user_data.get("link")
    img_src = context.user_data.get("image_source")
    platform = context.user_data.get("platform", "mercadolivre")
    btn_style = context.user_data.get("button_style")

    card_img = generate_card_image(img_src, platform=platform)
    
    if not old_price or old_price in ["0", "R$ 0", "R$ 0,00"]:
        caption = (
            f"🛒 *{title}*\n\n"
            f"✅ *Por: {price}*\n\n"
            f"🔥 *Oferta imperdível!*"
        )
    else:
        caption = (
            f"🛒 *{title}*\n\n"
            f"❌ De: {old_price}\n\n"
            f"✅ *Por: {price}*\n\n"
            f"🔥 *Oferta por tempo limitado!*"
        )

    button_kwargs = {"text": "🔥 COMPRAR AGORA 🛒", "url": link}
    if btn_style:
        button_kwargs["style"] = btn_style

    keyboard = [[InlineKeyboardButton(**button_kwargs)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_photo(
            chat_id=target_channel, 
            photo=card_img, 
            caption=caption, 
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        await update.message.reply_text(f"✅ Postagem criada e enviada com sucesso para `{target_channel}`!\n\nEnvie /start para criar uma nova oferta.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao enviar postagem: {e}")

    context.user_data.clear()
    return ConversationHandler.END
    
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada. Envie /start para reiniciar.")
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
                metadata = payment_info.get("metadata", {})
                plan_key = metadata.get("plan_key", "pro")

                expires_at = datetime.now() + timedelta(days=30)
                platforms = []

                if plan_key == "single_shopee":
                    platforms = ["shopee"]
                elif plan_key == "single_temu":
                    platforms = ["temu"]
                elif plan_key == "single_ml":
                    platforms = ["mercadolivre"]
                elif plan_key == "single_shein":
                    platforms = ["shein"]
                elif plan_key == "duo":
                    platforms = metadata.get("platforms", ["shopee", "temu"])
                elif plan_key == "pro":
                    platforms = ["shopee", "temu", "mercadolivre", "shein"]

                user_db[telegram_id] = {
                    "tests_left": 0,
                    "plan": "pro" if plan_key == "pro" else "single",
                    "platforms": platforms,
                    "expires_at": expires_at
                }
                print(f"✅ Pagamento aprovado e plano ativado para o usuário ID: {telegram_id} (Plano: {plan_key}, Plataformas: {platforms})")
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
            SELECTING_PLATFORM: [
                CallbackQueryHandler(platform_callback),
                CallbackQueryHandler(start, pattern="^back_start$")
            ],
            DUO_SELECT_FIRST: [
                CallbackQueryHandler(duo_first_choice, pattern="^duo1_"),
                CallbackQueryHandler(platform_callback, pattern="^menu_plans$")
            ],
            DUO_SELECT_SECOND: [
                CallbackQueryHandler(duo_second_choice, pattern="^duo2_"),
                CallbackQueryHandler(duo_first_choice, pattern="^buy_duo$")
            ],
            # Adicionado suporte ao botão "Voltar" (back_start) em todos os estados de envio de link para eliminar o loop
            ML_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_ml_link),
                CallbackQueryHandler(platform_callback, pattern="^back_start$")
            ],
            SHOPEE_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_shopee_link),
                CallbackQueryHandler(platform_callback, pattern="^back_start$")
            ],
            TEMU_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_temu_link),
                CallbackQueryHandler(platform_callback, pattern="^back_start$")
            ],
            SHEIN_ASK_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_shein_link),
                CallbackQueryHandler(platform_callback, pattern="^back_start$")
            ],
            ASK_IMAGE: [MessageHandler(filters.PHOTO, receive_image)],
            ASK_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            ASK_OLD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_old_price)],
            ASK_BUTTON_STYLE: [
                CallbackQueryHandler(receive_button_style, pattern="^style_")
            ],
            ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
            ASK_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_and_send)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    application.add_handler(CommandHandler("suporte", support))
    application.add_handler(conv_handler)

    print("🤖 Bot multiplataforma com navegação fluida iniciado no Render...")
    application.run_polling()

if __name__ == "__main__":
    main()
