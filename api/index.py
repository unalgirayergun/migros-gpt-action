from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, parse_qs, quote
import httpx, re, json, asyncio
from io import BytesIO
from datetime import date
from openpyxl import Workbook

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



def collect_category_id_candidates(obj):
    candidates = []

    def add(v, source="json"):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        s = str(v).strip()
        if s.isdigit() and 1 <= len(s) <= 20:
            item = {"id": s, "source": source}
            if item not in candidates:
                candidates.append(item)

    # Strongly named category identifier fields.
    for d in walk(obj):
        for k, v in d.items():
            kl = str(k).lower()
            if kl in {
                "categoryid", "category_id", "category-id",
                "categorycode", "category_code",
                "legacycategoryid", "legacy_category_id",
                "sapcategoryid", "sap_category_id"
            }:
                add(v, kl)

    # Embedded category-id query strings.
    raw = json.dumps(obj, ensure_ascii=False)
    for m in re.finditer(r'category-id(?:%3D|=)(\d+)', raw, re.I):
        add(m.group(1), "embedded_url")

    # Category-like objects often carry a short legacy numeric `id`.
    for d in walk(obj):
        keys = {str(k).lower() for k in d.keys()}
        if "id" not in keys:
            continue
        label = " ".join(
            str(pick(d, n) or "")
            for n in ("name", "title", "slug", "prettyName", "seoName", "url", "type")
        ).lower()
        if any(token in label for token in ("kategori", "category", "-c-", "dondurma")):
            add(pick(d, "id"), "category_object_id")

    return candidates


def metadata_int(obj, names):
    v = find_first_value(obj, names)
    try:
        return int(v)
    except Exception:
        return None


async def choose_best_category_id(client, first, expected_hits=None, expected_pages=None):
    candidates = collect_category_id_candidates(first)

    # Keep probing bounded to avoid hammering Migros if the screen JSON contains many IDs.
    candidates = candidates[:30]
    probes = []

    async def probe(candidate):
        cid = candidate["id"]
        url = (
            "https://www.migros.com.tr/rest/products/search"
            f"?category-id={cid}&sayfa=1&sirala=onerilenler"
        )
        try:
            data = await get_json(client, url)
            hits = metadata_int(data, ["hitCount", "hit_count", "totalCount", "total_count"])
            pages = metadata_int(data, ["pageCount", "page_count", "totalPages", "total_pages"])
            products = extract_product_dicts(data)

            score = 0
            if expected_hits is not None and hits == expected_hits:
                score += 100
            if expected_pages is not None and pages == expected_pages:
                score += 60
            if products:
                score += min(len(products), 30)
            if hits:
                score += min(hits / 1000, 10)

            return {
                "id": cid,
                "source": candidate["source"],
                "hits": hits,
                "pages": pages,
                "product_count": len(products),
                "score": score,
            }
        except Exception as e:
            return {
                "id": cid,
                "source": candidate["source"],
                "error": str(e),
                "score": -1,
            }

    if candidates:
        probes = await asyncio.gather(*(probe(c) for c in candidates))
        probes = sorted(probes, key=lambda x: x.get("score", -1), reverse=True)

    best = probes[0]["id"] if probes and probes[0].get("score", -1) >= 0 else None
    return best, probes

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

async def _migros_category_by_url(url: str, include_products: bool = True):
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

        # Discover the product-search category id. Migros screen payloads can
        # contain several different category identifiers; probe candidates and
        # prefer the one whose pagination metadata matches the screen response.
        category_id = None
        category_id_probes = []

        if search_url:
            parsed = urlparse(search_url)
            qs = parse_qs(parsed.query)
            embedded_id = (qs.get("category-id") or qs.get("categoryId") or [None])[0]
            if embedded_id:
                category_id = str(embedded_id)

        if not category_id:
            try:
                expected_hits = int(hit_count) if hit_count is not None else None
            except Exception:
                expected_hits = None
            try:
                expected_pages = int(page_count) if page_count is not None else None
            except Exception:
                expected_pages = None

            category_id, category_id_probes = await choose_best_category_id(
                client,
                first,
                expected_hits=expected_hits,
                expected_pages=expected_pages,
            )

        # Final HTML fallback if the API payload did not expose usable candidates.
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

        result = {
            "source_category_url": req_url,
            "category_slug": slug,
            "category_id": category_id,
            "category_id_probes": category_id_probes,
            "page_count_reported": page_count,
            "hit_count_reported": hit_count,
            "rows_extracted": len(all_products),
            "unique_products": len(unique),
            "page_errors": page_errors,
        }
        if include_products:
            result["products"] = unique
        return result

@app.post("/api/migros/category")
async def migros_category_post(req: CategoryRequest):
    result = await _migros_category_by_url(req.url, include_products=False)
    if result.get("page_errors"):
        result["export_ready"] = False
        result["download_url"] = None
    else:
        result["export_ready"] = True
        result["download_url"] = (
            "https://migros-gpt-action.vercel.app/api/migros/export?url="
            + quote(req.url, safe="")
        )
    return result

@app.get("/api/migros/category")
async def migros_category_get(url: str):
    return await _migros_category_by_url(url, include_products=True)

def build_excel_bytes(result):
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"

    headers = [
        "SKU",
        "Product ID",
        "Product Name",
        "Brand",
        "Category",
        "Regular Price (TL)",
        "Shown Price (TL)",
        "Discount Rate",
        "Unit Price",
        "Status",
        "Pretty Name",
        "Image URL",
        "Source API URL",
    ]
    ws.append(headers)

    for p in result.get("products", []):
        ws.append([
            p.get("sku"),
            p.get("product_id"),
            p.get("product_name"),
            p.get("brand"),
            p.get("category"),
            p.get("regular_price_tl"),
            p.get("shown_price_tl"),
            p.get("discount_rate"),
            p.get("unit_price"),
            p.get("status"),
            p.get("pretty_name"),
            p.get("image_url"),
            p.get("source_api_url"),
        ])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    widths = {
        "A": 16, "B": 20, "C": 55, "D": 24, "E": 24,
        "F": 20, "G": 20, "H": 16, "I": 24, "J": 16,
        "K": 55, "L": 55, "M": 70,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width

    for row in ws.iter_rows(min_row=2, min_col=6, max_col=7):
        for cell in row:
            cell.number_format = '#,##0.00'

    out = BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@app.get("/api/migros/export")
async def migros_export(url: str):
    result = await _migros_category_by_url(url, include_products=True)

    if result.get("page_errors"):
        raise HTTPException(
            502,
            detail={
                "message": "Some Migros pages could not be retrieved; export was not created.",
                "page_errors": result.get("page_errors"),
            },
        )

    excel = build_excel_bytes(result)
    slug = result.get("category_slug") or "category"
    filename = f"migros_{slug}_{date.today().isoformat()}.xlsx"

    return StreamingResponse(
        excel,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )

