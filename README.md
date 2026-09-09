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
| **InsideAirbnb** | Sí, libre | Volcados CSV de anuncios reales de Airbnb con precio y disponibilidad. **Sólo cubre una docena de ciudades españolas.** |
| **INE · viviendas turísticas** | Sí, libre | Estadística experimental: viviendas y plazas de alquiler turístico **de todos los municipios de España**, medidas rastreando las plataformas. Tablas Tempus3 39363 (municipios) y 39364 (provincias). |
| **AirROI** | Sí, de pago | Complemento opcional de pago por uso. |
| **AirDNA** | Descartado | Su API sólo se comercializa con contrato *enterprise* (del orden de 50.000 $/año). |
| **SEPES, ADIF, INVIED, Patrimonio del Estado** | Sí, libre, vía BOE | Los cuatro organismos que más suelo sacan al mercado en España. No tienen API y sus webs son noticias y pliegos en PDF, pero están obligados a anunciar cada venta en el Boletín: se leen por ahí. |

### Subastas del BOE: por la API documentada, no raspando el buscador

El buscador de `subastas.boe.es` **no es una API**: no está documentado, sus
parámetros hay que deducirlos leyendo el formulario, y devolvía siempre la
misma página aunque el HTTP fuera 200. La Agencia Estatal BOE sí publica una
[API de datos abiertos](https://www.boe.es/datosabiertos/api/api.php),
documentada, sin clave y sin cuota, y por ahí pasa toda venta de patrimonio
público: para celebrarse tiene que anunciarse antes en el Boletín.

- Las **judiciales** se publican en la sección IV (Administración de Justicia).
- Las **administrativas** —Agencia Tributaria, Seguridad Social, ayuntamientos—
  en la sección V (Anuncios).

`GET /datosabiertos/api/boe/sumario/AAAAMMDD` devuelve el sumario del día, y de
ahí salen **dos** fuentes distintas, porque no todas las ventas se celebran
igual.

### Por qué el Boletín daba cien anuncios al día y cero candidatas

El descubrimiento hacía un solo camino:

    sumario del BOE  →  identificador SUB-…  →  ficha del Portal de Subastas

y sólo llevan identificador `SUB-` las subastas **electrónicas**: las
judiciales, las notariales y las de la Agencia Tributaria. Los organismos que
más suelo venden no subastan así:

- el **INVIED** enajena cuarteles y solares de Defensa por «subasta pública con
  proposición económica al alza en sobre cerrado»;
- **ADIF** y **SEPES** venden parcelas sobrantes e industriales por pliego;
- los **ayuntamientos** sacan solares del patrimonio municipal de suelo.

Sus anuncios no tienen `SUB-`, así que el filtro los descartaba uno por uno: de
ahí que el panel mostrara 129 anuncios de subasta y 0 identificadas. La fuente
`boe_anuncios` los lee del **texto del propio anuncio**, que es un XML estable y
documentado: saca el tipo de licitación (no la fianza ni la deuda reclamada), la
superficie con sus unidades, el municipio, la provincia y la referencia
catastral, y parte en lotes los anuncios que sacan varias fincas de golpe.

```bash
curl -X POST "https://<tu-app>.fly.dev/api/ingest/boe-anuncios?province=Cantabria"
```

`GET /api/sources/boe-anuncios/probe?identificador=BOE-B-2026-23129` enseña el
desmenuzado de un anuncio concreto: de qué etiqueta sale el precio, qué lotes
encuentra y por qué descarta cada uno.

Dos detalles del formato real que cuestan la mitad de las fincas si se pasan por
alto. El primero: las descripciones registrales escriben la superficie en letra
tan a menudo como en cifras —«de superficie mil doscientos metros cuadrados»—,
así que se leen las dos formas. El segundo: un edicto judicial lleva
identificador `SUB-` **y** describe la finca en su texto, de modo que la misma
parcela salía dos veces, una por el portal y otra por el anuncio, con dos claves
distintas que no se podían cruzar. Gana la del portal, que trae los datos en
campos en vez de deducidos de una frase; si el portal no ha respondido, se
recoge la del anuncio, que es cuando más falta hace.

Las dos fuentes comparten un único recorrido de boletines: pedir dos veces los
mismos cien anuncios diarios era lo que hacía que el BOE empezara a cortar
peticiones a mitad de camino. El resumen de cada día distingue ahora los tres
casos que se confundían en un mismo cero —cuántos anuncios había, cuántos no se
pudieron descargar y cuántos se leyeron sin traer identificador de subasta—,
porque «0 identificadas» no decía si fallaba la red, el filtro o es que ese día
no había ninguna.

### El buscador del portal, como respaldo

Ningún portal privado ofrece lectura gratuita, pero el **Portal de Subastas del
BOE** sí: inmuebles y fincas de subastas judiciales, notariales y de Hacienda,
de toda España, con **valor de tasación, puja mínima y referencia catastral**.

```bash
curl -X POST "https://<tu-app>.fly.dev/api/ingest/boe-subastas?province=Cantabria"
```

Encaja con el propósito de la app por partida doble: en subasta el suelo suele
salir por debajo de mercado, y la referencia catastral permite cruzar cada finca
con su **geometría real** aunque la subasta no publique coordenadas — que nunca
lo hace.

Es información del sector público sujeta al régimen de reutilización de la Ley
37/2007, no scraping de un portal privado. El conector filtra a suelo (fincas
rústicas, solares, parcelas) y convierte hectáreas y áreas a metros cuadrados,
porque las fincas se describen así y confundirlas altera el precio por metro en
varios órdenes de magnitud.

`GET /api/sources/boe/probe?province=Cantabria` diagnostica el parseo, y
añadiendo `&id_sub=<id>` enseña el detalle de una subasta concreta.

### Respaldo no oficial vía RapidAPI

Además de lo anterior, la app admite proveedores de **RapidAPI** como respaldo:

| Portal | Proveedor | Cubre |
|---|---|---|
| Idealista | `happyendpoint/idealista17` | **Preferido.** Idealista Data API; plan gratuito de 500 peticiones/mes |
| Idealista | `oneapiproject/idealista-api1` | Segunda alternativa |
| Fotocasa | `happyendpoint/fotocasa3` | **El hueco que no se puede cubrir por vía oficial** |

Para `pisos.com` y `habitaclia` no se encontró ningún proveedor en RapidAPI.

**Claves.** RapidAPI entrega una clave por aplicación y lo habitual es crear una
por API suscrita, así que hay dos niveles: `RAPIDAPI_KEY` como respaldo común y
`RAPIDAPI_KEYS` para dar una clave propia a cada fuente
(`rapidapi_idealista17=...,rapidapi_fotocasa=...`).

**Coordenadas.** La búsqueda de estos proveedores **no siempre devuelve latitud
y longitud** — la de `idealista17` no lo hace. Exigirlas dejaría el resultado
vacío sin explicar por qué, así que el anuncio se conserva y se sitúa en su
municipio, usando los datos que ya están en la base y sin gastar cuota. Queda
marcado como aproximado, la ingesta informa de cuántos se resolvieron así, y la
ficha avisa de que sus distancias a playa o montaña son orientativas.

> **Estas APIs no son oficiales.** Son revendedores que extraen los datos de los
> portales, cuyas condiciones de uso prohíben la extracción automatizada. Se
> integran como respaldo explícito porque es una decisión consciente, no por
> descuido: van **desactivadas mientras no haya `RAPIDAPI_KEY`**, y la API
> oficial de Idealista siempre tiene prioridad cuando está configurada.
> Además se rompen cuando el portal cambia su web, por eso host, ruta y mapeo
> de campos son configurables sin tocar código.

El orden de la cadena es: **oficial primero, respaldo después**. La respuesta de
`/api/ingest/listings` dice en `source_used` cuál acabó sirviendo los datos y en
`fallbacks_tried` por qué fallaron los anteriores.

Como el esquema de estos proveedores no está documentado de forma fiable, hay un
endpoint de diagnóstico que lo comprueba contra datos reales:

```bash
curl "https://<tu-app>.fly.dev/api/sources/rapidapi/probe?source=rapidapi_idealista" | jq
```

Devuelve el estado HTTP, las claves de la respuesta, cuántos anuncios se
localizaron y cómo quedó el primero tras el mapeo, además de una pista sobre qué
ajustar. Nunca expone la clave.

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
AirROI), no de porcentajes inventados.

