# Scraper de comentarios de Instagram

Baja los comentarios de publicaciones de Instagram (posts y reels) a un
`.jsonl` deduplicado, con una tabla de auditoría que dice, post por post, cuántos
comentarios declara Instagram y cuántos se pudieron extraer.

Está pensado para **corridas largas y reanudables**: se puede cortar en cualquier
momento (Ctrl+C, bloqueo de Instagram, corte de luz) y al volver a correr retoma
donde quedó, sin duplicar nada.

- **Pasada 1** (`scraper_comentarios_ig.py`): comentarios raíz de cada post.
- **Pasada 2** (`pasada_respuestas.py`): respuestas de los hilos, con el
  comentario padre y el ID numérico del autor explícitos.
- **Metadatos** (`meta_posts.py`): hora de publicación, likes y comentarios
  declarados de una lista de posts, sin bajar comentarios (una carga por post).

---

## ⚠ Leer antes de correr

Instagram sirve los comentarios **detrás de sesión** (login). Eso implica:

- **Riesgo de baneo** de la cuenta cuya cookie uses. Usá una cuenta
  **descartable**, nunca la personal.
- **Va contra los Términos de Servicio** de Instagram. Corré bajo tu propio
  criterio y responsabilidad, sobre datos públicos y en volumen moderado.
- **Es frágil**: Instagram cambia su interfaz sin avisar y detecta
  automatización. Delays altos y corridas chicas.
- **Datos personales**: los comentarios llevan el handle de su autor. Si los vas
  a publicar o analizar, seudonimizá los usuarios y no subas los datos crudos a
  un repositorio.

El scraper **no** resuelve CAPTCHAs ni rota identidades. Cuando detecta un
bloqueo, **guarda todo y corta**: lo correcto es esperar horas, no insistir.

---

## Instalación

Requiere Python 3.10 o superior.

```bash
pip install -r requirements.txt
playwright install chromium
```

## Sesión (la cookie)

El scraper nunca usa usuario y contraseña: usa la cookie de una sesión ya abierta.

1. Entrá a instagram.com con tu cuenta **descartable** en un navegador.
2. F12 → pestaña **Application** (o **Storage**) → **Cookies** →
   `https://www.instagram.com`.
3. Copiá los valores de `sessionid` y `csrftoken` (y, si están, `ds_user_id`,
   `mid` e `ig_did`).
4. Pasalos de una de estas dos formas:

   **Opción A — archivo**: copiá `secrets.example.json` a `secrets.json` y
   completalo. `secrets.json` está en `.gitignore`: no se versiona.

   **Opción B — variables de entorno** (PowerShell):
   ```powershell
   $env:IG_SESSIONID = "..."
   $env:IG_CSRFTOKEN = "..."
   ```
   También se leen `IG_DS_USER_ID`, `IG_MID` e `IG_DID`. Las variables de
   entorno pisan a `secrets.json`.

## Configuración

Copiá `config.example.json` a `config.json` y editá:

| Clave | Qué es |
|---|---|
| `post_urls` | URLs de posts o reels a scrapear. Es el modo recomendado. |
| `cuentas` | Usernames: se scrapean sus publicaciones más recientes. ⚠ Ver «Qué puede romperse», punto 5. |
| `max_posts_por_cuenta` | Tope de posts por cuenta. `null` = todos. |
| `max_comentarios_por_post` | Tope de comentarios **raíz** por post. `null` = todos. |
| `max_minutos_por_post` | Corta un post tras N minutos y sigue con el siguiente (queda `ok_corte_tiempo` y se reintenta en la próxima corrida). `null` = sin límite. |
| `incluir_respuestas` | `true` despliega los hilos dentro del panel del post. **Dejalo en `false` y usá la pasada 2**: ver «Respuestas». |
| `delay_entre_requests` | Rango de segundos entre scrolls. Subilo si aparecen bloqueos. |
| `delay_entre_posts` | Rango de segundos entre posts (y entre hilos en la pasada 2). |
| `max_reintentos` | Reintentos por error transitorio (solo en el descubrimiento de posts por cuenta). |

## Uso

```bash
python scraper_comentarios_ig.py
```

- Se puede **cortar en cualquier momento**: la cola se guarda tras cada post y
  los comentarios cada 250. Volver a correr retoma donde quedó.
- `--estado`: muestra qué posts ya están y cuáles faltan. No abre el navegador
  ni toca Instagram.
- `--reset`: reconstruye la cola desde `config.json`.
- `--config otro.json --salida datos_otro/`: otro proyecto, en otra carpeta.
- `--headful`: muestra la ventana del navegador (para debuggear).
- Para **empezar de cero**: borrá la carpeta de salida (`data/` por defecto).

Las URLs se pueden **acumular** en `config.json`: un post ya resuelto no se
vuelve a scrapear. Un post cuenta como resuelto si `estado.csv` lo dejó en
`ok_completo`/`ok_tope`, **o** si en `comentarios.jsonl` ya hay
`max_comentarios_por_post` raíces suyas.

### Respuestas (pasada 2)

Después de la pasada 1:

