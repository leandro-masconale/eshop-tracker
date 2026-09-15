import os
import json
import requests

def fetch_deals():
    deals = []
    limit = 30
    
    print("1. Conectando aos servidores oficiais da Nintendo (ec.nintendo.com)...")
    try:
        # Busca a lista oficial de jogos em promoção na eShop Brasil
        req = requests.get(f"https://ec.nintendo.com/api/BR/pt/search/sales?count={limit}&offset=0")
        req.raise_for_status()
        data = req.json()
    except Exception as e:
        print(f"❌ Erro ao acessar a Nintendo: {e}")
        return []

    total = data.get("total", 0)
    print(f"Total de jogos em promoção na loja: {total}")
    
    # Para não sobrecarregar o robô, vamos pegar as primeiras 150 promoções (as principais)
    max_items = min(total, 150)
    
    for offset in range(0, max_items, limit):
        res = requests.get(f"https://ec.nintendo.com/api/BR/pt/search/sales?count={limit}&offset={offset}")
        if res.status_code != 200: continue
            
        contents = res.json().get("contents", [])
        if not contents: continue
            
        # Extrai os IDs dos jogos para perguntar o preço
        ids = [str(item["id"]) for item in contents if "id" in item]
        if not ids: continue
            
        # Pergunta para a API financeira da Nintendo o preço em Reais (R$) desses IDs
        price_res = requests.get(f"https://api.ec.nintendo.com/v1/price?country=BR&lang=pt&ids={','.join(ids)}")
        if price_res.status_code != 200: continue
            
        prices_data = price_res.json().get("prices", [])
        price_dict = {str(p["title_id"]): p for p in prices_data}
        
        # Junta o nome do jogo com o preço
        for item in contents:
            item_id = str(item["id"])
            price_info = price_dict.get(item_id, {})
            
            discount = price_info.get("discount_price")
            regular = price_info.get("regular_price")
            
            if discount and regular:
                try:
                    disc_val = float(discount["raw_value"])
                    reg_val = float(regular["raw_value"])
                    
                    deals.append({
                        "title": item.get("formal_name", "Desconhecido"),
                        "price": disc_val,
                        "old_price": reg_val,
                        "image": item.get("hero_banner_url", ""),
                        "url": f"https://ec.nintendo.com/BR/pt/titles/{item_id}"
                    })
                except Exception:
                    pass
                    
    return deals

def main():
    deals = fetch_deals()
    
    os.makedirs("data", exist_ok=True)
    with open("data/deals.json", "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)
        
    print(f"2. Sucesso! {len(deals)} promoções processadas e salvas.")
    
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    print(f"--- DIAGNÓSTICO ---")
    print(f"Token OK? {'SIM' if token else 'NÃO'}")
    print(f"Chat ID OK? {'SIM' if chat_id else 'NÃO'}")
    print(f"Promoções achadas: {len(deals)}")
    print(f"-------------------")
    
    if not token or not chat_id:
        print("3. Telegram cancelado: Chaves não configuradas no GitHub Secrets.")
    elif len(deals) == 0:
        print("3. Telegram cancelado: Nenhuma promoção encontrada.")
    else:
        print("3. Notificando seu celular...")
        msg = f"🎮 A eShop Brasil tem {len(deals)} grandes jogos em promoção hoje!\n\nAcesse seu painel para ver a lista."
        try:
            t_res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage", 
                json={"chat_id": chat_id, "text": msg}
            )
            if t_res.status_code == 200:
                print("4. Mensagem entregue com sucesso no Telegram!")
            else:
                print(f"❌ Erro no Telegram: {t_res.text}")
        except Exception as e:
            print(f"❌ Falha de conexão com o Telegram: {e}")

if __name__ == "__main__":
    main()