Como InsideAirbnb sólo publica volcados de una docena de ciudades españolas
—y no de la costa cantábrica ni de los pueblos de montaña, que es el perfil
que busca esta aplicación—, hay una cadena de respaldo en tres escalones:

1. **InsideAirbnb** del propio municipio, o del municipio con datos más
   cercano dentro de 40 km. Es el único con precio por anuncio y calendario.
2. **Intensidad turística del INE**: plazas de alquiler turístico por cada mil
   habitantes del municipio, en tramos. No es un precio observado y se marca
   como estimación, pero distingue Noja de un pueblo del interior, que con los
   valores de respaldo salían idénticos.
3. **Valores de respaldo conservadores**, dicho explícitamente.

La ficha dice siempre en qué escalón está y con qué muestra.

Produce cuenta de explotación anual, estacionalidad mes a mes según el tipo de
zona (costa, montaña, ciudad o rural), proyección a diez años con amortización
del préstamo, y métricas de retorno: rentabilidad bruta y neta, *cash-on-cash*,
periodo de recuperación, VAN y TIR.

La ocupación se estima por dos vías independientes —noches inferidas de las
reseñas y hueco cerrado del calendario— y se toma **la menor**, que es la
prudente.

### Simulación visual

**Selector de modelos como fichas.** Cada vivienda se muestra con un esquema
**dibujado a escala a partir de sus dimensiones reales** —alzado acotado y planta
con el reparto de salón, baños y dormitorios—, junto a superficie, dimensiones,
acabado, aislamiento, garantía, plazo de entrega y desglose del precio del módulo.
**Fotos reales.** La app puede **traer la foto del propio anuncio de referencia**:

```bash
curl -X POST https://<tu-app>.fly.dev/api/prefab-models/plegable-40-2dorm/image/fetch
# o todas de golpe:
curl -X POST https://<tu-app>.fly.dev/api/prefab-models/images/fetch-all
```

En el simulador hay un botón «Traer foto del anuncio» en cada ficha que hace lo
mismo. **La descarga la hace el servidor, no el navegador**: lee la `og:image`
de la página del producto, se la trae con las cabeceras adecuadas y la guarda
en el volumen. Enlazar la imagen de la tienda daría fotos rotas, porque
rechazan las peticiones cuyo `Referer` no es el suyo.

Si el vendedor bloquea la descarga —Amazon lo hace a menudo—, la respuesta lo
dice y queda la vía manual:

```bash
curl -X POST -F "file=@mi-foto.jpg;type=image/jpeg" \
  https://<tu-app>.fly.dev/api/prefab-models/plegable-40-2dorm/image
```

Se guarda en el volumen, así que sobrevive a los despliegues. Admite JPEG, PNG y
WebP hasta 6 MB; se rechaza cualquier otro tipo, SVG incluido, porque puede
llevar scripts. Se quita con `DELETE` sobre la misma ruta. También se puede
enlazar una URL externa con `PREFAB_IMAGES=modelo=https://...`.

**Los anuncios inmobiliarios ya traen su foto**: Idealista y los proveedores de
RapidAPI la devuelven en la respuesta, y el buscador la muestra como miniatura
en cada resultado.

**El esquema no desaparece cuando hay foto**: queda desplegable debajo. Los dos
sirven para cosas distintas — la foto enseña el acabado, el esquema las
proporciones y el reparto interior a escala— y la app no trae fotos de fábrica
porque son material del fabricante.

**Parcela real del Catastro.** El simulador consulta el Catastro con las
coordenadas y trae el polígono real de la parcela, con su referencia catastral y
su superficie oficial. Eso cambia la simulación por completo: los retranqueos y
el sitio donde cabe la casa dependen de la forma de la parcela, no de un
rectángulo equivalente. Si el Catastro no responde o no hay parcela en ese punto,
se dibuja el rectángulo y **se dice por qué**. La superficie oficial puede además
sustituir a la tecleada, porque es la que usarán notaría, registro y el ITP.