```bash
python pasada_respuestas.py                 # todos los hilos de todos los posts
python pasada_respuestas.py --top-hilos 2   # solo los 2 hilos con más respuestas por post
python pasada_respuestas.py --media <media_id>
```

Abre el permalink de cada comentario raíz con respuestas, clickea "Ver
respuestas (N)" hasta agotarlo y toma las respuestas del JSON que la propia
página recibe. Escribe al mismo `comentarios.jsonl`. Es reintentable hilo por
hilo (`data/respuestas_hechas.json`); `--reset` reprocesa todo. Si usaste
`--salida` en la pasada 1, pasá la misma carpeta acá.

Por qué no `incluir_respuestas: true`: desplegar hilos dentro del panel del post
obliga a tener miles de comentarios en un solo DOM (puede llegar a varios GB) y
no es reintentable por hilo. Además, si se usan las dos vías, la primera gana
el dedup y las respuestas quedan en calidad DOM (sin `autor_pk`, con el padre
inferido por indentación).

### Metadatos de posts

```bash
python meta_posts.py <url> <url> --csv posts_meta.csv
```

Una navegación por post, sin scroll. Sirve para decidir qué scrapear antes de
gastar una corrida.

### Piloto antes de la corrida grande

1. Poné 1–2 URLs en `config.json` y `max_comentarios_por_post: 100`.
2. Corré y abrí `data/comentarios.jsonl`.
3. Compará contra el post real: autores, textos, fechas y likes.

---

## Salida (en `data/`, o en `--salida`)

| Archivo | Qué es |
|---|---|
| `comentarios.jsonl` | Un comentario por línea, deduplicado por `comentario_pk`. Las respuestas llevan `parent_pk`; los raíz lo tienen en `null`. |
| `estado.csv` | Una fila **por corrida sobre un post** (append-only): métricas del post, cuántos comentarios se sacaron y cómo terminó. |
| `posts.csv` | Publicaciones descubiertas por cuenta (solo en modo `cuentas`). |
| `cola_pendiente.json` | La cola de trabajo. Vacía = corrida completa. |
| `respuestas_hechas.json` | Hilos ya bajados por la pasada 2. |
| `capturas/<shortcode>.png` | Captura del post (la página ya está abierta, no cuesta una carga extra). |
| `logs/` | Un log por corrida. |

### Campos de cada comentario

| Campo | Qué es |
|---|---|
| `comentario_pk` | Clave única. Es el ID real de Instagram; solo si el DOM no lo expone cae a un hash de `media_id\|autor\|texto\|fecha`. |
| `comentario_id` | ID real de Instagram (del permalink `/p/SHORTCODE/c/<id>/`). |
| `media_id`, `shortcode`, `cuenta_post` | El post al que pertenece. |
| `parent_pk` | ID del comentario raíz si es una respuesta; `null` si es raíz. |
| `autor` | Handle del autor. |
| `autor_pk` | ID numérico del autor. **Solo en las respuestas de la pasada 2**; en los raíz queda `null` (el DOM no lo expone). |
| `autor_verificado` | Badge de verificado. |
| `texto` | Texto del comentario. |
| `tiene_imagen`, `alt_imagen` | Sticker o GIF (el avatar del autor no cuenta). `null` en la pasada 2. |
| `created_at` | Fecha exacta en ISO UTC (del atributo `datetime` del `<time>`, no del "7 h"). |
| `like_count`, `reply_count` | Likes y respuestas declaradas. |
| `posicion_dom`, `carga_id` | Orden en que Instagram sirvió el comentario dentro de una carga de página. Leelos juntos: el contador se reinicia en cada carga. |
| `fase`, `origen` | 1 = scroll de raíces, 2 = hilos en el panel, 3 = pasada 2. `panel` / `permalink` / `api_child_comments`. |
| `x_bloque`, `umbral_indentacion`, `min_x` | Evidencia de la decisión raíz/respuesta en el DOM (por indentación en píxeles). `null` en la pasada 2, donde el padre viene dado. |
| `scrapeado_en` | Momento de la captura. |

### Campos de `estado.csv`

| Campo | Qué es |
|---|---|
| `post_like_count` | Likes al momento del scrapeo. ⚠ Instagram los **redondea** ("74K"). |
| `post_comment_count` | Comentarios que declara Instagram (exacto). |
| `post_meta_crudo` | El string original del que salen los dos anteriores. |
| `caption` | Texto del post. ⚠ Instagram lo trunca si es largo, y algunos posts no lo traen. |
| `taken_at`, `edad_post_horas` | Hora de publicación y horas entre publicación y captura. |
| `n_extraidos`, `n_raiz`, `n_respuestas` | Lo que se bajó **en esta corrida**. |
| `cobertura` | `n_extraidos / post_comment_count`. **Leela siempre**. |
| `estado` | `ok_completo`, `ok_tope`, `ok_truncado_por_ig`, `ok_corte_tiempo`, `ok_corte_seguridad`, `error_inesperado`. Solo `ok_completo` y `ok_tope` cuentan como resueltos. |
| `t_inicio`, `t_fin`, `duracion_seg`, `iteraciones`, `pasadas_fase2` | Duración y esfuerzo. |

