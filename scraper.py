import os, json, requests

ALGOLIA_URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9", 
    "x-algolia-application-id": "U3B6GR4UA3",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def fetch_deals():
    deals = []
    print("1. Buscando lista de jogos na Algolia (Nintendo)...")
    
    payload = {
        "requests": [
            {
                "indexName": "store_game_pt_br", 
                "params": "query=&hitsPerPage=300&facetFilters=[[\"corePlatforms:Nintendo Switch\"],[\"hasDiscount:true\"]]"
            }
        ]
    }
    
    try:
        res = requests.post(ALGOLIA_URL, headers=HEADERS, json=payload)
        res.raise_for_status()
        hits = res.json()["results"][0].get("hits", [])
    except Exception as e:
        print(f"Erro na Algolia: {e}")
        return []

    if not hits:
        print("Fallback: Buscando jogos gerais para filtrar preços depois...")
        payload["requests"][0]["params"] = "query=&hitsPerPage=300&facetFilters=[[\"corePlatforms:Nintendo Switch\"]]"
        res = requests.post(ALGOLIA_URL, headers=HEADERS, json=payload)
        hits = res.json()["results"][0].get("hits", [])

    print(f"Total de jogos encontrados no catálogo: {len(hits)}")
    
    games_dict = {}
    nsuids = []
    
    for h in hits:
        nsuid = h.get("nsuid") or h.get("objectID")
        if nsuid:
            nsuid = str(nsuid)
            nsuids.append(nsuid)
            url_path = h.get("url", "")
            games_dict[nsuid] = {
                "title": h.get("title", "Desconhecido"),
                # 👇 AQUI ESTÁ A CORREÇÃO: Removido o /pt-br duplicado!
                "url": f"https://www.nintendo.com{url_path}" if url_path.startswith("/") else url_path
            }

    print(f"2. Consultando a API Financeira para os {len(nsuids)} jogos...")
    
    chunk_size = 50
    for i in range(0, len(nsuids), chunk_size):
        chunk = nsuids[i:i + chunk_size]
        ids_str = ",".join(chunk)
        price_url = f"https://api.ec.nintendo.com/v1/price?country=BR&lang=pt&ids={ids_str}"
        
        try:
            p_res = requests.get(price_url)
            if p_res.status_code == 200:
                for p in p_res.json().get("prices", []):
                    tid = str(p.get("title_id"))
                    if tid in games_dict:
                        discount = p.get("discount_price")
                        regular = p.get("regular_price")
                        
                        if discount and regular:
                            try:
                                deals.append({
                                    "title": games_dict[tid]["title"],
                                    "price": float(discount["raw_value"]),
                                    "old_price": float(regular["raw_value"]),
                                    "url": games_dict[tid]["url"]
                                })
                            except:
                                pass
        except Exception as e:
            print(f"Erro ao buscar preços do lote: {e}")

    return deals

def main():
    deals = fetch_deals()
    
    os.makedirs("data", exist_ok=True)
    with open("data/deals.json", "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)
        
    print(f"3. Sucesso! {len(deals)} promoções processadas e salvas.")
    
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("4. Telegram cancelado: Chaves ausentes.")
    elif len(deals) == 0:
        print("4. Telegram cancelado: 0 promoções.")
    else:
        print("4. Notificando Telegram...")
        msg = f"🎮 A eShop Brasil tem {len(deals)} grandes jogos em promoção hoje!\n\nAcesse seu painel no GitHub Pages para ver a lista."
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage", 
                json={"chat_id": chat_id, "text": msg}
            )
        except Exception as e:
            pass

if __name__ == "__main__":
    main()