**Punto de partida.** El simulador arranca en Noja (Cantabria) y ofrece presets
de costa y montaña, para poder probarlo sin teclear coordenadas. Cada preset
lleva su comunidad autónoma, de modo que el ITP de la compra sale bien desde el
primer cálculo.

**Vista 3D.** Sol con la posición real para la latitud y la fecha, así que las
sombras dicen algo: se ve si la terraza queda soleada, que es el criterio con el
que se eligió dónde colocar la casa. Materiales con textura procedural (césped,
tarima, grava), cubierta con alero y peto, ventanal con carpintería, barandilla
en la terraza y arbolado de dos especies.

**Cuatro vistas del resultado**: plano acotado en SVG, vista 3D orbitable,
la implantación sobre el mapa (parcela, área edificable, casa, terraza, piscina y
aparcamiento georreferenciados) y la ficha del modelo elegido.

### Motor de implantación 2D y 3D
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

La app ya está declarada en `fly.toml` como `real-state-invest-reccomendator`
en la región `ams`, tal y como la generó `fly launch`. El volumen **debe crearse
en esa misma región** o el despliegue falla al montarlo.

```bash
fly auth login
fly volumes create investment_data --region ams --size 1
fly secrets set CONTACT_EMAIL="tu@email.com"
fly secrets set IDEALISTA_API_KEY="..." IDEALISTA_API_SECRET="..."   # opcional
fly deploy

# Comprueba el acceso real a las fuentes, ya con salida a internet sin restricciones
curl https://real-state-invest-reccomendator.fly.dev/api/sources/health | jq .summary

# Carga inicial de datos
fly ssh console -C "python -m scripts.seed municipios"
fly ssh console -C "python -m scripts.seed pois"
fly ssh console -C "python -m scripts.seed alquiler"
```

#### Despliegue automático desde GitHub Actions

El repositorio incluye `.github/workflows/deploy.yml`: pasa los tests, comprueba
que la app arranca y despliega en Fly en cada push a `main`. Para activarlo basta
con crear el token una vez y guardarlo como secreto del repositorio:

```bash
fly tokens create deploy      # copia el valor
# GitHub -> Settings -> Secrets and variables -> Actions -> New secret
#   Nombre: FLY_API_TOKEN
```

Sin ese secreto el workflow no falla: ejecuta los tests y **omite** el despliegue
con un aviso, de modo que el repositorio sigue en verde mientras no haya cuenta
de Fly. Tras cada despliegue correcto sondea `/api/sources/health` y publica el
estado de cada fuente en el resumen de la ejecución.

El workflow deja la app entera en pie, sin `flyctl` en tu máquina:

1. **Crea el volumen si no existe**, en la región que diga `primary_region`.
   Como el `fly.toml` declara `[[mounts]]`, sin volumen el despliegue aborta.
2. **Despliega.**
3. **Asigna IP pública si falta** (IPv6 y IPv4 compartida). Este paso importa
   más de lo que parece: sin IP asignada Fly no publica `<app>.fly.dev` y el
   dominio da `NXDOMAIN` aunque el despliegue haya terminado bien. Desplegar con
   `--image` sobre una app existente no las asigna automáticamente. Se pide la
   IPv4 **compartida** a propósito, porque la dedicada se factura aparte.

Los tres pasos comprueban antes de actuar, así que se pueden repetir sin
duplicar volúmenes ni IPs. Lo único que hay que crear a mano una vez es la
propia app (`fly launch` desde la web de Fly).

**La base de datos no necesita servidor aparte**: SQLite sobre un volumen de
Fly. La máquina se suspende sin tráfico y arranca con la primera petición, así
que su coste tiende a cero, pero **el volumen se factura siempre** (del orden de
0,15 $/GB al mes) esté la app parada o no. Gratuito es no pagar un Postgres
gestionado, no que el despliegue salga a cero.

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

# Demanda turística de todos los municipios de España (libre, sin clave)
curl -X POST localhost:8000/api/ingest/ine-turismo

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