⚠ `estado.csv` describe **la corrida, no el post**: si un post se bajó en dos
tandas, hay dos filas y ninguna tiene el total. Para el total por post, contá el
`.jsonl` agrupando por `media_id`.

⚠ Si cambiás las columnas, `append_csv` **corta** con un error que pide rotar
el archivo (`estado_v1.csv`), en vez de escribir filas desalineadas.

---

## Qué puede romperse (y cómo se ve)

0. **⚠ Límite blando de Instagram (`ok_truncado_por_ig`)**: el más engañoso,
   porque no parece un error. Tras horas de scrapeo, Instagram responde "no hay
   más comentarios" (HTTP 200, sin challenge) aunque el post declare miles. No
   es un bug del scraper: **pará varias horas** y subí los delays. Se marca
   `ok_truncado_por_ig` cuando la cobertura queda por debajo del 50%.
   Para distinguirlo de un problema de un post puntual: el límite blando afecta
   a **toda** la corrida; un post trabado deja a los demás intactos.
1. **Cookie vencida o cuenta marcada**: el navegador termina en
   `/accounts/login` o `/challenge/` y el log dice `⚠ BLOQUEO`. Copiá la cookie
   de nuevo (o cambiá de cuenta).
2. **Instagram cambió el layout**: un post da `0 comentarios nuevos
   (ok_completo)` pero en el navegador tiene. Los puntos de anclaje están en
   `_JS_EXTRAER_COMENTARIOS` (cada comentario se ancla en su `<time>`) y
   `panelComentarios()` (el panel con `overflowY: auto|scroll` que más `<time>`
   contiene).
3. **La página sale en inglés**: el parser busca literales en español
   ("Responder", "Me gusta"). Por eso las URLs llevan `?hl=es-la`. Si ves 0
   comentarios, verificá que ese parámetro siga funcionando.
4. **Cuenta privada**: solo se scrapea si la cuenta logueada la sigue.
5. **Modo `cuentas`**: depende de `web_profile_info` (username → user_id), que
   Instagram viene respondiendo con error 400. Si falla, usá `post_urls`.

## Limitaciones

- **`n_extraidos` va a ser menor que `post_comment_count`**, y es normal.
  Instagram cuenta comentarios que no renderiza (borrados, spam, filtrados) y,
  en la pasada 1, las respuestas de los hilos. En un post grande medido: 8.691
  declarados, 5.338 raíz extraídos (cobertura 0,61), ~1.700 respuestas en
  hilos, ~1.660 que Instagram no muestra. **Reportá la cobertura; no asumas que
  tenés el universo de comentarios.**
- **`ok_completo` no garantiza completitud.** Mirá siempre `cobertura`.
- **Un hilo tampoco se baja "completo"**: un hilo que declara 24 respuestas
  puede servir 21 y cerrar el paginador. Se loguea `hilo INCOMPLETO: N de M`.
- **`autor_pk` existe para unas filas y no para otras** (respuestas sí, raíces
  no).
- El orden de los comentarios lo decide Instagram (no es cronológico).
- **Concentración de voces**: pocos autores pueden aportar muchos comentarios.
  Si vas a tratar cada comentario como una opinión, agrupá por `autor` antes.

---

## Pruebas offline

```bash
python pruebas_offline.py
```

Arman un DOM sintético que imita el de Instagram y corren contra él el JS y el
parser reales: extracción, indentación, paginador de respuestas, resume, cola,
caption, mapeo del JSON de respuestas y una corrida de punta a punta. No tocan
Instagram (no gastan la cuenta). Tardan ~1 minuto.

Lo que no pueden probar es que Instagram siga sirviendo el DOM igual: eso solo
lo dice una corrida real. Si Instagram cambia, volcá el HTML de un post real
(`page.content()`) y actualizá las muestras.

## Cómo funciona (resumen técnico)

- **Navegador, no API.** Instagram dejó de servir los comentarios por el
  endpoint REST `api/v1/media/{id}/comments/` y los mueve por GraphQL con
  `doc_id` no documentados que rotan. En vez de reconstruir esas queries, se
  deja que Playwright (Chromium headless) cargue la página con la cookie
  inyectada y se lee el DOM ya renderizado.
- **Dos fases en el panel.** Primero se scrollea hasta cargar todos los raíz;
  recién después (si se pide) se despliegan hilos. Intercalarlas hace que
  Instagram deje de cargar más comentarios.
- **Fin de la carga.** No alcanza con "N rondas sin comentarios nuevos" (daba
  falsos completos): además el panel tiene que dejar de avanzar, con backoff
  creciente y más paciencia cuanto más lejos se está del total declarado.
- **Performance del DOM.** Con miles de comentarios cargados, se separan
  lecturas y escrituras de layout, se cachea el panel (con re-búsqueda si React
  lo reemplaza) y se evita `innerText` sobre ancestros grandes.
- **Respuestas por red.** La pasada 2 no lee el DOM: escucha la respuesta de
  `/child_comments/` que la página pide al clickear "Ver respuestas (N)". El
  botón se elige por *ancla* (el que está entre el comentario enfocado y el
  siguiente), para no abrir hilos ajenos.
