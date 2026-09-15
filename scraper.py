import os, json, requests

URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
HEADERS = {"x-algolia-api-key": "a29c6927638bfd8caa2394e63bd1018e", "x-algolia-application-id": "U3B6GR4UA3"}

def fetch_deals():
    payload = {"requests": [{"indexName": "store_game_pt_br", "params": "query=&hitsPerPage=150&facetFilters=[[\"corePlatforms:Nintendo Switch\"],[\"hasDiscount:true\"]]"}]}
    res = requests.post(URL, headers=HEADERS, json=payload).json()
    deals = []
    for h in res["results"][0].get("hits", []):
        reg, disc = h.get("prices", {}).get("regular"), h.get("prices", {}).get("discount")
        if reg and disc:
            deals.append({"title": h["title"], "price": disc, "old_price": reg, "url": f"https://www.nintendo.com{h['url']}"})
    return deals

deals = fetch_deals()
os.makedirs("data", exist_ok=True)
with open("data/deals.json", "w") as f: json.dump(deals, f)

if deals and os.getenv("TELEGRAM_TOKEN"):
    msg = f"🎮 {len(deals)} promoções na eShop! Confira o painel no GitHub Pages."
    requests.post(f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage", json={"chat_id": os.getenv("TELEGRAM_CHAT_ID"), "text": msg})
