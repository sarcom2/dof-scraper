# Research log — is there an API or bulk source?

Raw evidence behind [README §1](../README.md#1-i-checked-for-an-official-api-first).
Everything below was captured on **2026-08-01**. `dof-ingest research`
re-runs the live probes; this file is the record with full responses, including
the ones the automated probe cannot repeat.

---

## 0. Choosing the target

I looked at three targets.

| Candidate | Outcome |
|---|---|
| **CompraNet** | **Rejected — a bulk download is already available.** `compranet.hacienda.gob.mx` does not resolve from outside Mexico, but the same contracting data is published as bulk OCDS at `contratacionesabiertas.mx` (HTTP 200). Scraping a portal that offers a bulk download is the wrong answer regardless of how good the scraper is. |
| **datos.gob.mx** | **Rejected as a scraping target — it *is* an API.** Full CKAN 2.11.5 with `package_search`, `datastore`, `xloader`. Nothing here needs a scraper. Retained as a *source* to check for a DOF dataset (§2). |
| **DOF** | **Selected.** A documented API that does not work, no usable bulk export, real multi-dimensional enumeration (date × edition), a robots.txt that constrains the design, and ugly HTML. |

```console
$ curl -s https://www.datos.gob.mx/api/3/action/status_show
{"help": "...", "success": true, "result": {"site_title": "datos.gob.mx",
 "ckan_version": "2.11.5", "extensions": ["envvars","stats","resource_proxy",
 "datastore","xloader","text_view","image_view","datatables_view","atdt"]}}

$ curl -sS -o /dev/null -w "%{http_code} %{url_effective}\n" -L https://compranet.hacienda.gob.mx/
000 https://compranet.hacienda.gob.mx/          # does not resolve
$ curl -sS -o /dev/null -w "%{http_code} %{url_effective}\n" -L https://contratacionesabiertas.mx/
200 https://www.contratacionesabiertas.mx/      # bulk OCDS — no scraper needed
```

---

## 1. The official API — documented, and broken

SEGOB publishes a *Datos abiertos* page at
<https://sidof.segob.gob.mx/datos_abiertos> advertising "Consumo de
WebServices" with services for **Diario**, **Documentos**, **Indicadores** and
**Notas**. A working one would make this scraper unnecessary.

### It answers `200 OK` with nothing

```console
$ curl -s https://sidof.segob.gob.mx/dof/sidof/diarios/31-07-2026
{"messageCode":200,"response":"OK","ListaDiarios":[],"FechasSinPublicacion":[]}

$ curl -s https://sidof.segob.gob.mx/dof/sidof/diarios/30-07-2026
{"messageCode":200,"response":"OK","ListaDiarios":[],"FechasSinPublicacion":[]}

$ curl -s https://sidof.segob.gob.mx/dof/sidof/diarios/15-07-2026
{"messageCode":200,"response":"OK","ListaDiarios":[],"FechasSinPublicacion":[]}
```

Note that `FechasSinPublicacion` is *also* empty — the API is not claiming
these were non-publication days. It is claiming nothing at all.

### The same dates are not empty

```console
$ curl -s "https://www.dof.gob.mx/index_111.php?year=2026&month=07&day=31&edicion=MAT" \
  | grep -c "nota_detalle.php?codigo"
27
```

27 notices in the HTML, 0 in the API, same date.

### It is not my network — the API host is up

```console
$ curl -s https://sidof.segob.gob.mx/dof/sidof/indicadores/01-08-2026
{"messageCode":200,"response":"OK","ListaIndicadores":[
 {"codIndicador":39556,"codTipoIndicador":159,"fecha":"01-08-2026","valor":"8.794229"}]}
```

The `indicadores` service on the same host, same path prefix, returns live
data. Probing it is what makes the `diarios` result evidence rather than a
guess; that's why `research` runs both.

### Date-format variants, ruled out

```console
$ .../diarios/2026-07-31   -> {"ListaDiarios":[]}      # same
$ .../diarios/31-07-26     -> {"ListaDiarios":[]}      # same
$ .../diarios/31/07/2026   -> 404 Not Found            # wrong shape, correctly rejected
```

The endpoint distinguishes a malformed path (404) from a well-formed one
(200 + empty), so `dd-mm-yyyy` is being accepted and simply returns nothing.

### The other documented services

```console
$ curl -s https://sidof.segob.gob.mx/dof/sidof/notas/5788051
{"messageCode":400,"response":"BAD_REQUEST"}

$ curl -s https://sidof.segob.gob.mx/dof/sidof/diarios/documentos/31-07-2026
{"messageCode":404,"response":"El Servicio que deseas consultar no existe"}
$ curl -s https://sidof.segob.gob.mx/dof/sidof/documentos/31-07-2026
{"messageCode":404,"response":"El Servicio que deseas consultar no existe"}
$ curl -s https://sidof.segob.gob.mx/dof/sidof/notas/fecha/31-07-2026
{"messageCode":404,"response":"El Servicio que deseas consultar no existe"}
```

`notas` exists but rejects the documented argument; `documentos` is advertised
on the open-data page but is not routed at any path shape I could find.
`apiStatus` — the page that would document the correct shapes — times out:

```console
$ curl -sS -m 25 https://sidof.segob.gob.mx/apiStatus
curl: (28) Operation timed out after 25001 milliseconds with 0 bytes received
```

**Conclusion:** the only working service is `indicadores`, which returns
exchange rates, not notices.

---

## 2. datos.gob.mx — the wrong dataset

The national open-data portal has a live CKAN API, so it looked the most
promising.

```console
$ curl -sG --data-urlencode 'q=diario oficial de la federacion' --data 'rows=5' \
    https://www.datos.gob.mx/api/3/action/package_search
count: 5
  - Resumen del Diario Oficial de la Federación (DOF) | ['CSV']
  - Decretos Expropiatorios                            | ['CSV']
  - Aguas subterráneas                                 | ['CSV','SHP','ZIP','SHP']
  - Declaraciones y pagos                              | ['CSV','CSV','CSV','CSV']
  - Supervisión de Decretos Expropiatorios             | ['CSV']
```

The one that sounds right:

```console
titulo: Resumen del Diario Oficial de la Federación (DOF)
notas:  "Contiene disposiciones vigentes e históricas que provienen de la fuente
         de origen... emitidas por este servicio nacional en el ejercicio de sus
         atribuciones."
res: CSV "Lista de publicaciones del DOF (diciembre, 2025)"
     -> https://repodatos.atdt.gob.mx/api_update/senasica/
        resumen_diario_oficial_federacion_dof/resumen_del_DOF_diciembre_2025.csv
```

Read the URL path: **`senasica`**. This is the National Service for Agri-Food
Health publishing *its own* DOF notices — "este servicio nacional" in the
description is SENASICA referring to itself, not the DOF. One agency, one
month, last refreshed December 2025.

**Not the corpus.** Closed.

### A note on why `research` cannot re-run this probe

```console
$ curl -s https://www.datos.gob.mx/robots.txt
User-agent: *
Disallow: /dataset/rate/
Disallow: /revision/
Disallow: /dataset/*/history
Disallow: /api/
Crawl-Delay: 10
```

`/api/` is disallowed. The findings above came from a handful of one-off
requests during research, which I consider a reasonable non-crawling use of a
public read-only JSON API. The automated probe in `dof-ingest research` obeys
robots.txt and therefore **does not query CKAN**; it reports the robots verdict
and cites this file instead. A politeness rule that only applies when it’s free
isn’t a rule.

---

## 3. sitemap.xml — a fossil

```console
$ curl -s https://www.dof.gob.mx/sitemap.xml | head -20
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.google.com/schemas/sitemap/0.84" ...>
  <url>
    <loc>http://diariooficial.segob.gob.mx/</loc>
    <lastmod>2007-06-13T14:40:58-06:00</lastmod>
  </url>
  <url>
    <loc>http://diariooficial.segob.gob.mx/olvido_clave.php</loc>
    <lastmod>2007-06-13T14:40:58-06:00</lastmod>
  </url>
  ...
```

586 URLs, every `lastmod` in 2007, every `loc` pointing at
`diariooficial.segob.gob.mx` — a hostname the DOF no longer uses. Login pages
and site maps, no notices. Closed.

```console
$ curl -sI https://www.dof.gob.mx/rss.xml   | head -1
HTTP/1.1 404 Not Found
$ curl -sI https://sidof.segob.gob.mx/sitemap.xml | head -1
HTTP/1.1 404 Not Found
```

---

## 4. Conditional GET — also unavailable

Before settling on content hashing I checked whether the cheap layer above it
(`ETag` / `Last-Modified` → `304 Not Modified`) was on the table.

```console
$ curl -sI "https://www.dof.gob.mx/index_111.php?year=2026&month=07&day=31"
Expires: Thu, 19 Nov 1981 08:52:00 GMT
Cache-Control: no-store, no-cache, must-revalidate, post-check=0, pre-check=0
Pragma: no-cache
Set-Cookie: DOF_WEB=lho7dqln3thp9em9998oks7rj7; path=/
```

No `ETag`, no `Last-Modified`, and PHP's default anti-caching headers. There is
no way to ask "has this changed?" without downloading it, which is exactly why
change detection has to happen *after* the fetch, on canonicalised content.
It also rules out the session cookie as anything hashable — it is new on every
request.

---

## 5. Discovered while building, not while researching

### `edicion` is mandatory

```console
$ base="https://www.dof.gob.mx/index_111.php?year=2026&month=07&day=31"
$ curl -s "$base"              | grep -c nota_detalle.php?codigo   # 4
$ curl -s "$base&edicion=VES"  | grep -c nota_detalle.php?codigo   # 4
$ curl -s "$base&edicion=MAT"  | grep -c nota_detalle.php?codigo   # 27
```

Omitting the parameter silently returns the vespertina and hides 27 of the
day's 31 notices. Always enumerate editions explicitly.

### The notice text is behind an iframe

`https://sidof.segob.gob.mx/notas/{codigo}` does not contain the notice:

```console
$ grep -o 'iframe[^>]*' nota_5788395.html
iframe src="/notas/docFuente/5788395" frameborder="0" id="frameContainer" ...
```

`/notas/docFuente/{codigo}` returns clean UTF-8 with the full legal text and
none of the site chrome. `/notas/getDoc/{codigo}` returns a binary
`application/doc` (OLE compound file) and is not useful.

That sidof's robots.txt names 18 IDs under **both** `/notas/{id}` *and*
`/notas/docFuente/{id}` is what confirms `docFuente` is the canonical content
route rather than an implementation detail.

### The blocked notices are served normally

```console
$ curl -sS -o /dev/null -w "%{http_code}\n" \
    https://sidof.segob.gob.mx/notas/docFuente/5381640
200
```

There is no technical barrier. robots.txt is the entire reason we skip them.

Curiously, notice `5381640` — which robots.txt blocks by name, citing
`fecha=11/02/2015` — no longer appears in that date's index in any edition:

```console
$ for e in MAT VES EXT; do
    curl -s "https://www.dof.gob.mx/index_111.php?year=2015&month=02&day=11&edicion=$e" \
    | grep -c "codigo=5381640"
  done
0
0
0
```

The disallow outlived the listing. We still honour it.

---

## Verdict

| Door | Status |
|---|---|
| SIDOF `/diarios` WebService | 200 OK, empty, every date — **broken** |
| SIDOF `/notas`, `/documentos` | 400 / 404 |
| SIDOF `/indicadores` | Works — but returns exchange rates |
| datos.gob.mx CKAN | One agency's monthly CSV, not the corpus |
| sitemap.xml | 2007 fossil pointing at a dead host |
| rss.xml | 404 |
| Conditional GET | No `ETag`, no `Last-Modified` |
| Per-edition PDFs | Unstructured; the HTML already has the metadata |

Scraping the HTML index is what's left. **Last resort, not the first thing to
try** — and the moment `dof-ingest research` reports `/diarios` returning data,
this project should be deleted in favour of it.
