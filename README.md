# Migros GPT Action API

Amaç: Bir Migros kategori URL'sini alıp kategori ürünlerini JSON olarak döndürmek.
Custom GPT daha sonra bu JSON'u Excel'e çevirebilir.

## Endpointler

- `GET /api/health`
- `POST /api/migros/category`

Örnek body:

```json
{
  "url": "https://www.migros.com.tr/dondurma-c-41b"
}
```

## Vercel'e deploy

1. Bu klasörü GitHub repository'sine yükle.
2. Vercel > Add New > Project.
3. GitHub repository'yi seç.
4. Framework Preset: Other.
5. Deploy.
6. Deploy bittikten sonra:
   `https://PROJEN.vercel.app/api/health`
   adresini aç.
7. `{"ok":true}` görürsen backend çalışıyor.

## Not

Migros endpoint yapısı değişirse `api/index.py` içindeki parser'ı güncellemek gerekir.
Bu ilk sürüm, daha önce yakalanmış Migros endpoint desenlerine göre hazırlanmıştır:
- `/rest/search/screens/<kategori-slug>`
- `/rest/products/search?category-id=...&sayfa=...&sirala=onerilenler`
