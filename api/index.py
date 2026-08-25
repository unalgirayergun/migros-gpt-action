from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, parse_qs
import httpx, re, json, asyncio

app = FastAPI(title="Migros GPT Action API", version="0.1.0")

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "tr",
    "user-agent": "Mozilla/5.0 (compatible; MigrosGPTAction/0.1)"
}

class CategoryRequest(BaseModel):
    url: str

def category_slug(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        raise ValueError("Kategori URL'si geçersiz.")
    return path.split("/")[-1]

def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)

def find_first_value(obj, keys):
    keys_l = {k.lower() for k in keys}
    for d in walk(obj):
        for k, v in d.items():
            if str(k).lower() in keys_l and v not in (None, "", [], {}):
                return v
    return None

def find_product_search_url(obj):
    raw = json.dumps(obj, ensure_ascii=False)
    m = re.search(r'https?://www\.migros\.com\.tr/rest/products/search\?[^"\\]+', raw)
    if m:
        return m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    m = re.search(r'/rest/products/search\?[^"\\]+', raw)
    if m:
        return "https://www.migros.com.tr" + m.group(0).replace("\\u0026", "&").replace("\\/", "/")
    return None


def find_category_id(obj, slug=None):
    # 1) Directly named category id fields anywhere in the JSON.
    direct_keys = {
        "categoryid", "category_id", "category-id",
        "categorycode", "category_code"
    }
    for d in walk(obj):
        for k, v in d.items():
            if str(k).lower() in direct_keys and v not in (None, "", [], {}):
                if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
                    return str(int(v)) if isinstance(v, float) else str(v)

    # 2) Any embedded URL/string containing category-id=...
    raw = json.dumps(obj, ensure_ascii=False)
    m = re.search(r'category-id(?:%3D|=)(\d+)', raw, re.I)
    if m:
        return m.group(1)

    # 3) Category-looking objects whose slug/name resembles the requested category.
    slug_base = None
    if slug:
        slug_base = slug.split("-c-")[0].replace("-", " ").lower()

    for d in walk(obj):
        keys = {str(k).lower() for k in d.keys()}
        if "id" not in keys:
            continue
        ident = pick(d, "id")
        if not (isinstance(ident, (int, float)) or (isinstance(ident, str) and ident.isdigit())):
            continue

        text_fields = " ".join(
            str(pick(d, n) or "")
            for n in ("name", "title", "slug", "prettyName", "seoName", "url")
        ).lower()

        if slug_base and slug_base and slug_base in text_fields:
            return str(int(ident)) if isinstance(ident, float) else str(ident)

    return None

