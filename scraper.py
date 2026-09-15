"""
Scraper de promoções da Nintendo eShop Brasil.

Estratégia:
1. O índice Algolia da Nintendo (`store_game_pt_br`) tem um limite de segurança
   `paginationLimitedTo` de 1000 resultados por query (hitsPerPage * page nunca
   pode passar de 1000). Isso significa que, para uma letra muito comum (ex:
   "a", "e", "s"), paginar (page=0,1,2...) NÃO funciona: a partir da página 1
   a API já rejeita a requisição, então boa parte do catálogo era perdida.
2. Para contornar isso, ao invés de paginar, o script faz "query splitting
   recursivo": quando uma busca por um prefixo retorna mais que 1000 hits (ou
   seja, está no limite e pode estar cortando resultados), o prefixo é
   subdividido acrescentando mais um caractere (a-z0-9) e cada sub-prefixo é
   buscado separadamente, recursivamente, até que cada busca traga menos que
   o limite ou até uma profundidade máxima de segurança.
3. Todos os IDs (nsuid/objectID) encontrados são deduplicados num dicionário
   e, ao final, consultados em lotes de 50 na API oficial de preços da
   Nintendo, que é a fonte de verdade sobre haver ou não desconto ativo.
"""

import json
import logging
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

ALGOLIA_URL = "https://u3b6gr4ua3-dsn.algolia.net/1/indexes/*/queries"
ALGOLIA_INDEX = "store_game_pt_br"
ALGOLIA_HEADERS = {
    "x-algolia-api-key": "a29c6927638bfd8cee23993e51e721c9",
    "x-algolia-application-id": "U3B6GR4UA3",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

PRICE_API_URL = "https://api.ec.nintendo.com/v1/price"
PRICE_BATCH_SIZE = 50

DATA_PATH = "data/deals.json"

# O índice limita hitsPerPage*page a este valor (paginationLimitedTo padrão
# da Algolia é 1000). Usamos hitsPerPage no máximo permitido e nunca paginamos
# além da página 0 — em vez disso, refinamos a query quando o teto é atingido.
ALGOLIA_HITS_PER_PAGE = 1000
# Se uma busca retornar >= a este número de hits, consideramos que pode estar
# sendo cortada pelo teto da Algolia e precisamos refinar (subdividir) a query.
SPLIT_THRESHOLD = 1000
# Profundidade máxima de refinamento (tamanho máximo do prefixo de busca).
# 3500 ofertas ativas se espalham tranquilamente com prefixos de 2-3
# caracteres; o limite de 5 é só uma rede de segurança contra loop infinito.
MAX_PREFIX_DEPTH = 5

ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789")

MAX_WORKERS = int(os.getenv("SCRAPER_MAX_WORKERS", "6"))
REQUEST_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT", "20"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eshop-tracker")


def build_session() -> requests.Session:
    """Sessão HTTP com retry/backoff automático para falhas de rede."""
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=MAX_WORKERS * 2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# --------------------------------------------------------------------------
# Etapa 1: varredura exaustiva do catálogo via Algolia
# --------------------------------------------------------------------------

def algolia_query(session: requests.Session, prefix: str) -> dict:
    """Executa uma única query na Algolia para o prefixo informado."""
    facet_filters = json.dumps([["hasDiscount:true"]])
    params = urllib.parse.urlencode(
        {
            "query": prefix,
            "hitsPerPage": ALGOLIA_HITS_PER_PAGE,
            "page": 0,
            "facetFilters": facet_filters,
            "attributesToRetrieve": json.dumps(
                ["nsuid", "objectID", "id", "title", "url", "boxArt", "horizontalHeaderImage"]
            ),
        }
    )
    payload = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}

    resp = session.post(
        ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()["results"][0]


def normalize_hit(hit: dict) -> tuple[str, dict] | None:
    """Extrai (id, dados relevantes) de um hit da Algolia, ou None se inválido."""
    nsuid = hit.get("nsuid") or hit.get("objectID") or hit.get("id")
    if not nsuid:
        return None
    nsuid = str(nsuid)

    raw_url = hit.get("url", "") or ""
    if not raw_url.startswith("http"):
        raw_url = f"https://www.nintendo.com{raw_url if raw_url.startswith('/') else '/' + raw_url}"
    # Corrige possível duplicidade de segmento de idioma na URL.
    clean_url = raw_url.replace("/pt-br/pt-br/", "/pt-br/")

    image = (
        hit.get("boxArt")
        or hit.get("horizontalHeaderImage")
        or hit.get("topPictureUrl")
        or ""
    )

    return nsuid, {
        "title": hit.get("title", "Desconhecido"),
        "url": clean_url,
        "image": image,
    }


def collect_games() -> dict:
    """
    Varre todo o catálogo com desconto ativo, subdividindo recursivamente
    qualquer prefixo cuja busca esbarre no teto de 1000 resultados da
    Algolia, garantindo que nenhuma oferta fique de fora.
    """
    games: dict[str, dict] = {}
    lock = Lock()
    session = build_session()

    # Fila de prefixos a consultar. Começa com o alfabeto completo.
    pending = list(ALPHABET)
    seen_prefixes: set[str] = set()
    queries_done = 0

    def process_prefix(prefix: str) -> list[str]:
        """Consulta um prefixo; retorna sub-prefixos a explorar se necessário."""
        try:
            result = algolia_query(session, prefix)
        except Exception as exc:  # noqa: BLE001 - resiliência a qualquer falha de rede
            log.warning("Falha ao consultar prefixo '%s': %s — tentando de novo depois", prefix, exc)
            return [prefix] if len(prefix) < MAX_PREFIX_DEPTH else []

        hits = result.get("hits", [])
        nb_hits = result.get("nbHits", len(hits))

        with lock:
            for hit in hits:
                normalized = normalize_hit(hit)
                if normalized:
                    games[normalized[0]] = normalized[1]

        # Se bateu no teto e ainda dá para refinar, gera sub-prefixos.
        if nb_hits >= SPLIT_THRESHOLD and len(prefix) < MAX_PREFIX_DEPTH:
            return [prefix + c for c in ALPHABET]

        if nb_hits >= SPLIT_THRESHOLD:
            log.warning(
                "Prefixo '%s' atingiu profundidade máxima com %d hits reportados; "
                "parte do resultado pode ter sido descartada pelo teto da Algolia.",
                prefix,
                nb_hits,
            )
        return []

    log.info("1. Iniciando varredura exaustiva (query-splitting recursivo)...")
    while pending:
        batch = [p for p in pending if p not in seen_prefixes]
        pending = []
        if not batch:
            break
        seen_prefixes.update(batch)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(process_prefix, p): p for p in batch}
            for future in as_completed(futures):
                queries_done += 1
                pending.extend(future.result())

    log.info(
        "Varredura concluída: %d queries executadas, %d jogos únicos com desconto mapeados.",
        queries_done,
        len(games),
    )
    return games


