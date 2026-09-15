import os, json, requests

ALGOLIA_URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9", 
    "x-algolia-application-id": "U3B6GR4UA3",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def fetch_deals():
    deals = []
    payload = {
        "requests": [{"indexName": "store_game_pt_br", "params": "query=&hitsPerPage=300&facetFilters=[[\"corePlatforms:Nintendo Switch\"],[\"hasDiscount:true\"]]"}]
    }
    
    try:
        res = requests.post(ALGOLIA_URL, headers=HEADERS, json=payload)
        res.raise_for_status()
        hits = res.json()["results"][0].get("hits", [])
    except:
        return []

    if not hits:
        payload["requests"][0]["params"] = "query=&hitsPerPage=300&facetFilters=[[\"corePlatforms:Nintendo Switch\"]]"
        res = requests.post(ALGOLIA_URL, headers=HEADERS, json=payload)
        hits = res.json()["results"][0].get("hits", [])

    games_dict = {}
    nsuids = []
    
    for h in hits:
        nsuid = h.get("nsuid") or h.get("objectID")
        if nsuid:
            nsuid = str(nsuid)
            nsuids.append(nsuid)
            raw_url = h.get("url", "")
            if not raw_url.startswith("http"):
                raw_url = f"https://www.nintendo.com{raw_url if raw_url.startswith('/') else '/' + raw_url}"
            games_dict[nsuid] = {
                "title": h.get("title", "Desconhecido"),
                "url": raw_url.replace("/pt-br/pt-br/", "/pt-br/")
            }

    chunk_size = 50
    for i in range(0, len(nsuids), chunk_size):
        chunk = nsuids[i:i + chunk_size]
        price_url = f"https://api.ec.nintendo.com/v1/price?country=BR&lang=pt&ids={','.join(chunk)}"
        
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
                            except: pass
        except: pass

    return deals

def main():
    # 1. Carrega os dados do dia anterior (se existirem)
    previous_urls = set()
    if os.path.exists("data/deals.json"):
        try:
            with open("data/deals.json", "r", encoding="utf-8") as f:
                old_data = json.load(f)
                for item in old_data:
                    previous_urls.add(item["url"])
        except: pass

    # 2. Busca as ofertas de hoje
    deals = fetch_deals()
    
    # 3. Compara e classifica as novidades
    novos = 0
    anteriores = 0
    
    for deal in deals:
        if deal["url"] in previous_urls:
            deal["is_new"] = False
            anteriores += 1
        else:
            deal["is_new"] = True
            novos += 1
            
    os.makedirs("data", exist_ok=True)
    with open("data/deals.json", "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)
        
    # 4. Lógica das Mensagens Personalizadas
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if token and chat_id and len(deals) > 0:
        if novos == 0 and anteriores > 0:
            msg = f"🎮 As {anteriores} promoções de hoje são as mesmas do dia anterior.\n\nAcesse o painel para conferir."
        elif novos > 0 and anteriores > 0:
            msg = f"🎮 Temos {novos} NOVAS promoções hoje!\n(E {anteriores} do dia anterior continuam ativas).\n\nAcesse o painel para conferir."
        else:
            msg = f"🎮 A eShop Brasil tem {novos} promoções ativas hoje!\n\nAcesse o painel para ver a lista."
            
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg})
        except: pass

if __name__ == "__main__":
    main()
