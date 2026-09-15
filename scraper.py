"""
Scraper de promoções da Nintendo eShop Brasil.

Estratégia:
1. O índice Algolia da Nintendo (`store_game_pt_br`) tem um limite de segurança
   `paginationLimitedTo` de 1000 resultados por query (hitsPerPage * page nunca
   pode passar de 1000). Isso significa que, para um prefixo muito comum (ex:
   "a", "e", "s"), paginar (page=0,1,2...) NÃO funciona: a partir da página 1
   a API já rejeita a requisição, então boa parte do catálogo era perdida.
2. Para contornar isso, ao invés de paginar, o script faz "query splitting
   recursivo": quando uma busca por um prefixo retorna mais que 1000 hits (ou
   seja, está no limite e pode estar cortando resultados), o prefixo é
   subdividido acrescentando mais um caractere (a-z0-9) e cada sub-prefixo é
   buscado separadamente, recursivamente, até que cada busca traga menos que
   o limite ou até uma profundidade máxima de segurança.
3. Uma execução de diagnóstico (rodada uma vez, no início) descobriu que o
   índice NÃO expõe um facet chamado "hasDiscount" (era o que a versão
   anterior deste script usava — por isso zerava tudo). O objeto de cada
   hit já vem com um campo "price" embutido (ex:
   {"finalPrice": 18.5, "regPrice": 49.9, "discounted": true, "salePrice": 18.5,
   "amountOff": 31.4, "percentOff": 63}), que é a própria fonte que a loja
   usa para mostrar o preço. Ou seja, não é mais preciso consultar uma
   segunda API de preços separada: o próprio resultado da busca já diz se
   o item está em promoção e qual o preço.
4. Como não temos garantia de que "price.discounted" está de fato
   configurado como facet filtrável no índice (isso pode mudar sem aviso,
   como já aconteceu uma vez), o script tenta filtrar direto na Algolia
   (mais rápido) mas confirma sempre no lado do cliente (Python) se
   price.discounted é realmente true antes de considerar algo uma oferta.
   Se o filtro do lado da Algolia não funcionar por qualquer motivo, o
   script detecta isso automaticamente e cai para uma varredura do
   catálogo inteiro, filtrando tudo aqui mesmo — mais lento, porém à prova
   de mudanças futuras de schema.
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

# Nome do facet que (tentamos) usar para filtrar só itens com desconto direto
# na Algolia. Descoberto via diagnóstico a partir do campo "price.discounted"
# presente em cada hit. Se não estiver configurado como facet no índice, o
# script detecta e cai automaticamente para varredura completa + filtro local.
DISCOUNT_FACET = "price.discounted:true"

DATA_PATH = "data/deals.json"

# O índice limita hitsPerPage*page a este valor (paginationLimitedTo padrão
# da Algolia é 1000). Usamos hitsPerPage no máximo permitido e nunca paginamos
# além da página 0 — em vez disso, refinamos a query quando o teto é atingido.
ALGOLIA_HITS_PER_PAGE = 1000
# Se uma busca retornar >= a este número de hits, consideramos que pode estar
# sendo cortada pelo teto da Algolia e precisamos refinar (subdividir) a query.
SPLIT_THRESHOLD = 1000
# Profundidade máxima de refinamento (tamanho máximo do prefixo de busca).
# Se o filtro de desconto funcionar na Algolia, poucos caracteres bastam
# (o universo de itens em promoção é pequeno). Se cairmos para varredura do
# catálogo inteiro, prefixos de 3-4 caracteres costumam ser suficientes;
# 6 é uma rede de segurança contra loop infinito em casos extremos.
MAX_PREFIX_DEPTH = 6

ALPHABET = list("abcdefghijklmnopqrstuvwxyz0123456789")

MAX_WORKERS = int(os.getenv("SCRAPER_MAX_WORKERS", "8"))
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

def algolia_query(session: requests.Session, prefix: str, use_facet: bool) -> dict:
    """Executa uma única query na Algolia para o prefixo informado."""
    extra: dict = {}
    if use_facet:
        extra["facetFilters"] = json.dumps([[DISCOUNT_FACET]])

    params = urllib.parse.urlencode(
        {
            "query": prefix,
            "hitsPerPage": ALGOLIA_HITS_PER_PAGE,
            "page": 0,
            **extra,
        }
    )
    payload = {"requests": [{"indexName": ALGOLIA_INDEX, "params": params}]}

    resp = session.post(
        ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    body = resp.json()
    result = body["results"][0]

    # A API multi-query da Algolia responde HTTP 200 mesmo quando uma query
    # individual falha; o erro vem embutido no próprio resultado (sem "hits").
    # Isso fazia versões antigas deste script tratarem erros silenciosamente
    # como "zero resultados".
    if "hits" not in result:
        raise RuntimeError(f"Algolia retornou erro para a query (prefixo={prefix!r}): {result}")

    return result


def extract_deal(hit: dict) -> dict | None:
    """
    Verifica (sempre no lado do cliente, independente de facet) se o hit é
    de fato uma oferta com desconto ativo, usando o campo "price" embutido
    no próprio resultado da busca — a mesma fonte que a loja usa.
    """
    price_info = hit.get("price") or {}
    if not price_info.get("discounted"):
        return None

    sale_price = price_info.get("salePrice")
    reg_price = price_info.get("regPrice")
    if sale_price is None or reg_price is None:
        return None

    try:
        sale_price = float(sale_price)
        reg_price = float(reg_price)
    except (TypeError, ValueError):
        return None

    if sale_price <= 0 or reg_price <= 0 or sale_price >= reg_price:
        return None

    game_id = hit.get("nsuid") or hit.get("objectID") or hit.get("sku")
    if not game_id:
        return None

    raw_url = hit.get("url", "") or ""
    if not raw_url.startswith("http"):
        raw_url = f"https://www.nintendo.com{raw_url if raw_url.startswith('/') else '/' + raw_url}"
    clean_url = raw_url.replace("/pt-br/pt-br/", "/pt-br/")

    image = (
        hit.get("productImage")
        or hit.get("productImageSquare")
        or hit.get("productGallery")
        or ""
    )
    if isinstance(image, list):
        image = image[0] if image else ""

    return {
        "_id": str(game_id),
        "title": hit.get("title", "Desconhecido"),
        "price": sale_price,
        "old_price": reg_price,
        "url": clean_url,
        "image": image,
    }


_facet_usable: bool | None = None
_facet_lock = Lock()


def facet_is_usable(session: requests.Session) -> bool:
    """
    Testa uma vez, no início da execução, se o filtro de desconto funciona
    de fato na Algolia (facet configurado + resultados batendo com o que
    encontramos checando price.discounted manualmente). Se não bater, a
    varredura cai para modo "catálogo inteiro + filtro local", que é mais
    lento mas não depende de nenhum facet específico continuar existindo.
    """
    global _facet_usable
    with _facet_lock:
        if _facet_usable is not None:
            return _facet_usable

        probe_prefix = "a"
        try:
            with_facet = algolia_query(session, probe_prefix, use_facet=True)
            hits = with_facet.get("hits", [])
            nb_hits = with_facet.get("nbHits", 0)
            confirmed = sum(1 for h in hits if extract_deal(h))

            log.info(
                "[diagnóstico] facet '%s': nbHits=%s, %d/%d hits retornados são "
                "realmente descontos confirmados.",
                DISCOUNT_FACET,
                nb_hits,
                confirmed,
                len(hits),
            )

            # Só confiamos no facet se ele trouxe resultados e a imensa
            # maioria deles se confirma como desconto real.
            _facet_usable = nb_hits > 0 and len(hits) > 0 and confirmed >= len(hits) * 0.9
        except Exception as exc:  # noqa: BLE001
            log.warning("[diagnóstico] facet '%s' falhou (%s); usando varredura completa.", DISCOUNT_FACET, exc)
            _facet_usable = False

        if not _facet_usable:
            log.warning(
                "Filtro de desconto da Algolia não é confiável agora — "
                "a varredura vai passar pelo catálogo inteiro e filtrar "
                "os descontos aqui mesmo (mais lento, porém mais robusto)."
            )
        return _facet_usable


def collect_deals() -> dict:
    """
    Varre o catálogo (só o subconjunto com desconto, se o facet funcionar;
    o catálogo inteiro, caso contrário), subdividindo recursivamente
    qualquer prefixo cuja busca esbarre no teto de 1000 resultados da
    Algolia, garantindo que nenhuma oferta fique de fora.
    """
    deals: dict[str, dict] = {}
    lock = Lock()
    session = build_session()

    use_facet = facet_is_usable(session)

    pending = list(ALPHABET)
    seen_prefixes: set[str] = set()
    queries_done = 0

    def process_prefix(prefix: str) -> list[str]:
        """Consulta um prefixo; retorna sub-prefixos a explorar se necessário."""
        try:
            result = algolia_query(session, prefix, use_facet=use_facet)
        except Exception as exc:  # noqa: BLE001 - resiliência a qualquer falha de rede
            log.warning("Falha ao consultar prefixo '%s': %s — tentando de novo depois", prefix, exc)
            return [prefix] if len(prefix) < MAX_PREFIX_DEPTH else []

        hits = result.get("hits", [])
        nb_hits = result.get("nbHits", len(hits))

        with lock:
            for hit in hits:
                deal = extract_deal(hit)
                if deal:
                    deals[deal.pop("_id")] = deal

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

    log.info(
        "1. Iniciando varredura exaustiva (%s, query-splitting recursivo)...",
        "usando filtro de desconto da Algolia" if use_facet else "catálogo inteiro + filtro local",
    )
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
        "Varredura concluída: %d queries executadas, %d ofertas com desconto confirmado.",
        queries_done,
        len(deals),
    )
    return deals


def fetch_deals() -> list[dict]:
    return list(collect_deals().values())


# --------------------------------------------------------------------------
# Etapa 2: diff com o histórico + persistência
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
# Etapa 3: notificação no Telegram
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
        "2. Concluído em %.1fs! %d promoções processadas (%d novas, %d mantidas).",
        elapsed,
        len(deals),
        novos,
        anteriores,
    )

    send_telegram_notification(novos, anteriores, len(deals))


if __name__ == "__main__":
    main()