def find_category_id_in_html(html):
    patterns = [
        r'"categoryId"\s*:\s*"?(\d+)"?',
        r'"category_id"\s*:\s*"?(\d+)"?',
        r'category-id(?:=|%3D)(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None

def product_score(d):
    keys = {str(k).lower() for k in d.keys()}
    score = 0
    for group in [
        {"sku", "productid", "product_id", "id"},
        {"name", "productname", "product_name"},
        {"price", "regularprice", "shownprice", "saleprice"},
        {"brand", "brandname"},
    ]:
        if keys & group:
            score += 1
    return score

def extract_product_dicts(obj):
    candidates = []
    for d in walk(obj):
        if product_score(d) >= 3:
            candidates.append(d)
    return candidates

def pick(d, *names):
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None

def normalize_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        # Migros listing payloads mix TL decimals (e.g. 249.0) with
        # integer minor units for lower prices (e.g. 5000 -> 50.00 TL).
        return round(v / 100, 2) if v >= 1000 else float(v)
    if isinstance(v, str):
        s = v.strip().replace("TL", "").replace("₺", "").replace(" ", "")
        try:
            if "," in s and "." in s:
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", ".")
            return float(s)
        except Exception:
            return v
    return v

def normalize_product(d, page, source_url):
    sku = pick(d, "sku", "stockCode", "code")
    product_id = pick(d, "productId", "product_id", "id")
    name = pick(d, "name", "productName", "product_name", "title")
    brand = pick(d, "brand", "brandName")
    category = pick(d, "category", "categoryName")
    regular = pick(d, "regularPrice", "originalPrice", "listPrice", "price")
    shown = pick(d, "shownPrice", "salePrice", "discountedPrice", "price")
    discount = pick(d, "discountRate", "discountPercent", "discount")
    unit_price = pick(d, "unitPrice", "unit_price")
    status = pick(d, "status", "saleStatus")
    pretty = pick(d, "prettyName", "slug", "seoName")
    image = pick(d, "imageUrl", "image", "imageURL")

    if isinstance(brand, dict):
        brand = pick(brand, "name", "title")
    if isinstance(category, dict):
        category = pick(category, "name", "title")
    if isinstance(image, dict):
        image = pick(image, "url", "src")

    return {
        "page": page,
        "sku": str(sku) if sku is not None else None,
        "product_id": str(product_id) if product_id is not None else None,
        "product_name": name,
        "brand": brand,
        "category": category,
        "regular_price_tl": normalize_price(regular),
        "shown_price_tl": normalize_price(shown),
        "discount_rate": discount,
        "unit_price": unit_price,
        "status": status,
        "pretty_name": pretty,
        "image_url": image,
        "source_api_url": source_url,
    }

def dedupe(products):
    out, seen = [], set()
    for p in products:
        key = p.get("product_id") or p.get("sku") or (
            p.get("product_name"), p.get("shown_price_tl")
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out

async def get_json(client, url):
    r = await client.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

@app.get("/api/health")
async def health():
    return {"ok": True}

async def _migros_category_by_url(url: str):
    req_url = url
    try:
        slug = category_slug(url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    screen_url = f"https://www.migros.com.tr/rest/search/screens/{slug}"

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            first = await get_json(client, screen_url)
        except Exception as e:
            raise HTTPException(502, f"Migros kategori verisi alınamadı: {e}")

        page_count = find_first_value(first, ["pageCount", "page_count", "totalPages", "total_pages"])
        hit_count = find_first_value(first, ["hitCount", "hit_count", "totalCount", "total_count"])

        search_url = find_product_search_url(first)
        all_products = []

        # The first "screen" response usually already contains page-1 products.
        first_candidates = extract_product_dicts(first)
        all_products.extend(normalize_product(x, 1, screen_url) for x in first_candidates)

        # Discover category-id. The screen response does not always expose a
        # literal /rest/products/search URL, so use several fallbacks.
        category_id = None

        if search_url:
            parsed = urlparse(search_url)
            qs = parse_qs(parsed.query)
            category_id = (qs.get("category-id") or qs.get("categoryId") or [None])[0]

        if not category_id:
            category_id = find_category_id(first, slug)

        if not category_id:
            try:
                page_html = await client.get(req_url, headers=HEADERS, timeout=30)
                if page_html.status_code < 400:
                    category_id = find_category_id_in_html(page_html.text)
            except Exception:
                pass

        try:
            pages = int(page_count) if page_count else 1
        except Exception:
            pages = 1

        page_errors = []

        if category_id and pages > 1:
            async def fetch_page(page):
                url = (
                    "https://www.migros.com.tr/rest/products/search"
                    f"?category-id={category_id}&sayfa={page}&sirala=onerilenler"
                )
                data = await get_json(client, url)
                return page, url, data

            results = await asyncio.gather(
                *(fetch_page(p) for p in range(2, pages + 1)),
                return_exceptions=True
            )
            for page_no, item in zip(range(2, pages + 1), results):
                if isinstance(item, Exception):
                    page_errors.append({"page": page_no, "error": str(item)})
                    continue
                page, url, data = item
                candidates = extract_product_dicts(data)
                if not candidates:
                    page_errors.append({"page": page, "error": "No product objects found"})
                for x in candidates:
                    all_products.append(normalize_product(x, page, url))
        elif pages > 1 and not category_id:
            page_errors.append({
                "page": None,
                "error": "Category ID could not be discovered from screen response or category page"
            })

        unique = dedupe([p for p in all_products if p.get("product_name")])

        return {
            "source_category_url": url,
            "category_slug": slug,
            "category_id": category_id,
            "page_count_reported": page_count,
            "hit_count_reported": hit_count,
            "rows_extracted": len(all_products),
            "unique_products": len(unique),
            "page_errors": page_errors,
            "products": unique,
        }

@app.post("/api/migros/category")
async def migros_category_post(req: CategoryRequest):
    return await _migros_category_by_url(req.url)

@app.get("/api/migros/category")
async def migros_category_get(url: str):
    return await _migros_category_by_url(url)

