import os, json, requests, time

ALGOLIA_URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9", 
    "x-algolia-application-id": "U3B6GR4UA3",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def fetch_deals():
    deals = []
    games_dict = {}
    
    print("1. Iniciando varredura total (Letra por Letra com Paginação Dinâmica)...")
    caracteres = list("abcdefghijklmnopqrstuvwxyz0123456789")
    
    for letra in caracteres:
        page = 0
        while True:
            # Filtramos diretamente por 'hasDiscount:true' para pegar Jogos, DLCs, Bundles, Switch e Switch 2!
            payload = {
                "requests": [
                    {
                        "indexName": "store_game_pt_br", 
                        "params": f"query={letra}&hitsPerPage=1000&page={page}&facetFilters=[[\"hasDiscount:true\"]]"
                    }
                ]
            }
            
            try:
                res = requests.post(ALGOLIA_URL, headers=HEADERS, json=payload)
                res.raise_for_status()
                data = res.json()["results"][0]
                hits = data.get("hits", [])
                nb_pages = data.get("nbPages", 1) # Descobre quantas páginas a letra tem
                
                for h in hits:
                    nsuid = h.get("nsuid") or h.get("objectID") or h.get("id")
                    if nsuid:
                        nsuid = str(nsuid)
                        raw_url = h.get("url", "")
                        if not raw_url.startswith("http"):
                            raw_url = f"https://www.nintendo.com{raw_url if raw_url.startswith('/') else '/' + raw_url}"
                        
                        img = h.get("boxArt") or h.get("horizontalHeaderImage") or ""
                        
                        games_dict[nsuid] = {
                            "title": h.get("title", "Desconhecido"),
                            "url": raw_url.replace("/pt-br/pt-br/", "/pt-br/"),
                            "image": img
                        }
                        
                # Se já lemos todas as páginas dessa letra, saímos do loop
                if page >= nb_pages - 1:
                    break
                
                # Se não, avança para a próxima página da mesma letra
                page += 1
                
            except Exception as e:
                print(f"Erro na letra '{letra}', página {page}: {e}")
                break

    nsuids = list(games_dict.keys())
    print(f"\nTotal de itens com desconto mapeados no catálogo: {len(nsuids)}")
    
    print(f"2. Consultando a API Oficial de Preços para {len(nsuids)} itens...")
    
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
                                    "url": games_dict[tid]["url"],
                                    "image": games_dict[tid]["image"]
                                })
                            except:
                                pass
        except Exception as e:
            pass
            
        time.sleep(0.2) # Pausa rápida de segurança

    return deals

def main():
    previous_urls = set()
    if os.path.exists("data/deals.json"):
        try:
            with open("data/deals.json", "r", encoding="utf-8") as f:
                old_data = json.load(f)
                for item in old_data:
                    previous_urls.add(item["url"])
        except: pass

    deals = fetch_deals()
    
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
        
    print(f"\n3. Concluído! {len(deals)} promoções processadas com sucesso.")
    
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if token and chat_id and len(deals) > 0:
        if novos == 0 and anteriores > 0:
            msg = f"🎮 As {anteriores} promoções de hoje são as mesmas do dia anterior.\n\nTotal ativo: {len(deals)} ofertas.\nAcesse o painel para conferir."
        elif novos > 0 and anteriores > 0:
            msg = f"🎮 Temos {novos} NOVAS promoções hoje!\n(E {anteriores} do dia anterior continuam ativas).\n\nTotal ativo: {len(deals)} ofertas.\nAcesse o painel para conferir."
        else:
            msg = f"🎮 A eShop Brasil tem {novos} promoções ativas hoje!\n\nTotal ativo: {len(deals)} ofertas.\nAcesse o painel para ver a lista."
            
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg})
        except: pass

if __name__ == "__main__":
    main()
