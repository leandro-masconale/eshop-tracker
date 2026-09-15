import os, json, requests

URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8caa2394e63bd1018e", 
    "x-algolia-application-id": "U3B6GR4UA3",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" # Evita bloqueio da Nintendo
}

try:
    print("1. Buscando dados da Nintendo...")
    payload = {"requests": [{"indexName": "store_game_pt_br", "params": "query=&hitsPerPage=150&facetFilters=[[\"corePlatforms:Nintendo Switch\"],[\"hasDiscount:true\"]]"}]}
    res = requests.post(URL, headers=HEADERS, json=payload)
    res.raise_for_status()
    
    data = res.json()
    deals = []
    
    for h in data["results"][0].get("hits", []):
        reg = h.get("prices", {}).get("regular")
        disc = h.get("prices", {}).get("discount")
        if reg and disc:
            deals.append({"title": h["title"], "price": disc, "old_price": reg, "url": f"https://www.nintendo.com{h['url']}"})
    
    os.makedirs("data", exist_ok=True)
    with open("data/deals.json", "w", encoding="utf-8") as f: 
        json.dump(deals, f, ensure_ascii=False, indent=2)
        
    print(f"2. Sucesso! {len(deals)} promoções encontradas e salvas.")
    
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if deals and token and chat_id:
        print("3. Enviando aviso para o Telegram...")
        msg = f"🎮 {len(deals)} promoções na eShop Brasil!\nConfira seu painel no GitHub Pages."
        t_res = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", 
            json={"chat_id": chat_id, "text": msg}
        )
        if t_res.status_code != 200:
            print(f"Erro no Telegram: {t_res.text}")
        else:
            print("4. Telegram enviado com sucesso!")
    else:
        print("3. Telegram pulado (chaves ausentes).")
        
except Exception as e:
    print(f"❌ Ocorreu um erro: {e}")
    exit(1)
