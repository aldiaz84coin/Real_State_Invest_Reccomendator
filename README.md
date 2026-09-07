# Recomendador de Inversión Inmobiliaria

Localiza terrenos infravalorados en zonas donde el suelo sube y hay atractivo
turístico, simula la inversión completa con una vivienda prefabricada y genera
el plan de negocio del alquiler turístico con su implantación en 2D y 3D.

FastAPI + SQLite, desplegable en Fly.io. Cobertura: toda España.

---

## 1. Acceso a las fuentes: qué se puede y qué no

Esta fue la primera comprobación del proyecto y condiciona todo lo demás.

| Fuente | Acceso | Vía usada |
|---|---|---|
| **Idealista** | Sí, con clave | API oficial OAuth2 (`client_credentials`). Se solicita en [developers.idealista.com](https://developers.idealista.com/access-request); el plan de desarrollo son ~100 llamadas/mes. |
| **Fotocasa** | No para consulta | Su API sirve para que las inmobiliarias *publiquen* inmuebles, no para leerlos. La lectura de mercado se vende como [Fotocasa Pro Data](https://pro.fotocasa.es/soluciones-fotocasa-pro-data/) (cubre Fotocasa + Habitaclia + Milanuncios), sólo por contrato comercial. |
| **pisos.com** | No | No hay portal de desarrolladores ni API pública, y sus condiciones prohíben la extracción automatizada. |
| **Catastro** | Sí, libre | Servicios INSPIRE: **WFS** por referencia catastral y **ATOM** para descarga masiva por municipio. Da geometría y superficie oficial de cada parcela. |
| **INE** | Sí, libre | API Tempus3 en JSON, sin clave. |
| **Ministerio de Vivienda (MIVAU)** | Sí, libre | Descargas CSV/XLSX de precio de suelo y transacciones. |
| **OpenStreetMap** | Sí, libre | Overpass (playa, montaña, esquí, turismo, patrimonio) y Nominatim. |
| **InsideAirbnb** | Sí, libre | Volcados CSV de anuncios reales de Airbnb con precio y disponibilidad. |
| **AirROI** | Sí, de pago | Complemento opcional de pago por uso. |
| **AirDNA** | Descartado | Su API sólo se comercializa con contrato *enterprise* (del orden de 50.000 $/año). |

**La app se limita a vías oficiales.** No hace scraping de ningún portal. El
hueco que dejan Fotocasa y pisos.com se cubre con Idealista (anuncios) más
Catastro e INE (geometría y precios oficiales), y queda documentado en la
propia interfaz en lugar de ocultarse.

> **Comprobación en vivo.** `GET /api/sources/health` (o la página `/fuentes`)
> sondea todas las fuentes desde el servidor y devuelve el estado real de cada
> una. Distingue tres casos que suelen confundirse: la fuente responde, la
> fuente existe pero falta tu clave, o no hay vía pública de lectura.
> **Ejecuta esto tras desplegar**: es la respuesta definitiva sobre acceso,
> porque un entorno de desarrollo con la salida restringida da falsos negativos.

---

## 2. Funcionalidades

### Localizar inversión
Busca terrenos que cumplan a la vez cuatro condiciones, y puntúa cada uno de 0 a 100:

- **Precio muy por debajo de mercado** (peso 38%). Se compara contra la
  **mediana** de €/m² de terrenos de tamaño y ubicación comparables, no contra
  la media: unas pocas fincas de lujo disparan la media y harían pasar por ganga
  un precio normal. Un descuento superior al 75% no puntúa más, porque casi
  siempre esconde un problema (sin acceso, inundable, no edificable).
- **Tendencia alcista del suelo** (27%). Combina CAGR, R² de la regresión y
  pendiente reciente sobre la serie histórica del municipio. Premia la subida
  sostenida, no la explosiva: por encima del 12% anual el suelo suele estar
  recalentado y la puntuación satura.
- **Emplazamiento** (25%). Distancia a playa, montaña, estación de esquí, puerto
  deportivo y atracciones turísticas, con datos de OpenStreetMap.
- **Dimensión adecuada** (10%). El óptimo ronda los 1.000 m²: suficiente para
  casa, terraza, aparcamiento y privacidad, sin pagar por suelo que no rinde.

### Simular la inversión
Catálogo de viviendas prefabricadas seleccionable, encabezado por el modelo
plegable de referencia y con alternativas expandibles y modulares. Calcula el
desembolso completo:

- **Compra**: precio, ITP por comunidad autónoma (o IVA + AJD si el vendedor es
  empresa), notaría, registro, gestoría, comprobación jurídica y urbanística.
- **Estudios técnicos**: levantamiento topográfico y geotécnico (exigido por el CTE).
- **Vivienda**: módulo, transporte, montaje y grúa.
- **Obra de parcela**: desbroce, nivelación, cimentación, acceso rodado, vallado.
- **Acometidas**: agua, electricidad (o solar aislada), saneamiento o fosa
  séptica, telecomunicaciones.
- **Licencias e impuestos de obra**: proyecto y dirección, ICIO, tasa de licencia,
  visado, declaración de obra nueva, AJD, primera ocupación, cédula de
  habitabilidad y alta de vivienda de uso turístico.
- **Equipamiento** y **margen de imprevistos**.

### Plan de negocio del alquiler turístico
Parte de **tarifa y ocupación reales de la zona** (InsideAirbnb, opcionalmente
AirROI), no de porcentajes inventados. Si no hay dato para la zona, usa valores
de respaldo conservadores y lo dice explícitamente.

Produce cuenta de explotación anual, estacionalidad mes a mes según el tipo de
zona (costa, montaña, ciudad o rural), proyección a diez años con amortización
del préstamo, y métricas de retorno: rentabilidad bruta y neta, *cash-on-cash*,
periodo de recuperación, VAN y TIR.

La ocupación se estima por dos vías independientes —noches inferidas de las
reseñas y hueco cerrado del calendario— y se toma **la menor**, que es la
prudente.

### Simulación 2D y 3D
Con la geometría real de la parcela (Catastro) y las dimensiones del modelo:
aplica retranqueos, calcula el área edificable y busca la mejor colocación
—maximizando holgura a linderos, orientación sur y cercanía al acceso—; sitúa
terraza, aparcamiento, camino y piscina, y comprueba ocupación y edificabilidad.

Un único motor de geometría alimenta **dos renderizadores**: un plano acotado en
SVG generado en el servidor y una vista 3D orbitable con Three.js. Si el anuncio
no tiene geometría catastral, dibuja un rectángulo equivalente a su superficie y
lo advierte.

---

## 3. Puesta en marcha

### Local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # rellena CONTACT_EMAIL y, si la tienes, la clave de Idealista

python -m scripts.seed municipios          # 116 municipios de toda España, sin red
uvicorn app.main:app --reload --port 8000
```

Abre <http://localhost:8000>. La documentación interactiva de la API está en `/docs`.

### Fly.io

```bash
fly launch --no-deploy                       # usa el fly.toml del repositorio
fly volumes create investment_data --region mad --size 1
fly secrets set CONTACT_EMAIL="tu@email.com"
fly secrets set IDEALISTA_API_KEY="..." IDEALISTA_API_SECRET="..."   # opcional
fly deploy

# Comprueba el acceso real a las fuentes, ya con salida a internet sin restricciones
curl https://<tu-app>.fly.dev/api/sources/health | jq .summary

# Carga inicial de datos
fly ssh console -C "python -m scripts.seed municipios"
fly ssh console -C "python -m scripts.seed pois"
fly ssh console -C "python -m scripts.seed alquiler"
```

**La base de datos es gratuita**: SQLite en un volumen de Fly, sin servidor
aparte. La máquina se suspende sin tráfico y arranca con la primera petición.
Si algún día necesitas Postgres o Supabase, basta con cambiar `DATABASE_URL`:
el código va contra SQLAlchemy y no asume el motor.

---

## 4. Uso

### Cargar datos

```bash
# Puntos de interés de una zona (libre, sin clave)
curl -X POST localhost:8000/api/ingest/pois \
  -H 'Content-Type: application/json' \
  -d '{"lat":36.72,"lon":-4.42,"radius_km":40}'

# Terrenos en venta (requiere clave de Idealista)
curl -X POST localhost:8000/api/ingest/listings \
  -H 'Content-Type: application/json' \
  -d '{"lat":36.72,"lon":-4.42,"radius_km":25,"min_size_m2":300,"max_price_eur":120000}'

# Puntuar todo lo cargado
curl -X POST localhost:8000/api/analyze-all

# Geometría catastral de una parcela concreta
curl -X POST localhost:8000/api/listings/1/cadastre
```

Las series de precio del INE y del MIVAU se publican como descarga, no como API
de consulta directa, así que se importan desde un CSV con columnas
`municipio`, `anio` y `eur_m2` (acepta `;` y coma decimal):

```bash
python -m scripts.seed precios descarga_mivau.csv
```

### Simular

```bash
curl -X POST localhost:8000/api/simulate -H 'Content-Type: application/json' -d '{
  "land_price_eur": 45000,
  "parcel_area_m2": 900,
  "lat": 36.7213, "lon": -4.4214,
  "model_id": "plegable-40-2dorm",
  "ccaa": "Andalucia",
  "include_pool": true, "pool_eur": 22000,
  "adr_eur": 135, "occupancy_rate": 0.58,
  "loan_amount_eur": 90000, "loan_rate": 0.045, "loan_years": 15
}'
```

O usa el formulario en `/simular`, que además dibuja el plano 2D y la vista 3D.

---

## 5. Estructura

```
app/
  sources/      Conectores externos, cada uno con su comprobación de acceso
  analysis/     Geometría, tendencia, comparación de mercado, POIs y scoring
  simulation/   Catálogo, costes, plan de negocio, implantación y render 2D
  web/          Plantillas Jinja, hoja de estilos y visor 3D
  services.py   Orquestación de todo lo anterior
scripts/seed.py Carga inicial de datos
tests/          68 tests de la lógica financiera, geométrica y de scoring
```

```bash
pytest -q      # 68 tests
```

---

## 6. Límites que conviene tener presentes

- **Los precios del catálogo son valores por defecto editables**, órdenes de
  magnitud del mercado español, no ofertas en firme. Todo importe se puede
  sobrescribir en la simulación.
- **Retranqueos, ocupación y edificabilidad** los fija la ordenanza de cada
  municipio. Los valores por defecto (3 m a linderos, 5 m a frente, 30% de
  ocupación) son los habituales en suelo urbano de baja densidad, pero hay que
  verificarlos antes de comprar.
- **En suelo rústico las posibilidades son limitadas.** Que una parcela sea
  barata suele deberse precisamente a eso. Consulta siempre el planeamiento y
  pide certificado de compatibilidad urbanística antes de firmar.
- **El alquiler turístico está restringido en varias zonas tensionadas**, con
  moratorias de nuevas licencias. La app lo advierte en cada plan de negocio.
- **Los códigos INE del fichero semilla** deberían contrastarse con el listado
  oficial del INE antes de cruzarlos con estadísticas oficiales.
- Los tipos de ITP, AJD e ICIO son los de referencia vigentes y están
  parametrizados, pero la fiscalidad es autonómica y cambia: confírmalos.

Esto es una herramienta de análisis y cribado, no asesoramiento fiscal,
urbanístico ni de inversión.