# --------------------------------------------------------------------------
# Etapa 2: consulta oficial de preços
# --------------------------------------------------------------------------

def fetch_prices(session: requests.Session, ids: list[str]) -> list[dict]:
    """Consulta um lote de até 50 IDs na API oficial de preços da Nintendo."""
    url = f"{PRICE_API_URL}?country=BR&lang=pt&ids={','.join(ids)}"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("prices", [])


def fetch_deals() -> list[dict]:
    games = collect_games()
    nsuids = list(games.keys())

    log.info("2. Consultando a API oficial de preços para %d itens...", len(nsuids))
    session = build_session()
    batches = [nsuids[i : i + PRICE_BATCH_SIZE] for i in range(0, len(nsuids), PRICE_BATCH_SIZE)]

    deals: list[dict] = []
    lock = Lock()

    def process_batch(batch: list[str]) -> None:
        try:
            prices = fetch_prices(session, batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao consultar lote de preços (%d ids): %s", len(batch), exc)
            return

        local_deals = []
        for price in prices:
            title_id = str(price.get("title_id"))
            game = games.get(title_id)
            if not game:
                continue

            discount = price.get("discount_price")
            regular = price.get("regular_price")
            if not (discount and regular):
                continue

            try:
                local_deals.append(
                    {
                        "title": game["title"],
                        "price": float(discount["raw_value"]),
                        "old_price": float(regular["raw_value"]),
                        "url": game["url"],
                        "image": game["image"],
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue

        with lock:
            deals.extend(local_deals)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        list(executor.map(process_batch, batches))

    log.info("Total de %d ofertas com desconto confirmado pela API de preços.", len(deals))
    return deals


# --------------------------------------------------------------------------
# Etapa 3: diff com o histórico + persistência
# --------------------------------------------------------------------------

def load_previous_urls() -> set[str]:
    if not os.path.exists(DATA_PATH):
        return set()
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        return {item["url"] for item in old_data if "url" in item}
    except (json.JSONDecodeError, OSError, TypeError, KeyError):
        log.warning("Não foi possível ler %s; tratando como primeira execução.", DATA_PATH)
        return set()


def save_deals(deals: list[dict]) -> None:
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(deals, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# Etapa 4: notificação no Telegram
# --------------------------------------------------------------------------

def send_telegram_notification(novos: int, anteriores: int, total: int) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id) or total == 0:
        return

    if novos == 0 and anteriores > 0:
        msg = (
            f"🎮 As {anteriores} promoções de hoje são as mesmas do dia anterior.\n\n"
            f"Total ativo: {total} ofertas.\nAcesse o painel para conferir."
        )
    elif novos > 0 and anteriores > 0:
        msg = (
            f"🎮 Temos {novos} NOVAS promoções hoje!\n"
            f"(E {anteriores} do dia anterior continuam ativas).\n\n"
            f"Total ativo: {total} ofertas.\nAcesse o painel para conferir."
        )
    else:
        msg = (
            f"🎮 A eShop Brasil tem {novos} promoções ativas hoje!\n\n"
            f"Total ativo: {total} ofertas.\nAcesse o painel para ver a lista."
        )

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.warning("Falha ao enviar notificação do Telegram: %s", exc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    start = time.time()
    previous_urls = load_previous_urls()

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

    save_deals(deals)

    elapsed = time.time() - start
    log.info(
        "3. Concluído em %.1fs! %d promoções processadas (%d novas, %d mantidas).",
        elapsed,
        len(deals),
        novos,
        anteriores,
    )

    send_telegram_notification(novos, anteriores, len(deals))


if __name__ == "__main__":
    main()
